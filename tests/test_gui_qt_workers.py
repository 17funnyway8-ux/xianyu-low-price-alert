"""Qt 后台 worker 的循环与异常路径测试（v1.10.12，M16 补强）。

冷启动重评点名：`gui_qt/workers.py` 覆盖率 **35%** —— 缺的正是"后台线程真跑起来以后"
的路径：命中投递、单轮异常不终止、启动失败不崩窗、停止信号响应、资源必然关闭。

全部 offscreen；把 `build_fetcher` / `Storage` / `build_notifiers` / `Monitor`
替换成替身即可离线驱动，不需要网络与真实数据目录。
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


def make_config(tmp: str):
    from xianyu_alert.config import config_from_dict

    return config_from_dict(
        {
            "keywords": [{"keyword": "Switch", "max_price": 1000}],
            "monitor": {"interval_seconds": 1, "storage_path": os.path.join(tmp, "s.db")},
            "fetcher": {"type": "mock"},
        }
    )


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestMonitorWorkerLoop(unittest.TestCase):
    """MonitorWorker.run() 的成功 / 异常 / 停止 / 清理。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _worker(self, single_round: bool = True):
        from xianyu_alert.gui_qt.workers import MonitorWorker

        worker = MonitorWorker(self.config, single_round=single_round)
        self.messages: list = []
        worker.ui_message.connect(lambda kind, payload: self.messages.append((kind, payload)))
        return worker

    @staticmethod
    def _product():
        from xianyu_alert.models import Product

        return Product("810000000001", "Switch OLED", 999.0, "https://x/item?id=810000000001", keyword="Switch")

    def _patches(self, monitor, fetcher=None, storage=None):
        fake_fetcher = fetcher or mock.Mock()
        fake_storage = storage or mock.Mock()
        return [
            mock.patch("xianyu_alert.gui_qt.workers.build_fetcher", return_value=fake_fetcher),
            mock.patch("xianyu_alert.gui_qt.workers.Storage", return_value=fake_storage),
            mock.patch("xianyu_alert.gui_qt.workers.build_notifiers", return_value=[mock.Mock(name="console")]),
            mock.patch("xianyu_alert.gui_qt.workers.Monitor", return_value=monitor),
        ]

    def test_emits_alert_and_status_for_hits(self) -> None:
        monitor = mock.Mock()
        monitor.preflight_cookie.return_value = ""
        monitor.run_once.return_value = None
        monitor.last_result.notified_products = [self._product()]
        worker = self._worker()
        for p in self._patches(monitor):
            p.start()
        try:
            worker.run()
        finally:
            mock.patch.stopall()
        kinds = [kind for kind, _payload in self.messages]
        self.assertIn("alert", kinds, "命中的商品必须投递 alert")
        self.assertIn("status", kinds, "轮次结束后必须投递 status")
        self.assertIn("state", kinds, "退出前必须投递 state(running=False)")
        alerts = [payload for kind, payload in self.messages if kind == "alert"]
        self.assertEqual(alerts[0]["title"], "Switch OLED")
        self.assertEqual(alerts[0]["price"], "¥999.00")
        status = [payload for kind, payload in self.messages if kind == "status"][-1]
        self.assertEqual(status["rounds"], 1)
        self.assertEqual(status["alerts"], 1)

    def test_round_exception_is_logged_and_loop_survives(self) -> None:
        monitor = mock.Mock()
        monitor.preflight_cookie.return_value = ""
        monitor.run_once.side_effect = RuntimeError("本轮炸了")
        monitor.last_result.notified_products = []
        worker = self._worker(single_round=True)
        for p in self._patches(monitor):
            p.start()
        try:
            worker.run()
        finally:
            mock.patch.stopall()
        logs = [payload for kind, payload in self.messages if kind == "log"]
        levels = [level for level, _text in logs]
        self.assertIn("ERROR", levels, "单轮异常应记 ERROR 而不是崩线程")
        self.assertIn("state", [k for k, _ in self.messages], "异常后仍应投递退出状态")

    def test_setup_failure_does_not_crash_and_reports(self) -> None:
        worker = self._worker()
        with mock.patch("xianyu_alert.gui_qt.workers.build_fetcher", side_effect=RuntimeError("无抓取器")):
            worker.run()  # 不应抛异常
        messages = self.messages
        self.assertIn("message", [k for k, _ in messages], "启动失败应弹消息而不是静默")
        self.assertIn("state", [k for k, _ in messages])

    def test_stop_signal_before_first_round_exits_immediately(self) -> None:
        monitor = mock.Mock()
        monitor.preflight_cookie.return_value = ""
        worker = self._worker(single_round=False)
        worker.request_stop()
        for p in self._patches(monitor):
            p.start()
        try:
            worker.run()
        finally:
            mock.patch.stopall()
        monitor.run_once.assert_not_called()
        texts = " ".join(
            text for kind, payload in self.messages if kind == "log" for _level, text in [payload]
        )
        self.assertIn("停止信号", texts)

    def test_closes_fetcher_and_storage(self) -> None:
        monitor = mock.Mock()
        monitor.preflight_cookie.return_value = ""
        monitor.run_once.return_value = None
        monitor.last_result.notified_products = []
        fake_fetcher, fake_storage = mock.Mock(), mock.Mock()
        worker = self._worker()
        for p in self._patches(monitor, fetcher=fake_fetcher, storage=fake_storage):
            p.start()
        try:
            worker.run()
        finally:
            mock.patch.stopall()
        fake_fetcher.close.assert_called_once()
        fake_storage.close.assert_called_once()

    def test_loop_mode_stops_during_wait(self) -> None:
        """循环模式：等间隔时收到停止信号应立即退出（不是等满 interval）。"""
        monitor = mock.Mock()
        monitor.preflight_cookie.return_value = ""
        monitor.run_once.return_value = None
        monitor.last_result.notified_products = []
        worker = self._worker(single_round=False)
        for p in self._patches(monitor):
            p.start()
        try:
            original = worker._stop_event

            class Triggered:
                def is_set(self) -> bool:
                    return False

                def set(self) -> None:
                    pass

                def wait(self, timeout=None) -> bool:  # noqa: ANN001
                    original.set()
                    return True

            worker._stop_event = Triggered()  # type: ignore[assignment]
            worker.run()
        finally:
            mock.patch.stopall()
        self.assertEqual(monitor.run_once.call_count, 1, "只应跑一轮就因等待被打断而退出")


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestLogBridge(unittest.TestCase):
    """日志桥：QtLogHandler → LogBridge → 主线程。"""

    def test_relays_message(self) -> None:
        from xianyu_alert.gui_qt.workers import LogBridge

        bridge = LogBridge()
        got: list = []
        bridge.message.connect(lambda level, text: got.append((level, text)))
        bridge.message.emit("INFO", "hello")
        self.assertEqual(got, [("INFO", "hello")])

    def test_emitted_from_worker_thread(self) -> None:
        import threading

        from xianyu_alert.gui_qt.workers import LogBridge

        bridge = LogBridge()
        got: list = []
        bridge.message.connect(lambda level, text: got.append((level, text)))
        thread = threading.Thread(target=lambda: bridge.message.emit("WARN", "from-thread"))
        thread.start()
        thread.join(timeout=5)
        # 跨线程是 queued 连接：必须让主线程的事件循环真的处理一次，才会派发
        _APP.processEvents()
        self.assertTrue(got, "跨线程投递应被派发到主线程队列")


if __name__ == "__main__":
    unittest.main()
