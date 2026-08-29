import functools
import hmac

from django.conf import settings
from django.http import JsonResponse


def openai_error(message, code, status, type_="api_error"):
    return JsonResponse(
        {"error": {"message": message, "type": type_, "param": None, "code": code}},
        status=status,
    )


def admin_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth[6:].strip() if auth.lower().startswith("token ") else ""
        if not token or not hmac.compare_digest(token, str(settings.ADMIN_TOKEN)):
            return JsonResponse({"detail": "Authentication credentials were not provided."}, status=401)
        return view(request, *args, **kwargs)
    return wrapper


class AdminRequiredMixin:
    @classmethod
    def as_view(cls, **initkwargs):
        return admin_required(super().as_view(**initkwargs))
