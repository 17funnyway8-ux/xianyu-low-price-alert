"""Qt 对话框家族：Cookie 管理 / 关键词编辑 / 预置词编辑 / 通道字段 / 黑名单原因。

设计对齐（macOS 适配设计文档 §3.1.1）：
    - `CookieDialog`：多 Cookie 池管理（增删 / 启停 / 设默认 / 粘贴模式），
      复用 `gui.cookie_status` 状态灯与 `COOKIE_MANUAL_HELP` 帮助文案；
    - `KeywordEditDialog`：关键词 + 价格阈值 + 排除词 / 必含词编辑（复用
      `validate_keyword_entry` / `parse_keyword_lines` 纯函数）；
    - `PresetWordsDialog`：预置排除词编辑；
    - `ChannelEditDialog`：通道字段编辑（通知设置页「配置」入口）；
    - `BlacklistDialog`：加入黑名单原因输入。

所有对话框都是模态 QDialog，结果通过 `exec()` 返回值 + 公开属性读取。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..gui import (
    CHANNEL_FIELDS,
    COOKIE_MANUAL_HELP,
    cookie_status,
    parse_keyword_lines,
    validate_keyword_entry,
)

logger = logging.getLogger(__name__)


class KeywordEditDialog(QDialog):
    """关键词 + 价格阈值 + 过滤词编辑对话框。

    用法：
        dlg = KeywordEditDialog(keyword="Switch", price=1000.0,
                                exclude=["收"], required=["国行"], parent=self)
        if dlg.exec() == QDialog.Accepted:
            kw, price, excludes, requireds = dlg.result()
    """

    def __init__(
        self,
        keyword: str = "",
        price: float = 0.0,
        exclude: Optional[List[str]] = None,
        required: Optional[List[str]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑关键词")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.edit_keyword = QLineEdit(str(keyword or ""))
        self.edit_price = QLineEdit(f"{float(price):.2f}" if price else "")
        form.addRow("关键词：", self.edit_keyword)
        form.addRow("价格阈值（元）：", self.edit_price)
        layout.addLayout(form)

        layout.addWidget(QLabel("排除词（每行一个，命中即跳过该商品）："))
        self.edit_exclude = QPlainTextEdit()
        self.edit_exclude.setPlainText("\n".join(exclude or []))
        self.edit_exclude.setMaximumHeight(80)
        layout.addWidget(self.edit_exclude)

        layout.addWidget(QLabel("必含词（每行一个，商品必须包含至少一个）："))
        self.edit_required = QPlainTextEdit()
        self.edit_required.setPlainText("\n".join(required or []))
        self.edit_required.setMaximumHeight(80)
        layout.addWidget(self.edit_required)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        """校验并提交（校验失败不关闭对话框）。"""
        try:
            keyword, price = validate_keyword_entry(
                self.edit_keyword.text(), self.edit_price.text()
            )
        except ValueError as exc:
            QMessageBox.warning(self, "输入有误", str(exc))
            return
        self._result = (
            keyword,
            price,
            parse_keyword_lines(self.edit_exclude.toPlainText()),
            parse_keyword_lines(self.edit_required.toPlainText()),
        )
        self.accept()

    def result(self) -> Tuple[str, float, List[str], List[str]]:  # type: ignore[override]
        """返回 (关键词, 价格, 排除词列表, 必含词列表)。"""
        return self._result


class PresetWordsDialog(QDialog):
    """预置排除词编辑对话框（新关键词自动预置的模板）。"""

    def __init__(
        self,
        preset: Optional[List[str]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑预置排除词")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("新关键词会自动带上以下排除词（每行一个，可留空关闭预置）：")
        )
        self.edit_preset = QPlainTextEdit()
        self.edit_preset.setPlainText("\n".join(preset or []))
        layout.addWidget(self.edit_preset)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def preset_words(self) -> List[str]:
        """返回编辑后的预置排除词（去空去重保序）。"""
        return parse_keyword_lines(self.edit_preset.toPlainText())


class ChannelEditDialog(QDialog):
    """通道字段编辑对话框（通知设置页「配置」入口，可复用字段定义）。"""

    def __init__(
        self,
        ctype: str,
        options: Optional[Dict[str, str]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        from ..gui import CHANNEL_LABELS

        self.ctype = str(ctype or "")
        self.setWindowTitle(f"配置通知通道：{CHANNEL_LABELS.get(self.ctype, self.ctype)}")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._edits: Dict[str, QLineEdit] = {}
        fields = CHANNEL_FIELDS.get(self.ctype, ())
        for field_name, label, secret, _default in fields:
            edit = QLineEdit(str((options or {}).get(field_name, "")))
            if secret:
                edit.setEchoMode(QLineEdit.Password)
            edit.setPlaceholderText(_default)
            form.addRow(f"{label}：", edit)
            self._edits[field_name] = edit
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def options(self) -> Dict[str, str]:
        """返回编辑后的字段值（去空白，空值剔除）。"""
        result: Dict[str, str] = {}
        for field_name, edit in self._edits.items():
            text = edit.text().strip()
            if text:
                result[field_name] = text
        return result


class BlacklistDialog(QDialog):
    """加入黑名单原因输入对话框。"""

    def __init__(self, title: str = "加入黑名单", default_reason: str = "人工剔除", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("加入原因（可选）："))
        self.edit_reason = QLineEdit(default_reason)
        layout.addWidget(self.edit_reason)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def reason(self) -> str:
        """返回原因文本。"""
        return self.edit_reason.text().strip()


class CookieDialog(QDialog):
    """Cookie 管理对话框：多 Cookie 池（增删 / 启停 / 设默认）+ 粘贴模式。

    行为对齐 Tk 版「Cookie 管理」对话框（v3.2 多账号池）：
        - 列表展示名称 + 启用状态 + Cookie 健康状态；
        - 「添加」：输入名称 + 粘贴 Cookie → 校验后加入池（或单值字段）；
        - 「更新选中」/「删除选中」：编辑 / 移除；
        - 「启用/停用」：切换条目启用状态（池内停用条目不参与轮换）；
        - 「设为默认」：把选中条目的 Cookie 写回单值 `monitor.cookies`；
        - 「如何获取 Cookie」：展示 COOKIE_MANUAL_HELP。

    结果通过 `result_pool()` / `result_single_cookie()` 读取（未确认不生效）。
    """

    def __init__(
        self,
        cookie_pool: Optional[List[Dict[str, Any]]] = None,
        single_cookie: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cookie 管理")
        self.resize(560, 420)

        #: 池条目：{name, cookie, enabled}（工作副本，确认后才提交）
        self._pool: List[Dict[str, Any]] = [dict(e) for e in (cookie_pool or [])]
        #: 单值 Cookie（「设为默认」写入）
        self._single_cookie = str(single_cookie or "")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("多账号 Cookie 池（启用条目按轮次轮换）："))
        self.list_pool = QListWidget()
        self.list_pool.itemSelectionChanged.connect(self._refresh_detail)
        layout.addWidget(self.list_pool, 1)

        # 底部：选中条目的名称 / Cookie 编辑框（粘贴模式）
        detail_form = QFormLayout()
        self.edit_name = QLineEdit()
        self.edit_cookie = QLineEdit()
        self.edit_cookie.setPlaceholderText("粘贴整行 Cookie（必须含 _m_h5_tk=）")
        detail_form.addRow("名称：", self.edit_name)
        detail_form.addRow("Cookie：", self.edit_cookie)
        layout.addLayout(detail_form)

        # 按钮行
        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("➕ 添加")
        self.btn_update = QPushButton("✏️ 更新选中")
        self.btn_delete = QPushButton("🗑 删除选中")
        self.btn_toggle = QPushButton("启用/停用")
        self.btn_default = QPushButton("⭐ 设为默认")
        for btn in (self.btn_add, self.btn_update, self.btn_delete, self.btn_toggle, self.btn_default):
            btn_row.addWidget(btn)
        self.btn_add.clicked.connect(self._on_add)
        self.btn_update.clicked.connect(self._on_update)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_toggle.clicked.connect(self._on_toggle)
        self.btn_default.clicked.connect(self._on_set_default)
        layout.addLayout(btn_row)

        # 状态灯（单值 Cookie 健康状态）+ 帮助
        self.light_cookie = QLabel("")
        layout.addWidget(self.light_cookie)
        self.btn_help = QPushButton("❓ 如何获取 Cookie？")
        self.btn_help.clicked.connect(self._show_help)
        layout.addWidget(self.btn_help)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_pool()
        self._refresh_detail()

    # ------------------------------------------------------------------ #
    def _refresh_pool(self) -> None:
        """刷新列表显示。"""
        self.list_pool.clear()
        for idx, entry in enumerate(self._pool):
            name = str(entry.get("name") or "")
            enabled = bool(entry.get("enabled", True))
            prefix = "✅" if enabled else "⏸"
            item = QListWidgetItem(f"{prefix} {name}")
            item.setData(Qt.UserRole, idx)
            self.list_pool.addItem(item)

    def _refresh_detail(self) -> None:
        """刷新右侧名称/Cookie 编辑框 + 状态灯。"""
        entry = self._selected_entry()
        if entry is not None:
            self.edit_name.setText(str(entry.get("name") or ""))
            self.edit_cookie.setText(str(entry.get("cookie") or ""))
            state, text = cookie_status(str(entry.get("cookie") or ""))
            self.light_cookie.setText(text)
        else:
            self.edit_name.clear()
            self.edit_cookie.clear()
            state, text = cookie_status(self._single_cookie)
            self.light_cookie.setText(text)

    def _selected_index(self) -> int:
        items = self.list_pool.selectedItems()
        if not items:
            return -1
        return int(items[0].data(Qt.UserRole))

    def _selected_entry(self) -> Optional[Dict[str, Any]]:
        idx = self._selected_index()
        if 0 <= idx < len(self._pool):
            return self._pool[idx]
        return None

    def _validate(self, name: str, cookie: str) -> Optional[str]:
        """校验名称与 Cookie；返回错误文案（None 表示通过）。"""
        name = str(name or "").strip()
        cookie = str(cookie or "").strip()
        if not name:
            return "名称不能为空。"
        if not cookie:
            return "Cookie 不能为空。"
        if "_m_h5_tk=" not in cookie:
            return "Cookie 中缺少 _m_h5_tk=，可能无效，请重新粘贴完整 Cookie。"
        return None

    def _on_add(self) -> None:
        name = self.edit_name.text()
        cookie = self.edit_cookie.text()
        error = self._validate(name, cookie)
        if error:
            QMessageBox.warning(self, "输入有误", error)
            return
        self._pool.append({"name": name.strip(), "cookie": cookie.strip(), "enabled": True})
        self._refresh_pool()
        self.list_pool.setCurrentRow(len(self._pool) - 1)
        self._refresh_detail()

    def _on_update(self) -> None:
        idx = self._selected_index()
        if idx < 0:
            QMessageBox.information(self, "提示", "请先在列表中选择一条 Cookie。")
            return
        name = self.edit_name.text()
        cookie = self.edit_cookie.text()
        error = self._validate(name, cookie)
        if error:
            QMessageBox.warning(self, "输入有误", error)
            return
        self._pool[idx]["name"] = name.strip()
        self._pool[idx]["cookie"] = cookie.strip()
        self._refresh_pool()
        self.list_pool.setCurrentRow(idx)
        self._refresh_detail()

    def _on_delete(self) -> None:
        idx = self._selected_index()
        if idx < 0:
            QMessageBox.information(self, "提示", "请先在列表中选择一条 Cookie。")
            return
        del self._pool[idx]
        self._refresh_pool()
        self._refresh_detail()

    def _on_toggle(self) -> None:
        idx = self._selected_index()
        if idx < 0:
            QMessageBox.information(self, "提示", "请先在列表中选择一条 Cookie。")
            return
        self._pool[idx]["enabled"] = not bool(self._pool[idx].get("enabled", True))
        self._refresh_pool()
        self.list_pool.setCurrentRow(idx)
        self._refresh_detail()

    def _on_set_default(self) -> None:
        """把选中条目的 Cookie 写回单值字段（monitor.cookies）。"""
        entry = self._selected_entry()
        if entry is None:
            QMessageBox.information(self, "提示", "请先在列表中选择一条 Cookie。")
            return
        self._single_cookie = str(entry.get("cookie") or "")
        self._refresh_detail()
        QMessageBox.information(self, "已设为默认", "已把该 Cookie 设为默认（monitor.cookies）。")

    def _show_help(self) -> None:
        QMessageBox.information(self, "如何获取 Cookie？", COOKIE_MANUAL_HELP)

    # ------------------------------------------------------------------ #
    def result_pool(self) -> List[Dict[str, Any]]:
        """返回编辑后的 Cookie 池（确认后读取）。"""
        return [dict(e) for e in self._pool]

    def result_single_cookie(self) -> str:
        """返回单值 Cookie（确认后读取）。"""
        return self._single_cookie
