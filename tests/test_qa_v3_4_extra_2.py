"""v3.4 QA 独立验证补充边界用例（第 2 批）。

本文件由 QA（严过关）独立编写，重点覆盖工程师交付中未被
`test_qa_v3_4_extra.py` 精确断言/或需要独立复核的边界：

    1. format_price_bound：整数 / 浮点 / NaN / ±inf / None / 脏数据；
    2. build_search_payload：max_price=0 / None 的实际行为精确断言
       （注明：config 层已拦截 max_price<=0，此处按纯函数实际行为断言，
       防御未来绕过校验）；
    3. set_max_price 基类 no-op（调用不崩、返回 None）；
    4. getattr 防御：旧 fetcher 无 set_max_price 时 monitor 不崩；
    5. propValueStr 嵌套结构精确断言（dict 内层 searchFilter 字符串）；
    6. GUI：on_manage_cookies 的「❓ 如何获取 Cookie？」按钮 lambda
       延迟求值静态断言 + 真实 Tk 冒烟（不抛 UnboundLocalError）。

不依赖真实网络；GUI 用例依赖 tkinter（不可用时跳过，与既有测试一致）。
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
    MockFetcher,
    build_search_payload,
    format_price_bound,
)
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_config(max_price: float = 360.0) -> object:
    """构造能通过 config_from_dict 校验的配置。"""
    return config_from_dict(
        {
            "keywords": [{"keyword": "DDR4 3200 16G", "max_price": max_price}],
            "monitor": {"interval_seconds": 60},
            "fetcher": {"type": "mock"},
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        }
    )


# ---------------------------------------------------------------------- #
# 1. format_price_bound 边界
# ---------------------------------------------------------------------- #
class TestFormatPriceBoundExtra(unittest.TestCase):
    """价格边界格式化：整数无小数点、浮点保留、非有限值/脏数据兜底。"""

    def test_integer_int(self) -> None:
        self.assertEqual(format_price_bound(360), "360")

    def test_integer_float(self) -> None:
        self.assertEqual(format_price_bound(360.0), "360")

    def test_large_integer(self) -> None:
        self.assertEqual(format_price_bound(99999), "99999")

    def test_fraction_float(self) -> None:
        self.assertEqual(format_price_bound(99.5), "99.5")

    def test_nan(self) -> None:
        self.assertEqual(format_price_bound(float("nan")), "99999999")

    def test_positive_inf(self) -> None:
        self.assertEqual(format_price_bound(float("inf")), "99999999")

    def test_negative_inf(self) -> None:
        self.assertEqual(format_price_bound(float("-inf")), "99999999")

    def test_none(self) -> None:
        self.assertEqual(format_price_bound(None), "99999999")

    def test_string_number(self) -> None:
        """字符串数字可被 float() 转换 → 正常格式化。"""
        self.assertEqual(format_price_bound("360"), "360")

    def test_garbage_string(self) -> None:
        self.assertEqual(format_price_bound("abc"), "99999999")

    def test_negative_value(self) -> None:
        """负值按原样输出（config 层会拦截，纯函数不额外处理）。"""
        self.assertEqual(format_price_bound(-5.0), "-5")


# ---------------------------------------------------------------------- #
# 2. build_search_payload max_price=0 / None 行为
# ---------------------------------------------------------------------- #
class TestSearchPayloadEdgeValues(unittest.TestCase):
    """max_price 边界：0 / None 的实际行为精确断言。

    说明：config 层 `keywords[].max_price` 校验为「必须为正数」（<=0 抛
    ConfigError），业务上不可能出现 0；此处按纯函数实际行为断言，用于
    防御未来校验被绕过时请求体不会静默变成错误结构。
    """

    def test_max_price_zero_produces_price_range_0_0(self) -> None:
        """技术行为：max_price=0（非 None）→ 仍进入筛选分支 → priceRange:0,0;。

        这是纯函数的实际行为（`0 is not None` 为 True）。业务上 config 层
        已拦截 <=0，故不会出现在正常配置路径；断言锁定当前实现，防止
        未来改动静默改变结构。
        """
        payload = build_search_payload("DDR4 3200 16G", 1, 30, max_price=0)
        self.assertTrue(payload["fromFilter"])
        self.assertEqual(payload["propValueStr"], {"searchFilter": "priceRange:0,0;"})

    def test_max_price_none_keeps_legacy_payload(self) -> None:
        """None → 完全保持旧行为：propValueStr={}、fromFilter=false。"""
        payload = build_search_payload("DDR4 3200 16G", 1, 30, max_price=None)
        self.assertFalse(payload["fromFilter"])
        self.assertEqual(payload["propValueStr"], {})
        # 其余字段与旧行为完全一致
        self.assertEqual(payload["pageNumber"], 1)
        self.assertEqual(payload["rowsPerPage"], 30)
        self.assertEqual(payload["keyword"], "DDR4 3200 16G")
        self.assertEqual(payload["sortField"], "create")
        self.assertEqual(payload["sortValue"], "desc")
        self.assertEqual(payload["searchReqFromPage"], "pcSearch")
        self.assertEqual(payload["extraFilterValue"], "{}")

    def test_max_price_omitted_parameter_keeps_legacy_payload(self) -> None:
        """完全不传 max_price 参数 → 与不传 None 等价，propValueStr={}。"""
        payload = build_search_payload("DDR4 3200 16G", 1, 30)
        self.assertFalse(payload["fromFilter"])
        self.assertEqual(payload["propValueStr"], {})

    def test_prop_value_str_nested_structure_exact(self) -> None:
        """嵌套 dict 精确断言：外层 propValueStr 只有 searchFilter 一个键，
        值是精确的 `priceRange:0,{bound};` 字符串。"""
        payload = build_search_payload("DDR4 3200 16G", 1, 30, max_price=360)
        pvs = payload["propValueStr"]
        self.assertIsInstance(pvs, dict)
        self.assertEqual(set(pvs.keys()), {"searchFilter"})
        self.assertEqual(pvs["searchFilter"], "priceRange:0,360;")
        self.assertIsInstance(pvs["searchFilter"], str)
        self.assertTrue(pvs["searchFilter"].endswith(";"))

    def test_payload_keys_unchanged_after_max_price(self) -> None:
        """传 max_price 不引入/丢失任何顶层键（只改 fromFilter/propValueStr）。"""
        base = set(build_search_payload("K", 1, 30).keys())
        with_price = set(build_search_payload("K", 1, 30, max_price=100).keys())
        self.assertEqual(base, with_price)


# ---------------------------------------------------------------------- #
# 3. set_max_price 基类 no-op
# ---------------------------------------------------------------------- #
class TestSetMaxPriceBaseNoop(unittest.TestCase):
    """基类 set_max_price 是 no-op：调用不崩、返回 None、无副作用。"""

    def test_mock_fetcher_inherits_noop(self) -> None:
        """MockFetcher 未覆盖 set_max_price → 走基类实现，调用不崩。"""
        fetcher = MockFetcher(products_per_round=2)
        self.assertIsNone(fetcher.set_max_price(360.0))
        self.assertIsNone(fetcher.set_max_price(None))

    def test_fetcher_noop_returns_none(self) -> None:
        """直接调用基类实现（经最小子类）返回 None。"""
        class Minimal(Fetcher):
            name = "minimal"

            def fetch(self, keyword: str):  # type: ignore[override]
                return []

        fetcher = Minimal()
        self.assertIsNone(fetcher.set_max_price(123.0))


# ---------------------------------------------------------------------- #
# 4. getattr 防御：旧 fetcher 无 set_max_price 时 monitor 不崩
# ---------------------------------------------------------------------- #
class TestMonitorLegacyFetcherGuard(unittest.TestCase):
    """monitor 对 fetcher.set_max_price 的 getattr 防御。"""

    def test_legacy_fetcher_without_set_max_price_ok(self) -> None:
        class LegacyFetcher(Fetcher):
            name = "legacy"

            def fetch(self, keyword: str):  # type: ignore[override]
                return []

        config = make_config(max_price=360.0)
        storage = Storage(":memory:")
        monitor = Monitor(config, LegacyFetcher(), storage, [])
        try:
            monitor.run_once()  # 不应抛异常
            self.assertEqual(monitor.last_result.failed_keywords, [])
        finally:
            storage.close()

    def test_setter_exception_does_not_abort_keyword(self) -> None:
        """set_max_price 抛异常时应被捕获（warning），不阻断抓取。"""
        class BrokenSetterFetcher(MockFetcher):
            def set_max_price(self, max_price):  # type: ignore[override]
                raise RuntimeError("boom")

        config = make_config(max_price=360.0)
        storage = Storage(":memory:")
        monitor = Monitor(config, BrokenSetterFetcher(products_per_round=2), storage, [])
        try:
            with mock.patch("xianyu_alert.monitor.logger") as logger_mock:
                monitor.run_once()
            logger_mock.warning.assert_called_once()
            self.assertEqual(monitor.last_result.failed_keywords, [])
        finally:
            storage.close()


# ---------------------------------------------------------------------- #
# 5. GUI：Cookie 管理「❓ 如何获取 Cookie？」按钮 lambda 延迟求值
# ---------------------------------------------------------------------- #
class TestGuiCookieHelpLambda(unittest.TestCase):
    """v3.4 GUI 修复：帮助按钮必须用 lambda 延迟求值，且嵌套函数定义完整。

    静态断言（不依赖 Tk）+ 真实 Tk 冒烟（不可用时跳过）。
    """

    def test_help_button_uses_lambda_deferred(self) -> None:
        """源码级断言：按钮绑定处使用 `command=lambda: _on_cookie_help()`，
        而非直接 `command=_on_cookie_help`（后者会在定义前求值抛
        UnboundLocalError）。"""
        import inspect

        from xianyu_alert import gui as gui_module

        src = inspect.getsource(gui_module.XianyuAlertGUI.on_manage_cookies)
        # 按钮绑定处：必须在嵌套函数 _on_cookie_help 定义之前出现 lambda 延迟求值
        bind_pos = src.find('text="❓ 如何获取 Cookie？"')
        self.assertGreater(bind_pos, -1, "帮助按钮绑定应存在")
        binding_line = src[src.rfind("\n", 0, bind_pos) + 1 : src.find("\n", bind_pos)]
        self.assertIn("command=lambda: _on_cookie_help()", binding_line)
        # 延迟求值：直接引用 `command=_on_cookie_help`（不带括号）也是错误用法
        self.assertNotRegex(binding_line, r"command=_on_cookie_help\b")
        # 嵌套函数确实在绑定之后定义
        def_pos = src.find("def _on_cookie_help(")
        self.assertGreater(def_pos, bind_pos, "嵌套函数 _on_cookie_help 应在绑定之后定义")
        # 其它按钮回调也应使用 lambda 延迟求值
        for btn_text in ("添加", "编辑选中", "删除选中", "启用/停用", "检测全部", "设为默认"):
            pos = src.find(f'text="{btn_text}"')
            if pos > -1:
                line = src[src.rfind("\n", 0, pos) + 1 : src.find("\n", pos)]
                self.assertIn("command=lambda:", line, f"{btn_text} 按钮应使用 lambda")

    def test_on_manage_cookies_real_tk_no_unbound_error(self) -> None:
        """真实 Tk 冒烟：on_manage_cookies 不再抛 UnboundLocalError，
        对话框正常创建（回归 v3.4 修复点）。"""
        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover
            self.skipTest("tkinter 不可用")
        root = tk.Tk()
        root.withdraw()
        try:
            from xianyu_alert.gui import XianyuAlertGUI

            gui = XianyuAlertGUI(root, config_path="config.yaml")
            gui.on_manage_cookies()  # 曾在此抛 UnboundLocalError
            tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
            self.assertEqual(len(tops), 1)
            tops[0].destroy()
        finally:
            root.destroy()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
