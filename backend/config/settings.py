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
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

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
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_resolve_path(os.environ.get("DATABASE_PATH", DATA_DIR / "db.sqlite3"))),
        # Serialize writers & allow lock waits: protects per-key RPM counters under concurrency.
        "OPTIONS": {"timeout": 30},
        "TEST": {"NAME": str(DATA_DIR / "test_db.sqlite3")},
    }
}
TEMPLATES = []
USE_TZ = True
LANGUAGE_CODE = "en-us"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

CORS_ALLOW_ALL_ORIGINS = True
# 前端统一携带 X-Channel 头；django-cors-headers 默认列表不含它，需显式放行
CORS_ALLOW_HEADERS = [
    "accept", "accept-encoding", "authorization", "content-type", "dnt",
    "origin", "user-agent", "x-csrftoken", "x-requested-with", "x-channel",
]

# --- nvidia2api settings ---
NVIDIA_BASE_URL = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
DEFAULT_NVIDIA_RPM = int(os.environ.get("DEFAULT_NVIDIA_RPM", "40"))
PROXY_TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "10"))
UPSTREAM_CONNECT_TIMEOUT = float(os.environ.get("UPSTREAM_CONNECT_TIMEOUT", "10"))
UPSTREAM_READ_TIMEOUT = float(os.environ.get("UPSTREAM_READ_TIMEOUT", "120"))
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "100"))
MAX_ROUTES_PER_REQUEST = int(os.environ.get("MAX_ROUTES_PER_REQUEST", "50"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
}

# 开发默认值告警：不阻止启动，仅提示尽快修改。
import logging as _logging

if ADMIN_PASSWORD == "admin123" or ADMIN_TOKEN == "dev-admin-token":
    _logging.getLogger("django").warning(
        "【安全警告】正在使用默认管理凭据（ADMIN_PASSWORD=admin123 / "
        "ADMIN_TOKEN=dev-admin-token）。请勿在生产环境使用，"
        "请通过环境变量修改 ADMIN_PASSWORD 和 ADMIN_TOKEN。"
    )

if not os.environ.get("ENCRYPTION_KEY") and SECRET_KEY == "dev-insecure-secret-change-me":
    _logging.getLogger("django").warning(
        "【安全警告】未配置 ENCRYPTION_KEY 且 SECRET_KEY 为默认值，"
        "入库的 NVIDIA Key / 代理密码加密使用可被推导的密钥。"
        "生产环境请设置环境变量 ENCRYPTION_KEY；且 SECRET_KEY 变更后旧密文将无法解密。"
    )
