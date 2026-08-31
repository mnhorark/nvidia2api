"""前端静态托管（B1 all-in-one 镜像）测试。

B1 的根因是"没人跑过 docker build"，因此这里用 Django test client 把
URLconf + frontend_views 完整跑一遍：静态资源、前端路由回退、目录穿越防护、
以及"未知 API 路径不能被 index.html 吞掉"这条最容易踩的坑。

通过 monkeypatch 把 FRONTEND_DIR 指向临时目录，不依赖真实构建产物。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from django.test import TestCase, override_settings

from api import frontend_views


@override_settings(FRONTEND_DIR=None)  # 由 setUp 动态替换
class FrontendHostingTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="n2a-frontend-"))
        (self.tmp / "index.html").write_text("<html>ROOT</html>", encoding="utf-8")
        (self.tmp / "dashboard").mkdir()
        (self.tmp / "dashboard" / "index.html").write_text(
            "<html>DASH</html>", encoding="utf-8")
        (self.tmp / "_next" / "static").mkdir(parents=True)
        (self.tmp / "_next" / "static" / "app.js").write_text(
            "console.log(1)", encoding="utf-8")
        (self.tmp / "secret.txt").write_text("TOP SECRET", encoding="utf-8")

        self._patcher = unittest.mock.patch.object(
            frontend_views, "frontend_dir", lambda: self.tmp)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    @staticmethod
    def body(resp) -> bytes:
        """FileResponse 没有 `.content`，需从 streaming_content 取。"""
        return b"".join(resp.streaming_content)

    def test_root_serves_index(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"ROOT", self.body(resp))

    def test_route_directory_serves_its_index(self):
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"DASH", self.body(resp))

    def test_asset_served_with_immutable_cache(self):
        resp = self.client.get("/_next/static/app.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("immutable", resp["Cache-Control"])

    def test_html_not_cached(self):
        resp = self.client.get("/dashboard")
        self.assertEqual(resp["Cache-Control"], "no-cache")

    def test_unknown_client_route_falls_back_to_root_index(self):
        """前端路由直达/刷新（如 /channels/123）必须回退到根 index.html。"""
        resp = self.client.get("/channels/123")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"ROOT", self.body(resp))

    def test_path_traversal_never_leaks_files(self):
        """穿越探测的结果只能是 404，或 SPA 外壳——绝不是导出目录外的文件内容。

        注：`//../x` 这类以双斜杠起头的路径会被 WSGI 层在根处归一化掉 `..`，
        最终落到 SPA 回退（200 + index.html），这同样安全，因此断言的是
        "绝不泄露外部文件内容"而非"必须 404"。
        """
        index = (self.tmp / "index.html").read_bytes()
        for path in ("../backend/config/settings.py",
                     "..%2F.env",  # URL 编码过的 ..
                     "_next/../../backend/config/settings.py",
                     "/../backend/config/settings.py",
                     "..\\windows\\win.ini"):
            with self.subTest(path=path):
                resp = self.client.get(f"/{path}")
                if resp.status_code == 200:
                    body = self.body(resp)
                    self.assertEqual(
                        body, index,
                        f"{path} 返回了非 index.html 的内容，疑似目录穿越")
                else:
                    self.assertEqual(resp.status_code, 404)

    def test_direct_traversal_is_rejected(self):
        """`_resolve` 对明确的越界路径必须返回 TRAVERSAL（进而 404，不回落 SPA）。"""
        for rel in ("../x", "a/../../x", "/etc/passwd", "..", "../",
                    "..%2F.env",          # 编码过的 ..
                    "C:/windows/win.ini"):  # 盘符绝对路径
            with self.subTest(rel=rel):
                self.assertIs(frontend_views._resolve(rel),
                              frontend_views.TRAVERSAL)

    def test_plain_relative_path_is_not_false_positive(self):
        """正常路径不能被误判为穿越（否则整个静态托管会 404）。"""
        self.assertEqual(frontend_views._resolve("dashboard"),
                         self.tmp / "dashboard" / "index.html")
        self.assertEqual(frontend_views._resolve("_next/static/app.js"),
                         self.tmp / "_next" / "static" / "app.js")

    def test_unknown_api_path_is_not_swallowed_by_spa_fallback(self):
        """未知 /api/* 必须 404，不能返回 index.html（客户端会当 JSON 解析失败）。"""
        resp = self.client.get("/api/admin/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_v1_path_is_not_swallowed(self):
        resp = self.client.get("/v1/nope")
        self.assertEqual(resp.status_code, 404)

    def test_missing_asset_returns_404(self):
        resp = self.client.get("/_next/static/nope.js")
        self.assertEqual(resp.status_code, 404)

    def test_root_without_build_output_gives_actionable_hint(self):
        """纯 API 部署（无前端产物）时给出可操作提示，而不是静默 404。"""
        shutil.rmtree(self.tmp, ignore_errors=True)
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"nvidia2api API is running", resp.content)
