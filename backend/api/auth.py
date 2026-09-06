import functools
import hmac

from django.conf import settings

# `openai_error` 的实现已并入 api/errors.py（统一错误信封）。这里保留同名
# 导出，历史调用方 `from .auth import openai_error` 与既有测试无需改动。
from .errors import openai_error  # noqa: F401  (re-export)


def valid_admin_tokens() -> tuple[str, ...]:
    """当前生效的管理 Token 集合（支持多值轮换，见 settings.ADMIN_TOKENS）。"""
    tokens = getattr(settings, "ADMIN_TOKENS", None)
    if not tokens:
        single = str(getattr(settings, "ADMIN_TOKEN", "") or "")
        tokens = tuple(t.strip() for t in single.split(",") if t.strip())
    return tuple(tokens)


def admin_token_matches(token: str) -> bool:
    """常量时间比较，且遍历全部候选避免通过响应时间探测命中的是第几个。"""
    if not token:
        return False
    matched = False
    for candidate in valid_admin_tokens():
        matched |= hmac.compare_digest(token, str(candidate))
    return matched


def admin_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth[6:].strip() if auth.lower().startswith("token ") else ""
        if not admin_token_matches(token):
            # 与 DRF 侧同信封（api/errors.py）：本装饰器是纯 Django 层，
            # 走不到 DRF 的 EXCEPTION_HANDLER，所以这里自己发信封。
            return openai_error(
                "Authentication credentials were not provided.",
                "not_authenticated", 401, "authentication_error")
        return view(request, *args, **kwargs)
    return wrapper


class AdminRequiredMixin:
    @classmethod
    def as_view(cls, **initkwargs):
        return admin_required(super().as_view(**initkwargs))
