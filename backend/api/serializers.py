from rest_framework import serializers

from apps.core.models import (
    AIModel, Channel, ChannelKey, Proxy, ProxyGroup, RequestLog, SystemSetting,
    UserApiKey,
)
from services.key_service import mask_key


class ChannelSerializer(serializers.ModelSerializer):
    chat_url = serializers.CharField(read_only=True)
    models_url = serializers.CharField(read_only=True)
    key_count = serializers.IntegerField(read_only=True, default=0)
    enabled_key_count = serializers.IntegerField(read_only=True, default=0)
    proxy_count = serializers.IntegerField(read_only=True, default=0)
    enabled_proxy_count = serializers.IntegerField(read_only=True, default=0)
    model_count = serializers.IntegerField(read_only=True, default=0)
    enabled_model_count = serializers.IntegerField(read_only=True, default=0)
    in_cooldown = serializers.SerializerMethodField()

    class Meta:
        model = Channel
        fields = [
            "id", "name", "slug", "base_url", "chat_path", "models_path",
            "chat_url", "models_url", "key_prefix", "auth_scheme", "default_rpm",
            "allow_duplicate_keys",
            "disable_key_invalid",
            "disable_proxy_unhealthy",
            "enabled", "is_default", "notes",
            "consecutive_failures", "cooldown_until", "in_cooldown",
            "key_count", "enabled_key_count", "proxy_count", "enabled_proxy_count",
            "model_count", "enabled_model_count", "created_at", "updated_at",
        ]
        read_only_fields = ["slug"]

    def get_in_cooldown(self, obj) -> bool:
        from services.channel_health import is_open
        return is_open(obj)


class ChannelKeySerializer(serializers.ModelSerializer):
    api_key = serializers.SerializerMethodField()
    remaining_rpm = serializers.SerializerMethodField()
    is_anonymous = serializers.SerializerMethodField()

    class Meta:
        model = ChannelKey
        fields = [
            "id", "channel", "name", "api_key", "is_anonymous", "status", "rpm_limit",
            "minute_request_count", "remaining_rpm", "success_count", "failure_count",
            "last_used_at", "last_error", "created_at", "updated_at",
        ]

    def get_api_key(self, obj):
        # 列表接口绝不能逐行 Fernet 解密（千级 Key 下是主要延迟热点）：
        # 用保存时离线计算的提示位；空 hint（历史脏数据）回退一次解密。
        if obj.api_key_hint:
            return obj.api_key_hint
        if not obj.api_key:
            return ""
        from services.crypto import decrypt_secret
        return mask_key(decrypt_secret(obj.api_key))

    def get_is_anonymous(self, obj):
        return not obj.api_key

    def get_remaining_rpm(self, obj):
        from django.utils import timezone
        now = timezone.now()
        if obj.minute_window_start is None or (now - obj.minute_window_start).total_seconds() >= 60:
            return obj.rpm_limit
        return max(obj.rpm_limit - obj.minute_request_count, 0)


class ProxyGroupSerializer(serializers.ModelSerializer):
    proxy_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = ProxyGroup
        fields = ["id", "channel", "name", "description", "country", "enabled",
                  "proxy_count", "created_at", "updated_at"]


class ProxySerializer(serializers.ModelSerializer):
    password = serializers.SerializerMethodField()
    group_name = serializers.CharField(source="group.name", read_only=True, default="")
    url = serializers.SerializerMethodField()

    class Meta:
        model = Proxy
        fields = [
            "id", "channel", "name", "protocol", "host", "port", "username", "password",
            "group", "group_name", "country", "region", "city", "isp", "enabled",
            "status", "latency_ms", "public_ip", "last_check_at", "success_count",
            "failure_count", "consecutive_failures", "url", "created_at", "updated_at",
        ]

    def get_password(self, obj):
        return "••••••" if obj.password else ""

    def get_url(self, obj):
        return f"{obj.protocol}://{obj.host}:{obj.port}"


