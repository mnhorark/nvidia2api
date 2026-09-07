import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (stdlib only).

    Existing environment variables take precedence, so .env only supplies
    values that were not already set by the shell / launcher.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_env_file(PROJECT_ROOT / ".env")


def _resolve_path(path) -> Path:
    """Resolve a possibly-relative path against the project root.

    Keeps .env values like DATA_DIR=./data stable no matter where the
    server process is launched from.
    """
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


# The race engine performs short, serialized SQLite writes from an asyncio
# (single-thread) event loop. DB calls are brief and safe here.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
DATA_DIR = _resolve_path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
# 敏感字段加密专用密钥（crypto._fernet 优先取它，其次回落到 SECRET_KEY 派生）
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# 测试模式：禁用一切"秒级 TTL 缓存"类优化，避免用例间串缓存（同指纹+同参数
# 在 3s TTL 内会命中前一用例的缓存）。生产不受影响。
TESTING = ("pytest" in os.sys.modules) or ("PYTEST_CURRENT_TEST" in os.environ)
# 生产环境应显式声明可服务的域名（逗号分隔），例如
#   ALLOWED_HOSTS=nvidia2api.example.com,127.0.0.1
# 保留 "*" 兜底是为了不打断既有自建部署（大量实例用 IP 直连、无法预知域名），
# 但会在启动时打印警告：Host 头校验失效是缓存投毒一类攻击的入口。
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "*").split(",")
                 if h.strip()]
_USING_WILDCARD_HOSTS = any(h == "*" for h in ALLOWED_HOSTS)

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "corsheaders",
    "rest_framework",
    "apps.core",
]

MIDDLEWARE = [
    # GZip 放最前：大 JSON 列表（keys 125KB / proxies 178KB）压缩到 ~10x 小，
    # 慢链路与局域网部署下页面加载差异显著。Django 自带实现已处理
    # Accept-Encoding 协商与 SSE StreamingHttpResponse 的正确跳过。
    "django.middleware.gzip.GZipMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

# Next.js 静态导出产物目录（Dockerfile 复制到 /app/static/frontend）。
# 由 api/frontend_views 负责托管；目录不存在时前端路由返回明确提示，不影响 API。
# 本地裸起 uvicorn 时（不带 FRONTEND_DIR 环境变量）自动探测仓库源码树的
# frontend/out——否则每次手动重启都得记得带环境变量，忘了就是"前端未找到"
# （Docker 内仓库树不存在，探测自然回落，不影响容器部署）。
def _default_frontend_dir() -> Path:
    repo_out = PROJECT_ROOT / "frontend" / "out"
    if (repo_out / "index.html").is_file():
        return repo_out
    return BASE_DIR / "static" / "frontend"


FRONTEND_DIR = _resolve_path(os.environ.get("FRONTEND_DIR") or _default_frontend_dir())
# DATABASE_PATH 留空时回落到 $DATA_DIR/db.sqlite3（.env 里写空的场景按未设置处理）
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_resolve_path(os.environ.get("DATABASE_PATH") or DATA_DIR / "db.sqlite3")),
        # Serialize writers & allow lock waits: protects per-key RPM counters under concurrency.
        "OPTIONS": {"timeout": 30},
        # 连接保持 60s：默认 0 会让每请求重建连接并重跑 WAL/busy_timeout 等
        # pragma，高频小查询路径（Dashboard/列表轮询）上省掉一大块固定开销。
        "CONN_MAX_AGE": 60,
        "TEST": {"NAME": str(DATA_DIR / "test_db.sqlite3")},
    }
}

# --- SQLite 并发优化 ---
# 竞速引擎在热路径上频繁写 Key/代理统计（每次请求 × 每条线路各写一次）。
# 默认 rollback journal 模式下，写事务会阻塞其他读写，高并发下抛
# "database is locked"（即使 timeout=30 也可能在事务升级锁时直接失败）。
# 改用 WAL 模式：读写可并发（多读者 + 单写者）、写事务更短，显著降低锁冲突。
from django.db.backends.signals import connection_created


def _setup_sqlite(connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA busy_timeout=30000;")
    cursor.execute("PRAGMA foreign_keys=ON;")


connection_created.connect(_setup_sqlite, dispatch_uid="nvidia2api_sqlite_pragmas")
TEMPLATES = []
USE_TZ = True
LANGUAGE_CODE = "en-us"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # 管理端错误统一走 {"error": {message, type, param, code}} 信封
    # （见 api/errors.py）。DRF 自身抛的校验/405/404 也经此转换，
    # 否则同一个接口会混出 detail 与 error 两种形态，客户端无法稳定读消息。
    "EXCEPTION_HANDLER": "api.errors.admin_exception_handler",
}

