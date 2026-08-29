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

    class Meta:
        model = Channel
        fields = [
            "id", "name", "slug", "base_url", "chat_path", "models_path",
            "chat_url", "models_url", "key_prefix", "auth_scheme", "default_rpm",
            "allow_duplicate_keys",
            "enabled", "is_default", "notes",
            "key_count", "enabled_key_count", "proxy_count", "enabled_proxy_count",
            "model_count", "enabled_model_count", "created_at", "updated_at",
        ]
        read_only_fields = ["slug"]


class ChannelKeySerializer(serializers.ModelSerializer):
    api_key = serializers.SerializerMethodField()
    remaining_rpm = serializers.SerializerMethodField()

    class Meta:
        model = ChannelKey
        fields = [
            "id", "channel", "name", "api_key", "status", "rpm_limit",
            "minute_request_count", "remaining_rpm", "success_count", "failure_count",
            "last_used_at", "last_error", "created_at", "updated_at",
        ]

    def get_api_key(self, obj):
        return mask_key(obj.api_key)

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

    class Meta:
        model = AIModel
        fields = ["id", "channel", "model_name", "display_name", "alias",
                  "route_priority", "public_name", "description",
                  "provider", "status", "enabled", "created_at", "updated_at"]


class UserApiKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = UserApiKey
        fields = ["id", "name", "key_prefix", "enabled", "rate_limit", "total_requests",
                  "success_requests", "failed_requests", "last_used_at", "created_at",
                  "updated_at"]


class RequestLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = RequestLog
        fields = ["id", "channel", "request_id", "model", "created_at", "duration_ms",
                  "status", "http_status", "error_type", "winner_route_type",
                  "winner_key_name", "winner_proxy_name", "proxy_public_ip", "is_stream",
                  "routes_count", "prompt_tokens", "completion_tokens", "total_tokens",
                  "cached_tokens", "first_token_ms", "routes"]


class SettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemSetting
        fields = ["id", "key", "value", "description", "updated_at"]
