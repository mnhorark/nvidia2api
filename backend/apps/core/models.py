from __future__ import annotations

from django.db import models, transaction

from services.crypto import decrypt_secret, encrypt_secret


class Timestamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Channel（上游渠道）
# ---------------------------------------------------------------------------

CHAT_PATH_DEFAULT = "/chat/completions"
MODELS_PATH_DEFAULT = "/models"


def split_endpoint(url: str, suffix: str = CHAT_PATH_DEFAULT) -> tuple[str, str]:
    """把用户粘贴的完整端点拆成 (base_url, path)。

    支持直接粘贴 `https://host/v1/chat/completions` 这种完整地址，
    自动剥离出 base；也接受只写 base 的形式。

        https://opencode.ai/zen/v1/chat/completions
            -> ("https://opencode.ai/zen/v1", "/chat/completions")
        https://api.kilo.ai/api/gateway/chat/completions
            -> ("https://api.kilo.ai/api/gateway", "/chat/completions")
        https://integrate.api.nvidia.com/v1
            -> ("https://integrate.api.nvidia.com/v1", "/chat/completions")
    """
    raw = (url or "").strip()
    if not raw:
        return "", suffix
    u = raw.rstrip("/")
    low = u.lower()
    if low.endswith(suffix.lower()):
        return u[: -len(suffix)], suffix
    return u, suffix


def join_url(base: str, path: str) -> str:
    """把相对路径拼到 base 上，自动合并重叠的路径段。

    base=https://h.com/zen/v1, path=/v1/responses     -> https://h.com/zen/v1/responses
    base=https://h.com/zen/v1, path=/chat/completions -> https://h.com/zen/v1/chat/completions
    """
    base = (base or "").rstrip("/")
    path = (path or "").strip()
    if not path:
        return base
    if not path.startswith("/"):
        path = "/" + path
    # 取 base 的路径部分（剥掉 scheme://host）
    if "://" in base:
        _scheme, rest = base.split("://", 1)
        if "/" in rest:
            _host, base_path = rest.split("/", 1)
            base_path = "/" + base_path
        else:
            base_path = ""
    else:
        base_path = base
    # 找 base_path 后缀与 path 前缀的最大重叠段，合并去重（避免 /v1/v1 这类重复）
    overlap = 0
    for i in range(min(len(base_path), len(path)), 0, -1):
        if base_path[-i:] == path[:i]:
            overlap = i
            break
    return base + path[overlap:]


class AuthScheme(models.TextChoices):
    BEARER = "bearer", "Bearer Token"
    X_API_KEY = "x_api_key", "X-API-Key Header"
    NONE = "none", "无鉴权"


class Channel(Timestamped):
    """一个上游渠道 = 一个 OpenAI 兼容端点 + 独立的 Key/代理/模型/设置。"""

    name = models.CharField(max_length=64, unique=True)
    slug = models.SlugField(max_length=64, unique=True)
    base_url = models.CharField(max_length=512)
    chat_path = models.CharField(max_length=128, default=CHAT_PATH_DEFAULT)
    models_path = models.CharField(max_length=128, default=MODELS_PATH_DEFAULT)
    # 导入 Key 时的前缀提示（如 nvapi），仅用于自动命名与前端提示，不做强校验
    key_prefix = models.CharField(max_length=32, blank=True, default="")
    auth_scheme = models.CharField(
        max_length=16, choices=AuthScheme.choices, default=AuthScheme.BEARER
    )
    default_rpm = models.IntegerField(default=40)
    enabled = models.BooleanField(default=True, db_index=True)
    is_default = models.BooleanField(default=False, db_index=True)
    notes = models.TextField(blank=True, default="")
    # 公共 Key 渠道（如 Zen/Kilo）允许同渠道内复用同一 key 导入多条名额
    allow_duplicate_keys = models.BooleanField(default=False)
    # 关闭"无效"标记：公共/匿名 Key 渠道（如 Zen 的 public、无鉴权渠道）不因
    # 单次上游 401/403 把 Key 永久标记为 invalid（限流 429 / 冷却仍保留，会自动恢复）。
    disable_key_invalid = models.BooleanField(default=False)
    # 渠道级自动熔断：系统级连续失败（竞速全挂/5xx 等）达到阈值后进入冷却，
    # 冷却期间该渠道不参与线路构建，冷却结束自动恢复。
    consecutive_failures = models.IntegerField(default=0)
    cooldown_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "channel"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.base_url:
            self.base_url, detected = split_endpoint(self.base_url)
            if detected and not self.chat_path:
                self.chat_path = detected
        with transaction.atomic():
            if self.is_default:
                Channel.objects.exclude(pk=self.pk).filter(is_default=True).update(
                    is_default=False)
            super().save(*args, **kwargs)

    @property
    def chat_url(self) -> str:
        return join_url(self.base_url, self.chat_path)

    @property
    def models_url(self) -> str:
        return join_url(self.base_url, self.models_path)


