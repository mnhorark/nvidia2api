"""单进程契约（services/process_guard）守卫测试。

核心要证明的不是"代码能跑"，而是**第二个实例真的会被拒绝**——
这条契约一旦失效是静默的（闸门只是不再起作用，不报错），所以必须有
跨进程的真实断言，而不是只测同进程的短路分支。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from django.test import override_settings

from services import process_guard


def _reset_handle():
    """释放本进程持有的锁（测试隔离用）。"""
    fd = process_guard._LOCK_HANDLE
    if fd is not None:
        try:
            os.close(fd)   # 关闭 fd 即释放 OS 级文件锁
        except OSError:
            pass
    process_guard._LOCK_HANDLE = None


class ServerProcessDetectionTests(TestCase):
    """`_is_server_process()` 决定守卫与自动迁移是否生效，必须逐形态覆盖。

    真实教训：初版只匹配 `argv[0]` 的文件名，而 **start.bat 的两个启动分支
    用的都是 `python -m uvicorn`**——那种形态下 argv[0] 是
    `.../uvicorn/__main__.py`，文件名只剩 `__main__.py`，守卫会静默不生效。
    守卫静默不生效和守卫不存在是同一件事，所以这里把每种命令行形态钉住。
    """

    def _detect(self, argv):
        from apps.core import apps
        with patch.object(sys, "argv", argv):
            return apps._is_server_process()

    def test_console_script_form(self):
        self.assertTrue(self._detect(["/usr/local/bin/uvicorn",
                                      "config.asgi:application"]))

    def test_dash_m_module_form(self):
        """`python -m uvicorn`：Windows 本地部署的实际形态。"""
        self.assertTrue(self._detect([
            "C:/Python314/Lib/site-packages/uvicorn/__main__.py",
            "config.asgi:application", "--host", "0.0.0.0"]))

    def test_gunicorn_form(self):
        self.assertTrue(self._detect(["gunicorn", "config.wsgi:application"]))

    def test_runserver_form(self):
        self.assertTrue(self._detect(["manage.py", "runserver", "0.0.0.0:8000"]))

    def test_one_off_commands_are_not_server_processes(self):
        for argv in (["manage.py", "migrate"],
                     ["manage.py", "cleanlogs", "--days", "30"],
                     ["python", "-m", "pytest", "tests"],
                     ["manage.py", "shell"]):
            self.assertFalse(self._detect(argv), argv)

    def test_plain_script_is_not_server_process(self):
        self.assertFalse(self._detect(["diag.py"]))


class ProcessGuardTests(TestCase):
    def setUp(self):
        _reset_handle()
        os.environ.pop(process_guard.ENV_OVERRIDE, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        _reset_handle()
        os.environ.pop(process_guard.ENV_OVERRIDE, None)

    def _settings(self):
        return override_settings(DATA_DIR=str(self.data_dir))

    def test_lock_file_created_with_pid(self):
        with self._settings():
            self.assertTrue(process_guard.acquire_singleton_lock())
        lock = self.data_dir / ".gateway.lock"
        self.assertTrue(lock.exists())
        # 必须走 _read_holder_pid：PID 存在被锁区间之后，从 0 直接 read()
        # 在 Windows 上会被拒绝（LockFile 对锁定区间连读都拒）
        self.assertEqual(process_guard._read_holder_pid(lock), str(os.getpid()))

    def test_second_descriptor_on_same_file_is_refused(self):
        """OS 级语义前提：同一文件的第二个描述符拿不到独占锁。

        守卫的全部有效性押在这条上——若某平台上 flock/LockFile 对同进程
        第二个 fd 也放行，跨进程检测同样会失效。
        """
        with self._settings():
            process_guard.acquire_singleton_lock()
        path = self.data_dir / ".gateway.lock"
        fd2 = os.open(str(path), os.O_RDWR)
        try:
            self.assertFalse(process_guard._try_lock(fd2))
        finally:
            os.close(fd2)

    def test_env_override_skips_guard_without_locking(self):
        os.environ[process_guard.ENV_OVERRIDE] = "true"
        with self._settings():
            # 返回 False = 守卫未启用；且不得留下锁
            self.assertFalse(process_guard.acquire_singleton_lock())
        self.assertIsNone(process_guard._LOCK_HANDLE)
        self.assertFalse((self.data_dir / ".gateway.lock").exists())

    def test_second_instance_in_subprocess_is_refused(self):
        """真实跨进程：子进程先持锁，本进程必须抛 AlreadyRunning。"""
        child = textwrap.dedent(f"""
            import os, sys, time
            sys.path.insert(0, {str(Path(process_guard.__file__).parent.parent)!r})
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
            os.environ["DATA_DIR"] = {str(self.data_dir)!r}
            os.environ["ALLOW_DEFAULT_CREDENTIALS"] = "true"
            import django
            django.setup()
            from services.process_guard import acquire_singleton_lock
            acquire_singleton_lock()
            print("HELD", flush=True)
            time.sleep(30)
        """)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen([sys.executable, "-c", child],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", env=env,
                                cwd=str(Path(process_guard.__file__).parent.parent))
        try:
            # 等子进程真正持锁（打印 HELD），否则断言会因竞态假通过
            deadline = time.time() + 60
            held = False
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        self.fail(f"子进程未起来：{line!r}")
                    continue
                if "HELD" in line:
                    held = True
                    break
            self.assertTrue(held, "子进程未在超时内持有锁")

            with override_settings(DATA_DIR=str(self.data_dir)):
                with self.assertRaises(process_guard.AlreadyRunning) as ctx:
                    process_guard.acquire_singleton_lock()
            msg = str(ctx.exception)
            self.assertIn("nvidia2api", msg)
            # 诊断信息里要能看到持有者 PID，否则运维无从判断该杀谁
            self.assertIn(str(proc.pid), msg)
            # 被拒绝的一方不得留下句柄
            self.assertIsNone(process_guard._LOCK_HANDLE)
        finally:
            proc.kill()
            proc.wait(timeout=30)