class ProxyWriteSerializer(serializers.ModelSerializer):
    protocol = serializers.ChoiceField(choices=["socks5", "socks5h", "http", "https"])

    class Meta:
        model = Proxy
        fields = ["name", "protocol", "host", "port", "username", "password", "group"]


class ModelSerializer(serializers.ModelSerializer):
    public_name = serializers.CharField(read_only=True)
    proxy_group_name = serializers.CharField(
        source="proxy_group.name", read_only=True, default="")
    # 附加对外名（多别名）：与 alias 一起构成该模型的全部可调用名字
    aliases = serializers.JSONField(required=False)

    class Meta:
        model = AIModel
        fields = ["id", "channel", "model_name", "display_name", "alias", "aliases",
                  "route_priority", "public_name", "description",
                  "proxy_group", "proxy_group_name", "endpoint",
                  "provider", "status", "enabled", "created_at", "updated_at"]


class UserApiKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = UserApiKey
        fields = ["id", "name", "key_prefix", "enabled", "rate_limit", "quota",
                  "used_quota", "total_requests", "success_requests", "failed_requests",
                  "last_used_at", "created_at", "updated_at"]


class RequestLogSerializer(serializers.ModelSerializer):
    # token 生成速度（tokens/s）：输出 tokens / 总耗时。
    # 口径对齐 new-api / one-api：速度 = completion_tokens / use_time（总耗时，秒）。
    # 不做 TTFT 扣除、也不设最小窗口截断——一方面上游网关会"批量冲刷"（整段
    # 流缓冲到最后一次性下发，首字≈总耗时），扣 TTFT 会把分母压到极小、算出
    # 938 tok/s 这类虚高值；另一方面短响应是正常样本，不应被 500ms 阈值吞掉。
    # 只要求"有输出 + 有耗时"即给出有效吞吐。首字延迟（TTFT）由 first_token_ms
    # 独立字段承载，无需在速度里二次扣除。
    generation_speed = serializers.SerializerMethodField()

    class Meta:
        model = RequestLog
        fields = ["id", "channel", "request_id", "model", "created_at", "duration_ms",
                  "status", "http_status", "error_type", "winner_route_type",
                  "winner_key_name", "winner_proxy_name", "proxy_public_ip", "is_stream",
                  "routes_count", "prompt_tokens", "completion_tokens", "total_tokens",
                  "cached_tokens", "first_token_ms", "generation_speed", "routes",
                  "client_thinking", "upstream_thinking", "request_summary",
                  # 交付量观测：回答"截断前这条流到底有没有在动"——静默被掐该换线，
                  # 思考流了很久被掐绝不能换线，两者在 completion_tokens 上同形（都 0）
                  "stream_chunks", "content_chars", "reasoning_chars"]

    def get_generation_speed(self, obj) -> float | None:
        completion = obj.completion_tokens or 0
        duration_ms = obj.duration_ms or 0
        if completion <= 0 or duration_ms <= 0:
            return None
        # 保留 2 位小数：慢模型（<1 tok/s）不至于被 round(…,1) 压成 0.0 显示成"—"
        return round(completion / (duration_ms / 1000.0), 2)


class RequestLogListSerializer(RequestLogSerializer):
    """列表用轻量序列化：剔除高成本的明细字段。

    `routes`（每条日志最多 50 条线路竞速明细）、`client_thinking` / `upstream_thinking`
    仅在展开单条日志时才需要；列表每次轮询序列化 100 条时把它们整包带上会让响应
    体积与序列化耗时翻数倍，是"日志页加载缓慢/加载失败"的主因。明细改由
    LogDetailView（GET /api/admin/logs/<id>）按需返回全量序列化。
    """

    class Meta(RequestLogSerializer.Meta):
        fields = [
            "id", "request_id", "model", "created_at", "duration_ms",
            "status", "http_status", "error_type", "winner_route_type",
            "winner_key_name", "winner_proxy_name", "proxy_public_ip", "is_stream",
            "routes_count", "prompt_tokens", "completion_tokens", "total_tokens",
            "cached_tokens", "first_token_ms", "generation_speed",
        ]


class SettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemSetting
        fields = ["id", "key", "value", "description", "updated_at"]
