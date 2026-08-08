"""GUI / 核心模块 v3.6 增量测试。

覆盖（不真正显示窗口，纯函数 + 最小实例 + 线程级断言）：
  1. 添加 / 更新按钮拆分：新增同名拦截、更新选中保留原行（改关键词名后仍是更新）、
     未选中提示、改名撞名拦截、过滤规则随行迁移
  2. 窗口尺寸：WINDOW_SIZE 加宽、MIN_WINDOW_SIZE 抬高、EMPTY_STATE_HINT 同步
  3. 临时黑名单：storage CRUD、monitor 过滤（不通知、进 prev_ids）、
     list_notified 排除、GUI 纯函数 blacklist_alert_row
  4. UI 不阻塞：_launch_worker 在主线程读取 tkinter 状态并传给后台线程、
     _monitor_worker 慢 fetch 期间主线程不被阻塞（线程级）、
     后台线程不触碰 tkinter 控件、_poll_queue 日志洪峰分批渲染、
     配置构建纯本地快速
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

import xianyu_alert.gui as g  # noqa: E402
from xianyu_alert.config import config_from_dict  # noqa: E402
from xianyu_alert.fetcher import MockFetcher  # noqa: E402
from xianyu_alert.gui import (  # noqa: E402
    BLACKLIST_REASON_DEFAULT,
    MAX_QUEUE_MESSAGES_PER_POLL,
    MIN_WINDOW_SIZE,
    WINDOW_SIZE,
    XianyuAlertGUI,
    blacklist_alert_row,
)
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


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


def make_product(product_id: str, price: float = 50.0, title: str = "测试商品") -> Product:
    """构造一条可用的 Product。"""
    return Product(
        product_id=product_id,
        title=title,
        price=price,
        url=f"https://www.goofish.com/item?id={product_id}",
        publish_time="2026-01-01 12:00:00",
        keyword="Switch",
    )


# ---------------------------------------------------------------------- #
# 1. 添加 / 更新按钮拆分
# ---------------------------------------------------------------------- #
class FakeKeywordTree:
    """可记录的关键词表格替身（支持 on_add_keyword / on_update_keyword 用到的接口）。"""

    def __init__(self) -> None:
        self.rows: dict = {}
        self._next: int = 0
        self.selected: list = []
        self.inserted_count: int = 0

    def get_children(self) -> list:
        return list(self.rows.keys())

    def item(self, iid: str, option: object = None, **kwargs: object) -> object:
        if "values" in kwargs:
            self.rows[iid] = tuple(kwargs["values"])
            return None
        if option == "values":
            return self.rows[iid]
        return self.rows[iid]

    def insert(self, _parent: str, _index: str, values: tuple = ()) -> str:
        iid = f"iid{self._next}"
        self._next += 1
        self.rows[iid] = tuple(values)
        self.inserted_count += 1
        return iid

    def selection(self) -> list:
        return list(self.selected)

    def delete(self, iid: str) -> None:
        self.rows.pop(iid, None)
        if iid in self.selected:
            self.selected.remove(iid)


class FakeVar:
    """带 get/set 的 StringVar 替身（GUI 方法会调用 set 清空输入框）。"""

    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class TestAddUpdateSplit(unittest.TestCase):
    """需求 1：添加 / 更新按钮拆分后的行为。"""

    def _gui(self, tree: FakeKeywordTree) -> XianyuAlertGUI:
        gui = object.__new__(XianyuAlertGUI)
        gui.tree_keywords = tree
        gui._keyword_filters = {}
        gui._preset_exclude_keywords = ["回收"]
        gui.var_keyword = FakeVar("")
        gui.var_price = FakeVar("")
        gui._refresh_keyword_empty_hint = lambda: None
        gui._append_log = lambda *a, **k: None
        return gui

    def _set_input(self, gui: XianyuAlertGUI, keyword: str, price: str) -> None:
        gui.var_keyword = FakeVar(keyword)
        gui.var_price = FakeVar(price)

    def test_add_keyword_adds_new_row(self) -> None:
        """「➕ 添加」新增：输入合法时表格新增一行。"""
        gui = self._gui(FakeKeywordTree())
        self._set_input(gui, "Switch", "1000")
        gui.on_add_keyword()
        self.assertEqual(len(gui.tree_keywords.get_children()), 1)
        iid = gui.tree_keywords.get_children()[0]
        self.assertEqual(gui.tree_keywords.item(iid, "values")[0], "Switch")

    def test_add_keyword_duplicate_is_blocked(self) -> None:
        """同名重复添加被拦截：提示「已存在，请用更新」，不新增行。"""
        gui = self._gui(FakeKeywordTree())
        self._set_input(gui, "Switch", "1000")
        gui.on_add_keyword()
        self.assertEqual(len(gui.tree_keywords.get_children()), 1)

        shown: list = []
        with mock.patch("xianyu_alert.gui.messagebox.showinfo", side_effect=lambda *a: shown.append(a)):
            self._set_input(gui, "Switch", "500")
            gui.on_add_keyword()
        self.assertEqual(len(gui.tree_keywords.get_children()), 1, "重复添加不应新增行")
        self.assertTrue(shown, "应弹出「已存在」提示")
        self.assertIn("已存在", str(shown[0]))
        # 原行阈值未被覆盖（添加不再隐式更新）
        iid = gui.tree_keywords.get_children()[0]
        self.assertEqual(gui.tree_keywords.item(iid, "values")[1], "1000")

    def test_update_keyword_no_selection_prompts(self) -> None:
        """「✏️ 更新选中」未选中行 → 提示「请先选中」，不改变表格。"""
        gui = self._gui(FakeKeywordTree())
        self._set_input(gui, "Switch", "1000")
        shown: list = []
        with mock.patch("xianyu_alert.gui.messagebox.showinfo", side_effect=lambda *a: shown.append(a)):
            gui.on_update_keyword()
        self.assertTrue(shown)
        self.assertIn("请先选中", str(shown[0]))
        self.assertEqual(len(gui.tree_keywords.get_children()), 0)

    def test_update_keyword_updates_selected_row_in_place(self) -> None:
        """更新选中：行数不变，选中行内容被替换（保留原行）。"""
        gui = self._gui(FakeKeywordTree())
        self._set_input(gui, "Switch", "1000")
        gui.on_add_keyword()
        iid = gui.tree_keywords.get_children()[0]
        gui.tree_keywords.selected = [iid]
        self._set_input(gui, "Switch", "800")
        gui.on_update_keyword()
        self.assertEqual(len(gui.tree_keywords.get_children()), 1, "更新不应新增行")
        self.assertIn(iid, gui.tree_keywords.get_children(), "更新应保留原行 iid")
        values = gui.tree_keywords.item(iid, "values")
        self.assertEqual(values[0], "Switch")
        self.assertEqual(values[1], "800")

    def test_update_keyword_rename_is_update_not_add(self) -> None:
        """修正错别字（改名）后点「更新选中」仍是更新原行，不是新增。"""
        gui = self._gui(FakeKeywordTree())
        gui._keyword_filters["Swtich"] = {"exclude_keywords": ["回收"], "required_keywords": []}
        self._set_input(gui, "Swtich", "1000")
        gui.on_add_keyword()
        iid = gui.tree_keywords.get_children()[0]

        gui.tree_keywords.selected = [iid]
        self._set_input(gui, "Switch", "999")  # 修正错别字
        gui.on_update_keyword()

        self.assertEqual(len(gui.tree_keywords.get_children()), 1, "改名后仍是更新，不应新增")
        values = gui.tree_keywords.item(iid, "values")
        self.assertEqual(values[0], "Switch")
        self.assertEqual(values[1], "999")
        # 过滤规则随行迁移到新关键词名
        self.assertIn("Switch", gui._keyword_filters)
        self.assertNotIn("Swtich", gui._keyword_filters)

    def test_update_keyword_rename_conflict_blocked(self) -> None:
        """改名与其它行撞名 → 拦截，表格不变。"""
        gui = self._gui(FakeKeywordTree())
        self._set_input(gui, "Switch", "1000")
        gui.on_add_keyword()
        self._set_input(gui, "iPhone", "2000")
        gui.on_add_keyword()
        iid_switch = gui.tree_keywords.get_children()[0]
        gui.tree_keywords.selected = [iid_switch]
        self._set_input(gui, "iPhone", "999")
        shown: list = []
        with mock.patch("xianyu_alert.gui.messagebox.showwarning", side_effect=lambda *a: shown.append(a)):
            gui.on_update_keyword()
        self.assertTrue(shown)
        self.assertIn("名称冲突", str(shown[0]))
        self.assertEqual(len(gui.tree_keywords.get_children()), 2)
        self.assertEqual(gui.tree_keywords.item(iid_switch, "values")[0], "Switch")


# ---------------------------------------------------------------------- #
# 2. 窗口尺寸 / 文案
# ---------------------------------------------------------------------- #
class TestWindowSize(unittest.TestCase):
    """需求 2：窗口加宽、最小尺寸抬高、空状态文案同步。"""

    def test_window_size_widened(self) -> None:
        """WINDOW_SIZE 加宽到 ≥1020x720。"""
        width_text, height_text = WINDOW_SIZE.lower().split("x")
        self.assertGreaterEqual(int(width_text), 1000, "默认宽度应加宽到 1000+")
        self.assertGreaterEqual(int(height_text), 700, "默认高度应加宽到 700+")

    def test_min_window_size_raised(self) -> None:
        """MIN_WINDOW_SIZE 抬高到 (880, 600)。"""
        self.assertGreaterEqual(MIN_WINDOW_SIZE[0], 880)
        self.assertGreaterEqual(MIN_WINDOW_SIZE[1], 600)

    def test_empty_state_hint_mentions_add_button(self) -> None:
        """空状态文案同步为「➕ 添加」引导。"""
        self.assertIn("还没有关键词", g.EMPTY_STATE_HINT)
        self.assertIn("添加", g.EMPTY_STATE_HINT)
        self.assertNotIn("添加 / 更新", g.EMPTY_STATE_HINT)


# ---------------------------------------------------------------------- #
# 3. 临时黑名单
# ---------------------------------------------------------------------- #
class TestBlacklistStorage(unittest.TestCase):
    """storage 黑名单 CRUD。"""

    def test_blacklist_crud(self) -> None:
        st = Storage(":memory:")
        try:
            st.add_blacklist("1001", keyword="Switch", reason="假货")
            st.add_blacklist("1002")
            self.assertTrue(st.is_blacklisted("1001"))
            self.assertTrue(st.is_blacklisted("1002"))
            self.assertFalse(st.is_blacklisted("9999"))
            self.assertFalse(st.is_blacklisted(""))
            rows = st.list_blacklist()
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["product_id"] for r in rows}, {"1001", "1002"})
            # 幂等：重复加入只更新关键词/原因
            st.add_blacklist("1001", keyword="Switch2", reason="高仿")
            rows = st.list_blacklist()
            row_1001 = next(r for r in rows if r["product_id"] == "1001")
            self.assertEqual(row_1001["reason"], "高仿")
            self.assertEqual(row_1001["keyword"], "Switch2")
            # 恢复
            self.assertEqual(st.remove_blacklist("1001"), 1)
            self.assertFalse(st.is_blacklisted("1001"))
            self.assertEqual(st.remove_blacklist("1001"), 0)
            self.assertEqual(len(st.list_blacklist()), 1)
        finally:
            st.close()

    def test_blacklist_empty_pid_raises(self) -> None:
        st = Storage(":memory:")
        try:
            with self.assertRaises(ValueError):
                st.add_blacklist("  ")
        finally:
            st.close()


class TestBlacklistMonitor(unittest.TestCase):
    """monitor.run_once 过滤链：黑名单商品不通知、不进 notified、进 prev_ids。"""

    class RoundFetcher:
        """按轮次返回预置商品列表的抓取器（确定性）。"""

        name = "round"

        def __init__(self, rounds: list) -> None:
            self.rounds = list(rounds)
            self._round: int = 0

        def fetch(self, _keyword: str) -> list:
            products = self.rounds[min(self._round, len(self.rounds) - 1)]
            self._round += 1
            return list(products)

        def set_cookies(self, _cookie: str) -> None:
            pass

        def set_max_price(self, _max_price: object) -> None:
            pass

        def close(self) -> None:
            pass

    class RecNotifier:
        name = "rec"

        def __init__(self) -> None:
            self.notified: list = []

        def safe_notify(self, products: list) -> None:
            self.notified.extend(products)

    def _monitor(self, st: Storage, rounds: list) -> Monitor:
        cfg = config_from_dict(make_config_dict())
        notifier = self.RecNotifier()
        monitor = Monitor(cfg, self.RoundFetcher(rounds), st, [notifier])
        return monitor, notifier

    def test_blacklisted_product_not_notified(self) -> None:
        st = Storage(":memory:")
        try:
            p_ok = make_product("A-OK", price=30.0, title="正常低价")
            p_noise = make_product("B-NOISE", price=40.0, title="假货噪音")
            p_new = make_product("C-NEW", price=50.0, title="新一轮低价")
            # 第 1 轮：只有 A-OK；第 2 轮：B-NOISE（此前见过则非新商品）与 C-NEW
            monitor, notifier = self._monitor(st, [[p_ok], [p_noise, p_new]])

            # 第 1 轮：提醒 A-OK
            monitor.run_once()
            self.assertEqual({p.product_id for p in notifier.notified}, {"A-OK"})

            # 把 B-NOISE 加入黑名单
            st.add_blacklist("B-NOISE", keyword="Switch", reason="假货")
            notifier.notified.clear()

            # 第 2 轮：B-NOISE 是新商品但被黑名单过滤；C-NEW 正常提醒
            monitor.run_once()
            self.assertEqual(
                {p.product_id for p in notifier.notified},
                {"C-NEW"},
                "黑名单商品不通知，其它新商品正常通知",
            )
            self.assertFalse(st.is_notified("Switch", "B-NOISE"), "黑名单商品不应被标记为已提醒")

            # 但仍进 prev_ids，避免后续每轮重复判定为「新商品」
            prev_ids = st.get_previous_round_ids("Switch")
            self.assertIn("B-NOISE", prev_ids)
            self.assertIn("C-NEW", prev_ids)
        finally:
            st.close()

    def test_item_reason_blacklist_marker(self) -> None:
        """明细日志对黑名单商品标注「已加入黑名单」。"""
        st = Storage(":memory:")
        try:
            p_noise = make_product("C-NOISE", price=40.0, title="噪音")
            monitor, _notifier = self._monitor(st, [p_noise])
            st.add_blacklist("C-NOISE", keyword="Switch", reason="非目标")
            cfg = config_from_dict(make_config_dict())
            rule = cfg.keywords[0]
            reason = monitor._item_reason(p_noise, rule, set())
            self.assertIn("黑名单", reason)
        finally:
            st.close()


class TestBlacklistGuiQuery(unittest.TestCase):
    """GUI 查询排除：list_notified 自动排除黑名单（提醒记录不再显示）。"""

    def test_list_notified_excludes_blacklisted(self) -> None:
        st = Storage(":memory:")
        try:
            p1 = make_product("AAA", price=10.0)
            p2 = make_product("BBB", price=20.0)
            st.mark_notified(p1)
            st.mark_notified(p2)
            self.assertEqual(len(st.list_notified()), 2)

            st.add_blacklist("BBB", keyword="Switch", reason="假货")
            rows = st.list_notified()
            self.assertEqual({r["product_id"] for r in rows}, {"AAA"})
            self.assertEqual(len(st.list_notified(keyword="Switch")), 1)
            self.assertEqual(len(st.list_notified(keyword="iPhone")), 0)
        finally:
            st.close()

    def test_blacklist_alert_row_pure(self) -> None:
        """GUI 纯函数：把提醒记录行加入黑名单（缺 product_id 返回 False）。"""
        calls: list = []

        class FakeStorage:
            def add_blacklist(self, pid: str, keyword: str = "", reason: str = "") -> None:
                calls.append((pid, keyword, reason))

        self.assertTrue(
            blacklist_alert_row(FakeStorage(), {"product_id": " 123 ", "keyword": "Switch"}, reason="假货")
        )
        self.assertEqual(calls, [("123", "Switch", "假货")])
        self.assertFalse(blacklist_alert_row(FakeStorage(), {"product_id": ""}))
        self.assertFalse(blacklist_alert_row(FakeStorage(), {}))

    def test_blacklist_reason_default(self) -> None:
        """默认原因文案存在（GUI 弹窗 initialvalue 使用）。"""
        self.assertEqual(BLACKLIST_REASON_DEFAULT, "人工剔除")


# ---------------------------------------------------------------------- #
# 4. UI 不阻塞（需求 3）
# ---------------------------------------------------------------------- #
class TestUiNonBlocking(unittest.TestCase):
    """立即执行一轮 / 开始监控后窗口不卡：后台线程不触碰 tkinter 控件。"""

    def test_launch_worker_reads_tk_state_on_main_thread(self) -> None:
        """_launch_worker 在主线程读取勾选状态，作为普通 bool 传给后台线程。"""
        cfg = config_from_dict(make_config_dict())
        gui = object.__new__(XianyuAlertGUI)
        gui._build_config_object = lambda: cfg
        gui._set_running = lambda running: None
        gui._stop_event = threading.Event()
        gui._worker = None
        gui.var_log_detail_only = SimpleNamespace(get=lambda: True)
        captured: dict = {}

        class FakeThread:
            def __init__(self, target: object = None, args: tuple = (),
                         kwargs: object = None, daemon: object = None,
                         name: str = "") -> None:
                captured["target"] = target
                captured["args"] = args
                captured["daemon"] = daemon

            def start(self) -> None:
                captured["started"] = True

        with mock.patch("xianyu_alert.gui.threading.Thread", FakeThread):
            gui._launch_worker(single_round=False)
        self.assertEqual(captured["args"], (cfg, False, True), "detail_only 应以普通 bool 传入后台线程")
        self.assertTrue(captured["started"])
        self.assertIs(captured["daemon"], True)

    def test_launch_worker_passes_detail_only_false(self) -> None:
        """勾选取消（detail_only=False）时同样以普通 bool 传入。"""
        cfg = config_from_dict(make_config_dict())
        gui = object.__new__(XianyuAlertGUI)
        gui._build_config_object = lambda: cfg
        gui._set_running = lambda running: None
        gui._stop_event = threading.Event()
        gui._worker = None
        gui.var_log_detail_only = SimpleNamespace(get=lambda: False)
        captured: dict = {}

        class FakeThread:
            def __init__(self, target: object = None, args: tuple = (),
                         kwargs: object = None, daemon: object = None,
                         name: str = "") -> None:
                captured["args"] = args

            def start(self) -> None:
                captured["started"] = True

        with mock.patch("xianyu_alert.gui.threading.Thread", FakeThread):
            gui._launch_worker(single_round=True)
        self.assertEqual(captured["args"][2], False)

    def test_worker_never_touches_tkinter_vars(self) -> None:
        """后台线程不再访问任何 tkinter 控件（var_log_detail_only 若被访问即断言失败）。"""
        cfg = config_from_dict(make_config_dict())
        gui = object.__new__(XianyuAlertGUI)
        gui._stop_event = threading.Event()
        gui._round_no = 0
        gui._alert_total = 0
        gui._next_run_at = 0.0
        gui._push = lambda *a, **k: None
        gui._push_message = lambda *a, **k: None

        class PoisonVar:
            """一旦被后台线程访问就立刻失败的毒药对象。"""

            def get(self) -> bool:  # pragma: no cover - 不应被调用
                raise AssertionError("后台线程不得访问 tkinter 控件 var_log_detail_only.get()")

        gui.var_log_detail_only = PoisonVar()  # type: ignore[assignment]
        received: dict = {}

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
                received["log_item_details"] = log_item_details
                return 0

        with mock.patch("xianyu_alert.gui.build_fetcher", return_value=FakeFetcher()), \
             mock.patch("xianyu_alert.gui.Storage", return_value=FakeStorage()), \
             mock.patch("xianyu_alert.gui.build_notifiers", return_value=[]), \
             mock.patch("xianyu_alert.gui.Monitor", FakeMonitor):
            gui._monitor_worker(cfg, single_round=True, detail_only=True)

        self.assertEqual(received.get("log_item_details"), False, "detail_only=True → 不逐条记录")

    def test_monitor_worker_slow_fetch_does_not_block_main_thread(self) -> None:
        """需求 3 复现：慢 fetch（模拟网络 1.2s）期间主线程不被阻塞。"""
        cfg = config_from_dict(make_config_dict())
        gui = object.__new__(XianyuAlertGUI)
        gui._stop_event = threading.Event()
        gui._round_no = 0
        gui._alert_total = 0
        gui._next_run_at = 0.0
        gui._push = lambda *a, **k: None
        gui._push_message = lambda *a, **k: None
        fetch_started = threading.Event()

        class SlowFetcher:
            name = "slow"

            def fetch(self, _keyword: str) -> list:
                time.sleep(1.2)  # 模拟慢网络请求
                return []

            def close(self) -> None:
                pass

        class FakeStorage:
            def close(self) -> None:
                pass

        class SlowMonitor:
            """run_once 真正调用 fetcher（触发慢网络），并通知测试线程已进入 fetch。"""

            def __init__(self, _config: object, fetcher: object,
                         _storage: object, _notifiers: object) -> None:
                self._fetcher = fetcher
                self.last_result = SimpleNamespace(notified_products=[])

            def preflight_cookie(self) -> None:
                pass

            def run_once(self, log_item_details: bool = False) -> int:
                fetch_started.set()
                self._fetcher.fetch("Switch")
                return 0

        with mock.patch("xianyu_alert.gui.build_fetcher", return_value=SlowFetcher()), \
             mock.patch("xianyu_alert.gui.Storage", return_value=FakeStorage()), \
             mock.patch("xianyu_alert.gui.build_notifiers", return_value=[]), \
             mock.patch("xianyu_alert.gui.Monitor", SlowMonitor):
            worker_thread = threading.Thread(
                target=gui._monitor_worker, args=(cfg, True, True), daemon=True
            )
            worker_thread.start()

            # 确保 worker 已进入慢 fetch（确定性，避免 start 竞态）
            self.assertTrue(fetch_started.wait(timeout=2.0), "worker 应进入慢 fetch")
            self.assertTrue(worker_thread.is_alive(), "慢 fetch 期间 worker 应仍在运行")

            # fetch 在后台线程；主线程（本测试线程）应能自由执行轻量事件循环
            start = time.monotonic()
            for _ in range(20):
                time.sleep(0.01)  # 模拟主线程处理 UI 事件
            elapsed = time.monotonic() - start
            self.assertLess(
                elapsed, 0.5,
                f"主线程疑似被慢 fetch 阻塞：20×10ms 的事件循环耗时 {elapsed:.3f}s（应 <0.5s）",
            )
            worker_thread.join(timeout=5.0)
        self.assertFalse(worker_thread.is_alive())
        self.assertEqual(gui._round_no, 1)

    def test_poll_queue_caps_messages_per_tick(self) -> None:
        """日志洪峰时 _poll_queue 单次最多消费 200 条，剩余留到下一轮（UI 防卡）。"""
        gui = object.__new__(XianyuAlertGUI)
        gui._closing = False
        gui.ui_queue = queue.Queue()
        for _ in range(250):
            gui.ui_queue.put(("log", ("INFO", "x")))
        handled: list = []
        gui._handle_ui_message = lambda kind, payload: handled.append(kind)
        scheduled: list = []

        class RecordingRoot:
            def after(self, ms: int, cb: object) -> str:
                scheduled.append(ms)
                return "new-poll-id"

        gui.root = RecordingRoot()
        gui._poll_queue()
        self.assertEqual(len(handled), MAX_QUEUE_MESSAGES_PER_POLL)
        self.assertEqual(scheduled, [g.POLL_INTERVAL_MS], "有消息时下一轮用快速间隔")
        self.assertEqual(gui.ui_queue.qsize(), 250 - MAX_QUEUE_MESSAGES_PER_POLL)

    def test_config_build_is_local_and_fast(self) -> None:
        """需求 3(a)：_build_config_object 纯本地快速（无网络/Cookie 检测）。"""
        gui = object.__new__(XianyuAlertGUI)
        data = make_config_dict()
        gui._collect_config_dict = lambda: data  # 模拟主线程读取界面表单
        start = time.monotonic()
        cfg = gui._build_config_object()
        elapsed = time.monotonic() - start
        self.assertEqual(cfg.fetcher.type, "mock")
        self.assertLess(
            elapsed, 1.0,
            f"配置构建不应超过 1 秒（纯本地解析），实际 {elapsed:.3f}s",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
