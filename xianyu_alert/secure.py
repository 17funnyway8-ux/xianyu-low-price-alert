"""Cookie 安全模块：Fernet 对称加密（跨平台）。

设计决策（对齐 macOS 适配设计文档 §3.2）：
    - 采用 **Fernet（cryptography 库）** 替代 Windows DPAPI：
      跨平台可加解密（Windows / macOS / Linux 一致），不再绑定「当前用户 + 本机」；
    - 密文前缀 `fernet1:` 标记新密文；`dpapi1:` 为**遗留前缀**（仅用于识别老密文，
      识别后按「无法解密 → 请重新登录」处理，**不再保留 DPAPI 代码**）；
    - 密钥文件 `secret.key` 放在 `paths.data_dir()`，首次加密时自动生成并落盘；
      POSIX 下权限收紧为 0600；模块级缓存（进程内只读一次）；
    - 对外 4 函数签名 `encrypt_text / decrypt_text / is_encrypted / mask_cookie`
      保持不变 → config.py / cookie.py / gui.py 调用点**零改动**；
    - `cryptography` 缺失时降级为明文（打 warning，不抛异常），保证 GUI 可用。

本模块对包内其它模块采用**函数内延迟导入**（仅 `paths.data_dir()` 需要），
避免任何循环依赖；模块顶层只依赖标准库 + cryptography。
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: 遗留 DPAPI 前缀（v1.x Windows 老密文；仅识别，不可解密）
PREFIX = "dpapi1:"
#: 新密文前缀（Fernet，跨平台）
FERNET_PREFIX = "fernet1:"
#: 密钥文件默认文件名（位于 paths.data_dir()）
KEY_FILE_NAME = "secret.key"
#: POSIX 密钥文件权限（0600：仅属主可读写）
_KEY_FILE_MODE = 0o600

#: 用户显式指定的密钥文件路径（None → 默认 data_dir()/secret.key；测试/高级场景用）
_KEY_FILE: Optional[str] = None
#: 进程内密钥缓存（首次加载后不再读盘；set_key_file 会重置）
_key_cache: Optional[bytes] = None

try:  # pragma: no cover - 分支由运行环境决定
    from cryptography.fernet import Fernet, InvalidToken

    _CRYPTO_UNAVAILABLE = ""
except Exception as exc:  # noqa: BLE001 - 依赖缺失给出可读降级
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]
    _CRYPTO_UNAVAILABLE = str(exc)


# ---------------------------------------------------------------------- #
# 密钥管理
# ---------------------------------------------------------------------- #
def set_key_file(path: Optional[str]) -> None:
    """显式指定密钥文件路径并重置缓存；`None` 恢复默认（data_dir()/secret.key）。

    供测试隔离与高级用户使用；重复调用会重新读取磁盘密钥。

    Args:
        path: 密钥文件绝对路径；None 表示恢复默认位置。
    """
    global _KEY_FILE, _key_cache
    _KEY_FILE = path
    _key_cache = None


def _default_key_path() -> str:
    """默认密钥文件路径：data_dir()/secret.key（延迟导入 paths，避免循环依赖）。"""
    from . import paths  # 延迟导入：paths 不依赖 secure，安全

    return os.path.join(paths.data_dir(), KEY_FILE_NAME)


def _write_key_file(key_path: str, key: bytes) -> None:
    """写入密钥文件；POSIX 下收紧权限为 0600（防止其它用户读取）。"""
    parent = os.path.dirname(os.path.abspath(key_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(key_path, "wb") as fp:
        fp.write(key)
    if os.name == "posix":
        try:
            os.chmod(key_path, _KEY_FILE_MODE)
        except OSError:  # pragma: no cover - 权限收紧失败不影响主流程
            logger.debug("设置密钥文件权限失败：%s", key_path)


def _load_or_create_key() -> Optional[bytes]:
    """加载（或首次生成）Fernet 密钥并缓存。

    Returns:
        32 字节 URL-safe base64 密钥；cryptography 缺失 / 密钥损坏返回 None。
    """
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    if Fernet is None:
        logger.warning("cryptography 未安装，Cookie 加密不可用（将降级为明文）：%s", _CRYPTO_UNAVAILABLE)
        return None
    key_path = _KEY_FILE if _KEY_FILE else _default_key_path()
    try:
        if os.path.isfile(key_path):
            with open(key_path, "rb") as fp:
                key = fp.read().strip()
        else:
            key = Fernet.generate_key()
            _write_key_file(key_path, key)
        # 校验密钥格式（损坏 / 非 base64 时抛异常 → 返回 None）
        Fernet(key)
        _key_cache = key
        return key
    except Exception as exc:  # noqa: BLE001 - 密钥加载失败必须安全返回
        logger.warning("Cookie 密钥加载失败（%s），Cookie 将无法加解密", exc)
        return None


# ---------------------------------------------------------------------- #
# 公开 API
# ---------------------------------------------------------------------- #
def is_encrypted(cookie: str) -> bool:
    """判断 Cookie 是否为密文（`fernet1:` 或遗留 `dpapi1:` 前缀）。

    Args:
        cookie: 待判断字符串。

    Returns:
        True 表示是带前缀的密文。
    """
    text = str(cookie or "")
    return text.startswith(PREFIX) or text.startswith(FERNET_PREFIX)


def encrypt_text(plain: str) -> str:
    """加密明文（Fernet），返回带 `fernet1:` 前缀的 base64 密文。

    - 空串返回空串；
    - cryptography 缺失 / 密钥不可用 / 加密失败时**降级为明文**并打 warning
      （绝不抛异常，保证 GUI 保存路径不中断）。

    Args:
        plain: 明文 Cookie 字符串。

    Returns:
        `fernet1:base64` 密文，或降级后的原文。
    """
    plain = str(plain or "")
    if not plain:
        return ""
    key = _load_or_create_key()
    if key is None:
        logger.warning("Fernet 密钥不可用，Cookie 将以明文保存（降级模式）")
        return plain
    try:
        token = Fernet(key).encrypt(plain.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 降级不抛
        logger.warning("Cookie 加密失败，降级为明文保存：%s", exc)
        return plain
    return FERNET_PREFIX + token.decode("ascii")


def decrypt_text(cipher: str) -> str:
    """解密密文，返回明文。

    - 空串 / 无前缀输入原样返回（兼容存量明文）；
    - `dpapi1:` 遗留密文 → **无法解密** → warning「请重新登录」+ 返回空串；
    - `fernet1:` 密文解密失败（密钥丢失 / 损坏 / 密文无效）→ warning + 空串。

    Args:
        cipher: 密文或明文。

    Returns:
        解密后的明文；失败返回空串。
    """
    cipher = str(cipher or "")
    if not cipher:
        return ""
    if cipher.startswith(PREFIX):
        # 遗留 Windows DPAPI 密文：不再保留 DPAPI 代码，跨平台无法解开
        logger.warning("Cookie 无法解密（dpapi1: 为旧版 Windows 密文，无法跨平台解密），请重新登录获取")
        return ""
    if not cipher.startswith(FERNET_PREFIX):
        # 无前缀 → 存量明文，原样返回
        return cipher
    key = _load_or_create_key()
    if key is None:
        logger.warning("Cookie 无法解密（密钥不可用），请重新登录获取")
        return ""
    try:
        token = cipher[len(FERNET_PREFIX):].encode("ascii")
        decrypted = Fernet(key).decrypt(token)
    except (InvalidToken, ValueError, TypeError) as exc:
        logger.warning("Cookie 无法解密（密钥丢失/损坏/密文无效），请重新登录获取：%s", exc)
        return ""
    except Exception as exc:  # noqa: BLE001 - 兜底：解密失败返回空串
        logger.warning("Cookie 无法解密，请重新登录获取：%s", exc)
        return ""
    return decrypted.decode("utf-8", errors="replace")


def mask_cookie(cookie: str) -> str:
    """脱敏显示 Cookie：只保留前 8 后 4 字符（中间省略）。

    Args:
        cookie: 原始 Cookie 字符串。

    Returns:
        脱敏后的展示文本；空输入返回空串。
    """
    raw = str(cookie or "").strip()
    if not raw:
        return ""
    if len(raw) <= 12:
        return raw[0] + "***" + raw[-1] if len(raw) > 2 else raw
    return raw[:8] + "..." + raw[-4:]