# all-in-one 镜像下前端由 Django 同源托管，本不需要 CORS。仅当外部站点需要
# 直接调用 API 时才放行，且生产环境必须显式列出来源（逗号分隔），例如
#   CORS_ALLOWED_ORIGINS=https://console.example.com
# 置为 "*" 会允许任意站点带着浏览器的凭据环境发起跨域调用。
_CORS_ORIGINS = [o.strip() for o in
                 os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
# 开发态（前端 dev server 在 :3000 独立端口）必须放行；生产态 all-in-one 镜像
# 由 Django 同源托管前端，不需要跨域，默认收紧为"仅显式列出的来源"。
CORS_ALLOW_ALL_ORIGINS = os.environ.get(
    "CORS_ALLOW_ALL_ORIGINS", "true" if DEBUG else "false").lower() == "true"
CORS_ALLOWED_ORIGINS = _CORS_ORIGINS
# 前端统一携带 X-Channel 头；django-cors-headers 默认列表不含它，需显式放行
CORS_ALLOW_HEADERS = [
    "accept", "accept-encoding", "authorization", "content-type", "dnt",
    "origin", "user-agent", "x-csrftoken", "x-requested-with", "x-channel",
]

# --- nvidia2api settings ---
NVIDIA_BASE_URL = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
DEFAULT_NVIDIA_RPM = int(os.environ.get("DEFAULT_NVIDIA_RPM", "0"))
PROXY_TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "10"))
UPSTREAM_CONNECT_TIMEOUT = float(os.environ.get("UPSTREAM_CONNECT_TIMEOUT", "10"))
# 非流式请求的上游读超时。默认 **0 = 不限制**（2026-09）：慢模型（kimi-k3 写大文件）
# 单次生成可以跑数分钟以上，任何固定值都会误杀。
# ⚠ 已知代价（2026-09-07 复核后**更正故障域范围**）：本项目的**每一个**入口都是同步
# 视图——数据面 chat/responses/anthropic/count_tokens/models、管理面全部
# /api/admin/*、以及 /healthz 与 /metrics。Django 对非协程视图一律
# `sync_to_async(view, thread_sensitive=True)`，而 asgiref 的 thread-sensitive
# 执行器是 `ThreadPoolExecutor(max_workers=1)`（sync.py:402/:481，**无法配置**；
# 这里原先写的 ASGI_THREADS=64 是个不存在的环境变量，已随 config/asgi.py 删除）。
# 所以不设上限时，一条挂死的非流式请求冻结的不是"admin 面 + 健康检查"，而是
# **整个网关**：后续每一个请求（包括新起的流式请求——它的入口视图同样是同步的）
# 都排在同一条线程上，/healthz 也答不出来。
# 根治是把数据面视图改 async + 消灭请求路径里的 asyncio.run
# （docs/architecture-review-2026-09b.md 第六节第 11 项；它必须先补第 12 项的测试，
# 否则会把"冻结一条线程"换成"每个非流式请求泄漏 max_routes_per_request 个 RPM 槽位"）。
# 在那之前，若观察到后台整体卡死，把本值调回一个大于最慢正常生成的秒数即可。
UPSTREAM_READ_TIMEOUT = float(os.environ.get("UPSTREAM_READ_TIMEOUT", "0"))
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "500"))
# 单次请求的最大并发线路数。默认从 80 降到 8（2026-09）：
# 竞速是为了压尾延迟，但每条线路都是一个**真实的上游并发流**，而 agent 客户端
# 的工具调用是**原子具现**的——6 个子代理在同一毫秒派发，40 条/请求就是 240 条
# 同时流砸向同一个上游，直接把上游打到掐流（实测 kimi-k3 在 300s 被上游关闭）。
# 也就是说"竞速冗余"在这个倍数下是自伤。8 条已足够拿到"谁先出流"的收益。
# 注意：SystemSetting 里的按渠道覆盖值优先于本默认（当前库 nvidia=40 / zen=60 /
# openrouter=100 / kilo 与 bai 见后台设置页），要生效需在控制台调小。
MAX_ROUTES_PER_REQUEST = int(os.environ.get("MAX_ROUTES_PER_REQUEST", "8"))
# 全平台同时打开的上游 HTTP 连接数上限（跨请求的全局 socket 阀门）。
# 默认 0 = 不限制（对齐原始设计：并发只由 max_concurrent_requests ×
# max_routes_per_request 自然约束）。仅在受 fd 硬限制的环境（如 Windows +
# SelectorEventLoop，select.select 上限约 512 fd，"too many file descriptors
# in select()" 崩 worker）需要手动调小，让请求在余量不足时降级为更少线路。
# 后台设置 max_concurrent_upstream 可覆盖；设 0 表示不限制。
MAX_CONCURRENT_UPSTREAM = int(os.environ.get("MAX_CONCURRENT_UPSTREAM", "0"))
REASONING_DECRYPT_KEY = os.environ.get("REASONING_DECRYPT_KEY", "")
# Django 层请求体上限，必须 ≥ 业务层 max_request_bytes（默认 32MB），
# 否则会先于业务校验抛 RequestDataTooBig 返回裸 400。
DATA_UPLOAD_MAX_MEMORY_SIZE = 40 * 1024 * 1024
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")
# 管理 Token 支持多值（逗号分隔）：旧 Token 仍有效时即可追加新 Token 完成轮换，
# 全部生效期内并存，确认客户端都切走后再从环境变量里移除旧值。
ADMIN_TOKENS = tuple(
    t.strip() for t in str(ADMIN_TOKEN).split(",") if t.strip()
) or ("dev-admin-token",)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
}

