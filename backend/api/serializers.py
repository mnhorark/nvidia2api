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
    # token 生成速度（tokens/s）：输出 tokens / 生成耗时。
    # 口径与 new-api 日志「速度」列一致：流式扣除首字耗时（TTFT），非流式用总耗时。
    # 补充保护：流式生成阶段过短（首字≈总耗时、输出一次性涌入）时样本无统计意义，
    # 会算出虚高的 tok/s，此时不展示。new-api 仅做 genTime>0 判断，本实现更稳健。
    generation_speed = serializers.SerializerMethodField()

    class Meta:
        model = RequestLog
        fields = ["id", "channel", "request_id", "model", "created_at", "duration_ms",
                  "status", "http_status", "error_type", "winner_route_type",
                  "winner_key_name", "winner_proxy_name", "proxy_public_ip", "is_stream",
                  "routes_count", "prompt_tokens", "completion_tokens", "total_tokens",
                  "cached_tokens", "first_token_ms", "generation_speed", "routes",
                  "client_thinking", "upstream_thinking"]

    # 流式生成阶段的最小统计窗口：低于该值（毫秒）视为样本不可靠，不展示速度
    _MIN_STREAM_GEN_MS = 500

    def get_generation_speed(self, obj) -> float | None:
        duration_ms = obj.duration_ms or 0
        if duration_ms <= 0:
            return None
        completion = obj.completion_tokens or 0
        if completion <= 0:
            return None
        gen_ms = duration_ms
        if obj.is_stream and obj.first_token_ms:
            if obj.first_token_ms >= duration_ms:
                return None
            gen_ms = duration_ms - obj.first_token_ms
            if gen_ms < self._MIN_STREAM_GEN_MS:
                return None
        return round(completion / (gen_ms / 1000.0), 1)


class SettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemSetting
        fields = ["id", "key", "value", "description", "updated_at"]
