"""GUI v3.5 纯函数 / 线程级测试：预置排除词可配置可持久化 + 关闭流程稳定性修复。

沿用 test_gui.py 系列的「抽纯函数 / 最小实例 / mock」模式，不真正显示窗口。

覆盖：
  1. 预置排除词：config 读写（缺省回退默认 / 显式配置 / 空列表关闭）、
     config_to_form / build_config_dict 往返、add_preset_excludes 定制 preset
  2. GUI 预置词编辑流程：_apply_preset_edit 写内存 + 写 raw_config、
     _default_filters 使用实例预置词（stub 回退默认）、on_add_preset_excludes 用定制预置词
  3. 关闭流程：on_close 置位 stop_event + join(带超时) + 移除 handler + 销毁窗口；
     用户取消不销毁；_closing 置位后 _poll_queue / _tick 不再重新调度 after
  4. 线程模型：_launch_worker 创建 daemon 线程；_monitor_worker 停止后关闭 fetcher/storage
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import (  # noqa: E402
    DEFAULT_PRESET_EXCLUDE_KEYWORDS,
    config_from_dict,
)
import xianyu_alert.gui as g  # noqa: E402
from xianyu_alert.gui import (  # noqa: E402
    CLOSE_JOIN_TIMEOUT,
    PRESET_EXCLUDE_KEYWORDS,
    XianyuAlertGUI,
    add_preset_excludes,
    build_config_dict,
    config_to_form,
    resolve_preset_exclude_keywords,
)


def make_config_dict(**overrides: object) -> dict:
    """构造能通过 config_from_dict 校验的最小配置字典（mock 抓取器）。"""
    data: dict = {
        "keywords": [{"keyword": "Switch", "max_price": 1000}],
        "monitor": {"interval_seconds": 60},
        "fetcher": {"type": "mock"},
        "storage": {"path": ":memory:"},
        "notify": {"channels": [{"type": "console"}]},
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------- #
# 1. 预置排除词：config 层读写与缺省回退
# ---------------------------------------------------------------------- #
class TestPresetConfig(unittest.TestCase):
    """config_from_dict / config_to_form / build_config_dict 的预置词行为。"""

    def test_config_missing_falls_back_to_default(self) -> None:
        """旧配置（无 preset_exclude_keywords 字段）→ 回退默认，向后兼容。"""
        cfg = config_from_dict(make_config_dict())
        self.assertEqual(cfg.preset_exclude_keywords, DEFAULT_PRESET_EXCLUDE_KEYWORDS)
        self.assertEqual(cfg.preset_exclude_keywords, list(PRESET_EXCLUDE_KEYWORDS))

    def test_config_explicit_list(self) -> None:
        """显式配置预置词 → 原样解析（去空去重保序）。"""
        cfg = config_from_dict(
            make_config_dict(preset_exclude_keywords=["商家", "实体店", "", "商家"])
        )
        self.assertEqual(cfg.preset_exclude_keywords, ["商家", "实体店"])

    def test_config_empty_list_disables_preset(self) -> None:
        """显式空列表 = 关闭自动预置（区别于缺省回退默认）。"""
        cfg = config_from_dict(make_config_dict(preset_exclude_keywords=[]))
        self.assertEqual(cfg.preset_exclude_keywords, [])

    def test_config_to_form_reads_preset(self) -> None:
        """GUI 表单读取预置词；缺省回退默认。"""
        form = config_to_form(
            {"keywords": [{"keyword": "Switch", "max_price": 100}],
             "preset_exclude_keywords": ["商家"]}
        )
        self.assertEqual(form["preset_exclude_keywords"], ["商家"])
        form_default = config_to_form({"keywords": [{"keyword": "Switch", "max_price": 100}]})
        self.assertEqual(form_default["preset_exclude_keywords"], DEFAULT_PRESET_EXCLUDE_KEYWORDS)

    def test_build_config_dict_writes_preset_when_provided(self) -> None:
        """GUI 保存路径：传入 preset 时显式写出。"""
        data = build_config_dict(
            keywords=[("Switch", 100.0)],
            interval_seconds=600,
            fetcher_type="mock",
            cookies="",
            storage_path=":memory:",
            channels={"console": {"enabled": True, "options": {}}},
            preset_exclude_keywords=["商家", "实体店"],
        )
        self.assertEqual(data["preset_exclude_keywords"], ["商家", "实体店"])
        # 与关键词的 exclude_keywords（结果）区分：预置词是顶层模板
        self.assertIn("keywords", data)

    def test_build_config_dict_preset_none_preserves_base(self) -> None:
        """preset 为 None 时保留 base 已有字段（向后兼容旧调用方）。"""
        data = build_config_dict(
            keywords=[("Switch", 100.0)],
            interval_seconds=600,
            fetcher_type="mock",
            cookies="",
            storage_path=":memory:",
            channels={"console": {"enabled": True, "options": {}}},
            base={"preset_exclude_keywords": ["旧词"]},
        )
        self.assertEqual(data["preset_exclude_keywords"], ["旧词"])

    def test_build_config_dict_preset_none_no_base_no_key(self) -> None:
        """preset 为 None 且 base 无该字段 → 不写（不污染旧行为）。"""
        data = build_config_dict(
            keywords=[("Switch", 100.0)],
            interval_seconds=600,
            fetcher_type="mock",
            cookies="",
            storage_path=":memory:",
            channels={"console": {"enabled": True, "options": {}}},
        )
        self.assertNotIn("preset_exclude_keywords", data)

    def test_add_preset_excludes_custom_preset(self) -> None:
        """add_preset_excludes 支持定制预置词；缺省仍用默认（向后兼容）。"""
        self.assertEqual(
            add_preset_excludes(["回收"], preset=["商家", "实体店"]),
            ["回收", "商家", "实体店"],
        )
        self.assertEqual(add_preset_excludes([], preset=[]), [])
        self.assertEqual(
            add_preset_excludes([]),
            ["回收", "置换", "收购", "高价回收", "收"],
        )


# ---------------------------------------------------------------------- #
# 2. GUI 预置词编辑流程
# ---------------------------------------------------------------------- #
class TestPresetEditFlow(unittest.TestCase):
    """GUI 编辑预置排除词：内存态 + raw_config 持久化 + 新关键词默认规则。"""

    def _gui_stub(self) -> XianyuAlertGUI:
        """不调用 __init__ 的最小实例。"""
        return object.__new__(XianyuAlertGUI)

    def test_apply_preset_edit_updates_memory_and_raw_config(self) -> None:
        """_apply_preset_edit：解析多行文本 → 更新内存态 + 写回 raw_config。"""
        gui = self._gui_stub()
        gui._preset_exclude_keywords = ["回收"]
        gui._raw_config = {}
        result = gui._apply_preset_edit("商家\n实体店\n\n商家\n  ")
        self.assertEqual(result, ["商家", "实体店"])
        self.assertEqual(gui._preset_exclude_keywords, ["商家", "实体店"])
        self.assertEqual(gui._raw_config["preset_exclude_keywords"], ["商家", "实体店"])

    def test_apply_preset_edit_empty(self) -> None:
        """清空预置词 = 关闭自动预置。"""
        gui = self._gui_stub()
        gui._preset_exclude_keywords = ["回收"]
        gui._raw_config = {}
        result = gui._apply_preset_edit("")
        self.assertEqual(result, [])
        self.assertEqual(gui._raw_config["preset_exclude_keywords"], [])

    def test_default_filters_uses_instance_presets(self) -> None:
        """_default_filters 使用实例预置词（用户定制后新关键词自动带上）。"""
        gui = self._gui_stub()
        gui._preset_exclude_keywords = ["商家", "实体店"]
        filters = gui._default_filters("Switch")
        self.assertEqual(filters, {"exclude_keywords": ["商家", "实体店"], "required_keywords": []})

    def test_default_filters_stub_falls_back_to_default(self) -> None:
        """无实例属性的 stub（既有测试模式）→ 回退默认预置词，向后兼容。"""
        gui = self._gui_stub()
        filters = gui._default_filters("Switch")
        self.assertEqual(filters["exclude_keywords"], ["回收", "置换", "收购", "高价回收", "收"])
        self.assertEqual(filters["required_keywords"], [])

    # ------------------------------------------------------------------ #
    # BUG-1 回归：显式空列表 = 关闭自动预置（不能用 falsy 判断回退默认）
    # ------------------------------------------------------------------ #
    def test_resolve_preset_empty_list_keeps_empty(self) -> None:
        """resolve_preset_exclude_keywords([]) → []（关闭自动预置，不回退默认）。"""
        self.assertEqual(resolve_preset_exclude_keywords([]), [])

    def test_resolve_preset_none_falls_back_to_default(self) -> None:
        """resolve_preset_exclude_keywords(None) → 默认 5 词（仅 None 缺省才回退）。"""
        self.assertEqual(
            resolve_preset_exclude_keywords(None),
            ["回收", "置换", "收购", "高价回收", "收"],
        )

    def test_resolve_preset_custom_list(self) -> None:
        """resolve_preset_exclude_keywords 保留定制非空列表。"""
        self.assertEqual(resolve_preset_exclude_keywords(["商家", "实体店"]), ["商家", "实体店"])

    def test_default_filters_empty_presets_disables_auto_preset(self) -> None:
        """实例预置词为 []（用户清空=关闭自动预置）→ 新关键词 exclude_keywords 为空。"""
        gui = self._gui_stub()
        gui._preset_exclude_keywords = []
        filters = gui._default_filters("Switch")
        self.assertEqual(filters["exclude_keywords"], [])
        self.assertEqual(filters["required_keywords"], [])

    def test_config_to_form_empty_preset_roundtrip(self) -> None:
        """config `preset_exclude_keywords: []` → config_to_form 为空 → GUI 解析仍为空。"""
        form = config_to_form(
            {"keywords": [{"keyword": "Switch", "max_price": 100}],
             "preset_exclude_keywords": []}
        )
        self.assertEqual(form["preset_exclude_keywords"], [])
        # 模拟 GUI __init__ 的解析路径（resolve_preset_exclude_keywords）
        self.assertEqual(resolve_preset_exclude_keywords(form["preset_exclude_keywords"]), [])

    def test_on_add_preset_excludes_uses_custom_presets(self) -> None:
        """「添加预置排除词」使用当前配置的定制预置词（而非写死常量）。"""
        gui = self._gui_stub()
        gui._preset_exclude_keywords = ["商家", "实体店"]
        gui._keyword_filters = {"Switch": {"exclude_keywords": [], "required_keywords": []}}

        class FakeTree:
            def selection(self) -> list:
                return ["iid1"]

            def item(self, _iid: str, _option: object = None) -> object:
                return ("Switch", "1000", "")

        gui.tree_keywords = FakeTree()
        gui._refresh_keyword_row = lambda keyword: None
        gui._append_log = lambda *args, **kwargs: None
        gui._ensure_filters = lambda keyword: None
        gui.on_add_preset_excludes()
        self.assertEqual(
            gui._keyword_filters["Switch"]["exclude_keywords"],
            ["商家", "实体店"],
        )


# ---------------------------------------------------------------------- #
# 3. 关闭流程稳定性修复
# ---------------------------------------------------------------------- #
class FakeWorker:
    """可记录的假监控线程。"""

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.joined_with: object = None

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout: object = None) -> None:
        self.joined_with = timeout


class FakeRoot:
    """可记录的假 Tk 根窗口。"""

    def __init__(self) -> None:
        self.destroyed = False
        self.cancelled: list = []

    def after_cancel(self, after_id: object) -> None:
        self.cancelled.append(after_id)

    def destroy(self) -> None:
        self.destroyed = True


class TestCloseFlow(unittest.TestCase):
    """on_close：stop_event → join(带超时) → 取消 after → 移除 handler → destroy。"""

    def _gui(self, worker: FakeWorker) -> XianyuAlertGUI:
        gui = object.__new__(XianyuAlertGUI)
        gui._worker = worker
        gui._stop_event = threading.Event()
        gui._closing = False
        gui._poll_after_id = "poll-id"
        gui._tick_after_id = "tick-id"
        gui.root = FakeRoot()
        removed: list = []
        gui._remove_log_handler = lambda: removed.append(True)
        gui._removed = removed
        return gui

    def test_on_close_sets_stop_event_joins_and_destroys(self) -> None:
        """关闭时：置位停止信号 + join(带超时) + 取消 after + 移除 handler + 销毁窗口。"""
        worker = FakeWorker(alive=True)
        gui = self._gui(worker)
        with mock.patch("xianyu_alert.gui.messagebox.askyesno", return_value=True):
            gui.on_close()
        self.assertTrue(gui._stop_event.is_set())
        self.assertEqual(worker.joined_with, CLOSE_JOIN_TIMEOUT)
        self.assertTrue(gui._closing)
        self.assertEqual(gui._removed, [True])
        self.assertEqual(gui.root.cancelled, ["poll-id", "tick-id"])
        self.assertTrue(gui.root.destroyed)

    def test_on_close_user_cancel_no_destroy(self) -> None:
        """用户取消退出 → 不置位停止信号、不销毁窗口。"""
        worker = FakeWorker(alive=True)
        gui = self._gui(worker)
        with mock.patch("xianyu_alert.gui.messagebox.askyesno", return_value=False):
            gui.on_close()
        self.assertFalse(gui._stop_event.is_set())
        self.assertIsNone(worker.joined_with)
        self.assertFalse(gui._closing)
        self.assertFalse(gui.root.destroyed)

    def test_on_close_worker_not_alive_no_confirm(self) -> None:
        """监控未运行时直接关闭（不弹确认框、不 join）。"""
        worker = FakeWorker(alive=False)
        gui = self._gui(worker)
        with mock.patch("xianyu_alert.gui.messagebox.askyesno", side_effect=AssertionError("不应弹确认框")):
            gui.on_close()
        self.assertFalse(gui._stop_event.is_set())
        self.assertIsNone(worker.joined_with)
        self.assertTrue(gui._closing)
        self.assertTrue(gui.root.destroyed)

    def test_on_close_join_exception_does_not_block_destroy(self) -> None:
        """join 抛异常（极端情况）也不阻断销毁窗口。"""

        class BadWorker(FakeWorker):
            def join(self, timeout: object = None) -> None:  # type: ignore[override]
                raise RuntimeError("join failed")

        worker = BadWorker(alive=True)
        gui = self._gui(worker)
        with mock.patch("xianyu_alert.gui.messagebox.askyesno", return_value=True):
            gui.on_close()
        self.assertTrue(gui.root.destroyed)

    def test_poll_queue_no_reschedule_after_closing(self) -> None:
        """_closing 置位后 _poll_queue 不重新调度 after（销毁后无回调残留）。"""
        gui = object.__new__(XianyuAlertGUI)
        gui._closing = True
        gui.ui_queue = queue.Queue()

        class BoomRoot:
            def after(self, *args: object) -> None:
                raise AssertionError("关闭后不应再调度 after")

        gui.root = BoomRoot()
        gui._poll_queue()  # 应静默返回

    def test_poll_queue_reschedules_when_open(self) -> None:
        """正常运行时 _poll_queue 消费空队列后按空闲间隔重新调度自身。"""
        gui = object.__new__(XianyuAlertGUI)
        gui._closing = False
        gui.ui_queue = queue.Queue()
        scheduled: list = []

        class RecordingRoot:
            def after(self, ms: int, cb: object) -> str:
                scheduled.append((ms, cb))
                return "new-poll-id"

        gui.root = RecordingRoot()
        gui._poll_queue()
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], g.POLL_IDLE_INTERVAL_MS)
        self.assertEqual(gui._poll_after_id, "new-poll-id")

    def test_poll_queue_fast_interval_when_busy(self) -> None:
        """队列有消息时，下一轮用 100ms 快速间隔（有消息 → 快速消费）。"""
        gui = object.__new__(XianyuAlertGUI)
        gui._closing = False
        gui.ui_queue = queue.Queue()
        gui.ui_queue.put(("log", ("INFO", "hi")))
        scheduled: list = []

        class RecordingRoot:
            def after(self, ms: int, cb: object) -> str:
                scheduled.append((ms, cb))
                return "new-poll-id"

        gui.root = RecordingRoot()
        gui._append_log = lambda *a, **k: None  # 避免触碰真实 widget
        gui._handle_ui_message = lambda kind, payload: None  # 不真正渲染
        gui._poll_queue()
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], g.POLL_INTERVAL_MS)

    def test_tick_no_reschedule_after_closing(self) -> None:
        """_closing 置位后 _tick 不再重新调度 after。"""
        gui = object.__new__(XianyuAlertGUI)
        gui._closing = True
        gui._running = False
        gui._next_run_at = 0.0
        gui.var_countdown = SimpleNamespace(set=lambda text: None)

        class BoomRoot:
            def after(self, *args: object) -> None:
                raise AssertionError("关闭后不应再调度 after")

        gui.root = BoomRoot()
        gui._tick()  # 应静默返回


# ---------------------------------------------------------------------- #
# 4. 线程模型：daemon 线程 + 停止后释放资源
# ---------------------------------------------------------------------- #
class TestThreadModel(unittest.TestCase):
    """监控线程 daemon=True；停止信号后 fetcher/storage 被关闭。"""

    def test_launch_worker_creates_daemon_thread(self) -> None:
        """_launch_worker 创建的监控线程必须是 daemon（进程不残留）。"""
        cfg = config_from_dict(make_config_dict())
        gui = object.__new__(XianyuAlertGUI)
        gui._build_config_object = lambda: cfg
        gui._set_running = lambda running: None
        gui._stop_event = threading.Event()
        gui._worker = None
        captured: dict = {}

        class FakeThread:
            def __init__(self, target: object = None, args: tuple = (),
                         kwargs: object = None, daemon: object = None,
                         name: str = "") -> None:
                captured["target"] = target
                captured["daemon"] = daemon
                captured["name"] = name

            def start(self) -> None:
                captured["started"] = True

        with mock.patch("xianyu_alert.gui.threading.Thread", FakeThread):
            gui._launch_worker(single_round=False)
        self.assertTrue(captured["started"])
        self.assertIs(captured["daemon"], True)
        self.assertEqual(captured["name"], "xianyu-monitor")

    def test_monitor_worker_closes_resources_on_stop(self) -> None:
        """停止信号置位后 _monitor_worker 退出并关闭 fetcher/storage（finally）。"""
        cfg = config_from_dict(make_config_dict())
        gui = object.__new__(XianyuAlertGUI)
        gui._stop_event = threading.Event()
        gui._stop_event.set()  # 预置停止信号：worker 进入循环后立即退出
        gui._round_no = 0
        gui._alert_total = 0
        gui._next_run_at = 0.0
        gui._push = lambda *args, **kwargs: None
        gui._push_message = lambda *args, **kwargs: None

        class FakeFetcher:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class FakeStorage:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class FakeMonitor:
            last_result = SimpleNamespace(notified_products=[])

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def preflight_cookie(self) -> None:
                pass

            def run_once(self, log_item_details: bool = False) -> int:
                return 0

        fetcher = FakeFetcher()
        storage = FakeStorage()
        with mock.patch("xianyu_alert.gui.build_fetcher", return_value=fetcher), \
             mock.patch("xianyu_alert.gui.Storage", return_value=storage), \
             mock.patch("xianyu_alert.gui.build_notifiers", return_value=[]), \
             mock.patch("xianyu_alert.gui.Monitor", FakeMonitor):
            gui._monitor_worker(cfg, single_round=False)

        self.assertTrue(fetcher.closed)
        self.assertTrue(storage.closed)
        self.assertEqual(gui._next_run_at, 0.0)

    def test_monitor_worker_stop_event_wait_breaks_loop(self) -> None:
        """循环中 event.wait 可被停止信号立刻打断（不阻塞 sleep）。"""
        cfg = config_from_dict(make_config_dict())
        gui = object.__new__(XianyuAlertGUI)
        gui._stop_event = threading.Event()
        gui._round_no = 0
        gui._alert_total = 0
        gui._next_run_at = 0.0
        gui._push = lambda *args, **kwargs: None
        gui._push_message = lambda *args, **kwargs: None

        class FakeFetcher:
            def close(self) -> None:
                pass

        class FakeStorage:
            def close(self) -> None:
                pass

        class FakeMonitor:
            last_result = SimpleNamespace(notified_products=[])

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def preflight_cookie(self) -> None:
                pass

            def run_once(self, log_item_details: bool = False) -> int:
                return 0

        # 用后台线程跑 worker，主线程 0.2s 后发停止信号，验证能及时退出
        with mock.patch("xianyu_alert.gui.build_fetcher", return_value=FakeFetcher()), \
             mock.patch("xianyu_alert.gui.Storage", return_value=FakeStorage()), \
             mock.patch("xianyu_alert.gui.build_notifiers", return_value=[]), \
             mock.patch("xianyu_alert.gui.Monitor", FakeMonitor):
            thread = threading.Thread(target=gui._monitor_worker, args=(cfg, False), daemon=True)
            thread.start()
            threading.Event().wait(0.2)
            gui._stop_event.set()
            thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