# ---------------------------------------------------------------------------
# 渠道 Keys
# ---------------------------------------------------------------------------

class ChannelKeyStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    RATE_LIMITED = "rate_limited", "Rate limited"
    ERROR = "error", "Error"
    DISABLED = "disabled", "Disabled"
    INVALID = "invalid", "Invalid"


class ChannelKey(Timestamped):
    """某个渠道下的上游 API Key。"""

    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, related_name="keys",
        null=True, blank=True, db_index=True,
    )
    name = models.CharField(max_length=128)
    api_key = models.CharField(max_length=256)
    status = models.CharField(
        max_length=16, choices=ChannelKeyStatus.choices,
        default=ChannelKeyStatus.AVAILABLE, db_index=True,
    )
    rpm_limit = models.IntegerField(default=40)
    minute_window_start = models.DateTimeField(null=True, blank=True)
    minute_request_count = models.IntegerField(default=0)
    success_count = models.IntegerField(default=0)
    failure_count = models.IntegerField(default=0)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.CharField(max_length=256, blank=True, default="")

    class Meta:
        db_table = "channel_key"
        # 不建数据库层唯一约束:zen / kilo 等渠道允许导入重复 key,交由应用层控制
        constraints = []
        indexes = [models.Index(fields=["status", "last_used_at"])]

    def save(self, *args, **kwargs):
        # 敏感字段加密存储；幂等（已加密值跳过），旧明文在读取时自动回落
        if self.api_key:
            self.api_key = encrypt_secret(self.api_key)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ProxyGroup(Timestamped):
    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, related_name="proxy_groups",
        null=True, blank=True, db_index=True,
    )
    name = models.CharField(max_length=64)
    description = models.CharField(max_length=256, blank=True, default="")
    country = models.CharField(max_length=64, blank=True, default="")
    enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "proxy_group"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "name"], name="unique_channel_proxy_group"
            )
        ]

    def __str__(self):
        return self.name


class ProxyStatus(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    HEALTHY = "healthy", "Healthy"
    DEGRADED = "degraded", "Degraded"
    UNHEALTHY = "unhealthy", "Unhealthy"
    DISABLED = "disabled", "Disabled"


class Proxy(Timestamped):
    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, related_name="proxies",
        null=True, blank=True, db_index=True,
    )
    name = models.CharField(max_length=128)
    protocol = models.CharField(max_length=16, default="socks5")
    host = models.CharField(max_length=255)
    port = models.IntegerField()
    username = models.CharField(max_length=128, blank=True, default="")
    password = models.CharField(max_length=128, blank=True, default="")
    group = models.ForeignKey(
        ProxyGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name="proxies"
    )
    country = models.CharField(max_length=64, blank=True, default="")
    region = models.CharField(max_length=64, blank=True, default="")
    city = models.CharField(max_length=64, blank=True, default="")
    isp = models.CharField(max_length=128, blank=True, default="")
    enabled = models.BooleanField(default=False, db_index=True)
    status = models.CharField(
        max_length=16, choices=ProxyStatus.choices,
        default=ProxyStatus.UNKNOWN, db_index=True,
    )
    latency_ms = models.FloatField(null=True, blank=True)
    public_ip = models.CharField(max_length=64, blank=True, default="")
    last_check_at = models.DateTimeField(null=True, blank=True)
    success_count = models.IntegerField(default=0)
    failure_count = models.IntegerField(default=0)
    consecutive_failures = models.IntegerField(default=0)
    cooldown_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "proxy"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "protocol", "host", "port", "username"],
                name="unique_channel_proxy_endpoint",
            )
        ]
        indexes = [models.Index(fields=["enabled", "status"])]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # 敏感字段加密存储；幂等（已加密值跳过）
        if self.password:
            self.password = encrypt_secret(self.password)
        super().save(*args, **kwargs)

    @property
    def url(self) -> str:
        auth = f"{self.username}:{decrypt_secret(self.password)}@" if self.username else ""
        return f"{self.protocol}://{auth}{self.host}:{self.port}"


