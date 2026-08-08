"""图形界面（Tkinter + ttk）。

面向**不熟悉命令行**的用户：双击 `run_gui.pyw` 或 `start_gui.bat` 即可打开，
在窗口里配置关键词、通知方式、获取 Cookie，并一键开始监控。

技术选型：Python 标准库 Tkinter + ttk，**零新增依赖**，Windows 开箱即用。

线程模型（关键）：
    - Tkinter **不是线程安全**的，所有 widget 操作只能在主线程执行；
    - 监控循环 / 测试通知 / Playwright 登录一律跑在 `threading.Thread(daemon=True)`；
    - 子线程通过 `queue.Queue` 投递消息，主线程用 `root.after(100, ...)` 轮询消费；
    - 停止信号用 `threading.Event`，睡眠用 `event.wait(interval)` 以便立刻响应停止；
    - v3.6：后台线程**绝不访问任何 tkinter 控件**（含 `self.var_*`）；
      控件状态在主线程一次性读取后作为普通值传给后台线程，
      从根上避免后台线程进入 Tcl 解释器争锁导致「窗口无响应」。
      另：单次队列轮询最多消费 `MAX_QUEUE_MESSAGES_PER_POLL` 条，
      日志洪峰时分批渲染，保证主线程始终可拖动、可点击。

本模块**不修改**核心模块的任何既有行为，只复用
`load_config / build_fetcher / Storage / build_notifiers / Monitor.run_once()`。
"""

from __future__ import annotations

import copy
import logging
import os
import queue
import re
import threading
import time
import webbrowser
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

from . import __version__, secure

# 防御性导入：无图形环境（如 CI / 无 tkinter 的打包机）也能 import 本模块
# 并运行全部纯函数测试；真正构造窗口时 tkinter 必须可用。
try:  # pragma: no cover - 分支由运行环境决定
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    TK_AVAILABLE = True
except ImportError:  # pragma: no cover - 无 tkinter 环境
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    ScrolledText = None  # type: ignore[assignment]
    TK_AVAILABLE = False
from .config import (
    DEFAULT_PRESET_EXCLUDE_KEYWORDS,
    DEFAULT_USER_AGENT,
    VALID_FETCHER_TYPES,
    Config,
    ConfigError,
    NotifyChannel,
    config_from_dict,
    serialize_cookie_pool,
)
from .cookie import (
    cookie_has_token,
)
from .fetcher import MTOP_TOKEN_COOKIE, build_fetcher
from .filters import extract_required_keywords, normalize_keywords
from .models import Product
from .monitor import Monitor
from .notifier import build_notifier, build_notifiers
from .shortcut import create_shortcut
from .storage import Storage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------- #
# 常量
# ---------------------------------------------------------------------- #
#: v3.2 起标题栏直接带版本号，用户一眼可知当前版本
WINDOW_TITLE = f"闲鱼低价提醒工具 v{__version__}"
#: v3.6 窗口加宽：让「监控配置」页整排按钮（➕ 添加 / ✏️ 更新选中 / 删除 / 编辑…
#: / 预置词…）在默认窗口内完整可见不溢出；同时适当抬高最小尺寸下限。
WINDOW_SIZE = "1020x720"
MIN_WINDOW_SIZE = (880, 600)
#: UI 队列轮询间隔（毫秒）：队列有消息时用这个频率快速消费
POLL_INTERVAL_MS = 100
#: UI 队列空闲轮询间隔（毫秒，v3.5）：队列为空（挂机）时降到 500ms，
#: 把空转唤醒次数从 10 次/秒降到 2 次/秒，降低挂机 CPU 消耗；
#: 队列一旦有消息立即回到 POLL_INTERVAL_MS。
POLL_IDLE_INTERVAL_MS = 500
#: 单次 `_poll_queue` 最多处理的消息条数（v3.6 UI 防卡）：
#: 关闭「仅展示符合的低价」或日志洪峰时，队列可能瞬间积压大量日志；
#: 若一次性全部在主线程渲染（每次 insert + see("end") 都是 O(n)），
#: 会把主线程拖住数秒。限制单次处理量，剩余消息留到下一轮 after 再消费，
#: 保证窗口始终可拖动、可点击。
MAX_QUEUE_MESSAGES_PER_POLL = 200
#: 提醒记录「加入黑名单」弹窗的默认原因（v3.6）
BLACKLIST_REASON_DEFAULT = "人工剔除"
#: 日志区最多保留的行数（超出后从头裁剪，避免长期运行内存膨胀）
MAX_LOG_LINES = 2000
#: 启动时从数据库加载的历史提醒条数
HISTORY_LIMIT = 200
DEFAULT_DB_PATH = os.path.join("state", "xianyu_alert.db")
#: 关闭窗口时等待监控线程收尾的最大秒数（v3.5 稳定性修复）。
#: 监控线程若正卡在网络请求（mtop 超时 20s + 重试退避）无法立刻响应停止信号，
#: 主线程只等这么久就继续销毁窗口，避免「关闭卡死 / 进程残留」。
CLOSE_JOIN_TIMEOUT = 5.0
#: 「校验在架」批量检查时相邻两次详情接口请求的最小间隔秒数（v3.7）。
#: 详情接口与搜索接口共用同一套 mtop 签名与风控策略，批量校验必须限速，
#: 避免短时间内高频请求触发风控。
SOLD_CHECK_INTERVAL = 1.5
#: 单次「校验在架」最多检查的提醒记录条数（防止一次点按钮请求过猛）。
SOLD_CHECK_MAX_ITEMS = 30
#: 「标记已售出 / 校验在架」的默认原因文案（写回 product.sold_reason）。
SOLD_REASON_MANUAL = "人工标记"
SOLD_REASON_DETAIL = "详情接口判定"

#: 预置排除词（「添加预置排除词」按钮一次性写入；对应回收商/置换商典型帖子）
#: v3.3 起追加「收」，且**添加新关键词时自动预置**（必含词保持留空由用户自填）。
#: v3.5 起可配置：该常量仅作默认值兜底，实际预置词从
#: `config.yaml` 顶层 `preset_exclude_keywords`（GUI「编辑预置排除词」弹窗可改）读取；
#: 缺省时回退到 `DEFAULT_PRESET_EXCLUDE_KEYWORDS`（向后兼容）。
PRESET_EXCLUDE_KEYWORDS: Tuple[str, ...] = tuple(DEFAULT_PRESET_EXCLUDE_KEYWORDS)

#: Cookie 状态灯（v3 升级为六态：未配置 / 缺 token / 已过期 / 即将过期 / 无法解密 / 正常）
COOKIE_STATE_MISSING = "missing"
COOKIE_STATE_NO_TOKEN = "no_token"
COOKIE_STATE_EXPIRED = "expired"
COOKIE_STATE_EXPIRING = "expiring"
COOKIE_STATE_UNDECRYPTABLE = "undecryptable"
COOKIE_STATE_OK = "ok"

#: 抓取器下拉框：(内部值, 界面显示文案)
#: v3.2 起：只展示 mtop（默认，★推荐）+ mock（标注「开发演示用」）；
#:           web 不再展示（代码保留为 legacy，向后兼容旧配置）。
FETCHER_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("mtop", "mtop（真实抓取闲鱼，需登录 Cookie）★推荐"),
    ("mock", "mock（开发演示用，本地假数据，无需登录）"),
)

#: 通知通道展示顺序（v3 新增 bark / webhook）
CHANNEL_ORDER: Tuple[str, ...] = ("console", "serverchan", "email", "telegram", "bark", "webhook")
#: 通道中文名
CHANNEL_LABELS: Dict[str, str] = {
    "console": "控制台（打印到日志区，永远可用）",
    "serverchan": "Server酱（微信推送）",
    "email": "邮件（SMTP）",
    "telegram": "Telegram Bot",
    "bark": "Bark（iOS 推送）",
    "webhook": "企业微信机器人（Webhook）",
}
#: 各通道必填字段
CHANNEL_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    "console": (),
    "serverchan": ("sendkey",),
    "email": ("smtp_host", "smtp_port", "username", "password", "to"),
    "telegram": ("bot_token", "chat_id"),
    "bark": ("url",),
    "webhook": ("url",),
}
#: 各通道字段的界面定义：(字段名, 中文标签, 是否密码框, 默认值)
CHANNEL_FIELDS: Dict[str, Tuple[Tuple[str, str, bool, str], ...]] = {
    "console": (),
    "serverchan": (("sendkey", "SendKey", True, ""),),
    "email": (
        ("smtp_host", "SMTP 服务器", False, "smtp.qq.com"),
        ("smtp_port", "端口（465=SSL / 587=TLS）", False, "465"),
        ("username", "账号（同时作为发件人）", False, ""),
        ("password", "密码 / 授权码", True, ""),
        ("to", "收件人（多个用英文逗号分隔）", False, ""),
    ),
    "telegram": (
        ("bot_token", "Bot Token", True, ""),
        ("chat_id", "Chat ID", False, ""),
    ),
    "bark": (("url", "Bark URL（形如 https://api.day.app/YourKey/）", False, "https://api.day.app/"),),
    "webhook": (("url", "Webhook URL（企业微信群机器人地址）", False, ""),),
}

#: config.yaml 不存在时使用的内置默认配置（v3.2：间隔 600s、抓取器 mtop）
DEFAULT_CONFIG_DICT: Dict[str, Any] = {
    "keywords": [{"keyword": "Switch", "max_price": 1000}],
    "monitor": {"interval_seconds": 600, "user_agent": "", "cookies": ""},
    "fetcher": {"type": "mtop", "mock_products_per_round": 5, "mock_fail_rounds": []},
    "storage": {"path": DEFAULT_DB_PATH},
    "notify": {"channels": [{"type": "console"}]},
    "preset_exclude_keywords": list(DEFAULT_PRESET_EXCLUDE_KEYWORDS),
}

#: 功能更新日志（v3.2：「关于」对话框展示版本历史）
UPDATE_LOG = (
    "## 版本历史\n"
    "- **v1.7.0** 关键词可启用/停用（停用不删除，监控跳过停用词）、"
    "运行日志高亮（新商品/低价命中蓝色加粗、完成绿色、轮次分隔、已下架灰色）、"
    "提醒记录不再显示已售出/下架商品（手动标记 + 详情接口「校验在架」，默认隐藏可切换显示）\n"
    "- **v1.6.0** 「添加」与「更新选中」按钮拆分（修正错别字/改名不再是新增而是更新原行）、"
    "窗口加宽自适应（filters 列拉伸）、修复「立即执行一轮 / 开始监控」后窗口无响应"
    "（后台监控线程不再触碰任何 tkinter 控件 + 日志洪峰分批渲染）、"
    "新增临时黑名单（提醒记录中人工剔除噪音商品 → 不再提醒、不再进提醒记录，支持恢复）\n"
    "- **v1.0.0** 初始 CLI：多关键词+阈值、循环监测、新商品筛选、SQLite 去重、4 通道通知\n"
    "- **v1.1.0** GUI + mtop 真实抓取 + Cookie 自动获取 + DPAPI 加密 + 六态检测\n"
    "- **v1.2.0** 排除关键词 + 必含词过滤 + 打包独立 exe\n"
    "- **v1.3.0** 默认间隔 600s、mtop 默认、多 Cookie 管理、日志清空/排序/可读性、版本号+更新日志\n"
    "- **v1.4.0** 新关键词默认预置排除词（含「收」）、必含词留空自填、移除 GUI 自动获取 Cookie"
    "（改「Cookie 管理」内手动步骤）、企业微信机器人通道更名、日志「仅展示符合的低价」开关、"
    "搜索排序修正（sortField=create + sortValue=desc = 最新发布）\n"
    "- **v1.4.1** 服务端价格筛选（抓取结果与网页「最新发布+价格<阈值」一致，实机验证）、"
    "修复 Cookie 管理「添加」按钮无反应\n"
    "- **v1.5.0** 预置排除词可配置可持久化（「编辑预置排除词」弹窗，新关键词自动带上定制预置词）、"
    "关闭流程稳定性修复（停止信号 → join 超时 → 取消 after → 移除日志 handler → 销毁窗口，"
    "避免运行几轮后关闭 GUI 卡死 / 进程残留）\n"
)

#: 手动粘贴 Cookie 的操作说明
COOKIE_MANUAL_HELP = (
    "手动获取 Cookie 步骤：\n"
    "  1. 用 Chrome / Edge 打开 https://www.goofish.com 并登录你的闲鱼账号；\n"
    "  2. 按 F12 打开开发者工具，切到「网络 / Network」标签；\n"
    "  3. 在闲鱼页面随便搜索一个词（例如 Switch）；\n"
    "  4. 在请求列表中找到发往 h5api.m.goofish.com 的请求并点击；\n"
    "  5. 在「标头 / Headers」→「请求标头 / Request Headers」中找到 Cookie；\n"
    "  6. 复制整行 Cookie 的值（必须包含 _m_h5_tk=）粘贴到下方输入框。"
)


