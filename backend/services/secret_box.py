"""密钥安全存储（批次 24 · C6）

数据库里的 `model_configs.api_key` 是管理后台填的第三方密钥。历史上它明文落库，
一次 mysqldump 或一份误传的备份就足以泄光。这里提供一个极小的对称加密封装：

- 写：`encrypt_secret(plain)` → `"enc:v1:<fernet-token>"`
- 读：`decrypt_secret(stored)` → 明文；对**没有前缀的历史明文原样返回**，
  所以老数据不需要停机迁移，属于「写时加密、读时兼容」的平滑升级。
- 展示：`mask_secret()` 一律返回固定掩码，**不再回显密钥的任何字节**。

密钥来自 `SECRET_ENCRYPTION_KEY`。没配置时降级为明文并告警一次 ——
安全加固不应该让一个已经跑起来的系统直接起不来，但必须让人看见它没生效。
"""
from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from backend.config import settings

#: 密文前缀。带版本号，将来换算法可以按前缀分流解密。
SECRET_PREFIX = "enc:v1:"

#: 对外展示的统一掩码 —— 不保留任何原文片段（旧实现回显前 8 位，等于泄了一半密钥）
SECRET_MASK = "********"

#: 降级告警只打一次，避免每次写配置都刷屏
_warned_plaintext = False


def is_encrypted(value: Optional[str]) -> bool:
    """值是否为本模块产出的密文。"""
    return bool(value) and value.startswith(SECRET_PREFIX)


def is_configured() -> bool:
    """是否配置了加密密钥。未配置时所有写入都会是明文。"""
    return bool(getattr(settings, "SECRET_ENCRYPTION_KEY", "") or "")


def generate_key() -> str:
    """生成一个可直接写进 .env 的密钥。"""
    return Fernet.generate_key().decode()


def _derive_fernet_key(raw: str) -> bytes:
    """把任意口令规范成合法的 Fernet key。

    允许用户在 .env 里填随便一串口令，而不是被 `Fernet.generate_key()` 的格式绑住；
    同一个口令永远派生出同一把钥匙，所以重启不会让历史密文解不开。
    """
    raw = (raw or "").strip()
    try:
        # 已经是合法 key（44 字符 urlsafe base64）就直接用，避免多余派生
        if len(raw) == 44:
            base64.urlsafe_b64decode(raw.encode())
            return raw.encode()
    except Exception:
        pass
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Optional[Fernet]:
    """拿 Fernet 实例；未配置密钥返回 None。"""
    raw = getattr(settings, "SECRET_ENCRYPTION_KEY", "") or ""
    if not raw.strip():
        return None
    try:
        return Fernet(_derive_fernet_key(raw))
    except Exception as exc:  # 密钥本身坏了也不能让写入路径炸掉
        logger.error(f"SECRET_ENCRYPTION_KEY 无法使用，密钥将以明文保存: {exc}")
        return None


def encrypt_secret(plain: Optional[str]) -> Optional[str]:
    """明文 → 密文。

    - 空值返回 None（保持「未配置」语义，不要存空串密文）
    - 已是密文则原样返回，重复保存不会二次加密
    - 未配置密钥时返回明文，并告警一次
    """
    global _warned_plaintext
    if plain is None:
        return None
    text = str(plain)
    if not text:
        return None
    if is_encrypted(text):
        return text

    fernet = _fernet()
    if fernet is None:
        if not _warned_plaintext:
            _warned_plaintext = True
            logger.warning(
                "SECRET_ENCRYPTION_KEY 未配置：API Key 将以明文存入数据库。"
                "请用 `python -m backend.services.secret_box` 生成密钥并写入 .env"
            )
        return text

    try:
        token = fernet.encrypt(text.encode("utf-8")).decode("ascii")
    except Exception as exc:
        logger.error(f"密钥加密失败，改为明文保存: {exc}")
        return text
    return SECRET_PREFIX + token


def decrypt_secret(value: Optional[str]) -> str:
    """密文 → 明文；历史明文原样返回；解不开时返回空串。

    解不开返回空串而不是抛异常：这个值最终要喂给 LLM 客户端，
    抛异常会把「密钥坏了」升级成「整个功能 500」，返回空串至少还能走到
    「未配置 API Key」的原有提示分支。
    """
    if not value:
        return ""
    text = str(value)
    if not is_encrypted(text):
        return text  # 历史明文，平滑兼容

    fernet = _fernet()
    if fernet is None:
        logger.error("密文存在但 SECRET_ENCRYPTION_KEY 未配置，无法解密")
        return ""

    try:
        return fernet.decrypt(text[len(SECRET_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("密钥解密失败：SECRET_ENCRYPTION_KEY 可能已更换")
        return ""
    except Exception as exc:
        logger.error(f"密钥解密异常: {exc}")
        return ""


def mask_secret(value: Optional[str]) -> Optional[str]:
    """返回固定掩码；没有值返回 None。

    注意：**不看原文**。旧实现返回 `value[:8] + "***"`，把密钥的前 8 位交了出去，
    而很多厂商的 Key 前缀是固定的、熵很低，前 8 位并不能起到遮蔽作用。
    """
    return SECRET_MASK if value else None


def describe_storage() -> str:
    """给启动日志用的一行状态描述。"""
    return "已启用（Fernet/enc:v1）" if is_configured() else "未启用（明文存储）"


if __name__ == "__main__":  # pragma: no cover - 手工生成密钥用
    print(generate_key())
