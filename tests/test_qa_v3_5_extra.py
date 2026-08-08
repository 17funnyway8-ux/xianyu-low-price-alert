"""QA 独立验证 —— v3.5 增量补充边界用例（test_qa_v3_5_extra）。

由 QA 工程师（严过关）独立编写，聚焦工程师自测未覆盖的边界：

  1. 预置排除词：缺省回退 / 显式空列表 = 关闭自动预置 / 编辑往返（含中文词）/
     类型非法（非 list）不崩 / GUI 层空列表语义（★ 重点风险）
  2. 关闭流程：on_close 在 worker 未启动时调用不崩（防御）/ join 超时后仍继续
     关闭不卡死（模拟 worker 卡在网络请求）/ after_cancel 异常不阻断销毁
  3. _default_filters：无 _preset_exclude_keywords 属性回退默认（不抛 AttributeError）
  4. 自适应轮询：常量取值 + 空闲间隔 500ms / 忙间隔 100ms 的调度逻辑

说明：所有断言均依据 v3.5 需求文档（PRD：显式空列表 = 关闭自动预置）。
若某用例失败，则说明实现偏离需求 —— 属于源码缺陷，应回传工程师修复。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

from xianyu_alert.config import (  # noqa: E402
    DEFAULT_PRESET_EXCLUDE_KEYWORDS,
    ConfigError,
    config_from_dict,
)
import xianyu_alert.gui as g  # noqa: E402
from xianyu_alert.gui import (  # noqa: E402
    CLOSE_JOIN_TIMEOUT,
    POLL_IDLE_INTERVAL_MS,
    POLL_INTERVAL_MS,
    XianyuAlertGUI,
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


def gui_stub() -> XianyuAlertGUI:
    """不调用 __init__ 的最小实例（沿用既有测试模式）。"""
    return object.__new__(XianyuAlertGUI)


class FakeRoot:
    """可记录的假 Tk 根窗口。"""

    def __init__(self) -> None:
        self.destroyed = False
        self.cancelled: list = []

    def after_cancel(self, after_id: object) -> None:
        self.cancelled.append(after_id)

    def destroy(self) -> None:
        self.destroyed = True


# ---------------------------------------------------------------------- #
# 1. 预置排除词：边界
# ---------------------------------------------------------------------- #
class TestPresetExtra(unittest.TestCase):
    """预置词的缺省 / 空列表 / 类型非法 / 编辑往返。"""

    def test_config_missing_field_falls_back_default(self) -> None:
        """旧配置无该字段 → 回退默认 5 词（向后兼容）。"""
        cfg = config_from_dict(make_config_dict())
        self.assertEqual(
            cfg.preset_exclude_keywords, ["回收", "置换", "收购", "高价回收", "收"]
        )

    def test_config_explicit_empty_list_disables(self) -> None:
        """显式空列表 = 关闭自动预置（config 层）。"""
        cfg = config_from_dict(make_config_dict(preset_exclude_keywords=[]))
        self.assertEqual(cfg.preset_exclude_keywords, [])

    def test_config_non_list_raises_config_error(self) -> None:
        """config 层：非 list 类型 → 抛出受控 ConfigError（不静默吞掉）。"""
        with self.assertRaises(ConfigError):
            config_from_dict(make_config_dict(preset_exclude_keywords="回收"))

    def test_config_to_form_non_list_does_not_crash(self) -> None:
        """GUI 表单读取路径：非 list 脏数据 → 容错回退默认，不抛异常。"""
        form = config_to_form(
            {"keywords": [{"keyword": "Switch", "max_price": 100}],
             "preset_exclude_keywords": "回收"}
        )
        self.assertEqual(form["preset_exclude_keywords"], DEFAULT_PRESET_EXCLUDE_KEYWORDS)

    def test_config_preset_dedup_and_order(self) -> None:
        """去空 / 去重 / 保序。"""
        cfg = config_from_dict(
            make_config_dict(preset_exclude_keywords=["商家", "", "实体店", "商家", "  "])
        )
        self.assertEqual(cfg.preset_exclude_keywords, ["商家", "实体店"])

    def test_edit_roundtrip_chinese(self) -> None:
        """编辑往返（含中文词）：_apply_preset_edit → 内存态 + raw_config → 落盘往返。"""
        gui = gui_stub()
        gui._preset_exclude_keywords = ["回收"]
        gui._raw_config = {}
        result = gui._apply_preset_edit("高价回收\n回收\n\n商家")
        self.assertEqual(result, ["高价回收", "回收", "商家"])
        self.assertEqual(gui._preset_exclude_keywords, ["高价回收", "回收", "商家"])
        self.assertEqual(gui._raw_config["preset_exclude_keywords"], ["高价回收", "回收", "商家"])
        # 保存路径：build_config_dict 写入 → config_from_dict 再读回 = 同一份
        data = build_config_dict(
            keywords=[("Switch", 100.0)],
            interval_seconds=600,
            fetcher_type="mock",
            cookies="",
            storage_path=":memory:",
            channels={"console": {"enabled": True, "options": {}}},
            preset_exclude_keywords=gui._preset_exclude_keywords,
        )
        cfg = config_from_dict(data)
        self.assertEqual(cfg.preset_exclude_keywords, ["高价回收", "回收", "商家"])

    def test_edit_roundtrip_persist_empty(self) -> None:
        """编辑为空 → 内存态 + raw_config 均为空列表（持久化为关闭自动预置）。"""
        gui = gui_stub()
        gui._preset_exclude_keywords = ["回收"]
        gui._raw_config = {}
        result = gui._apply_preset_edit("\n  \n")
        self.assertEqual(result, [])
        self.assertEqual(gui._preset_exclude_keywords, [])
        self.assertEqual(gui._raw_config["preset_exclude_keywords"], [])

    def test_gui_init_expression_respects_empty_list(self) -> None:
        """★ 重点：GUI __init__ 的赋值表达式在 form 为 [] 时应保留空列表。

        复刻 `XianyuAlertGUI.__init__` 中的**实际（修复后）表达式**：
            resolve_preset_exclude_keywords(form.get("preset_exclude_keywords"))
        （BUG-1 修复：旧表达式 `list(... or DEFAULT_PRESET_EXCLUDE_KEYWORDS)` 用 falsy
        判断，空列表被回退成默认 5 词；现改为 None 判断，仅缺省才回退默认。）
        需求：显式空列表 = 关闭自动预置 → 应得到 []，而非回退默认 5 词。
        """
        form = config_to_form(
            {"keywords": [{"keyword": "Switch", "max_price": 100}],
             "preset_exclude_keywords": []}
        )
        self.assertEqual(form.get("preset_exclude_keywords"), [])
        presets = resolve_preset_exclude_keywords(
            form.get("preset_exclude_keywords")
        )
        self.assertEqual(
            presets,
            [],
            "显式空列表应保留为空（关闭自动预置），而非回退默认 5 词",
        )

    def test_default_filters_explicit_empty_list_disables(self) -> None:
        """★ 重点：_default_filters 在实例预置词为 []（关闭自动预置）时应返回空排除词。"""
        gui = gui_stub()
        gui._preset_exclude_keywords = []  # 用户清空预置词 = 关闭自动预置
        filters = gui._default_filters("Switch")
        self.assertEqual(
            filters,
            {"exclude_keywords": [], "required_keywords": []},
            "关闭自动预置后，新关键词不应再自动带上默认 5 词",
        )

    def test_default_filters_custom_presets_apply(self) -> None:
        """定制非空预置词 → 新关键词自动带上（需求主路径）。"""
        gui = gui_stub()
        gui._preset_exclude_keywords = ["商家", "实体店"]
        filters = gui._default_filters("Switch")
        self.assertEqual(
            filters, {"exclude_keywords": ["商家", "实体店"], "required_keywords": []}
        )

    def test_default_filters_stub_no_attr_no_attributeerror(self) -> None:
        """stub 无 _preset_exclude_keywords 属性 → 回退默认，不抛 AttributeError。"""
        gui = gui_stub()
        self.assertFalse(hasattr(gui, "_preset_exclude_keywords"))
        filters = gui._default_filters("Switch")
        self.assertEqual(
            filters["exclude_keywords"], ["回收", "置换", "收购", "高价回收", "收"]
        )


# ---------------------------------------------------------------------- #
# 2. 关闭流程：边界
# ---------------------------------------------------------------------- #
class TestCloseFlowExtra(unittest.TestCase):
    """on_close 防御性边界：worker 未启动 / join 超时 / after_cancel 异常。"""

    def _close_gui(self, worker: object = None) -> XianyuAlertGUI:
        gui = gui_stub()
        gui._worker = worker
        gui._stop_event = threading.Event()
        gui._closing = False
        gui._poll_after_id = None
        gui._tick_after_id = None
        gui.root = FakeRoot()
        gui._remove_log_handler = lambda: None
        return gui

    def test_on_close_worker_none_no_crash(self) -> None:
        """worker 从未启动（None）→ 直接关闭不崩、不弹确认框。"""
        gui = self._close_gui(worker=None)
        with mock.patch(
            "xianyu_alert.gui.messagebox.askyesno",
            side_effect=AssertionError("worker 未启动不应弹确认框"),
        ):
            gui.on_close()
        self.assertTrue(gui._closing)
        self.assertTrue(gui.root.destroyed)

    def test_on_close_after_id_none_no_crash(self) -> None:
        """_poll_after_id / _tick_after_id 为 None（尚未调度过）→ after_cancel 跳过不崩。"""
        gui = self._close_gui(worker=None)
        gui.on_close()
        self.assertEqual(gui.root.cancelled, [])
        self.assertTrue(gui.root.destroyed)

    def test_on_close_join_timeout_still_destroys_stuck_worker(self) -> None:
        """★ 重点：worker 卡在网络请求（不响应停止信号）→ join 超时后仍继续关闭不卡死。

        用真实 daemon 线程模拟卡死的监控线程：线程收到 stop 信号后仍 sleep，
        不回退 → on_close 的 join(timeout=CLOSE_JOIN_TIMEOUT) 超时后继续销毁窗口。
        """
        stop = threading.Event()

        def stuck_worker() -> None:
            # 模拟 mtop 网络请求：不检查 stop_event，一直阻塞
            stop.wait(60)

        thread = threading.Thread(target=stuck_worker, daemon=True)
        thread.start()

        gui = self._close_gui(worker=thread)
        started = time.monotonic()
        with mock.patch("xianyu_alert.gui.messagebox.askyesno", return_value=True):
            gui.on_close()
        elapsed = time.monotonic() - started

        self.assertTrue(gui._stop_event.is_set())
        self.assertTrue(gui._closing)
        self.assertTrue(gui.root.destroyed)
        # join 超时后应立即继续，不无限等待；上限放宽到超时 + 1s 容差
        self.assertLess(elapsed, CLOSE_JOIN_TIMEOUT + 1.0)
        self.assertTrue(thread.is_alive())  # 线程仍卡着，但关闭流程已完成 → 不残留阻塞
        stop.set()  # 释放测试线程

    def test_on_close_after_cancel_exception_does_not_block(self) -> None:
        """after_cancel 抛 TclError（已销毁窗口等）→ 被捕获，不阻断后续 destroy。"""
        gui = self._close_gui(worker=None)
        gui._poll_after_id = "poll-id"
        gui._tick_after_id = "tick-id"

        class FlakyRoot(FakeRoot):
            def after_cancel(self, after_id: object) -> None:  # type: ignore[override]
                raise tk.TclError("bad window path name")

        gui.root = FlakyRoot()
        gui.on_close()
        self.assertTrue(gui.root.destroyed)

    def test_poll_queue_closing_no_reschedule(self) -> None:
        """_closing 置位后 _poll_queue 即使队列有消息也不重新调度 after。"""
        gui = gui_stub()
        gui._closing = True
        gui.ui_queue = queue.Queue()
        gui.ui_queue.put(("log", ("INFO", "hi")))

        class BoomRoot:
            def after(self, *args: object) -> None:
                raise AssertionError("关闭后不应再调度 after")

        gui.root = BoomRoot()
        gui._poll_queue()  # 应静默返回

    def test_tick_closing_no_reschedule(self) -> None:
        """_closing 置位后 _tick 不再重排 after。"""
        gui = gui_stub()
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
# 3. 自适应轮询：常量与调度逻辑
# ---------------------------------------------------------------------- #
class TestAdaptivePolling(unittest.TestCase):
    """自适应轮询：空闲 500ms / 有消息 100ms / 常量取值。"""

    def test_constants_expected_values(self) -> None:
        """常量取值符合需求：忙 100ms / 空闲 500ms / 关闭 join 超时 5s。"""
        self.assertEqual(POLL_INTERVAL_MS, 100)
        self.assertEqual(POLL_IDLE_INTERVAL_MS, 500)
        self.assertEqual(CLOSE_JOIN_TIMEOUT, 5.0)

    def test_poll_queue_empty_uses_idle_interval(self) -> None:
        """队列为空（挂机）→ 下一次轮询间隔为 500ms。"""
        gui = gui_stub()
        gui._closing = False
        gui.ui_queue = queue.Queue()
        scheduled: list = []

        class RecordingRoot:
            def after(self, ms: int, cb: object) -> str:
                scheduled.append((ms, cb))
                return "new-id"

        gui.root = RecordingRoot()
        gui._poll_queue()
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], POLL_IDLE_INTERVAL_MS)

    def test_poll_queue_busy_uses_fast_interval(self) -> None:
        """队列有消息 → 下一次轮询间隔恢复 100ms（快速消费）。"""
        gui = gui_stub()
        gui._closing = False
        gui.ui_queue = queue.Queue()
        gui.ui_queue.put(("log", ("INFO", "hi")))
        scheduled: list = []

        class RecordingRoot:
            def after(self, ms: int, cb: object) -> str:
                scheduled.append((ms, cb))
                return "new-id"

        gui.root = RecordingRoot()
        gui._handle_ui_message = lambda kind, payload: None
        gui._poll_queue()
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], POLL_INTERVAL_MS)


# ---------------------------------------------------------------------- #
# 4. 真实 Tk 集成（可选：环境不支持 Tk 时跳过）
# ---------------------------------------------------------------------- #
@unittest.skipUnless(
    os.environ.get("XIANYU_QA_REAL_TK") == "1", "设置 XIANYU_QA_REAL_TK=1 才运行真实 Tk 集成用例"
)
class TestRealTkIntegration(unittest.TestCase):
    """真实 Tk 建窗验证：显式空列表应关闭自动预置（★ 重点风险）。"""

    def _make_config(self, tmpdir: str, preset: object) -> str:
        import yaml

        path = os.path.join(tmpdir, "config.yaml")
        data = {
            "keywords": [{"keyword": "Switch", "max_price": 1000}],
            "monitor": {"interval_seconds": 600, "user_agent": "", "cookies": ""},
            "fetcher": {"type": "mock", "mock_products_per_round": 5, "mock_fail_rounds": []},
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        }
        if preset is not None:
            data["preset_exclude_keywords"] = preset
        with open(path, "w", encoding="utf-8") as fp:
            yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)
        return path

    def test_real_tk_empty_preset_disables(self) -> None:
        """真实 GUI 启动：config 显式空列表 → _preset_exclude_keywords 应为 []。"""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="qa_v35_tk_") as d:
            cp = self._make_config(d, preset=[])
            root = tk.Tk()
            root.withdraw()
            gui: object = None
            try:
                gui = XianyuAlertGUI(root, config_path=cp)
                self.assertEqual(
                    gui._preset_exclude_keywords,
                    [],
                    "真实 GUI 启动时显式空列表应关闭自动预置",
                )
                filters = gui._default_filters("iPhone")
                self.assertEqual(
                    filters["exclude_keywords"],
                    [],
                    "关闭自动预置后新关键词不应自动带上默认 5 词",
                )
            finally:
                if gui is not None:
                    gui.on_close()
                else:
                    try:
                        root.destroy()
                    except Exception:  # noqa: BLE001
                        pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
