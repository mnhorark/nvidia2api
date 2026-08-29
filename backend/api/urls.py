from django.urls import path

from . import admin_views, openai_views

urlpatterns = [
    # OpenAI-compatible
    # /v1/*                      -> 平台默认渠道
    # /c/<slug>/v1/*             -> 指定渠道，例如 /c/zen/v1/chat/completions
    path("v1/models", openai_views.list_models),
    path("v1/chat/completions", openai_views.chat_completions),
    path("v1/responses", openai_views.responses),
    path("c/<slug:channel_slug>/v1/models", openai_views.list_models),
    path("c/<slug:channel_slug>/v1/chat/completions", openai_views.chat_completions),
    path("c/<slug:channel_slug>/v1/responses", openai_views.responses),

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
]
