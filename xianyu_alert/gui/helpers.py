"""GUI 纯函数层：格式化 / 校验 / 表单转换，不依赖任何 widget，便于单元测试（v1.9.5 从 gui.py 拆出）。"""

from __future__ import annotations

import contextlib
import copy
import logging
import os
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import yaml

from .. import __version__, secure

# 防御性导入：无图形环境（如 CI / 无 tkinter 的打包机）也能 import 本模块
# 并运行全部纯函数测试；真正构造窗口时 tkinter 必须可用。
try:  # pragma: no cover - 分支由运行环境决定
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    TK_AVAILABLE = True
except ImportError:  # pragma: no cover - 无 tkinter 环境
    tk = None  # type: ignore[misc,assignment]
    messagebox = None  # type: ignore[misc,assignment]
    ttk = None  # type: ignore[misc,assignment]
    ScrolledText = None  # type: ignore[misc,assignment]
    TK_AVAILABLE = False
from ..config import (
    DEFAULT_PRESET_EXCLUDE_KEYWORDS,
    VALID_FETCHER_TYPES,
    serialize_cookie_pool,
)
from ..cookie import (
    cookie_has_token,
    cookie_prefers_rotation,
)
from ..fetcher import MTOP_TOKEN_COOKIE
from ..filters import normalize_keywords, required_keywords_default
from ..models import Product
from ..storage import Storage
from .constants import (
    CHANNEL_FIELDS,
    CHANNEL_ORDER,
    CHANNEL_REQUIRED_FIELDS,
    COOKIE_STATE_EXPIRED,
    COOKIE_STATE_EXPIRING,
    COOKIE_STATE_MISSING,
    COOKIE_STATE_NO_TOKEN,
    COOKIE_STATE_OK,
    COOKIE_STATE_UNDECRYPTABLE,
    DEFAULT_CONFIG_DICT,
    DEFAULT_DB_PATH,
    FETCHER_CHOICES,
    PRESET_EXCLUDE_KEYWORDS,
    UPDATE_LOG,
)

logger = logging.getLogger(__name__)

def config_file_mtime(path: str) -> float | None:
    """读取配置文件的修改时间戳（秒）；文件不存在 / 读取失败返回 None。

    v1.8（C22）：GUI 用它在 `_tick` 里检测 config.yaml 是否被外部修改
    （如挂机时用 `cli login` 刷新 Cookie），本进程保存后需更新快照避免自触发。

    Args:
        path: 配置文件路径。

    Returns:
        mtime 秒数；无法读取返回 None。
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def cookie_status(cookie_str: str) -> tuple[str, str]:
    """判定 Cookie 的状态并给出展示文案（v3 升级为六态）。

    状态码：
        COOKIE_STATE_MISSING       未配置
        COOKIE_STATE_UNDECRYPTABLE 密文无法解密（换机/换用户）→ 请重新登录
        COOKIE_STATE_NO_TOKEN      已配置但缺 `_m_h5_tk`
        COOKIE_STATE_EXPIRED       令牌已过期（距上次续期超过有效期；下次抓取会自动续期）
        COOKIE_STATE_EXPIRING      令牌即将过期（同样会在下次抓取自动续期）
        COOKIE_STATE_OK            正常（含 `_m_h5_tk` 且未过期）

    Args:
        cookie_str: Cookie 请求头字符串（可为 `dpapi1:` 密文）。

    Returns:
        (状态码, 中文展示文案)。
    """
    raw = str(cookie_str or "").strip()
    if not raw:
        return COOKIE_STATE_MISSING, "⚠️ 未配置 Cookie（mtop 真实抓取必需）"
    if secure.is_encrypted(raw):
        # 密文：解密后继续判定；解密失败则给出「无法解密」提示
        decrypted = secure.decrypt_text(raw)
        if not decrypted:
            return COOKIE_STATE_UNDECRYPTABLE, "❌ Cookie 无法解密（可能换机/换用户），请重新登录"
        raw = decrypted
    # 键级判断（避免 `_m_h5_tk_enc` 等含子串的 Cookie 被误判为有 token）
    if not cookie_has_token(raw):
        return COOKIE_STATE_NO_TOKEN, f"⚠️ 已配置但不含 {MTOP_TOKEN_COOKIE}，可能无效"

    from ..cookie import cookie_expiry_status, token_expiring_text, token_ttl_text

    status = cookie_expiry_status(raw)
    if status == "expired":
        return (
            COOKIE_STATE_EXPIRED,
            f"❌ 登录令牌已过期（{token_ttl_text()}内未续期）—— 抓取时会自动申请新令牌；"
            "持续失败才需要重新登录",
        )
    if status == "expiring":
        return (
            COOKIE_STATE_EXPIRING,
            f"⚠️ 令牌即将过期（剩余不足 {token_expiring_text()}），抓取时会自动续期",
        )
    return COOKIE_STATE_OK, f"✅ 已配置（含 {MTOP_TOKEN_COOKIE}）"


def validate_pages(text: Any) -> int:
    """校验「抓取页数」输入。

    Args:
        text: 页数输入内容。

    Returns:
        正整数页数（>=1）。

    Raises:
        ValueError: 为空 / 非数字 / 非整数 / 小于 1。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("抓取页数不能为空")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"抓取页数必须是整数，当前输入：{raw}") from exc
    if value != int(value):
        raise ValueError(f"抓取页数必须是整数，当前输入：{raw}")
    pages = int(value)
    if pages < 1:
        raise ValueError(f"抓取页数必须大于等于 1，当前输入：{raw}")
    return pages


