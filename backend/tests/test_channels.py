"""多渠道：端点解析、渠道隔离、按渠道路由。"""
import json
from unittest.mock import patch

import httpx
from django.conf import settings
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from api import admin_views, openai_views
from apps.core.models import AIModel, Channel, ChannelKey, Proxy, RequestLog, SystemSetting
from apps.core.models import split_endpoint
from services import api_key_service, channel_service, key_service, proxy_service
from services import race_engine as race_engine_module


class SplitEndpointTests(TestCase):
    """用户直接粘贴完整 chat 端点，必须能自动拆出 base + path。"""

    def test_full_chat_urls(self):
        cases = [
            ("https://opencode.ai/zen/v1/chat/completions",
             "https://opencode.ai/zen/v1", "/chat/completions"),
            ("https://api.kilo.ai/api/gateway/chat/completions",
             "https://api.kilo.ai/api/gateway", "/chat/completions"),
            ("https://api.llm7.io/v1/chat/completions",
             "https://api.llm7.io/v1", "/chat/completions"),
            ("https://integrate.api.nvidia.com/v1/chat/completions",
             "https://integrate.api.nvidia.com/v1", "/chat/completions"),
        ]
        for raw, base, path in cases:
            self.assertEqual(split_endpoint(raw), (base, path), raw)

    def test_bare_base_url(self):
        self.assertEqual(
            split_endpoint("https://api.llm7.io/v1"),
            ("https://api.llm7.io/v1", "/chat/completions"))
        self.assertEqual(
            split_endpoint("https://api.llm7.io/v1/"),
            ("https://api.llm7.io/v1", "/chat/completions"))

    def test_channel_urls_roundtrip(self):
        """保存后 chat_url 必须还原成用户粘贴时的完整地址。"""
        for i, raw in enumerate((
            "https://opencode.ai/zen/v1/chat/completions",
            "https://api.kilo.ai/api/gateway/chat/completions",
            "https://api.llm7.io/v1/chat/completions",
        )):
            ch = Channel.objects.create(name=f"c{i}", slug=f"c{i}", base_url=raw)
            self.assertEqual(ch.chat_url, raw, raw)
            self.assertTrue(ch.models_url.endswith("/models"))


class AdminChannelApiTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.headers = {"HTTP_AUTHORIZATION": f"Token {settings.ADMIN_TOKEN}"}

    def _get(self, view, path, **extra):
        request = self.factory.get(path, **self.headers, **extra)
        return view(request)

    def _post(self, view, path, data, **extra):
        request = self.factory.post(path, data=json.dumps(data),
                                    content_type="application/json",
                                    **self.headers, **extra)
        return view(request)

    def test_create_channel_from_full_endpoint(self):
        resp = self._post(admin_views.ChannelListView.as_view(), "/api/admin/channels", {
            "name": "OpenCode Zen",
            "slug": "zen",
            "base_url": "https://opencode.ai/zen/v1/chat/completions",
            "default_rpm": 60,
        })
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["base_url"], "https://opencode.ai/zen/v1")
        self.assertEqual(resp.data["chat_url"], "https://opencode.ai/zen/v1/chat/completions")

    def test_duplicate_slug_rejected(self):
        Channel.objects.create(name="A", slug="a", base_url="https://a.test/v1")
        resp = self._post(admin_views.ChannelListView.as_view(), "/api/admin/channels",
                          {"name": "A2", "slug": "a", "base_url": "https://a2.test/v1"})
        self.assertEqual(resp.status_code, 400)

    def test_keys_are_scoped_by_header(self):
        nvidia = Channel.objects.create(name="NVIDIA", slug="nvidia",
                                        base_url="https://n.test/v1", is_default=True)
        zen = Channel.objects.create(name="Zen", slug="zen", base_url="https://z.test/v1")
        ChannelKey.objects.create(channel=nvidia, name="n1", api_key="n-key")
        ChannelKey.objects.create(channel=zen, name="z1", api_key="z-key")

        view = admin_views.ChannelKeyListView.as_view()
        a = self._get(view, "/api/admin/keys", HTTP_X_CHANNEL="nvidia")
        b = self._get(view, "/api/admin/keys", HTTP_X_CHANNEL="zen")
        c = self._get(view, "/api/admin/keys")
        self.assertEqual([k["name"] for k in a.data], ["n1"])
        self.assertEqual([k["name"] for k in b.data], ["z1"])
        self.assertEqual([k["name"] for k in c.data], ["n1"])  # 默认渠道

    def test_query_param_channel(self):
        Channel.objects.create(name="NVIDIA", slug="nvidia", base_url="https://n.test/v1",
                               is_default=True)
        zen = Channel.objects.create(name="Zen", slug="zen", base_url="https://z.test/v1")
        ChannelKey.objects.create(channel=zen, name="z1", api_key="z-key")
        resp = self._get(admin_views.ChannelKeyListView.as_view(),
                         "/api/admin/keys?channel=zen")
        self.assertEqual([k["name"] for k in resp.data], ["z1"])

    def test_import_keys_goes_to_current_channel(self):
        nvidia = Channel.objects.create(name="NVIDIA", slug="nvidia",
                                        base_url="https://n.test/v1", is_default=True)
        Channel.objects.create(name="Zen", slug="zen", base_url="https://z.test/v1")
        resp = self._post(admin_views.ChannelKeyImportView.as_view(),
                          "/api/admin/keys/import",
                          {"text": "x---key-a\nkey-b"}, HTTP_X_CHANNEL="zen")
        self.assertEqual(resp.data["success"], 2)
        zen = Channel.objects.get(slug="zen")
        self.assertEqual(zen.keys.count(), 2)
        self.assertEqual(nvidia.keys.count(), 0)
        # 未命名的那条按渠道名自动命名
        self.assertEqual(zen.keys.get(api_key="key-b").name, "Zen Key 001")
        self.assertEqual(zen.keys.get(api_key="key-a").name, "x")

    def test_models_and_logs_scoped(self):
        nvidia = Channel.objects.create(name="NVIDIA", slug="nvidia",
                                        base_url="https://n.test/v1", is_default=True)
        zen = Channel.objects.create(name="Zen", slug="zen", base_url="https://z.test/v1")
        AIModel.objects.create(channel=nvidia, model_name="n-model")
        AIModel.objects.create(channel=zen, model_name="z-model")
        RequestLog.objects.create(channel=zen, request_id="r1", model="z-model")

        models = self._get(admin_views.ModelListView.as_view(),
                           "/api/admin/models", HTTP_X_CHANNEL="zen")
        self.assertEqual([m["model_name"] for m in models.data], ["z-model"])

        logs = self._get(admin_views.LogListView.as_view(),
                         "/api/admin/logs", HTTP_X_CHANNEL="nvidia")
        self.assertEqual(logs.data["results"], [])

    def test_settings_are_per_channel(self):
        Channel.objects.create(name="NVIDIA", slug="nvidia", base_url="https://n.test/v1",
                               is_default=True)
        Channel.objects.create(name="Zen", slug="zen", base_url="https://z.test/v1")
        view = admin_views.SettingsView.as_view()
        request = self.factory.patch(
            "/api/admin/settings", data=json.dumps({"settings": {"proxy_timeout": 3}}),
            content_type="application/json", HTTP_X_CHANNEL="zen", **self.headers)
        view(request)

        zen = self._get(view, "/api/admin/settings", HTTP_X_CHANNEL="zen")
        nvidia = self._get(view, "/api/admin/settings", HTTP_X_CHANNEL="nvidia")
        z_timeout = next(p for p in zen.data["settings"] if p["key"] == "proxy_timeout")
        n_timeout = next(p for p in nvidia.data["settings"] if p["key"] == "proxy_timeout")
        self.assertEqual(z_timeout["value"], 3)
        self.assertNotEqual(n_timeout["value"], 3)

    def test_proxy_group_and_proxy_scoped(self):
        zen = Channel.objects.create(name="Zen", slug="zen", base_url="https://z.test/v1")
        other = Channel.objects.create(name="Other", slug="other",
                                       base_url="https://o.test/v1")
        g = self._post(admin_views.ProxyGroupListView.as_view(),
                       "/api/admin/proxy-groups", {"name": "美西"}, HTTP_X_CHANNEL="zen")
        self.assertEqual(g.status_code, 201)
        resp = self._get(admin_views.ProxyGroupListView.as_view(),
                         "/api/admin/proxy-groups", HTTP_X_CHANNEL="other")
        self.assertEqual(resp.data, [])

        p = self._post(admin_views.ProxyListView.as_view(), "/api/admin/proxies",
                       {"name": "p1", "protocol": "socks5", "host": "1.1.1.1", "port": 1080},
                       HTTP_X_CHANNEL="zen")
        self.assertEqual(p.status_code, 201)
        self.assertEqual(Proxy.objects.get(pk=p.data["id"]).channel_id, zen.id)

    def test_proxy_patch_rejects_group_from_other_channel(self):
        zen = Channel.objects.create(name="Zen", slug="zen", base_url="https://z.test/v1")
        other = Channel.objects.create(name="Other", slug="other",
                                       base_url="https://o.test/v1")
        g = other.proxy_groups.create(name="海外")
        p = zen.proxies.create(name="p1", protocol="socks5", host="1.1.1.1", port=1080)
        request = self.factory.patch(
            f"/api/admin/proxies/{p.id}", data=json.dumps({"group": g.id}),
            content_type="application/json", **self.headers)
        resp = admin_views.ProxyDetailView.as_view()(request, pk=p.id)
        self.assertEqual(resp.status_code, 400)
        p.refresh_from_db()
        self.assertIsNone(p.group)

    def test_user_api_keys_are_global(self):
        """用户 Key 是平台级的，不随渠道切换。"""
        api_key_service.create_key("global")
        Channel.objects.create(name="Zen", slug="zen", base_url="https://z.test/v1")
        resp = self._get(admin_views.UserApiKeyListView.as_view(),
                         "/api/admin/api-keys", HTTP_X_CHANNEL="zen")
        self.assertEqual(len(resp.data), 1)


