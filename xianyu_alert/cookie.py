"""Cookie 获取与保存工具。

为 `cli login` 子命令提供三种获取方式的底层能力：
    1. acquire_via_playwright : 半自动 —— 打开真实浏览器让用户登录，自动提取 Cookie；
    2. acquire_via_prompt     : 手动 —— 用户把浏览器复制的 Cookie 请求头粘贴进来；
    3. （脚本模式由 cli 直接调用 save_cookies_to_config，无需本模块额外函数。）

注意：Playwright 是**可选依赖**（见 requirements-cookie.txt），
本模块顶层不 import playwright，仅在 acquire_via_playwright 函数体内延迟导入。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import secure

logger = logging.getLogger(__name__)

#: 登录页地址（登录成功后站点会种下含 _m_h5_tk 的 Cookie）
LOGIN_URL = "https://www.goofish.com"
#: 判定「已登录/已拿到可用 Cookie」的关键 Cookie 名
REQUIRED_COOKIE_NAME = "_m_h5_tk"
#: 等待用户完成登录的默认超时（秒）
DEFAULT_LOGIN_TIMEOUT = 120.0
#: 轮询 Cookie 的间隔（秒）
POLL_INTERVAL = 1.0
#: 浏览器持久化 profile 目录名（位于 `paths.data_dir()`）。
#:
#: 放在数据目录里的意义：容器重建 / 迁移 NAS 时，浏览器登录态跟着数据卷一起走，
#: 不必每换一个环境就重新扫码。首次生成后即可支撑「免扫码刷新」。
PROFILE_DIR_NAME = "browser_profile"
#: 静默刷新（profile 已存在、无人值守）的默认等待上限（秒）。
#: 比人工登录的 120 秒短得多 —— 静默模式不需要等人，超时即代表登录态已失效。
DEFAULT_SILENT_TIMEOUT = 30.0

#: `_m_h5_tk` 的有效期（毫秒）。
#:
#: 2026-09-24 实测校准：向 mtop 详情接口发一次请求，服务端在「令牌过期」响应中
#: 下发的 Set-Cookie 为 `_m_h5_tk=...; Max-Age=5400` —— 即 **90 分钟**。
#: 此前代码假定 24 小时（相差 16 倍），差值区间内会把已失效的令牌误报为「正常」。
#:
#: 注意：令牌**每次请求都会滑动续期**（服务端主动下发新值），因此只要监控在跑
#: 就不会过期；本阈值只在「停跑超过 90 分钟后重启」时才具有实际意义。
TOKEN_TTL_MS = 90 * 60 * 1000
#: 临期预警阈值：剩余不足 TTL 的 1/6（= 15 分钟）视为「即将过期」。
#: 按 TTL 比例定义而非固定值，避免调整 TTL 后预警窗口覆盖过大比例的时间。
TOKEN_EXPIRING_SOON_MS = TOKEN_TTL_MS // 6
#: 匹配 `_m_h5_tk=...` 的值（形如 `xxx_1785488087003`）
_M_H5_TK_PATTERN = re.compile(r"(?:^|;\s*)_m_h5_tk=([^;]+)")


def token_ttl_text() -> str:
    """把 `TOKEN_TTL_MS` 渲染为中文时长文案。

    各处提示文案统一调用本函数，避免把「24 小时」这类数字写死 —— 否则
    每次校准 TTL 都要去改散落多处的字符串（且极易漏改）。
    """
    minutes = TOKEN_TTL_MS // 60000
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def token_expiring_text() -> str:
    """把 `TOKEN_EXPIRING_SOON_MS` 渲染为中文时长文案（同上，避免写死）。"""
    minutes = TOKEN_EXPIRING_SOON_MS // 60000
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


class PlaywrightUnavailable(Exception):
    """本机未安装 Playwright（或其浏览器内核）时抛出。"""


class LoginTimeout(Exception):
    """等待用户登录超时（未在限时内检测到关键 Cookie）时抛出。"""


# ---------------------------------------------------------------------- #
# 纯函数：Cookie 头拼装
# ---------------------------------------------------------------------- #
def build_cookie_header(cookies: List[Dict[str, Any]]) -> str:
    """把 Playwright 风格的 cookie 列表拼成 Cookie 请求头字符串。

    Args:
        cookies: 形如 [{"name": "_m_h5_tk", "value": "abc", ...}, ...] 的列表，
            多余字段（domain/path 等）会被忽略；缺少 name 的条目会被跳过。

    Returns:
        `name=value; name2=value2` 格式的字符串；空列表返回空串。
    """
    parts: List[str] = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "")).strip()
        if not name:
            continue
        value = str(cookie.get("value", ""))
        parts.append(f"{name}={value}")
    return "; ".join(parts)


# ---------------------------------------------------------------------- #
# Cookie 过期检测（_m_h5_tk 内嵌时间戳）
# ---------------------------------------------------------------------- #
def cookie_token_timestamp(cookie_str: str) -> Optional[int]:
    """解析 `_m_h5_tk` 内嵌的 13 位毫秒时间戳。

    闲鱼的 `_m_h5_tk` 值形如 `xxx_1785488087003`：下划线后半段是
    签发时间戳（毫秒）。本函数只认 13 位纯数字后缀。

    Args:
        cookie_str: Cookie 请求头字符串。

    Returns:
        毫秒时间戳整数；无法解析返回 None。
    """
    raw = str(cookie_str or "")
    match = _M_H5_TK_PATTERN.search(raw)
    if not match:
        return None
    value = match.group(1).strip()
    if "_" not in value:
        return None
    suffix = value.rsplit("_", 1)[1]
    if len(suffix) == 13 and suffix.isdigit():
        return int(suffix)
    return None


def cookie_has_token(cookie_str: str) -> bool:
    """键级判断 Cookie 是否包含真正的 `_m_h5_tk`。

    注意：不能用子串 `"_m_h5_tk" not in raw` 判断，否则
    `_m_h5_tk_enc` 会因包含该子串而误判为「有 token」。
    这里用 `(?:^|;\\s*)_m_h5_tk=` 正则精确匹配「键 =」形态。

    Args:
        cookie_str: Cookie 请求头字符串。

    Returns:
        True 表示存在真正的 `_m_h5_tk` 键。
    """
    return _M_H5_TK_PATTERN.search(str(cookie_str or "")) is not None


def cookie_expiry_status(cookie_str: str, now_ms: Optional[int] = None) -> str:
    """判定 Cookie 过期状态（纯函数，便于单测）。

    返回状态：
        missing   : 未配置
        no_token  : 有 Cookie 但缺 `_m_h5_tk`
        expired   : 已过期（签发时间 + TOKEN_TTL_MS < 当前）
        expiring  : 即将过期（剩余不足 TOKEN_EXPIRING_SOON_MS）
        ok        : 正常（剩余超过 TOKEN_EXPIRING_SOON_MS）
        unknown   : 含 `_m_h5_tk` 但无 13 位时间戳，无法判断

    Args:
        cookie_str: Cookie 请求头字符串。
        now_ms: 当前时间（毫秒）；None 取系统时间。

    Returns:
        上述状态之一。
    """
    raw = str(cookie_str or "").strip()
    if not raw:
        return "missing"
    if not cookie_has_token(raw):
        return "no_token"
    ts = cookie_token_timestamp(raw)
    if ts is None:
        return "unknown"
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    remain = ts + TOKEN_TTL_MS - now
    if remain <= 0:
        return "expired"
    if remain <= TOKEN_EXPIRING_SOON_MS:
        return "expiring"
    return "ok"


# ---------------------------------------------------------------------- #
# 有效性检测（v3.2 多 Cookie 管理）
# ---------------------------------------------------------------------- #
#: detect_cookie_health 的状态码取值
HEALTH_OK = "ok"
HEALTH_EXPIRED = "expired"
HEALTH_EXPIRING = "expiring"
HEALTH_NO_TOKEN = "no_token"
HEALTH_MISSING = "missing"
HEALTH_INVALID_ENCRYPT = "invalid_encrypt"
HEALTH_STATES: Tuple[str, ...] = (
    HEALTH_OK,
    HEALTH_EXPIRED,
    HEALTH_EXPIRING,
    HEALTH_NO_TOKEN,
    HEALTH_MISSING,
    HEALTH_INVALID_ENCRYPT,
)


def detect_cookie_health(cookie_str: str) -> tuple[str, str]:
    """检测单个 Cookie 的有效性（纯函数，供 Cookie 管理对话框 / 监控预检复用）。

    状态码（state）：
        ok               : 有效（含 `_m_h5_tk` 且未过期；无时间戳按有效处理）
        expired          : 令牌已过期（距上次成功续期超过 TOKEN_TTL_MS；下次请求会自动申请新令牌）
        expiring         : 令牌即将过期（同样会在下次请求自动续期）
        no_token         : 有内容但缺 `_m_h5_tk`
        missing          : 未配置（空串）
        invalid_encrypt  : `dpapi1:` 密文无法解密（换机/换用户）

    Args:
        cookie_str: Cookie 请求头字符串（可为 `dpapi1:` 密文）。

    Returns:
        (state, 中文原因文案)。
    """
    raw = str(cookie_str or "").strip()
    if not raw:
        return HEALTH_MISSING, "未配置 Cookie"
    if secure.is_encrypted(raw):
        decrypted = secure.decrypt_text(raw)
        if not decrypted:
            return HEALTH_INVALID_ENCRYPT, "密文无法解密（可能换机/换用户），请重新登录"
        raw = decrypted
    if not cookie_has_token(raw):
        return HEALTH_NO_TOKEN, f"缺少 {REQUIRED_COOKIE_NAME}，无法用于 mtop 签名"
    status = cookie_expiry_status(raw)
    if status == "expired":
        # 令牌过期 ≠ 登录态失效：mtop 令牌在每次请求时由服务端滑动续期，
        # 所以「过期」只意味着「距上次成功请求已超过有效期（默认 90 分钟）」。
        # 下一次请求通常就会带回新令牌；只有**持续**失败才说明登录态真的失效。
        # 文案必须把这个区别讲清楚，否则用户会去做一次根本没必要的重新登录。
        return (
            HEALTH_EXPIRED,
            f"登录令牌已过期（{token_ttl_text()}内未续期）—— 下次抓取会自动申请新令牌；"
            "若持续失败则说明登录态已失效，需重新登录",
        )
    if status == "expiring":
        return (
            HEALTH_EXPIRING,
            f"令牌即将过期（剩余不足 {token_expiring_text()}），下次抓取会自动续期",
        )
    if status == "unknown":
        return HEALTH_OK, "含 _m_h5_tk 但无时间戳，按有效处理（历史样本兼容）"
    return HEALTH_OK, "有效（含 _m_h5_tk 且未过期）"


def pool_enabled_cookies(pool: Any) -> List[str]:
    """返回 Cookie 池中**启用且非空**条目的明文 Cookie 列表（保序）。

    供轮换与预检复用。条目既可以是 `CookiePoolItem` dataclass，
    也可以是形如 {"name":..., "cookie":..., "enabled":...} 的字典，
    通过属性访问保持解耦，避免 cookie 模块反向依赖 config 模块。

    Args:
        pool: Cookie 池（列表）。

    Returns:
        启用条目的 Cookie 字符串列表；池为空 / 无启用条目时返回空列表。
    """
    result: List[str] = []
    for item in pool or []:
        try:
            enabled = bool(getattr(item, "enabled", True))
            cookie = str(getattr(item, "cookie", "") or "").strip()
        except Exception:  # noqa: BLE001 - 脏数据容错
            continue
        if enabled and cookie:
            result.append(cookie)
    return result


def pool_usable_cookies(pool: Any) -> List[str]:
    """返回 Cookie 池中**「启用 + 非空 + 健康」**条目的明文 Cookie 列表（保序）。

    健康定义（v1.8，C11）：`detect_cookie_health` 状态 ∈ {ok, expiring}。
    与 `pool_enabled_cookies` 互补：后者只过滤启用/非空，前者再过滤过期 /
    缺 token / 无法解密等失效条目——**失效条目不参与轮换**，避免向 fetcher
    注入已过期 Cookie。

    Args:
        pool: Cookie 池（列表）。

    Returns:
        健康条目的 Cookie 字符串列表（保序）；池为空 / 无健康条目时返回空列表。
    """
    usable: List[str] = []
    for cookie in pool_enabled_cookies(pool):
        try:
            state, _reason = detect_cookie_health(cookie)
        except Exception:  # noqa: BLE001 - 检测异常按不可用处理
            continue
        if state in (HEALTH_OK, HEALTH_EXPIRING):
            usable.append(cookie)
    return usable


def resolve_cookie_for_round(monitor: Any, round_index: int = 0) -> str:
    """多 Cookie 轮换策略：**池优先、单值兜底**（v3.2 + v1.8 健康过滤）。

    v1.8（C11/C14）：
        - 池中仅用「健康」条目（ok / expiring）轮换，过期 / 缺 token / 无法解密
          的条目自动跳过，避免向 fetcher 注入已过期 Cookie；
        - 池内健康条目为空但存在启用条目时：回退单值 `monitor.cookies`
          （单值健康才用）；单值也不健康 → 返回空串并打 warning
          （「全部 Cookie 失效，本轮抓取将失败」，C14）；
        - 池为空 / 无启用条目 → 回退单值（与旧行为一致）。

    Args:
        monitor: MonitorConfig 或结构兼容对象（含 cookie_pool / cookies 属性）。
        round_index: 从 0 开始的轮次序号。

    Returns:
        本轮应使用的 Cookie 字符串（可能为空串）。
    """
    pool = pool_usable_cookies(getattr(monitor, "cookie_pool", None))
    if pool:
        return pool[int(round_index) % len(pool)]

    single = str(getattr(monitor, "cookies", "") or "")
    if single:
        try:
            state, _reason = detect_cookie_health(single)
        except Exception:  # noqa: BLE001 - 检测异常按不可用处理
            state = HEALTH_MISSING
        if state in (HEALTH_OK, HEALTH_EXPIRING):
            return single

    # 池存在启用条目但无健康条目，且单值也不可用 → C14 兜底日志
    if pool_enabled_cookies(getattr(monitor, "cookie_pool", None)) and not pool:
        logger.warning("池中所有 Cookie 已过期/无效，且单值 Cookie 亦不可用，本轮抓取将失败（C14）")
    return ""


# ---------------------------------------------------------------------- #
# 配置写回
# ---------------------------------------------------------------------- #
def save_cookies_to_config(config_path: str, cookie_str: str) -> None:
    """把 Cookie 字符串写入 config.yaml 的 monitor.cookies，保留其它字段。

    注意：这是**低电平**接口，保持明文语义（存量测试依赖）。
    推荐使用 `save_cookies_encrypted`（高电平，自动加密）。

    Args:
        config_path: 配置文件路径。
        cookie_str: Cookie 请求头字符串。

    Raises:
        ValueError: cookie_str 为空。
        OSError: 文件读写失败。
        yaml.YAMLError: 原文件 YAML 语法错误。
    """
    cookie_str = str(cookie_str or "").strip()
    if not cookie_str:
        raise ValueError("Cookie 字符串不能为空")

    # 读取现有配置（文件不存在时从空结构开始，保证 login 可先于其它配置执行）
    data: Dict[str, Any] = {}
    try:
        with open(config_path, "r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp)
        if isinstance(loaded, dict):
            data = loaded
    except FileNotFoundError:
        logger.warning("配置文件 %s 不存在，将创建仅含 monitor.cookies 的新文件", config_path)

    monitor = data.get("monitor")
    if not isinstance(monitor, dict):
        monitor = {}
    monitor["cookies"] = cookie_str
    data["monitor"] = monitor

    with open(config_path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False, default_flow_style=False)
    logger.info("已把 Cookie 写入 %s 的 monitor.cookies（长度 %d）", config_path, len(cookie_str))


def save_cookies_validated(config_path: str, cookie_str: str) -> None:
    """校验后保存：`detect_cookie_health` 非 `ok` → 抛 ValueError 且**不落盘**。

    v1.8（C15/C20）：任何刷新路径（GUI / CLI）保存前必须校验——缺 token /
    已过期 / 无法解密等状态一律拒绝保存并给出可操作中文原因，避免把无效
    Cookie 写进 config.yaml。通过校验后调用 `save_cookies_to_config`
    （保持既有明文/加密语义，不破坏存量测试与 frozen 的 ensure_cookie_encrypted）。

    Args:
        config_path: 配置文件路径。
        cookie_str: Cookie 请求头字符串。

    Raises:
        ValueError: cookie 为空 / 校验非 `ok`（含中文原因文案）。
        OSError: 文件读写失败。
        yaml.YAMLError: 原文件 YAML 语法错误。
    """
    cookie_str = str(cookie_str or "").strip()
    state, reason = detect_cookie_health(cookie_str)
    if state != HEALTH_OK:
        raise ValueError(f"Cookie 无效（{state}）：{reason}，未保存任何改动。")
    save_cookies_to_config(config_path, cookie_str)


def save_cookies_validated_encrypted(config_path: str, cookie_str: str) -> None:
    """校验后**加密**保存：`detect_cookie_health` 非 `ok` → 抛 ValueError 且**不落盘**。

    与 `save_cookies_validated` 的区别（设计 §4.2 / 共享知识 7「不允许存在明文
    持久化路径」）：
        - 流程为「校验（不落盘）→ 内存 `encrypt_text` → **单次原子写盘**」，
          Cookie 明文只存在于内存，**磁盘上不存在明文持久化窗口**；
        - 加密不可用（cryptography 缺失 / 密钥失败 / `encrypt_text` 降级返回明文）
          → 抛 ValueError 拒绝保存，**绝不降级明文落盘**（与 `save_cookies_encrypted`
          的降级语义刻意不同，供 Web 粘贴路径使用）；
        - 写盘用「同目录临时文件 + `os.replace`」原子替换：进程中途被 kill
          也不会留下半截文件或明文内容。

    v1.8 兼容性：与 `save_cookies_validated` 共用同一校验函数 `detect_cookie_health`，
    无时间戳的 `_m_h5_tk=t` 历史样本仍判定 `ok` 可保存（旧测试不破）；CLI login
    路径（`save_cookies_validated`）语义不变。

    Args:
        config_path: 配置文件路径。
        cookie_str: Cookie 请求头字符串（明文）。

    Raises:
        ValueError: cookie 为空 / 校验非 `ok` / 加密不可用（含中文原因文案）。
        OSError: 文件读写失败。
        yaml.YAMLError: 原文件 YAML 语法错误。
    """
    cookie_str = str(cookie_str or "").strip()
    state, reason = detect_cookie_health(cookie_str)
    if state != HEALTH_OK:
        raise ValueError(f"Cookie 无效（{state}）：{reason}，未保存任何改动。")
    # 内存加密（绝不先写明文）：加密降级返回明文时视为不可用，拒绝保存
    cipher = secure.encrypt_text(cookie_str)
    if not secure.is_encrypted(cipher):
        raise ValueError("Cookie 加密不可用（Fernet 密钥缺失或不可用），未保存任何改动，请检查安装。")

    # 读取现有配置（文件不存在时从空结构开始），仅更新 monitor.cookies 字段
    data: Dict[str, Any] = {}
    try:
        with open(config_path, "r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp)
        if isinstance(loaded, dict):
            data = loaded
    except FileNotFoundError:
        logger.warning("配置文件 %s 不存在，将创建新文件", config_path)

    monitor = data.get("monitor")
    if not isinstance(monitor, dict):
        monitor = {}
    monitor["cookies"] = cipher
    monitor["cookies_encrypted"] = True
    data["monitor"] = monitor

    # 单次原子写盘：同目录临时文件 + os.replace
    parent = os.path.dirname(os.path.abspath(config_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False, default_flow_style=False)
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info("已把 Cookie 加密写入 %s 的 monitor.cookies（fernet1: 密文）", config_path)


def save_cookies_encrypted(config_path: str, cookie_str: str) -> None:
    """高电平：把 Cookie **加密**写入 config.yaml（DPAPI 可用时）。

    - DPAPI 可用：写入 `dpapi1:base64` 密文，并置 `cookies_encrypted: true`；
    - 非 Windows / 加密失败：降级写明文（不抛异常），不写加密标记。

    Args:
        config_path: 配置文件路径。
        cookie_str: Cookie 请求头字符串。

    Raises:
        ValueError: cookie_str 为空。
        OSError: 文件读写失败。
    """
    cookie_str = str(cookie_str or "").strip()
    if not cookie_str:
        raise ValueError("Cookie 字符串不能为空")

    data: Dict[str, Any] = {}
    try:
        with open(config_path, "r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp)
        if isinstance(loaded, dict):
            data = loaded
    except FileNotFoundError:
        logger.warning("配置文件 %s 不存在，将创建新文件", config_path)

    cipher = secure.encrypt_text(cookie_str)
    monitor = data.get("monitor")
    if not isinstance(monitor, dict):
        monitor = {}
    monitor["cookies"] = cipher
    if secure.is_encrypted(cipher):
        monitor["cookies_encrypted"] = True
    else:
        monitor.pop("cookies_encrypted", None)
    data["monitor"] = monitor

    with open(config_path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False, default_flow_style=False)
    logger.info("已把 Cookie 写入 %s 的 monitor.cookies（加密：%s）", config_path, secure.is_encrypted(cipher))


def ensure_cookie_encrypted(config_path: str) -> bool:
    """迁移存量明文 Cookie → DPAPI 密文（就地重写）。

    用于 `cli login`（frozen 打包版）与 GUI 保存路径：检测到明文 Cookie
    且 DPAPI 可用时自动加密，日志输出「已自动加密 Cookie」。

    Args:
        config_path: 配置文件路径。

    Returns:
        True 表示执行了明文→密文迁移；否则返回 False（已加密/为空/失败）。
    """
    try:
        with open(config_path, "r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp)
    except FileNotFoundError:
        return False
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("读取配置 %s 失败，跳过 Cookie 加密迁移：%s", config_path, exc)
        return False

    if not isinstance(loaded, dict):
        return False
    monitor = loaded.get("monitor")
    if not isinstance(monitor, dict):
        return False
    raw = str(monitor.get("cookies") or "").strip()
    if not raw or secure.is_encrypted(raw):
        return False

    cipher = secure.encrypt_text(raw)
    if not secure.is_encrypted(cipher):
        return False  # 非 Windows 降级为明文，无需迁移

    monitor["cookies"] = cipher
    monitor["cookies_encrypted"] = True
    try:
        with open(config_path, "w", encoding="utf-8") as fp:
            yaml.safe_dump(loaded, fp, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except OSError as exc:
        logger.warning("写入配置 %s 失败，Cookie 保持明文：%s", config_path, exc)
        return False

    logger.info("已自动加密 Cookie（%s）", config_path)
    return True


# ---------------------------------------------------------------------- #
# 半自动：Playwright 打开浏览器让用户登录
# ---------------------------------------------------------------------- #
def profile_dir() -> str:
    """返回浏览器持久化 profile 目录（位于 `paths.data_dir()`，随数据卷持久化）。

    放在数据目录下，容器重建 / 迁移 NAS 时登录态跟着一起走 ——
    这正是「只需人工登录一次」的实现基础。
    """
    from . import paths  # 延迟导入，避免循环依赖

    return os.path.join(paths.data_dir(), PROFILE_DIR_NAME)


def profile_ready() -> bool:
    """浏览器持久化 profile 是否已建立且非空。

    这是「能否走免扫码静默刷新」的判据。profile 为空时静默刷新**必然失败**，
    调用方应快速失败并提示「人工登录一次」，而不是启动浏览器空等满超时 ——
    Web 端点曾因此在一次冒烟测试里整整挂住 30 秒。
    """
    target = profile_dir()
    try:
        return os.path.isdir(target) and any(os.scandir(target))
    except OSError:
        return False


def acquire_via_playwright(
    timeout: Optional[float] = None,
    headless: Optional[bool] = None,
) -> str:
    """用**持久化 profile** 打开闲鱼并提取 Cookie（免扫码优先）。

    与旧实现的关键差别：改用 `launch_persistent_context(user_data_dir=...)`，
    而不是 `launch() + new_context()`。旧写法每次都是**全新匿名会话**，
    所以每次都要重新扫码；持久化之后，只要 profile 里的登录态还在，
    打开一次站点就能拿到新鲜的 `_m_h5_tk`，**无需任何人工操作**。

    模式选择（`headless=None` 时自动判断）：
        - profile 已存在 → **静默模式**（headless=True）：无人值守刷新，
          等待上限 `DEFAULT_SILENT_TIMEOUT`（30 秒）；
        - profile 不存在 → **交互模式**（headless=False）：需要人工扫码一次，
          等待上限 `DEFAULT_LOGIN_TIMEOUT`（120 秒）。

    容器内没有显示器，无法扫码。首次请按「本机登录 → 把 `browser_profile/`
    目录拷进目标机数据目录」的方式建立 profile，此后即可静默刷新。

    Args:
        timeout: 等待上限（秒）。None 时按模式取默认值。
        headless: 是否无头；None = 依 profile 是否存在自动判断。

    Returns:
        含 `_m_h5_tk` 的 Cookie 请求头字符串。

    Raises:
        PlaywrightUnavailable: 未安装 playwright 或浏览器内核未安装。
        LoginTimeout: 超时仍未取到关键 Cookie（登录态已失效，需人工登录一次）。
    """
    try:
        # 延迟导入：playwright 是可选依赖，避免主流程硬依赖
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightUnavailable(
            "未安装 Playwright。请先执行：\n"
            "    pip install playwright\n"
            "    playwright install chromium\n"
            "或改用手动粘贴模式。"
        ) from exc

    ready = profile_ready()
    if headless is None:
        headless = ready
    if headless and not ready:
        # 静默模式的前提就是 profile 已存在。不存在时**立刻失败**，
        # 不要白白启动一个浏览器再等满超时（Web 端点曾因此挂住 30 秒）。
        raise LoginTimeout(
            "尚未建立浏览器登录 profile，无法静默刷新。请先在有浏览器的机器上运行一次 "
            "`cli cookie refresh`（会打开浏览器完成登录），再把生成的 browser_profile "
            "目录复制到本机数据目录；或直接粘贴 Cookie。"
        )
    if timeout is None:
        timeout = DEFAULT_SILENT_TIMEOUT if headless else DEFAULT_LOGIN_TIMEOUT
    timeout = max(1.0, float(timeout))

    target = profile_dir()
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        raise LoginTimeout(f"无法创建浏览器 profile 目录 {target}：{exc}") from exc

    if headless:
        logger.info(
            "使用已有浏览器 profile 静默刷新 Cookie（最多等待 %d 秒）……", int(timeout)
        )
    else:
        logger.info("未检测到可用 profile，启动浏览器等待人工登录……")

    try:
        with sync_playwright() as pw:
            try:
                # 容器内以 root 运行 chromium 必须关沙盒；
                # --disable-dev-shm-usage 规避 /dev/shm 过小导致的渲染进程崩溃。
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=target,
                    headless=headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except Exception as exc:  # noqa: BLE001 - 内核未安装等启动失败
                raise PlaywrightUnavailable(
                    f"Chromium 启动失败：{exc}\n"
                    "若尚未安装浏览器内核，请执行：playwright install chromium"
                ) from exc

            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
                if not headless:
                    print(
                        "已打开闲鱼页面，请在浏览器窗口中完成登录；"
                        f"登录成功后将自动提取 Cookie（最多等待 {int(timeout)} 秒）……"
                    )
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    cookies = context.cookies()
                    if any(c.get("name") == REQUIRED_COOKIE_NAME for c in cookies):
                        header = build_cookie_header(cookies)
                        logger.info(
                            "已检测到 %s，共提取 %d 个 Cookie（profile: %s）",
                            REQUIRED_COOKIE_NAME,
                            len(cookies),
                            target,
                        )
                        return header
                    time.sleep(POLL_INTERVAL)
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                    pass

        if headless:
            raise LoginTimeout(
                f"静默刷新失败（{int(timeout)} 秒内未取到 {REQUIRED_COOKIE_NAME}）——"
                "profile 中的登录态可能已失效，需要人工登录一次：\n"
                "  1) 在**能打开浏览器**的机器上运行 `cli login`（会写入 browser_profile/）；\n"
                "  2) 把该目录（位于数据目录下）复制到本机的数据目录；\n"
                "  3) 之后即可继续免扫码静默刷新。"
            )
        raise LoginTimeout(
            f"登录超时（{int(timeout)} 秒内未检测到 {REQUIRED_COOKIE_NAME}）。"
            "请重试，或改用手动模式：cli login --cookie-string \"...\""
        )
    except (PlaywrightUnavailable, LoginTimeout):
        raise
    except Exception as exc:  # noqa: BLE001 - 其余 playwright 运行期错误统一转成清晰提示
        raise LoginTimeout(f"浏览器会话异常中断：{exc}。请重试或改用手动模式。") from exc


# ---------------------------------------------------------------------- #
# 手动：终端粘贴
# ---------------------------------------------------------------------- #
def acquire_via_prompt() -> str:
    """提示用户在终端粘贴 Cookie 请求头字符串。

    Returns:
        strip 后的非空 Cookie 字符串。

    Raises:
        ValueError: 用户输入为空。
    """
    print(
        "请粘贴浏览器复制的 Cookie 请求头字符串"
        f"（形如 `cookie2=...; {REQUIRED_COOKIE_NAME}=...`，须包含 {REQUIRED_COOKIE_NAME}）："
    )
    raw = input("> ").strip()
    if not raw:
        raise ValueError("输入为空，未保存任何 Cookie")
    if REQUIRED_COOKIE_NAME not in raw:
        # 只提醒不拦截：某些场景用户可能确实只有部分 Cookie
        logger.warning("输入中未发现 %s，抓取真实数据时可能被风控拦截", REQUIRED_COOKIE_NAME)
    return raw
