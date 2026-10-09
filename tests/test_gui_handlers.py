"""Tk GUI 主窗口 handler 测试（v1.10.6，M15 补强）。

重评发现：`xianyu_alert/gui/app.py` 覆盖率只有 **49%**（`helpers.py` 有 95%）——
缺口全在**主窗口的交互处理器**上。这些 handler 大多只依赖"状态属性的读写 + 少量回调"，
用 **SimpleNamespace + __get__ 绑定** 就能在没有真实 Tk 窗口的情况下直接测，
不需要 xvfb，也不需要把 tkinter 装上。

本文件优先覆盖**分支多、变体易漏**的几个：
    _set_running（loop / once 两种模式的按钮态）
    _handle_ui_message（6 种消息类型的分发）
    on_clear_records（运行中拒绝 / 用户取消 / 真的清空）
    _tick（关闭中早退 / 运行中倒计时 / 停止态）
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.gui import XianyuAlertGUI  # noqa: E402


class FakeVar:
    """tkinter.StringVar 的最小替身。"""

    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class FakeWidget:
    """按钮替身：记录 configure 调用。"""

    def __init__(self) -> None:
        self.states: list[dict] = []

    def configure(self, **kwargs) -> None:  # noqa: ANN003
        self.states.append(kwargs)


def make_app(**attrs) -> types.SimpleNamespace:
    """构造 GUI stub（带常见属性）。"""
    app = types.SimpleNamespace()
    app.var_status = FakeVar()
    app.var_countdown = FakeVar()
    app.var_rounds = FakeVar()
    app.var_alerts = FakeVar()
    app.btn_start = FakeWidget()
    app.btn_once = FakeWidget()
    app.btn_stop = FakeWidget()
    app._running = False
    app._mode = ""
    app._next_run_at = 0.0
    app._closing = False
    app._tick_after_id = None
    app.logs: list = []
    app.alerts: list = []
    app.messages: list = []
    app._append_log = lambda level, text: app.logs.append((level, text))
    app._insert_alert_row = lambda row, to_top=False: app.alerts.append(row)
    app._show_message = lambda level, title, text: app.messages.append((level, title, text))
    app._check_config_mtime = lambda: None
    # handler 内部会调用 self 上的其它方法：SimpleNamespace 不会自动绑定，需显式挂上
    app._set_running = XianyuAlertGUI._set_running.__get__(app)
    app._tick = XianyuAlertGUI._tick.__get__(app)
    # 提醒记录表格与缓存（clear_records 之后会刷新它们）
    app.tree_alerts = types.SimpleNamespace(
        get_children=lambda: [],
        delete=lambda _item: None,
    )
    app._alert_urls = {}
    app._alert_product_ids = {}
    app._alert_sold = {}
    app._alert_total = 0
    for key, value in attrs.items():
        setattr(app, key, value)
    return app


class TestSetRunning(unittest.TestCase):
    """_set_running 的两种模式分支。"""

    def _bind(self, app):
        return XianyuAlertGUI._set_running.__get__(app)

    def test_loop_mode_enables_stop(self) -> None:
        app = make_app()
        app._mode = "loop"
        self._bind(app)(True)
        self.assertTrue(app._running)
        self.assertIn("循环", app.var_status.get())
        self.assertEqual(app.btn_start.states[-1], {"state": "disabled"})
        self.assertEqual(app.btn_once.states[-1], {"state": "disabled"})
        self.assertEqual(app.btn_stop.states[-1], {"state": "normal"})

    def test_single_round_mode_keeps_stop_disabled(self) -> None:
        app = make_app()
        app._mode = "once"
        self._bind(app)(True)
        self.assertIn("单轮", app.var_status.get())
        self.assertEqual(app.btn_stop.states[-1], {"state": "disabled"}, "单轮跑完即停，无需停止按钮")

    def test_stop_resets_state(self) -> None:
        app = make_app()
        app._mode = "loop"
        app._next_run_at = 123.0
        self._bind(app)(False)
        self.assertFalse(app._running)
        self.assertEqual(app._mode, "")
        self.assertEqual(app._next_run_at, 0.0)
        self.assertEqual(app.var_status.get(), "状态：已停止")
        self.assertEqual(app.btn_start.states[-1], {"state": "normal"})
        self.assertEqual(app.btn_stop.states[-1], {"state": "disabled"})


class TestHandleUiMessage(unittest.TestCase):
    """_handle_ui_message 的 6 条分发 + 未知类型。"""

    def _bind(self, app):
        return XianyuAlertGUI._handle_ui_message.__get__(app)

    def test_log(self) -> None:
        app = make_app()
        self._bind(app)("log", ("INFO", "hello"))
        self.assertEqual(app.logs, [("INFO", "hello")])

    def test_alert(self) -> None:
        app = make_app()
        self._bind(app)("alert", {"title": "x"})
        self.assertEqual(app.alerts, [{"title": "x"}])

    def test_status(self) -> None:
        app = make_app()
        self._bind(app)("status", {"rounds": 3, "alerts": 5})
        self.assertIn("3", app.var_rounds.get())
        self.assertIn("5", app.var_alerts.get())

    def test_state_delegates_to_set_running(self) -> None:
        app = make_app()
        app._mode = "loop"
        self._bind(app)("state", {"running": True})
        self.assertTrue(app._running, "state 消息应切换运行状态")

    def test_message(self) -> None:
        app = make_app()
        self._bind(app)("message", ("info", "标题", "正文"))
        self.assertEqual(app.messages, [("info", "标题", "正文")])

    def test_callable(self) -> None:
        app = make_app()
        called = []
        self._bind(app)("callable", lambda: called.append(1))
        self.assertEqual(called, [1])

    def test_unknown_kind_is_ignored(self) -> None:
        app = make_app()
        try:
            self._bind(app)("unknown-kind", None)
        except Exception as exc:  # pragma: no cover - 不应发生
            self.fail(f"未知消息类型不应抛异常：{exc}")


class TestClearRecords(unittest.TestCase):
    """on_clear_records 的三条分支。"""

    def _bind(self, app):
        return XianyuAlertGUI.on_clear_records.__get__(app)

    def test_refuses_while_worker_alive(self) -> None:
        app = make_app()
        app._worker_alive = lambda: True
        with mock.patch("xianyu_alert.gui.app.messagebox") as box:
            self._bind(app)()
        box.showinfo.assert_called_once()
        box.askyesno.assert_not_called()

    def test_user_cancels(self) -> None:
        app = make_app()
        app._worker_alive = lambda: False
        app._storage_path = ":memory:"
        with mock.patch("xianyu_alert.gui.app.messagebox") as box:
            box.askyesno.return_value = False
            self._bind(app)()
        box.askyesno.assert_called_once()

    def test_clears_records(self) -> None:
        app = make_app()
        app._worker_alive = lambda: False
        with tempfile.TemporaryDirectory() as tmp:
            app._storage_path = os.path.join(tmp, "x.db")
            with mock.patch("xianyu_alert.gui.app.messagebox") as box:
                box.askyesno.return_value = True
                self._bind(app)()
            # 表应已建好且为空
            import sqlite3

            conn = sqlite3.connect(app._storage_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM product").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)


class TestTick(unittest.TestCase):
    """_tick 的三条分支（含关闭中早退）。"""

    def _bind(self, app):
        return XianyuAlertGUI._tick.__get__(app)

    def test_returns_early_when_closing(self) -> None:
        app = make_app()
        app._closing = True
        app.root = types.SimpleNamespace(after=lambda *_a: self.fail("关闭中不应重新调度"))
        self._bind(app)()

    def test_running_shows_countdown(self) -> None:
        app = make_app()
        app._running = True
        app._next_run_at = 0.0
        scheduled = []
        app.root = types.SimpleNamespace(after=lambda ms, fn: scheduled.append(ms))
        self._bind(app)()
        self.assertIn("执行中", app.var_countdown.get())
        self.assertEqual(scheduled, [1000], "应继续调度下一次 tick")

    def test_stopped_shows_placeholder(self) -> None:
        app = make_app()
        app._running = False
        app.root = types.SimpleNamespace(after=lambda ms, fn: None)
        self._bind(app)()
        self.assertEqual(app.var_countdown.get(), "下次执行：--:--")


if __name__ == "__main__":
    unittest.main()
