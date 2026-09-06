"""多渠道：端点解析、渠道隔离、按渠道路由。"""
import asyncio
import json
import types
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


def _acollect(agen) -> list:
    """同步消费异步生成器（streaming_content / SSE 转换器现为 async 生成器）。"""
    async def _gather():
        return [part async for part in agen]
    return asyncio.run(_gather())


def _consume_stream(resp) -> str:
    """同步消费异步流式响应体（streaming_content 现为 async 生成器）。"""
    return b"".join(_acollect(resp.streaming_content)).decode()



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

    def test_channel_list_counts(self):
        c = Channel.objects.create(name="A", slug="a", base_url="https://a.test/v1")
        ChannelKey.objects.create(channel=c, name="k1", api_key="k1")
        AIModel.objects.create(channel=c, model_name="m1", enabled=True)
        resp = self._get(admin_views.ChannelListView.as_view(), "/api/admin/channels")
        row = next(x for x in resp.data["results"] if x["slug"] == "a")
        self.assertEqual(row["key_count"], 1)
        self.assertEqual(row["enabled_key_count"], 1)
        self.assertEqual(row["model_count"], 1)
        self.assertEqual(row["enabled_model_count"], 1)

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
        # 未命名的那条按渠道名自动命名（Key 已加密存储，按明文解密后比较）
        from services.crypto import decrypt_secret
        plain = {decrypt_secret(k.api_key or ""): k for k in zen.keys.all()}
        self.assertEqual(plain["key-b"].name, "Zen Key 001")
        self.assertEqual(plain["key-a"].name, "x")

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
    """对外名称映射（仅保留别名系统）：alias > model_name；display_name 不参与对外。"""

    def setUp(self):
        self.channel = Channel.objects.create(
            name="NVIDIA", slug="nvidia", base_url="https://n.test/v1", is_default=True)

    def test_public_name_fallback_chain(self):
        m = AIModel.objects.create(channel=self.channel, model_name="raw/name",
                                   display_name="显示名", alias="alias-name", enabled=True)
        self.assertEqual(m.public_name, "alias-name")
        m.alias = ""
        # display_name 只是后台标签，不再参与对外命名
        self.assertEqual(m.public_name, "raw/name")

    def test_resolve_by_alias_only(self):
        from services import model_registry
        AIModel.objects.create(channel=self.channel, model_name="raw/name",
                               display_name="显示名", alias="", enabled=True)
        self.assertEqual(model_registry.resolve("raw/name").model_name, "raw/name")
        # display_name 不再可调用
        self.assertIsNone(model_registry.resolve("显示名"))
        self.assertIsNone(model_registry.resolve("nope"))

    def test_list_models_ignores_display_name(self):
        AIModel.objects.create(channel=self.channel, model_name="raw/name",
                               display_name="显示名", enabled=True)
        _user, raw_key = api_key_service.create_key("tester")
        request = RequestFactory().get(
            "/v1/models", HTTP_AUTHORIZATION=f"Bearer {raw_key}")
        data = json.loads(openai_views.list_models(request).content)
        # /v1/models 只暴露别名/原始名，display_name 不出现
        self.assertEqual([m["id"] for m in data["data"]], ["raw/name"])

    def test_upstream_body_uses_real_model_name(self):
        """核心：客户端用别名调用时，上游必须收到真实上游模型名而非别名。"""
        from api.openai_views import _build_upstream_body
        body = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]}
        out = _build_upstream_body(body, "moonshotai/kimi-k3")
        self.assertEqual(out["model"], "moonshotai/kimi-k3")
        # 别名只存在于平台对外层，不进上游请求体
        self.assertNotEqual(out["model"], "kimi-k3")

    def test_multiple_aliases(self):
        """一个模型可暴露多个对外名（主名 + 附加别名），/v1 与解析均生效。"""
        from services import model_registry
        AIModel.objects.create(channel=self.channel, model_name="raw/name",
                               alias="main-name", aliases=["alias-1", "alias-2"],
                               enabled=True)
        m = AIModel.objects.get(model_name="raw/name")
        self.assertEqual(model_registry.public_names(m),
                         ["main-name", "alias-1", "alias-2"])

        _user, raw_key = api_key_service.create_key("tester")
        request = RequestFactory().get(
            "/v1/models", HTTP_AUTHORIZATION=f"Bearer {raw_key}")
        data = json.loads(openai_views.list_models(request).content)
        self.assertEqual(sorted(x["id"] for x in data["data"]),
                         ["alias-1", "alias-2", "main-name"])

        # 任意对外名都能解析到同一个上游模型
        for n in ("main-name", "alias-1", "alias-2"):
            self.assertEqual(model_registry.resolve(n).model_name, "raw/name")
        # 渠道内解析也命中附加别名
        self.assertEqual(
            model_registry.resolve_in_channel("alias-2", self.channel).model_name,
            "raw/name")


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