#: 关键词表为空时的引导文案（U1；v3.6 按钮拆分后同步更新）
EMPTY_STATE_HINT = "还没有关键词，请在上方输入后点击「➕ 添加」"


def empty_state_hint(has_keywords: bool) -> str:
    """关键词表为空时的占位引导文案。

    Args:
        has_keywords: 表格中是否已有关键词。

    Returns:
        引导文案；已有关键词时返回空串（隐藏占位）。
    """
    return "" if has_keywords else EMPTY_STATE_HINT


#: 首次使用（mtop + 无 Cookie）时的获取步骤引导（U2）
#: v3.3：GUI 已移除「获取 Cookie」按钮，自动登录入口取消；
#:       手动步骤说明收进「Cookie 管理」对话框的「如何获取 Cookie？」帮助。
COOKIE_FIRST_USE_GUIDE = (
    "首次使用 mtop 真实抓取需要登录 Cookie：\n"
    "  1. 点击右侧「Cookie 管理」按钮；\n"
    "  2. 在对话框中点击「❓ 如何获取 Cookie？」查看手动步骤（登录 goofish.com →"
    " F12 → Network → 搜词 → 找 h5api.m.goofish.com 请求 → 复制含 _m_h5_tk 的 Cookie 头）；\n"
    "  3. 把 Cookie 粘贴进「添加」对话框保存，状态灯变绿后即可开始监控。"
)


def first_use_guide(ftype: str, state: str) -> str:
    """首次使用引导：fetcher=mtop 且 Cookie 未配置时返回引导文案。

    Args:
        ftype: 抓取器类型。
        state: Cookie 状态码（cookie_status 的返回值）。

    Returns:
        引导文案；不需要引导时返回空串。
    """
    if str(ftype or "").strip().lower() == "mtop" and state == COOKIE_STATE_MISSING:
        return COOKIE_FIRST_USE_GUIDE
    return ""


def about_text() -> str:
    """「关于」对话框文案（版本号 / 作者 / 说明 / 免责声明）。

    版本号随 `xianyu_alert.__version__` 自动更新，无需手工维护。
    """
    return (
        f"闲鱼低价提醒工具 v{__version__}\n\n"
        "作者：寇豆码（Kou）\n\n"
        "功能说明：\n"
        "  周期性监测闲鱼关键词搜索结果，商品价格低于阈值时通过\n"
        "  控制台 / Server酱 / 邮件 / Telegram / Bark / Webhook 推送提醒。\n\n"
        "快速上手：\n"
        "  1. 在「监控配置」页添加关键词与价格阈值；\n"
        "  2. 选择 mtop 抓取并获取登录 Cookie（支持多账号 Cookie 池，Cookie 会加密保存）；\n"
        "  3. 在「通知设置」页勾选通知通道；\n"
        "  4. 回到「运行监控」页点击「开始监控」。\n\n"
        "免责声明：\n"
        "  本工具仅供个人学习与辅助使用，请遵守闲鱼平台规则，\n"
        "  控制抓取频率，勿用于商业用途。"
    )


def about_full_text() -> str:
    """「关于」对话框完整文案：基础信息 + 版本历史（v3.2）。"""
    return f"{about_text()}\n\n{UPDATE_LOG}"


def validate_keyword_entry(keyword: Any, price_text: Any) -> tuple[str, float]:
    """校验「关键词 + 价格阈值」输入。

    Args:
        keyword: 关键词输入内容。
        price_text: 价格阈值输入内容。

    Returns:
        (规范化关键词, 价格阈值 float) 二元组。

    Raises:
        ValueError: 关键词为空、价格为空 / 非数字 / 非正数。
    """
    kw = str(keyword or "").strip()
    if not kw:
        raise ValueError("关键词不能为空")

    text = str(price_text or "").strip()
    if not text:
        raise ValueError("价格阈值不能为空")
    try:
        price = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"价格阈值必须是数字，当前输入：{text}") from exc
    if price <= 0:
        raise ValueError(f"价格阈值必须为正数，当前输入：{text}")
    return kw, price


def parse_keyword_lines(text: Any) -> list[str]:
    """把多行文本解析为去空、去重的关键词列表（每行一个）。

    Args:
        text: 对话框中的多行文本（可为 None / 空串）。

    Returns:
        规范化后的关键词列表（保序去重）。
    """
    return normalize_keywords(str(text or "").splitlines())


def add_preset_excludes(
    excludes: list[str], preset: Sequence[str] | None = None
) -> list[str]:
    """在现有排除词基础上追加预置排除词（去重保序）。

    Args:
        excludes: 当前排除词列表。
        preset: 预置排除词列表；None 时使用内置默认
            （`PRESET_EXCLUDE_KEYWORDS`，v3.5 起默认值来自
            `config.DEFAULT_PRESET_EXCLUDE_KEYWORDS`，向后兼容）。

    Returns:
        合并预置排除词后的新列表。
    """
    presets = normalize_keywords(list(preset) if preset is not None else list(PRESET_EXCLUDE_KEYWORDS))
    return normalize_keywords(list(excludes or []) + presets)


