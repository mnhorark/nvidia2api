"""后端管理路径的源码形态守卫。

与 `test_review_fixes.NoNPlusOneOnBulkAdminPaths` 的**运行时 SQL 条数**断言互补：
那边才是"确实只打常数条查询"的证明；这里只钉用整文件文本就能可靠检查的事。

刻意不做的事：不用括号配对去切函数体。`def sync_models(...) -> dict:` 带返回注解、
内部又有嵌套 def，按括号定位会切错位置（实测返回过 `{status_code}` 这种片段）。
需要函数体级别的断言时，用运行时测试而不是源码正则。
"""
import re
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _blank(m: re.Match) -> str:
    """等长抹成空格（保留换行），这样偏移量仍对得上原文。"""
    return "".join("\n" if ch == "\n" else " " for ch in m.group(0))


_DOCSTRING = re.compile(r'"""[\s\S]*?"""')
_HASH_COMMENT = re.compile(r"(?m)^(\s*)#.*$")


def _code(rel: str) -> str:
    """读后端文件并抹掉注释。

    必须抹：这些"不得出现 X"的断言会被**解释为什么删掉 X 的注释**误触发——
    本轮就撞了两次（`get_or_create` 与 `bulk_create` 都出现在警示性注释里）。

    注意不能复用 test_frontend_guards 的 `_code()`：那个只处理 JS 的 `//` 与
    `/* */`，这里是 Python 的 `#` 与三引号 docstring。
    """
    src = (BACKEND / rel).read_text(encoding="utf-8")
    src = _DOCSTRING.sub(_blank, src)
    return _HASH_COMMENT.sub(_blank, src)


class ProxyBulkWriteSecurityInvariantTests(unittest.TestCase):
    """代理批量写入绝不能改成 bulk_create。

    `Proxy.save()` 负责密码加密（幂等，已加密值跳过）。bulk_create 绕过 save()，
    会把**明文密码**写进库——而 `decrypt_secret` 有明文回落，所以运行期完全看不出
    异常，直到某次换 ENCRYPTION_KEY 才炸。这是"顺手把导入也批量化"最容易踩的坑。
    """

    def test_proxy_import_still_creates_row_by_row(self):
        code = _code("services/proxy_service.py")
        self.assertIn("Proxy.objects.create(channel=channel", code,
                      "代理导入不再逐行 create——检查是否绕过了 Proxy.save() 的加密")

    def test_proxy_service_does_not_bulk_create(self):
        code = _code("services/proxy_service.py")
        self.assertNotIn("bulk_create", code,
                         "Proxy 用 bulk_create 会绕过 save() 的密码加密")


class AdminBulkPathShapeTests(unittest.TestCase):
    """模型同步与代理导入的存量查询必须是集合式，不要退回逐行探测。"""

    def test_proxy_import_uses_a_single_existence_query(self):
        code = _code("services/proxy_service.py")
        # 逐行 exists() 是原来的 N+1 形态
        self.assertNotIn(").exists():", code, "代理导入又回到逐行 exists() 查重")
        self.assertIn('values_list("protocol", "host", "port", "username")', code,
                      "存量端点集合应一次性取回")

    def test_sync_models_does_not_get_or_create_per_model(self):
        code = _code("services/upstream_service.py")
        self.assertNotIn("get_or_create", code,
                         "模型同步又回到每个上游模型一次 get_or_create")
        self.assertIn("bulk_create", code)
        # bulk_create 不发 post_save，而 model_registry 的缓存失效靠信号
        self.assertIn("model_registry.invalidate()", code,
                      "bulk_create 后必须显式失效注册表缓存，否则新模型要等 TTL 才可见")


class AsyncUnsafePremiseTests(unittest.TestCase):
    """记录一个前提：DJANGO_ALLOW_ASYNC_UNSAFE 全局打开，框架不会报同步 ORM 违规。

    所以"async 函数里不要直接打同步 ORM"这条只能靠自觉加源码守卫。
    哪天把它关掉，这条守卫会提醒重新审视所有 async 路径。
    """

    def test_allow_async_unsafe_is_still_set(self):
        src = (BACKEND / "config/settings.py").read_text(encoding="utf-8")
        self.assertIn("DJANGO_ALLOW_ASYNC_UNSAFE", src)


if __name__ == "__main__":
    unittest.main()
