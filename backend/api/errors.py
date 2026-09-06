"""统一错误信封。

## 为什么需要这个模块

管理端历史上同时存在**四种**错误形态：

1. 数据面 OpenAI 风格 `{"error": {"message", "type", "param", "code"}}`
2. 管理端简版 `{"error": {"message", "code"}}`（缺 type/param）
3. DRF 原生 `{"detail": "..."}`（手写 404 与 DRF 框架错误）
4. `{"error": "log_not_found"}` —— `error` 是**字符串**而非对象

后果是任何管理端客户端都没法稳定读 `error.message`，序列化校验失败时
前端只能显示"请求失败 (HTTP 400)"这种零信息文案。

本模块把三者收敛到**同一个信封**：`{"error": {message, type, param, code}}`。

- `openai_error`：数据面（纯 Django 视图）用，返回 `JsonResponse`
- `admin_error`：管理面（DRF APIView）用，返回 DRF `Response`
- `admin_exception_handler`：DRF 框架自身抛出的错误（序列化校验、
  方法不允许、DRF 内置 404 等）也走同一信封

前端 `extractErrorMessage` 早已同时解析 `detail|message|error.message|error`，
因此这次收敛不需要前端改动；`detail` 分支保留是为了兼容旧版服务端。
"""
from __future__ import annotations

from django.http import JsonResponse
from rest_framework import status as http_status
from rest_framework.exceptions import (
    APIException, AuthenticationFailed, MethodNotAllowed, NotAuthenticated,
    NotFound, PermissionDenied, Throttled, ValidationError,
)
from rest_framework.response import Response


def _envelope(message: str, code: str, type_: str,
              param: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_,
                      "param": param, "code": code}}


def openai_error(message, code, status, type_="api_error", param=None):
    """数据面错误（/v1/*）：OpenAI 官方信封，JsonResponse。"""
    return JsonResponse(_envelope(message, code, type_, param), status=status)


def _type_for_status(status: int) -> str:
    """按 HTTP 状态推导 OpenAI 的 error.type，避免每个调用点自己猜。"""
    return {
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        405: "invalid_request_error",
        408: "timeout_error",
        409: "invalid_request_error",
        413: "invalid_request_error",
        429: "rate_limit_error",
    }.get(int(status), "api_error" if int(status) >= 500
          else "invalid_request_error")


def admin_error(message, code, status=http_status.HTTP_400_BAD_REQUEST,
                type_: str | None = None, param: str | None = None) -> Response:
    """管理面错误（/api/admin/*）：与数据面同构的信封，DRF Response。

    `type_` 省略时按状态码推导，调用点只需关心 message/code。
    """
    return Response(_envelope(message, code,
                              type_ or _type_for_status(status), param),
                    status=status)


def _first_validation_message(exc: ValidationError) -> tuple[str, str | None]:
    """把 DRF 字段错误压成一句可读文案，并给出首个出错字段作为 param。

    旧行为是原样吐出 `{"name": ["This field is required."]}`，前端
    `extractErrorMessage` 三个分支都不命中，用户看到的是
    "请求失败 (HTTP 400)"——校验信息完全丢失。
    """
    detail = exc.detail
    if isinstance(detail, str):
        return detail, None
    if isinstance(detail, list):
        parts = [str(p) for p in detail if p]
        return "; ".join(parts) or "Invalid request", None
    if isinstance(detail, dict):
        bits: list[str] = []
        first_param: str | None = None
        for field, msgs in detail.items():
            if first_param is None:
                first_param = str(field)
            if isinstance(msgs, (list, tuple)):
                text = "; ".join(str(m) for m in msgs if m)
            else:
                text = str(msgs)
            bits.append(f"{field}: {text}" if text else str(field))
        return "; ".join(bits) or "Invalid request", first_param
    return str(detail), None


def admin_exception_handler(exc, context):
    """DRF EXCEPTION_HANDLER：框架错误也走统一信封。

    返回 None 表示"不是 APIException"，DRF 会照常向上抛出交给 Django
    的 500 处理——本处理器绝不吞掉未预期异常。
    """
    if not isinstance(exc, APIException):
        return None

    code_map: dict[type, tuple[str, str]] = {
        ValidationError: ("invalid_request", "invalid_request_error"),
        NotAuthenticated: ("not_authenticated", "authentication_error"),
        AuthenticationFailed: ("authentication_failed", "authentication_error"),
        PermissionDenied: ("permission_denied", "permission_error"),
        NotFound: ("not_found", "not_found_error"),
        MethodNotAllowed: ("method_not_allowed", "invalid_request_error"),
        Throttled: ("rate_limited", "rate_limit_error"),
    }
    code, type_ = code_map.get(type(exc), ("api_error", "api_error"))

    param: str | None = None
    if isinstance(exc, ValidationError):
        message, param = _first_validation_message(exc)
    else:
        detail = getattr(exc, "detail", None)
        message = str(detail) if detail else str(exc)

    response = Response(_envelope(message, code, type_, param),
                        status=exc.status_code)
    if isinstance(exc, Throttled) and getattr(exc, "wait", None):
        response["Retry-After"] = str(int(exc.wait))
    # 兼容期：仍带一份 detail，旧客户端（含已部署的旧版前端）不至于读到空消息。
    # 新代码一律读 error.message；本字段计划在下个大版本移除。
    response.data["detail"] = message
    return response
