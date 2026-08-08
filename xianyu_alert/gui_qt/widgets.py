"""Qt 通用控件：日志区 / 状态灯 / 关键词表 / 提醒记录表 / 表单行。

设计对齐（macOS 适配设计文档 §3.1.3）：
    - `LogView(QPlainTextEdit)`：只读；`setMaximumBlockCount(2000)` 自动裁剪；
      按级别 `QTextCharFormat` 高亮（颜色沿用 Tk 版）；字号 8~16pt 可调；
      追加后自动滚动到底；
    - `StatusLight`：彩色圆点 + 文案（Cookie 状态灯）；
    - `KeywordTable` / `AlertTable`：QTableWidget 封装（增删改 / 排序 / 双击）。

**线程铁律**：本模块控件只允许在主线程操作；后台线程仅通过信号投递数据。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..gui import ALERT_COLUMNS, ALERT_HEADING_TEXTS, log_tag_for_text

logger = logging.getLogger(__name__)

#: 日志区最多保留行数（QTextDocument::setMaximumBlockCount 自动裁剪）
MAX_LOG_LINES = 2000
#: 日志字号范围（pt）
LOG_FONT_MIN = 8
LOG_FONT_MAX = 16
LOG_FONT_DEFAULT = 10

#: 日志级别 / 高亮 tag → (颜色, 是否粗体)。颜色沿用 Tk 版（v3.7 配色）。
LOG_LEVEL_STYLES: Dict[str, Tuple[str, bool]] = {
    "INFO": ("#333333", False),
    "DEBUG": ("#888888", False),
    "WARNING": ("#d97706", False),
    "ERROR": ("#dc2626", False),
    "ALERT": ("#059669", True),
    "NEW_ITEM": ("#2563eb", True),
    "SUMMARY": ("#059669", True),
    "ROUND": ("#6d28d9", True),
    "DIM": ("#9ca3af", False),
}


class LogView(QPlainTextEdit):
    """只读日志区：自动裁剪 + 按级别高亮 + 字号可调。

    `append_log(level, text)` 为主线程槽函数（由 Qt 信号跨线程 queued 投递调用），
    后台线程绝不直接调用本方法。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.document().setMaximumBlockCount(MAX_LOG_LINES)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._font_size = LOG_FONT_DEFAULT
        font = QFont()
        font.setPointSize(self._font_size)
        font.setFamily("Menlo")  # macOS 等宽字体；Windows/Linux 自动回退
        self.setFont(font)
        self._formats: Dict[str, QTextCharFormat] = self._build_formats()

    def _build_formats(self) -> Dict[str, QTextCharFormat]:
        """预构建各级别/标签的 QTextCharFormat（避免每次追加重建）。"""
        formats: Dict[str, QTextCharFormat] = {}
        for level, (color, bold) in LOG_LEVEL_STYLES.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Bold)
            formats[level] = fmt
        return formats

    def append_log(self, level: str, text: str) -> None:
        """按级别着色追加一行日志并自动滚动到底（主线程调用）。"""
        tag = log_tag_for_text(str(level or "INFO"), str(text or ""))
        fmt = self._formats.get(tag, self._formats["INFO"])
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(str(text or "") + "\n", fmt)
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def set_font_size(self, delta: int) -> None:
        """调整日志字号（8~16pt），立即生效。"""
        self._font_size = max(LOG_FONT_MIN, min(LOG_FONT_MAX, self._font_size + delta))
        font = self.font()
        font.setPointSize(self._font_size)
        self.setFont(font)

    def clear_log(self) -> None:
        """清空日志区。"""
        self.clear()


class StatusLight(QWidget):
    """状态灯：彩色圆点 + 文案（用于 Cookie 状态等）。"""

    def __init__(self, text: str = "", color: str = "#9ca3af", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._dot = QLabel("●")
        self._label = QLabel(str(text or ""))
        self._label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._dot)
        layout.addWidget(self._label, 1)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        """设置圆点颜色（如 '#059669'）。"""
        self._dot.setStyleSheet(f"color: {color}; font-size: 14px;")

    def set_text(self, text: str) -> None:
        """设置文案。"""
        self._label.setText(str(text or ""))


