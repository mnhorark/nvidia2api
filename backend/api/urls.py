from django.urls import path, re_path

from . import admin_views, frontend_views, health_views, openai_views

urlpatterns = [
    # 健康检查 / 可观测性
    path("healthz", health_views.liveness),
    path("metrics", health_views.metrics),
    path("api/admin/health", health_views.admin_health),

    # OpenAI-compatible
    # /v1/*                      -> 平台默认渠道
    # /c/<slug>/v1/*             -> 指定渠道，例如 /c/zen/v1/chat/completions
    path("v1/models", openai_views.list_models),
    path("v1/chat/completions", openai_views.chat_completions),
    path("v1/responses", openai_views.responses),
    path("v1/messages", openai_views.anthropic_messages),
    path("v1/messages/count_tokens", openai_views.anthropic_count_tokens),
    path("c/<slug:channel_slug>/v1/models", openai_views.list_models),
    path("c/<slug:channel_slug>/v1/chat/completions", openai_views.chat_completions),
    path("c/<slug:channel_slug>/v1/responses", openai_views.responses),
    path("c/<slug:channel_slug>/v1/messages", openai_views.anthropic_messages),
    path("c/<slug:channel_slug>/v1/messages/count_tokens", openai_views.anthropic_count_tokens),

    # Admin
    path("api/admin/login", admin_views.LoginView.as_view()),
    path("api/admin/dashboard", admin_views.DashboardView.as_view()),
    path("api/admin/dashboard/usage", admin_views.DashboardUsageView.as_view()),
    path("api/admin/chat", admin_views.AdminChatView.as_view()),
    path("api/admin/settings", admin_views.SettingsView.as_view()),

    # Channels
    path("api/admin/channels", admin_views.ChannelListView.as_view()),
    path("api/admin/channels/<int:pk>", admin_views.ChannelDetailView.as_view()),
    path("api/admin/channels/<int:pk>/test", admin_views.ChannelTestView.as_view()),

    # 渠道 Keys（兼容旧路径 /api/admin/nvidia-keys/*）
    path("api/admin/keys", admin_views.ChannelKeyListView.as_view()),
    path("api/admin/keys/batch", admin_views.KeyBatchView.as_view()),
    path("api/admin/keys/import", admin_views.ChannelKeyImportView.as_view()),
    path("api/admin/keys/cleanup-invalid", admin_views.ChannelKeyCleanupInvalidView.as_view()),
    path("api/admin/keys/<int:pk>", admin_views.ChannelKeyDetailView.as_view()),
    path("api/admin/keys/<int:pk>/test", admin_views.ChannelKeyTestView.as_view()),
    path("api/admin/nvidia-keys", admin_views.ChannelKeyListView.as_view()),
    path("api/admin/nvidia-keys/import", admin_views.ChannelKeyImportView.as_view()),
    path("api/admin/nvidia-keys/<int:pk>", admin_views.ChannelKeyDetailView.as_view()),
    path("api/admin/nvidia-keys/<int:pk>/test", admin_views.ChannelKeyTestView.as_view()),

    path("api/admin/proxies", admin_views.ProxyListView.as_view()),
    path("api/admin/proxies/batch", admin_views.ProxyBatchView.as_view()),
    path("api/admin/proxies/import", admin_views.ProxyImportView.as_view()),
    path("api/admin/proxies/test-all", admin_views.ProxyTestAllView.as_view()),
    path("api/admin/proxies/<int:pk>", admin_views.ProxyDetailView.as_view()),
    path("api/admin/proxies/<int:pk>/test", admin_views.ProxyTestView.as_view()),
    path("api/admin/proxies/<int:pk>/fetch-ip", admin_views.ProxyFetchIpView.as_view()),

    path("api/admin/proxy-groups", admin_views.ProxyGroupListView.as_view()),
    path("api/admin/proxy-groups/<int:pk>", admin_views.ProxyGroupDetailView.as_view()),

    path("api/admin/models", admin_views.ModelListView.as_view()),
    path("api/admin/models/batch", admin_views.ModelBatchView.as_view()),
    path("api/admin/models/sync", admin_views.ModelSyncView.as_view()),
    path("api/admin/models/<int:pk>", admin_views.ModelDetailView.as_view()),

    path("api/admin/api-keys", admin_views.UserApiKeyListView.as_view()),
    path("api/admin/api-keys/<int:pk>", admin_views.UserApiKeyDetailView.as_view()),

    path("api/admin/logs", admin_views.LogListView.as_view()),
    path("api/admin/logs/clean", admin_views.LogCleanView.as_view()),
    path("api/admin/logs/<int:pk>", admin_views.LogDetailView.as_view()),

    # 敏感操作审计（明文回看上游 Key 等），默认跨渠道
    path("api/admin/audit/secret-access",
         admin_views.SecretAccessLogView.as_view()),

    # ---- 前端静态托管（all-in-one 镜像）----
    # 必须放在最后，且用否定预查排除 API 前缀：未知 API 路径仍然要返回 404，
    # 不能被前端 index.html 吞掉（否则客户端会把 404 HTML 当成 JSON 解析失败）。
    path("", frontend_views.frontend_root),
    re_path(r"^(?!api/|v1/|c/|healthz|metrics)(?P<path>.*)$",
            frontend_views.serve_frontend),
]
