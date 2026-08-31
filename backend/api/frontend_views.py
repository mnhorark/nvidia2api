"""前端静态托管（all-in-one 镜像）。

Next.js 以 `output: 'export'` 产出纯静态 `out/` 目录，Dockerfile 把它复制到
`/app/static/frontend`。本模块负责：

- `/_next/*`、`/*.ico`、`/*.png` 等带扩展名的静态资源：按路径直接取文件；
- 其余无扩展名路径（/dashboard、/channels…）：回退到对应目录的 index.html，
  最兜底返回根 index.html，让前端路由接管；
- 前端产物不存在（纯 API 部署 / 本地开发）：返回明确的安装提示，
  而不是静默 404 或抛异常。

安全约束：
- 只在本模块内做路径归一化与目录穿越校验（`..`、绝对路径一律拒绝）；
- 只接受位于 FRONTEND_DIR 之内的文件。
"""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from urllib.parse import unquote

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger("nvidia2api.frontend")

# 拥有真实扩展名的请求视为静态资源；其余交给 index.html 回退
_ASSET_SUFFIXES = (
    ".js", ".mjs", ".css", ".map", ".json", ".ico", ".png", ".jpg", ".jpeg",
    ".svg", ".webp", ".gif", ".woff", ".woff2", ".ttf", ".eot", ".txt",
)

_MISSING_HINT = (
    "前端静态产物未找到。all-in-one 镜像应由 Dockerfile 构建时生成；"
    "本地开发请直接运行 `npm run dev`（前端 :3000 + 后端 :8000）。"
)


def frontend_dir() -> Path:
    """前端导出目录（settings.FRONTEND_DIR，默认 <BASE_DIR>/static/frontend）。"""
    return Path(getattr(settings, "FRONTEND_DIR",
                        Path(settings.BASE_DIR) / "static" / "frontend"))


def frontend_index_exists() -> bool:
    return (frontend_dir() / "index.html").is_file()


#: `_resolve` 的返回值：路径企图逃出导出目录
TRAVERSAL = ":traversal:"


def _is_traversal(path: str) -> bool:
    """是否企图逃出导出目录（绝对路径 / `..` / URL 编码过的 `..`）。

    同时检查原文与 unquote 后的形式：正常 WSGI 服务器会先解码 PATH_INFO，
    但若有前置代理原样转发（`..%2f`），只查原文就会漏判。
    """
    for cand in (path, unquote(path)):
        if cand.startswith(("/", "\\")):
            return True
        parts = Path(cand).parts
        if ".." in parts or ".." in [p.strip("/\\") for p in parts]:
            return True
    return False


def _resolve(path: str) -> Path | None:
    """把 URL 路径解析成导出目录内的真实文件。

    返回 `TRAVERSAL` 表示路径企图越界（调用方必须 404，不能回落到 SPA
    index.html——否则探测者会拿到 200，掩盖攻击意图）；
    返回 None 表示"文件确实不存在"。
    """
    root = frontend_dir()
    raw = path or ""
    # 必须在 lstrip("/") 之前判定：否则 "/etc/passwd" 会被当成
    # 导出目录内的相对路径，绝对路径检查形同虚设。
    if _is_traversal(raw):
        return TRAVERSAL
    rel = raw.lstrip("/").strip()
    if not rel:
        rel = "index.html"
    candidate = (root / rel)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return TRAVERSAL
    if candidate.is_file():
        return candidate
    # 目录 -> index.html（trailingSlash: true 时 Next 导出为 <route>/index.html）
    index = candidate / "index.html"
    if index.is_file():
        return index
    return None


def _guess_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


@csrf_exempt
def serve_frontend(request, path: str = ""):
    """静态资源精确匹配 + 前端路由 index.html 回退。"""
    resolved = _resolve(path)
    if resolved is TRAVERSAL:
        raise Http404("invalid path")
    if resolved is None:
        # 无扩展名 = 前端路由（刷新/直达子页面都走这里）
        if path and not path.endswith(_ASSET_SUFFIXES):
            fallback = frontend_dir() / "index.html"
            if fallback.is_file():
                return FileResponse(fallback.open("rb"),
                                    content_type="text/html; charset=utf-8")
        raise Http404("frontend asset not found")
    # 静态资源可长缓存（Next 产物文件名带 hash），HTML 不缓存
    is_html = resolved.name.endswith(".html")
    response = FileResponse(resolved.open("rb"), content_type=_guess_type(resolved))
    if is_html:
        response["Cache-Control"] = "no-cache"
    else:
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@csrf_exempt
def frontend_root(request):
    """`/`：有前端产物则给首页，否则给出可操作的提示。"""
    index = frontend_dir() / "index.html"
    if index.is_file():
        return FileResponse(index.open("rb"), content_type="text/html; charset=utf-8")
    return HttpResponse(
        "nvidia2api API is running.\n" + _MISSING_HINT + "\n",
        content_type="text/plain; charset=utf-8", status=200,
    )
