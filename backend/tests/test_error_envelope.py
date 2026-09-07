"""统一错误信封契约（api/errors.py）。

审查时管理端并存四种形态：完整 OpenAI 信封、缺 type/param 的简版、
DRF 原生 `{"detail": ...}`、以及 `{"error": "log_not_found"}`（error 是字符串）。
本文件把"只有一个形态"这件事钉成测试：任何管理端错误都必须能被
`data["error"]["message"]` 读到，否则契约再次漂移时测试会红。
"""
from __future__ import annotations

import json

from django.conf import settings
from django.test import TransactionTestCase, RequestFactory, TestCase

from api import admin_views, openai_views
from api.errors import admin_exception_handler, openai_error

ENVELOPE_KEYS = {"message", "type", "param", "code"}


def _assert_envelope(test, payload, *, status=None):
    """只锁"形态"，不锁 type 取值。

    type 的具体值允许按语义选择（例如禁用 Key 走 `authentication_error`
    而不是 `permission_error`，与 OpenAI 官方一致），所以这里只要求
    四个键齐全、message 非空。
    """
    test.assertIn("error", payload, payload)
    err = payload["error"]
    test.assertIsInstance(err, dict, f"error 必须是对象，实际 {type(err).__name__}")
    test.assertEqual(set(err.keys()), ENVELOPE_KEYS, err)
    test.assertIsInstance(err["message"], str)
    test.assertTrue(err["message"], "message 不得为空")
    test.assertIsInstance(err["type"], str)
    test.assertTrue(err["type"], "type 不得为空")
    test.assertIsInstance(err["code"], str)
    test.assertTrue(err["code"], "code 不得为空")


class AdminEnvelopeContractTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.auth = f"Token {settings.ADMIN_TOKEN}"
        from services import channel_service
        self.channel = channel_service.ensure_default_channel()

    def _get(self, view, path, pk=None, **extra):
        request = self.factory.get(path, **extra)
        return view(request, pk=pk) if pk is not None else view(request)

    def _post(self, view, path, data, **extra):
        return view(self.factory.post(path, data=json.dumps(data),
                                      content_type="application/json", **extra))

    def test_admin_404_uses_envelope(self):
        resp = self._get(admin_views.ChannelKeyDetailView.as_view(),
                         "/api/admin/keys/999999", pk=999999,
                         HTTP_AUTHORIZATION=self.auth,
                         HTTP_X_CHANNEL=self.channel.slug)
        self.assertEqual(resp.status_code, 404)
        _assert_envelope(self, resp.data)
        self.assertEqual(resp.data["error"]["code"], "not_found")

    def test_log_detail_404_uses_envelope(self):
        """曾经的离群点：`{'error': 'log_not_found'}`，error 是字符串。"""
        resp = self._get(admin_views.LogDetailView.as_view(),
                         "/api/admin/logs/999999", pk=999999,
                         HTTP_AUTHORIZATION=self.auth,
                         HTTP_X_CHANNEL=self.channel.slug)
        self.assertEqual(resp.status_code, 404)
        _assert_envelope(self, resp.data)
        self.assertEqual(resp.data["error"]["code"], "log_not_found")

    def test_missing_token_401_uses_envelope(self):
        from api import health_views
        resp = health_views.metrics(self.factory.get("/metrics"))
        self.assertEqual(resp.status_code, 401)
        payload = json.loads(resp.content)
        _assert_envelope(self, payload)

    def test_drf_validation_error_uses_envelope_with_param(self):
        """序列化校验失败：旧形态是 `{"protocol": ["..."]}`，前端只能显示
        "请求失败 (HTTP 400)"，校验信息全丢。"""
        resp = self._post(admin_views.ProxyListView.as_view(),
                          "/api/admin/proxies",
                          {"name": "p", "protocol": "gopher",   # 非法 choice
                           "host": "1.2.3.4", "port": 1080},
                          HTTP_AUTHORIZATION=self.auth,
                          HTTP_X_CHANNEL=self.channel.slug)
        self.assertEqual(resp.status_code, 400)
        _assert_envelope(self, resp.data)
        self.assertIn("protocol", resp.data["error"]["message"])
        self.assertEqual(resp.data["error"]["param"], "protocol")
        # 兼容期镜像：旧客户端读 detail 也不至于拿到空消息
        self.assertEqual(resp.data["detail"], resp.data["error"]["message"])

    def test_business_400_uses_full_envelope(self):
        """简版 `{'error': {'message','code'}}`（缺 type/param）已收敛。"""
        resp = self._post(admin_views.UserApiKeyListView.as_view(),
                          "/api/admin/api-keys", {"name": ""},
                          HTTP_AUTHORIZATION=self.auth,
                          HTTP_X_CHANNEL=self.channel.slug)
        self.assertEqual(resp.status_code, 400)
        _assert_envelope(self, resp.data)

    def test_method_not_allowed_uses_envelope(self):
        """PUT 在只有 get/patch/delete 的详情视图上 -> DRF 405 也走信封。"""
        view = admin_views.ChannelDetailView.as_view()
        resp = view(self.factory.put("/api/admin/channels/1",
                                 HTTP_AUTHORIZATION=self.auth), pk=1)
        self.assertEqual(resp.status_code, 405)
        _assert_envelope(self, resp.data)
        self.assertEqual(resp.data["error"]["code"], "method_not_allowed")


class DataPlaneEnvelopeTests(TransactionTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    async def test_invalid_user_key_401_uses_envelope(self):
        """数据面 401 此前**完全没有测试**（审查覆盖缺口 #1）。"""
        resp = await openai_views.chat_completions(
            self.factory.post("/v1/chat/completions",
                              data=json.dumps({"model": "m", "messages": [
                                  {"role": "user", "content": "hi"}]}),
                              content_type="application/json",
                              HTTP_AUTHORIZATION="Bearer sk-not-a-real-key"))
        self.assertEqual(resp.status_code, 401)
        payload = json.loads(resp.content)
        _assert_envelope(self, payload)
        self.assertEqual(payload["error"]["code"], "invalid_api_key")

    async def test_missing_authorization_header_401(self):
        resp = await openai_views.list_models(self.factory.get("/v1/models"))
        self.assertEqual(resp.status_code, 401)
        _assert_envelope(self, json.loads(resp.content))

    async def test_disabled_key_403_uses_envelope(self):
        from services import api_key_service
        rec, raw = api_key_service.create_key("disabled")
        rec.enabled = False
        rec.save()
        resp = await openai_views.list_models(
            self.factory.get("/v1/models", HTTP_AUTHORIZATION=f"Bearer {raw}"))
        self.assertEqual(resp.status_code, 403)
        _assert_envelope(self, json.loads(resp.content))


class ExceptionHandlerSafetyTests(TestCase):
    def test_non_api_exception_is_not_swallowed(self):
        """返回 None 才会让 DRF 继续向上抛——绝不能把真异常变成 400。"""
        self.assertIsNone(admin_exception_handler(ValueError("boom"), {}))

    def test_openai_error_helper_shape(self):
        resp = openai_error("nope", "some_code", 400, "invalid_request_error")
        payload = json.loads(resp.content)
        _assert_envelope(self, payload)
        self.assertEqual(resp.status_code, 400)