def resolve_preset_exclude_keywords(form_value: Any) -> list[str]:
    """解析 GUI 表单中的预置排除词模板（v3.5，BUG-1 修复）。

    **只有 `None`（表单缺省/缺失）才回退默认列表**；
    显式空列表 `[]` 表示「关闭自动预置」，必须原样保留为空。
    （旧实现用 falsy 判断，导致空列表被 `or` 回退成默认 5 词，关闭失效。）

    Args:
        form_value: config_to_form 返回的 `preset_exclude_keywords` 值。

    Returns:
        规范化后的预置排除词列表（可为空）。
    """
    if form_value is None:
        return list(DEFAULT_PRESET_EXCLUDE_KEYWORDS)
    return normalize_keywords(form_value)


def apply_filter_edit(
    current: dict[str, list[str]] | None,
    exclude_text: Any,
    required_text: Any,
) -> dict[str, list[str]]:
    """把过滤编辑对话框中的多行文本合并为新的过滤规则字典。

    Args:
        current: 当前过滤规则（可能为 None）。
        exclude_text: 排除词多行文本。
        required_text: 必含词多行文本。

    Returns:
        形如 {"exclude_keywords": [...], "required_keywords": [...]} 的新字典。
    """
    result: dict[str, list[str]] = {
        "exclude_keywords": [],
        "required_keywords": [],
    }
    result.update(current or {})
    result["exclude_keywords"] = parse_keyword_lines(exclude_text)
    result["required_keywords"] = parse_keyword_lines(required_text)
    return result


def keyword_filter_summary(filters: dict[str, Any] | None) -> str:
    """把过滤规则字典格式化为表格摘要文案。

    Args:
        filters: 形如 {"exclude_keywords": [...], "required_keywords": [...]} 的字典。

    Returns:
        展示文案；无规则时返回 "—"。
    """
    state = filters or {}
    excludes = normalize_keywords(state.get("exclude_keywords"))
    required = normalize_keywords(state.get("required_keywords"))
    parts: list[str] = []
    if excludes:
        parts.append("排除:" + ",".join(excludes))
    if required:
        parts.append("必含:" + ",".join(required))
    if state.get("spec_filter") is False:
        parts.append("规格过滤:关")
    return " ".join(parts) if parts else "—"


def _parse_str_list(value: Any) -> list[str]:
    """把配置中的列表字段解析为去空去重的字符串列表；非法类型视为空。

    供 config_to_form 使用：界面读取路径对脏数据保持容错，绝不抛异常。
    """
    if not isinstance(value, list):
        return []
    return normalize_keywords(str(item) for item in value)


