"""GUI v3.3 纯函数测试：新关键词必含留空 / 预置排除词（含「收」）/ 日志明细开关 /
企业微信机器人标签 / 最新发布排序参数。

沿用 test_gui.py 系列的「抽纯函数 / 最小实例」模式，不真正显示窗口。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import config_from_dict  # noqa: E402
from xianyu_alert.fetcher import MockFetcher  # noqa: E402
from xianyu_alert.gui import (  # noqa: E402
    CHANNEL_LABELS,
    PRESET_EXCLUDE_KEYWORDS,
    XianyuAlertGUI,
    add_preset_excludes,
    build_config_dict,
)
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_monitor_config(max_price: float = 1000.0) -> object:
    """构造能通过 config_from_dict 校验的配置字典（mock 抓取器）。"""
    return config_from_dict(
        {
            "keywords": [{"keyword": "Switch", "max_price": max_price}],
            "monitor": {"interval_seconds": 60},
            "fetcher": {"type": "mock"},
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        }
    )


# ---------------------------------------------------------------------- #
# 1. PRESET_EXCLUDE_KEYWORDS：v3.3 新增「收」
# ---------------------------------------------------------------------- #
class TestPresetExcludesV33(unittest.TestCase):
    """预置排除词常量含「收」，add_preset_excludes 合并结果正确。"""

    def test_preset_contains_shou(self) -> None:
        self.assertIn("收", PRESET_EXCLUDE_KEYWORDS)
        self.assertEqual(
            list(PRESET_EXCLUDE_KEYWORDS),
            ["回收", "置换", "收购", "高价回收", "收"],
        )

    def test_add_preset_excludes_includes_shou(self) -> None:
        self.assertEqual(
            add_preset_excludes([]),
            ["回收", "置换", "收购", "高价回收", "收"],
        )
        self.assertEqual(
            add_preset_excludes(["回收"]),
            ["回收", "置换", "收购", "高价回收", "收"],
        )


# ---------------------------------------------------------------------- #
# 2. 新关键词默认过滤规则：必含留空 + 预置排除词
# ---------------------------------------------------------------------- #
class TestDefaultFiltersV33(unittest.TestCase):
    """GUI 添加关键词时的默认规则（v3.3 需求 1 / 需求 2）。"""

    def _gui_stub(self) -> XianyuAlertGUI:
        """不调用 __init__ 的最小实例（_default_filters 不依赖实例状态）。"""
        return object.__new__(XianyuAlertGUI)

    def test_required_keywords_empty_on_add(self) -> None:
        """需求 1：添加关键词不再自动补齐必含词（即使关键词含数字+单位）。"""
        gui = self._gui_stub()
        filters = gui._default_filters("光威 笔记本DDR4 3200 16G")
        self.assertEqual(filters["required_keywords"], [])

    def test_exclude_keywords_preset_on_add(self) -> None:
        """需求 2：添加关键词自动预置排除词（含新增的「收」）。"""
        gui = self._gui_stub()
        filters = gui._default_filters("Switch")
        self.assertEqual(filters["exclude_keywords"], ["回收", "置换", "收购", "高价回收", "收"])

    def test_build_config_roundtrip_keeps_empty_required(self) -> None:
        """添加流程 → build_config_dict → config_from_dict：必含仍为空、排除为预置词。"""
        gui = self._gui_stub()
        filters = gui._default_filters("DDR4 16G")
        data = build_config_dict(
            keywords=[("DDR4 16G", 300.0)],
            interval_seconds=600,
            fetcher_type="mock",
            cookies="",
            storage_path=":memory:",
            channels={"console": {"enabled": True, "options": {}}},
            keyword_filters={"DDR4 16G": filters},
        )
        self.assertEqual(data["keywords"][0]["required_keywords"], [])
        self.assertEqual(
            data["keywords"][0]["exclude_keywords"],
            ["回收", "置换", "收购", "高价回收", "收"],
        )
        cfg = config_from_dict(data)
        self.assertEqual(cfg.keywords[0].required_keywords, [])
        self.assertEqual(
            cfg.keywords[0].exclude_keywords,
            ["回收", "置换", "收购", "高价回收", "收"],
        )


# ---------------------------------------------------------------------- #
# 3. 日志「仅展示符合的低价」开关（需求 5）：monitor.log_item_details
# ---------------------------------------------------------------------- #
class TestLogItemDetailsV33(unittest.TestCase):
    """monitor.run_once 开/关 detail_logging 的行为差异。"""

    def setUp(self) -> None:
        self.config = make_monitor_config()
        self.storage = Storage(":memory:")
        self.fetcher = MockFetcher(products_per_round=5)
        self.monitor = Monitor(self.config, self.fetcher, self.storage, [])

    def tearDown(self) -> None:
        self.storage.close()

    def test_detail_logging_off_no_item_lines(self) -> None:
        """默认（关闭）：不输出任何 [明细] 行。"""
        with self.assertLogs("xianyu_alert.monitor", level="INFO") as cm:
            self.monitor.run_once(log_item_details=False)
        detail_lines = [line for line in cm.output if "[明细]" in line]
        self.assertEqual(detail_lines, [])

    def test_detail_logging_on_logs_every_product(self) -> None:
        """开启：每个抓取到的商品都有一条 [明细] 行（含命中/过滤原因）。"""
        with self.assertLogs("xianyu_alert.monitor", level="INFO") as cm:
            self.monitor.run_once(log_item_details=True)
        detail_lines = [line for line in cm.output if "[明细]" in line]
        self.assertEqual(len(detail_lines), 5)
        # 每条明细都带价格文案与原因标记
        self.assertTrue(all("¥" in line for line in detail_lines))
        self.assertTrue(
            all(any(mark in line for mark in ("命中低价", "超阈值", "必含词缺失", "排除词命中", "已提醒过"))
                for line in detail_lines)
        )

    def test_detail_logging_off_still_runs_normally(self) -> None:
        """默认参数调用（向后兼容）：不传 log_item_details 也能正常跑。"""
        count = self.monitor.run_once()
        self.assertGreater(count, 0)
        self.assertEqual(self.monitor.last_result.fetched, 5)


# ---------------------------------------------------------------------- #
# 4. 企业微信机器人通道标签（需求 4）
# ---------------------------------------------------------------------- #
class TestWebhookLabelV33(unittest.TestCase):
    """webhook 通道明确为「企业微信机器人」。"""

    def test_channel_label_is_wecom(self) -> None:
        self.assertIn("企业微信", CHANNEL_LABELS["webhook"])

    def test_channel_order_still_contains_webhook(self) -> None:
        from xianyu_alert.gui import CHANNEL_ORDER

        self.assertIn("webhook", CHANNEL_ORDER)


# ---------------------------------------------------------------------- #
# 5. 最新发布排序参数（需求 6）
# ---------------------------------------------------------------------- #
class TestNewestSortPayloadV33(unittest.TestCase):
    """build_search_payload 必须携带 sortField=create + sortValue=desc。"""

    def test_payload_newest_sort(self) -> None:
        from xianyu_alert.fetcher import build_search_payload

        payload = build_search_payload("Switch", page_number=1, rows_per_page=30)
        self.assertEqual(payload["sortField"], "create")
        self.assertEqual(payload["sortValue"], "desc")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
