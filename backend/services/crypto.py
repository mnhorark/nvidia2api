"""敏感字段加密（AES-GCM，Fernet 封装）。

用于 `ChannelKey.api_key` 与 `Proxy.password`：写入时加密、读取时在
实际使用点解密。存储格式带 `enc:v1:` 前缀；旧库里的明文在解密时自动回落
（向后兼容，无需迁移即可平滑上线）。

密钥来源优先级：环境变量 `ENCRYPTION_KEY`（推荐显式配置）> Django SECRET_KEY 派生。
"""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from django.conf import settings

logger = logging.getLogger("nvidia2api.crypto")

_PREFIX = "enc:v1:"
_fernet_singleton: Fernet | None = None
_fernet_key_fingerprint: str | None = None


def _fernet() -> Fernet:
    # Fernet 构造含 SHA-256 + 对象分配；available_keys / 竞速每条线路都会解密。
    # 密钥在进程生命周期内通常不变，缓存单例即可。测试覆盖 ENCRYPTION_KEY /
    # SECRET_KEY 时指纹变化，自动重建，避免跨测试污染。
    global _fernet_singleton, _fernet_key_fingerprint
    raw = str(getattr(settings, "ENCRYPTION_KEY", None) or settings.SECRET_KEY or "nvidia2api")
    if _fernet_singleton is None or _fernet_key_fingerprint != raw:
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
        _fernet_singleton = Fernet(key)
        _fernet_key_fingerprint = raw
    return _fernet_singleton


def reset_fernet_cache() -> None:
    """测试用：清空 Fernet 单例，避免跨测试密钥污染。"""
    global _fernet_singleton, _fernet_key_fingerprint
    _fernet_singleton = None
    _fernet_key_fingerprint = None


def encrypt_secret(plain: str) -> str:
    """加密敏感字符串；空值 / 已加密值原样返回（幂等，save() 重复调用安全）。"""
    if not plain or plain.startswith(_PREFIX):
        return plain
    token = _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def mask_secret(plain: str) -> str:
    """密文无关的展示掩码（供保存时离线计算提示位，避免列表页逐行解密）。

    **脱敏口径的单一事实来源**：前 10 + 8 个 `*` + 后 4；短串退化为前 4+****。
    `key_service.mask_key` 委托到这里，`ChannelKey.api_key_hint` 在写入时用它
    预计算——列表页与接口即时脱敏必须同口径，用户会拿脱敏串比对是哪把 Key。
    """
    if not plain:
        return ""
    if len(plain) <= 10:
        return plain[:4] + "****"
    return plain[:10] + "*" * 8 + plain[-4:]


def decrypt_secret(stored: str) -> str:
    """解密敏感字符串；无前缀视为历史明文原样返回，解密失败返回空串。

    解密失败（典型场景：换过 ENCRYPTION_KEY / SECRET_KEY，或数据被手工改动）
    时**不能**回落原值——那会把密文当明文 Key 发往上游，等于把内部密文泄漏
    给第三方接口，且失败原因被掩盖成"上游 401"。返回空串让上层立即以
    "缺少凭据"失败，同时留下可定位的日志。
    """
    if not stored or not stored.startswith(_PREFIX):
        return stored
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        logger.error("解密敏感字段失败（ENCRYPTION_KEY 或 SECRET_KEY 与加密时不一致？）: %s",
                     exc)
        return ""
