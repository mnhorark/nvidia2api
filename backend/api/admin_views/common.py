"""Admin 辅助函数与登录限流（由 admin_views.py 拆分而来）。"""
"""Admin API：所有资源按渠道隔离，渠道由 `X-Channel` 头或 `?channel=` 决定。

本文件由原单文件 admin_views.py 按资源拆分（new-api 按资源分 handler 的理念），
模块划分见包内各文件；`__init__.py` 聚合导出保持 `from . import admin_views`
兼容（urls.py 与测试无需改动）。
"""

import threading
import time
from datetime import timedelta

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import (
    AIModel, Channel, ChannelKey, ChannelKeyStatus, Proxy, ProxyGroup, ProxyStatus,
    RequestLog, SystemSetting, UserApiKey,
)
from services import (
    api_key_service, channel_service, key_service, model_registry, proxy_service,
    thinking, upstream_service,
)
from services.proxy_checker import check_all, check_proxy

from ..auth import AdminRequiredMixin
from ..errors import admin_error

def current_channel(request) -> Channel:
    return channel_service.resolve_from_request(request)

def _parse_int(value):
    """宽松转 int；非法值返回 None（由调用方决定返回 400）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

def _bad_param(name: str) -> Response:
    return admin_error(f'参数 {name} 必须是整数', 'bad_request', 400,
                       'invalid_request_error', param=name)

def _bad_bool(name: str) -> Response:
    return admin_error(f'参数 {name} 必须是布尔值', 'bad_request', 400,
                       'invalid_request_error', param=name)

_TRUE_LITERALS = {'1', 'true', 'yes', 'on'}

_FALSE_LITERALS = {'0', 'false', 'no', 'off', ''}

def _parse_bool(value):
    """显式解析布尔值；无法识别返回 None。

    不能用 `bool(value)`：Python 里 `bool("false")` 是 True，前端传字符串
    "false" 意为关闭，却会被静默当成开启。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)) and (not isinstance(value, bool)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_LITERALS:
        return True
    if text in _FALSE_LITERALS:
        return False
    return None

def _require_bool(data: dict, name: str):
    """从请求数据取布尔字段；非法返回 (None, error_response)。"""
    if name not in data:
        return (None, None)
    parsed = _parse_bool(data[name])
    if parsed is None:
        return (None, _bad_bool(name))
    return (parsed, None)

def _apply_bool(data: dict, name: str, obj, field: str | None=None):
    """把请求里的布尔字段写到对象上；非法返回 error_response，成功返回 None。"""
    parsed, err = _require_bool(data, name)
    if err is not None:
        return err
    if parsed is not None:
        setattr(obj, field or name, parsed)
    return None

def _require_int(data: dict, name: str, *, minimum: int | None=None, maximum: int | None=None):
    """从请求数据取整数字段；非法/越界返回 (None, error_response)。"""
    if name not in data:
        return (None, None)
    parsed = _parse_int(data[name])
    if parsed is None:
        return (None, _bad_param(name))
    if minimum is not None and parsed < minimum:
        return (None, admin_error(f'参数 {name} 不能小于 {minimum}', 'bad_request', 400))
    if maximum is not None and parsed > maximum:
        return (None, admin_error(f'参数 {name} 不能大于 {maximum}', 'bad_request', 400))
    return (parsed, None)

_LOGIN_FAIL_LIMIT = 10

_LOGIN_FAIL_WINDOW = 60.0

_LOGIN_FAIL_SWEEP_THRESHOLD = 256
_login_fail_lock = threading.Lock()
_login_fail_bucket: dict[str, list[float]] = {}

def _login_client_key(request) -> str:
    """限流维度：反代后 REMOTE_ADDR 全是网关地址，全员共桶会被他人拖累锁定。

    取 X-Forwarded-For 首跳参与分桶，使反代后的不同真实客户端各自计数。
    该值可被伪造，因此只作为"防误伤"的可用性措施，不作为防爆破的唯一手段
    ——真正的防线是强口令（生产环境已由 settings 门禁强制非默认凭据）。
    """
    remote = request.META.get('REMOTE_ADDR', '') or 'unknown'
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '') or ''
    hop = xff.split(',')[0].strip() if xff else ''
    return f'{remote}|{hop}' if hop else remote

def _login_fail_exceeded(key: str) -> bool:
    now = time.monotonic()
    with _login_fail_lock:
        bucket = [t for t in _login_fail_bucket.get(key, []) if now - t < _LOGIN_FAIL_WINDOW]
        if len(bucket) >= _LOGIN_FAIL_LIMIT:
            _login_fail_bucket[key] = bucket
            return True
        bucket.append(now)
        _login_fail_bucket[key] = bucket
        if len(_login_fail_bucket) > _LOGIN_FAIL_SWEEP_THRESHOLD:
            for k in [k for k, v in _login_fail_bucket.items() if not v or now - v[-1] >= _LOGIN_FAIL_WINDOW]:
                _login_fail_bucket.pop(k, None)
        return False

def _slugify(name: str) -> str:
    import re
    slug = re.sub('[^a-zA-Z0-9]+', '-', name).strip('-').lower()
    return slug or 'channel'

def _parse_ids(request) -> list[int]:
    ids = request.data.get('ids') or []
    if not isinstance(ids, list):
        return []
    return [int(i) for i in ids if str(i).isdigit()]

def _normalize_aliases(value) -> list[str]:
    """规范化附加别名：支持 JSON 数组或逗号分隔字符串；去空格、去空、去重。"""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        for part in str(item or '').split(','):
            p = part.strip()
            if p and p not in out:
                out.append(p)
    return out


__all__ = [
    'current_channel',
    # 统一错误信封：各资源视图靠 `from .common import *` 拿到它，
    # 必须显式列进 __all__（本模块有 __all__，star import 只认这里列的）
    'admin_error',
    '_parse_int',
    '_bad_param',
    '_bad_bool',
    '_parse_bool',
    '_require_bool',
    '_apply_bool',
    '_require_int',
    '_login_client_key',
    '_login_fail_exceeded',
    '_slugify',
    '_parse_ids',
    '_normalize_aliases',
    '_TRUE_LITERALS',
    '_FALSE_LITERALS',
    '_LOGIN_FAIL_LIMIT',
    '_LOGIN_FAIL_WINDOW',
    '_LOGIN_FAIL_SWEEP_THRESHOLD',
    '_login_fail_lock',
    '_login_fail_bucket',
]
