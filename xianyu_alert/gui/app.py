"""Tk 主界面：日志队列桥、XianyuAlertGUI 主窗口与命令行入口（v1.9.5 从 gui.py 拆出）。"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import re
import threading
import time
import webbrowser
from datetime import datetime
from typing import Any

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
    Config,
    ConfigError,
    NotifyChannel,
    config_from_dict,
)
from ..cookie import (
    cookie_has_token,
)
from ..fetcher import build_fetcher
from ..filters import normalize_keywords
from ..models import Product
from ..monitor import Monitor
from ..notifier import build_notifier, build_notifiers
from ..shortcut import create_shortcut
from ..singleton import acquire_instance_lock, lock_holder_pid, release_instance_lock
from ..storage import Storage
from .constants import (
    BLACKLIST_REASON_DEFAULT,
    CHANNEL_FIELDS,
    CHANNEL_LABELS,
    CHANNEL_ORDER,
    CHANNEL_REQUIRED_FIELDS,
    CLOSE_JOIN_TIMEOUT,
    COOKIE_MANUAL_HELP,
    COOKIE_STATE_EXPIRED,
    COOKIE_STATE_EXPIRING,
    COOKIE_STATE_MISSING,
    COOKIE_STATE_NO_TOKEN,
    COOKIE_STATE_OK,
    COOKIE_STATE_UNDECRYPTABLE,
    DEFAULT_DB_PATH,
    FETCHER_CHOICES,
    HISTORY_LIMIT,
    MAX_LOG_LINES,
    MAX_QUEUE_MESSAGES_PER_POLL,
    MIN_WINDOW_SIZE,
    POLL_IDLE_INTERVAL_MS,
    POLL_INTERVAL_MS,
    SOLD_CHECK_INTERVAL,
    SOLD_CHECK_MAX_ITEMS,
    SOLD_REASON_DETAIL,
    SOLD_REASON_MANUAL,
    WINDOW_SIZE,
    WINDOW_TITLE,
)
from .helpers import (
    ALERT_COLUMNS,
    ALERT_HEADING_TEXTS,
    _apply_row_style_if_available,
    _keyword_enabled_dict,
    about_full_text,
    add_preset_excludes,
    apply_filter_edit,
    blacklist_alert_row,
    build_config_dict,
    channel_is_complete,
    config_file_mtime,
    config_to_form,
    cookie_status,
    empty_state_hint,
    fetcher_label,
    fetcher_type_from_label,
    first_use_guide,
    format_countdown,
    keyword_filter_summary,
    keyword_status_text,
    load_raw_config,
    log_tag_for_text,
    make_sample_product,
    normalize_channel_options,
    parse_enabled_flag,
    parse_keyword_lines,
    resolve_preset_exclude_keywords,
    save_raw_config,
    sort_alert_rows,
    validate_interval,
    validate_keyword_entry,
    validate_pages,
)

logger = logging.getLogger(__name__)

class QueueLogHandler(logging.Handler):
    """把日志记录推送到线程安全队列，由主线程渲染到日志区。

    这样 monitor / fetcher / notifier 等模块的既有日志无需任何改动
    就能显示在图形界面里。
    """

    def __init__(self, target_queue: queue.Queue, level: int = logging.INFO) -> None:
        """初始化。

        Args:
            target_queue: 目标队列，元素形如 ("log", (级别名, 文本))。
            level: 处理的最低日志级别。
        """
        super().__init__(level=level)
        self.target_queue: queue.Queue = target_queue

    def emit(self, record: logging.LogRecord) -> None:
        """把一条日志放入队列（自身异常绝不向外抛）。"""
        with contextlib.suppress(Exception):   # 日志失败绝不能影响业务
            message = record.getMessage()
            timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            text = f"[{timestamp}] {message}"
            if record.exc_info:
                text = f"{text}\n{self.format(record)}"
            self.target_queue.put_nowait(("log", (record.levelname, text)))


# ====================================================================== #
# 主窗口
# ====================================================================== #
class XianyuAlertGUI:
    """闲鱼低价提醒工具的图形界面主窗口。

    Attributes:
        root: Tk 根窗口。
        config_path: 配置文件路径。
    """

    def __init__(self, root: tk.Tk, config_path: str = "config.yaml") -> None:
        """构造窗口与全部控件。

        Args:
            root: Tk 根窗口。
            config_path: 配置文件路径。
        """
        self.root: tk.Tk = root
        self.config_path: str = config_path

        # ---- 运行时状态 ----
        self.ui_queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()
        self._mode: str = ""            # "loop" / "once" / ""
        self._round_no: int = 0
        self._alert_total: int = 0
        self._next_run_at: float = 0.0
        self._running: bool = False
        self._alert_urls: dict[str, str] = {}
        #: 提醒记录 iid -> 商品 ID（v3.6 黑名单「加入」需要 product_id）
        self._alert_product_ids: dict[str, str] = {}
        #: 提醒记录 iid -> 是否已标记「售出/下架」（v3.7；显示已下架时用于置灰）
        self._alert_sold: dict[str, bool] = {}
        #: 提醒记录是否显示已售出/下架商品（v3.7；默认隐藏，勾选后显示并置灰）
        self._show_sold: bool = False
        #: 关闭流程标志（v3.5）：置位后 `_poll_queue` / `_tick` 不再重新调度 after，
        #: 避免窗口销毁后回调残留导致进程不退出。
        self._closing: bool = False
        #: 已注册的 after 回调 id（v3.5，关闭时显式取消）
        self._poll_after_id: str | None = None
        self._tick_after_id: str | None = None

        # ---- 配置 ----
        self._raw_config: dict[str, Any] = load_raw_config(self.config_path)
        #: v1.8（C22）：config.yaml 的 mtime 快照，用于检测外部修改（如挂机时 cli login）
        self._config_mtime: float | None = config_file_mtime(self.config_path)
        form = config_to_form(self._raw_config)
        self._cookies: str = form["cookies"]
        #: Cookie 原为密文但无法解密（换机/换用户）→ 状态灯显示「无法解密」
        self._cookies_undecryptable: bool = bool(form.get("cookies_undecryptable", False))
        #: 多 Cookie 池（v3.2）：[{"name", "cookie"(明文), "enabled"}]，内存态
        self._cookie_pool: list[dict[str, Any]] = list(form.get("cookie_pool") or [])
        self._storage_path: str = form["storage_path"]
        self._keywords: list[tuple[str, float]] = list(form["keywords"])
        #: 关键词 -> 是否启用（v3.7；停用不删除，保存后写回 config，monitor 跳过）
        self._keyword_enabled: dict[str, bool] = dict(form.get("keyword_enabled") or {})
        #: 关键词 -> {exclude_keywords, required_keywords}（v3.1 过滤规则）
        self._keyword_filters: dict[str, dict[str, list[str]]] = dict(form.get("keyword_filters") or {})
        #: 预置排除词模板（v3.5）：新关键词自动预置的列表；来源 config 顶层
        #: `preset_exclude_keywords`，缺省（缺失/None）回退默认；GUI「编辑预置排除词」可改。
        #: 注意：显式空列表 [] 表示「关闭自动预置」，必须保留为空，不能用 falsy 判断
        #: 回退默认（BUG-1 修复：`or` → None 判断，见 resolve_preset_exclude_keywords）。
        self._preset_exclude_keywords: list[str] = resolve_preset_exclude_keywords(
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
    def _build_widgets(self, form: dict[str, Any]) -> None:
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
    def _build_tab_config(self, parent: ttk.Frame, form: dict[str, Any]) -> None:
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
        # v1.8：新增「🔄 一键刷新 Cookie」入口（C7）——引导式三步刷新。
        self.btn_refresh_cookie = ttk.Button(row3, text="🔄 一键刷新 Cookie", command=self.on_refresh_cookie)
        self.btn_refresh_cookie.pack(side="left", padx=(8, 0))
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
    def _build_tab_notify(self, parent: ttk.Frame, form: dict[str, Any]) -> None:
        """构建「通知设置」标签页。"""
        canvas_hint = ttk.Label(
            parent,
            text="勾选需要启用的通知方式并填写参数；只有「勾选且参数完整」的通道才会被保存。",
            foreground="#555555",
        )
        canvas_hint.pack(fill="x", padx=12, pady=(10, 4))

        self.var_channel_enabled: dict[str, tk.BooleanVar] = {}
        self.var_channel_fields: dict[str, dict[str, tk.StringVar]] = {}

        channels_state: dict[str, dict[str, Any]] = form["channels"]
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

            field_vars: dict[str, tk.StringVar] = {}
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
    def _build_tab_run(self, parent: ttk.Frame) -> None:
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
            with contextlib.suppress(Exception):
                logging.getLogger("xianyu_alert").removeHandler(handler)

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
        with contextlib.suppress(tk.TclError):   # pragma: no cover - 窗口销毁等边缘情况
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

    def _adjust_log_font(self, delta: int) -> None:
        """调整日志区字号（v3.2，范围 8~16）。"""
        self._log_font_size = max(8, min(16, self._log_font_size + int(delta)))
        self._apply_log_font()

    def on_clear_log(self) -> None:
        """清空日志区（v3.2；日志非关键数据，直接清空不二次确认）。"""
        widget = getattr(self, "text_log", None)
        if widget is None:
            return
        with contextlib.suppress(tk.TclError):   # pragma: no cover - 窗口销毁等边缘情况
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")

    # ================================================================== #
    # 队列轮询（子线程 -> 主线程）
    # ================================================================== #
    def _push(self, kind: str, payload: Any) -> None:
        """子线程安全地投递一条 UI 消息。"""
        with contextlib.suppress(Exception): # 队列异常不应影响业务线程
            self.ui_queue.put_nowait((kind, payload))

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
                with contextlib.suppress(Exception): # 窗口销毁等边缘情况
                    self._poll_after_id = self.root.after(delay, self._poll_queue)

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
        with contextlib.suppress(Exception):   # 刷新失败不影响主流程
            if self._running and self._next_run_at > 0:
                remain = self._next_run_at - time.monotonic()
                self.var_countdown.set(f"下次执行：{format_countdown(remain)}")
            elif self._running:
                self.var_countdown.set("下次执行：执行中…")
            else:
                self.var_countdown.set("下次执行：--:--")
        try:
            # v1.8（C22）：检测 config.yaml 是否被外部修改（本进程保存会更新快照，不触发）
            self._check_config_mtime()
        except Exception:  # noqa: BLE001, S110 - mtime 检测失败不影响主流程；此处为 try/except/finally 结构，finally 必须继续执行，无法用 contextlib.suppress 表达
            pass
        finally:
            if not getattr(self, "_closing", False):
                with contextlib.suppress(Exception): # 窗口销毁等边缘情况
                    self._tick_after_id = self.root.after(1000, self._tick)

    # ------------------------------------------------------------------ #
    # v1.8（C22）：config.yaml 外部修改检测 → 提示重载
    # ------------------------------------------------------------------ #
    def _touch_config_mtime(self) -> None:
        """本进程保存后更新 mtime 快照，避免「自己保存」触发重载提示。"""
        self._config_mtime = config_file_mtime(self.config_path)

    def _check_config_mtime(self) -> None:
        """比对磁盘 mtime 与快照；外部修改 → 弹「是否重载？」询问（Q7：弹框）。

        用户确认 → 从磁盘重载内存态；拒绝 → 更新快照避免每秒重复弹框。
        """
        current = config_file_mtime(self.config_path)
        if current is None or self._config_mtime is None:
            return
        if abs(current - self._config_mtime) > 0.001:
            proceed = messagebox.askyesno(
                "配置文件已被外部修改",
                f"检测到配置文件已被外部修改：\n{os.path.abspath(self.config_path)}\n\n是否立即重载？",
            )
            if proceed:
                try:
                    self._reload_config_from_disk()
                except Exception as exc:  # noqa: BLE001 - 重载失败只记日志
                    self._append_log("ERROR", f"重载配置失败：{exc}")
            self._touch_config_mtime()

    def _rebuild_keyword_tree(self) -> None:
        """按内存态重建关键词表格（外部重载 / 重置用）。"""
        for iid in self.tree_keywords.get_children():
            self.tree_keywords.delete(iid)
        for keyword, price in self._keywords:
            enabled = bool(self._keyword_enabled.get(keyword, True))
            item = self.tree_keywords.insert(
                "",
                "end",
                values=(
                    keyword,
                    f"{price:g}",
                    keyword_status_text(enabled),
                    self._filters_summary(keyword),
                ),
            )
            _apply_row_style_if_available(self, item, keyword)

    def _reload_config_from_disk(self) -> None:
        """从磁盘重载配置到内存态并刷新界面（C22 用户确认后调用）。"""
        raw = load_raw_config(self.config_path)
        form = config_to_form(raw)
        self._raw_config = raw
        self._cookies = str(form.get("cookies", "") or "")
        self._cookies_undecryptable = bool(form.get("cookies_undecryptable", False))
        self._cookie_pool = list(form.get("cookie_pool") or [])
        self._storage_path = str(form.get("storage_path") or DEFAULT_DB_PATH)
        self._keywords = list(form.get("keywords") or [])
        self._keyword_enabled = dict(form.get("keyword_enabled") or {})
        self._keyword_filters = dict(form.get("keyword_filters") or {})
        self._preset_exclude_keywords = resolve_preset_exclude_keywords(
            form.get("preset_exclude_keywords")
        )
        # 刷新界面控件
        self.var_interval.set(str(form.get("interval", 600)))
        self.var_fetcher.set(fetcher_label(form.get("fetcher_type", "mtop")))
        self.var_pages.set(str(form.get("pages", 1)))
        self._rebuild_keyword_tree()
        self._refresh_cookie_status()
        self._refresh_first_use_guide()
        self._refresh_keyword_empty_hint()
        self._touch_config_mtime()
        self._append_log("INFO", f"检测到配置文件被外部修改，已重载：{os.path.abspath(self.config_path)}")

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
        with contextlib.suppress(tk.TclError):   # pragma: no cover - 窗口销毁等边缘情况
            if has_keywords:
                self.label_kw_empty.pack_forget()
            else:
                self.label_kw_empty.pack(fill="x", padx=12, pady=(0, 2))

    def _filters_summary(self, keyword: str) -> str:
        """返回某关键词过滤规则的表格摘要文案。"""
        return keyword_filter_summary(self._keyword_filters.get(str(keyword)))

    def _collect_keywords(self) -> list[tuple[str, float]]:
        """从表格读取当前关键词列表（(关键词, 价格阈值)）。

        v3.7 兼容说明：本方法**保持返回 (keyword, price) 二元组不变**
        （既有测试 test_gui.py:517 断言该形状）；启用/停用状态由
        `_collect_keyword_rules` 提供（3 元组），`_collect_config_dict`
        走后者，保证 enabled 写入 config。
        """
        result: list[tuple[str, float]] = []
        for item in self.tree_keywords.get_children():
            values = self.tree_keywords.item(item, "values")
            if not values or len(values) < 2:
                continue
            try:
                result.append((str(values[0]), float(values[1])))
            except (TypeError, ValueError):
                continue
        return result

    def _collect_keyword_rules(self) -> list[tuple[str, float, bool]]:
        """从表格读取完整关键词规则（(关键词, 价格阈值, 是否启用)，v3.7）。

        启用状态以 `self._keyword_enabled` 为准（缺省 True），与表格「状态」
        列保持一致；停用的关键词仍会被收集，保存后写回 config 供 monitor 跳过。
        """
        rules: list[tuple[str, float, bool]] = []
        for keyword, price in self._collect_keywords():
            enabled = parse_enabled_flag(_keyword_enabled_dict(self).get(str(keyword)), default=True)
            rules.append((keyword, price, enabled))
        return rules

    def _apply_keyword_row_style(self, item: str, keyword: str) -> None:
        """按启用状态刷新关键词行：状态列文案 + 停用行灰显（v3.7）。"""
        enabled = parse_enabled_flag(_keyword_enabled_dict(self).get(str(keyword)), default=True)
        with contextlib.suppress(Exception):   # FakeTree 等测试替身不支持 tags 时忽略
            values = list(self.tree_keywords.item(item, "values") or ())
            while len(values) < 3:
                values.append("")
            values[2] = keyword_status_text(enabled)
            self.tree_keywords.item(item, values=tuple(values))
            self.tree_keywords.item(item, tags=("enabled",) if enabled else ("disabled",))

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

    def _default_filters(self, keyword: str) -> dict[str, list[str]]:
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
        with contextlib.suppress(tk.TclError):   # pragma: no cover - 窗口销毁等边缘情况
            dialog.grab_set()
        text_exclude.focus_set()

    def _dialog_add_preset(self, text_widget: tk.Text) -> None:
        """把当前预置排除词追加到对话框的排除词文本框（去重保序）。"""
        existing = parse_keyword_lines(text_widget.get("1.0", "end"))
        merged = add_preset_excludes(existing, preset=self._preset_exclude_keywords)
        text_widget.delete("1.0", "end")
        for token in merged:
            text_widget.insert("end", token + "\n")

    # ------------------------------------------------------------------ #
    # 预置排除词编辑（v3.5）：可配置、可持久化
    # ------------------------------------------------------------------ #
    def _apply_preset_edit(self, text: Any) -> list[str]:
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
        with contextlib.suppress(tk.TclError):   # pragma: no cover - 窗口销毁等边缘情况
            dialog.grab_set()
        text.focus_set()

    # ================================================================== #
    # 配置保存
    # ================================================================== #
    def _collect_channels(self) -> dict[str, dict[str, Any]]:
        """从界面读取通知通道状态。"""
        channels: dict[str, dict[str, Any]] = {}
        for ctype in CHANNEL_ORDER:
            options = {
                name: var.get() for name, var in self.var_channel_fields.get(ctype, {}).items()
            }
            channels[ctype] = {
                "enabled": bool(self.var_channel_enabled[ctype].get()),
                "options": options,
            }
        return channels

    def _collect_config_dict(self) -> dict[str, Any]:
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
        # v1.8（C22）：本进程保存后更新 mtime 快照，避免触发「外部修改」重载提示
        self._touch_config_mtime()
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
        with contextlib.suppress(tk.TclError):   # pragma: no cover - 主题不支持时忽略
            self.label_cookie.configure(foreground=color)
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
    def on_refresh_cookie(self) -> None:
        """「🔄 一键刷新 Cookie」：引导式三步刷新（C7）。

        ① 展示手动获取步骤（COOKIE_MANUAL_HELP）；
        ② 用户粘贴新 Cookie（或点 Playwright 按钮半自动提取）；
        ③ 校验（detect_cookie_health 非 ok 拒绝保存，C15）→ Fernet 加密回写
           config.yaml → 同步内存态 `_cookies` → 状态灯即时变绿（C17）。

        校验失败不落盘，给出可操作原因（C20）。
        """
        dialog = tk.Toplevel(self.root)
        dialog.title("🔄 一键刷新 Cookie")
        dialog.geometry("640x420")
        dialog.transient(self.root)
        dialog.resizable(True, True)

        wrap = ttk.Frame(dialog)
        wrap.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        ttk.Label(
            wrap,
            text=COOKIE_MANUAL_HELP,
            justify="left",
            foreground="#555555",
            wraplength=600,
        ).pack(fill="x", pady=(0, 6))

        ttk.Label(wrap, text="把新 Cookie 粘贴到下方（须包含 _m_h5_tk=）：").pack(anchor="w")
        text_cookie = tk.Text(wrap, height=8, wrap="char")
        text_cookie.pack(fill="both", expand=True, pady=(4, 6))

        def _on_playwright() -> None:
            """Playwright 半自动提取（可选依赖，失败提示安装或改用手动粘贴）。"""
            from ..cookie import PlaywrightUnavailable, acquire_via_playwright

            try:
                cookie_str = acquire_via_playwright()
            except PlaywrightUnavailable as exc:
                messagebox.showwarning("Playwright 不可用", str(exc), parent=dialog)
                return
            except Exception as exc:  # noqa: BLE001 - 提取失败给出提示
                messagebox.showwarning("提取失败", f"自动提取 Cookie 失败：{exc}", parent=dialog)
                return
            text_cookie.delete("1.0", "end")
            text_cookie.insert("1.0", cookie_str)

        def _on_save() -> None:
            """校验并保存（不通过不落盘）。"""
            from ..cookie import cookie_accept_state, detect_cookie_health

            new_cookie = text_cookie.get("1.0", "end").strip()
            state, reason = detect_cookie_health(new_cookie)
            if not cookie_accept_state(state):
                messagebox.showerror(
                    "校验失败",
                    f"Cookie 无效（{state}）：{reason}\n\n未保存任何改动。",
                    parent=dialog,
                )
                return
            self._cookies = new_cookie
            self._cookies_undecryptable = False
            try:
                data = self._collect_config_dict()  # encrypt_cookies=True → Fernet 回写
                config_from_dict(data)
                save_raw_config(self.config_path, data)
            except (ValueError, ConfigError, OSError) as exc:
                messagebox.showerror("保存失败", f"写入配置失败：{exc}", parent=dialog)
                return
            self._raw_config = data
            self._touch_config_mtime()
            self._refresh_cookie_status()
            self._append_log(
                "INFO",
                f"✅ Cookie 已更新并加密保存（脱敏：{secure.mask_cookie(new_cookie) or '（空）'}），下一轮将生效。",
            )
            messagebox.showinfo(
                "刷新成功",
                "Cookie 已更新并加密保存，下一轮将生效。",
                parent=dialog,
            )
            dialog.destroy()

        btn_row = ttk.Frame(dialog)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_row, text="🖥 Playwright 半自动提取", command=_on_playwright).pack(side="left")
        ttk.Button(btn_row, text="✅ 校验并保存", command=_on_save).pack(side="left", padx=6)
        ttk.Button(btn_row, text="取消", command=dialog.destroy).pack(side="right")

    @staticmethod
    def _cookie_health_label(state: str) -> tuple[str, str]:
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
        # v1.8（C8）：刷新选中 —— 粘贴新 Cookie 替换选中条目（校验后写回内存态）
        ttk.Button(btn_row, text="🔄 刷新选中", command=lambda: _on_refresh_selected()).pack(
            side="left", padx=6
        )
        ttk.Button(btn_row, text="🗑 删除选中", command=lambda: _on_delete()).pack(side="left", padx=6)
        ttk.Button(btn_row, text="⏻ 启用/停用", command=lambda: _on_toggle()).pack(side="left", padx=6)
        # v1.8（C13）：自动停用过期项 —— 确认后写 enabled=false 保留条目
        ttk.Button(btn_row, text="⏹ 自动停用过期项", command=lambda: _on_auto_disable()).pack(
            side="left", padx=6
        )
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
            from ..cookie import TOKEN_TTL_MS, cookie_token_timestamp

            raw = str(cookie_str or "").strip()
            ts = cookie_token_timestamp(raw)
            if ts is None:
                return "未知"
            expire_ms = ts + TOKEN_TTL_MS
            return datetime.fromtimestamp(expire_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

        def _refresh() -> None:
            """重绘列表（对每条 Cookie 检测有效性）。"""
            from ..cookie import detect_cookie_health

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

        def _selected_index() -> int | None:
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

        def _on_refresh_selected() -> None:
            """v1.8（C8）：刷新选中 —— 粘贴新 Cookie 替换选中条目。

            校验（detect_cookie_health 非 ok 拒绝，C15）；通过后更新内存态
            `self._cookie_pool` 并刷新健康列；落盘由主界面「💾 保存配置」统一完成。
            """
            index = _selected_index()
            if index is None:
                messagebox.showinfo("提示", "请先在表格中选中要刷新的条目。", parent=dialog)
                return
            item = self._cookie_pool[index]

            refresh_dialog = tk.Toplevel(dialog)
            refresh_dialog.title(f"🔄 刷新 Cookie - {item.get('name', '')}")
            refresh_dialog.geometry("560x260")
            refresh_dialog.transient(dialog)
            refresh_dialog.resizable(False, False)

            frame = ttk.Frame(refresh_dialog, padding=12)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="粘贴新的 Cookie 请求头（须包含 _m_h5_tk=）：").pack(anchor="w")
            text_cookie = tk.Text(frame, height=7, wrap="char")
            text_cookie.pack(fill="both", expand=True, pady=(2, 6))

            def on_save() -> None:
                from ..cookie import cookie_accept_state, detect_cookie_health

                cookie = text_cookie.get("1.0", "end").strip()
                if not cookie:
                    messagebox.showwarning("Cookie 为空", "请粘贴 Cookie 内容。", parent=refresh_dialog)
                    return
                state, reason = detect_cookie_health(cookie)
                if not cookie_accept_state(state):
                    messagebox.showerror(
                        "校验失败",
                        f"Cookie 无效（{state}）：{reason}\n\n未保存任何改动。",
                        parent=refresh_dialog,
                    )
                    return
                item["cookie"] = cookie
                _refresh()
                refresh_dialog.destroy()
                self._append_log(
                    "INFO",
                    f"✅ 已刷新 Cookie「{item.get('name', '')}」（脱敏：{secure.mask_cookie(cookie)}），"
                    "点击主界面「💾 保存配置」落盘。",
                )

            ttk.Button(frame, text="保存", command=on_save).pack(side="right")
            ttk.Button(frame, text="取消", command=refresh_dialog.destroy).pack(side="right", padx=(0, 6))

        def _on_auto_disable() -> None:
            """v1.8（C13）：自动停用过期项 —— 确认后写 `enabled=false` 保留条目。

            对池中检测为 expired / no_token / missing / invalid_encrypt 的条目
            统一停用（不删除）；写内存态前 `askyesno` 确认，落盘由主界面保存完成。
            """
            from ..cookie import detect_cookie_health

            invalid_indexes = [
                i
                for i, e in enumerate(self._cookie_pool)
                if detect_cookie_health(str(e.get("cookie") or ""))[0]
                not in ("ok", "expiring")
            ]
            if not invalid_indexes:
                messagebox.showinfo("无需处理", "池中没有需要停用的过期条目。", parent=dialog)
                return
            names = "、".join(
                str(self._cookie_pool[i].get("name") or f"#{i + 1}") for i in invalid_indexes
            )
            if not messagebox.askyesno(
                "确认停用",
                f"将停用 {len(invalid_indexes)} 条过期/无效 Cookie：\n{names}\n\n"
                "停用后条目仍保留（enabled=false），可在「⏻ 启用/停用」中恢复。\n\n继续吗？",
                parent=dialog,
            ):
                return
            for i in invalid_indexes:
                self._cookie_pool[i]["enabled"] = False
            _refresh()
            self._append_log(
                "INFO",
                f"已停用 {len(invalid_indexes)} 条过期/无效 Cookie（保留条目），点击主界面保存落盘。",
            )

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
                # v1.8（C22）：本进程保存后更新 mtime 快照
                self._touch_config_mtime()
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
    def _insert_alert_row(self, row: dict[str, Any], to_top: bool = False) -> None:
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
            with contextlib.suppress(Exception): # 测试替身可能不支持 tags
                self.tree_alerts.item(item, tags=("sold",))

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

        ALERT_COLUMNS.index(column)
        rows: list[dict[str, Any]] = []
        for item in self.tree_alerts.get_children(""):
            values = self.tree_alerts.item(item, "values") or ()
            row: dict[str, Any] = {"iid": item}
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
                        "sold": bool(row["sold_out"]) if "sold_out" in row else False,
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
        items: list[dict[str, str]] = []
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
            sold_ids: list[str] = []
            unknown = 0
            online = 0
            try:
                fetcher = build_fetcher(config)
            except Exception as exc:  # noqa: BLE001
                self._push_message("error", "校验失败", f"构造抓取器失败：\n{exc}")
                self._push("log", ("ERROR", f"[{datetime.now():%H:%M:%S}] 校验在架失败（构造抓取器）：{exc}"))
                return
            storage: Storage | None = None
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
                    with contextlib.suppress(Exception):
                        storage.close()
                with contextlib.suppress(Exception):
                    fetcher.close()
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
    def _ask_blacklist_reason(self, title: str) -> str | None:
        """弹出「加入黑名单」原因输入框。

        Args:
            title: 商品标题（展示用）。

        Returns:
            原因字符串；用户点「取消」返回 None。
        """
        try:
            from tkinter import simpledialog
        except ImportError:  # pragma: no cover - 极老 tk 缺失该子模块
            simpledialog = None  # type: ignore[misc,assignment]
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

        def _read_blacklist() -> list[Any]:
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
                hits: list[Product] = []
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
                with contextlib.suppress(Exception):
                    closable.close()
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
                with contextlib.suppress(Exception): # join 异常不影响关闭
                    worker.join(timeout=CLOSE_JOIN_TIMEOUT)

        self._closing = True
        with contextlib.suppress(Exception):   # 窗口销毁等边缘情况
            if getattr(self, "_poll_after_id", None) is not None:
                self.root.after_cancel(self._poll_after_id)
            if getattr(self, "_tick_after_id", None) is not None:
                self.root.after_cancel(self._tick_after_id)

        self._remove_log_handler()
        with contextlib.suppress(tk.TclError):   # pragma: no cover
            self.root.destroy()


# ====================================================================== #
# 入口
# ====================================================================== #
def _notify_instance_conflict(holder_pid: str) -> None:
    """弹「已有实例正在运行」提示（无图形环境时降级为 stderr 打印）。"""
    text = f"已有实例正在运行（PID {holder_pid or '未知'}），请先关闭再启动。"
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("已有实例正在运行", text)
        with contextlib.suppress(Exception):
            root.destroy()
    except Exception:  # noqa: BLE001 - 无图形环境降级打印
        print(text)


def main(config_path: str = "config.yaml") -> int:
    """启动图形界面。

    Args:
        config_path: 配置文件路径。

    Returns:
        进程退出码，0 表示正常退出。
    """
    # windowed 打包 exe 无控制台：安装滚动文件日志便于查错（失败不影响启动）
    with contextlib.suppress(Exception):  # 日志安装失败不影响启动
        from ..cli import install_file_logging

        install_file_logging()

    # v1.8 单实例锁（L5）：检测到已有实例 → 弹中文提示 + 返回非 0，不抢锁。
    # 同进程重复获取幂等（cli.main 已持有时会返回同一对象，不会自锁）。
    lock = acquire_instance_lock()
    if lock is None:
        _notify_instance_conflict(lock_holder_pid())
        return 1

    try:
        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001 - 无图形环境时给出清晰提示
            print(f"无法创建图形界面窗口：{exc}\n请确认当前环境支持 GUI 显示。")
            return 1

        try:
            XianyuAlertGUI(root, config_path=config_path)
        except Exception as exc:  # noqa: BLE001 - 构造失败也要给出提示而非白屏
            logger.exception("图形界面初始化失败：%s", exc)
            with contextlib.suppress(Exception):
                messagebox.showerror("启动失败", f"图形界面初始化失败：\n{exc}")
            with contextlib.suppress(Exception):
                root.destroy()
            return 1

        root.mainloop()
        return 0
    finally:
        release_instance_lock(lock)



__all__ = [
    "QueueLogHandler",
    "XianyuAlertGUI",
    "main",
]
