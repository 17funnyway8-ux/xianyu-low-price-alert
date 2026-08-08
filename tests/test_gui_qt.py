"""Qt（PySide6）gui_qt 包逻辑测试（macOS 适配设计文档 §5.2 / 任务 T05）。

全部使用 `QT_QPA_PLATFORM=offscreen`（无显示环境可跑）；只测**不依赖真实网络**的
逻辑通路：
    1. QtLogHandler → LogBridge 信号 → 收集器；
    2. LogView 2000 行自动裁剪 / 分级高亮 / 清空；
    3. MonitorWorker 启停（mock monitor 全家桶，single_round 一轮即退）；
    4. 入口分发：darwin → gui_qt.main；其余 → gui.main；
    5. 表单收集：tab_config.collect_config / tab_notify.collect_channels /
       state.form_to_config_dict；
    6. 消息分发：XianyuAlertQtApp._handle_ui_message（log/alert/status/state）。

若 PySide6 不可用（如 CI 未装），本模块整体 skip 并提示。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# offscreen 必须早于 PySide6 导入
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QCoreApplication
    import yaml  # noqa: E402

    _APP = QApplication.instance() or QApplication([])
    QT_AVAILABLE = True
except Exception as exc:  # pragma: no cover - 环境缺 PySide6
    QT_AVAILABLE = False
    _QT_IMPORT_ERROR = str(exc)


def _make_config_dict(tmp: str) -> dict:
    """构造最小合法配置字典（mock 抓取器 + 临时 storage）。"""
    return {
        "keywords": [{"keyword": "Switch", "max_price": 1000}],
        "monitor": {"interval_seconds": 60, "cookies": ""},
        "fetcher": {"type": "mock", "mock_products_per_round": 1},
        "storage": {"path": os.path.join(tmp, "state", "x.db")},
        "notify": {"channels": [{"type": "console"}]},
    }


def _write_config(tmp: str) -> str:
    """写入临时 config.yaml，返回路径。"""
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(_make_config_dict(tmp), fp, allow_unicode=True, sort_keys=False)
    return path


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestQtLogBridge(unittest.TestCase):
    """QtLogHandler → LogBridge 信号通路。"""

    def setUp(self) -> None:
        from xianyu_alert.gui_qt.workers import LogBridge, QtLogHandler

        self.bridge = LogBridge()
        self.received: list = []
        self.bridge.message.connect(lambda level, text: self.received.append((level, text)))
        self.handler = QtLogHandler(self.bridge, level=0)
        self.logger = logging.getLogger("xianyu_alert.gui_qt.test_logbridge")
        self.logger.addHandler(self.handler)
        # 注意：setLevel(0) 是 NOTSET → effective level 会上溯到 root（默认 WARNING），
        # INFO 会被过滤导致 handler 不被调用（单独跑该文件时必挂、全量跑时依赖其它
        # 测试把 root 级别调低才碰巧通过）。必须显式设为 DEBUG 保证独立可跑。
        self.logger.setLevel(logging.DEBUG)
        # 同时确保名字链上的父级不拦截（独立运行 / 全量运行行为一致）
        for name in ("xianyu_alert", "xianyu_alert.gui_qt"):
            parent = logging.getLogger(name)
            if parent.level == logging.NOTSET:
                parent.setLevel(logging.DEBUG)
                self.addCleanup(parent.setLevel, logging.NOTSET)
        self.addCleanup(self.logger.removeHandler, self.handler)

    def _pump(self, times: int = 20) -> None:
        """处理事件循环，让跨线程信号投递到收集器。"""
        for _ in range(times):
            _APP.processEvents()
            time.sleep(0.005)

    def test_emit_delivers_level_and_text(self) -> None:
        import logging

        self.logger.info("测试日志内容")
        self._pump()
        self.assertTrue(self.received, "LogBridge 未收到消息")
        level, text = self.received[-1]
        self.assertEqual(level, "INFO")
        self.assertIn("测试日志内容", text)
        self.assertIn("[", text)  # 带时间戳前缀


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestLogView(unittest.TestCase):
    """LogView 裁剪 / 高亮 / 清空。"""

    def setUp(self) -> None:
        from xianyu_alert.gui_qt.widgets import LogView

        self.view = LogView()

    def test_maximum_block_count_clips(self) -> None:
        from xianyu_alert.gui_qt.widgets import MAX_LOG_LINES

        for i in range(MAX_LOG_LINES + 100):
            self.view.append_log("INFO", f"line-{i}")
        self.assertEqual(self.view.document().blockCount(), MAX_LOG_LINES)

    def test_append_highlights_without_crash(self) -> None:
        lines = [
            ("INFO", "普通日志"),
            ("ERROR", "❌ 出错了"),
            ("WARNING", "⚠️ 警告"),
            ("ALERT", "🔔 命中低价！"),
            ("NEW_ITEM", "✨ 新出现"),
            ("SUMMARY", "✅ 本轮完成"),
            ("ROUND", "===== 第 1 轮监测开始 ====="),
            ("DIM", "🚫 已停用"),
        ]
        for level, text in lines:
            self.view.append_log(level, text)  # 不应抛异常
        # QPlainTextEdit 初始含 1 个空块；追加 8 行 → 9 个块
        self.assertEqual(self.view.document().blockCount(), len(lines) + 1)
        for _level, text in lines:
            self.assertIn(text, self.view.toPlainText())

    def test_clear(self) -> None:
        self.view.append_log("INFO", "x")
        self.view.clear_log()
        self.assertEqual(self.view.toPlainText(), "")

    def test_font_size_clamped(self) -> None:
        for _ in range(20):
            self.view.set_font_size(1)
        self.assertLessEqual(self.view.font().pointSize(), 16)
        for _ in range(20):
            self.view.set_font_size(-1)
        self.assertGreaterEqual(self.view.font().pointSize(), 8)


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestMonitorWorker(unittest.TestCase):
    """MonitorWorker 启停（mock monitor 全家桶，single_round 一轮即退）。"""

    def test_single_round_emits_messages_and_finishes(self) -> None:
        import threading

        from xianyu_alert.config import config_from_dict
        from xianyu_alert.gui_qt import workers

        messages: list = []
        finished: list = []

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_from_dict(_make_config_dict(tmp))
            fake_monitor = mock.Mock()
            fake_monitor.preflight_cookie.return_value = ""
            fake_monitor.run_once.return_value = 0
            fake_monitor.last_result.notified_products = []

            fake_storage = mock.Mock()
            fake_fetcher = mock.Mock()

            with mock.patch.object(workers, "build_fetcher", return_value=fake_fetcher), \
                 mock.patch.object(workers, "Storage", return_value=fake_storage), \
                 mock.patch.object(workers, "build_notifiers", return_value=[]), \
                 mock.patch.object(workers, "Monitor", return_value=fake_monitor):
                worker = workers.MonitorWorker(cfg, single_round=True, detail_only=True)
                worker.ui_message.connect(lambda kind, payload: messages.append((kind, payload)))
                worker.finished.connect(lambda: finished.append(True))
                worker.start()

                # 等待线程结束，同时泵事件循环投递跨线程信号
                deadline = time.monotonic() + 10
                while worker.isRunning() and time.monotonic() < deadline:
                    _APP.processEvents()
                    time.sleep(0.01)
                worker.wait(5000)
                for _ in range(30):
                    _APP.processEvents()
                    time.sleep(0.005)

        self.assertTrue(finished, "worker 未结束")
        kinds = [k for k, _ in messages]
        self.assertIn("log", kinds)
        self.assertIn("status", kinds)
        self.assertIn("state", kinds)
        # 单轮模式结束 → 发送 running False
        state_msg = [p for k, p in messages if k == "state"]
        self.assertTrue(state_msg)
        self.assertFalse(state_msg[-1].get("running", True))

    def test_request_stop_interrupts_wait(self) -> None:
        """request_stop 置位后，循环退出（不依赖真实网络）。"""
        import threading

        from xianyu_alert.config import config_from_dict
        from xianyu_alert.gui_qt import workers

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_from_dict(_make_config_dict(tmp))
            fake_monitor = mock.Mock()
            fake_monitor.preflight_cookie.return_value = ""
            fake_monitor.run_once.return_value = 0
            fake_monitor.last_result.notified_products = []
            fake_storage = mock.Mock()
            fake_fetcher = mock.Mock()

            with mock.patch.object(workers, "build_fetcher", return_value=fake_fetcher), \
                 mock.patch.object(workers, "Storage", return_value=fake_storage), \
                 mock.patch.object(workers, "build_notifiers", return_value=[]), \
                 mock.patch.object(workers, "Monitor", return_value=fake_monitor):
                worker = workers.MonitorWorker(cfg, single_round=False, detail_only=True)
                worker.start()
                time.sleep(0.3)
                worker.request_stop()
                deadline = time.monotonic() + 10
                while worker.isRunning() and time.monotonic() < deadline:
                    _APP.processEvents()
                    time.sleep(0.01)
                worker.wait(5000)
                self.assertFalse(worker.isRunning())


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestEntryDispatch(unittest.TestCase):
    """入口分发：darwin → gui_qt.main；其余 → gui.main。"""

    def test_is_available_true_when_pyside_installed(self) -> None:
        from xianyu_alert import gui_qt

        self.assertTrue(gui_qt.is_available())

    def test_cmd_gui_darwin_uses_qt(self) -> None:
        from xianyu_alert import cli

        args = mock.Mock(config="x.yaml")
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch("xianyu_alert.gui_qt.main", return_value=0) as qt_main:
            result = cli.cmd_gui(args)
        self.assertEqual(result, 0)
        qt_main.assert_called_once_with(config_path="x.yaml")

    def test_cmd_gui_non_darwin_uses_tk(self) -> None:
        from xianyu_alert import cli

        args = mock.Mock(config="y.yaml")
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch("xianyu_alert.gui.main", return_value=0) as tk_main:
            result = cli.cmd_gui(args)
        self.assertEqual(result, 0)
        tk_main.assert_called_once_with(config_path="y.yaml")

    def test_cmd_gui_darwin_pyside_missing_falls_back_to_tk(self) -> None:
        """Bug #2 回归：PySide6 缺失（is_available=False）→ 回退 Tk，不崩溃。"""
        from xianyu_alert import cli

        args = mock.Mock(config="z.yaml")
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch("xianyu_alert.gui_qt.is_available", return_value=False) as probe, \
             mock.patch("xianyu_alert.gui.main", return_value=0) as tk_main:
            result = cli.cmd_gui(args)
        probe.assert_called_once_with()
        self.assertEqual(result, 0)
        tk_main.assert_called_once_with(config_path="z.yaml")

    def test_cmd_gui_darwin_qt_main_import_error_falls_back(self) -> None:
        """Bug #2 防御：is_available 通过但 main() 内部导入失败 → 仍回退 Tk。"""
        from xianyu_alert import cli

        def _boom(config_path: str = "") -> int:
            raise ImportError("No module named 'PySide6.QtWidgets'")

        args = mock.Mock(config="z.yaml")
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch("xianyu_alert.gui_qt.is_available", return_value=True), \
             mock.patch("xianyu_alert.gui_qt.main", side_effect=_boom), \
             mock.patch("xianyu_alert.gui.main", return_value=0) as tk_main:
            result = cli.cmd_gui(args)
        self.assertEqual(result, 0)
        self.assertTrue(tk_main.called)

    def test_cmd_gui_darwin_pyside_missing_blocked_import(self) -> None:
        """Bug #2 真实场景：builtins 层面拦截 PySide6 import → 回退 Tk，不崩溃。"""
        import builtins

        from xianyu_alert import cli

        orig_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "PySide6" or name.startswith("PySide6."):
                raise ImportError("No module named 'PySide6'")
            return orig_import(name, *a, **kw)

        builtins.__import__ = _fake_import
        try:
            args = mock.Mock(config="z.yaml")
            with mock.patch.object(sys, "platform", "darwin"), \
                 mock.patch("xianyu_alert.gui.main", return_value=0) as tk_main:
                result = cli.cmd_gui(args)
            self.assertEqual(result, 0)
            self.assertTrue(tk_main.called)
        finally:
            builtins.__import__ = orig_import

    def test_entry_main_darwin_pyside_missing_falls_back_to_tk(self) -> None:
        """Bug #2 回归（build/entry.py）：darwin + PySide6 缺失 → 回退 Tk。"""
        import builtins
        import importlib.util

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        entry_path = os.path.join(project_root, "build", "entry.py")
        spec = importlib.util.spec_from_file_location("build_entry_under_test", entry_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        entry_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry_mod)

        orig_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "PySide6" or name.startswith("PySide6."):
                raise ImportError("No module named 'PySide6'")
            return orig_import(name, *a, **kw)

        builtins.__import__ = _fake_import
        try:
            with mock.patch.object(sys, "platform", "darwin"), \
                 mock.patch.object(sys, "argv", ["entry.py"]), \
                 mock.patch("xianyu_alert.paths.default_config_path", return_value="x.yaml"), \
                 mock.patch("xianyu_alert.gui.main", return_value=0) as tk_main:
                result = entry_mod.main()
            self.assertEqual(result, 0)
            tk_main.assert_called_once_with(config_path="x.yaml")
        finally:
            builtins.__import__ = orig_import

    def test_cmd_shortcut_non_windows_returns_1(self) -> None:
        from xianyu_alert import cli

        args = mock.Mock(name="闲鱼低价提醒工具")
        with mock.patch.object(sys, "platform", "darwin"):
            result = cli.cmd_shortcut(args)
        self.assertEqual(result, 1)


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestAlertTableInstanceIsolation(unittest.TestCase):
    """Bug #3 回归：AlertTable._rows 为实例属性，多实例/重复构造不串数据。"""

    def test_instances_do_not_share_rows(self) -> None:
        from xianyu_alert.gui_qt.widgets import AlertTable

        t1 = AlertTable()
        t2 = AlertTable()
        t1.append_row({"title": "A", "product_id": "1"})
        self.assertEqual(t1.row_count(), 1)
        self.assertEqual(t2.row_count(), 0)  # 修复前此处为 1（类属性共享）
        t2.append_row({"title": "B", "product_id": "2"})
        self.assertEqual(t1.row_count(), 1)
        self.assertEqual(t2.row_count(), 1)
        self.assertEqual(t1.all_rows()[0]["title"], "A")
        self.assertEqual(t2.all_rows()[0]["title"], "B")
        t2.clear_rows()
        self.assertEqual(t1.row_count(), 1)  # t2 清空不影响 t1


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestFormCollection(unittest.TestCase):
    """三页签表单收集 + config 组装（纯逻辑）。"""

    def test_tab_config_collects_form(self) -> None:
        from xianyu_alert.gui import config_to_form
        from xianyu_alert.gui_qt.tab_config import MonitorConfigTab

        with tempfile.TemporaryDirectory() as tmp:
            form = config_to_form(_make_config_dict(tmp))
            tab = MonitorConfigTab(form)
            collected = tab.collect_config()
            self.assertEqual(collected["interval"], 60)
            self.assertEqual(collected["fetcher_type"], "mock")
            self.assertEqual(collected["pages"], 1)
            self.assertEqual(collected["keywords"], [("Switch", 1000.0)])

    def test_tab_notify_collects_channels(self) -> None:
        from xianyu_alert.gui import config_to_form
        from xianyu_alert.gui_qt.tab_notify import NotifyConfigTab

        with tempfile.TemporaryDirectory() as tmp:
            form = config_to_form(_make_config_dict(tmp))
            tab = NotifyConfigTab(form)
            channels = tab.collect_channels()
            # 默认配置启用 console
            self.assertTrue(channels["console"]["enabled"])

    def test_form_to_config_dict_encrypts_cookie(self) -> None:
        from xianyu_alert import secure
        from xianyu_alert.gui_qt import state

        with tempfile.TemporaryDirectory() as tmp:
            key_path = os.path.join(tmp, "secret.key")
            secure.set_key_file(key_path)
            try:
                form = {"keywords": [("Switch", 1000.0)], "interval": 60,
                        "fetcher_type": "mock", "cookies": "_m_h5_tk=abc_1",
                        "storage_path": os.path.join(tmp, "s.db"),
                        "channels": {"console": {"enabled": True, "options": {}}},
                        "pages": 1, "cookie_pool": [], "preset_exclude_keywords": []}
                data = state.form_to_config_dict(form)
                self.assertTrue(data["monitor"]["cookies"].startswith(secure.FERNET_PREFIX))
                self.assertTrue(data["monitor"]["cookies_encrypted"])
            finally:
                secure.set_key_file(None)


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestMessageDispatch(unittest.TestCase):
    """XianyuAlertQtApp._handle_ui_message 主线程分发。"""

    def test_handle_log_alert_status_state(self) -> None:
        from xianyu_alert.gui_qt.app import XianyuAlertQtApp

        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_config(tmp)
            app_win = XianyuAlertQtApp(config_path=config_path)
            try:
                # log → 日志区
                app_win._handle_ui_message("log", ("INFO", "hello-qt"))
                self.assertIn("hello-qt", app_win.tab_run.log_view.toPlainText())

                # alert → 提醒记录表
                app_win._handle_ui_message(
                    "alert",
                    {"time": "2025-01-01 00:00:00", "keyword": "Switch", "title": "测试商品",
                     "price": "¥999", "publish": "2025-01-01", "url": "https://x",
                     "product_id": "p1"},
                )
                rows = app_win.tab_run.table_alerts.all_rows()
                self.assertTrue(rows)
                self.assertEqual(rows[0]["title"], "测试商品")

                # status → 统计
                app_win._handle_ui_message("status", {"rounds": 3, "alerts": 5})
                self.assertEqual(app_win.tab_run.lbl_rounds.text(), "轮次：3")

                # state running False → 按钮恢复
                app_win._handle_ui_message("state", {"running": False})
                self.assertTrue(app_win.tab_run.btn_start.isEnabled())
            finally:
                app_win.close()
                _APP.processEvents()


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestV18GuiControls(unittest.TestCase):
    """v1.8（A6）：Qt 一键刷新入口 / Cookie 管理刷新能力 / mtime 检测。"""

    def test_tab_config_has_refresh_button_and_signal(self) -> None:
        from xianyu_alert.gui import config_to_form
        from xianyu_alert.gui_qt.tab_config import MonitorConfigTab

        with tempfile.TemporaryDirectory() as tmp:
            form = config_to_form(_make_config_dict(tmp))
            tab = MonitorConfigTab(form)
            self.assertEqual(tab.btn_refresh_cookie.text(), "🔄 一键刷新 Cookie")
            emitted: list = []
            tab.refresh_cookie_requested.connect(lambda: emitted.append(True))
            tab.btn_refresh_cookie.click()
            _APP.processEvents()
            self.assertEqual(emitted, [True])

    def test_refresh_cookie_dialog_validates(self) -> None:
        from xianyu_alert.gui_qt.dialogs import RefreshCookieDialog

        dlg = RefreshCookieDialog()
        dlg.edit_cookie.setPlainText("cookie2=only")  # 缺 _m_h5_tk
        with mock.patch("xianyu_alert.gui_qt.dialogs.QMessageBox.critical") as crit:
            dlg._on_save()
        self.assertTrue(crit.called)
        self.assertEqual(dlg.cookie(), "")  # 未保存

        dlg.edit_cookie.setPlainText("_m_h5_tk=t; c=1")  # 无时间戳 → ok（历史样本兼容）
        dlg._on_save()
        self.assertEqual(dlg.cookie(), "_m_h5_tk=t; c=1")

    def test_cookie_dialog_has_refresh_and_auto_disable(self) -> None:
        from xianyu_alert.gui_qt.dialogs import CookieDialog

        dlg = CookieDialog(
            cookie_pool=[
                {"name": "a", "cookie": "_m_h5_tk=t; c=1", "enabled": True},
            ]
        )
        self.assertEqual(dlg.btn_refresh.text(), "🔄 刷新选中")
        self.assertEqual(dlg.btn_disable_expired.text(), "⏹ 自动停用过期项")
        # 自动停用：过期条目被停用（确认 mock）
        expired = "_m_h5_tk=abc_1000000000000; c=1"
        dlg._pool.append({"name": "bad", "cookie": expired, "enabled": True})
        with mock.patch("xianyu_alert.gui_qt.dialogs.QMessageBox.question", return_value=QMessageBox.Yes):
            dlg._on_auto_disable()
        bad = next(e for e in dlg._pool if e["name"] == "bad")
        self.assertFalse(bad["enabled"])  # enabled=false 保留条目
        good = next(e for e in dlg._pool if e["name"] == "a")
        self.assertTrue(good["enabled"])

    def test_app_mtime_detection(self) -> None:
        from xianyu_alert.gui_qt.app import XianyuAlertQtApp

        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_config(tmp)
            app_win = XianyuAlertQtApp(config_path=config_path)
            try:
                self.assertIsNotNone(app_win._config_mtime)
                # 外部修改 → 弹重载询问（mock Yes）→ 关键词重载
                _write_config(tmp)  # 重新写入（内容相同，但显式拨快 mtime 模拟外部修改）
                import time as _time

                os.utime(config_path, (_time.time() + 2, _time.time() + 2))
                with mock.patch(
                    "xianyu_alert.gui_qt.app.QMessageBox.question", return_value=QMessageBox.Yes
                ) as q:
                    app_win._check_config_mtime()
                self.assertTrue(q.called)
                # 本进程保存 → 快照更新，不再触发
                app_win._touch_config_mtime()
                with mock.patch(
                    "xianyu_alert.gui_qt.app.QMessageBox.question", return_value=QMessageBox.Yes
                ) as q2:
                    app_win._check_config_mtime()
                self.assertFalse(q2.called)
            finally:
                app_win.close()
                _APP.processEvents()


if __name__ == "__main__":  # pragma: no cover
    import logging

    unittest.main()