def validate_interval(text: Any) -> int:
    """校验监测间隔输入。

    Args:
        text: 间隔输入内容（秒）。

    Returns:
        正整数秒数。

    Raises:
        ValueError: 为空 / 非整数 / 非正数。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("监测间隔不能为空")
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"监测间隔必须是整数秒，当前输入：{raw}") from exc
    if seconds <= 0:
        raise ValueError(f"监测间隔必须大于 0，当前输入：{raw}")
    return seconds


def normalize_channel_options(ctype: str, options: dict[str, Any]) -> dict[str, Any]:
    """规范化通道参数：去空白、端口转 int、丢弃空值。

    Args:
        ctype: 通道类型。
        options: 原始参数字典（界面上都是字符串）。

    Returns:
        可直接写入 YAML 的参数字典。
    """
    result: dict[str, Any] = {}
    for key, value in (options or {}).items():
        text = str(value if value is not None else "").strip()
        if not text:
            continue
        if ctype == "email" and key == "smtp_port":
            try:
                result[key] = int(float(text))
            except (TypeError, ValueError):
                result[key] = text
        else:
            result[key] = text
    return result


def channel_is_complete(ctype: str, options: dict[str, Any]) -> bool:
    """判断某通道的必填参数是否齐全。

    Args:
        ctype: 通道类型。
        options: 通道参数字典。

    Returns:
        True 表示可以启用该通道。
    """
    required = CHANNEL_REQUIRED_FIELDS.get(str(ctype or "").strip().lower())
    if required is None:
        return False
    data = options or {}
    return all(str(data.get(field_name, "") or "").strip() for field_name in required)


def fetcher_label(ftype: str) -> str:
    """把抓取器内部值转换为下拉框显示文案。"""
    target = str(ftype or "").strip().lower()
    for value, label in FETCHER_CHOICES:
        if value == target:
            return label
    return FETCHER_CHOICES[0][1]


def fetcher_type_from_label(label: str) -> str:
    """把下拉框显示文案还原为抓取器内部值。"""
    text = str(label or "").strip()
    for value, item_label in FETCHER_CHOICES:
        if item_label == text:
            return value
    # 兼容直接传入内部值
    lowered = text.lower()
    if lowered in VALID_FETCHER_TYPES:
        return lowered
    return FETCHER_CHOICES[0][0]


def default_channel_options(ctype: str) -> dict[str, str]:
    """返回某通道的默认参数字典（用于初始化界面输入框）。"""
    return {name: default for name, _label, _secret, default in CHANNEL_FIELDS.get(ctype, ())}


def config_to_form(data: Any) -> dict[str, Any]:
    """把（可能不规范的）配置字典转换为界面表单状态。

    对任何脏数据都保持容错：非法项直接忽略并回退到默认值，绝不抛异常。
    Cookie 若为 `dpapi1:` 密文会自动解密供界面展示与编辑。

    Args:
        data: config.yaml 解析出的原始字典。

    Returns:
        形如 {"keywords": [(kw, price)], "interval": int, "fetcher_type": str,
        "cookies": str, "storage_path": str, "channels": {...}, "pages": int} 的表单状态。
    """
    root = data if isinstance(data, dict) else {}

    # ---- 关键词 ----
    keywords: list[tuple[str, float]] = []
    #: 关键词 -> 是否启用（v3.7；缺省 True，停用不删除）
    keyword_enabled: dict[str, bool] = {}
    #: 关键词 -> {exclude_keywords, required_keywords}（v3.1 过滤规则）
    keyword_filters: dict[str, dict[str, Any]] = {}
    #: v1.11：规格语义过滤的全局默认（单条关键词可用 spec_filter 覆盖）
    global_spec_filter = parse_enabled_flag(root.get("spec_filter"), default=True)
    raw_keywords = root.get("keywords")
    if isinstance(raw_keywords, list):
        for item in raw_keywords:
            if not isinstance(item, dict):
                continue
            kw = str(item.get("keyword", "") or "").strip()
            if not kw:
                continue
            try:
                price = float(item.get("max_price", 0))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            keywords.append((kw, price))
            # v3.7：启用/停用标记（脏数据容错，缺省 True）
            keyword_enabled[kw] = parse_enabled_flag(item.get("enabled"), default=True)
            # 过滤规则：排除词原样读取；必含词未显式配置时按主关键词自动提取，
            # 与 config 解析保持一致（保证界面展示的就是实际生效的规则）。
            exclude_keywords = _parse_str_list(item.get("exclude_keywords"))
            spec_on = parse_enabled_flag(item.get("spec_filter"), default=global_spec_filter)
            if "required_keywords" in item:
                required_keywords = _parse_str_list(item.get("required_keywords"))
            else:
                required_keywords = required_keywords_default(kw, spec_on)
            keyword_filters[kw] = {
                "exclude_keywords": exclude_keywords,
                "required_keywords": required_keywords,
                # v1.11：规格语义过滤开关随表单往返（缺省 true；false = 退回字面匹配）
                "spec_filter": spec_on,
            }

    # ---- monitor ----
    monitor = root.get("monitor")
    monitor = monitor if isinstance(monitor, dict) else {}
    try:
        interval = int(float(monitor.get("interval_seconds", 600)))
    except (TypeError, ValueError):
        interval = 600
    if interval <= 0:
        interval = 600
    cookies_raw = str(monitor.get("cookies") or "").strip()
    cookies_was_encrypted = secure.is_encrypted(cookies_raw)
    cookies_undecryptable = False
    if cookies_was_encrypted:
        decrypted = secure.decrypt_text(cookies_raw)
        if decrypted:
            cookies = decrypted
        else:
            cookies = ""
            cookies_undecryptable = True
    else:
        cookies = cookies_raw
    user_agent = str(monitor.get("user_agent") or "").strip()

    # ---- 多 Cookie 池（v3.2）：读取并解密每条 Cookie ----
    # 数据保真（v1.8.1）：解密失败时明文为空，但保留原始密文 `_raw_cipher`，
    # 保存配置时由 serialize_cookie_pool 原样回写该密文，避免「密钥变更 /
    # secret.key 未迁移」导致条目在下一次保存时被整条删除。
    cookie_pool: list[dict[str, Any]] = []
    raw_pool = monitor.get("cookie_pool")
    if isinstance(raw_pool, list):
        for entry in raw_pool:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            raw_cookie = str(entry.get("cookie") or "").strip()
            if not name or not raw_cookie:
                continue
            decrypt_failed = False
            if secure.is_encrypted(raw_cookie):
                decrypted = secure.decrypt_text(raw_cookie)
                if not decrypted:
                    # 密文无法解密：明文置空（对话框内提示），但保留原密文
                    decrypted = ""
                    decrypt_failed = True
            else:
                decrypted = raw_cookie
            try:
                enabled = bool(entry.get("enabled", True))
            except Exception:  # noqa: BLE001 - 脏数据容错
                enabled = True
            item: dict[str, Any] = {"name": name, "cookie": decrypted, "enabled": enabled}
            if decrypt_failed:
                item["_raw_cipher"] = raw_cookie
            cookie_pool.append(item)

    # ---- fetcher ----
    fetcher = root.get("fetcher")
    fetcher = fetcher if isinstance(fetcher, dict) else {}
    ftype = str(fetcher.get("type") or "mtop").strip().lower()
    if ftype not in VALID_FETCHER_TYPES:
        ftype = "mtop"
    try:
        pages = int(float(fetcher.get("pages", 1)))
    except (TypeError, ValueError):
        pages = 1
    if pages < 1:
        pages = 1

    # ---- storage ----
    storage = root.get("storage")
    storage = storage if isinstance(storage, dict) else {}
    storage_path = str(storage.get("path") or DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH

    # ---- notify ----
    channels: dict[str, dict[str, Any]] = {
        ctype: {"enabled": False, "options": default_channel_options(ctype)}
        for ctype in CHANNEL_ORDER
    }
    notify = root.get("notify")
    notify = notify if isinstance(notify, dict) else {}
    raw_channels = notify.get("channels")
    if isinstance(raw_channels, list):
        for item in raw_channels:
            if not isinstance(item, dict):
                continue
            ctype = str(item.get("type", "") or "").strip().lower()
            if ctype not in channels:
                continue
            channels[ctype]["enabled"] = True
            options = channels[ctype]["options"]
            for key, value in item.items():
                if key == "type":
                    continue
                options[key] = "" if value is None else str(value)

    if not any(state["enabled"] for state in channels.values()):
        channels["console"]["enabled"] = True

    # ---- 预置排除词（v3.5）：新关键词自动预置的模板；缺省回退默认 ----
    raw_preset = root.get("preset_exclude_keywords")
    if isinstance(raw_preset, list):
        preset_exclude_keywords = _parse_str_list(raw_preset)
    else:
        preset_exclude_keywords = list(DEFAULT_PRESET_EXCLUDE_KEYWORDS)

    return {
        "keywords": keywords,
        "keyword_enabled": keyword_enabled,
        "keyword_filters": keyword_filters,
        "interval": interval,
        "fetcher_type": ftype,
        "cookies": cookies,
        "cookies_was_encrypted": cookies_was_encrypted,
        "cookies_undecryptable": cookies_undecryptable,
        "user_agent": user_agent,
        "storage_path": storage_path,
        "pages": pages,
        "channels": channels,
        "cookie_pool": cookie_pool,
        "preset_exclude_keywords": preset_exclude_keywords,
    }


def build_config_dict(
    keywords: Sequence[tuple[str, float]],
    interval_seconds: int,
    fetcher_type: str,
    cookies: str,
    storage_path: str,
    channels: dict[str, dict[str, Any]],
    base: dict[str, Any] | None = None,
    pages: int = 1,
    encrypt_cookies: bool = False,
    keyword_filters: dict[str, dict[str, list[str]]] | None = None,
    cookie_pool: list[dict[str, Any]] | None = None,
    preset_exclude_keywords: Sequence[str] | None = None,
    keyword_enabled: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """由界面表单状态组装出完整的配置字典（用于写回 config.yaml）。

    会在 `base` 的基础上做增量覆盖，从而**保留用户手工添加的其它字段**
    （例如 fetcher.mock_products_per_round、monitor.user_agent）。

    Args:
        keywords: [(关键词, 价格阈值)] 列表。
        interval_seconds: 监测间隔秒数。
        fetcher_type: 抓取器类型。
        cookies: Cookie 字符串（明文）。
        storage_path: SQLite 路径。
        channels: {通道类型: {"enabled": bool, "options": {...}}}。
        base: 原有配置字典（保留未被界面覆盖的字段）。
        pages: mtop 多页抓取总页数。
        encrypt_cookies: True 时把 Cookie 加密为 `dpapi1:` 密文再写盘。
        keyword_filters: 关键词过滤规则字典（v3.1）；None 时不写过滤字段，
            传入时对每个关键词显式写出 exclude_keywords / required_keywords
            （含空列表），保证「清空必含词 = 关闭强制」在保存后依然成立。
        cookie_pool: 多 Cookie 池（v3.2），形如
            [{"name": str, "cookie": str(明文), "enabled": bool}]；
            每条 cookie 落盘时自动 DPAPI 加密（不可用则降级明文）。
            None 时保留 base 中已有的 cookie_pool 字段不覆盖。
        preset_exclude_keywords: 预置排除词模板（v3.5）。
            None 时保留 base 中已有字段（若 base 也没有则不写）；
            传入时写为去重保序的字符串列表。
        keyword_enabled: 关键词启用状态字典（v3.7）：{关键词: bool}。
            None 时不写 enabled 字段（向后兼容旧保存路径）；
            传入时对每个关键词写出 `enabled`（停用的关键词保存后仍写回，
            monitor 会跳过它，但 GUI 仍可见可编辑）。

    Returns:
        可直接 yaml.safe_dump 的配置字典。
    """
    data: dict[str, Any] = copy.deepcopy(base) if isinstance(base, dict) else {}

    if preset_exclude_keywords is not None:
        data["preset_exclude_keywords"] = normalize_keywords(preset_exclude_keywords)

    enabled_map = keyword_enabled or {}
    filters = keyword_filters or {}
    out_keywords: list[dict[str, Any]] = []
    for kw, price in (keywords or []):
        entry: dict[str, Any] = {"keyword": str(kw), "max_price": float(price)}
        if keyword_filters is not None:
            state = filters.get(str(kw)) or {}
            entry["exclude_keywords"] = normalize_keywords(state.get("exclude_keywords"))
            entry["required_keywords"] = normalize_keywords(state.get("required_keywords"))
            if state.get("spec_filter") is False:
                # v1.11：只有"显式关闭"才写盘（缺省 true 不落盘，保持配置干净）
                entry["spec_filter"] = False
        if keyword_enabled is not None:
            # v3.7：保存启用状态；停用关键词写 enabled: false
            entry["enabled"] = parse_enabled_flag(enabled_map.get(str(kw)), default=True)
        out_keywords.append(entry)
    data["keywords"] = out_keywords

    monitor = data.get("monitor")
    monitor = dict(monitor) if isinstance(monitor, dict) else {}
    monitor["interval_seconds"] = int(interval_seconds)
    monitor.setdefault("user_agent", "")
    raw_cookies = str(cookies or "")
    if encrypt_cookies and raw_cookies:
        cipher = secure.encrypt_text(raw_cookies)
        if secure.is_encrypted(cipher):
            monitor["cookies"] = cipher
            monitor["cookies_encrypted"] = True
        else:
            # 非 Windows / DPAPI 不可用 → 降级明文，不写加密标记
            monitor["cookies"] = raw_cookies
            monitor.pop("cookies_encrypted", None)
    else:
        monitor["cookies"] = raw_cookies
    if cookie_pool is not None:
        # 界面显式提供了 Cookie 池 → 序列化（逐条加密）后写盘
        monitor["cookie_pool"] = serialize_cookie_pool(cookie_pool, encrypt=True)
    data["monitor"] = monitor

    fetcher = data.get("fetcher")
    fetcher = dict(fetcher) if isinstance(fetcher, dict) else {}
    fetcher["type"] = str(fetcher_type or "mock")
    fetcher["pages"] = int(pages)
    data["fetcher"] = fetcher

    storage = data.get("storage")
    storage = dict(storage) if isinstance(storage, dict) else {}
    storage["path"] = str(storage_path or DEFAULT_DB_PATH)
    data["storage"] = storage

    out_channels: list[dict[str, Any]] = []
    for ctype in CHANNEL_ORDER:
        state = (channels or {}).get(ctype) or {}
        if not state.get("enabled"):
            continue
        options = normalize_channel_options(ctype, state.get("options") or {})
        if not channel_is_complete(ctype, options):
            logger.warning("通道 %s 参数不完整，未写入配置", ctype)
            continue
        entry: dict[str, Any] = {"type": ctype}
        entry.update(options)
        out_channels.append(entry)

    if not out_channels:
        # 兜底：至少保留控制台，避免「提醒静默丢失」
        out_channels = [{"type": "console"}]
    data["notify"] = {"channels": out_channels}
    return data


def load_raw_config(path: str) -> dict[str, Any]:
    """读取 config.yaml 原始字典；文件缺失 / 解析失败时返回内置默认配置。

    Args:
        path: 配置文件路径。

    Returns:
        配置字典（永远不为空，绝不抛异常）。
    """
    try:
        with open(path, encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp)
        if isinstance(loaded, dict) and loaded:
            return loaded
        logger.warning("配置文件 %s 内容为空，已使用内置默认配置", path)
    except FileNotFoundError:
        logger.warning("配置文件 %s 不存在，已使用内置默认配置", path)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("配置文件 %s 读取失败（%s），已使用内置默认配置", path, exc)
    return copy.deepcopy(DEFAULT_CONFIG_DICT)


def save_raw_config(path: str, data: dict[str, Any]) -> None:
    """把配置字典写回 YAML 文件。

    Args:
        path: 配置文件路径。
        data: 配置字典。

    Raises:
        OSError: 写入失败。
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False, default_flow_style=False)