# ====================================================================== #
# 纯函数区（不依赖任何 widget，便于单元测试）
# ====================================================================== #
def cookie_status(cookie_str: str) -> Tuple[str, str]:
    """判定 Cookie 的状态并给出展示文案（v3 升级为六态）。

    状态码：
        COOKIE_STATE_MISSING       未配置
        COOKIE_STATE_UNDECRYPTABLE 密文无法解密（换机/换用户）→ 请重新登录
        COOKIE_STATE_NO_TOKEN      已配置但缺 `_m_h5_tk`
        COOKIE_STATE_EXPIRED       已过期（时间戳超过 24h）
        COOKIE_STATE_EXPIRING      即将过期（剩余不足 1h）
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

    from .cookie import cookie_expiry_status

    status = cookie_expiry_status(raw)
    if status == "expired":
        return COOKIE_STATE_EXPIRED, "❌ Cookie 已过期（超过 24 小时），请重新登录获取"
    if status == "expiring":
        return COOKIE_STATE_EXPIRING, "⚠️ Cookie 即将过期（1 小时内），建议尽快重新登录"
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


def validate_keyword_entry(keyword: Any, price_text: Any) -> Tuple[str, float]:
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


def parse_keyword_lines(text: Any) -> List[str]:
    """把多行文本解析为去空、去重的关键词列表（每行一个）。

    Args:
        text: 对话框中的多行文本（可为 None / 空串）。

    Returns:
        规范化后的关键词列表（保序去重）。
    """
    return normalize_keywords(str(text or "").splitlines())


def add_preset_excludes(
    excludes: List[str], preset: Optional[Sequence[str]] = None
) -> List[str]:
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


def resolve_preset_exclude_keywords(form_value: Any) -> List[str]:
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
    current: Optional[Dict[str, List[str]]],
    exclude_text: Any,
    required_text: Any,
) -> Dict[str, List[str]]:
    """把过滤编辑对话框中的多行文本合并为新的过滤规则字典。

    Args:
        current: 当前过滤规则（可能为 None）。
        exclude_text: 排除词多行文本。
        required_text: 必含词多行文本。

    Returns:
        形如 {"exclude_keywords": [...], "required_keywords": [...]} 的新字典。
    """
    result: Dict[str, List[str]] = {
        "exclude_keywords": [],
        "required_keywords": [],
    }
    result.update(current or {})
    result["exclude_keywords"] = parse_keyword_lines(exclude_text)
    result["required_keywords"] = parse_keyword_lines(required_text)
    return result


def keyword_filter_summary(filters: Optional[Dict[str, List[str]]]) -> str:
    """把过滤规则字典格式化为表格摘要文案。

    Args:
        filters: 形如 {"exclude_keywords": [...], "required_keywords": [...]} 的字典。

    Returns:
        展示文案；无规则时返回 "—"。
    """
    state = filters or {}
    excludes = normalize_keywords(state.get("exclude_keywords"))
    required = normalize_keywords(state.get("required_keywords"))
    parts: List[str] = []
    if excludes:
        parts.append("排除:" + ",".join(excludes))
    if required:
        parts.append("必含:" + ",".join(required))
    return " ".join(parts) if parts else "—"


def _parse_str_list(value: Any) -> List[str]:
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


def normalize_channel_options(ctype: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """规范化通道参数：去空白、端口转 int、丢弃空值。

    Args:
        ctype: 通道类型。
        options: 原始参数字典（界面上都是字符串）。

    Returns:
        可直接写入 YAML 的参数字典。
    """
    result: Dict[str, Any] = {}
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


def channel_is_complete(ctype: str, options: Dict[str, Any]) -> bool:
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
    for field_name in required:
        if not str(data.get(field_name, "") or "").strip():
            return False
    return True


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


def default_channel_options(ctype: str) -> Dict[str, str]:
    """返回某通道的默认参数字典（用于初始化界面输入框）。"""
    return {name: default for name, _label, _secret, default in CHANNEL_FIELDS.get(ctype, ())}


def config_to_form(data: Any) -> Dict[str, Any]:
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
    keywords: List[Tuple[str, float]] = []
    #: 关键词 -> 是否启用（v3.7；缺省 True，停用不删除）
    keyword_enabled: Dict[str, bool] = {}
    #: 关键词 -> {exclude_keywords, required_keywords}（v3.1 过滤规则）
    keyword_filters: Dict[str, Dict[str, List[str]]] = {}
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
            if "required_keywords" in item:
                required_keywords = _parse_str_list(item.get("required_keywords"))
            else:
                required_keywords = extract_required_keywords(kw)
            keyword_filters[kw] = {
                "exclude_keywords": exclude_keywords,
                "required_keywords": required_keywords,
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
    cookie_pool: List[Dict[str, Any]] = []
    raw_pool = monitor.get("cookie_pool")
    if isinstance(raw_pool, list):
        for entry in raw_pool:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            raw_cookie = str(entry.get("cookie") or "").strip()
            if not name or not raw_cookie:
                continue
            if secure.is_encrypted(raw_cookie):
                decrypted = secure.decrypt_text(raw_cookie)
                if not decrypted:
                    # 密文无法解密：保留为空并在对话框中提示
                    decrypted = ""
            else:
                decrypted = raw_cookie
            try:
                enabled = bool(entry.get("enabled", True))
            except Exception:  # noqa: BLE001 - 脏数据容错
                enabled = True
            cookie_pool.append({"name": name, "cookie": decrypted, "enabled": enabled})

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
    channels: Dict[str, Dict[str, Any]] = {
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
    keywords: Sequence[Tuple[str, float]],
    interval_seconds: int,
    fetcher_type: str,
    cookies: str,
    storage_path: str,
    channels: Dict[str, Dict[str, Any]],
    base: Optional[Dict[str, Any]] = None,
    pages: int = 1,
    encrypt_cookies: bool = False,
    keyword_filters: Optional[Dict[str, Dict[str, List[str]]]] = None,
    cookie_pool: Optional[List[Dict[str, Any]]] = None,
    preset_exclude_keywords: Optional[Sequence[str]] = None,
    keyword_enabled: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
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
    data: Dict[str, Any] = copy.deepcopy(base) if isinstance(base, dict) else {}

    if preset_exclude_keywords is not None:
        data["preset_exclude_keywords"] = normalize_keywords(preset_exclude_keywords)

    enabled_map = keyword_enabled or {}
    filters = keyword_filters or {}
    out_keywords: List[Dict[str, Any]] = []
    for kw, price in (keywords or []):
        entry: Dict[str, Any] = {"keyword": str(kw), "max_price": float(price)}
        if keyword_filters is not None:
            state = filters.get(str(kw)) or {}
            entry["exclude_keywords"] = normalize_keywords(state.get("exclude_keywords"))
            entry["required_keywords"] = normalize_keywords(state.get("required_keywords"))
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

    out_channels: List[Dict[str, Any]] = []
    for ctype in CHANNEL_ORDER:
        state = (channels or {}).get(ctype) or {}
        if not state.get("enabled"):
            continue
        options = normalize_channel_options(ctype, state.get("options") or {})
        if not channel_is_complete(ctype, options):
            logger.warning("通道 %s 参数不完整，未写入配置", ctype)
            continue
        entry: Dict[str, Any] = {"type": ctype}
        entry.update(options)
        out_channels.append(entry)

    if not out_channels:
        # 兜底：至少保留控制台，避免「提醒静默丢失」
        out_channels = [{"type": "console"}]
    data["notify"] = {"channels": out_channels}
    return data


def load_raw_config(path: str) -> Dict[str, Any]:
    """读取 config.yaml 原始字典；文件缺失 / 解析失败时返回内置默认配置。

    Args:
        path: 配置文件路径。

    Returns:
        配置字典（永远不为空，绝不抛异常）。
    """
    try:
        with open(path, "r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp)
        if isinstance(loaded, dict) and loaded:
            return loaded
        logger.warning("配置文件 %s 内容为空，已使用内置默认配置", path)
    except FileNotFoundError:
        logger.warning("配置文件 %s 不存在，已使用内置默认配置", path)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("配置文件 %s 读取失败（%s），已使用内置默认配置", path, exc)
    return copy.deepcopy(DEFAULT_CONFIG_DICT)


def save_raw_config(path: str, data: Dict[str, Any]) -> None:
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
ALERT_COLUMNS: Tuple[str, ...] = ("time", "keyword", "title", "price", "publish")
#: 提醒记录表的表头中文名
ALERT_HEADING_TEXTS: Dict[str, str] = {
    "time": "提醒时间",
    "keyword": "关键词",
    "title": "商品名称",
    "price": "价格",
    "publish": "发布时间",
}


def sort_alert_rows(
    rows: Sequence[Dict[str, Any]], column: str, ascending: bool = True
) -> List[Dict[str, Any]]:
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
        def parse_price(row: Dict[str, Any]) -> Optional[float]:
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
    storage: "Storage",
    row: Dict[str, Any],
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
        try:
            apply(item, keyword)
        except Exception:  # noqa: BLE001 - FakeTree 等替身不支持 tags 时忽略
            pass


def _keyword_enabled_dict(obj: Any) -> Dict[str, bool]:
    """读取 / 惰性初始化对象的 `_keyword_enabled` 状态字典（v3.7）。

    用 getattr / setattr 而不是实例方法：既有测试常用
    `object.__new__(XianyuAlertGUI)` 或 `SimpleNamespace` 构造最小替身，
    不设该属性也没有该方法；这里保证任何路径都能拿到一个可写的 dict。
    """
    state = getattr(obj, "_keyword_enabled", None)
    if state is None:
        state = {}
        try:
            obj._keyword_enabled = state
        except Exception:  # noqa: BLE001 - 只读替身无法写入时退化为局部 dict
            pass
    return state


# ====================================================================== #
# 日志 -> 队列
# ====================================================================== #
class QueueLogHandler(logging.Handler):
    """把日志记录推送到线程安全队列，由主线程渲染到日志区。

    这样 monitor / fetcher / notifier 等模块的既有日志无需任何改动
    就能显示在图形界面里。
    """

    def __init__(self, target_queue: "queue.Queue", level: int = logging.INFO) -> None:
        """初始化。

        Args:
            target_queue: 目标队列，元素形如 ("log", (级别名, 文本))。
            level: 处理的最低日志级别。
        """
        super().__init__(level=level)
        self.target_queue: "queue.Queue" = target_queue

    def emit(self, record: logging.LogRecord) -> None:
        """把一条日志放入队列（自身异常绝不向外抛）。"""
        try:
            message = record.getMessage()
            timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            text = f"[{timestamp}] {message}"
            if record.exc_info:
                text = f"{text}\n{self.format(record)}"
            self.target_queue.put_nowait(("log", (record.levelname, text)))
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响业务
            pass


# ====================================================================== #
# 主窗口
# ====================================================================== #
class XianyuAlertGUI:
    """闲鱼低价提醒工具的图形界面主窗口。

    Attributes:
        root: Tk 根窗口。
        config_path: 配置文件路径。
    """

    def __init__(self, root: "tk.Tk", config_path: str = "config.yaml") -> None:
        """构造窗口与全部控件。

        Args:
            root: Tk 根窗口。
            config_path: 配置文件路径。
        """
        self.root: "tk.Tk" = root
        self.config_path: str = config_path

        # ---- 运行时状态 ----
        self.ui_queue: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        self._mode: str = ""            # "loop" / "once" / ""
        self._round_no: int = 0
        self._alert_total: int = 0
        self._next_run_at: float = 0.0
        self._running: bool = False
        self._alert_urls: Dict[str, str] = {}
        #: 提醒记录 iid -> 商品 ID（v3.6 黑名单「加入」需要 product_id）
        self._alert_product_ids: Dict[str, str] = {}
        #: 提醒记录 iid -> 是否已标记「售出/下架」（v3.7；显示已下架时用于置灰）
        self._alert_sold: Dict[str, bool] = {}
        #: 提醒记录是否显示已售出/下架商品（v3.7；默认隐藏，勾选后显示并置灰）
        self._show_sold: bool = False
        #: 关闭流程标志（v3.5）：置位后 `_poll_queue` / `_tick` 不再重新调度 after，
        #: 避免窗口销毁后回调残留导致进程不退出。
        self._closing: bool = False
        #: 已注册的 after 回调 id（v3.5，关闭时显式取消）
        self._poll_after_id: Optional[str] = None
        self._tick_after_id: Optional[str] = None

        # ---- 配置 ----
        self._raw_config: Dict[str, Any] = load_raw_config(self.config_path)
        form = config_to_form(self._raw_config)
        self._cookies: str = form["cookies"]
        #: Cookie 原为密文但无法解密（换机/换用户）→ 状态灯显示「无法解密」
        self._cookies_undecryptable: bool = bool(form.get("cookies_undecryptable", False))
        #: 多 Cookie 池（v3.2）：[{"name", "cookie"(明文), "enabled"}]，内存态
        self._cookie_pool: List[Dict[str, Any]] = list(form.get("cookie_pool") or [])
        self._storage_path: str = form["storage_path"]
        self._keywords: List[Tuple[str, float]] = list(form["keywords"])
        #: 关键词 -> 是否启用（v3.7；停用不删除，保存后写回 config，monitor 跳过）
        self._keyword_enabled: Dict[str, bool] = dict(form.get("keyword_enabled") or {})
        #: 关键词 -> {exclude_keywords, required_keywords}（v3.1 过滤规则）
        self._keyword_filters: Dict[str, Dict[str, List[str]]] = dict(form.get("keyword_filters") or {})
        #: 预置排除词模板（v3.5）：新关键词自动预置的列表；来源 config 顶层
        #: `preset_exclude_keywords`，缺省（缺失/None）回退默认；GUI「编辑预置排除词」可改。
        #: 注意：显式空列表 [] 表示「关闭自动预置」，必须保留为空，不能用 falsy 判断
        #: 回退默认（BUG-1 修复：`or` → None 判断，见 resolve_preset_exclude_keywords）。
        self._preset_exclude_keywords: List[str] = resolve_preset_exclude_keywords(
            form.get("preset_exclude_keywords")
        )

        # ---- 提醒记录表排序状态（v3.2）----
        self._alert_sort_col: str = ""
        self._alert_sort_asc: bool = True

        # ---- 日志区字号（v3.2，可选调节）----
        self._log_font_size: int = 9

        # ---- 界面 ----
        self.root.title(WINDOW_TITLE)
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*MIN_WINDOW_SIZE)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_widgets(form)
        self._install_log_handler()
        self._load_history()

        self.root.after(POLL_INTERVAL_MS, self._poll_queue)
        self.root.after(1000, self._tick)
        self._append_log("INFO", f"[{datetime.now():%H:%M:%S}] 界面已就绪，配置文件：{self.config_path}")
    # ================================================================== #
    # 界面构建
    # ================================================================== #
    def _build_widgets(self, form: Dict[str, Any]) -> None:
        """构建全部控件。

        Args:
            form: 由 config_to_form 得到的初始表单状态。
        """
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        tab_config = ttk.Frame(notebook)
        tab_notify = ttk.Frame(notebook)
        tab_run = ttk.Frame(notebook)
        notebook.add(tab_config, text="  监控配置  ")
        notebook.add(tab_notify, text="  通知设置  ")
        notebook.add(tab_run, text="  运行监控  ")

        self._build_tab_config(tab_config, form)
        self._build_tab_notify(tab_notify, form)
        self._build_tab_run(tab_run)

    # ------------------------------------------------------------------ #
    def _build_tab_config(self, parent: "ttk.Frame", form: Dict[str, Any]) -> None:
        """构建「监控配置」标签页。"""
        # ---------------- 关键词表格 ----------------
        kw_frame = ttk.LabelFrame(parent, text="关键词与价格阈值（仅当商品价格 < 阈值时提醒）")
        kw_frame.pack(fill="both", expand=True, padx=10, pady=(10, 6))

        tree_wrap = ttk.Frame(kw_frame)
        tree_wrap.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.tree_keywords = ttk.Treeview(
            tree_wrap, columns=("keyword", "price", "status", "filters"), show="headings", height=8
        )
        self.tree_keywords.heading("keyword", text="关键词")
        self.tree_keywords.heading("price", text="价格阈值(元)")
        # v3.7：状态列（启用✅ / 停用⏸），停用行灰显
        self.tree_keywords.heading("status", text="状态")
        self.tree_keywords.heading("filters", text="排除 / 必含（v3.1）")
        self.tree_keywords.column("keyword", width=230, anchor="w")
        self.tree_keywords.column("price", width=90, anchor="e")
        self.tree_keywords.column("status", width=70, anchor="center")
        # v3.6：窗口加宽后让「filters」列自适应拉伸，充分利用剩余宽度
        self.tree_keywords.column("filters", width=300, anchor="w", stretch=True)
        self.tree_keywords.pack(side="left", fill="both", expand=True)
        self.tree_keywords.bind("<Double-1>", self._on_keyword_double_click)
        # v3.7：关键词行样式（启用 = 深色，停用 = 灰色）
        self.tree_keywords.tag_configure("enabled", foreground="#111827")
        self.tree_keywords.tag_configure("disabled", foreground="#9ca3af")

        kw_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree_keywords.yview)
        kw_scroll.pack(side="right", fill="y")
        self.tree_keywords.configure(yscrollcommand=kw_scroll.set)

        # 空状态引导（U1）：关键词表为空时显示占位提示
        self.var_kw_empty = tk.StringVar(value="")
        self.label_kw_empty = ttk.Label(
            kw_frame, textvariable=self.var_kw_empty, foreground="#888888", justify="left"
        )

        entry_row = ttk.Frame(kw_frame)
        entry_row.pack(fill="x", padx=8, pady=(0, 8))
        #: 关键词输入行（v3.6 布局冒烟会校验整排按钮在默认窗口内不溢出）
        self.entry_row = entry_row
        ttk.Label(entry_row, text="关键词：").pack(side="left")
        self.var_keyword = tk.StringVar(value="")
        # v3.7：输入框略收窄（16→14），为新增「⏸ 停用/启用」按钮腾出宽度，
        # 保证整排按钮在默认窗口（1020px）内不溢出（gui_smoke 有断言 ≤968）。
        ttk.Entry(entry_row, textvariable=self.var_keyword, width=14).pack(side="left", padx=(0, 8))
        ttk.Label(entry_row, text="价格阈值(元)：").pack(side="left")
        self.var_price = tk.StringVar(value="")
        ttk.Entry(entry_row, textvariable=self.var_price, width=8).pack(side="left", padx=(0, 8))
        # v3.6：原「添加 / 更新」二合一按钮拆分为两个独立按钮——
        # 「➕ 添加」只做新增（同名已存在时提示改用更新）；
        # 「✏️ 更新选中」只更新表格选中行（即使修改了关键词名也是更新原行，不会误判成新增）。
        ttk.Button(entry_row, text="➕ 添加", command=self.on_add_keyword).pack(side="left")
        ttk.Button(entry_row, text="✏️ 更新选中", command=self.on_update_keyword).pack(side="left", padx=4)
        # v3.7：启用/停用切换（停用 = 不抓取、不提醒，但保留配置与过滤规则）
        ttk.Button(entry_row, text="⏸ 停用/启用", command=self.on_toggle_keyword).pack(side="left", padx=4)
        ttk.Button(entry_row, text="删除选中", command=self.on_delete_keyword).pack(side="left", padx=4)
        ttk.Button(entry_row, text="编辑过滤词", command=self.on_edit_filters).pack(side="left")
        ttk.Button(
            entry_row, text="添加预置词", command=self.on_add_preset_excludes
        ).pack(side="left", padx=4)
        ttk.Button(
            entry_row, text="编辑预置词", command=self.on_edit_preset_excludes
        ).pack(side="left")

        for keyword, price in self._keywords:
            enabled = bool(self._keyword_enabled.get(keyword, True))
            item = self.tree_keywords.insert(
                "",
                "end",
                values=(keyword, f"{price:g}", keyword_status_text(enabled), self._filters_summary(keyword)),
            )
            _apply_row_style_if_available(self, item, keyword)

        # 表格填充完成后刷新空状态引导（避免启动时误显示占位文案）
        self._refresh_keyword_empty_hint()

        # ---------------- 监测设置 ----------------
        setting_frame = ttk.LabelFrame(parent, text="监测设置")
        setting_frame.pack(fill="x", padx=10, pady=6)

        row1 = ttk.Frame(setting_frame)
        row1.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(row1, text="监测间隔（秒）：").pack(side="left")
        self.var_interval = tk.StringVar(value=str(form["interval"]))
        ttk.Entry(row1, textvariable=self.var_interval, width=10).pack(side="left", padx=(0, 8))
        ttk.Label(
            row1, text="默认 600 秒（10 分钟），过短容易触发闲鱼风控", foreground="#888888"
        ).pack(side="left")

        row2 = ttk.Frame(setting_frame)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="抓取方式：").pack(side="left")
        self.var_fetcher = tk.StringVar(value=fetcher_label(form["fetcher_type"]))
        combo = ttk.Combobox(
            row2,
            textvariable=self.var_fetcher,
            values=[label for _value, label in FETCHER_CHOICES],
            state="readonly",
            width=46,
        )
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_first_use_guide())

        row_pages = ttk.Frame(setting_frame)
        row_pages.pack(fill="x", padx=8, pady=4)
        ttk.Label(row_pages, text="抓取页数（仅 mtop）：").pack(side="left")
        self.var_pages = tk.StringVar(value=str(form.get("pages", 1)))
        ttk.Entry(row_pages, textvariable=self.var_pages, width=6).pack(side="left", padx=(0, 8))
        ttk.Label(
            row_pages, text="翻页会增加请求频率，建议配合 600s+ 监测间隔使用", foreground="#888888"
        ).pack(side="left")

        row3 = ttk.Frame(setting_frame)
        row3.pack(fill="x", padx=8, pady=4)
        ttk.Label(row3, text="登录 Cookie：").pack(side="left")
        self.var_cookie_status = tk.StringVar(value="")
        self.label_cookie = ttk.Label(row3, textvariable=self.var_cookie_status)
        self.label_cookie.pack(side="left", padx=(0, 10))
        # v3.3：移除「获取 Cookie」按钮（自动登录入口取消）；
        # 手动步骤说明收进「Cookie 管理」对话框的「如何获取 Cookie？」帮助。
        ttk.Button(row3, text="Cookie 管理", command=self.on_manage_cookies).pack(
            side="left", padx=(8, 0)
        )

        # 首次使用引导（U2）：mtop + 无 Cookie 时显示获取步骤
        self.var_cookie_guide = tk.StringVar(value="")
        self.label_cookie_guide = ttk.Label(
            setting_frame,
            textvariable=self.var_cookie_guide,
            foreground="#2563eb",
            justify="left",
            wraplength=660,
        )
        self.label_cookie_guide.pack(fill="x", padx=8, pady=(0, 6))
        self._refresh_cookie_status()

        # ---------------- 保存 ----------------
        save_row = ttk.Frame(parent)
        save_row.pack(fill="x", padx=10, pady=(4, 12))
        ttk.Button(save_row, text="💾 保存配置", command=self.on_save_config).pack(side="left")
        ttk.Button(save_row, text="🖱 创建桌面快捷方式", command=self.on_create_shortcut).pack(
            side="left", padx=6
        )
        ttk.Button(save_row, text="ℹ 关于", command=self.on_show_about).pack(side="left", padx=6)
        ttk.Label(
            save_row,
            text=f"配置文件：{os.path.abspath(self.config_path)}",
            foreground="#888888",
        ).pack(side="left", padx=10)

    # ------------------------------------------------------------------ #
    def _build_tab_notify(self, parent: "ttk.Frame", form: Dict[str, Any]) -> None:
        """构建「通知设置」标签页。"""
        canvas_hint = ttk.Label(
            parent,
            text="勾选需要启用的通知方式并填写参数；只有「勾选且参数完整」的通道才会被保存。",
            foreground="#555555",
        )
        canvas_hint.pack(fill="x", padx=12, pady=(10, 4))

        self.var_channel_enabled: Dict[str, "tk.BooleanVar"] = {}
        self.var_channel_fields: Dict[str, Dict[str, "tk.StringVar"]] = {}

        channels_state: Dict[str, Dict[str, Any]] = form["channels"]
        for ctype in CHANNEL_ORDER:
            state = channels_state.get(ctype, {"enabled": False, "options": {}})
            frame = ttk.LabelFrame(parent, text=CHANNEL_LABELS[ctype])
            frame.pack(fill="x", padx=12, pady=5)

            head = ttk.Frame(frame)
            head.pack(fill="x", padx=8, pady=(6, 2))
            enabled_var = tk.BooleanVar(value=bool(state.get("enabled")))
            self.var_channel_enabled[ctype] = enabled_var
            ttk.Checkbutton(head, text="启用", variable=enabled_var).pack(side="left")
            ttk.Button(
                head,
                text="测试发送",
                command=lambda c=ctype: self.on_test_channel(c),
            ).pack(side="right")

            field_vars: Dict[str, "tk.StringVar"] = {}
            options = state.get("options") or {}
            for name, label, secret, default in CHANNEL_FIELDS[ctype]:
                row = ttk.Frame(frame)
                row.pack(fill="x", padx=8, pady=2)
                ttk.Label(row, text=f"{label}：", width=24, anchor="w").pack(side="left")
                var = tk.StringVar(value=str(options.get(name, default) or ""))
                entry = ttk.Entry(row, textvariable=var, width=46)
                if secret:
                    entry.configure(show="*")
                entry.pack(side="left", fill="x", expand=True)
                field_vars[name] = var
            self.var_channel_fields[ctype] = field_vars

            if ctype == "console":
                ttk.Label(
                    frame,
                    text="无需参数，提醒内容会直接显示在「运行监控」页的日志区。",
                    foreground="#888888",
                ).pack(anchor="w", padx=8, pady=(0, 6))

            if ctype == "webhook":
                # v3.3：通道明确为企业微信机器人，提示在企微群添加群机器人
                ttk.Label(
                    frame,
                    text="使用步骤：在企业微信群里「添加群机器人」→ 复制 Webhook 地址粘贴到上方。"
                    "消息将以文本卡片形式推送到该群。",
                    foreground="#888888",
                    wraplength=640,
                    justify="left",
                ).pack(anchor="w", padx=8, pady=(0, 6))

        ttk.Button(parent, text="💾 保存配置", command=self.on_save_config).pack(
            anchor="w", padx=12, pady=10
        )

    # ------------------------------------------------------------------ #
    def _build_tab_run(self, parent: "ttk.Frame") -> None:
        """构建「运行监控」标签页。"""
        # ---------------- 按钮栏 ----------------
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", padx=10, pady=(10, 4))
        self.btn_start = ttk.Button(toolbar, text="▶ 开始监控", command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(toolbar, text="■ 停止监控", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_once = ttk.Button(toolbar, text="⚡ 立即执行一轮", command=self.on_run_once)
        self.btn_once.pack(side="left", padx=6)
        ttk.Button(toolbar, text="🗑 清空去重记录", command=self.on_clear_records).pack(side="left", padx=6)
        # v3.6：临时黑名单——把选中提醒记录的商品人工剔除（不再提醒/不再进记录）
        ttk.Button(toolbar, text="🚫 加入黑名单", command=self.on_blacklist_selected).pack(
            side="left", padx=6
        )
        ttk.Button(toolbar, text="📋 黑名单管理", command=self.on_manage_blacklist).pack(
            side="left", padx=6
        )
        # v3.7：已售出/下架处理——手动标记 + 详情接口批量校验（限速）
        ttk.Button(toolbar, text="🗑 标记已售出", command=self.on_mark_sold_selected).pack(
            side="left", padx=6
        )
        ttk.Button(toolbar, text="🔍 校验在架", command=self.on_check_on_shelf).pack(side="left", padx=6)

        # ---------------- 状态栏 ----------------
        status = ttk.Frame(parent)
        status.pack(fill="x", padx=10, pady=4)
        self.var_status = tk.StringVar(value="状态：已停止")
        self.var_rounds = tk.StringVar(value="已执行轮数：0")
        self.var_alerts = tk.StringVar(value="累计提醒：0")
        self.var_countdown = tk.StringVar(value="下次执行：--:--")
        for var in (self.var_status, self.var_rounds, self.var_alerts, self.var_countdown):
            ttk.Label(status, textvariable=var, width=22, anchor="w").pack(side="left")

        # ---------------- 日志区 ----------------
        log_frame = ttk.LabelFrame(parent, text="运行日志")
        log_frame.pack(fill="both", expand=True, padx=10, pady=4)
        log_head = ttk.Frame(log_frame)
        log_head.pack(fill="x", padx=6, pady=(4, 0))
        # v3.2：日志区字号可调（A− / A+）与清空日志按钮
        ttk.Label(log_head, text="字号：", foreground="#888888").pack(side="left")
        ttk.Button(
            log_head, text="A−", width=3, command=lambda: self._adjust_log_font(-1)
        ).pack(side="left", padx=(0, 2))
        ttk.Button(
            log_head, text="A+", width=3, command=lambda: self._adjust_log_font(1)
        ).pack(side="left")
        # v3.3：日志「仅展示符合的低价」开关（默认勾选 = 只显示概况与命中明细；
        # 取消勾选时 monitor 会把每个关键词抓取到的商品明细逐条写入日志，含被过滤原因）
        self.var_log_detail_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            log_head,
            text="仅展示符合的低价",
            variable=self.var_log_detail_only,
        ).pack(side="left", padx=(10, 0))
        ttk.Button(log_head, text="🗑 清空日志", command=self.on_clear_log).pack(side="right")
        self.text_log = ScrolledText(log_frame, height=14, wrap="word", state="disabled")
        self.text_log.pack(fill="both", expand=True, padx=6, pady=6)
        self.text_log.tag_configure("INFO", foreground="#333333")
        self.text_log.tag_configure("DEBUG", foreground="#888888")
        self.text_log.tag_configure("WARNING", foreground="#d97706")
        self.text_log.tag_configure("ERROR", foreground="#dc2626")
        self.text_log.tag_configure("CRITICAL", foreground="#dc2626")
        self.text_log.tag_configure(
            "ALERT", foreground="#059669", font=("TkDefaultFont", self._log_font_size, "bold")
        )
        # v3.7：日志高亮自定义 tag（新商品/命中 → 蓝加粗；完成 → 绿；轮次 → 靛；弱化 → 灰）
        self.text_log.tag_configure(
            "NEW_ITEM", foreground="#2563eb", font=("TkDefaultFont", self._log_font_size, "bold")
        )
        self.text_log.tag_configure(
            "SUMMARY", foreground="#059669", font=("TkDefaultFont", self._log_font_size, "bold")
        )
        self.text_log.tag_configure(
            "ROUND", foreground="#6d28d9", font=("TkDefaultFont", self._log_font_size, "bold")
        )
        self.text_log.tag_configure("DIM", foreground="#9ca3af")
        self._apply_log_font()

        # ---------------- 提醒记录 ----------------
        alert_frame = ttk.LabelFrame(parent, text="提醒记录（双击某行用浏览器打开商品页；点击表头排序）")
        alert_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        # v3.7：已售出/下架开关（默认隐藏；勾选后显示并灰显「已下架」记录）
        alert_head = ttk.Frame(alert_frame)
        alert_head.pack(fill="x", padx=8, pady=(4, 0))
        self.var_show_sold = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            alert_head,
            text="显示已下架/已售出",
            variable=self.var_show_sold,
            command=self.on_toggle_show_sold,
        ).pack(side="left")
        ttk.Label(
            alert_head,
            text="已售出/下架的商品默认不显示；可手动「🗑 标记已售出」或「🔍 校验在架」自动判定。",
            foreground="#888888",
        ).pack(side="left", padx=(8, 0))
        wrap = ttk.Frame(alert_frame)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)

        columns = ALERT_COLUMNS
        self.tree_alerts = ttk.Treeview(wrap, columns=columns, show="headings", height=7)
        for key, width, anchor in (
            ("time", 140, "w"),
            ("keyword", 90, "w"),
            ("title", 340, "w"),
            ("price", 80, "e"),
            ("publish", 140, "w"),
        ):
            # v3.2：表头点击排序（再点反序）
            self.tree_alerts.heading(
                key, text=ALERT_HEADING_TEXTS[key], command=lambda k=key: self._on_alert_sort(k)
            )
            self.tree_alerts.column(key, width=width, anchor=anchor)
        self.tree_alerts.pack(side="left", fill="both", expand=True)
        self.tree_alerts.bind("<Double-1>", self._on_alert_double_click)
        # v3.7：已售出/下架记录灰显
        self.tree_alerts.tag_configure("sold", foreground="#9ca3af")

        alert_scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree_alerts.yview)
        alert_scroll.pack(side="right", fill="y")
        self.tree_alerts.configure(yscrollcommand=alert_scroll.set)

    # ================================================================== #
    # 日志
    # ================================================================== #
    def _install_log_handler(self) -> None:
        """把 `xianyu_alert` 及其子模块的日志接管到界面日志区。"""
        self.log_handler = QueueLogHandler(self.ui_queue, level=logging.INFO)
        self.log_handler.setFormatter(logging.Formatter("%(message)s"))
        package_logger = logging.getLogger("xianyu_alert")
        package_logger.setLevel(logging.INFO)
        package_logger.addHandler(self.log_handler)

    def _remove_log_handler(self) -> None:
        """移除日志 handler（关闭窗口时调用）。"""
        handler = getattr(self, "log_handler", None)
        if handler is not None:
            try:
                logging.getLogger("xianyu_alert").removeHandler(handler)
            except Exception:  # noqa: BLE001
                pass

    def _append_log(self, level: str, text: str) -> None:
        """把一行日志写入日志区（只能在主线程调用）。

        v3.2 起统一前置 `[HH:MM:SS]` 时间戳：若文本已以 `[HH:MM:SS]` 开头
        （例如来自 QueueLogHandler 或旧调用点手工拼的时间戳）则不再重复加，
        保证**每行恰好一个时间戳**，避免重复前缀。

        v3.7：先按文本前缀映射高亮 tag（`log_tag_for_text`）——🔔/新出现 →
        蓝色加粗、✅/本轮完成 → 绿色加粗、🚫/已停用 → 灰色、轮次分隔 → 靛色，
        让「新商品/低价命中」从全黑日志中凸显出来；monitor 侧只需在关键行
        打 emoji 前缀即可，无需改动日志管道。
        """
        widget = getattr(self, "text_log", None)
        if widget is None:
            return
        tag = log_tag_for_text(level, text)
        line = str(text or "")
        if not re.match(r"^\d{2}:\d{2}:\d{2}\]", line):
            line = f"[{datetime.now():%H:%M:%S}] {line}"
        widget.configure(state="normal")
        widget.insert("end", line + "\n", tag)
        # 裁剪过长日志
        try:
            line_count = int(widget.index("end-1c").split(".")[0])
            if line_count > MAX_LOG_LINES:
                widget.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        except (ValueError, tk.TclError):  # pragma: no cover - 防御性分支
            pass
        widget.configure(state="disabled")
        widget.see("end")

    def _apply_log_font(self) -> None:
        """把当前日志字号应用到日志区（v3.2，v3.7 扩展自定义 tag 字号）。"""
        widget = getattr(self, "text_log", None)
        if widget is None or tk is None:
            return
        try:
            widget.configure(font=("TkDefaultFont", self._log_font_size))
            widget.tag_configure(
                "ALERT",
                foreground="#059669",
                font=("TkDefaultFont", self._log_font_size, "bold"),
            )
            widget.tag_configure(
                "NEW_ITEM",
                foreground="#2563eb",
                font=("TkDefaultFont", self._log_font_size, "bold"),
            )
            widget.tag_configure(
                "SUMMARY",
                foreground="#059669",
                font=("TkDefaultFont", self._log_font_size, "bold"),
            )
            widget.tag_configure(
                "ROUND",
                foreground="#6d28d9",
                font=("TkDefaultFont", self._log_font_size, "bold"),
            )
            widget.tag_configure("DIM", foreground="#9ca3af")
        except tk.TclError:  # pragma: no cover - 窗口销毁等边缘情况
            pass

    def _adjust_log_font(self, delta: int) -> None:
        """调整日志区字号（v3.2，范围 8~16）。"""
        self._log_font_size = max(8, min(16, self._log_font_size + int(delta)))
        self._apply_log_font()

    def on_clear_log(self) -> None:
        """清空日志区（v3.2；日志非关键数据，直接清空不二次确认）。"""
        widget = getattr(self, "text_log", None)
        if widget is None:
            return
        try:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        except tk.TclError:  # pragma: no cover - 窗口销毁等边缘情况
            pass

    # ================================================================== #
    # 队列轮询（子线程 -> 主线程）
    # ================================================================== #
    def _push(self, kind: str, payload: Any) -> None:
        """子线程安全地投递一条 UI 消息。"""
        try:
            self.ui_queue.put_nowait((kind, payload))
        except Exception:  # noqa: BLE001 - 队列异常不应影响业务线程
            pass

    def _poll_queue(self) -> None:
        """主线程轮询队列并更新界面。

        v3.5 稳定性：关闭流程置位 `_closing` 后不再重新调度自身，
        避免窗口销毁后 `root.after` 回调残留（TclError / 进程不退出）。
        v3.5 挂机优化：队列为空（挂机）时把下一次轮询间隔降到
        `POLL_IDLE_INTERVAL_MS`（500ms），有消息时恢复 `POLL_INTERVAL_MS`（100ms）。
        v3.6 防卡优化：单次最多消费 `MAX_QUEUE_MESSAGES_PER_POLL` 条——
        日志洪峰（关闭「仅展示符合的低价」等）时剩余消息留到下一轮再消费，
        避免主线程被大量日志渲染拖住导致窗口无响应。
        """
        if getattr(self, "_closing", False):
            return
        had_message = False
        processed = 0
        try:
            while processed < MAX_QUEUE_MESSAGES_PER_POLL:
                kind, payload = self.ui_queue.get_nowait()
                had_message = True
                processed += 1
                try:
                    self._handle_ui_message(kind, payload)
                except Exception as exc:  # noqa: BLE001 - 单条消息失败不能中断轮询
                    logger.debug("处理 UI 消息 %s 失败：%s", kind, exc)
        except queue.Empty:
            pass
        finally:
            if not getattr(self, "_closing", False):
                delay = POLL_INTERVAL_MS if had_message else POLL_IDLE_INTERVAL_MS
                try:
                    self._poll_after_id = self.root.after(delay, self._poll_queue)
                except Exception:  # noqa: BLE001 - 窗口销毁等边缘情况
                    pass

    def _handle_ui_message(self, kind: str, payload: Any) -> None:
        """分发一条 UI 消息。

        Args:
            kind: 消息类型（log / alert / status / state / message / callable）。
            payload: 消息载荷。
        """
        if kind == "log":
            level, text = payload
            self._append_log(level, text)
        elif kind == "alert":
            self._insert_alert_row(payload, to_top=True)
        elif kind == "status":
            self.var_rounds.set(f"已执行轮数：{payload.get('rounds', 0)}")
            self.var_alerts.set(f"累计提醒：{payload.get('alerts', 0)}")
        elif kind == "state":
            self._set_running(bool(payload.get("running")))
        elif kind == "message":
            level, title, text = payload
            self._show_message(level, title, text)
        elif kind == "callable":
            payload()

    @staticmethod
    def _show_message(level: str, title: str, text: str) -> None:
        """弹出提示框。"""
        if level == "error":
            messagebox.showerror(title, text)
        elif level == "warning":
            messagebox.showwarning(title, text)
        else:
            messagebox.showinfo(title, text)

    def _push_message(self, level: str, title: str, text: str) -> None:
        """子线程安全地请求弹框。"""
        self._push("message", (level, title, text))

    def _tick(self) -> None:
        """每秒刷新倒计时。

        v3.5 稳定性：关闭流程置位 `_closing` 后不再重新调度自身。
        """
        if getattr(self, "_closing", False):
            return
        try:
            if self._running and self._next_run_at > 0:
                remain = self._next_run_at - time.monotonic()
                self.var_countdown.set(f"下次执行：{format_countdown(remain)}")
            elif self._running:
                self.var_countdown.set("下次执行：执行中…")
            else:
                self.var_countdown.set("下次执行：--:--")
        except Exception:  # noqa: BLE001 - 刷新失败不影响主流程
            pass
        finally:
            if not getattr(self, "_closing", False):
                try:
                    self._tick_after_id = self.root.after(1000, self._tick)
                except Exception:  # noqa: BLE001 - 窗口销毁等边缘情况
                    pass

    def _set_running(self, running: bool) -> None:
        """根据运行状态刷新按钮与状态文案。"""
        self._running = running
        if running:
            label = "运行中（循环）" if self._mode == "loop" else "运行中（单轮）"
            self.var_status.set(f"状态：{label}")
            self.btn_start.configure(state="disabled")
            self.btn_once.configure(state="disabled")
            self.btn_stop.configure(state="normal" if self._mode == "loop" else "disabled")
        else:
            self._mode = ""
            self._next_run_at = 0.0
            self.var_status.set("状态：已停止")
            self.btn_start.configure(state="normal")
            self.btn_once.configure(state="normal")
            self.btn_stop.configure(state="disabled")

    # ================================================================== #
    # 关键词表格
    # ================================================================== #
    def _refresh_keyword_empty_hint(self) -> None:
        """根据表格是否为空，显示/隐藏空状态引导文案（U1）。"""
        has_keywords = bool(self.tree_keywords.get_children())
        self.var_kw_empty.set(empty_state_hint(has_keywords))
        try:
            if has_keywords:
                self.label_kw_empty.pack_forget()
            else:
                self.label_kw_empty.pack(fill="x", padx=12, pady=(0, 2))
        except tk.TclError:  # pragma: no cover - 窗口销毁等边缘情况
            pass

    def _filters_summary(self, keyword: str) -> str:
        """返回某关键词过滤规则的表格摘要文案。"""
        return keyword_filter_summary(self._keyword_filters.get(str(keyword)))

    def _collect_keywords(self) -> List[Tuple[str, float]]:
        """从表格读取当前关键词列表（(关键词, 价格阈值)）。

        v3.7 兼容说明：本方法**保持返回 (keyword, price) 二元组不变**
        （既有测试 test_gui.py:517 断言该形状）；启用/停用状态由
        `_collect_keyword_rules` 提供（3 元组），`_collect_config_dict`
        走后者，保证 enabled 写入 config。
        """
        result: List[Tuple[str, float]] = []
        for item in self.tree_keywords.get_children():
            values = self.tree_keywords.item(item, "values")
            if not values or len(values) < 2:
                continue
            try:
                result.append((str(values[0]), float(values[1])))
            except (TypeError, ValueError):
                continue
        return result

    def _collect_keyword_rules(self) -> List[Tuple[str, float, bool]]:
        """从表格读取完整关键词规则（(关键词, 价格阈值, 是否启用)，v3.7）。

        启用状态以 `self._keyword_enabled` 为准（缺省 True），与表格「状态」
        列保持一致；停用的关键词仍会被收集，保存后写回 config 供 monitor 跳过。
        """
        rules: List[Tuple[str, float, bool]] = []
        for keyword, price in self._collect_keywords():
            enabled = parse_enabled_flag(_keyword_enabled_dict(self).get(str(keyword)), default=True)
            rules.append((keyword, price, enabled))
        return rules

    def _apply_keyword_row_style(self, item: str, keyword: str) -> None:
        """按启用状态刷新关键词行：状态列文案 + 停用行灰显（v3.7）。"""
        enabled = parse_enabled_flag(_keyword_enabled_dict(self).get(str(keyword)), default=True)
        try:
            values = list(self.tree_keywords.item(item, "values") or ())
            while len(values) < 3:
                values.append("")
            values[2] = keyword_status_text(enabled)
            self.tree_keywords.item(item, values=tuple(values))
            self.tree_keywords.item(item, tags=("enabled",) if enabled else ("disabled",))
        except Exception:  # noqa: BLE001 - FakeTree 等测试替身不支持 tags 时忽略
            pass

    def on_toggle_keyword(self) -> None:
        """切换选中关键词的启用/停用状态（v3.7）。

        停用 = 临时不监控（不抓取、不提醒），但保留配置与过滤规则，
        在表格中灰显，随时可再启用。切换只改内存态，点「💾 保存配置」后落盘。
        """
        selection = self.tree_keywords.selection()
        if not selection:
            messagebox.showinfo("提示", "请先在表格中选中要启用/停用的关键词。")
            return
        item = selection[0]
        values = self.tree_keywords.item(item, "values")
        if not values:
            return
        keyword = str(values[0])
        state = _keyword_enabled_dict(self)
        current = parse_enabled_flag(state.get(keyword), default=True)
        state[keyword] = not current
        _apply_row_style_if_available(self, item, keyword)
        action = "停用" if current else "启用"
        self._append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] 已{action}关键词「{keyword}」"
            + ("（停用期间不抓取、不提醒）" if not current else "（恢复监控）"),
        )

    def _default_filters(self, keyword: str) -> Dict[str, List[str]]:
        """返回某关键词的默认过滤规则（v3.3 新行为，v3.5 预置词可配置）。

        必含词**留空**（不再从主关键词自动提取，由用户自行在编辑弹窗填写）；
        排除词**自动预置**当前配置的预置排除词（`self._preset_exclude_keywords`，
        缺省回退 `DEFAULT_PRESET_EXCLUDE_KEYWORDS`，即 回收 / 置换 / 收购 / 高价回收 / 收），
        用户可在编辑弹窗中增删。
        """
        presets = getattr(self, "_preset_exclude_keywords", None)
        # 只有「属性缺失」（stub / 旧实例）才回退默认；
        # 显式空列表 [] 表示「关闭自动预置」，必须原样返回（BUG-1 修复：`not presets` → None 判断）。
        if presets is None:
            presets = DEFAULT_PRESET_EXCLUDE_KEYWORDS
        return {
            "exclude_keywords": normalize_keywords(presets),
            "required_keywords": [],
        }

    def _ensure_filters(self, keyword: str) -> None:
        """确保某关键词在 _keyword_filters 中有记录；缺失时按自动提取补默认值。"""
        key = str(keyword)
        if key not in self._keyword_filters:
            self._keyword_filters[key] = self._default_filters(key)

    def on_add_keyword(self) -> None:
        """添加一条新的关键词规则（v3.6：只做新增，不再隐式更新）。

        若表格中已存在同名关键词 → 提示「已存在，请用更新选中」，
        避免用户以为「添加成功」实则覆盖了旧行阈值。

        Returns:
            None。
        """
        try:
            keyword, price = validate_keyword_entry(self.var_keyword.get(), self.var_price.get())
        except ValueError as exc:
            messagebox.showwarning("输入有误", str(exc))
            return

        for item in self.tree_keywords.get_children():
            values = self.tree_keywords.item(item, "values")
            if values and str(values[0]) == keyword:
                messagebox.showinfo(
                    "已存在",
                    f"关键词「{keyword}」已存在。\n\n"
                    "如需修改，请先在表格中选中该行，再用「✏️ 更新选中」。",
                )
                return

        self._ensure_filters(keyword)
        # v3.7：新关键词默认启用；若曾经停用后又删除再添加，也重置为启用
        _keyword_enabled_dict(self)[keyword] = True
        item = self.tree_keywords.insert(
            "",
            "end",
            values=(keyword, f"{price:g}", keyword_status_text(True), self._filters_summary(keyword)),
        )
        _apply_row_style_if_available(self, item, keyword)
        self.var_keyword.set("")
        self.var_price.set("")
        self._refresh_keyword_empty_hint()
        self._append_log("INFO", f"[{datetime.now():%H:%M:%S}] 已添加关键词「{keyword}」，阈值 {price:g} 元")

    def on_update_keyword(self) -> None:
        """更新**表格选中行**的关键词规则（v3.6 新增独立按钮）。

        与旧「添加 / 更新」合并逻辑的关键区别：
            只对选中行做更新，**保留原行**（即使修改了关键词名，
            例如修正错别字「Swtich」→「Switch」，仍是更新该行而非新增）。
        未选中任何行时提示「请先选中要更新的行」。

        Returns:
            None。
        """
        selection = self.tree_keywords.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选中要更新的行（可双击行载入输入框后再修改）。")
            return
        try:
            keyword, price = validate_keyword_entry(self.var_keyword.get(), self.var_price.get())
        except ValueError as exc:
            messagebox.showwarning("输入有误", str(exc))
            return

        item = selection[0]
        old_values = self.tree_keywords.item(item, "values") or ()
        old_keyword = str(old_values[0]) if len(old_values) > 0 else ""

        # 改名时禁止与表格中其它行重名（避免出现两条相同关键词）
        if old_keyword != keyword:
            for other in self.tree_keywords.get_children():
                if other == item:
                    continue
                values = self.tree_keywords.item(other, "values")
                if values and str(values[0]) == keyword:
                    messagebox.showwarning(
                        "名称冲突",
                        f"关键词「{keyword}」已被其它行使用。\n\n"
                        "请换一个名称，或先删除/修改那一行。",
                    )
                    return
            # 过滤规则随行迁移：旧关键词 -> 新关键词（若无旧规则则走默认）
            if old_keyword and old_keyword in self._keyword_filters:
                self._keyword_filters[keyword] = self._keyword_filters.pop(old_keyword)
            # v3.7：启用状态随行迁移（停用的关键词改名后仍保持停用）
            enabled_state = _keyword_enabled_dict(self)
            if old_keyword and old_keyword in enabled_state:
                enabled_state[keyword] = enabled_state.pop(old_keyword)
            else:
                enabled_state[keyword] = True

        self._ensure_filters(keyword)
        self._refresh_keyword_item(item, keyword, price)
        _apply_row_style_if_available(self, item, keyword)
        self.var_keyword.set("")
        self.var_price.set("")
        self._refresh_keyword_empty_hint()
        if old_keyword == keyword:
            self._append_log(
                "INFO",
                f"[{datetime.now():%H:%M:%S}] 已更新关键词「{keyword}」阈值为 {price:g} 元",
            )
        else:
            self._append_log(
                "INFO",
                f"[{datetime.now():%H:%M:%S}] 已更新选中行：关键词「{old_keyword}」→"
                f"「{keyword}」，阈值 {price:g} 元",
            )

    def _refresh_keyword_item(self, item: str, keyword: str, price: float) -> None:
        """按 iid 刷新表格中某行（v3.6；改名后不能按关键词名查找，必须按 iid）。"""
        enabled = parse_enabled_flag(_keyword_enabled_dict(self).get(str(keyword)), default=True)
        self.tree_keywords.item(
            item,
            values=(keyword, f"{price:g}", keyword_status_text(enabled), self._filters_summary(keyword)),
        )
        _apply_row_style_if_available(self, item, keyword)

    def on_delete_keyword(self) -> None:
        """删除选中的关键词。"""
        selection = self.tree_keywords.selection()
        if not selection:
            messagebox.showinfo("提示", "请先在表格中选中要删除的关键词。")
            return
        for item in selection:
            values = self.tree_keywords.item(item, "values")
            if values:
                self._keyword_filters.pop(str(values[0]), None)
                # v3.7：清理启用状态
                _keyword_enabled_dict(self).pop(str(values[0]), None)
            self.tree_keywords.delete(item)
        self._refresh_keyword_empty_hint()

    def _on_keyword_double_click(self, _event: Any) -> None:
        """双击表格行：把该行内容载入输入框以便修改。"""
        selection = self.tree_keywords.selection()
        if not selection:
            return
        values = self.tree_keywords.item(selection[0], "values")
        if values and len(values) >= 2:
            self.var_keyword.set(str(values[0]))
            self.var_price.set(str(values[1]))

    # ------------------------------------------------------------------ #
    # 过滤规则编辑（v3.1）：排除词 / 必含词
    # ------------------------------------------------------------------ #
    def on_edit_filters(self) -> None:
        """编辑选中关键词的排除词 / 必含词（弹窗，每行一个关键词）。"""
        selection = self.tree_keywords.selection()
        if not selection:
            messagebox.showinfo("提示", "请先在表格中选中要编辑的关键词。")
            return
        values = self.tree_keywords.item(selection[0], "values")
        if not values:
            return
        self._open_filter_dialog(str(values[0]))

    def on_add_preset_excludes(self) -> None:
        """为选中关键词一次性追加当前配置的预置排除词（v3.5 起可定制）。"""
        selection = self.tree_keywords.selection()
        if not selection:
            messagebox.showinfo("提示", "请先在表格中选中要添加预置排除词的关键词。")
            return
        values = self.tree_keywords.item(selection[0], "values")
        if not values:
            return
        keyword = str(values[0])
        presets = list(self._preset_exclude_keywords)
        self._ensure_filters(keyword)
        state = self._keyword_filters[keyword]
        state["exclude_keywords"] = add_preset_excludes(
            state.get("exclude_keywords"), preset=presets
        )
        self._refresh_keyword_row(keyword)
        self._append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] 已为「{keyword}」添加预置排除词："
            + "、".join(presets),
        )

    def _refresh_keyword_row(self, keyword: str) -> None:
        """刷新表格中某关键词行的摘要列（过滤规则变化后调用）。"""
        for item in self.tree_keywords.get_children():
            values = self.tree_keywords.item(item, "values")
            if values and str(values[0]) == str(keyword):
                enabled = parse_enabled_flag(_keyword_enabled_dict(self).get(str(keyword)), default=True)
                self.tree_keywords.item(
                    item,
                    values=(str(values[0]), str(values[1]), keyword_status_text(enabled), self._filters_summary(keyword)),
                )
                _apply_row_style_if_available(self, item, str(keyword))
                return

    def _open_filter_dialog(self, keyword: str) -> None:
        """打开过滤规则编辑对话框（多行文本，每行一个关键词）。"""
        self._ensure_filters(keyword)
        state = self._keyword_filters[keyword]

        dialog = tk.Toplevel(self.root)
        dialog.title(f"编辑过滤规则 - {keyword}")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame, text="排除关键词（标题命中任一即跳过，每行一个；留空 = 不排除）："
        ).pack(anchor="w")
        text_exclude = tk.Text(frame, width=46, height=5)
        text_exclude.pack(fill="x", pady=(2, 6))
        for token in normalize_keywords(state.get("exclude_keywords")):
            text_exclude.insert("end", token + "\n")

        ttk.Label(
            frame,
            text="必含词（标题必须包含全部，每行一个；留空 = 不强制要求）：",
        ).pack(anchor="w")
        text_required = tk.Text(frame, width=46, height=5)
        text_required.pack(fill="x", pady=(2, 6))
        for token in normalize_keywords(state.get("required_keywords")):
            text_required.insert("end", token + "\n")

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x", pady=(2, 0))
        ttk.Button(
            btn_row,
            text="添加预置排除词",
            command=lambda: self._dialog_add_preset(text_exclude),
        ).pack(side="left")

        def on_save() -> None:
            self._keyword_filters[keyword] = apply_filter_edit(
                self._keyword_filters.get(keyword),
                text_exclude.get("1.0", "end"),
                text_required.get("1.0", "end"),
            )
            self._refresh_keyword_row(keyword)
            self._append_log(
                "INFO",
                f"[{datetime.now():%H:%M:%S}] 已更新「{keyword}」过滤规则："
                f"{keyword_filter_summary(self._keyword_filters[keyword])}",
            )
            dialog.destroy()

        def on_cancel() -> None:
            dialog.destroy()

        ttk.Button(btn_row, text="保存", command=on_save).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="取消", command=on_cancel).pack(side="right")

        dialog.update_idletasks()
        try:
            dialog.grab_set()
        except tk.TclError:  # pragma: no cover - 窗口销毁等边缘情况
            pass
        text_exclude.focus_set()

    def _dialog_add_preset(self, text_widget: "tk.Text") -> None:
        """把当前预置排除词追加到对话框的排除词文本框（去重保序）。"""
        existing = parse_keyword_lines(text_widget.get("1.0", "end"))
        merged = add_preset_excludes(existing, preset=self._preset_exclude_keywords)
        text_widget.delete("1.0", "end")
        for token in merged:
            text_widget.insert("end", token + "\n")

    # ------------------------------------------------------------------ #
    # 预置排除词编辑（v3.5）：可配置、可持久化
    # ------------------------------------------------------------------ #
    def _apply_preset_edit(self, text: Any) -> List[str]:
        """应用预置排除词编辑结果（多行文本，每行一个）。

        更新内存态 `self._preset_exclude_keywords`，并同步写回
        `self._raw_config["preset_exclude_keywords"]`（供后续保存配置落盘）。

        Args:
            text: 弹窗中的多行文本。

        Returns:
            规范化后的预置排除词列表（去空去重保序）。
        """
        presets = parse_keyword_lines(text)
        self._preset_exclude_keywords = presets
        self._raw_config["preset_exclude_keywords"] = list(presets)
        return presets

    def on_edit_preset_excludes(self) -> None:
        """弹出「编辑预置排除词」对话框（v3.5）。

        每行一个预置词；「保存」后立即更新内存态并写回 config（持久化），
        后续「添加新关键词」与「添加预置排除词」都会使用这份定制列表。
        """
        dialog = tk.Toplevel(self.root)
        dialog.title("编辑预置排除词（v3.5）")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="预置排除词（每行一个；添加新关键词 / 点「添加预置排除词」时自动带上）：",
            wraplength=420,
            justify="left",
        ).pack(anchor="w")
        text = tk.Text(frame, width=40, height=8)
        text.pack(fill="x", pady=(4, 6))
        for token in normalize_keywords(self._preset_exclude_keywords):
            text.insert("end", token + "\n")

        ttk.Label(
            frame,
            text="提示：只影响之后添加/追加的排除词；已有关键词的排除词请用「编辑排除/必含词」单独调整。",
            foreground="#888888",
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x")

        def on_save() -> None:
            presets = self._apply_preset_edit(text.get("1.0", "end"))
            try:
                save_raw_config(self.config_path, self._raw_config)
            except OSError as exc:
                messagebox.showerror("保存失败", f"写入 {self.config_path} 失败：{exc}")
                return
            self._append_log(
                "INFO",
                f"[{datetime.now():%H:%M:%S}] 已更新预置排除词："
                + ("、".join(presets) if presets else "（空）"),
            )
            dialog.destroy()

        def on_cancel() -> None:
            dialog.destroy()

        ttk.Button(btn_row, text="保存", command=on_save).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="取消", command=on_cancel).pack(side="right")

        dialog.update_idletasks()
        try:
            dialog.grab_set()
        except tk.TclError:  # pragma: no cover - 窗口销毁等边缘情况
            pass
        text.focus_set()

    # ================================================================== #
    # 配置保存
    # ================================================================== #
    def _collect_channels(self) -> Dict[str, Dict[str, Any]]:
        """从界面读取通知通道状态。"""
        channels: Dict[str, Dict[str, Any]] = {}
        for ctype in CHANNEL_ORDER:
            options = {
                name: var.get() for name, var in self.var_channel_fields.get(ctype, {}).items()
            }
            channels[ctype] = {
                "enabled": bool(self.var_channel_enabled[ctype].get()),
                "options": options,
            }
        return channels

    def _collect_config_dict(self) -> Dict[str, Any]:
        """从界面收集完整配置字典。

        多 Cookie（v3.2）：`mtop` 未配置 Cookie 时**不再拦截保存**——
        允许保存，保存成功后由 `on_save_config` 给出 warning 提示
        （首次使用默认就是 mtop，不应卡住首用）；若 Cookie 池已配置
        启用条目，则 mtop 可通过池轮换正常工作。

        Returns:
            配置字典。

        Raises:
            ValueError: 界面输入非法（关键词为空、间隔非法等）。
        """
        keywords = self._collect_keywords()
        if not keywords:
            raise ValueError("请至少添加一个关键词。")
        interval = validate_interval(self.var_interval.get())
        pages = validate_pages(self.var_pages.get())
        ftype = fetcher_type_from_label(self.var_fetcher.get())
        return build_config_dict(
            keywords=keywords,
            interval_seconds=interval,
            fetcher_type=ftype,
            cookies=self._cookies,
            storage_path=self._storage_path,
            channels=self._collect_channels(),
            base=self._raw_config,
            pages=pages,
            encrypt_cookies=bool(self._cookies),
            keyword_filters=self._keyword_filters,
            cookie_pool=self._cookie_pool,
            preset_exclude_keywords=self._preset_exclude_keywords,
            # v3.7：把关键词启用/停用状态一并写入 config（停用不删除）
            keyword_enabled=_keyword_enabled_dict(self),
        )

    def _build_config_object(self) -> Config:
        """从界面收集配置并构造校验通过的 Config 对象。

        Returns:
            Config 实例。

        Raises:
            ValueError: 界面输入非法。
            ConfigError: 组装出的配置未通过校验。
        """
        return config_from_dict(self._collect_config_dict())

    def on_save_config(self) -> None:
        """保存配置到 config.yaml。

        v3.2：监控配置页与通知设置页的「保存配置」按钮共用本方法，
        保存时同时收集**两页**状态（`_collect_channels` 参与组装），
        因此从任意一页点保存都会把另一页的改动一并落盘。

        mtop 未配置 Cookie 时**不拦截保存**，仅保存成功后弹 warning
        提示（首次使用默认即为 mtop，不应卡住首用）。
        """
        try:
            data = self._collect_config_dict()
            config_from_dict(data)  # 保存前先校验，避免写出跑不起来的配置
        except (ValueError, ConfigError) as exc:
            messagebox.showwarning("配置有误", str(exc))
            return

        try:
            save_raw_config(self.config_path, data)
        except OSError as exc:
            messagebox.showerror("保存失败", f"写入 {self.config_path} 失败：{exc}")
            return

        self._raw_config = data
        self._storage_path = data["storage"]["path"]
        enabled = [c["type"] for c in data["notify"]["channels"]]
        self._append_log(
            "INFO",
            f"配置已保存到 {self.config_path}，启用通知通道：{', '.join(enabled)}",
        )
        messagebox.showinfo(
            "保存成功",
            f"配置已写入：\n{os.path.abspath(self.config_path)}\n\n启用的通知通道：{', '.join(enabled)}",
        )

        # v3.2：mtop 且既无单值 Cookie 也无 Cookie 池启用条目 → warning（不阻断）
        ftype = data.get("fetcher", {}).get("type", "")
        pool_has_enabled = any(
            item.get("enabled") and str(item.get("cookie") or "").strip()
            for item in (data.get("monitor", {}).get("cookie_pool") or [])
        )
        if ftype == "mtop" and not str(data.get("monitor", {}).get("cookies") or "") and not pool_has_enabled:
            self._append_log(
                "WARNING",
                "已保存，但 mtop 未配置任何 Cookie（单值或 Cookie 池均为空），"
                "真实抓取将失败。请点击「Cookie 管理」查看手动获取步骤并补充登录态。",
            )
            messagebox.showwarning(
                "Cookie 未配置",
                "配置已保存，但当前选择的是 mtop 真实抓取，\n"
                "尚未配置任何登录 Cookie（单值或 Cookie 池均为空），\n"
                "开始监控后真实抓取将失败。\n\n"
                "请点击「Cookie 管理」→「如何获取 Cookie？」按手动步骤补充登录态。",
            )

    # ================================================================== #
    # Cookie
    # ================================================================== #
    def _refresh_cookie_status(self) -> None:
        """刷新 Cookie 状态灯（六态）与首次使用引导。"""
        if getattr(self, "_cookies_undecryptable", False):
            state, text = COOKIE_STATE_UNDECRYPTABLE, "❌ Cookie 无法解密（可能换机/换用户），请重新登录"
        else:
            state, text = cookie_status(self._cookies)
        self.var_cookie_status.set(text)
        color = {
            COOKIE_STATE_OK: "#059669",
            COOKIE_STATE_EXPIRING: "#d97706",
            COOKIE_STATE_NO_TOKEN: "#d97706",
            COOKIE_STATE_EXPIRED: "#dc2626",
            COOKIE_STATE_MISSING: "#dc2626",
            COOKIE_STATE_UNDECRYPTABLE: "#dc2626",
        }.get(state, "#333333")
        try:
            self.label_cookie.configure(foreground=color)
        except tk.TclError:  # pragma: no cover - 主题不支持时忽略
            pass
        self._refresh_first_use_guide()

    def _refresh_first_use_guide(self) -> None:
        """根据抓取方式与 Cookie 状态刷新首次使用引导（U2）。"""
        ftype = fetcher_type_from_label(self.var_fetcher.get())
        state, _text = cookie_status(self._cookies)
        self.var_cookie_guide.set(first_use_guide(ftype, state))

    def on_show_about(self) -> None:
        """弹出「关于 / 使用说明 + 更新日志」对话框（v3.2 升级为可滚动全文）。"""
        dialog = tk.Toplevel(self.root)
        dialog.title(f"关于 - 闲鱼低价提醒工具 v{__version__}")
        dialog.geometry("560x520")
        dialog.transient(self.root)
        dialog.resizable(True, True)

        text = ScrolledText(dialog, wrap="word", state="disabled", padx=12, pady=12)
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.configure(state="normal")
        text.insert("1.0", about_full_text())
        # 更新日志标题（`## ` 开头行）加粗高亮
        text.tag_configure("h2", font=("TkDefaultFont", 11, "bold"), foreground="#1f2937")
        for index in range(1, int(text.index("end-1c").split(".")[0]) + 1):
            line_start = f"{index}.0"
            if text.get(line_start, f"{index}.0 lineend").startswith("## "):
                text.tag_add("h2", line_start, f"{index}.0 lineend")
        text.configure(state="disabled")
        text.focus_set()

        ttk.Button(dialog, text="关闭", command=dialog.destroy).pack(pady=(0, 10))

    def on_create_shortcut(self) -> None:
        """后台线程创建桌面快捷方式，成功/失败弹框提示。"""
        self._append_log("INFO", f"[{datetime.now():%H:%M:%S}] 正在创建桌面快捷方式…")

        def worker() -> None:
            """子线程：调用 shortcut 模块创建快捷方式。"""
            try:
                result = create_shortcut()
            except Exception as exc:  # noqa: BLE001 - 任何异常都不能崩窗
                logger.warning("创建桌面快捷方式异常：%s", exc)
                result = None
            if result:
                self._push_message("info", "创建成功", f"已在桌面创建快捷方式：\n{result}")
                self._push("log", ("INFO", f"[{datetime.now():%H:%M:%S}] 桌面快捷方式已创建：{result}"))
            else:
                self._push_message("error", "创建失败", "创建桌面快捷方式失败，请查看运行日志。")
                self._push("log", ("ERROR", f"[{datetime.now():%H:%M:%S}] 创建桌面快捷方式失败"))

        threading.Thread(target=worker, daemon=True, name="shortcut").start()

    # v3.3：已移除「获取 Cookie」对话框（on_get_cookie）。
    # 自动登录入口取消；手动获取步骤说明收进「Cookie 管理」对话框的
    # 「❓ 如何获取 Cookie？」帮助（见 on_manage_cookies）。
    # cookie.py 的 acquire_via_playwright / PlaywrightUnavailable / LoginTimeout
    # 仍被 `python -m xianyu_alert.cli login` 使用，故保留在 cookie.py 中不删。

    # ================================================================== #
    # Cookie 管理（v3.2 多账号 Cookie 池）
    # ================================================================== #
    @staticmethod
    def _cookie_health_label(state: str) -> Tuple[str, str]:
        """把 `detect_cookie_health` 状态码映射为（状态灯文案, 颜色）。"""
        mapping = {
            "ok": ("✅ 有效", "#059669"),
            "expiring": ("⚠️ 即将过期", "#d97706"),
            "expired": ("❌ 已过期", "#dc2626"),
            "no_token": ("⚠️ 缺 _m_h5_tk", "#d97706"),
            "missing": ("⚠️ 未配置", "#d97706"),
            "invalid_encrypt": ("❌ 无法解密", "#dc2626"),
        }
        return mapping.get(state, ("❓ 未知", "#6b7280"))

    def on_manage_cookies(self) -> None:
        """弹出「Cookie 管理」对话框：多账号 Cookie 池的增删 / 启停 / 检测 / 设默认。

        打开时自动检测全部 Cookie 的有效性；所有修改写入 `self._cookie_pool`
        （内存态），点主界面「💾 保存配置」后落盘（密文）。
        """
        dialog = tk.Toplevel(self.root)
        dialog.title("Cookie 管理（多账号轮换）")
        dialog.geometry("760x440")
        dialog.transient(self.root)
        dialog.resizable(True, True)

        wrap = ttk.Frame(dialog)
        wrap.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        columns = ("name", "status", "health", "expire")
        tree = ttk.Treeview(wrap, columns=columns, show="headings", height=10)
        for key, text, width, anchor in (
            ("name", "名称", 120, "w"),
            ("status", "状态", 60, "center"),
            ("health", "有效性", 220, "w"),
            ("expire", "过期时间", 180, "w"),
        ):
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor=anchor)
        tree.pack(side="left", fill="both", expand=True)
        tree.bind("<Double-1>", lambda _e: _on_edit_selected())

        scroll = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)

        hint = ttk.Label(
            dialog,
            text="池中启用的 Cookie 会按轮次轮换取用（分摊风控）；池为空时回退「获取 Cookie」保存的单值。",
            foreground="#555555",
            wraplength=720,
            justify="left",
        )
        hint.pack(fill="x", padx=10, pady=(0, 4))

        btn_row = ttk.Frame(dialog)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_row, text="➕ 添加", command=lambda: _on_add()).pack(side="left")
        ttk.Button(btn_row, text="✏ 编辑选中", command=lambda: _on_edit_selected()).pack(
            side="left", padx=6
        )
        ttk.Button(btn_row, text="🗑 删除选中", command=lambda: _on_delete()).pack(side="left", padx=6)
        ttk.Button(btn_row, text="⏻ 启用/停用", command=lambda: _on_toggle()).pack(side="left", padx=6)
        ttk.Button(btn_row, text="🔍 检测全部", command=lambda: _refresh()).pack(side="left", padx=6)
        ttk.Button(btn_row, text="⭐ 设为默认", command=lambda: _on_set_default()).pack(side="left", padx=6)
        # v3.3：手动获取 Cookie 步骤说明（原「获取 Cookie」按钮移除后的保留入口）。
        # 注意：必须用 lambda 延迟求值 —— `_on_cookie_help` 等嵌套函数在本函数
        # 后部才定义；若此处直接 `command=_on_cookie_help` 会在对话框创建阶段
        # 抛 UnboundLocalError 并中断整个函数，导致「添加」等按钮回调永不定义
        # （点击无反应，v3.4 已修复）。
        ttk.Button(btn_row, text="❓ 如何获取 Cookie？", command=lambda: _on_cookie_help()).pack(
            side="left", padx=6
        )
        ttk.Button(btn_row, text="关闭", command=dialog.destroy).pack(side="right")

        def _fmt_expire(cookie_str: str) -> str:
            """格式化过期时间；无时间戳 / 无法解析时返回占位文案。"""
            from .cookie import cookie_token_timestamp, TOKEN_TTL_MS

            raw = str(cookie_str or "").strip()
            ts = cookie_token_timestamp(raw)
            if ts is None:
                return "未知"
            expire_ms = ts + TOKEN_TTL_MS
            return datetime.fromtimestamp(expire_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

        def _refresh() -> None:
            """重绘列表（对每条 Cookie 检测有效性）。"""
            from .cookie import detect_cookie_health

            tree.delete(*tree.get_children())
            for item in self._cookie_pool:
                name = str(item.get("name") or "（未命名）")
                enabled = bool(item.get("enabled", True))
                cookie = str(item.get("cookie") or "")
                state, reason = detect_cookie_health(cookie)
                label, _color = self._cookie_health_label(state)
                status_text = "启用" if enabled else "停用"
                expire = _fmt_expire(cookie) if enabled and cookie else "—"
                tree.insert(
                    "", "end",
                    values=(name, status_text, f"{label} {reason}", expire),
                )
            _recolor()

        def _recolor() -> None:
            """给每行应用启用/停用样式（简化：整行灰显停用项）。"""
            tree.tag_configure("enabled", foreground="#111827")
            tree.tag_configure("disabled", foreground="#9ca3af")
            for iid in tree.get_children():
                values = tree.item(iid, "values") or []
                status_text = values[1] if len(values) > 1 else ""
                tree.item(iid, tags=("disabled",) if status_text == "停用" else ("enabled",))

        def _selected_index() -> Optional[int]:
            """返回选中行对应的 `_cookie_pool` 下标。"""
            selection = tree.selection()
            if not selection:
                return None
            children = tree.get_children()
            try:
                return children.index(selection[0])
            except ValueError:
                return None

        def _on_add() -> None:
            """弹出添加对话框：命名 + 粘贴 Cookie。"""
            add_dialog = tk.Toplevel(dialog)
            add_dialog.title("添加 Cookie")
            add_dialog.geometry("560x240")
            add_dialog.transient(dialog)
            add_dialog.resizable(False, False)

            frame = ttk.Frame(add_dialog, padding=12)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="名称（如「主账号」「小号1」，仅用于区分）：").pack(anchor="w")
            var_name = tk.StringVar()
            ttk.Entry(frame, textvariable=var_name, width=40).pack(fill="x", pady=(2, 6))
            ttk.Label(frame, text="Cookie 请求头（须包含 _m_h5_tk=）：").pack(anchor="w")
            text_cookie = tk.Text(frame, height=5, wrap="char")
            text_cookie.pack(fill="both", expand=True, pady=(2, 6))

            def on_save() -> None:
                name = var_name.get().strip()
                cookie = text_cookie.get("1.0", "end").strip()
                if not name:
                    messagebox.showwarning("名称为空", "请填写一个名称标识该账号。", parent=add_dialog)
                    return
                if not cookie:
                    messagebox.showwarning("Cookie 为空", "请粘贴 Cookie 内容。", parent=add_dialog)
                    return
                from .cookie import cookie_has_token

                if not cookie_has_token(cookie):
                    proceed = messagebox.askyesno(
                        "缺少关键 Cookie",
                        "粘贴的内容中未发现 _m_h5_tk=，mtop 抓取很可能失败。\n\n仍然添加吗？",
                        parent=add_dialog,
                    )
                    if not proceed:
                        return
                self._cookie_pool.append({"name": name, "cookie": cookie, "enabled": True})
                _refresh()
                add_dialog.destroy()

            ttk.Button(frame, text="保存", command=on_save).pack(side="right")
            ttk.Button(frame, text="取消", command=add_dialog.destroy).pack(side="right", padx=(0, 6))

        def _on_edit_selected() -> None:
            """编辑选中条目的名称 / Cookie。"""
            index = _selected_index()
            if index is None:
                messagebox.showinfo("提示", "请先在表格中选中要编辑的条目。", parent=dialog)
                return
            item = self._cookie_pool[index]

            edit_dialog = tk.Toplevel(dialog)
            edit_dialog.title(f"编辑 Cookie - {item.get('name', '')}")
            edit_dialog.geometry("560x240")
            edit_dialog.transient(dialog)
            edit_dialog.resizable(False, False)

            frame = ttk.Frame(edit_dialog, padding=12)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="名称：").pack(anchor="w")
            var_name = tk.StringVar(value=str(item.get("name", "")))
            ttk.Entry(frame, textvariable=var_name, width=40).pack(fill="x", pady=(2, 6))
            ttk.Label(frame, text="Cookie 请求头：").pack(anchor="w")
            text_cookie = tk.Text(frame, height=5, wrap="char")
            text_cookie.insert("1.0", str(item.get("cookie", "")))
            text_cookie.pack(fill="both", expand=True, pady=(2, 6))

            def on_save() -> None:
                name = var_name.get().strip()
                cookie = text_cookie.get("1.0", "end").strip()
                if not name:
                    messagebox.showwarning("名称为空", "请填写一个名称标识该账号。", parent=edit_dialog)
                    return
                if not cookie:
                    messagebox.showwarning("Cookie 为空", "请粘贴 Cookie 内容。", parent=edit_dialog)
                    return
                item["name"] = name
                item["cookie"] = cookie
                _refresh()
                edit_dialog.destroy()

            ttk.Button(frame, text="保存", command=on_save).pack(side="right")
            ttk.Button(frame, text="取消", command=edit_dialog.destroy).pack(side="right", padx=(0, 6))

        def _on_delete() -> None:
            """删除选中的 Cookie 条目。"""
            index = _selected_index()
            if index is None:
                messagebox.showinfo("提示", "请先在表格中选中要删除的条目。", parent=dialog)
                return
            name = self._cookie_pool[index].get("name", "")
            if not messagebox.askyesno("确认删除", f"确定删除 Cookie「{name}」吗？", parent=dialog):
                return
            self._cookie_pool.pop(index)
            _refresh()

        def _on_toggle() -> None:
            """切换选中条目的启用 / 停用状态。"""
            index = _selected_index()
            if index is None:
                messagebox.showinfo("提示", "请先在表格中选中要切换的条目。", parent=dialog)
                return
            item = self._cookie_pool[index]
            item["enabled"] = not bool(item.get("enabled", True))
            _refresh()

        def _on_set_default() -> None:
            """把选中条目设为默认：写入单值 monitor.cookies（立即落盘）。"""
            index = _selected_index()
            if index is None:
                messagebox.showinfo("提示", "请先在表格中选中要设为默认的条目。", parent=dialog)
                return
            item = self._cookie_pool[index]
            cookie = str(item.get("cookie") or "")
            if not cookie:
                messagebox.showwarning("内容为空", "该条目没有可用的 Cookie 内容。", parent=dialog)
                return
            self._cookies = cookie
            self._cookies_undecryptable = False
            self._refresh_cookie_status()
            monitor = self._raw_config.get("monitor")
            monitor = dict(monitor) if isinstance(monitor, dict) else {}
            cipher = secure.encrypt_text(cookie)
            if secure.is_encrypted(cipher):
                monitor["cookies"] = cipher
                monitor["cookies_encrypted"] = True
            else:
                monitor["cookies"] = cookie
                monitor.pop("cookies_encrypted", None)
            self._raw_config["monitor"] = monitor
            try:
                save_raw_config(self.config_path, self._raw_config)
                saved = True
            except OSError as exc:
                saved = False
                logger.warning("设为默认 Cookie 写入失败：%s", exc)
            self._append_log(
                "INFO",
                f"已把 Cookie「{item.get('name', '')}」设为默认（脱敏："
                f"{secure.mask_cookie(cookie) or '（空）'}，写入配置文件：{'成功' if saved else '失败'}）",
            )
            messagebox.showinfo(
                "已设为默认",
                f"「{item.get('name', '')}」已写入单值 Cookie（monitor.cookies）。\n"
                "点击主界面「💾 保存配置」可连同其它改动一并落盘。",
                parent=dialog,
            )

        def _on_cookie_help() -> None:
            """v3.3：展示手动获取 Cookie 的步骤说明（原「获取 Cookie」按钮的保留入口）。"""
            messagebox.showinfo(
                "如何获取 Cookie？（手动步骤）",
                COOKIE_MANUAL_HELP,
                parent=dialog,
            )

        _refresh()

    # ================================================================== #
    # 通知测试
    # ================================================================== #
    def on_test_channel(self, ctype: str) -> None:
        """测试某个通知通道（后台线程执行，不卡 UI）。

        Args:
            ctype: 通道类型。
        """
        raw_options = {name: var.get() for name, var in self.var_channel_fields.get(ctype, {}).items()}
        options = normalize_channel_options(ctype, raw_options)
        if not channel_is_complete(ctype, options):
            missing = [
                field_name
                for field_name in CHANNEL_REQUIRED_FIELDS.get(ctype, ())
                if not str(options.get(field_name, "") or "").strip()
            ]
            messagebox.showwarning(
                "参数不完整",
                f"通道「{CHANNEL_LABELS.get(ctype, ctype)}」缺少必填参数：{', '.join(missing)}",
            )
            return

        notifier = build_notifier(NotifyChannel(type=ctype, options=options))
        if notifier is None:
            messagebox.showerror("构造失败", f"无法构造通道 {ctype}，请检查参数。")
            return

        product = make_sample_product()

        def worker() -> None:
            """子线程：真正发送测试消息。"""
            try:
                notifier.notify([product])
            except Exception as exc:  # noqa: BLE001 - 网络类异常一律弹框告知
                self._push_message(
                    "error", "测试失败", f"通道「{CHANNEL_LABELS.get(ctype, ctype)}」发送失败：\n{exc}"
                )
                self._push("log", ("ERROR", f"[{datetime.now():%H:%M:%S}] 测试发送失败（{ctype}）：{exc}"))
            else:
                self._push_message(
                    "info", "测试成功", f"通道「{CHANNEL_LABELS.get(ctype, ctype)}」已发送测试消息。"
                )
                self._push("log", ("INFO", f"[{datetime.now():%H:%M:%S}] 测试发送成功（{ctype}）"))

        self._append_log("INFO", f"[{datetime.now():%H:%M:%S}] 正在测试通道 {ctype}…")
        threading.Thread(target=worker, daemon=True, name=f"test-{ctype}").start()

    # ================================================================== #
    # 提醒记录
    # ================================================================== #
    def _insert_alert_row(self, row: Dict[str, Any], to_top: bool = False) -> None:
        """向提醒记录表插入一行。

        Args:
            row: 含 time / keyword / title / price / publish / url 的字典；
                v3.6 起还可带 product_id（「🚫 加入黑名单」需要）；
                v3.7 起还可带 sold（True 表示已售出/下架，置灰显示）。
            to_top: True 表示插到最前面。
        """
        values = (
            row.get("time", ""),
            row.get("keyword", ""),
            row.get("title", ""),
            row.get("price", ""),
            row.get("publish", ""),
        )
        index = 0 if to_top else "end"
        item = self.tree_alerts.insert("", index, values=values)
        self._alert_urls[item] = str(row.get("url", "") or "")
        self._alert_product_ids[item] = str(row.get("product_id", "") or "")
        sold = bool(row.get("sold", False))
        self._alert_sold[item] = sold
        if sold:
            # v3.7：已售出/下架记录灰显，标题列加「[已下架]」标记
            try:
                self.tree_alerts.item(item, tags=("sold",))
            except Exception:  # noqa: BLE001 - 测试替身可能不支持 tags
                pass

    # ------------------------------------------------------------------ #
    # v3.2：提醒记录表点击表头排序
    # ------------------------------------------------------------------ #
    def _on_alert_sort(self, column: str) -> None:
        """点击表头排序：同列再点反序，换列默认升序（v3.2）。

        通过 `tree.move` 原地重排 **item（iid 不变）**，因此
        `self._alert_urls[iid] -> url` 映射保持有效，双击打开链接不受影响。
        """
        if self._alert_sort_col != column:
            self._alert_sort_col = column
            self._alert_sort_asc = True
        else:
            self._alert_sort_asc = not self._alert_sort_asc

        col_index = ALERT_COLUMNS.index(column)
        rows: List[Dict[str, Any]] = []
        for item in self.tree_alerts.get_children(""):
            values = self.tree_alerts.item(item, "values") or ()
            row: Dict[str, Any] = {"iid": item}
            for idx, key in enumerate(ALERT_COLUMNS):
                row[key] = values[idx] if idx < len(values) else ""
            rows.append(row)

        sorted_rows = sort_alert_rows(rows, column, self._alert_sort_asc)
        for position, row in enumerate(sorted_rows):
            self.tree_alerts.move(row["iid"], "", position)

        # 表头显示排序方向
        arrow = " ▲" if self._alert_sort_asc else " ▼"
        for key in ALERT_COLUMNS:
            text = ALERT_HEADING_TEXTS[key]
            if key == column:
                text += arrow
            self.tree_alerts.heading(key, text=text)

    def _load_history(self) -> None:
        """启动时从 SQLite 加载历史已提醒记录。

        v3.7：默认按「隐藏已售出」加载（`include_sold=False`）；
        若用户勾选了「显示已下架/已售出」则包含并灰显。
        """
        try:
            storage = Storage(self._storage_path)
        except Exception as exc:  # noqa: BLE001 - 数据库不可用不应阻塞启动
            logger.warning("加载历史提醒记录失败：%s", exc)
            return
        try:
            rows = storage.list_notified(limit=HISTORY_LIMIT, include_sold=self._show_sold)
            for row in rows:
                self._insert_alert_row(
                    {
                        "time": row["last_seen"],
                        "keyword": row["keyword"],
                        "title": row["title"],
                        "price": f"¥{float(row['price']):.2f}",
                        "publish": row["publish_time"] or "未知",
                        "url": row["url"],
                        "product_id": row["product_id"],
                        "sold": bool(row["sold_out"]) if "sold_out" in row.keys() else False,
                    }
                )
            if rows:
                self._append_log(
                    "INFO", f"[{datetime.now():%H:%M:%S}] 已加载 {len(rows)} 条历史提醒记录"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取历史提醒记录失败：%s", exc)
        finally:
            storage.close()

    # ------------------------------------------------------------------ #
    # v3.7：已售出/下架（提醒记录不再显示已卖掉/下架的商品）
    # ------------------------------------------------------------------ #
    def _reload_alerts(self) -> None:
        """清空提醒记录表并按当前开关状态重载（排序状态保留）。"""
        for item in self.tree_alerts.get_children():
            self.tree_alerts.delete(item)
        self._alert_urls.clear()
        self._alert_product_ids.clear()
        self._alert_sold.clear()
        self._load_history()

    def on_toggle_show_sold(self) -> None:
        """切换「显示已下架/已售出」开关（v3.7）。"""
        self._show_sold = bool(self.var_show_sold.get())
        self._reload_alerts()
        self._append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] 已{'显示' if self._show_sold else '隐藏'}已下架/已售出商品",
        )

    def on_mark_sold_selected(self) -> None:
        """把提醒记录中选中的商品手动标记为「已售出/下架」（v3.7）。

        标记后商品从提醒记录隐藏（后续 `list_notified` 默认排除 sold_out）；
        如需恢复，勾选「显示已下架/已售出」后手动取消？——本版提供
        「显示已下架」查看，恢复入口见 `on_unmark_sold` 的双击/上下文
        （简单起见：显示已下架时，对灰显行再点一次「🗑 标记已售出」即恢复）。
        """
        selection = self.tree_alerts.selection()
        if not selection:
            messagebox.showinfo("提示", "请先在提醒记录中选中要标记的商品。")
            return
        item = selection[0]
        product_id = str(self._alert_product_ids.get(item, "") or "")
        if not product_id:
            messagebox.showwarning("缺少商品 ID", "该记录缺少商品 ID，无法标记售出。")
            return
        values = self.tree_alerts.item(item, "values") or ()
        title = str(values[2]) if len(values) > 2 else ""
        already_sold = bool(self._alert_sold.get(item, False))

        try:
            storage = Storage(self._storage_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("操作失败", f"无法打开数据库：{exc}")
            return
        try:
            if already_sold:
                # 已售出行再点一次 = 恢复在架
                storage.unmark_sold_out(product_id)
                self._append_log(
                    "INFO",
                    f"[{datetime.now():%H:%M:%S}] 已把商品「{title}」（{product_id}）恢复为在架",
                )
            else:
                storage.mark_sold_out_by_id(product_id, reason=SOLD_REASON_MANUAL)
                self._append_log(
                    "INFO",
                    f"[{datetime.now():%H:%M:%S}] 已把商品「{title}」（{product_id}）标记为已售出/下架",
                )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("操作失败", f"标记售出失败：{exc}")
            return
        finally:
            storage.close()

        # 隐藏已下架时直接移除该行；显示已下架时重载（恢复在架的行不再灰显）
        self._reload_alerts()

    def on_check_on_shelf(self) -> None:
        """批量校验提醒记录中商品的在架状态（v3.7，方案 B 详情接口）。

        - 主线程只读取表格中的 (product_id, keyword, title) 普通值并构造配置，
          网络请求全部在后台线程执行（与 v3.5/v3.6 线程模型一致，不碰 tkinter）；
        - 后台线程逐条调用 `MtopFetcher.check_item_status`（实测可用接口
          `mtop.taobao.idle.pc.detail`），两次请求间固定限速
          `SOLD_CHECK_INTERVAL` 秒，避免触发风控；
        - 判定为已售出/下架的商品写回 `product.sold_out`，完成后主线程重载
          提醒记录（默认隐藏售出商品）。
        """
        if self._worker_alive():
            messagebox.showinfo("正在运行", "监控正在运行中，请先停止后再校验在架状态。")
            return
        # 主线程一次性读取：只取当前展示的行（隐藏售出时即「在架候选」）
        items: List[Dict[str, str]] = []
        for item in self.tree_alerts.get_children():
            product_id = str(self._alert_product_ids.get(item, "") or "")
            if not product_id:
                continue
            values = self.tree_alerts.item(item, "values") or ()
            items.append(
                {
                    "product_id": product_id,
                    "keyword": str(values[1]) if len(values) > 1 else "",
                    "title": str(values[2]) if len(values) > 2 else "",
                }
            )
        if not items:
            messagebox.showinfo("没有可校验的商品", "提醒记录为空，没有可校验在架状态的商品。")
            return
        items = items[:SOLD_CHECK_MAX_ITEMS]
        try:
            config = self._build_config_object()
        except (ValueError, ConfigError) as exc:
            messagebox.showwarning("配置有误", str(exc))
            return
        if config.fetcher.type != "mtop":
            messagebox.showinfo(
                "校验不可用",
                "「校验在架」需要 mtop 真实抓取（调用闲鱼商品详情接口）。\n"
                f"当前抓取方式是 {config.fetcher.type}，无法校验，请改用 mtop 并配置 Cookie。",
            )
            return

        self._append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] 开始校验 {len(items)} 个商品的在架状态"
            f"（每次间隔 {SOLD_CHECK_INTERVAL:g}s 限速）…",
        )

        def worker() -> None:
            """后台线程：逐条调详情接口，售出则标记；全程不触碰 tkinter 控件。"""
            sold_ids: List[str] = []
            unknown = 0
            online = 0
            try:
                fetcher = build_fetcher(config)
            except Exception as exc:  # noqa: BLE001
                self._push_message("error", "校验失败", f"构造抓取器失败：\n{exc}")
                self._push("log", ("ERROR", f"[{datetime.now():%H:%M:%S}] 校验在架失败（构造抓取器）：{exc}"))
                return
            storage: Optional[Storage] = None
            try:
                storage = Storage(config.storage.path)
                for index, entry in enumerate(items):
                    if self._stop_event.is_set():
                        self._push("log", ("INFO", f"[{datetime.now():%H:%M:%S}] 已收到停止信号，校验提前结束。"))
                        break
                    pid = entry["product_id"]
                    try:
                        online_flag = fetcher.check_item_status(pid, timeout=12.0)
                    except Exception as exc:  # noqa: BLE001 - 单条失败不中断批量
                        self._push("log", ("WARNING", f"[{datetime.now():%H:%M:%S}] 校验 {pid} 异常：{exc}"))
                        online_flag = None
                    if online_flag is False:
                        storage.mark_sold_out_by_id(pid, reason=SOLD_REASON_DETAIL)
                        sold_ids.append(pid)
                        self._push(
                            "log",
                            ("INFO", f"[{datetime.now():%H:%M:%S}] 🚫 商品「{entry['title'][:24]}」（{pid}）已下架/售出，已标记"),
                        )
                    elif online_flag is True:
                        online += 1
                    else:
                        unknown += 1
                        self._push(
                            "log",
                            ("WARNING", f"[{datetime.now():%H:%M:%S}] ⚠️ 商品 {pid} 在架状态无法判定（跳过，未标记）"),
                        )
                    # 限速：除最后一条外都在两次请求之间等待
                    if index < len(items) - 1:
                        self._stop_event.wait(SOLD_CHECK_INTERVAL)
            except Exception as exc:  # noqa: BLE001 - 后台异常绝不崩窗
                self._push("log", ("ERROR", f"[{datetime.now():%H:%M:%S}] 校验在架线程异常：{exc}"))
            finally:
                if storage is not None:
                    try:
                        storage.close()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    fetcher.close()
                except Exception:  # noqa: BLE001
                    pass
            self._push(
                "log",
                ("INFO", f"[{datetime.now():%H:%M:%S}] ✅ 校验完成：在架 {online}，已下架/售出 {len(sold_ids)}，无法判定 {unknown}"),
            )
            self._push("callable", self._reload_alerts)

        threading.Thread(target=worker, daemon=True, name="sold-check").start()

    def _on_alert_double_click(self, _event: Any) -> None:
        """双击提醒记录：用系统浏览器打开商品页。"""
        selection = self.tree_alerts.selection()
        if not selection:
            return
        url = self._alert_urls.get(selection[0], "")
        if not url:
            messagebox.showinfo("无链接", "该记录没有可打开的商品链接。")
            return
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", f"无法打开链接：{exc}")

    # ------------------------------------------------------------------ #
    # 临时黑名单（v3.6）：人工剔除噪音/假货/非目标商品
    # ------------------------------------------------------------------ #
    def _ask_blacklist_reason(self, title: str) -> Optional[str]:
        """弹出「加入黑名单」原因输入框。

        Args:
            title: 商品标题（展示用）。

        Returns:
            原因字符串；用户点「取消」返回 None。
        """
        try:
            from tkinter import simpledialog
        except ImportError:  # pragma: no cover - 极老 tk 缺失该子模块
            simpledialog = None  # type: ignore[assignment]
        if simpledialog is None:  # pragma: no cover - 防御分支
            return ""
        return simpledialog.askstring(
            "加入黑名单",
            "把该商品加入黑名单后：\n"
            "  · 不再提醒、不再出现在提醒记录\n"
            "  · 可在「📋 黑名单管理」中恢复\n\n"
            f"商品：{title}\n\n"
            "原因（可选）：",
            initialvalue=BLACKLIST_REASON_DEFAULT,
            parent=self.root,
        )

    def on_blacklist_selected(self) -> None:
        """把提醒记录中选中的商品加入黑名单（确认后可填原因）。

        成功后立即从表格移除该行（后续 `list_notified` 也会自动排除黑名单）。
        """
        selection = self.tree_alerts.selection()
        if not selection:
            messagebox.showinfo("提示", "请先在提醒记录中选中要加入黑名单的商品。")
            return
        item = selection[0]
        product_id = str(self._alert_product_ids.get(item, "") or "")
        if not product_id:
            messagebox.showwarning("缺少商品 ID", "该记录缺少商品 ID，无法加入黑名单。")
            return
        values = self.tree_alerts.item(item, "values") or ()
        keyword = str(values[1]) if len(values) > 1 else ""
        title = str(values[2]) if len(values) > 2 else ""
        reason = self._ask_blacklist_reason(title)
        if reason is None:  # 用户取消
            return

        try:
            storage = Storage(self._storage_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("操作失败", f"无法打开数据库：{exc}")
            return
        try:
            added = blacklist_alert_row(
                storage, {"product_id": product_id, "keyword": keyword}, reason=reason
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("操作失败", f"加入黑名单失败：{exc}")
            return
        finally:
            storage.close()

        if not added:
            messagebox.showwarning("缺少商品 ID", "该记录缺少商品 ID，无法加入黑名单。")
            return
        self.tree_alerts.delete(item)
        self._alert_urls.pop(item, None)
        self._alert_product_ids.pop(item, None)
        self._alert_sold.pop(item, None)
        self._append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] 已把商品「{title}」（{product_id}）加入黑名单"
            + (f"（原因：{reason}）" if str(reason or "").strip() else ""),
        )

    def on_manage_blacklist(self) -> None:
        """弹出「黑名单管理」对话框：查看黑名单商品，支持恢复（移出黑名单）。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("黑名单管理（人工剔除的商品）")
        dialog.geometry("680x360")
        dialog.transient(self.root)
        dialog.resizable(True, True)

        wrap = ttk.Frame(dialog)
        wrap.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        columns = ("product_id", "keyword", "reason", "created_at")
        tree = ttk.Treeview(wrap, columns=columns, show="headings", height=10)
        for key, text, width in (
            ("product_id", "商品 ID", 160),
            ("keyword", "关键词", 110),
            ("reason", "原因", 170),
            ("created_at", "加入时间", 150),
        ):
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor="w")
        tree.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)

        def _read_blacklist() -> List[Any]:
            """从数据库读取黑名单列表（失败弹框并返回空列表）。"""
            try:
                storage = Storage(self._storage_path)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("读取失败", f"无法打开数据库：{exc}", parent=dialog)
                return []
            try:
                return list(storage.list_blacklist())
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("读取失败", f"读取黑名单失败：{exc}", parent=dialog)
                return []
            finally:
                storage.close()

        def refresh() -> None:
            """重绘黑名单列表。"""
            tree.delete(*tree.get_children())
            for row in _read_blacklist():
                tree.insert(
                    "",
                    "end",
                    values=(row["product_id"], row["keyword"], row["reason"], row["created_at"]),
                )

        def on_restore() -> None:
            """把选中的商品移出黑名单（恢复提醒）。"""
            selection = tree.selection()
            if not selection:
                messagebox.showinfo("提示", "请先选中要恢复的商品。", parent=dialog)
                return
            values = tree.item(selection[0], "values") or []
            pid = str(values[0]) if values else ""
            if not pid:
                return
            if not messagebox.askyesno(
                "确认恢复",
                f"确定把商品 {pid} 移出黑名单吗？\n\n"
                "移出后，若该商品再次低价出现，将恢复正常提醒。",
                parent=dialog,
            ):
                return
            try:
                storage = Storage(self._storage_path)
                try:
                    storage.remove_blacklist(pid)
                finally:
                    storage.close()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("操作失败", f"恢复失败：{exc}", parent=dialog)
                return
            refresh()
            self._append_log(
                "INFO", f"[{datetime.now():%H:%M:%S}] 已把商品 {pid} 移出黑名单（恢复提醒）"
            )

        btn_row = ttk.Frame(dialog)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_row, text="♻️ 恢复选中", command=on_restore).pack(side="left")
        ttk.Button(btn_row, text="关闭", command=dialog.destroy).pack(side="right")

        refresh()

    # ================================================================== #
    # 监控运行
    # ================================================================== #
    def _worker_alive(self) -> bool:
        """判断是否有后台监控任务在跑。"""
        return self._worker is not None and self._worker.is_alive()

    def on_start(self) -> None:
        """开始循环监控。"""
        if self._worker_alive():
            messagebox.showinfo("已在运行", "监控已经在运行中。")
            return
        self._launch_worker(single_round=False)

    def on_run_once(self) -> None:
        """立即执行一轮监测。"""
        if self._worker_alive():
            messagebox.showinfo("正在运行", "监控正在运行中，请先停止后再手动执行。")
            return
        self._launch_worker(single_round=True)

    def _launch_worker(self, single_round: bool) -> None:
        """校验配置并启动后台监控线程。

        v3.6 线程安全修复（需求 3「立即执行一轮 / 开始监控后窗口无响应」根因）：
            旧实现中 `_monitor_worker` 在**后台线程**里直接读取
            `self.var_log_detail_only.get()`（tkinter 控件）。Tkinter 不是
            线程安全的，后台线程进入 Tcl 解释器会与主线程（拖动/点击/绘制）
            争用 Tcl 互斥锁，表现为「点击后窗口无响应 / 拖动卡住」。
            修复：所有 tkinter 控件状态（此处为「仅展示符合的低价」勾选）
            一律在**主线程**一次性读取，作为普通 bool 传给后台线程；
            后台线程从此不再触碰任何 tkinter 控件。
        `_build_config_object()` 为纯本地操作（无网络、无 Cookie 检测，
        仅读取界面表单 + 组装 dict + 校验），耗时毫秒级，保留在主线程执行，
        以便配置错误能立即弹框提示；下方单测 `TestConfigBuildIsFastLocal`
        对「纯本地 + 快速」做了显式断言。

        Args:
            single_round: True 只跑一轮，False 按间隔循环。
        """
        try:
            config = self._build_config_object()
        except (ValueError, ConfigError) as exc:
            messagebox.showwarning("配置有误", str(exc))
            return

        # 主线程读取「仅展示符合的低价」勾选状态，作为普通值传给后台线程
        detail_only = bool(
            getattr(self, "var_log_detail_only", None) is None
            or self.var_log_detail_only.get()
        )

        self._stop_event = threading.Event()
        self._mode = "once" if single_round else "loop"
        self._set_running(True)
        self._worker = threading.Thread(
            target=self._monitor_worker,
            args=(config, single_round, detail_only),
            daemon=True,
            name="xianyu-monitor",
        )
        self._worker.start()

    def _monitor_worker(self, config: Config, single_round: bool, detail_only: bool = True) -> None:
        """后台监控线程主体。

        v3.6：新增 `detail_only` 参数（主线程传入的普通 bool），
        本线程**绝不访问任何 tkinter 控件**（含 `self.var_*`），
        只通过 `queue.Queue` 与主线程通信，从根上消除 UI 卡死。

        Args:
            config: 已校验的配置对象。
            single_round: True 只跑一轮。
            detail_only: True 时 monitor 只记录概况与命中明细（对应 GUI
                「仅展示符合的低价」勾选）；False 时逐条记录全部商品明细。
        """
        fetcher = None
        storage = None
        try:
            fetcher = build_fetcher(config)
            storage = Storage(config.storage.path)
            notifiers = build_notifiers(config)
            monitor = Monitor(config, fetcher, storage, notifiers)
            interval = config.monitor.interval_seconds

            # 启动预检：Cookie 过期 → warning 日志（不阻断运行）
            monitor.preflight_cookie()

            self._push(
                "log",
                (
                    "INFO",
                    f"[{datetime.now():%H:%M:%S}] 监控启动：抓取方式 {config.fetcher.type}，"
                    f"关键词 {[r.keyword for r in config.keywords]}，"
                    f"间隔 {interval} 秒，通知通道 {[n.name for n in notifiers]}",
                ),
            )

            while True:
                # v3.5：每轮开始前先检查停止信号，缩短停止响应时间
                if self._stop_event.is_set():
                    self._push("log", ("INFO", f"[{datetime.now():%H:%M:%S}] 已收到停止信号，监控退出。"))
                    break
                self._next_run_at = 0.0
                self._push("log", ("INFO", f"[{datetime.now():%H:%M:%S}] ===== 第 {self._round_no + 1} 轮监测开始 ====="))
                hits: List[Product] = []
                try:
                    # v3.6：detail_only 由主线程在 _launch_worker 时读好传入，
                    # 后台线程不再访问 tkinter 控件（UI 无响应修复的关键）。
                    monitor.run_once(log_item_details=not detail_only)
                    hits = list(monitor.last_result.notified_products)
                except Exception as exc:  # noqa: BLE001 - 单轮异常不终止循环
                    self._push("log", ("ERROR", f"[{datetime.now():%H:%M:%S}] 本轮监测异常：{exc}"))
                    logger.debug("监测轮次异常", exc_info=True)

                self._round_no += 1
                self._alert_total += len(hits)
                now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for product in hits:
                    self._push(
                        "alert",
                        {
                            "time": now_text,
                            "keyword": product.keyword,
                            "title": product.title,
                            "price": product.price_text,
                            "publish": product.publish_time or "未知",
                            "url": product.url,
                            "product_id": product.product_id,
                        },
                    )
                    self._push(
                        "log",
                        (
                            "ALERT",
                            f"[{datetime.now():%H:%M:%S}] 🔔 低价命中！[{product.keyword}] "
                            f"{product.title} —— {product.price_text}",
                        ),
                    )
                self._push("status", {"rounds": self._round_no, "alerts": self._alert_total})

                if single_round or self._stop_event.is_set():
                    break

                self._next_run_at = time.monotonic() + interval
                # 用 Event.wait 代替 sleep，才能立刻响应「停止监控」
                if self._stop_event.wait(interval):
                    self._push("log", ("INFO", f"[{datetime.now():%H:%M:%S}] 已收到停止信号，监控退出。"))
                    break
        except Exception as exc:  # noqa: BLE001 - 后台异常绝不允许崩窗
            logger.debug("监控线程异常", exc_info=True)
            self._push("log", ("ERROR", f"[{datetime.now():%H:%M:%S}] 监控线程异常退出：{exc}"))
            self._push_message("error", "监控异常", f"监控线程异常退出：\n{exc}")
        finally:
            for closable in (fetcher, storage):
                if closable is None:
                    continue
                try:
                    closable.close()
                except Exception:  # noqa: BLE001
                    pass
            self._next_run_at = 0.0
            self._push("log", ("INFO", f"[{datetime.now():%H:%M:%S}] 监控已停止。"))
            self._push("state", {"running": False})

    def on_stop(self) -> None:
        """请求停止监控。"""
        if not self._worker_alive():
            self._set_running(False)
            return
        self._stop_event.set()
        self.var_status.set("状态：正在停止…")
        self.btn_stop.configure(state="disabled")
        self._append_log("INFO", f"[{datetime.now():%H:%M:%S}] 已发送停止信号，等待当前轮结束…")

    # ================================================================== #
    # 清空记录
    # ================================================================== #
    def on_clear_records(self) -> None:
        """清空去重记录（product 表 + meta 表）。"""
        if self._worker_alive():
            messagebox.showinfo("正在运行", "请先停止监控再清空记录。")
            return
        proceed = messagebox.askyesno(
            "确认清空",
            "确定要清空全部去重记录吗？\n\n"
            "清空后，之前提醒过的商品会被重新视为「新商品」，\n"
            "下一轮监测可能会重复提醒。此操作不可撤销。",
        )
        if not proceed:
            return

        try:
            storage = Storage(self._storage_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("清空失败", f"无法打开数据库：{exc}")
            return
        try:
            deleted = storage.clear_all()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("清空失败", f"清空记录时出错：{exc}")
            return
        finally:
            storage.close()

        for item in self.tree_alerts.get_children():
            self.tree_alerts.delete(item)
        self._alert_urls.clear()
        self._alert_product_ids.clear()
        self._alert_sold.clear()
        self._alert_total = 0
        self.var_alerts.set("累计提醒：0")
        self._append_log("INFO", f"[{datetime.now():%H:%M:%S}] 已清空去重记录，共删除 {deleted} 条。")
        messagebox.showinfo("已清空", f"已删除 {deleted} 条商品记录。")

    # ================================================================== #
    def on_close(self) -> None:
        """关闭窗口：优雅停止后台线程并释放资源（v3.5 稳定性修复）。

        关闭顺序（保证「反复启停无残留、关闭干净」）：
            1. 监控线程存活时弹确认框；用户同意后置位 `_stop_event`；
            2. **join 监控线程（带超时 `CLOSE_JOIN_TIMEOUT`）**——修复关闭卡死根因：
               旧实现不 join，若监控线程正卡在 mtop 网络请求（超时 20s + 重试退避），
               daemon 线程在解释器退出阶段仍占用线程状态，Windows 上表现为
               「窗口关了但进程还在」；带超时 join 保证主线程最多等 5 秒；
            3. 置位 `_closing` 并取消已注册的 after 回调（`_poll_queue` / `_tick`
               不再重新调度，避免销毁后回调残留）；
            4. 移除日志 handler（避免 queue 日志线程泄漏）；
            5. 销毁窗口（监控线程的 Storage / fetcher 在其自身 finally 中关闭）。
        """
        if self._worker_alive():
            proceed = messagebox.askyesno("确认退出", "监控正在运行，确定要退出吗？")
            if not proceed:
                return
            self._stop_event.set()
            worker = self._worker
            if worker is not None:
                try:
                    worker.join(timeout=CLOSE_JOIN_TIMEOUT)
                except Exception:  # noqa: BLE001 - join 异常不影响关闭
                    pass

        self._closing = True
        try:
            if getattr(self, "_poll_after_id", None) is not None:
                self.root.after_cancel(self._poll_after_id)
            if getattr(self, "_tick_after_id", None) is not None:
                self.root.after_cancel(self._tick_after_id)
        except Exception:  # noqa: BLE001 - 窗口销毁等边缘情况
            pass

        self._remove_log_handler()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover
            pass


# ====================================================================== #
# 入口
# ====================================================================== #
def main(config_path: str = "config.yaml") -> int:
    """启动图形界面。

    Args:
        config_path: 配置文件路径。

    Returns:
        进程退出码，0 表示正常退出。
    """
    # windowed 打包 exe 无控制台：安装滚动文件日志便于查错（失败不影响启动）
    try:
        from .cli import install_file_logging

        install_file_logging()
    except Exception:  # noqa: BLE001
        pass

    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 - 无图形环境时给出清晰提示
        print(f"无法创建图形界面窗口：{exc}\n请确认当前环境支持 GUI 显示。")
        return 1

    try:
        XianyuAlertGUI(root, config_path=config_path)
    except Exception as exc:  # noqa: BLE001 - 构造失败也要给出提示而非白屏
        logger.exception("图形界面初始化失败：%s", exc)
        try:
            messagebox.showerror("启动失败", f"图形界面初始化失败：\n{exc}")
        except Exception:  # noqa: BLE001
            pass
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        return 1

    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    import sys

    sys.exit(main())