class DashboardUsageMergedQueryTests(TestCase):
    """2026-09 查询三合一重构的等价性守卫。

    _build_payload 由 6 条串行查询收敛为 3 条（分桶捎带区间汇总、
    三维分布合一、环比独立），avg 类指标由独立 filtered Avg 改为
    sum/n 派生。这里钉住新口径的关键语义：
    - duration 全为 0 的行不计入 avg_latency_s（原 filtered Avg 语义）
    - 分布合并后 model 维度的 success_rate / avg_latency_s 不失真
    - models 按 (-total_tokens, model) 排序、Top 20；channels/keys 同序
    - 空 key 行归到 "(未知 Key)"、空渠道归到 "(无渠道)"
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.headers = {"HTTP_AUTHORIZATION": f"Token {settings.ADMIN_TOKEN}"}
        self.ch = Channel.objects.create(name="A", slug="a",
                                         base_url="https://a.test/v1")

    def _get(self, qs="days=7"):
        req = self.factory.get(f"/api/admin/dashboard/usage?{qs}", **self.headers)
        return admin_views.DashboardUsageView.as_view()(req).data

    def test_avg_latency_excludes_zero_duration_rows(self):
        # 3 行 duration=0（不计入均值）+ 1 行 2000ms -> 平均 2s，不是 0.5s
        for i in range(3):
            RequestLog.objects.create(channel=self.ch, request_id=f"z{i}",
                                      model="m", status="success",
                                      duration_ms=0, total_tokens=1)
        RequestLog.objects.create(channel=self.ch, request_id="ok", model="m",
                                  status="success", duration_ms=2000,
                                  total_tokens=1)
        data = self._get()
        self.assertEqual(data["totals"]["requests"], 4)
        self.assertEqual(data["totals"]["avg_latency_s"], 2.0)
        # 全零 TTFT -> None（前端据此显示 "—"）
        self.assertIsNone(data["totals"]["avg_ttft_ms"])

    def test_avg_none_when_no_qualified_rows(self):
        RequestLog.objects.create(channel=self.ch, request_id="z", model="m",
                                  status="success", duration_ms=0,
                                  total_tokens=1)
        data = self._get()
        self.assertIsNone(data["totals"]["avg_latency_s"])
        self.assertIsNone(data["totals"]["avg_ttft_ms"])

    def test_distribution_merge_semantics(self):
        user1, _ = api_key_service.create_key("k1")
        user2, _ = api_key_service.create_key("k2")
        # 同一模型跨渠道/跨用户 3 行：合并后 requests=3、success=2、
        # avg 只计入 duration>0 的 2 行（1s + 3s -> 2s）
        RequestLog.objects.create(channel=self.ch, request_id="d1", model="m",
                                  user_api_key=user1, status="success",
                                  duration_ms=1000, total_tokens=10)
        RequestLog.objects.create(channel=self.ch, request_id="d2", model="m",
                                  user_api_key=user2, status="success",
                                  duration_ms=3000, total_tokens=10)
        RequestLog.objects.create(channel=self.ch, request_id="d3", model="m",
                                  user_api_key=user2, status="error",
                                  duration_ms=0, total_tokens=5)
        data = self._get()
        m = {x["model"]: x for x in data["models"]}["m"]
        self.assertEqual(m["requests"], 3)
        self.assertEqual(m["success"], 2)
        self.assertEqual(m["total_tokens"], 25)
        self.assertAlmostEqual(m["success_rate"], 66.7)
        self.assertEqual(m["avg_latency_s"], 2.0)
        keys = {k["name"]: k for k in data["keys"]}
        self.assertEqual(keys["k1"]["total_tokens"], 10)
        self.assertEqual(keys["k2"]["total_tokens"], 15)
        # 渠道行带 requests 字段（旧实现同样产出）
        self.assertEqual(data["channels"][0]["name"], "A")
        self.assertEqual(data["channels"][0]["requests"], 3)

    def test_unknown_labels_and_top20_order(self):
        # 无渠道/无用户 Key 的行 + 超过 20 个模型时截断
        for i in range(25):
            RequestLog.objects.create(request_id=f"u{i}",
                                      model=f"model-{i:02d}",
                                      status="success", total_tokens=i)
        data = self._get()
        self.assertEqual(len(data["models"]), 20)
        # total_tokens 降序、同额按模型名升序（与原 SQL ORDER BY 对齐）
        toks = [m["total_tokens"] for m in data["models"]]
        self.assertEqual(toks, sorted(toks, reverse=True))
        self.assertEqual(data["models"][0]["model"], "model-24")
        self.assertEqual({k["name"] for k in data["keys"]}, {"(未知 Key)"})
        # 无渠道行归到 "(无渠道)"（channel 为 SET_NULL 外键）
        self.assertEqual(data["channels"][0]["name"], "(无渠道)")

    def test_usage_cover_index_exists_and_sortable(self):
        """覆盖索引 request_log_usage_cover 已在迁移与模型层声明。"""
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' "
                        "AND name='request_log_usage_cover'")
            self.assertIsNotNone(cur.fetchone())
        # 模型 Meta 与迁移保持一致（防止后人只改一处）
        idx_names = {i.name for i in RequestLog._meta.indexes}
        self.assertIn("request_log_usage_cover", idx_names)


class RequestLogIndexPlanTests(TestCase):
    """日志列表/仪表盘聚合的查询计划守卫。

    这里钉的是**索引形状**而不是索引存在性：日志页恒 `ORDER BY -id` + LIMIT，
    服务它的渠道内索引必须以 `id` 收尾。少了 id，SQLite 会先用 channel 前缀捞出
    该渠道全部 rowid 再建 TEMP B-TREE 排序——实测比不加索引还慢
    （12k 行渠道：7.4ms → 35ms）。这种退化不会让任何功能测试变红，只能靠
    计划断言拦住。
    """

    def setUp(self):
        self.ch = Channel.objects.create(name="P", slug="p",
                                         base_url="https://p.test/v1")
        for i in range(5):
            RequestLog.objects.create(channel=self.ch, request_id=f"r{i}",
                                      model="m", status="success")

    def _plan(self, sql, params=()):
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("EXPLAIN QUERY PLAN " + sql, params)
            return " | ".join(str(r[-1]) for r in cur.fetchall())

    def test_default_list_ordering_uses_no_temp_btree(self):
        plan = self._plan(
            'SELECT "id" FROM "request_log" WHERE "channel_id" = %s '
            'ORDER BY "id" DESC LIMIT 100', [self.ch.id])
        self.assertIn("request_log_channel_id", plan)
        self.assertNotIn("TEMP B-TREE", plan.upper())

    def test_status_filtered_list_ordering_uses_no_temp_btree(self):
        plan = self._plan(
            'SELECT "id" FROM "request_log" WHERE "channel_id" = %s '
            'AND "status" = %s ORDER BY "id" DESC LIMIT 100',
            [self.ch.id, "failed"])
        self.assertIn("request_log_channel_status", plan)
        self.assertNotIn("TEMP B-TREE", plan.upper())

    def test_model_filtered_list_ordering_uses_no_temp_btree(self):
        plan = self._plan(
            'SELECT "id" FROM "request_log" WHERE "channel_id" = %s '
            'AND "model" = %s ORDER BY "id" DESC LIMIT 100',
            [self.ch.id, "m"])
        self.assertIn("request_log_channel_model", plan)
        self.assertNotIn("TEMP B-TREE", plan.upper())

    def test_dashboard_today_aggregate_is_covering(self):
        """DashboardView 的今日聚合必须走覆盖索引（曾占该接口 58ms/77ms 全部耗时）。"""
        plan = self._plan(
            'SELECT COUNT("id"), AVG("duration_ms") FROM "request_log" '
            'WHERE "channel_id" = %s AND "created_at" >= %s',
            [self.ch.id, "2026-01-01 00:00:00"])
        self.assertIn("request_log_channel_created", plan)
        self.assertIn("COVERING INDEX", plan.upper())

    def test_list_query_defers_heavy_json_columns(self):
        """列表 SQL 不得 SELECT 那四个肥 JSON 列（20KB/行的回表成本来源）。"""
        from api.admin_views.logs import LogListView
        from django.test import RequestFactory as RF
        from django.conf import settings as st
        # 直接检查 ORM 侧生成的列集合
        from api.admin_views.logs import _LOG_HEAVY_FIELDS
        qs = self.ch.logs.order_by("-id").defer(*_LOG_HEAVY_FIELDS)
        sql = str(qs.query)
        for col in _LOG_HEAVY_FIELDS:
            self.assertNotIn(f'"{col}"', sql,
                             f"列表查询仍在 SELECT 肥列 {col}")


class DashboardStatsCaliberTests(TestCase):
    """DashboardView 查询收敛后的口径守卫。

    原实现发 10 条 COUNT/GROUP BY，现由状态分布的 GROUP BY 一次派生
    total / enabled / 上限。同时修了一处**显示与强制不一致**：仪表盘的
    `max_enabled_proxies` 旧口径把 INVALID（401/403 已判死）Key 计入分母，
    于是提示的上限高于 `proxy_service.set_enabled` 实际强制的值——用户按
    仪表盘提示去启用会被后端拒绝。现在两边同源。
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.headers = {"HTTP_AUTHORIZATION": f"Token {settings.ADMIN_TOKEN}"}
        self.ch = Channel.objects.create(name="S", slug="s",
                                         base_url="https://s.test/v1")

    def _stats(self):
        req = self.factory.get("/api/admin/dashboard", **self.headers)
        return admin_views.DashboardView.as_view()(req).data

    def test_max_enabled_proxies_matches_backend_enforcement(self):
        from apps.core.models import ChannelKey, ChannelKeyStatus as S
        for i, st in enumerate([S.AVAILABLE, S.AVAILABLE, S.AVAILABLE,
                                S.RATE_LIMITED, S.INVALID, S.DISABLED]):
            ChannelKey.objects.create(channel=self.ch, name=f"k{i}",
                                      api_key=f"nvapi-x{i}", status=st)
        data = self._stats()
        # 可调度 = 排除 DISABLED 与 INVALID = 4
        self.assertEqual(data["nvidia_keys"], 6)
        self.assertEqual(data["enabled_keys"], 4)
        self.assertEqual(data["max_enabled_proxies"], 3)
        # 与后端强制值完全一致（这是本轮修的点）
        self.assertEqual(
            data["max_enabled_proxies"],
            proxy_service.max_proxies_for_channel(self.ch))

    def test_counts_match_independent_queries(self):
        from apps.core.models import (AIModel, ChannelKey, Proxy, ProxyStatus)
        for i in range(3):
            ChannelKey.objects.create(channel=self.ch, name=f"k{i}",
                                      api_key=f"nvapi-k{i}")
        for i in range(4):
            Proxy.objects.create(channel=self.ch, name=f"p{i}", host="1.2.3.4",
                                 port=1000 + i, enabled=(i < 2),
                                 status=ProxyStatus.HEALTHY if i == 0
                                 else ProxyStatus.UNKNOWN)
        for i in range(5):
            AIModel.objects.create(channel=self.ch, model_name=f"m{i}",
                                   enabled=(i < 3))
        data = self._stats()
        self.assertEqual(data["nvidia_keys"], 3)
        self.assertEqual(data["proxies"], 4)
        self.assertEqual(data["enabled_proxies"], 2)
        self.assertEqual(data["models"], 5)
        self.assertEqual(data["enabled_models"], 3)
        # 状态分布求和必须等于总数（派生自同一条 GROUP BY）
        self.assertEqual(sum(data["key_status"].values()), 3)
        self.assertEqual(sum(data["proxy_status"].values()), 4)

    def test_proxies_summary_total_keys(self):
        """代理页不再整拉 /api/admin/keys，Key 总数由 summary 提供。"""
        from apps.core.models import ChannelKey, ChannelKeyStatus as S
        for i in range(3):
            ChannelKey.objects.create(channel=self.ch, name=f"k{i}",
                                      api_key=f"nvapi-{i}")
        ChannelKey.objects.create(channel=self.ch, name="dead",
                                  api_key="nvapi-dead", status=S.INVALID)
        req = self.factory.get("/api/admin/proxies", **self.headers)
        summary = admin_views.ProxyListView.as_view()(req).data["summary"]
        self.assertEqual(summary["total_keys"], 4)      # 全部
        self.assertEqual(summary["nvidia_keys"], 3)     # 可调度（排除 INVALID）
        self.assertEqual(summary["max_enabled_proxies"], 2)


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

    def test_no_retry_when_disabled(self):
        """retry_count=0：竞速失败后不重试（默认值已改为 1，此处显式关闭验证关闭语义）。"""
        from services import sysconfig
        sysconfig.set_params({"retry_count": 0}, self.channel)
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