class AIModel(Timestamped):
    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, related_name="models",
        null=True, blank=True, db_index=True,
    )
    model_name = models.CharField(max_length=256)
    display_name = models.CharField(max_length=256, blank=True, default="")
    # 对外暴露的名字，参与 /v1 路由：留空则用 model_name
    alias = models.CharField(max_length=256, blank=True, default="", db_index=True)
    # 附加对外名（多别名）：一个模型可暴露多个可调用名字（参考 one-api 的
    # 模型映射思路）。全部对外名 = alias(主) + aliases(附加)。
    aliases = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, default="")
    provider = models.CharField(max_length=64, default="nvidia")
    status = models.CharField(max_length=16, default="active")
    enabled = models.BooleanField(default=False, db_index=True)
    # 跨渠道重名时的路由优先级，越大越优先
    route_priority = models.IntegerField(default=0, db_index=True)
    # 独立代理分组：设置后该模型的请求只走该分组内的代理线路
    proxy_group = models.ForeignKey(
        ProxyGroup, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="models", db_index=True,
    )
    # 模型独立端点覆盖：完整 URL 或相对路径（如 /v1/responses）；
    # 留空则用渠道的 chat 端点。供走 Response API 等非标准端点的模型使用。
    endpoint = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        db_table = "model"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "model_name"], name="unique_channel_model"
            )
        ]
        indexes = [models.Index(fields=["enabled", "model_name"])]

    def __str__(self):
        return self.model_name

    @property
    def public_name(self) -> str:
        """对外暴露的模型名：别名 > 显示名称 > 上游原始名。"""
        return ((self.alias or "").strip()
                or (self.display_name or "").strip()
                or self.model_name)


class UserApiKey(Timestamped):
    """平台对外发放的用户 Key：跨渠道共享，不属于任何渠道。"""

    name = models.CharField(max_length=128)
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    key_prefix = models.CharField(max_length=32)
    enabled = models.BooleanField(default=True)
    rate_limit = models.IntegerField(default=0, help_text="requests per minute, 0 = unlimited")
    # Token 额度体系：quota 为该 Key 的总 token 额度（0 = 不限），
    # used_quota 为累计消耗（input + output tokens，缓存部分按比例折算），
    # 达到额度后拒绝新请求并返回 402，直到管理员充值（增加 quota）。
    quota = models.BigIntegerField(default=0, help_text="total token quota, 0 = unlimited")
    used_quota = models.BigIntegerField(default=0, help_text="accumulated token usage")
    total_requests = models.IntegerField(default=0)
    success_requests = models.IntegerField(default=0)
    failed_requests = models.IntegerField(default=0)
    minute_window_start = models.DateTimeField(null=True, blank=True)
    minute_request_count = models.IntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "user_api_key"

    def __str__(self):
        return self.name


class RequestLog(models.Model):
    channel = models.ForeignKey(
        Channel, on_delete=models.SET_NULL, related_name="logs",
        null=True, blank=True, db_index=True,
    )
    request_id = models.CharField(max_length=40, db_index=True)
    user_api_key = models.ForeignKey(
        UserApiKey, null=True, blank=True, on_delete=models.SET_NULL, related_name="logs"
    )
    model = models.CharField(max_length=256, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    duration_ms = models.FloatField(default=0)
    status = models.CharField(max_length=16, default="pending", db_index=True)  # success / error
    http_status = models.IntegerField(default=0)
    error_type = models.CharField(max_length=64, blank=True, default="")
    winner_route_type = models.CharField(max_length=16, blank=True, default="")  # direct / proxy
    winner_key_name = models.CharField(max_length=128, blank=True, default="")
    winner_proxy_name = models.CharField(max_length=128, blank=True, default="")
    proxy_public_ip = models.CharField(max_length=64, blank=True, default="")
    is_stream = models.BooleanField(default=False)
    routes_count = models.IntegerField(default=0)
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    cached_tokens = models.IntegerField(default=0)          # 缓存读取的输入 token
    first_token_ms = models.FloatField(null=True, blank=True)  # 首 chunk 耗时（TTFT）
    # Full per-route outcome of the race: winner / failed / cancelled
    routes = models.JSONField(default=list, blank=True)
    # 客户端实际传入的思考参数（原始形态，含 extra_body 透传）
    client_thinking = models.JSONField(default=dict, blank=True)
    # 实际下发到上游的思考强度参数（reasoning_effort / reasoning_budget / chat_template_kwargs）
    upstream_thinking = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "request_log"
        indexes = [models.Index(fields=["created_at", "status"])]

    def __str__(self):
        return self.request_id


class SystemSetting(models.Model):
    """运行时参数。按渠道隔离：同一 key 在不同渠道可有不同取值。"""

    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, related_name="settings",
        null=True, blank=True, db_index=True,
    )
    key = models.CharField(max_length=64)
    value = models.TextField(blank=True, default="")
    description = models.CharField(max_length=256, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "system_setting"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "key"], name="unique_channel_setting"
            )
        ]

    def __str__(self):
        return self.key
