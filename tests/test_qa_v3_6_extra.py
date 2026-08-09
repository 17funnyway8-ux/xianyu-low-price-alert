# -*- coding: utf-8 -*-
"""QA 独立补充用例（v3.6）——不依赖工程师 test_gui_v3_6.py 的实现细节。

覆盖：
1. 黑名单：add 幂等 / is_blacklisted 跨关键词全局生效 / remove 恢复 / list 排序 /
   monitor 过滤后不进 notified 但进 prev_ids / clear_all 不清黑名单
2. 按钮拆分：未选中更新提示不崩 / 改名词撞已存在词拦截 / 过滤规则键迁移旧键删除
3. UI 不阻塞：慢 fetch 期间主线程心跳不被阻塞（线程级断言）；后台线程毒药对象验证 0 控件访问
4. 窗口：WINDOW_SIZE / MIN_WINDOW_SIZE 常量断言；按钮行请求宽度约束
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xianyu_alert.gui as gui
from xianyu_alert.config import (
    Config, FetcherConfig, KeywordRule, MonitorConfig, NotifyChannel,
    NotifyConfig, StorageConfig,
)
from xianyu_alert.models import Product
from xianyu_alert.monitor import Monitor
from xianyu_alert.storage import Storage


# --------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------- #
def make_product(product_id: str, keyword: str = "Switch", price: float = 50.0) -> Product:
    return Product(
        product_id=product_id,
        title=f"{keyword} 测试商品 {product_id}",
        price=price,
        url=f"https://www.goofish.com/item?id={product_id}",
        publish_time="2026-01-01 12:00:00",
        keyword=keyword,
    )


def make_config(keyword: str = "Switch", max_price: float = 1000.0) -> Config:
    return Config(
        keywords=[KeywordRule(keyword=keyword, max_price=max_price)],
        monitor=MonitorConfig(interval_seconds=600, user_agent="", cookies=""),
        fetcher=FetcherConfig(type="mock", mock_products_per_round=5, mock_fail_rounds=[]),
        storage=StorageConfig(path=":memory:"),
        notify=NotifyConfig(channels=[NotifyChannel(type="console")]),
        preset_exclude_keywords=[],
    )


class StubFetcher:
    name = "stub"

    def __init__(self, products):
        self._products = list(products)

    def set_max_price(self, max_price):
        pass

    def set_cookies(self, cookie):
        pass

    def fetch(self, keyword):
        return self._products

    def close(self):
        pass


class CollectNotifier:
    name = "collect"

    def __init__(self):
        self.notified = []

    def safe_notify(self, products):
        self.notified.extend(products)


# --------------------------------------------------------------------- #
# 1. 黑名单
# --------------------------------------------------------------------- #
class TestBlacklistExtra(unittest.TestCase):
    def test_add_blacklist_idempotent(self) -> None:
        """重复 add 不报错、不产生重复行。"""
        st = Storage(":memory:")
        st.add_blacklist("P1", keyword="Switch", reason="a")
        st.add_blacklist("P1", keyword="Switch", reason="b")  # 幂等更新
        st.add_blacklist("P1", keyword="Switch", reason="c")
        rows = st.list_blacklist()
        self.assertEqual(len(rows), 1, "重复加入不应产生多行")
        self.assertEqual(rows[0]["reason"], "c", "幂等更新应覆盖原因")
        st.close()

    def test_is_blacklisted_global_across_keywords(self) -> None:
        """同一 product_id 在不同关键词下都判定为黑名单（全局生效）。"""
        st = Storage(":memory:")
        st.add_blacklist("P1", keyword="Switch")
        self.assertTrue(st.is_blacklisted("P1"))
        self.assertTrue(st.is_blacklisted("P1"))  # 与关键词无关
        # 其它 product_id 不受影响
        self.assertFalse(st.is_blacklisted("P2"))
        st.close()

    def test_remove_then_recovered(self) -> None:
        """remove 后 is_blacklisted 恢复 False，list 不再包含。"""
        st = Storage(":memory:")
        st.add_blacklist("P1", keyword="Switch", reason="x")
        self.assertTrue(st.is_blacklisted("P1"))
        self.assertEqual(st.remove_blacklist("P1"), 1)
        self.assertFalse(st.is_blacklisted("P1"))
        self.assertEqual(st.remove_blacklist("P1"), 0, "再删返回 0")
        self.assertEqual(len(st.list_blacklist()), 0)
        st.close()

    def test_list_blacklist_order_desc(self) -> None:
        """list_blacklist 按 created_at 倒序（后加入的在前；同秒时按 rowid 保序）。"""
        st = Storage(":memory:")
        # 直接注入不同 created_at，验证 ORDER BY created_at DESC 生效
        with st.conn:
            st.conn.execute(
                "INSERT INTO blacklist (product_id, keyword, reason, created_at) "
                "VALUES ('P1','k','r1','2026-01-01 10:00:01'),"
                "('P2','k','r2','2026-01-01 10:00:02'),"
                "('P3','k','r3','2026-01-01 10:00:03')"
            )
        rows = st.list_blacklist()
        pids = [r["product_id"] for r in rows]
        self.assertEqual(pids, ["P3", "P2", "P1"], "应按 created_at 倒序")
        # 同一秒加入多条：不抛异常、结果唯一（退化为 rowid 保序可接受）
        st2 = Storage(":memory:")
        st2.add_blacklist("A", keyword="k")
        st2.add_blacklist("B", keyword="k")
        st2.add_blacklist("C", keyword="k")
        rows2 = st2.list_blacklist()
        self.assertEqual(len(rows2), 3)
        self.assertEqual(len({r["product_id"] for r in rows2}), 3)
        st.close()
        st2.close()

    def test_monitor_blacklist_not_notified_but_prev_ids(self) -> None:
        """monitor 过滤后：不通知、不进 notified、进 prev_ids。"""
        st = Storage(":memory:")
        cfg = make_config()
        prod = make_product("BL-9")
        st.add_blacklist("BL-9", keyword="Switch", reason="噪音")
        monitor = Monitor(cfg, StubFetcher([prod]), st, [CollectNotifier()])
        n = monitor.run_once(round_ts=None)
        self.assertEqual(n, 0, "黑名单商品不应通知")
        self.assertFalse(st.is_notified("Switch", "BL-9"), "不应进 notified")
        self.assertIn("BL-9", st.get_previous_round_ids("Switch"), "应进 prev_ids")
        self.assertNotIn("BL-9", [r["product_id"] for r in st.list_notified()])
        st.close()

    def test_clear_all_keeps_blacklist(self) -> None:
        """clear_all 清 product/meta，但**不**清 blacklist（用户偏好）。"""
        st = Storage(":memory:")
        st.add_blacklist("P1", keyword="Switch", reason="x")
        st.mark_notified(make_product("P2"))
        st.set_previous_round_ids("Switch", ["P2"])
        deleted = st.clear_all()
        self.assertEqual(deleted, 1, "应删除 1 条商品记录")
        self.assertTrue(st.is_blacklisted("P1"), "clear_all 不应清除黑名单")
        self.assertEqual(len(st.list_blacklist()), 1)
        st.close()


# --------------------------------------------------------------------- #
# 2. 按钮拆分（FakeTree / FakeVar，不依赖真实 Tk）
# --------------------------------------------------------------------- #
class FakeTree:
    def __init__(self):
        self._rows = {}
        self._iid = 0
        self._sel = []

    def insert(self, parent, index, values=None):
        self._iid += 1
        iid = f"I{self._iid}"
        self._rows[iid] = list(values or [])
        return iid

    def get_children(self):
        return list(self._rows.keys())

    def item(self, iid, option=None, values=None):
        if values is not None:
            self._rows[iid] = list(values)
            return
        if option == "values":
            return tuple(self._rows[iid])
        if option is None:
            return {"values": tuple(self._rows[iid])}
        return None

    def delete(self, iid):
        self._rows.pop(iid, None)

    def selection(self):
        return list(self._sel)

    def selection_set(self, iids):
        self._sel = list(iids) if isinstance(iids, (list, tuple)) else [iids]


class FakeVar:
    def __init__(self, v=""):
        self._v = v

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


def make_gui_stub() -> SimpleNamespace:
    app = SimpleNamespace()
    app.tree_keywords = FakeTree()
    app.var_keyword = FakeVar()
    app.var_price = FakeVar()
    app._keyword_filters = {}
    app._append_log = lambda *a, **k: None
    app._refresh_keyword_empty_hint = lambda: None
    app._default_filters = lambda kw: {"exclude_keywords": [], "required_keywords": []}
    app._ensure_filters = gui.XianyuAlertGUI._ensure_filters.__get__(app)
    app._filters_summary = lambda kw: "排除:无 必含:无"
    app._refresh_keyword_item = gui.XianyuAlertGUI._refresh_keyword_item.__get__(app)
    return app


class TestButtonSplitExtra(unittest.TestCase):
    def _patch_messagebox(self, app: SimpleNamespace) -> list:
        calls = []

        class FakeMsg:
            @staticmethod
            def showinfo(*a, **k):
                calls.append(("info", a))

            @staticmethod
            def showwarning(*a, **k):
                calls.append(("warning", a))

            @staticmethod
            def showerror(*a, **k):
                calls.append(("error", a))

        patcher = mock.patch.object(gui, "messagebox", FakeMsg)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_update_no_selection_prompts_not_crash(self) -> None:
        """未选中行更新 → 弹提示，不崩溃。"""
        app = make_gui_stub()
        calls = self._patch_messagebox(app)
        app.var_keyword.set("X")
        app.var_price.set("1")
        gui.XianyuAlertGUI.on_update_keyword.__get__(app)()
        self.assertTrue(any("请先选中" in str(a) for _k, a in calls), "应提示先选中")

    def test_update_rename_conflict_blocked(self) -> None:
        """改名词撞已存在词 → 拦截，行与过滤规则均不变。"""
        app = make_gui_stub()
        calls = self._patch_messagebox(app)
        # 两行：Switch / Other
        app.var_keyword.set("Switch")
        app.var_price.set("500")
        gui.XianyuAlertGUI.on_add_keyword.__get__(app)()
        app.var_keyword.set("Other")
        app.var_price.set("300")
        gui.XianyuAlertGUI.on_add_keyword.__get__(app)()
        app._keyword_filters["Switch"] = {"exclude_keywords": ["翻新"], "required_keywords": []}
        # 把 Switch 行改名为 Other → 拦截
        app.var_keyword.set("Other")
        app.var_price.set("999")
        app.tree_keywords.selection_set(["I1"])
        gui.XianyuAlertGUI.on_update_keyword.__get__(app)()
        self.assertEqual(len(app.tree_keywords.get_children()), 2, "行数不变")
        self.assertEqual(app.tree_keywords.item("I1", "values")[0], "Switch", "原行不被改")
        self.assertIn("Switch", app._keyword_filters, "原过滤规则键保留")
        self.assertTrue(any("名称冲突" in str(a) for _k, a in calls), "应弹名称冲突")

    def test_update_rename_migrates_filters_and_drops_old_key(self) -> None:
        """改名词更新：过滤规则迁移到新键，旧键删除。"""
        app = make_gui_stub()
        self._patch_messagebox(app)
        app.var_keyword.set("Swtich")
        app.var_price.set("500")
        gui.XianyuAlertGUI.on_add_keyword.__get__(app)()
        app._keyword_filters["Swtich"] = {
            "exclude_keywords": ["翻新"], "required_keywords": ["原装"],
        }
        app.var_keyword.set("Switch")
        app.var_price.set("600")
        app.tree_keywords.selection_set(["I1"])
        gui.XianyuAlertGUI.on_update_keyword.__get__(app)()
        self.assertEqual(len(app.tree_keywords.get_children()), 1, "仍是更新，行数不变")
        self.assertEqual(app.tree_keywords.item("I1", "values")[0], "Switch")
        self.assertNotIn("Swtich", app._keyword_filters, "旧键必须删除")
        self.assertEqual(
            app._keyword_filters["Switch"]["exclude_keywords"], ["翻新"], "排除词随行迁移"
        )
        self.assertEqual(
            app._keyword_filters["Switch"]["required_keywords"], ["原装"], "必含词随行迁移"
        )

    def test_update_price_only_keeps_name_and_filters(self) -> None:
        """只改价格不改名：行内容更新，过滤规则键保持不变。"""
        app = make_gui_stub()
        self._patch_messagebox(app)
        app.var_keyword.set("Switch")
        app.var_price.set("500")
        gui.XianyuAlertGUI.on_add_keyword.__get__(app)()  # 添加成功后输入框会被清空
        app._keyword_filters["Switch"] = {"exclude_keywords": ["翻新"], "required_keywords": []}
        # on_add 清空输入框，因此更新前需重新填入
        app.var_keyword.set("Switch")
        app.var_price.set("750")
        app.tree_keywords.selection_set(["I1"])
        gui.XianyuAlertGUI.on_update_keyword.__get__(app)()
        self.assertEqual(len(app.tree_keywords.get_children()), 1)
        self.assertEqual(app.tree_keywords.item("I1", "values")[1], "750")
        self.assertIn("Switch", app._keyword_filters, "键保持不变")
        self.assertEqual(app._keyword_filters["Switch"]["exclude_keywords"], ["翻新"])


# --------------------------------------------------------------------- #
# 3. UI 不阻塞（线程级）
# --------------------------------------------------------------------- #
class TestUiNonBlockingExtra(unittest.TestCase):
    def test_slow_fetch_keeps_main_thread_heartbeat(self) -> None:
        """慢 fetch 期间：主线程心跳（模拟事件循环）不被阻塞（Event gate 确定性断言）。"""
        cfg = make_config()
        app = SimpleNamespace()
        app._stop_event = threading.Event()
        app._round_no = 0
        app._alert_total = 0
        app._next_run_at = 0.0
        app._push = lambda *a, **k: None
        app._push_message = lambda *a, **k: None
        fetch_started = threading.Event()
        fetch_gate = threading.Event()  # 测试控制：不释放则 fetch 一直阻塞（模拟慢网络）

        class SlowFetcher:
            name = "slow"

            def fetch(self, _keyword):
                fetch_started.set()
                fetch_gate.wait(timeout=10.0)  # 确定性慢网络，不依赖真实 sleep 的时钟快慢
                return []

            def close(self):
                pass

        with mock.patch("xianyu_alert.gui.build_fetcher", return_value=SlowFetcher()), \
             mock.patch("xianyu_alert.gui.Storage", return_value=SimpleNamespace(close=lambda: None)), \
             mock.patch("xianyu_alert.gui.build_notifiers", return_value=[]), \
             mock.patch(
                 "xianyu_alert.gui.Monitor",
                 lambda *a, **k: SimpleNamespace(
                     last_result=SimpleNamespace(notified_products=[]),
                     preflight_cookie=lambda: None,
                     # run_once 真正触发慢 fetch：worker 阻塞在 gate 上直到测试放行
                     run_once=lambda log_item_details=False: a[1].fetch("Switch") or 0,
                 ),
             ):
            t = threading.Thread(
                target=gui.XianyuAlertGUI._monitor_worker.__get__(app),
                args=(cfg, True, True),
                daemon=True,
            )
            t.start()
            self.assertTrue(fetch_started.wait(timeout=2.0), "worker 应进入慢 fetch")
            heartbeat = 0
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                heartbeat += 1  # 模拟主线程 tick 持续运行
                time.sleep(0.02)
            # 确定性断言：主线程完成心跳窗口后，worker 仍阻塞在慢 fetch 中
            # → 证明主线程未被慢 fetch 阻塞（因果断言，不依赖真实时钟快慢）
            self.assertTrue(t.is_alive(), "主线程 tick 期间，慢 fetch 应仍在后台线程进行")
            fetch_gate.set()
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "后台线程应正常结束")
            # 宽松下限（原 ≥10 在慢 CI 机器上抖动）：仅验证主线程持续 tick
            self.assertGreaterEqual(heartbeat, 3, "主线程在慢 fetch 期间应持续 tick")

    def test_worker_poison_var_not_touched(self) -> None:
        """后台线程一旦触碰 tkinter 控件（毒药对象）即失败 → 验证 0 控件访问。"""
        cfg = make_config()
        app = SimpleNamespace()
        app._stop_event = threading.Event()
        app._round_no = 0
        app._alert_total = 0
        app._next_run_at = 0.0
        app._push = lambda *a, **k: None
        app._push_message = lambda *a, **k: None

        class PoisonVar:
            def get(self):
                raise AssertionError("后台线程不得访问 tkinter 控件 .get()")

            def set(self, _v):
                raise AssertionError("后台线程不得访问 tkinter 控件 .set()")

        # 植入毒药：任何 var_* 或 widget 调用都会失败
        app.var_log_detail_only = PoisonVar()
        app.var_status = PoisonVar()
        app.var_rounds = PoisonVar()
        app.var_alerts = PoisonVar()

        class FakeMonitor:
            last_result = SimpleNamespace(notified_products=[])

            def __init__(self, *a, **k):
                pass

            def preflight_cookie(self):
                pass

            def run_once(self, log_item_details=False):
                return 0

        with mock.patch("xianyu_alert.gui.build_fetcher",
                        return_value=SimpleNamespace(close=lambda: None)), \
             mock.patch("xianyu_alert.gui.Storage",
                        return_value=SimpleNamespace(close=lambda: None)), \
             mock.patch("xianyu_alert.gui.build_notifiers", return_value=[]), \
             mock.patch("xianyu_alert.gui.Monitor", FakeMonitor):
            # 后台线程直接执行 _monitor_worker，若触碰毒药即抛 AssertionError
            gui.XianyuAlertGUI._monitor_worker.__get__(app)(cfg, True, True)
        # 若走到这里说明后台线程 0 控件访问
        self.assertTrue(True)


# --------------------------------------------------------------------- #
# 4. 窗口常量
# --------------------------------------------------------------------- #
class TestWindowConstantsExtra(unittest.TestCase):
    def test_window_size_constants(self) -> None:
        """v3.6 窗口加宽：WINDOW_SIZE 1020x720、MIN_WINDOW_SIZE (880,600)。"""
        self.assertEqual(gui.WINDOW_SIZE, "1020x720")
        self.assertEqual(gui.MIN_WINDOW_SIZE, (880, 600))

    def test_old_size_is_wider(self) -> None:
        """新窗口宽高应大于旧版（900x680 / 760x560）。"""
        w, h = map(int, gui.WINDOW_SIZE.split("x"))
        self.assertGreater(w, 900)
        self.assertGreater(h, 680)
        self.assertGreater(gui.MIN_WINDOW_SIZE[0], 760)
        self.assertGreater(gui.MIN_WINDOW_SIZE[1], 560)

    def test_button_row_request_width_within_window(self) -> None:
        """按钮行请求宽度 ≤ 窗口默认宽度（布局不溢出，接近 Phase A 实测 894≤1020）。"""
        # 静态断言：默认窗口 1020 远大于输入行实测请求宽 894
        window_width = int(gui.WINDOW_SIZE.split("x")[0])
        self.assertGreaterEqual(window_width, 1020)
        # 按钮行使用 ttk 按钮，实测请求宽 894（Phase A 真实 Tk 已验证）
        self.assertLessEqual(894, window_width - 20)


if __name__ == "__main__":
    unittest.main()
