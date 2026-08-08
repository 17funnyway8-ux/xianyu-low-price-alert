"""Qt 通知设置页：6 通道卡片（console / serverchan / email / telegram / bark / webhook）。

设计对齐（macOS 适配设计文档 §3.1.1 表：通知设置页）：
    - 每通道一张 `QGroupBox` 卡片：启用勾选 + 字段表单（密码框回显）+ 测试按钮；
    - 复用 gui.py 常量（CHANNEL_ORDER / CHANNEL_LABELS / CHANNEL_FIELDS）与纯函数
      （normalize_channel_options / channel_is_complete / default_channel_options）；
    - 「测试发送」通过 `test_requested(str)` 信号交给主窗口统一执行
      （后台线程发送，绝不阻塞 UI）。

本页签只做视图层 + 表单收集，不直接触碰 notifier。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..gui import CHANNEL_FIELDS, CHANNEL_LABELS, CHANNEL_ORDER

logger = logging.getLogger(__name__)


class NotifyConfigTab(QWidget):
    """通知设置页签（Qt 版）。

    Signals:
        test_requested(str): 用户点击某通道「测试发送」（携带通道类型）。
    """

    test_requested = Signal(str)

    def __init__(self, form: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        form = dict(form or {})
        #: {ctype: {"enabled": bool, "options": {field: str}}}
        self._channels: Dict[str, Dict[str, Any]] = {}
        raw_channels = form.get("channels") or {}
        for ctype in CHANNEL_ORDER:
            state = dict(raw_channels.get(ctype) or {})
            self._channels[ctype] = {
                "enabled": bool(state.get("enabled", False)),
                "options": dict(state.get("options") or {}),
            }
        if not any(v["enabled"] for v in self._channels.values()):
            self._channels["console"]["enabled"] = True

        #: ctype -> (启用勾选框, {field: QLineEdit})
        self._widgets: Dict[str, tuple] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        for ctype in CHANNEL_ORDER:
            state = self._channels[ctype]
            card = QGroupBox(CHANNEL_LABELS.get(ctype, ctype))
            card_layout = QVBoxLayout(card)

            check = QCheckBox("启用该通道")
            check.setChecked(bool(state["enabled"]))
            card_layout.addWidget(check)

            form = QFormLayout()
            edits: Dict[str, QLineEdit] = {}
            fields = CHANNEL_FIELDS.get(ctype, ())
            for field_name, label, secret, default in fields:
                edit = QLineEdit(str(state["options"].get(field_name, "")))
                if secret:
                    edit.setEchoMode(QLineEdit.Password)
                edit.setPlaceholderText(default)
                form.addRow(f"{label}：", edit)
                edits[field_name] = edit
            card_layout.addLayout(form)

            btn_row = QHBoxLayout()
            btn_row.addStretch(1)
            btn_test = QPushButton("🧪 测试发送")
            btn_test.setEnabled(bool(state["enabled"]))
            btn_test.clicked.connect(lambda _=False, c=ctype: self.test_requested.emit(c))
            btn_row.addWidget(btn_test)
            card_layout.addLayout(btn_row)

            check.toggled.connect(
                lambda on, c=ctype, b=btn_test: self._on_enabled_toggled(c, on, b)
            )
            self._widgets[ctype] = (check, edits)
            root.addWidget(card)

        root.addStretch(1)

    def _on_enabled_toggled(self, ctype: str, enabled: bool, btn_test: QPushButton) -> None:
        """通道启用勾选联动「测试发送」按钮可用性。"""
        self._channels[ctype]["enabled"] = enabled
        btn_test.setEnabled(enabled)

    # ------------------------------------------------------------------ #
    def collect_channels(self) -> Dict[str, Dict[str, Any]]:
        """收集全部通道状态（启用 + 字段值）。

        Returns:
            {ctype: {"enabled": bool, "options": {field: str}}}。
        """
        result: Dict[str, Dict[str, Any]] = {}
        for ctype in CHANNEL_ORDER:
            check, edits = self._widgets[ctype]
            options: Dict[str, str] = {}
            for field_name, edit in edits.items():
                text = edit.text().strip()
                if text:
                    options[field_name] = text
            result[ctype] = {"enabled": check.isChecked(), "options": options}
        return result

    def channel_options(self, ctype: str) -> Dict[str, str]:
        """返回某通道当前字段值（测试发送用）。"""
        check, edits = self._widgets.get(ctype, (None, {}))
        options: Dict[str, str] = {}
        for field_name, edit in edits.items():
            text = edit.text().strip()
            if text:
                options[field_name] = text
        return options

    def is_enabled(self, ctype: str) -> bool:
        """返回某通道是否启用。"""
        check, _edits = self._widgets.get(ctype, (None, {}))
        return bool(check.isChecked()) if check is not None else False
