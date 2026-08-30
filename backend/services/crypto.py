"""敏感字段加密（AES-GCM，Fernet 封装）。

用于 `ChannelKey.api_key` 与 `Proxy.password`：写入时加密、读取时在
实际使用点解密。存储格式带 `enc:v1:` 前缀；旧库里的明文在解密时自动回落
（向后兼容，无需迁移即可平滑上线）。

密钥来源优先级：环境变量 `ENCRYPTION_KEY`（推荐显式配置）> Django SECRET_KEY 派生。
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from django.conf import settings

_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    raw = getattr(settings, "ENCRYPTION_KEY", None) or settings.SECRET_KEY or "nvidia2api"
    key = base64.urlsafe_b64encode(hashlib.sha256(str(raw).encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    """加密敏感字符串；空值 / 已加密值原样返回（幂等，save() 重复调用安全）。"""
    if not plain or plain.startswith(_PREFIX):
        return plain
    token = _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt_secret(stored: str) -> str:
    """解密敏感字符串；旧明文或解密失败时回落原值。"""
    if not stored or not stored.startswith(_PREFIX):
        return stored
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return stored
