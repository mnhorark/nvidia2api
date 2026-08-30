"""用户 API Key Token 额度（quota）体系测试。"""
import json
import threading

from django.test import RequestFactory, TestCase, TransactionTestCase
from django.conf import settings

from api import openai_views
from services import api_key_service


class QuotaServiceTests(TestCase):
    def test_quota_disabled_by_default(self):
        rec, _ = api_key_service.create_key("t")
        self.assertFalse(api_key_service.quota_enabled(rec))
        self.assertTrue(api_key_service.check_quota(rec)[0])

    def test_record_usage_accumulates(self):
        rec, _ = api_key_service.create_key("t", quota=100)
        api_key_service.record_usage(rec, prompt_tokens=30, completion_tokens=20)
        rec.refresh_from_db()
        self.assertEqual(rec.used_quota, 50)

    def test_cached_tokens_not_billed(self):
        rec, _ = api_key_service.create_key("t", quota=100)
        api_key_service.record_usage(rec, prompt_tokens=50, completion_tokens=10,
                                     cached_tokens=40)
        rec.refresh_from_db()
        # 仅计费：10（未缓存输入）+ 10（输出）= 20
        self.assertEqual(rec.used_quota, 20)

    def test_quota_exceeded_rejected(self):
        rec, _ = api_key_service.create_key("t", quota=100)
        rec.used_quota = 100
        rec.save()
        ok, reason = api_key_service.check_quota(rec)
        self.assertFalse(ok)
        self.assertEqual(reason, "quota_exceeded")


class ConcurrentUserRateLimitTests(TransactionTestCase):
    def test_concurrent_requests_do_not_exceed_user_rate_limit(self):
        rec, _ = api_key_service.create_key("rl", rate_limit=5)
        results = []

        def worker():
            results.append(api_key_service.check_and_count(rec)[0])

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 5)
        rec.refresh_from_db()
        self.assertEqual(rec.minute_request_count, 5)


class QuotaApiTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _post(self, view, path, data, **extra):
        return view(self.factory.post(path, data=json.dumps(data),
                                      content_type="application/json", **extra))

    def _patch(self, view, path, data, **extra):
        return view(self.factory.patch(path, data=json.dumps(data),
                                       content_type="application/json", **extra))

    def _patch_detail(self, view, pk, data, **extra):
        return view(self.factory.patch(f"/api/admin/api-keys/{pk}",
                                       data=json.dumps(data),
                                       content_type="application/json", **extra),
                    pk=pk)

    def test_quota_exceeded_returns_402(self):
        from apps.core.models import AIModel, Channel, ChannelKey
        from services import channel_service
        channel = channel_service.ensure_default_channel()
        ChannelKey.objects.create(channel=channel, name="k", api_key="nvapi-x",
                                  rpm_limit=100)
        AIModel.objects.create(channel=channel, model_name="m", enabled=True)
        rec, raw = api_key_service.create_key("quota-user", quota=0)
        rec.used_quota = 100  # 即使 0 表示不限也测一下 402 通道
        rec.quota = 100
        rec.save()
        resp = self._post(openai_views.chat_completions, "/v1/chat/completions", {
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
        }, HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(resp.status_code, 402)
        data = json.loads(resp.content)
        self.assertEqual(data["error"]["code"], "insufficient_quota")

    def test_admin_create_with_quota(self):
        from api import admin_views
        resp = self._post(admin_views.UserApiKeyListView.as_view(),
                          "/api/admin/api-keys",
                          {"name": "q", "quota": 5000, "rate_limit": 10},
                          HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["quota"], 5000)
        self.assertEqual(resp.data["used_quota"], 0)

    def test_admin_patch_quota(self):
        from api import admin_views
        rec, _ = api_key_service.create_key("q2")
        resp = self._patch_detail(admin_views.UserApiKeyDetailView.as_view(),
                                  rec.id,
                                  {"quota": 999},
                                  HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["quota"], 999)
        rec.refresh_from_db()
        self.assertEqual(rec.quota, 999)
