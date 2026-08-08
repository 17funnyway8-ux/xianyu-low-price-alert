"""Qt 监控配置页：关键词表 + 监测设置 + Cookie 管理。

设计对齐（macOS 适配设计文档 §3.1.1 表：监控配置页）：
    - 关键词表（QTableWidget）：添加 / 更新选中 / 删除 / 启用停用 / 过滤词编辑 /
      预置词编辑，复用 gui.py 纯函数（validate_keyword_entry / apply_filter_edit /
      keyword_filter_summary / resolve_preset_exclude_keywords 等）；
    - 监测设置：抓取方式 / 间隔 / 页数 / User-Agent；
    - Cookie 状态灯 + 「Cookie 管理」对话框（粘贴模式，多账号池）；
    - **macOS 不渲染「创建桌面快捷方式」按钮**（shortcut.supported() 判断）；
    - 「保存配置」按钮通过 `save_requested` 信号交给主窗口统一处理。

本页签只做视图层 + 表单收集，**不直接触碰 monitor/storage/notifier**。
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..gui import (
    CHANNEL_ORDER,
    DEFAULT_DB_PATH,
    FETCHER_CHOICES,
    apply_filter_edit,
    cookie_status,
    fetcher_label,
    fetcher_type_from_label,
    keyword_filter_summary,
    resolve_preset_exclude_keywords,
    validate_interval,
    validate_keyword_entry,
    validate_pages,
)
from ..shortcut import supported as shortcut_supported
from .dialogs import CookieDialog, KeywordEditDialog, PresetWordsDialog
from .widgets import KeywordTable, StatusLight

logger = logging.getLogger(__name__)


class MonitorConfigTab(QWidget):
    """监控配置页签（Qt 版）。

    Signals:
        save_requested(): 用户点击「保存配置」（主窗口统一执行保存）。
        cookie_changed(): Cookie 池 / 单值 Cookie 被修改（主窗口刷新状态灯）。
    """

    save_requested = Signal()
    cookie_changed = Signal()

    def __init__(self, form: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._form = dict(form or {})
        #: 运行时状态（从 form 初始化）
        self._keywords: List[Tuple[str, float]] = list(self._form.get("keywords") or [])
        self._keyword_enabled: Dict[str, bool] = dict(self._form.get("keyword_enabled") or {})
        self._keyword_filters: Dict[str, Dict[str, List[str]]] = dict(
            self._form.get("keyword_filters") or {}
        )
        self._preset_exclude_keywords: List[str] = resolve_preset_exclude_keywords(
            self._form.get("preset_exclude_keywords")
        )
        self._cookies: str = str(self._form.get("cookies", "") or "")
        self._cookies_undecryptable: bool = bool(self._form.get("cookies_undecryptable", False))
        self._cookie_pool: List[Dict[str, Any]] = [
            dict(e) for e in (self._form.get("cookie_pool") or [])
        ]
        self._storage_path: str = str(self._form.get("storage_path") or DEFAULT_DB_PATH)

        self._build_ui()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ---- 关键词表 ----
        group_keywords = QGroupBox("关键词与价格阈值")
        kw_layout = QVBoxLayout(group_keywords)
        self.table_keywords = KeywordTable()
        self.table_keywords.row_double_clicked.connect(self._on_double_click_edit)
        kw_layout.addWidget(self.table_keywords, 1)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("➕ 添加")
        self.btn_update = QPushButton("✏️ 更新选中")
        self.btn_delete = QPushButton("🗑 删除选中")
        self.btn_toggle = QPushButton("⏯ 启用/停用")
        self.btn_filters = QPushButton("🛠 编辑过滤词")
        self.btn_preset = QPushButton("📋 编辑预置词")
        for btn in (self.btn_add, self.btn_update, self.btn_delete, self.btn_toggle,
                    self.btn_filters, self.btn_preset):
            btn_row.addWidget(btn)
        self.btn_add.clicked.connect(self._on_add_keyword)
        self.btn_update.clicked.connect(self._on_update_keyword)
        self.btn_delete.clicked.connect(self._on_delete_keyword)
        self.btn_toggle.clicked.connect(self._on_toggle_keyword)
        self.btn_filters.clicked.connect(self._on_edit_filters)
        self.btn_preset.clicked.connect(self._on_edit_preset)
        kw_layout.addLayout(btn_row)
        root.addWidget(group_keywords, 3)

        # ---- 监测设置 ----
        group_monitor = QGroupBox("监测设置")
        mon_form = QFormLayout(group_monitor)
        self.combo_fetcher = QComboBox()
        for _value, label in FETCHER_CHOICES:
            self.combo_fetcher.addItem(label)
        self.combo_fetcher.setCurrentText(fetcher_label(self._form.get("fetcher_type", "mtop")))
        mon_form.addRow("抓取方式：", self.combo_fetcher)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(1, 86400)
        self.spin_interval.setValue(int(self._form.get("interval", 600)))
        self.spin_interval.setSuffix(" 秒")
        mon_form.addRow("监测间隔：", self.spin_interval)

        self.spin_pages = QSpinBox()
        self.spin_pages.setRange(1, 10)
        self.spin_pages.setValue(int(self._form.get("pages", 1)))
        mon_form.addRow("抓取页数：", self.spin_pages)

        self.edit_user_agent = QLineEdit(str(self._form.get("user_agent", "") or ""))
        self.edit_user_agent.setPlaceholderText("留空使用内置默认 User-Agent")
        mon_form.addRow("User-Agent：", self.edit_user_agent)
        root.addWidget(group_monitor)

        # ---- Cookie 状态 + 管理 ----
        group_cookie = QGroupBox("Cookie（mtop 真实抓取必需）")
        cookie_layout = QHBoxLayout(group_cookie)
        self.light_cookie = StatusLight("")
        cookie_layout.addWidget(self.light_cookie, 1)
        self.btn_cookies = QPushButton("🍪 Cookie 管理…")
        self.btn_cookies.clicked.connect(self._on_manage_cookies)
        cookie_layout.addWidget(self.btn_cookies)
        root.addWidget(group_cookie)

        # ---- 底部按钮行（保存 / 快捷方式） ----
        bottom_row = QHBoxLayout()
        self.btn_save = QPushButton("💾 保存配置")
        self.btn_save.clicked.connect(self.save_requested.emit)
        bottom_row.addWidget(self.btn_save)
        # macOS：不渲染「创建桌面快捷方式」（.app 拖入「应用程序」即用）
        if shortcut_supported():
            self.btn_shortcut = QPushButton("🖥 创建桌面快捷方式")
            self.btn_shortcut.clicked.connect(self._on_create_shortcut)
            bottom_row.addWidget(self.btn_shortcut)
        else:
            self.btn_shortcut = None  # type: ignore[assignment]
        bottom_row.addStretch(1)
        root.addLayout(bottom_row)

        self._refresh_table()
        self._refresh_cookie_light()

    # ------------------------------------------------------------------ #
    def _refresh_table(self) -> None:
        """把内部关键词状态刷新到表格。"""
        self.table_keywords.set_keywords(self._keywords, self._keyword_enabled, self._keyword_filters)

    def _refresh_cookie_light(self) -> None:
        """刷新 Cookie 状态灯（六态，复用 gui.cookie_status 纯函数）。"""
        if self._cookies_undecryptable:
            self.light_cookie.set_text("❌ Cookie 无法解密（可能换机/换用户），请重新登录")
            self.light_cookie.set_color("#dc2626")
            return
        state, text = cookie_status(self._cookies)
        colors = {
            "missing": "#9ca3af",
            "no_token": "#d97706",
            "expired": "#dc2626",
            "expiring": "#d97706",
            "undecryptable": "#dc2626",
            "ok": "#059669",
        }
        self.light_cookie.set_text(text)
        self.light_cookie.set_color(colors.get(state, "#9ca3af"))

    # ------------------------------------------------------------------ #
    # 关键词操作
    # ------------------------------------------------------------------ #
    def _on_add_keyword(self) -> None:
        dlg = KeywordEditDialog(parent=self)
        if dlg.exec() != KeywordEditDialog.Accepted:
            return
        kw, price, excludes, requireds = dlg.result()
        if kw in [k for k, _ in self._keywords]:
            QMessageBox.warning(self, "重复关键词", f"关键词「{kw}」已存在，请直接更新选中。")
            return
        # 新关键词自动预置排除词（v3.5）
        merged_excludes = list(excludes)
        for preset in self._preset_exclude_keywords:
            if preset not in merged_excludes:
                merged_excludes.append(preset)
        self._keywords.append((kw, price))
        self._keyword_enabled[kw] = True
        self._keyword_filters[kw] = {
            "exclude_keywords": merged_excludes,
            "required_keywords": requireds,
        }
        self._refresh_table()

    def _on_update_keyword(self) -> None:
        selected = self.table_keywords.selected_keyword()
        if selected is None:
            QMessageBox.information(self, "提示", "请先在表格中选择一个关键词。")
            return
        old_filters = self._keyword_filters.get(selected) or {}
        dlg = KeywordEditDialog(
            keyword=selected,
            price=dict(self._keywords)[selected] if selected in dict(self._keywords) else 0.0,
            exclude=old_filters.get("exclude_keywords") or [],
            required=old_filters.get("required_keywords") or [],
            parent=self,
        )
        if dlg.exec() != KeywordEditDialog.Accepted:
            return
        kw, price, excludes, requireds = dlg.result()
        # 重名处理：改成另一个已有关键词 → 拒绝
        existing = [k for k, _ in self._keywords if k != selected]
        if kw in existing:
            QMessageBox.warning(self, "重复关键词", f"关键词「{kw}」已存在。")
            return
        self._keywords = [(kw, price) if k == selected else (k, p) for k, p in self._keywords]
        self._keyword_enabled[kw] = self._keyword_enabled.pop(selected, True)
        self._keyword_filters[kw] = {
            "exclude_keywords": excludes,
            "required_keywords": requireds,
        }
        self._keyword_filters.pop(selected, None)
        self.table_keywords.update_row(
            selected, kw, price,
            enabled=self._keyword_enabled.get(kw, True),
            summary=keyword_filter_summary(self._keyword_filters.get(kw) or {}),
        )

    def _on_delete_keyword(self) -> None:
        selected = self.table_keywords.selected_keyword()
        if selected is None:
            QMessageBox.information(self, "提示", "请先在表格中选择一个关键词。")
            return
        if QMessageBox.question(self, "确认删除", f"确定删除关键词「{selected}」吗？") != QMessageBox.Yes:
            return
        self._keywords = [(k, p) for k, p in self._keywords if k != selected]
        self._keyword_enabled.pop(selected, None)
        self._keyword_filters.pop(selected, None)
        self.table_keywords.remove_keyword(selected)

    def _on_toggle_keyword(self) -> None:
        selected = self.table_keywords.selected_keyword()
        if selected is None:
            QMessageBox.information(self, "提示", "请先在表格中选择一个关键词。")
            return
        enabled = not self._keyword_enabled.get(selected, True)
        self._keyword_enabled[selected] = enabled
        self.table_keywords.set_enabled_state(selected, enabled)

    def _on_double_click_edit(self, keyword: str) -> None:
        """双击关键词行 → 直接进入更新编辑（等价「更新选中」）。"""
        self.table_keywords.setCurrentCell(
            max(0, self.table_keywords.row_of(keyword)), 0
        )
        self._on_update_keyword()

    def _on_edit_filters(self) -> None:
        selected = self.table_keywords.selected_keyword()
        if selected is None:
            QMessageBox.information(self, "提示", "请先在表格中选择一个关键词。")
            return
        current = self._keyword_filters.get(selected) or {}
        dlg = KeywordEditDialog(
            keyword=selected,
            price=dict(self._keywords).get(selected, 0.0),
            exclude=current.get("exclude_keywords") or [],
            required=current.get("required_keywords") or [],
            parent=self,
        )
        if dlg.exec() != KeywordEditDialog.Accepted:
            return
        kw, price, excludes, requireds = dlg.result()
        # 编辑过滤词仅更新过滤规则；关键词/价格保持不变
        self._keyword_filters[kw] = {
            "exclude_keywords": excludes,
            "required_keywords": requireds,
        }
        self.table_keywords.update_row(
            kw, price,
            enabled=self._keyword_enabled.get(kw, True),
            summary=keyword_filter_summary(self._keyword_filters[kw]),
        )

    def _on_edit_preset(self) -> None:
        dlg = PresetWordsDialog(self._preset_exclude_keywords, parent=self)
        if dlg.exec() != PresetWordsDialog.Accepted:
            return
        self._preset_exclude_keywords = dlg.preset_words()

    # ------------------------------------------------------------------ #
    # Cookie 管理
    # ------------------------------------------------------------------ #
    def _on_manage_cookies(self) -> None:
        dlg = CookieDialog(
            cookie_pool=self._cookie_pool,
            single_cookie=self._cookies,
            parent=self,
        )
        if dlg.exec() != CookieDialog.Accepted:
            return
        self._cookie_pool = dlg.result_pool()
        self._cookies = dlg.result_single_cookie()
        self._cookies_undecryptable = False
        self._refresh_cookie_light()
        self.cookie_changed.emit()

    def _on_create_shortcut(self) -> None:
        """创建桌面快捷方式（仅 Windows；macOS 不渲染该按钮）。"""
        from ..shortcut import create_shortcut

        result = create_shortcut()
        if result:
            QMessageBox.information(self, "已创建", f"桌面快捷方式：\n{result}")
        else:
            QMessageBox.warning(self, "创建失败", "创建桌面快捷方式失败，请查看日志。")

    # ------------------------------------------------------------------ #
    # 表单收集
    # ------------------------------------------------------------------ #
    def collect_config(self) -> Dict[str, Any]:
        """收集本页签全部表单状态（供主窗口组装配置字典）。

        Returns:
            含 keywords / keyword_enabled / keyword_filters / interval /
            fetcher_type / cookies / user_agent / storage_path / pages /
            cookie_pool / preset_exclude_keywords 的字典。
        """
        return {
            "keywords": list(self._keywords),
            "keyword_enabled": dict(self._keyword_enabled),
            "keyword_filters": {k: dict(v) for k, v in self._keyword_filters.items()},
            "interval": int(self.spin_interval.value()),
            "fetcher_type": fetcher_type_from_label(self.combo_fetcher.currentText()),
            "cookies": self._cookies,
            "user_agent": self.edit_user_agent.text().strip(),
            "storage_path": self._storage_path,
            "pages": int(self.spin_pages.value()),
            "cookie_pool": [dict(e) for e in self._cookie_pool],
            "preset_exclude_keywords": list(self._preset_exclude_keywords),
        }
