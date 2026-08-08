"""Qt 运行监控页：控制区 + 状态行 + 提醒记录表 + 日志区。

设计对齐（macOS 适配设计文档 §3.1.1 表：运行监控页）：
    - 控制按钮：开始 / 停止 / 立即执行一轮 / 清空日志 / 清空记录 / 字号±；
    - 状态行：当前状态 / 轮次 / 累计提醒 / 下次倒计时；
    - 提醒记录表：双击打开链接 / 点击表头排序 / 右键菜单（标记售出·恢复在架 /
      校验在架 / 加入黑名单 / 打开链接），表格与 Tk 版「隐藏已下架」语义一致；
    - 日志区：`LogView`（2000 行自动裁剪 + 分级高亮 + 字号可调）；
    - 「仅展示符合的低价」勾选：主线程读取为普通 bool 传给后台线程。

本页签只做视图层：按钮通过信号交给主窗口处理；表格右键操作通过信号上抛
（涉及 Storage / 网络的操作由主窗口执行，保持「控件只在主线程被触碰」铁律）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..gui import ALERT_COLUMNS, format_countdown
from .widgets import AlertTable, LogView

logger = logging.getLogger(__name__)


class RunMonitorTab(QWidget):
    """运行监控页签（Qt 版）。

    Signals:
        start_requested(): 点击「开始监控」。
        stop_requested(): 点击「停止」。
        run_once_requested(): 点击「立即执行一轮」。
        clear_records_requested(): 点击「清空记录」。
        check_shelf_requested(list): 点击「校验在架」（携带待校验行记录）。
        blacklist_requested(dict): 右键「加入黑名单」（携带行记录）。
        sold_toggle_requested(dict): 右键「标记售出/恢复在架」（携带行记录）。
        open_url_requested(dict): 双击 / 右键「打开链接」（携带行记录）。
    """

    start_requested = Signal()
    stop_requested = Signal()
    run_once_requested = Signal()
    clear_records_requested = Signal()
    check_shelf_requested = Signal(list)
    blacklist_requested = Signal(dict)
    sold_toggle_requested = Signal(dict)
    open_url_requested = Signal(dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._running = False
        self._mode = ""
        self._rounds = 0
        self._alerts = 0
        self._next_run_at = 0.0
        self._build_ui()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ---- 控制按钮行 ----
        ctrl = QHBoxLayout()
        self.btn_start = QPushButton("▶️ 开始监控")
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_once = QPushButton("⚡ 立即执行一轮")
        self.btn_clear_log = QPushButton("🧹 清空日志")
        self.btn_clear_records = QPushButton("🗑 清空记录")
        self.btn_font_plus = QPushButton("A+")
        self.btn_font_minus = QPushButton("A-")
        for btn in (self.btn_start, self.btn_stop, self.btn_once, self.btn_clear_log,
                    self.btn_clear_records, self.btn_font_plus, self.btn_font_minus):
            ctrl.addWidget(btn)
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_requested.emit)
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_once.clicked.connect(self.run_once_requested.emit)
        self.btn_clear_log.clicked.connect(self._clear_log)
        self.btn_clear_records.clicked.connect(self.clear_records_requested.emit)
        self.btn_font_plus.clicked.connect(lambda: self._font_delta(1))
        self.btn_font_minus.clicked.connect(lambda: self._font_delta(-1))
        root.addLayout(ctrl)

        # ---- 状态行 ----
        status_row = QHBoxLayout()
        self.lbl_state = QLabel("状态：未运行")
        self.lbl_rounds = QLabel("轮次：0")
        self.lbl_alerts = QLabel("累计提醒：0")
        self.lbl_countdown = QLabel("下次：--:--")
        self.check_detail_only = QCheckBox("仅展示符合的低价")
        self.check_detail_only.setChecked(True)
        self.check_detail_only.setToolTip("关闭后，日志会逐条展示抓取到的全部商品明细（信息量更大）")
        for lbl in (self.lbl_state, self.lbl_rounds, self.lbl_alerts, self.lbl_countdown):
            status_row.addWidget(lbl)
        status_row.addStretch(1)
        status_row.addWidget(self.check_detail_only)
        root.addLayout(status_row)

        # ---- 提醒记录表 ----
        self.table_alerts = AlertTable()
        self.table_alerts.row_double_clicked.connect(self._on_row_double_clicked)
        self.table_alerts.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_alerts.customContextMenuRequested.connect(self._show_context_menu)
        root.addWidget(self.table_alerts, 3)

        # ---- 日志区 ----
        self.log_view = LogView()
        root.addWidget(self.log_view, 4)

    # ------------------------------------------------------------------ #
    # 槽函数（主线程调用；由 app 的消息分发 / 信号连接触发）
    # ------------------------------------------------------------------ #
    def append_log(self, level: str, text: str) -> None:
        """追加一条日志（主线程槽函数）。"""
        self.log_view.append_log(level, text)

    def append_alert(self, record: Dict[str, Any]) -> None:
        """追加一条提醒记录到表格（主线程槽函数，插到顶部）。"""
        entry = dict(record)
        entry.setdefault("sold", False)
        self.table_alerts.append_row(entry, to_top=True)
        self._alerts += 1
        self.lbl_alerts.setText(f"累计提醒：{self._alerts}")

    def set_running(self, running: bool) -> None:
        """更新运行状态（按钮可用性 / 状态行）。"""
        self._running = bool(running)
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_once.setEnabled(not running)
        self.lbl_state.setText("状态：监控运行中…" if running else "状态：未运行")

    def update_stats(self, rounds: int, alerts: int) -> None:
        """更新轮次 / 累计提醒（主线程槽函数）。"""
        self._rounds = int(rounds)
        self._alerts = int(alerts)
        self.lbl_rounds.setText(f"轮次：{self._rounds}")
        self.lbl_alerts.setText(f"累计提醒：{self._alerts}")

    def refresh_status(self, now_monotonic: float = 0.0) -> None:
        """刷新倒计时（QTimer 每秒调用）。"""
        if not self._running:
            self.lbl_countdown.setText("下次：--:--")
            return
        remaining = self._next_run_at - (now_monotonic or 0.0)
        self.lbl_countdown.setText(f"下次：{format_countdown(remaining)}")

    def set_next_run_at(self, value: float) -> None:
        """记录下次运行时间戳（monotonic）。"""
        self._next_run_at = float(value)

    def detail_only(self) -> bool:
        """读取「仅展示符合的低价」勾选（主线程读取为普通 bool）。"""
        return self.check_detail_only.isChecked()

    def current_alert_rows(self) -> List[Dict[str, Any]]:
        """返回当前展示的提醒记录行（校验在架用，主线程读取）。"""
        return self.table_alerts.all_rows()

    def reload_alerts(self, rows: List[Dict[str, Any]]) -> None:
        """整体重载提醒记录（校验在架 / 标记售出后由主窗口调用）。"""
        self.table_alerts.replace_rows(rows)

    def show_sold_toggle_hint(self, text: str) -> None:
        """显示「显示已下架」开关提示（占位，简单版本不实现切换显示）。"""
        logger.debug("show_sold_toggle_hint: %s", text)

    # ------------------------------------------------------------------ #
    def _clear_log(self) -> None:
        self.log_view.clear_log()

    def _font_delta(self, delta: int) -> None:
        self.log_view.set_font_size(delta)

    def _on_row_double_clicked(self, row_index: int) -> None:
        row = self.table_alerts.row_data(row_index)
        if row is not None:
            self.open_url_requested.emit(row)

    def _show_context_menu(self, pos) -> None:
        """右键菜单：打开链接 / 标记售出·恢复在架 / 校验在架 / 加入黑名单。"""
        row_index = self.table_alerts.rowAt(pos.y())
        if row_index < 0:
            return
        row = self.table_alerts.row_data(row_index)
        if row is None:
            return
        menu = QMenu(self)
        act_open = QAction("🔗 打开链接", self)
        act_sold = QAction("🗑 标记已售出/下架" if not row.get("sold") else "↩️ 恢复在架", self)
        act_check = QAction("🔍 校验在架", self)
        act_black = QAction("🚫 加入黑名单", self)
        act_open.triggered.connect(lambda: self.open_url_requested.emit(row))
        act_sold.triggered.connect(lambda: self.sold_toggle_requested.emit(row))
        act_check.triggered.connect(lambda: self.check_shelf_requested.emit([row]))
        act_black.triggered.connect(lambda: self.blacklist_requested.emit(row))
        menu.addAction(act_open)
        menu.addAction(act_sold)
        menu.addAction(act_check)
        menu.addAction(act_black)
        menu.exec(self.table_alerts.viewport().mapToGlobal(pos))
