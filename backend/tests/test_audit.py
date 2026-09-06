"""敏感操作审计（services/audit_service + SecretAccessLog）。

要守的性质有两条，缺一不可：
1. 明文回看**必须**留下流水（否则 Token 泄漏后无法界定影响面）；
2. 审计**绝不能**成为新的泄漏面或可用性故障源——不记明文、写失败不影响请求。
"""
from __future__ import annotations

import json
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, TestCase

from api import admin_views
from apps.core.models import Channel, ChannelKey, SecretAccessAction, SecretAccessLog
from services import audit_service


class RevealAuditTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.auth = f"Token {settings.ADMIN_TOKEN}"
        self.channel = Channel.objects.create(name="A", slug="a",
                                              base_url="https://a.test/v1")
        self.key = ChannelKey.objects.create(channel=self.channel, name="主账号-01",
                                             api_key="nvapi-secret-value-123")

    def _get_view(self):
        return admin_views.ChannelKeyDetailView.as_view()

    def test_reveal_writes_one_audit_row(self):
        resp = self._get_view()(
            self.factory.get(f"/api/admin/keys/{self.key.pk}?reveal=1",
                             HTTP_AUTHORIZATION=self.auth,
                             REMOTE_ADDR="203.0.113.9",
                             HTTP_X_FORWARDED_FOR="198.51.100.7, 10.0.0.1",
                             HTTP_USER_AGENT="curl/8"),
            pk=self.key.pk)
        self.assertEqual(resp.status_code, 200)
        rows = SecretAccessLog.objects.all()
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertEqual(row.action, SecretAccessAction.REVEAL_KEY)
        self.assertEqual(row.channel_id, self.channel.pk)
        self.assertEqual(row.target_id, self.key.pk)
        self.assertEqual(row.target_name, "主账号-01")
        self.assertEqual(row.remote_addr, "203.0.113.9")
        self.assertEqual(row.forwarded_for, "198.51.100.7, 10.0.0.1")
        self.assertEqual(row.user_agent, "curl/8")

    def test_plain_get_writes_no_audit(self):
        view = self._get_view()
        view(self.factory.get(f"/api/admin/keys/{self.key.pk}",
                              HTTP_AUTHORIZATION=self.auth), pk=self.key.pk)
        self.assertEqual(SecretAccessLog.objects.count(), 0)

    def test_reveal_still_returns_plaintext_but_audit_does_not(self):
        """审计表只记"看了哪条"，绝不记"看到了什么"。"""
        view = self._get_view()
        resp = view(self.factory.get(f"/api/admin/keys/{self.key.pk}?reveal=1",
                                     HTTP_AUTHORIZATION=self.auth),
                    pk=self.key.pk)
        from services.crypto import decrypt_secret
        self.assertEqual(resp.data["api_key"], decrypt_secret(self.key.api_key))
        blob = json.dumps([
            {f: str(getattr(r, f)) for f in (
                "action", "target_name", "remote_addr", "forwarded_for",
                "user_agent")}
            for r in SecretAccessLog.objects.all()], ensure_ascii=False)
        self.assertNotIn("nvapi-secret-value-123", blob)

    def test_audit_write_failure_does_not_break_reveal(self):
        """安全功能绝不能变成可用性故障：写不进审计也要正常应答。"""
        with patch.object(SecretAccessLog, "objects") as objects:
            objects.create.side_effect = RuntimeError("db locked")
            resp = self._get_view()(
                self.factory.get(f"/api/admin/keys/{self.key.pk}?reveal=1",
                                 HTTP_AUTHORIZATION=self.auth), pk=self.key.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["api_key"])


class AuditQueryEndpointTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.auth = f"Token {settings.ADMIN_TOKEN}"
        self.a = Channel.objects.create(name="A2", slug="a2",
                                        base_url="https://a2.test/v1")
        self.b = Channel.objects.create(name="B2", slug="b2",
                                        base_url="https://b2.test/v1")
        for ch in (self.a, self.b):
            k = ChannelKey.objects.create(channel=ch, name=f"k-{ch.slug}",
                                          api_key="nvapi-x")
            audit_service.log_secret_access(
                SecretAccessAction.REVEAL_KEY, channel=ch, target=k)

    def _view(self):
        return admin_views.SecretAccessLogView.as_view()

    def test_requires_admin_token(self):
        resp = self._view()(self.factory.get("/api/admin/audit/secret-access"))
        self.assertEqual(resp.status_code, 401)

    def test_lists_cross_channel_by_default(self):
        resp = self._view()(self.factory.get(
            "/api/admin/audit/secret-access", HTTP_AUTHORIZATION=self.auth))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["total"], 2)
        self.assertEqual({r["channel"] for r in resp.data["results"]},
                         {"a2", "b2"})

    def test_channel_filter(self):
        resp = self._view()(self.factory.get(
            "/api/admin/audit/secret-access?channel=b2",
            HTTP_AUTHORIZATION=self.auth))
        self.assertEqual(resp.data["total"], 1)
        self.assertEqual(resp.data["results"][0]["channel"], "b2")

    def test_bad_limit_returns_envelope(self):
        resp = self._view()(self.factory.get(
            "/api/admin/audit/secret-access?limit=abc",
            HTTP_AUTHORIZATION=self.auth))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.data["error"]),
                         {"message", "type", "param", "code"})
        self.assertEqual(resp.data["error"]["param"], "limit")