class StreamRetryTests(TransactionTestCase):
    """流式请求：首字节发出前的一切失败都纳入 retry_count 自动重试。"""

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
                             "messages": [{"role": "user", "content": "hi"}],
                             "stream": True}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        return openai_views.chat_completions(request)

    def test_stream_retries_when_breaks_before_first_byte(self):
        from services import sysconfig
        sysconfig.set_params({"retry_count": 2}, self.channel)
        ok_chunk = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        behaviors = [
            _FakeStreamWinner(error=httpx.ReadError("stream broke")),
            _FakeStreamWinner(chunks=[ok_chunk, "data: [DONE]\n\n"]),
        ]
        calls = {"n": 0}

        async def fake_race_stream(*args, **kwargs):
            calls["n"] += 1
            return behaviors.pop(0)

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            resp = self._call()
            body = _consume_stream(resp)
        self.assertEqual(calls["n"], 2)
        self.assertIn('data: {"choices"', body)
        self.assertIn("data: [DONE]", body)
        self.assertNotIn("stream_error", body)

    def test_stream_retries_when_breaks_after_heartbeat_only(self):
        # 首字节只是心跳（choices 为空），未交付任何实际内容 → 中断后应重试
        from services import sysconfig
        sysconfig.set_params({"retry_count": 2}, self.channel)
        heartbeat = 'data: {"id":"chatcmpl-a","object":"chat.completion.chunk",' \
                    '"choices":[]}\n\n'
        ok_chunk = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        behaviors = [
            _FakeStreamWinner(chunks=[heartbeat], error_after=1),
            _FakeStreamWinner(chunks=[ok_chunk, "data: [DONE]\n\n"]),
        ]
        calls = {"n": 0}

        async def fake_race_stream(*args, **kwargs):
            calls["n"] += 1
            return behaviors.pop(0)

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            resp = self._call()
            body = _consume_stream(resp)
        self.assertEqual(calls["n"], 2)
        self.assertIn('data: {"choices"', body)
        self.assertIn("data: [DONE]", body)
        self.assertNotIn("stream_error", body)

    def test_stream_retries_when_breaks_after_reasoning_only(self):
        # 思考模型：只输出了 reasoning_content（思考过程），尚未产出正文
        # → 思考阶段断流不算"已提交内容"，应重建线路自动重试
        from services import sysconfig
        sysconfig.set_params({"retry_count": 2}, self.channel)
        reasoning = 'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n'
        ok_chunk = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        behaviors = [
            _FakeStreamWinner(chunks=[reasoning], error_after=1),
            _FakeStreamWinner(chunks=[ok_chunk, "data: [DONE]\n\n"]),
        ]
        calls = {"n": 0}

        async def fake_race_stream(*args, **kwargs):
            calls["n"] += 1
            return behaviors.pop(0)

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            resp = self._call()
            body = _consume_stream(resp)
        self.assertEqual(calls["n"], 2)
        self.assertIn("thinking...", body)
        self.assertIn('data: {"choices"', body)
        self.assertIn("data: [DONE]", body)
        self.assertNotIn("stream_error", body)

    def test_stream_no_retry_after_answer_content(self):
        # 已交付正文 content 后才断流 → 响应已提交，不能重试；
        # 且绝不能发 error 事件（否则客户端 SSE 解析报 "error decoding response body"），
        # 应干净收尾 [DONE]。
        from services import sysconfig
        sysconfig.set_params({"retry_count": 3}, self.channel)
        answer = 'data: {"choices":[{"delta":{"content":"ans"}}]}\n\n'
        behaviors = [
            _FakeStreamWinner(chunks=[answer], error_after=1),
        ]
        calls = {"n": 0}

        async def fake_race_stream(*args, **kwargs):
            calls["n"] += 1
            return behaviors.pop(0)

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            resp = self._call()
            body = _consume_stream(resp)
        self.assertEqual(calls["n"], 1)
        self.assertIn("data: [DONE]", body)
        self.assertNotIn("stream_error", body)

    def test_stream_no_retry_after_first_byte_sent(self):
        from services import sysconfig
        sysconfig.set_params({"retry_count": 3}, self.channel)
        ok_chunk = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        behaviors = [
            # 先吐出首字节，随后流中断 → 响应已提交，不能重试，干净收尾
            _FakeStreamWinner(chunks=[ok_chunk], error_after=1),
        ]
        calls = {"n": 0}

        async def fake_race_stream(*args, **kwargs):
            calls["n"] += 1
            return behaviors.pop(0)

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            resp = self._call()
            body = _consume_stream(resp)
        self.assertEqual(calls["n"], 1)
        self.assertIn("data: [DONE]", body)
        self.assertNotIn("stream_error", body)

    def test_stream_retry_exhausted_returns_stream_error(self):
        from services import sysconfig
        sysconfig.set_params({"retry_count": 2}, self.channel)
        behaviors = [_FakeStreamWinner(error=httpx.ReadError("boom")) for _ in range(3)]
        calls = {"n": 0}

        async def fake_race_stream(*args, **kwargs):
            calls["n"] += 1
            return behaviors.pop(0)

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            resp = self._call()
            body = _consume_stream(resp)
        self.assertEqual(calls["n"], 3)
        self.assertIn("stream_error", body)

    def test_stream_failure_tail_persists_failed_log(self):
        """失败收尾日志必须落库为 failed（锁死 _safe_finish 递归不再吞掉落库）。"""
        from services import sysconfig
        sysconfig.set_params({"retry_count": 0}, self.channel)
        behaviors = [_FakeStreamWinner(error=httpx.ReadError("boom"))]
        calls = {"n": 0}

        async def fake_race_stream(*args, **kwargs):
            calls["n"] += 1
            return behaviors.pop(0)

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            resp = self._call()
            _consume_stream(resp)
        self.assertEqual(calls["n"], 1)
        log = RequestLog.objects.order_by("-id").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, "failed")

    def test_stream_releases_upstream_slots_when_done(self):
        """流结束后全局上游阀门必须归零（锁死 slot 泄漏，防并发流被拖到 no_available_route）。"""
        openai_views._upstream_active = 0
        ok_chunk = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'

        async def fake_race_stream(*args, **kwargs):
            return _FakeStreamWinner(chunks=[ok_chunk, "data: [DONE]\n\n"])

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            before = openai_views._upstream_active
            resp = self._call()
            body = _consume_stream(resp)
        self.assertIn("data: [DONE]", body)
        self.assertEqual(openai_views._upstream_active, before)

    def test_truncated_after_content_is_marked_not_silent(self):
        """已交付内容后上游断流：必须在日志里留下 stream_truncated 标记，
        而非伪装成"正常成功"的无声中断（锁死无报错中断的可见性）。"""
        from services import sysconfig
        sysconfig.set_params({"retry_count": 0}, self.channel)
        answer = 'data: {"choices":[{"delta":{"content":"ans"}}]}\n\n'
        behaviors = [_FakeStreamWinner(chunks=[answer], error_after=1)]

        async def fake_race_stream(*args, **kwargs):
            return behaviors.pop(0)

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            resp = self._call()
            body = _consume_stream(resp)
        self.assertIn("data: [DONE]", body)
        log = RequestLog.objects.order_by("-id").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.error_type, "stream_truncated")


