"""Qt 对话框 / 页签 / 后台 worker 测试（v1.10.9，M16 补强）。

冷启动重评点名：Qt 版用例偏少且**覆盖率未纳入门禁**（本地实测 gui_qt 55%：
app.py 43% / dialogs.py 54% / workers.py 35%）。本文件补上可离线覆盖的部分。

全部 offscreen；PySide6 不可用时整体 skip（与 test_gui_qt.py 同一约定）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    QT_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺 PySide6
    QT_AVAILABLE = False

VALID_COOKIE = "cookie2=abc; unb=1; _m_h5_tk=tok_1791559226130"


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestKeywordEditDialog(unittest.TestCase):
    """关键词编辑对话框：构造 / 校验 / 回传。"""

    def _dialog(self, **kwargs):
        from xianyu_alert.gui_qt.dialogs import KeywordEditDialog

        return KeywordEditDialog(**kwargs)

    def test_result_round_trip(self) -> None:
        dialog = self._dialog(keyword="Switch", price=1000.0, exclude=["配件"], required=["oled"])
        keyword, price, exclude, required = dialog.result()
        self.assertEqual((keyword, price), ("Switch", 1000.0))
        self.assertEqual(exclude, ["配件"])
        self.assertEqual(required, ["oled"])
        dialog.close()

    def test_defaults_are_empty(self) -> None:
        dialog = self._dialog()
        keyword, price, exclude, required = dialog.result()
        self.assertEqual((keyword, price, exclude, required), ("", 0.0, [], []))
        dialog.close()

    def test_result_without_accept_returns_constructor_values(self) -> None:
        """result() 必须是全函数：没点「确定」时返回构造入参而不是抛异常。"""
        dialog = self._dialog(keyword="Switch", price=999.0)
        self.assertEqual(dialog.result()[0], "Switch")
        self.assertEqual(dialog.result()[1], 999.0)
        dialog.close()

    def test_accept_rejects_empty_keyword(self) -> None:
        from xianyu_alert.gui_qt import dialogs as dlg_mod

        dialog = self._dialog()
        with mock.patch.object(dlg_mod, "QMessageBox") as box:
            dialog._on_accept()
        box.warning.assert_called_once()
        self.assertEqual(dialog.result()[0], "", "校验失败不应写入结果")
        dialog.close()

    def test_accept_with_valid_input_sets_result(self) -> None:
        dialog = self._dialog()
        dialog.edit_keyword.setText("Switch OLED")
        dialog.edit_price.setText("1299")
        dialog._on_accept()
        self.assertEqual(dialog.result()[0], "Switch OLED")
        self.assertEqual(dialog.result()[1], 1299.0)
        dialog.close()

    def test_accept_rejects_dirty_price(self) -> None:
        from xianyu_alert.gui_qt import dialogs as dlg_mod

        dialog = self._dialog()
        dialog.edit_keyword.setText("Switch")
        dialog.edit_price.setText("很贵")
        with mock.patch.object(dlg_mod, "QMessageBox") as box:
            dialog._on_accept()
        box.warning.assert_called_once()
        dialog.close()


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestSimpleDialogs(unittest.TestCase):
    """预置词 / 通道 / 黑名单原因 / 刷新 Cookie 对话框。"""

    def test_preset_words_dialog(self) -> None:
        from xianyu_alert.gui_qt.dialogs import PresetWordsDialog

        dialog = PresetWordsDialog(["收", "求购", "配件"])
        self.assertEqual(dialog.preset_words(), ["收", "求购", "配件"])
        dialog.close()

    def test_preset_words_dialog_empty(self) -> None:
        from xianyu_alert.gui_qt.dialogs import PresetWordsDialog

        dialog = PresetWordsDialog(None)
        self.assertEqual(dialog.preset_words(), [])
        dialog.close()

    def test_channel_edit_dialog_round_trip(self) -> None:
        from xianyu_alert.gui_qt.dialogs import ChannelEditDialog

        dialog = ChannelEditDialog("webhook", {"url": "https://example.com/hook"})
        self.assertEqual(dialog.options(), {"url": "https://example.com/hook"})
        dialog.close()

    def test_channel_edit_dialog_drops_blank_fields(self) -> None:
        from xianyu_alert.gui_qt.dialogs import ChannelEditDialog

        dialog = ChannelEditDialog("console", None)
        self.assertEqual(dialog.options(), {}, "全空字段应被剔除")
        dialog.close()

    def test_blacklist_dialog_reason(self) -> None:
        from xianyu_alert.gui_qt.dialogs import BlacklistDialog

        dialog = BlacklistDialog(default_reason="人工剔除")
        self.assertEqual(dialog.reason(), "人工剔除")
        dialog.close()

    def test_refresh_cookie_dialog_default_cookie_is_empty(self) -> None:
        from xianyu_alert.gui_qt.dialogs import RefreshCookieDialog

        dialog = RefreshCookieDialog()
        self.assertEqual(dialog.cookie(), "")
        dialog.close()


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestCookieDialog(unittest.TestCase):
    """Cookie 池对话框：校验规则 / 选中 / 增改。

    **必须屏蔽 QMessageBox**：本文件的用例会触发校验失败分支，未屏蔽时
    模态框会挂住测试进程（本项目早期就在 Qt 上踩过这个坑）。
    """

    def setUp(self) -> None:
        self._box = mock.patch("xianyu_alert.gui_qt.dialogs.QMessageBox")
        self._box.start()

    def tearDown(self) -> None:
        self._box.stop()

    def _pool(self) -> list:
        return [
            {"name": "主号", "cookie": VALID_COOKIE, "enabled": True},
            {"name": "小号", "cookie": "cookie2=def", "enabled": False},
        ]

    def _dialog(self, pool=None, single=""):
        from xianyu_alert.gui_qt.dialogs import CookieDialog

        return CookieDialog(cookie_pool=pool if pool is not None else self._pool(), single_cookie=single)

    def test_selection_and_entries(self) -> None:
        dialog = self._dialog()
        dialog.list_pool.setCurrentRow(0)
        self.assertEqual(dialog._selected_index(), 0)
        self.assertEqual(dialog._selected_entry().get("name"), "主号")
        self.assertEqual(len(dialog.result_pool()), 2)
        dialog.close()

    def test_empty_pool_selection_is_safe(self) -> None:
        dialog = self._dialog(pool=[], single="")
        self.assertIsNone(dialog._selected_entry(), "空池时不应崩")
        self.assertEqual(dialog.result_pool(), [])
        dialog.close()

    def test_validate_rules(self) -> None:
        dialog = self._dialog(pool=[])
        self.assertIsNotNone(dialog._validate("", VALID_COOKIE), "空名称应被拒绝")
        self.assertIsNotNone(dialog._validate("主号", ""), "空 Cookie 应被拒绝")
        self.assertIsNotNone(dialog._validate("主号", "cookie2=abc"), "缺少 _m_h5_tk 应被拒绝")
        self.assertIsNone(dialog._validate("主号", VALID_COOKIE), "完整 Cookie 应通过")
        dialog.close()

    def test_add_entry_appends_to_pool(self) -> None:
        dialog = self._dialog(pool=[])
        dialog.edit_name.setText("新号")
        dialog.edit_cookie.setText(VALID_COOKIE)
        dialog._on_add()
        pool = dialog.result_pool()
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["name"], "新号")
        self.assertIn("_m_h5_tk", pool[0]["cookie"])
        dialog.close()

    def test_add_entry_with_invalid_cookie_is_rejected(self) -> None:
        dialog = self._dialog(pool=[])
        dialog.edit_name.setText("新号")
        dialog.edit_cookie.setText("cookie2=abc")  # 缺 _m_h5_tk
        dialog._on_add()
        self.assertEqual(dialog.result_pool(), [], "非法条目不应被加入")
        dialog.close()

    def test_single_cookie_round_trip(self) -> None:
        dialog = self._dialog(pool=[], single=VALID_COOKIE)
        self.assertIn("_m_h5_tk", dialog.result_single_cookie())
        dialog.close()


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestMonitorConfigTabHandlers(unittest.TestCase):
    """配置页签的关键词增删改与开关。"""

    def _tab(self):
        from xianyu_alert.gui import config_to_form
        from xianyu_alert.gui_qt.tab_config import MonitorConfigTab

        tmp = tempfile.mkdtemp()
        form = config_to_form(
            {
                "keywords": [{"keyword": "Switch", "max_price": 1000}],
                "monitor": {"interval_seconds": 60, "storage_path": os.path.join(tmp, "s.db")},
                "fetcher": {"type": "mock"},
            }
        )
        self._tmp = tmp
        return MonitorConfigTab(form)

    def setUp(self) -> None:
        # 同上：屏蔽模态框，避免挂住测试进程
        self._box = mock.patch("xianyu_alert.gui_qt.tab_config.QMessageBox")
        self._box.start()

    def tearDown(self) -> None:
        self._box.stop()

    def test_toggle_keyword_switches_enabled_state(self) -> None:
        tab = self._tab()
        tab.table_keywords.selectRow(0)
        tab._on_toggle_keyword()
        enabled = tab.collect_config().get("keyword_enabled") or {}
        self.assertEqual(list(enabled.values()), [False], "默认启用的关键词应被停用")
        tab.close()

    def test_toggle_keyword_twice_restores(self) -> None:
        tab = self._tab()
        tab.table_keywords.selectRow(0)
        tab._on_toggle_keyword()
        tab.table_keywords.selectRow(0)
        tab._on_toggle_keyword()
        enabled = tab.collect_config().get("keyword_enabled") or {}
        self.assertEqual(list(enabled.values()), [True])
        tab.close()

    def test_add_keyword_appends_row(self) -> None:
        from PySide6.QtWidgets import QDialog

        tab = self._tab()
        with mock.patch("xianyu_alert.gui_qt.tab_config.KeywordEditDialog") as dlg:
            dlg.Accepted = QDialog.Accepted
            dlg.return_value.exec.return_value = QDialog.Accepted
            dlg.return_value.result.return_value = ("新词", 500.0, [], [])
            tab._on_add_keyword()
        keywords = tab.collect_config().get("keywords") or []
        self.assertIn("新词", [k[0] for k in keywords])
        tab.close()

    def test_delete_keyword_removes_row(self) -> None:
        from PySide6.QtWidgets import QMessageBox as RealBox

        tab = self._tab()
        tab.table_keywords.selectRow(0)
        with mock.patch("xianyu_alert.gui_qt.tab_config.QMessageBox") as box:
            # Mock 不带枚举：handler 比较的是 QMessageBox.Yes，必须显式给出真实值
            box.Yes = RealBox.Yes
            box.question.return_value = RealBox.Yes
            tab._on_delete_keyword()
        self.assertEqual(tab.collect_config().get("keywords"), [])
        tab.close()

    def test_edit_filters_updates_filters(self) -> None:
        from PySide6.QtWidgets import QDialog

        tab = self._tab()
        tab.table_keywords.selectRow(0)
        with mock.patch("xianyu_alert.gui_qt.tab_config.KeywordEditDialog") as dlg:
            dlg.Accepted = QDialog.Accepted
            dlg.return_value.exec.return_value = QDialog.Accepted
            dlg.return_value.result.return_value = ("Switch", 1000.0, ["配件"], ["oled"])
            tab._on_edit_filters()
        filters = tab.collect_config().get("keyword_filters") or {}
        self.assertIn("Switch", filters)
        tab.close()


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestWorkers(unittest.TestCase):
    """后台 worker 的生命周期。"""

    def _config(self):
        from xianyu_alert.config import config_from_dict

        tmp = tempfile.mkdtemp()
        return config_from_dict(
            {
                "keywords": [{"keyword": "Switch", "max_price": 1000}],
                "monitor": {"interval_seconds": 60, "storage_path": os.path.join(tmp, "s.db")},
                "fetcher": {"type": "mock"},
            }
        )

    def test_monitor_worker_request_stop_before_start_is_safe(self) -> None:
        from xianyu_alert.gui_qt.workers import MonitorWorker

        worker = MonitorWorker(self._config(), single_round=True)
        worker.request_stop()
        self.assertFalse(worker.isRunning())

    def test_monitor_worker_stop_event_set(self) -> None:
        from xianyu_alert.gui_qt.workers import MonitorWorker

        worker = MonitorWorker(self._config(), single_round=True)
        worker.request_stop()
        self.assertTrue(worker._stop_event.is_set(), "request_stop 必须置位停止事件")

    def test_monitor_worker_exposes_signals(self) -> None:
        from xianyu_alert.gui_qt.workers import MonitorWorker

        worker = MonitorWorker(self._config(), single_round=True)
        # MonitorWorker 用单一 ui_message 信号做跨线程投递（log/alert/status 都走它）
        self.assertTrue(hasattr(worker, "ui_message"))


if __name__ == "__main__":
    unittest.main()