class OpenAiChannelRoutingTests(TransactionTestCase):
    """/v1/* 走默认渠道，/c/<slug>/v1/* 走指定渠道。"""

    def _call(self, path, body, channel_slug=None, extra=None):
        captured = {}

        def handler(request: httpx.Request):
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization", "")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "chatcmpl-1", "object": "chat.completion",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        orig = race_engine_module._client_kwargs

        def patched(route, stream):
            kwargs = orig(route, stream)
            kwargs["transport"] = httpx.MockTransport(handler)
            return kwargs

        _user, raw_key = api_key_service.create_key("tester")
        request = RequestFactory().post(
            path, data=json.dumps(body), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw_key}", **(extra or {}))
        with patch.object(race_engine_module, "_client_kwargs", patched), \
             patch.object(openai_views, "_finish_log"):
            if channel_slug is None:
                response = openai_views.chat_completions(request)
            else:
                response = openai_views.chat_completions(request, channel_slug)
        return response, captured

    def setUp(self):
        self.nvidia = Channel.objects.create(
            name="NVIDIA", slug="nvidia", base_url="https://nvidia.test/v1", is_default=True)
        self.zen = Channel.objects.create(
            name="Zen", slug="zen", base_url="https://opencode.ai/zen/v1")
        ChannelKey.objects.create(channel=self.nvidia, name="n1", api_key="nvapi-n")
        ChannelKey.objects.create(channel=self.zen, name="z1", api_key="zen-key")
        AIModel.objects.create(channel=self.nvidia, model_name="m1", enabled=True)
        AIModel.objects.create(channel=self.zen, model_name="m1", enabled=True)

    def test_default_channel_route(self):
        response, captured = self._call(
            "/v1/chat/completions", {"model": "m1", "messages": [{"role": "user",
                                                                 "content": "hi"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["url"], "https://nvidia.test/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer nvapi-n")

    def test_named_channel_route(self):
        response, captured = self._call(
            "/c/zen/v1/chat/completions",
            {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            channel_slug="zen")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["url"],
                         "https://opencode.ai/zen/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer zen-key")

    def test_body_channel_field(self):
        response, captured = self._call(
            "/v1/chat/completions",
            {"model": "m1", "messages": [{"role": "user", "content": "hi"}],
             "channel": "zen"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["url"],
                         "https://opencode.ai/zen/v1/chat/completions")

    def test_model_must_exist_in_target_channel(self):
        AIModel.objects.filter(channel=self.zen).update(enabled=False)
        response, _ = self._call(
            "/c/zen/v1/chat/completions",
            {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            channel_slug="zen")
        self.assertEqual(response.status_code, 404)

    def test_list_models_per_channel(self):
        _user, raw_key = api_key_service.create_key("tester")
        AIModel.objects.create(channel=self.zen, model_name="zen-only", enabled=True)
        request = RequestFactory().get("/v1/models",
                                       HTTP_AUTHORIZATION=f"Bearer {raw_key}")
        default = json.loads(openai_views.list_models(request).content)
        zen = json.loads(openai_views.list_models(request, "zen").content)
        # /v1/models 汇总所有启用渠道的模型（含 zen 渠道）
        self.assertEqual([m["id"] for m in default["data"]], ["m1", "zen-only"])
        self.assertEqual([m["id"] for m in zen["data"]], ["m1", "zen-only"])


class ModelAliasTests(TestCase):
    """对外名称映射：alias > display_name > model_name，/v1 与 /c/<slug> 均生效。"""

    def setUp(self):
        self.channel = Channel.objects.create(
            name="NVIDIA", slug="nvidia", base_url="https://n.test/v1", is_default=True)

    def test_public_name_fallback_chain(self):
        m = AIModel.objects.create(channel=self.channel, model_name="raw/name",
                                   display_name="显示名", alias="alias-name", enabled=True)
        self.assertEqual(m.public_name, "alias-name")
        m.alias = ""
        self.assertEqual(m.public_name, "显示名")
        m.display_name = ""
        self.assertEqual(m.public_name, "raw/name")

    def test_resolve_by_alias_and_display_name(self):
        from services import model_registry
        AIModel.objects.create(channel=self.channel, model_name="raw/name",
                               display_name="显示名", alias="", enabled=True)
        self.assertEqual(model_registry.resolve("显示名").model_name, "raw/name")
        self.assertEqual(model_registry.resolve("raw/name").model_name, "raw/name")
        self.assertIsNone(model_registry.resolve("nope"))

    def test_list_models_returns_public_name(self):
        AIModel.objects.create(channel=self.channel, model_name="raw/name",
                               display_name="显示名", enabled=True)
        _user, raw_key = api_key_service.create_key("tester")
        request = RequestFactory().get(
            "/v1/models", HTTP_AUTHORIZATION=f"Bearer {raw_key}")
        data = json.loads(openai_views.list_models(request).content)
        self.assertEqual([m["id"] for m in data["data"]], ["显示名"])


class DashboardUsageAggregateTests(TestCase):
    """token 用量统计跨渠道汇总（仪表盘 Token 卡片不受渠道切换影响）。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.headers = {"HTTP_AUTHORIZATION": f"Token {settings.ADMIN_TOKEN}"}
        self.a = Channel.objects.create(name="A", slug="a", base_url="https://a.test/v1")
        self.b = Channel.objects.create(name="B", slug="b", base_url="https://b.test/v1")

    def test_usage_aggregates_all_channels(self):
        user, _ = api_key_service.create_key("测试用户")
        RequestLog.objects.create(channel=self.a, request_id="r1", model="m1",
                                  user_api_key=user, status="success",
                                  total_tokens=100, cached_tokens=15,
                                  prompt_tokens=60, completion_tokens=40,
                                  duration_ms=500)
        RequestLog.objects.create(channel=self.b, request_id="r2", model="m2",
                                  status="error", total_tokens=50,
                                  prompt_tokens=30, completion_tokens=20)
        request = self.factory.get("/api/admin/dashboard/usage?days=7",
                                   **self.headers)
        resp = admin_views.DashboardUsageView.as_view()(request)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["totals"]["requests"], 2)
        self.assertEqual(resp.data["totals"]["success"], 1)
        self.assertEqual(resp.data["totals"]["total_tokens"], 150)
        models = {m["model"]: m for m in resp.data["models"]}
        self.assertEqual(models["m1"]["total_tokens"], 100)
        self.assertEqual(models["m2"]["total_tokens"], 50)
        self.assertEqual(len(resp.data["days"]), 7)
        # 缓存命中率 = 缓存 / 输入 = 15 / (60 + 30)
        self.assertAlmostEqual(resp.data["totals"]["cache_hit_rate"], 16.7)
        channels = {c["name"]: c for c in resp.data["channels"]}
        self.assertEqual({c["total_tokens"] for c in channels.values()}, {100, 50})
        self.assertIn("prev_totals", resp.data)
        keys = {k["name"]: k for k in resp.data["keys"]}
        self.assertEqual(keys["测试用户"]["total_tokens"], 100)

    def test_hourly_buckets_for_today(self):
        """days=1 切换到按小时分桶，桶数 = 当前小时 + 1，prev 环比为昨日。"""
        RequestLog.objects.create(channel=self.a, request_id="h1", model="m1",
                                  status="success", total_tokens=10,
                                  prompt_tokens=6, completion_tokens=4)
        request = self.factory.get("/api/admin/dashboard/usage?days=1",
                                   **self.headers)
        resp = admin_views.DashboardUsageView.as_view()(request)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["granularity"], "hour")
        now = timezone.localtime(timezone.now())
        self.assertEqual(len(resp.data["days"]), now.hour + 1)
        self.assertEqual(resp.data["days"][0]["date"], "00:00")
        self.assertEqual(resp.data["days"][-1]["date"], f"{now.hour:02d}:00")
        self.assertEqual(resp.data["totals"]["requests"], 1)
        # 环比区间 = 昨天（空）
        self.assertEqual(resp.data["prev_totals"]["requests"], 0)

        # 天长视图仍是 day
        resp2 = admin_views.DashboardUsageView.as_view()(
            self.factory.get("/api/admin/dashboard/usage?days=7", **self.headers))
        self.assertEqual(resp2.data["granularity"], "day")

    def test_invalid_days_returns_400(self):
        request = self.factory.get("/api/admin/dashboard/usage?days=abc",
                                   **self.headers)
        resp = admin_views.DashboardUsageView.as_view()(request)
        self.assertEqual(resp.status_code, 400)


class RetryTests(TransactionTestCase):
    """retry_count 系统参数:竞速全部失败后自动重建线路重试。"""

    def setUp(self):
        self.channel = Channel.objects.create(
            name="NVIDIA", slug="nvidia", base_url="https://n.test/v1",
            is_default=True)
        ChannelKey.objects.create(channel=self.channel, name="k1", api_key="k1")
        AIModel.objects.create(channel=self.channel, model_name="m1", enabled=True)
        _user, self.raw_key = api_key_service.create_key("tester")

    def _call(self):
        request = RequestFactory().post(
            "/v1/chat/completions",
            data=json.dumps({"model": "m1",
                             "messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        return openai_views.chat_completions(request)

    @staticmethod
    def _ok_result():
        from unittest.mock import MagicMock
        r = MagicMock()
        r.route.kind = "direct"
        r.route.key.name = "k1"
        r.route.proxy = None
        r.http_status = 200
        r.payload = {"choices": [], "usage": {}}
        r.report = []
        return r

    def test_retry_succeeds_on_second_attempt(self):
        from services import sysconfig
        sysconfig.set_params({"retry_count": 2}, self.channel)
        with patch.object(openai_views, "race_chat",
                          side_effect=[race_engine_module.AllRoutesFailed(["boom"]),
                                       self._ok_result()]) as m:
            resp = self._call()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(m.call_count, 2)

    def test_no_retry_by_default(self):
        with patch.object(openai_views, "race_chat",
                          side_effect=race_engine_module.AllRoutesFailed(["boom"])) as m:
            resp = self._call()
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(m.call_count, 1)

    def test_retry_exhausted_returns_502(self):
        from services import sysconfig
        sysconfig.set_params({"retry_count": 2}, self.channel)
        with patch.object(openai_views, "race_chat",
                          side_effect=race_engine_module.AllRoutesFailed(["boom"])) as m:
            resp = self._call()
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(m.call_count, 3)


class BatchApiTests(TestCase):
    """模型/代理批量操作接口。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.headers = {"HTTP_AUTHORIZATION": f"Token {settings.ADMIN_TOKEN}"}
        self.channel = Channel.objects.create(
            name="NVIDIA", slug="nvidia", base_url="https://n.test/v1", is_default=True)

    def _post(self, view, data):
        request = self.factory.post("/batch", data=json.dumps(data),
                                    content_type="application/json", **self.headers)
        return view(request)

    def test_model_batch_enable_disable_delete(self):
        ids = [
            AIModel.objects.create(channel=self.channel, model_name=f"m{i}").id
            for i in range(3)
        ]
        view = admin_views.ModelBatchView.as_view()
        resp = self._post(view, {"ids": ids, "action": "enable"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AIModel.objects.filter(enabled=True).count(), 3)

        resp = self._post(view, {"ids": ids[:1], "action": "disable"})
        self.assertEqual(AIModel.objects.filter(enabled=True).count(), 2)

        resp = self._post(view, {"ids": ids, "action": "delete"})
        self.assertEqual(AIModel.objects.count(), 0)

        resp = self._post(view, {"ids": ids, "action": "nope"})
        self.assertEqual(resp.status_code, 400)

    def test_proxy_batch_respects_enable_limit(self):
        ChannelKey.objects.create(channel=self.channel, name="k1", api_key="k1")
        ChannelKey.objects.create(channel=self.channel, name="k2", api_key="k2")
        proxies = [
            Proxy.objects.create(channel=self.channel, name=f"p{i}",
                                 host="127.0.0.1", port=10000 + i)
            for i in range(3)
        ]
        view = admin_views.ProxyBatchView.as_view()
        resp = self._post(view, {"ids": [p.id for p in proxies], "action": "enable"})
        # 2 个 Key 最多启用 1 个代理，其余被跳过
        self.assertEqual(resp.data["succeeded"], 1)
        self.assertEqual(len(resp.data["skipped"]), 2)
        self.assertEqual(Proxy.objects.filter(enabled=True).count(), 1)

        resp = self._post(view, {"ids": [proxies[0].id], "action": "delete"})
        self.assertEqual(Proxy.objects.count(), 2)
