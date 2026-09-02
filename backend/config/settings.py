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
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

# Next.js 静态导出产物目录（Dockerfile 复制到 /app/static/frontend）。
# 由 api/frontend_views 负责托管；目录不存在时前端路由返回明确提示，不影响 API。
FRONTEND_DIR = _resolve_path(os.environ.get("FRONTEND_DIR", BASE_DIR / "static" / "frontend"))
# DATABASE_PATH 留空时回落到 $DATA_DIR/db.sqlite3（.env 里写空的场景按未设置处理）
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_resolve_path(os.environ.get("DATABASE_PATH") or DATA_DIR / "db.sqlite3")),
        # Serialize writers & allow lock waits: protects per-key RPM counters under concurrency.
        "OPTIONS": {"timeout": 30},
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
UPSTREAM_READ_TIMEOUT = float(os.environ.get("UPSTREAM_READ_TIMEOUT", "120"))
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "500"))
MAX_ROUTES_PER_REQUEST = int(os.environ.get("MAX_ROUTES_PER_REQUEST", "80"))
# 全平台同时打开的上游 HTTP 连接数上限（跨请求的全局 socket 阀门）。
# 默认 0 = 不限制（对齐原始设计：并发只由 max_concurrent_requests ×
# max_routes_per_request 自然约束）。仅在受 fd 硬限制的环境（如 Windows +
# SelectorEventLoop，select.select 上限约 512 fd，"too many file descriptors
# in select()" 崩 worker）需要手动调小，让请求在余量不足时降级为更少线路。
# 后台设置 max_concurrent_upstream 可覆盖；设 0 表示不限制。
MAX_CONCURRENT_UPSTREAM = int(os.environ.get("MAX_CONCURRENT_UPSTREAM", "0"))
# 请求体大小上限（字节）。必须与 openai_views._parse_body 的 MAX_BODY_BYTES 对齐：
# Django 默认 DATA_UPLOAD_MAX_MEMORY_SIZE=2.5MB 会先于业务校验在 request.body 处
# 抛 RequestDataTooBig（返回裸 400 页面）；这里把它提到 8MB，业务层的 4MB 上限
# 仍然先触发并返回干净的 OpenAI 413，第 8MB 极端值由 _parse_body 兜底捕获。
REASONING_DECRYPT_KEY = os.environ.get("REASONING_DECRYPT_KEY", "")
DATA_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024
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

DEFAULT_ADMIN_PASSWORD = "admin123"
DEFAULT_ADMIN_TOKEN = "dev-admin-token"
DEFAULT_SECRET_KEY = "dev-insecure-secret-change-me"
USING_DEFAULT_CREDENTIALS = (
    ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD
    or any(t == DEFAULT_ADMIN_TOKEN for t in ADMIN_TOKENS)
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

if not os.environ.get("ENCRYPTION_KEY") and SECRET_KEY == DEFAULT_SECRET_KEY:
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