def make_sample_product(keyword: str = "测试关键词") -> Product:
    """构造一个用于「测试发送」的假商品。

    Args:
        keyword: 命中关键词文案。

    Returns:
        假的 Product 实例。
    """
    return Product(
        product_id="0000000000",
        title="【测试消息】闲鱼低价提醒工具通道连通性测试",
        price=1.0,
        url="https://www.goofish.com/",
        publish_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        keyword=keyword,
    )


def format_countdown(seconds: float) -> str:
    """把剩余秒数格式化为 `mm:ss`。

    Args:
        seconds: 剩余秒数（负数按 0 处理）。

    Returns:
        形如 `04:59` 的字符串。
    """
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


#: 提醒记录表的列顺序（与 ttk.Treeview columns 一致）
ALERT_COLUMNS: tuple[str, ...] = ("time", "keyword", "title", "price", "publish")
#: 提醒记录表的表头中文名
ALERT_HEADING_TEXTS: dict[str, str] = {
    "time": "提醒时间",
    "keyword": "关键词",
    "title": "商品名称",
    "price": "价格",
    "publish": "发布时间",
}


def sort_alert_rows(
    rows: Sequence[dict[str, Any]], column: str, ascending: bool = True
) -> list[dict[str, Any]]:
    """按列排序提醒记录（纯函数，v3.2 表格点击表头排序）。

    - `price` 列：按数值排序（剥离 `¥` / 千分位逗号后 `float` 解析）；
      解析失败（如「面议」）视为非法值，**始终排在合法值之后**，
      非法值之间按原字符串兜底排序；
    - 其余列（time / keyword / title / publish）：按字符串排序
      （publish 格式统一为 YYYY-MM-DD HH:MM:SS，字符串序即时间序）；
    - 稳定排序：同 key 记录保持原有相对顺序；`ascending=False` 反序。

    Args:
        rows: 待排序记录列表，每条为含 `column` 键的字典（可含 iid 等附加键）。
        column: 排序列名（必须存在于 ALERT_COLUMNS）。
        ascending: True 升序，False 降序。

    Returns:
        排序后的新列表（不修改入参）。
    """
    if column not in ALERT_COLUMNS:
        return list(rows)

    if column == "price":
        def parse_price(row: dict[str, Any]) -> float | None:
            """解析价格数值；失败返回 None。"""
            text = str(row.get(column, "") or "").replace("¥", "").replace(",", "").strip()
            try:
                return float(text)
            except (TypeError, ValueError):
                return None

        valid = [row for row in rows if parse_price(row) is not None]
        invalid = [row for row in rows if parse_price(row) is None]
        valid.sort(key=lambda row: parse_price(row) or 0.0, reverse=not ascending)
        invalid.sort(key=lambda row: str(row.get(column, "") or ""))
        return valid + invalid

    return sorted(rows, key=lambda row: str(row.get(column, "") or ""), reverse=not ascending)