# --- 启动安全门禁 ---
# 出厂默认凭据（admin/admin123 + dev-admin-token）仅允许在 DEBUG 下使用；
# 生产环境（DEBUG=false）继续使用即直接拒绝启动——泄露后 `?reveal=1` 可明文
# 回读全部上游 Key，管理面等于完全失守。
import logging as _logging

# 已知公知凭据集合：仅含出厂默认值与 .env.example 中随仓库公开分发的占位值。
# 不扩展到 generic 弱口令词典（如 "password"/"admin"）——那样会误伤内网环境
# 的合法自定义凭据；门禁的目标是拦截"按仓库公开值部署"这一具体高危路径。
KNOWN_WEAK_ADMIN_PASSWORDS = {"admin123", "change-me"}
KNOWN_WEAK_ADMIN_TOKENS = {"dev-admin-token", "change-me"}
KNOWN_WEAK_SECRET_KEYS = {
    "dev-insecure-secret-change-me",
    "change-me-to-a-random-string",
}
USING_DEFAULT_CREDENTIALS = (
    ADMIN_PASSWORD in KNOWN_WEAK_ADMIN_PASSWORDS
    or any(t in KNOWN_WEAK_ADMIN_TOKENS for t in ADMIN_TOKENS)
)

if USING_DEFAULT_CREDENTIALS:
    message = (
        "【安全警告】正在使用默认管理凭据（ADMIN_PASSWORD=admin123 / "
        "ADMIN_TOKEN=dev-admin-token）。请勿在生产环境使用，"
        "请通过环境变量修改 ADMIN_PASSWORD 和 ADMIN_TOKEN。"
    )
    opted_out = os.environ.get("ALLOW_DEFAULT_CREDENTIALS", "false").lower() == "true"
    if not DEBUG and not opted_out:
        raise RuntimeError(
            "拒绝以默认管理凭据启动生产环境（DEBUG=false）。" + message +
            " 如确需在封闭内网临时验证，请设置环境变量 "
            "ALLOW_DEFAULT_CREDENTIALS=true 显式确认风险。"
        )
    _logging.getLogger("django").warning(message)

if not os.environ.get("ENCRYPTION_KEY") and SECRET_KEY in KNOWN_WEAK_SECRET_KEYS:
    _logging.getLogger("django").warning(
        "【安全警告】未配置 ENCRYPTION_KEY 且 SECRET_KEY 为默认值，"
        "入库的 NVIDIA Key / 代理密码加密使用可被推导的密钥。"
        "生产环境请设置环境变量 ENCRYPTION_KEY；且 SECRET_KEY 变更后旧密文将无法解密。"
    )

if not DEBUG:
    if _USING_WILDCARD_HOSTS:
        _logging.getLogger("django").warning(
            "【安全提示】ALLOWED_HOSTS 为通配 '*'，Host 头校验失效。"
            "生产环境建议显式设置 ALLOWED_HOSTS 为实际域名或 IP（逗号分隔）。"
        )
    if CORS_ALLOW_ALL_ORIGINS:
        _logging.getLogger("django").warning(
            "【安全提示】CORS_ALLOW_ALL_ORIGINS=true，任意站点可跨域调用本服务 API。"
            "all-in-one 部署下前端由本服务同源托管，建议设为 false，"
            "确需跨域时改用 CORS_ALLOWED_ORIGINS 显式列出来源。"
        )