class _FakeStreamWinner:
    """模拟 race_stream 返回的 winner：可按行产出 SSE，或在指定位置抛错。"""

    def __init__(self, error=None, chunks=None, error_after=None):
        self.route = types.SimpleNamespace(
            kind="direct", key=types.SimpleNamespace(name="k1"), proxy=None)
        self.report = []
        self._error = error
        self._chunks = list(chunks or [])
        self._error_after = error_after

    async def lines(self):
        if self._error:
            raise self._error
        for i, chunk in enumerate(self._chunks):
            if self._error_after is not None and i >= self._error_after:
                raise httpx.ReadError("mid-stream broke")
            yield chunk
        if self._error_after is not None:
            # 全部 chunk 已吐完后再补一次中断，模拟"先出数据、随后流断开"
            raise httpx.ReadError("mid-stream broke")

    async def close(self):
        pass


class ResponsesEndpointTests(TransactionTestCase):
    """/v1/responses —— Responses API 协议入口（非流式/流式、格式转换）。"""

    def setUp(self):
        self.channel = Channel.objects.create(
            name="NVIDIA", slug="nvidia", base_url="https://n.test/v1",
            is_default=True)
        ChannelKey.objects.create(channel=self.channel, name="k1", api_key="k1")
        AIModel.objects.create(channel=self.channel, model_name="m1", enabled=True)
        _user, self.raw_key = api_key_service.create_key("tester")

    def _call(self, body):
        request = RequestFactory().post(
            "/v1/responses",
            data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        return openai_views.responses(request)

    @staticmethod
    def _ok_result():
        from unittest.mock import MagicMock
        r = MagicMock()
        r.route.kind = "direct"
        r.route.key.name = "k1"
        r.route.proxy = None
        r.http_status = 200
        r.payload = {
            "id": "chatcmpl-1", "object": "chat.completion", "created": 123,
            "model": "m1",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "hi there"},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 3},
        }
        r.report = []
        return r

    def test_non_stream_returns_responses_format(self):
        with patch.object(openai_views, "race_chat", return_value=self._ok_result()):
            resp = self._call({"model": "m1", "input": "hi", "stream": False})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["object"], "response")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["output"][0]["type"], "message")
        self.assertEqual(data["output"][0]["content"][0]["text"], "hi there")

    def test_string_input_becomes_user_message(self):
        with patch.object(openai_views, "race_chat", return_value=self._ok_result()) as m:
            resp = self._call({"model": "m1", "input": "hello", "stream": False})
        self.assertEqual(resp.status_code, 200)
        sent = m.call_args[0][1]
        self.assertEqual(sent["messages"], [{"role": "user", "content": "hello"}])

    def test_max_output_tokens_maps_to_max_tokens(self):
        with patch.object(openai_views, "race_chat", return_value=self._ok_result()) as m:
            resp = self._call({"model": "m1", "input": "hi",
                               "max_output_tokens": 512, "stream": False})
        self.assertEqual(resp.status_code, 200)
        sent = m.call_args[0][1]
        self.assertEqual(sent["max_tokens"], 512)
        self.assertNotIn("seed", sent)

    def test_missing_input_returns_400(self):
        resp = self._call({"model": "m1"})
        self.assertEqual(resp.status_code, 400)

    def test_stream_returns_responses_sse(self):
        ok_chunk = ('data: {"id":"c1","choices":[{"delta":{"role":"assistant",'
                    '"content":"he"}}]}\n\n')
        done_chunk = "data: [DONE]\n\n"
        fake = _FakeStreamWinner(chunks=[ok_chunk, done_chunk])

        async def fake_race_stream(*args, **kwargs):
            return fake

        with patch.object(openai_views, "race_stream", new=fake_race_stream):
            resp = self._call({"model": "m1", "input": "hi", "stream": True})
            # streaming_content 是惰性生成器，必须在 patch 生效期间消费
            body = _consume_stream(resp)
        self.assertIn("event: response.created", body)
        self.assertIn("event: response.output_text.delta", body)
        self.assertIn('"delta": "he"', body)
        self.assertIn("data: [DONE]", body)