#: 黑名单相关（v3.6）——提醒记录「🚫 加入黑名单」按钮使用的纯逻辑。
def blacklist_alert_row(
    storage: Storage,
    row: dict[str, Any],
    reason: str = "",
) -> bool:
    """把一条提醒记录加入黑名单（纯逻辑，便于单元测试，GUI 直接复用）。

    Args:
        storage: Storage 实例（已打开）。
        row: 提醒记录行，至少含 `product_id`（可含 `keyword`）。
        reason: 加入原因（可选）。

    Returns:
        True 表示成功加入；False 表示行内缺少 product_id（调用方应提示）。

    Raises:
        ValueError: product_id 为空字符串（由 storage.add_blacklist 抛出）。
    """
    product_id = str(row.get("product_id", "") or "").strip()
    if not product_id:
        return False
    storage.add_blacklist(
        product_id,
        keyword=str(row.get("keyword", "") or ""),
        reason=str(reason or ""),
    )
    return True


# ---------------------------------------------------------------------- #
# v3.7：日志高亮纯函数（前缀 → tag 映射）
# ---------------------------------------------------------------------- #
#: 日志高亮 tag（`_append_log` 会注册这些 Text tag）：
#:   NEW_ITEM : 新商品 / 低价命中（醒目蓝加粗）
#:   SUMMARY  : 本轮完成 / 成功事件（绿加粗）
#:   ROUND    : 轮次分隔线（紫/靛）
#:   DIM      : 已停用 / 已下架等弱化信息（灰）
#: 其余沿用 level tag（INFO / WARNING / ERROR / ALERT）。
LOG_TAG_NEW_ITEM = "NEW_ITEM"
LOG_TAG_SUMMARY = "SUMMARY"
LOG_TAG_ROUND = "ROUND"
LOG_TAG_DIM = "DIM"
#: 可识别的日志 tag 全集（`_append_log` 据此决定是否注册自定义 tag）
LOG_TAGS_CUSTOM = (LOG_TAG_NEW_ITEM, LOG_TAG_SUMMARY, LOG_TAG_ROUND, LOG_TAG_DIM)


