"""v3.4 回归测试：服务端价格筛选（最新发布+价格<阈值）与 Cookie 管理「添加」按钮。

覆盖：
    1. build_search_payload 新增 max_price 参数（服务端 priceRange 筛选 + fromFilter）；
    2. format_price_bound 边界格式化；
    3. MtopFetcher.set_max_price 注入阈值 → _search 构造的 payload 含 priceRange；
    4. Monitor 每关键词调用 fetcher.set_max_price（阈值注入）；
    5. Cookie 管理 on_manage_cookies 不再因「按钮 command 引用后定义函数」抛
       UnboundLocalError（v3.4 修复），「添加」按钮可弹出子对话框并可保存。

沿用既有「抽纯函数 / mock / 最小实例」模式，不依赖真实网络、不真正显示窗口。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import config_from_dict  # noqa: E402
from xianyu_alert.fetcher import (  # noqa: E402
    Fetcher,
    MtopFetcher,
    MockFetcher,
    build_search_payload,
    format_price_bound,
)
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_config(max_price: float = 360.0, ftype: str = "mock") -> object:
    """构造能通过 config_from_dict 校验的配置（关键词阈值可控）。"""
    return config_from_dict(
        {
            "keywords": [{"keyword": "DDR4 3200 16G", "max_price": max_price}],
            "monitor": {"interval_seconds": 60},
            "fetcher": {"type": ftype},
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        }
    )


# ---------------------------------------------------------------------- #
# 1. build_search_payload 服务端价格筛选
# ---------------------------------------------------------------------- #
class TestSearchPayloadMaxPrice(unittest.TestCase):
    """v3.4：max_price 参数应生成 propValueStr.searchFilter + fromFilter=true。"""

    def test_default_payload_no_price_filter(self) -> None:
        """不传 max_price 时保持旧行为：fromFilter=false、propValueStr={}。"""
        payload = build_search_payload("DDR4 3200 16G", page_number=1, rows_per_page=30)
        self.assertFalse(payload["fromFilter"])
        self.assertEqual(payload["propValueStr"], {})
        self.assertEqual(payload["sortField"], "create")
        self.assertEqual(payload["sortValue"], "desc")

    def test_max_price_float_integer(self) -> None:
        """max_price=360.0 → searchFilter=priceRange:0,360; 且 fromFilter=true。"""
        payload = build_search_payload("DDR4 3200 16G", page_number=1, rows_per_page=30, max_price=360.0)
        self.assertTrue(payload["fromFilter"])
        self.assertEqual(payload["propValueStr"], {"searchFilter": "priceRange:0,360;"})
        # 排序参数不受影响
        self.assertEqual(payload["sortField"], "create")
        self.assertEqual(payload["sortValue"], "desc")

    def test_max_price_int(self) -> None:
        payload = build_search_payload("DDR4 3200 16G", 1, 30, max_price=300)
        self.assertEqual(payload["propValueStr"], {"searchFilter": "priceRange:0,300;"})

    def test_max_price_fractional(self) -> None:
        """非整数阈值保留小数（服务端按元粒度接受）。"""
        payload = build_search_payload("DDR4 3200 16G", 1, 30, max_price=99.5)
        self.assertEqual(payload["propValueStr"], {"searchFilter": "priceRange:0,99.5;"})

    def test_max_price_none_keeps_empty(self) -> None:
        payload = build_search_payload("DDR4 3200 16G", 1, 30, max_price=None)
        self.assertFalse(payload["fromFilter"])
        self.assertEqual(payload["propValueStr"], {})

    def test_other_fields_preserved(self) -> None:
        payload = build_search_payload("Switch", 2, 20, max_price=100.0)
        self.assertEqual(payload["pageNumber"], 2)
        self.assertEqual(payload["rowsPerPage"], 20)
        self.assertEqual(payload["keyword"], "Switch")
        self.assertEqual(payload["searchReqFromPage"], "pcSearch")


# ---------------------------------------------------------------------- #
# 2. format_price_bound
# ---------------------------------------------------------------------- #
class TestFormatPriceBound(unittest.TestCase):
    """价格边界格式化：整数无小数点，非有限值兜底。"""

    def test_integer_float(self) -> None:
        self.assertEqual(format_price_bound(360.0), "360")

    def test_int(self) -> None:
        self.assertEqual(format_price_bound(360), "360")

    def test_fraction(self) -> None:
        self.assertEqual(format_price_bound(99.5), "99.5")

    def test_nan(self) -> None:
        self.assertEqual(format_price_bound(float("nan")), "99999999")

    def test_inf(self) -> None:
        self.assertEqual(format_price_bound(float("inf")), "99999999")

    def test_negative(self) -> None:
        self.assertEqual(format_price_bound(-5.0), "-5")


# ---------------------------------------------------------------------- #
# 3. MtopFetcher.set_max_price → 请求体注入
# ---------------------------------------------------------------------- #
class TestMtopSetMaxPrice(unittest.TestCase):
    """set_max_price 后 _search 构造的 payload 应携带 priceRange。"""

    def setUp(self) -> None:
        self.fetcher = MtopFetcher(cookies="_m_h5_tk=abc_9999999999999; cookie2=1", page_size=30)

    def tearDown(self) -> None:
        self.fetcher.close()

    def test_default_max_price_none(self) -> None:
        self.assertIsNone(self.fetcher._max_price)

    def test_set_max_price(self) -> None:
        self.fetcher.set_max_price(360.0)
        self.assertEqual(self.fetcher._max_price, 360.0)

    def test_set_max_price_none(self) -> None:
        self.fetcher.set_max_price(100.0)
        self.fetcher.set_max_price(None)
        self.assertIsNone(self.fetcher._max_price)

    def test_set_max_price_invalid(self) -> None:
        self.fetcher.set_max_price("abc")
        self.assertIsNone(self.fetcher._max_price)

    def test_search_payload_includes_price_range(self) -> None:
        """mock _post_once 后验证 _search 传给 build_search_payload 的 max_price。"""
        self.fetcher.set_max_price(360.0)
        with mock.patch.object(
            self.fetcher, "_post_once", return_value={"ret": ["SUCCESS::调用成功"], "data": {}}
        ) as post:
            result = self.fetcher._search("DDR4 3200 16G", page_number=1)
            self.assertEqual(result["ret"][0], "SUCCESS::调用成功")
            # 验证 post 收到的 data 里含 priceRange
            data_arg = post.call_args[0][0]
            self.assertEqual(data_arg["propValueStr"], {"searchFilter": "priceRange:0,360;"})
            self.assertTrue(data_arg["fromFilter"])


# ---------------------------------------------------------------------- #
# 4. Monitor 每关键词注入价格上限
# ---------------------------------------------------------------------- #
class TestMonitorInjectMaxPrice(unittest.TestCase):
    """monitor._process_keyword 应调用 fetcher.set_max_price(rule.max_price)。"""

    def test_set_max_price_called_with_threshold(self) -> None:
        config = make_config(max_price=360.0, ftype="mock")
        storage = Storage(":memory:")
        fetcher = MockFetcher(products_per_round=5)
        monitor = Monitor(config, fetcher, storage, [])
        try:
            with mock.patch.object(fetcher, "set_max_price", wraps=fetcher.set_max_price) as setter:
                monitor.run_once()
                setter.assert_called_once_with(360.0)
        finally:
            storage.close()

    def test_set_max_price_missing_fetcher_ok(self) -> None:
        """无 set_max_price 的旧 fetcher（如测试桩）不应导致崩溃。"""
        config = make_config(max_price=360.0, ftype="mock")
        storage = Storage(":memory:")

        class LegacyFetcher(Fetcher):
            name = "legacy"

            def fetch(self, keyword: str):  # type: ignore[override]
                return []

        monitor = Monitor(config, LegacyFetcher(), storage, [])
        try:
            # 不应抛异常；fetch 返回空列表（成功），阈值注入缺省无碍
            monitor.run_once()
            self.assertEqual(monitor.last_result.failed_keywords, [])
        finally:
            storage.close()

    def test_fetch_still_keyword_only_signature(self) -> None:
        """契约不变：monitor 仍以 fetch(keyword) 单参调用。"""
        config = make_config(max_price=360.0, ftype="mock")
        storage = Storage(":memory:")
        fetcher = MockFetcher(products_per_round=5)
        monitor = Monitor(config, fetcher, storage, [])
        try:
            with mock.patch.object(fetcher, "fetch", wraps=fetcher.fetch) as fetch_mock:
                monitor.run_once()
                fetch_mock.assert_called_once_with("DDR4 3200 16G")
        finally:
            storage.close()


# ---------------------------------------------------------------------- #
# 5. Cookie 管理「添加」按钮修复（不真正显示窗口）
# ---------------------------------------------------------------------- #
class TestCookieManagerAddButton(unittest.TestCase):
    """v3.4：on_manage_cookies 不应因 command 引用后定义函数抛 UnboundLocalError；
    「添加」按钮应能弹出子对话框并保存到 _cookie_pool。"""

    def test_on_manage_cookies_builds_without_error(self) -> None:
        """打开 Cookie 管理对话框不应抛异常（回归：曾因 _on_cookie_help 提前引用崩溃）。"""
        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover - 无 tkinter 环境跳过
            self.skipTest("tkinter 不可用")
        root = tk.Tk()
        root.withdraw()
        try:
            from xianyu_alert.gui import XianyuAlertGUI

            gui = XianyuAlertGUI(root, config_path="config.yaml")
            gui.on_manage_cookies()
            tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
            self.assertEqual(len(tops), 1, "应创建 Cookie 管理对话框")
            self.assertIn("Cookie 管理", tops[0].title())
            tops[0].destroy()
        finally:
            root.destroy()

    def test_add_button_opens_subdialog_and_saves(self) -> None:
        """点击「添加」→ 弹出「添加 Cookie」子对话框 → 填写保存 → 入池 + 关闭。"""
        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover
            self.skipTest("tkinter 不可用")
        root = tk.Tk()
        root.withdraw()
        try:
            from xianyu_alert.gui import XianyuAlertGUI

            gui = XianyuAlertGUI(root, config_path="config.yaml")
            gui.on_manage_cookies()
            tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
            dlg = tops[0]

            add_btn = self._find_button(dlg, "添加")
            self.assertIsNotNone(add_btn, "应存在「添加」按钮")
            add_btn.invoke()

            # 子对话框应出现
            sub = self._find_toplevel(dlg, "添加 Cookie")
            self.assertIsNotNone(sub, "点击添加后应弹出「添加 Cookie」子对话框")

            # 填写名称 + Cookie，点保存
            entry = self._find_widget(sub, tk.Entry)
            text = self._find_widget(sub, tk.Text)
            self.assertIsNotNone(entry)
            self.assertIsNotNone(text)
            entry.insert(0, "测试账号")
            text.insert("1.0", "cookie2=abc; _m_h5_tk=test_1785488087003")
            save_btn = self._find_button(sub, "保存")
            self.assertIsNotNone(save_btn)
            save_btn.invoke()

            self.assertEqual(len(gui._cookie_pool), 1)
            item = gui._cookie_pool[0]
            self.assertEqual(item["name"], "测试账号")
            self.assertTrue(item["enabled"])
            self.assertIn("_m_h5_tk=", item["cookie"])
            # 子对话框已关闭
            self.assertIsNone(self._find_toplevel(dlg, "添加 Cookie"))

            dlg.destroy()
        finally:
            root.destroy()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_button(w, text_part: str):
        for child in w.winfo_children():
            try:
                if child.winfo_class() == "TButton" and text_part in str(child.cget("text")):
                    return child
            except Exception:  # noqa: BLE001 - cget 可能失败
                pass
            found = TestCookieManagerAddButton._find_button(child, text_part)
            if found:
                return found
        return None

    @staticmethod
    def _find_widget(w, cls):
        for child in w.winfo_children():
            if isinstance(child, cls):
                return child
            found = TestCookieManagerAddButton._find_widget(child, cls)
            if found:
                return found
        return None

    @staticmethod
    def _find_toplevel(w, title_part: str):
        for child in w.winfo_children():
            if isinstance(child, __import__("tkinter").Toplevel) and title_part in child.title():
                return child
            found = TestCookieManagerAddButton._find_toplevel(child, title_part)
            if found:
                return found
        return None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