class ResponsesTranslateTests(TestCase):
    """responses_api 双向转换：assistant 消息 / 参数过滤 / 请求-响应互转。"""

    def test_assistant_message_uses_output_text(self):
        from services import responses_api
        out = responses_api.chat_to_responses_body({
            "model": "m",
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "yo"}]})
        self.assertEqual(out["input"][0]["content"][0]["type"], "input_text")
        self.assertEqual(out["input"][1]["content"][0]["type"], "output_text")

    def test_common_params_passed_through(self):
        """同名通用参数（seed/stop 等）忠实透传，不做白名单裁剪。"""
        from services import responses_api
        out = responses_api.chat_to_responses_body({
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "seed": 1, "stop": ["x"], "temperature": 0.5, "max_tokens": 100})
        self.assertEqual(out["seed"], 1)
        self.assertEqual(out["stop"], ["x"])
        self.assertEqual(out["temperature"], 0.5)
        self.assertEqual(out["max_output_tokens"], 100)

    def test_chat_only_params_dropped_to_responses_but_kept_from(self):
        """n / penalties 是 chat 独有、responses 明确移除：chat->responses 不携带，
        responses->chat 方向保留（避免 400，又不丢数据）。"""
        from services import responses_api
        out = responses_api.chat_to_responses_body({
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "n": 2, "frequency_penalty": 0.3, "presence_penalty": 0.2})
        self.assertNotIn("n", out)
        self.assertNotIn("frequency_penalty", out)
        self.assertNotIn("presence_penalty", out)
        back = responses_api.responses_to_chat_body({
            "model": "m", "input": "hi", "n": 2, "frequency_penalty": 0.3})
        self.assertEqual(back["n"], 2)
        self.assertEqual(back["frequency_penalty"], 0.3)

    def test_reasoning_effort_mapped_both_ways(self):
        """思考参数：chat reasoning_effort <-> responses reasoning.effort。"""
        from services import responses_api
        out = responses_api.chat_to_responses_body({
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "max"})
        self.assertEqual(out["reasoning"]["effort"], "high")
        out_off = responses_api.chat_to_responses_body({
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "chat_template_kwargs": {"thinking": False}})
        self.assertEqual(out_off["reasoning"]["effort"], "none")
        back = responses_api.responses_to_chat_body({
            "model": "m", "input": "hi", "reasoning": {"effort": "none"}})
        self.assertEqual(back["reasoning_effort"], "off")

    def test_finish_reason_incomplete_details_mapped(self):
        """finish_reason <-> incomplete_details.reason 双向忠实映射。"""
        from services import responses_api
        chat = responses_api.responses_payload_to_chat({
            "id": "r", "object": "response", "created_at": 1, "model": "m",
            "status": "incomplete", "output": [
                {"id": "m1", "type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "x"}]}],
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 1, "output_tokens": 2}})
        self.assertEqual(chat["choices"][0]["finish_reason"], "length")
        resp = responses_api.chat_to_responses_payload({
            "id": "c", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": "x"},
                         "finish_reason": "content_filter"}],
            "usage": {}})
        self.assertEqual(resp["status"], "incomplete")
        self.assertEqual(resp["incomplete_details"]["reason"], "content_filter")

    def test_json_schema_format_details_preserved(self):
        """json_schema 结构化输出细节双向保留（name/schema/strict）。"""
        from services import responses_api
        js = {"type": "json_schema", "json_schema": {
            "name": "person", "schema": {"type": "object"}, "strict": True}}
        out = responses_api.chat_to_responses_body({
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "response_format": js})
        fmt = out["text"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["name"], "person")
        self.assertIs(fmt["strict"], True)
        self.assertEqual(fmt["schema"], {"type": "object"})
        back = responses_api.responses_to_chat_body({
            "model": "m", "input": "hi", "text": {"format": fmt}})
        self.assertEqual(back["response_format"]["json_schema"]["name"], "person")

    def test_responses_payload_echoes_request_params(self):
        """/v1/responses 非流式输出回显请求参数（instructions/tools 等）。"""
        from services import responses_api
        resp = responses_api.chat_to_responses_payload({
            "id": "c", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {}}, echo={"instructions": "be nice", "store": False})
        self.assertEqual(resp["instructions"], "be nice")
        self.assertFalse(resp["store"])
        self.assertEqual(resp["status"], "completed")

    def test_refusal_preserved(self):
        """responses refusal 内容 -> chat message.refusal -> responses refusal 条目。"""
        from services import responses_api
        chat = responses_api.responses_payload_to_chat({
            "id": "r", "object": "response", "created_at": 1, "model": "m",
            "status": "completed", "output": [
                {"id": "m1", "type": "message", "role": "assistant",
                 "content": [{"type": "refusal", "refusal": "I cannot help"}]}],
            "usage": {}})
        self.assertEqual(chat["choices"][0]["message"]["refusal"], "I cannot help")
        resp = responses_api.chat_to_responses_payload({
            "id": "c", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": "", "refusal": "no"},
                         "finish_reason": "stop"}], "usage": {}})
        self.assertEqual(resp["output"][0]["content"][0]["type"], "refusal")

    def test_tools_and_tool_choice_converted_both_ways(self):
        """tools / tool_choice 结构包装差异的忠实映射（双向）。"""
        from services import responses_api
        # chat -> responses
        out = responses_api.chat_to_responses_body({
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {
                "name": "get_weather", "description": "d",
                "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}}})
        self.assertEqual(out["tools"][0]["name"], "get_weather")
        self.assertNotIn("function", out["tools"][0])
        self.assertEqual(out["tool_choice"]["name"], "get_weather")
        # responses -> chat
        back = responses_api.responses_to_chat_body({
            "model": "m", "input": "hi",
            "tools": [{"type": "function", "name": "get_weather",
                       "description": "d", "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "get_weather"}})
        self.assertEqual(back["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(back["tool_choice"]["function"]["name"], "get_weather")

    def test_developer_role_preserved(self):
        """developer role 是 Responses 原生角色，不被改写为 system。"""
        from services import responses_api
        out = responses_api.chat_to_responses_body({
            "model": "m",
            "messages": [{"role": "developer", "content": "be strict"}]})
        self.assertEqual(out["input"][0]["role"], "developer")

    def test_function_call_input_not_misread_as_user(self):
        """responses input 里的 assistant function_call 条目转成 chat tool_calls。"""
        from services import responses_api
        chat = responses_api.responses_to_chat_body({
            "model": "m",
            "input": [{"type": "function_call", "call_id": "c1",
                       "name": "f1", "arguments": "{}"}]})
        self.assertEqual(chat["messages"][0]["role"], "assistant")
        self.assertEqual(chat["messages"][0]["tool_calls"][0]["id"], "c1")

    def test_instructions_become_system_message(self):
        """responses instructions 顶层参数 -> chat system 消息。"""
        from services import responses_api
        chat = responses_api.responses_to_chat_body({
            "model": "m", "instructions": "be nice", "input": "hi"})
        self.assertEqual(chat["messages"][0]["role"], "system")
        self.assertEqual(chat["messages"][0]["content"], "be nice")

    def test_stream_tool_call_arguments_accumulate_without_duplication(self):
        """标准流式工具调用：added(name) + arguments.delta(增量)，done 不重复。"""
        from services import responses_api
        state: dict = {"args_seen": set()}
        out1 = json.loads(responses_api._translate_event("data: " + json.dumps({
            "type": "response.output_item.added", "output_index": 0,
            "item": {"id": "fc_1", "type": "function_call", "call_id": "fc_1",
                     "name": "get_weather", "arguments": ""}}), state))
        self.assertEqual(out1["choices"][0]["delta"]["tool_calls"][0]["function"]["name"],
                         "get_weather")
        out2 = json.loads(responses_api._translate_event("data: " + json.dumps({
            "type": "response.function_call_arguments.delta", "output_index": 0,
            "item_id": "fc_1", "delta": "{\"city\": \"beijing\"}"}), state))
        self.assertEqual(
            out2["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"],
            "{\"city\": \"beijing\"}")
        # done 已按增量透传过参数，不重复追加完整参数
        out3 = responses_api._translate_event("data: " + json.dumps({
            "type": "response.output_item.done", "output_index": 0,
            "item": {"id": "fc_1", "type": "function_call", "call_id": "fc_1",
                     "name": "get_weather", "arguments": "{\"city\": \"beijing\"}"}}), state)
        self.assertIsNone(out3)
        # 非标准上游只发 done 未发 delta：兜底补发一次完整参数
        state2: dict = {"args_seen": set()}
        out4 = json.loads(responses_api._translate_event("data: " + json.dumps({
            "type": "response.output_item.done", "output_index": 0,
            "item": {"id": "fc_2", "type": "function_call", "call_id": "fc_2",
                     "name": "get_weather", "arguments": "{}"}}), state2))
        self.assertEqual(
            out4["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], "{}")

    def test_responses_input_to_messages_roundtrip(self):
        from services import responses_api
        chat = responses_api.responses_to_chat_body({
            "model": "m",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]},
                      {"type": "function_call_output", "call_id": "c1", "output": "42"}]})
        self.assertEqual(chat["messages"][0]["role"], "user")
        self.assertEqual(chat["messages"][1]["role"], "tool")
        self.assertEqual(chat["messages"][1]["tool_call_id"], "c1")

    @staticmethod
    def _translate(ev: dict) -> dict | None:
        from services import responses_api
        out = responses_api._translate_event(
            "data: " + json.dumps(ev, ensure_ascii=False))
        return json.loads(out) if out else None

    def test_encrypted_reasoning_passed_through(self):
        """上游把推理内容加密时，原样透传 encrypted_content 供下游解密，不替换。"""
        out = self._translate({
            "type": "response.output_item.done", "output_index": 0,
            "item": {"id": "rs_1", "type": "reasoning", "status": "completed",
                     "encrypted_content": "Q-PaDgG1qC1DLLFH_xxxx", "summary": []}})
        self.assertIsNotNone(out)
        delta = out["choices"][0]["delta"]
        self.assertEqual(delta["reasoning_content"], "Q-PaDgG1qC1DLLFH_xxxx")

    def test_plaintext_summary_reasoning_translated(self):
        """OpenAI 标准明文 summary 推理事件 -> reasoning_content。"""
        out = self._translate({
            "type": "response.reasoning_summary_text.delta", "output_index": 0,
            "delta": "先计算 1+1"})
        self.assertEqual(out["choices"][0]["delta"]["reasoning_content"], "先计算 1+1")
        # 汇总事件（reasoning item done，带明文 summary）
        out = self._translate({
            "type": "response.output_item.done", "output_index": 0,
            "item": {"id": "rs_1", "type": "reasoning", "status": "completed",
                     "summary": [{"type": "summary_text", "text": "先计算 1+1"}]}})
        self.assertEqual(out["choices"][0]["delta"]["reasoning_content"], "先计算 1+1")

    def test_responses_payload_to_chat_carries_reasoning(self):
        """非流式 responses 响应中的推理条目 -> message.reasoning_content。"""
        from services import responses_api
        chat = responses_api.responses_payload_to_chat({
            "id": "resp_1", "object": "response", "created_at": 1,
            "model": "m", "status": "completed",
            "output": [
                {"id": "rs_1", "type": "reasoning", "status": "completed",
                 "encrypted_content": "Q-PaDgG1qC1DLLFH_xxxx", "summary": []},
                {"id": "m_1", "type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "答案是 2"}]},
            ],
            "usage": {"total_tokens": 8}})
        msg = chat["choices"][0]["message"]
        self.assertEqual(msg["content"], "答案是 2")
        self.assertEqual(msg["reasoning_content"], "Q-PaDgG1qC1DLLFH_xxxx")

    def test_chat_sse_to_responses_emits_reasoning_event(self):
        """内部 chat SSE 含 reasoning_content 时，/v1/responses 出口发出推理事件。"""
        from services import responses_api
        chat_iter = iter([
            'data: {"id":"c1","choices":[{"delta":{"role":"assistant",'
            '"reasoning_content":"思考中","content":""},"finish_reason":null}]}\n\n',
            'data: {"id":"c1","choices":[{"delta":{"content":"结果"},'
            '"finish_reason":null}]}\n\n',
            'data: [DONE]\n\n',
        ])

        async def _achunks():
            for c in chat_iter:
                yield c

        out = "".join(_acollect(
            responses_api.iter_chat_sse_as_responses(_achunks())))
        self.assertIn("response.reasoning_summary_text.delta", out)
        self.assertIn("思考中", out)


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


class DisabledChannelAccessTests(TestCase):
    """M1 回归：禁用渠道必须彻底下线，不得被 /c/<slug>/ 或 body.channel 显式寻址。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.channel = Channel.objects.create(
            name="Zen", slug="zen", base_url="https://z.test/v1", enabled=False)
        ChannelKey.objects.create(channel=self.channel, name="k1", api_key="k1")
        AIModel.objects.create(channel=self.channel, model_name="m1", enabled=True)
        _user, self.raw_key = api_key_service.create_key("tester")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def test_chat_by_slug_rejected_when_disabled(self):
        request = self.factory.post(
            "/c/zen/v1/chat/completions",
            data=json.dumps({"model": "m1",
                             "messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json", **self.auth)
        resp = openai_views.chat_completions(request, channel_slug="zen")
        self.assertEqual(resp.status_code, 404)

    def test_chat_by_body_channel_rejected_when_disabled(self):
        request = self.factory.post(
            "/v1/chat/completions",
            data=json.dumps({"model": "m1", "channel": "zen",
                             "messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json", **self.auth)
        resp = openai_views.chat_completions(request)
        self.assertEqual(resp.status_code, 404)

    def test_models_by_slug_rejected_when_disabled(self):
        request = self.factory.get("/c/zen/v1/models", **self.auth)
        resp = openai_views.list_models(request, channel_slug="zen")
        self.assertEqual(resp.status_code, 404)

    def test_reenable_restores_access(self):
        self.channel.enabled = True
        self.channel.save()
        request = self.factory.get("/c/zen/v1/models", **self.auth)
        resp = openai_views.list_models(request, channel_slug="zen")
        self.assertEqual(resp.status_code, 200)


class MuseReasoningDefaultSummaryTests(TestCase):
    """muse 思考修复回归：思考型模型走 Responses 端点时，即使客户端没传
    思考参数，也必须带 reasoning.summary，否则上游只回密文块。"""

    def test_known_thinking_model_gets_default_summary(self):
        from services import responses_api
        out = responses_api.chat_to_responses_body({
            "model": "muse-spark-1.2-contributor-free",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("reasoning", out)
        self.assertEqual(out["reasoning"]["summary"], "auto")
        self.assertEqual(out["include"], ["reasoning.encrypted_content"])

    def test_non_thinking_model_not_polluted(self):
        """非思考模型不得被强行注入 reasoning 字段。"""
        from services import responses_api
        out = responses_api.chat_to_responses_body({
            "model": "meta/llama-3.3-70b-instruct",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotIn("reasoning", out)

    def test_done_event_blob_passes_through(self):
        """零丢失契约：done 事件里的密文（非 Fernet）原样透传，不丢弃。

        旧契约是静默丢弃（防乱码）；零丢失原则下密文原样下发，
        客户端可保存回传上游做会话续写。
        """
        from services.responses_api import _translate_event
        import json as _json
        blob = "Q-PaDg" + "X7" * 400
        raw = "data: " + _json.dumps({
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "encrypted_content": blob,
                     "summary": []}}) + "\n\n"
        out = _translate_event(raw, {})
        self.assertIsNotNone(out)
        self.assertIn(blob, out)

    def test_done_event_no_duplicate_when_streamed(self):
        """summary 增量已流式下发过，done 不再重复整段。"""
        from services.responses_api import _translate_event
        import json as _json
        state = {}
        delta = "data: " + _json.dumps({
            "type": "response.reasoning_summary_text.delta",
            "delta": "thinking aloud..."}) + "\n\n"
        first = _json.loads(_translate_event(delta, state))
        self.assertIn("thinking aloud",
                      first["choices"][0]["delta"]["reasoning_content"])
        done = "data: " + _json.dumps({
            "type": "response.output_item.done",
            "item": {"type": "reasoning",
                     "summary": [{"type": "summary_text", "text": "thinking aloud..."}]}}) + "\n\n"
        self.assertIsNone(_translate_event(done, state))


class ToolNameAliasTests(TestCase):
    """超长工具名（上游 64 字符硬上限）的别名化与还原。"""

    def test_long_tool_name_shortened_and_restored(self):
        from services import tool_alias
        long_name = "mcp__my_server__" + "x" * 60  # 77 chars
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function",
                        "function": {"name": long_name,
                                     "parameters": {"type": "object"}}}],
        }
        mapping = tool_alias.shorten_function_names(body)
        self.assertTrue(mapping)
        aliased = body["tools"][0]["function"]["name"]
        self.assertNotEqual(aliased, long_name)
        self.assertLessEqual(len(aliased), 64)
        # 还原
        payload = {"choices": [{"message": {"tool_calls": [
            {"type": "function", "function": {"name": aliased}}]}}]}
        tool_alias.restore_payload(payload, mapping)
        self.assertEqual(payload["choices"][0]["message"]
                         ["tool_calls"][0]["function"]["name"], long_name)

    def test_short_names_untouched(self):
        from services import tool_alias
        body = {"model": "m", "messages": [],
                "tools": [{"type": "function", "function": {"name": "normal_tool"}}]}
        mapping = tool_alias.shorten_function_names(body)
        self.assertEqual(mapping, {})
        self.assertEqual(body["tools"][0]["function"]["name"], "normal_tool")

    def test_stream_chunk_restore(self):
        import json as _json
        from services import tool_alias
        mapping = {"fn_abc123": "mcp__srv__" + "y" * 50}
        chunk = "data: " + _json.dumps({"choices": [{"delta": {"tool_calls": [
            {"function": {"name": "fn_abc123"}}]}}]}) + "\n\n"
        out = tool_alias.restore_stream_chunk(chunk, mapping)
        self.assertIn("mcp__srv__", out)
        self.assertNotIn("fn_abc123", out)
        # 无命中时原样返回
        other = "data: " + _json.dumps({"choices": [{"delta": {"content": "x"}}]}) + "\n\n"
        self.assertEqual(tool_alias.restore_stream_chunk(other, mapping), other)