def log_tag_for_text(level: str, text: str) -> str:
    """根据日志级别与文本前缀映射高亮 tag（纯函数，v3.7）。

    设计：monitor / GUI 在关键日志行首打 emoji 前缀（🔔 命中 / ✨ 新出现 /
    🚫 已停用 / ✅ 完成），GUI 侧**零侵入**地按前缀着色；即使 monitor 未来
    调整措辞，只要保留这些前缀，高亮就一直生效。

    优先级（从上到下，命中即返回）：
        1. 🚫 / 已停用 → DIM（灰）
        2. 🔔 / 命中低价 / 新出现 / 发现新商品 → NEW_ITEM（蓝加粗）
        3. ✅ / 本轮完成 / 已保存 / 已启动 → SUMMARY（绿加粗）
        4. ⚠️ / WARNING → WARNING（橙）
        5. ❌ / ERROR / 失败 / 异常 → ERROR（红）
        6. ===== / 第 N 轮监测开始 → ROUND（靛）
        7. 其余 → 按 level 回退（ALERT 保持绿色加粗）

    Args:
        level: 日志级别名（INFO / WARNING / ERROR / ALERT / DEBUG / CRITICAL）。
        text: 日志文本（含前缀）。

    Returns:
        应使用的 Text tag 名。
    """
    line = str(text or "")
    if "🚫" in line or "已停用" in line:
        return LOG_TAG_DIM
    if "🔔" in line or "命中低价" in line or "新出现" in line or "发现新商品" in line:
        return LOG_TAG_NEW_ITEM
    if "✅" in line or "本轮完成" in line or "已保存" in line or "已启动" in line:
        return LOG_TAG_SUMMARY
    if "⚠️" in line or "WARNING" in line:
        return "WARNING"
    if "❌" in line or "ERROR" in line or "失败" in line or "异常" in line:
        return "ERROR"
    if "=====" in line or "轮监测开始" in line:
        return LOG_TAG_ROUND
    return level if level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "ALERT") else "INFO"


#: 关键词状态列的展示文案
def keyword_status_text(enabled: bool) -> str:
    """返回关键词表格「状态」列的文案。

    Args:
        enabled: 是否启用。

    Returns:
        "启用✅" 或 "停用⏸"。
    """
    return "启用✅" if enabled else "停用⏸"


