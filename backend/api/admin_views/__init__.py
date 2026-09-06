"""admin_views 包：按资源拆分的管理视图，聚合导出保持旧引用兼容。"""
from __future__ import annotations

import time  # 兼容：原单文件模块的 `admin_views.time` 引用（测试 patch 用）

from .common import *  # noqa: F401,F403
# 测试与外部引用依赖的下划线符号（`from .common import *` 不导入 _ 前缀）
from .common import (  # noqa: F401
    _LOGIN_FAIL_LIMIT, _LOGIN_FAIL_WINDOW, _LOGIN_FAIL_SWEEP_THRESHOLD,
    _login_fail_bucket, _login_fail_lock, _login_client_key, _login_fail_exceeded,
)

from .login import (
    LoginView,
)
from .channels import (
    ChannelListView,
    ChannelDetailView,
    ChannelTestView,
)
from .chat import (
    AdminChatView,
)
from .dashboard import (
    DashboardView,
    DashboardUsageView,
)
from .keys import (
    ChannelKeyListView,
    ChannelKeyImportView,
    ChannelKeyDetailView,
    ChannelKeyTestView,
    ChannelKeyCleanupInvalidView,
    KeyBatchView,
)
from .logs import (
    LogListView,
    LogDetailView,
    LogCleanView,
    SecretAccessLogView,
)
from .models_admin import (
    ModelListView,
    ModelSyncView,
    ModelDetailView,
    ModelBatchView,
)
from .proxies import (
    ProxyListView,
    ProxyImportView,
    ProxyDetailView,
    ProxyTestView,
    ProxyFetchIpView,
    ProxyTestAllView,
    ProxyBatchView,
)
from .proxy_groups import (
    ProxyGroupListView,
    ProxyGroupDetailView,
)
from .settings import (
    SettingsView,
)
from .user_keys import (
    UserApiKeyListView,
    UserApiKeyDetailView,
)