class KeywordTable(QTableWidget):
    """关键词表：列 = 关键词 / 价格阈值 / 过滤规则 / 状态。

    Signals:
        row_double_clicked(str): 双击某行，携带关键词文本（触发编辑）。
    """

    row_double_clicked = Signal(str)
    COL_KEYWORD = 0
    COL_PRICE = 1
    COL_FILTER = 2
    COL_STATUS = 3

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["关键词", "价格阈值（元）", "过滤规则", "状态"])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(self.COL_KEYWORD, QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(self.COL_PRICE, QHeaderView.ResizeToContents)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.cellDoubleClicked.connect(self._on_cell_double_clicked)
        #: 行号 -> 关键词（删除/更新时定位）
        self._rows: List[str] = []

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        if 0 <= row < len(self._rows):
            self.row_double_clicked.emit(self._rows[row])

    def set_keywords(
        self,
        keywords: Sequence[Tuple[str, float]],
        enabled_map: Optional[Dict[str, bool]] = None,
        filters: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """整体刷新表格内容（加载配置时调用）。"""
        self.setRowCount(0)
        self._rows = []
        from ..gui import keyword_filter_summary, keyword_status_text

        for kw, price in keywords or []:
            enabled = bool((enabled_map or {}).get(kw, True))
            state = (filters or {}).get(kw) or {}
            summary = keyword_filter_summary(state)
            self._append_row(kw, price, summary, keyword_status_text(enabled))
            self._rows.append(kw)

    def _append_row(self, kw: str, price: float, summary: str, status: str) -> None:
        row = self.rowCount()
        self.insertRow(row)
        items = [
            QTableWidgetItem(str(kw)),
            QTableWidgetItem(f"{float(price):.2f}"),
            QTableWidgetItem(str(summary)),
            QTableWidgetItem(str(status)),
        ]
        for col, item in enumerate(items):
            if col == self.COL_PRICE:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.setItem(row, col, item)

    def selected_keyword(self) -> Optional[str]:
        """返回当前选中行的关键词；未选中返回 None。"""
        row = self.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def row_of(self, keyword: str) -> int:
        """返回关键词所在行号；不存在返回 -1。"""
        try:
            return self._rows.index(keyword)
        except ValueError:
            return -1

    def update_row(self, old_keyword: str, new_keyword: str, price: float,
                   enabled: bool, summary: str) -> bool:
        """更新某关键词所在行；成功返回 True。"""
        row = self.row_of(old_keyword)
        if row < 0:
            return False
        from ..gui import keyword_status_text

        self._rows[row] = new_keyword
        self.setItem(row, self.COL_KEYWORD, QTableWidgetItem(str(new_keyword)))
        price_item = QTableWidgetItem(f"{float(price):.2f}")
        price_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setItem(row, self.COL_PRICE, price_item)
        self.setItem(row, self.COL_FILTER, QTableWidgetItem(str(summary)))
        self.setItem(row, self.COL_STATUS, QTableWidgetItem(keyword_status_text(enabled)))
        return True

    def remove_keyword(self, keyword: str) -> bool:
        """删除某关键词行；成功返回 True。"""
        row = self.row_of(keyword)
        if row < 0:
            return False
        self.removeRow(row)
        self._rows.pop(row)
        return True

    def all_keywords(self) -> List[str]:
        """返回全部关键词（按表格顺序）。"""
        return list(self._rows)

    def set_enabled_state(self, keyword: str, enabled: bool) -> bool:
        """更新某关键词的启用状态列；成功返回 True。"""
        row = self.row_of(keyword)
        if row < 0:
            return False
        from ..gui import keyword_status_text

        self.setItem(row, self.COL_STATUS, QTableWidgetItem(keyword_status_text(enabled)))
        return True


class AlertTable(QTableWidget):
    """提醒记录表：列 = 提醒时间 / 关键词 / 商品名称 / 价格 / 发布时间。

    Signals:
        row_double_clicked(int): 双击某行（打开商品链接）。
        sort_requested(str): 点击表头（请求按列排序）。
    """

    row_double_clicked = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        cols = len(ALERT_COLUMNS)
        super().__init__(0, cols, parent)
        #: 行号 -> 记录字典（含 iid/product_id/url，供双击/右键操作）。
        #: 必须是**实例属性**（与 KeywordTable._rows 一致）：若为类属性，
        #: 多窗口/重复构造主窗口会共享提醒记录（Bug #3，QA 回归用例覆盖）。
        self._rows: List[Dict[str, Any]] = []
        self.setHorizontalHeaderLabels([ALERT_HEADING_TEXTS[c] for c in ALERT_COLUMNS])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(ALERT_COLUMNS.index("title"), QHeaderView.Stretch)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.cellDoubleClicked.connect(lambda row, _col: self.row_double_clicked.emit(row))
        self.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        #: 列名 -> 当前排序方向（True=升序）
        self._sort_state: Dict[str, bool] = {col: True for col in ALERT_COLUMNS}

    def _on_header_clicked(self, index: int) -> None:
        if 0 <= index < len(ALERT_COLUMNS):
            column = ALERT_COLUMNS[index]
            ascending = self._sort_state.get(column, True)
            self._sort_state[column] = not ascending
            self.sort_by(column, ascending)

    def sort_by(self, column: str, ascending: bool = True) -> None:
        """按列排序（复用 gui.sort_alert_rows 纯函数）。"""
        from ..gui import sort_alert_rows

        sorted_rows = sort_alert_rows(list(self._rows), column, ascending)
        self._rows = sorted_rows
        self._render()

    def _render(self) -> None:
        self.setRowCount(0)
        for row_data in self._rows:
            row = self.rowCount()
            self.insertRow(row)
            values = [
                str(row_data.get("time", "") or ""),
                str(row_data.get("keyword", "") or ""),
                str(row_data.get("title", "") or ""),
                str(row_data.get("price", "") or ""),
                str(row_data.get("publish", "") or ""),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == ALERT_COLUMNS.index("price"):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if row_data.get("sold"):
                    item.setForeground(QColor("#9ca3af"))  # 已售出/下架置灰
                self.setItem(row, col, item)

    def replace_rows(self, rows: Sequence[Dict[str, Any]]) -> None:
        """整体替换记录（加载历史时调用）。"""
        self._rows = [dict(r) for r in rows]
        self._render()

    def append_row(self, row: Dict[str, Any], to_top: bool = False) -> None:
        """追加一条记录（to_top=True 插到顶部）。"""
        entry = dict(row)
        if to_top:
            self._rows.insert(0, entry)
        else:
            self._rows.append(entry)
        self._render()

    def remove_row(self, row_index: int) -> Optional[Dict[str, Any]]:
        """删除某行并返回其记录；越界返回 None。"""
        if not (0 <= row_index < len(self._rows)):
            return None
        return self._rows.pop(row_index)

    def row_data(self, row_index: int) -> Optional[Dict[str, Any]]:
        """返回某行记录字典；越界返回 None。"""
        if not (0 <= row_index < len(self._rows)):
            return None
        return dict(self._rows[row_index])

    def all_rows(self) -> List[Dict[str, Any]]:
        """返回全部记录（深拷贝，供排序/校验等只读使用）。"""
        return [dict(r) for r in self._rows]

    def clear_rows(self) -> None:
        self._rows = []
        self.setRowCount(0)

    def row_count(self) -> int:
        return len(self._rows)


def make_form_row(parent: QWidget, label: str, widget: QWidget) -> QHBoxLayout:
    """构造「标签 + 控件」一行（FormRow 快捷方式，对齐 macOS 留白节奏）。

    Args:
        parent: 父控件（布局由调用方 addLayout）。
        label: 左侧标签文案。
        widget: 右侧控件。

    Returns:
        水平布局（label + widget）。
    """
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    lbl = QLabel(str(label))
    layout.addWidget(lbl)
    layout.addWidget(widget, 1)
    return layout