def parse_enabled_flag(value: Any, default: bool = True) -> bool:
    """把配置 / 表单中的 enabled 值容错解析为布尔（纯函数，v3.7）。

    YAML / 表单可能给出 `True`、`"true"`、`"false"`、`1`、`0`、`None`
    等形态；与 `config._parse_keywords` 的容错语义保持一致，绝不抛异常。

    Args:
        value: 原始值。
        default: 解析失败（None / 空 / 未知类型）时的兜底值。

    Returns:
        归一化后的布尔值。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "y"):
            return True
        if lowered in ("0", "false", "no", "off", "n", ""):
            return False
        return default
    return default


def _apply_row_style_if_available(obj: Any, item: str, keyword: str) -> None:
    """v3.7：行样式刷新守卫。

    既有测试（test_qa_v3_6_extra）用 SimpleNamespace 绑定真实类方法构造替身，
    只预绑定了部分方法；`_apply_keyword_row_style` 不存在时跳过样式刷新
    （样式是纯视觉增强，不影响逻辑正确性）。
    """
    apply = getattr(obj, "_apply_keyword_row_style", None)
    if apply is not None:
        with contextlib.suppress(Exception): # FakeTree 等替身不支持 tags 时忽略
            apply(item, keyword)


def _keyword_enabled_dict(obj: Any) -> dict[str, bool]:
    """读取 / 惰性初始化对象的 `_keyword_enabled` 状态字典（v3.7）。

    用 getattr / setattr 而不是实例方法：既有测试常用
    `object.__new__(XianyuAlertGUI)` 或 `SimpleNamespace` 构造最小替身，
    不设该属性也没有该方法；这里保证任何路径都能拿到一个可写的 dict。
    """
    state = getattr(obj, "_keyword_enabled", None)
    if state is None:
        state = {}
        with contextlib.suppress(Exception): # 只读替身无法写入时退化为局部 dict
            obj._keyword_enabled = state
    return state


# ====================================================================== #
# 日志 -> 队列
# ====================================================================== #

__all__ = [
    "config_file_mtime",
    "cookie_status",
    "validate_pages",
    "EMPTY_STATE_HINT",
    "empty_state_hint",
    "COOKIE_FIRST_USE_GUIDE",
    "first_use_guide",
    "about_text",
    "about_full_text",
    "validate_keyword_entry",
    "parse_keyword_lines",
    "add_preset_excludes",
    "resolve_preset_exclude_keywords",
    "apply_filter_edit",
    "keyword_filter_summary",
    "validate_interval",
    "normalize_channel_options",
    "channel_is_complete",
    "fetcher_label",
    "fetcher_type_from_label",
    "default_channel_options",
    "config_to_form",
    "build_config_dict",
    "load_raw_config",
    "save_raw_config",
    "make_sample_product",
    "format_countdown",
    "ALERT_COLUMNS",
    "ALERT_HEADING_TEXTS",
    "sort_alert_rows",
    "blacklist_alert_row",
    "LOG_TAG_NEW_ITEM",
    "LOG_TAG_SUMMARY",
    "LOG_TAG_ROUND",
    "LOG_TAG_DIM",
    "LOG_TAGS_CUSTOM",
    "log_tag_for_text",
    "keyword_status_text",
    "parse_enabled_flag",
]


# ---------------------------------------------------------------------- #
# Cookie 池操作（v1.10.10）：纯函数，Tk / Qt 共用
# ---------------------------------------------------------------------- #
# 背景：Tk 的「Cookie 管理」对话框（on_manage_cookies，367 行）与 Qt 的
# CookieDialog 各自实现了一遍「切换启用 / 停用过期 / 删除 / 增改」，
# 逻辑重复且都埋在控件回调里 —— 既难测（要 Tk/Qt 环境）又容易两边跑偏。
# 这里抽成**纯函数**：不碰控件、不改入参、返回新列表。


def pool_toggle_entry(pool: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    """切换第 index 条的启用态。

    Args:
        pool: Cookie 池（每项含 name / cookie / enabled）。
        index: 目标下标；越界时原样返回副本。

    Returns:
        新列表（不改原列表）。
    """
    items = [dict(entry) for entry in pool]
    if 0 <= index < len(items):
        items[index]["enabled"] = not bool(items[index].get("enabled", True))
    return items


def pool_expired_indexes(pool: list[dict[str, Any]]) -> list[int]:
    """过期 / 无效条目下标。

    复用 cookie_prefers_rotation，避免 Tk 与 Qt 各写一套「过期」判定。
    """
    return [
        i
        for i, entry in enumerate(pool)
        if not cookie_prefers_rotation(str(entry.get("cookie") or ""))
    ]


def pool_disable_indexes(pool: list[dict[str, Any]], indexes: list[int]) -> list[dict[str, Any]]:
    """批量停用（**保留条目**，只把 enabled 置 False，可随时恢复）。"""
    items = [dict(entry) for entry in pool]
    for i in indexes:
        if 0 <= i < len(items):
            items[i]["enabled"] = False
    return items


def pool_delete_entry(pool: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    """删除第 index 条；越界时原样返回副本。"""
    items = [dict(entry) for entry in pool]
    if 0 <= index < len(items):
        items.pop(index)
    return items


def pool_upsert_entry(
    pool: list[dict[str, Any]], entry: dict[str, Any], index: int | None = None
) -> list[dict[str, Any]]:
    """新增（index=None）或原地替换第 index 条。

    Args:
        pool: Cookie 池。
        entry: 新条目（会被复制，避免外部后续修改穿透进来）。
        index: 替换位置；None 或越界表示追加到末尾。

    Returns:
        新列表。
    """
    items = [dict(item) for item in pool]
    new_entry = dict(entry)
    if index is None or not (0 <= index < len(items)):
        items.append(new_entry)
    else:
        items[index] = new_entry
    return items


def pool_summary(pool: list[dict[str, Any]]) -> dict[str, int]:
    """池概况：总数 / 启用数 / 停用数 / 过期数（供界面与日志展示）。"""
    enabled = sum(1 for entry in pool if bool(entry.get("enabled", True)))
    return {
        "total": len(pool),
        "enabled": enabled,
        "disabled": len(pool) - enabled,
        "expired": len(pool_expired_indexes(pool)),
    }
