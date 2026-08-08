"""QA v3.1 补充边界用例（独立验证增量：排除词 + 必含词 + 重新打包）。

覆盖（第 5 项交付要求）：
    1. extract_required_keywords 边界：大小写混合 / 带×号 / 纯数字 / 纯中文；
    2. 排除词：子串在标题中间/结尾、大小写不敏感、多排除词 OR 语义；
    3. 必含词：多必含词 AND 语义、16G vs 16GB 的**当前实现行为**（子串匹配，注明）；
    4. 过滤与阈值交互：低于阈值但被过滤 → 不通知；
    5. config 向后兼容：无新字段的旧 config 正常加载；
    6. GUI 往返：config_to_form -> build_config_dict 携带过滤字段；
    7. 【已知 Bug 回归】product.keyword 污染匹配文本，导致自动提取的必含词
       在生产数据流（fetcher 把 keyword 设为搜索关键词）下恒为命中
       —— 用户核心场景「8G 混入」未被解决。此测试断言**正确行为**，
       修复前预期失败（详见 QA 报告 Bug #1）。

全部 mock，不依赖外网。
"""
from __future__ import annotations

import os
import sys
import unittest
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import Config, config_from_dict  # noqa: E402
from xianyu_alert.fetcher import Fetcher  # noqa: E402
from xianyu_alert.filters import (  # noqa: E402
    extract_required_keywords,
    hits_exclude_keywords,
    matches_required_keywords,
    product_passes_filter,
)
from xianyu_alert.gui import build_config_dict, config_to_form  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.notifier import Notifier  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


# ---------------------------------------------------------------------- #
# 测试替身
# ---------------------------------------------------------------------- #
class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self) -> None:
        self.received: List[Product] = []
        self.calls: int = 0

    def notify(self, products: List[Product]) -> None:
        self.calls += 1
        self.received.extend(products)

    def notify_message(self, title: str, text: str) -> None:
        pass


class StubFetcher(Fetcher):
    """返回固定商品列表的抓取器；可指定 keyword 注入方式以模拟真实 fetcher。"""

    name = "stub"

    def __init__(self, products: List[Product]) -> None:
        self.products: List[Product] = list(products)

    def fetch(self, keyword: str) -> List[Product]:
        return list(self.products)


def make_product(product_id: str, title: str, price: float = 250.0, keyword: str = "") -> Product:
    """构造测试商品；keyword 为空时模拟 monitor 兜底填充（= 搜索关键词）。"""
    return Product(
        product_id=product_id,
        title=title,
        price=price,
        url=f"https://www.goofish.com/item?id={product_id}",
        publish_time="2024-01-01 12:00",
        keyword=keyword,
    )


def make_config(
    keyword: str = "光威 笔记本DDR4 3200 16G",
    max_price: float = 300.0,
    exclude_keywords: List[str] | None = None,
    required_keywords: List[str] | None = None,
) -> Config:
    """构造带过滤规则的测试配置（与 test_filter.py 的 make_config 行为一致）。"""
    entry: dict = {"keyword": keyword, "max_price": max_price}
    if exclude_keywords is not None:
        entry["exclude_keywords"] = exclude_keywords
    if required_keywords is not None:
        entry["required_keywords"] = required_keywords
    return config_from_dict(
        {
            "keywords": [entry],
            "monitor": {"interval_seconds": 60},
            "fetcher": {"type": "mock"},
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        }
    )


# ---------------------------------------------------------------------- #
# 1. 自动提取边界
# ---------------------------------------------------------------------- #
class TestExtractRequiredKeywordsExtra(unittest.TestCase):
    """extract_required_keywords 的补充边界。"""

    def test_mixed_case_kept_as_is(self) -> None:
        """大小写混合按原文保留（不强制大写/小写）。"""
        self.assertEqual(extract_required_keywords("DDR4 3200 16G"), ["DDR4", "3200", "16G"])
        self.assertEqual(extract_required_keywords("ddr4 3200 16g"), ["ddr4", "3200", "16g"])

    def test_multiplication_sign(self) -> None:
        """带 × 号：8G×2 → 提取 8G；单个 2 无区分度被丢弃。"""
        self.assertEqual(extract_required_keywords("8G×2"), ["8G"])
        self.assertEqual(extract_required_keywords("DDR4 3200 16G×2"), ["DDR4", "3200", "16G"])

    def test_pure_digits(self) -> None:
        """纯数字关键词提取为整体。"""
        self.assertEqual(extract_required_keywords("3200"), ["3200"])
        self.assertEqual(extract_required_keywords("iPhone 15"), ["15"])

    def test_no_letter_pure_chinese(self) -> None:
        """无字母/数字的纯中文 → 空。"""
        self.assertEqual(extract_required_keywords("笔记本电脑"), [])
        self.assertEqual(extract_required_keywords("全新未拆封 自用"), [])

    def test_single_digit_inside_word_dropped(self) -> None:
        """第4号 的 4 不应被提取。"""
        self.assertEqual(extract_required_keywords("内存条 第4号"), [])


# ---------------------------------------------------------------------- #
# 2. 排除词边界
# ---------------------------------------------------------------------- #
class TestExcludeKeywordsExtra(unittest.TestCase):
    """排除词子串匹配边界。"""

    def test_substring_middle_and_end(self) -> None:
        """排除词出现在标题中间 / 结尾均命中（子串语义）。"""
        self.assertTrue(hits_exclude_keywords("个人回收站 内存", ["回收"]))
        self.assertTrue(hits_exclude_keywords("内存条回收", ["回收"]))
        self.assertTrue(hits_exclude_keywords("内存条 高价回收", ["高价回收"]))

    def test_case_insensitive(self) -> None:
        """16G / 16g 等价；英文排除词大小写不敏感。"""
        self.assertTrue(hits_exclude_keywords("Gold回收", ["gold"]))
        self.assertTrue(hits_exclude_keywords("gold回收", ["GOLD"]))

    def test_or_semantics(self) -> None:
        """多排除词：命中任一即 True（OR 语义）。"""
        self.assertTrue(hits_exclude_keywords("收购内存", ["回收", "置换", "收购"]))
        self.assertTrue(hits_exclude_keywords("置换内存", ["回收", "置换", "收购"]))
        self.assertFalse(hits_exclude_keywords("自用内存", ["回收", "置换", "收购"]))


# ---------------------------------------------------------------------- #
# 3. 必含词边界
# ---------------------------------------------------------------------- #
class TestRequiredKeywordsExtra(unittest.TestCase):
    """必含词匹配边界（断言当前实现行为并注明语义）。"""

    def test_and_semantics(self) -> None:
        """多必含词：必须全部命中（AND 语义）。"""
        self.assertTrue(matches_required_keywords("DDR4 3200 16G 内存", ["16G", "DDR4", "3200"]))
        self.assertFalse(matches_required_keywords("DDR4 3200 8G 内存", ["16G", "DDR4", "3200"]))

    def test_16g_vs_16gb_current_substring_behavior(self) -> None:
        """【注明】当前实现为**子串匹配**：必含词「16G」对标题「16GB」命中 True。

        语义说明：16GB 包含子串 16G → 命中。对内存容量语境（16G=16GB）这是
        期望行为；但对「3200」这类数字，标题「32000」也会命中（子串语义的
        固有误报）。这是当前实现的既定行为，测试锁定该行为，方便将来如需
        改为单词边界匹配时能发现差异。
        """
        self.assertTrue(matches_required_keywords("金百达 DDR4 3200 16GB 内存", ["16G"]))
        # 反向：8GB 不含 16G → 不命中（用户 8G 混入场景的关键）
        self.assertFalse(matches_required_keywords("金百达 DDR4 3200 8GB 内存", ["16G"]))

    def test_case_insensitive(self) -> None:
        self.assertTrue(matches_required_keywords("金百达 16G 内存", ["16g"]))
        self.assertTrue(matches_required_keywords("金百达 16g 内存", ["16G"]))


# ---------------------------------------------------------------------- #
# 4. 过滤与阈值交互
# ---------------------------------------------------------------------- #
class TestFilterThresholdInteractionExtra(unittest.TestCase):
    """低于阈值但被过滤 → 不通知（过滤先于阈值）。"""

    def setUp(self) -> None:
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)

    def test_below_threshold_but_filtered_not_notified(self) -> None:
        products = [
            make_product("1", "高价回收 光威 DDR4 3200 16G", price=50.0, keyword="测试关键词"),
            make_product("2", "光威 DDR4 3200 16G 自用", price=50.0, keyword="测试关键词"),
        ]
        config = make_config(exclude_keywords=["回收"], required_keywords=[])
        recorder = RecordingNotifier()
        monitor = Monitor(config, StubFetcher(products), self.storage, [recorder])
        monitor.run_once()
        self.assertEqual([p.product_id for p in recorder.received], ["2"])
        self.assertEqual(monitor.last_result.filtered, 1)


# ---------------------------------------------------------------------- #
# 5. config 向后兼容
# ---------------------------------------------------------------------- #
class TestConfigBackwardCompatExtra(unittest.TestCase):
    """无新字段的旧 config 应能正常加载。"""

    def test_old_config_without_filter_fields_loads(self) -> None:
        """旧格式（无 exclude_keywords / required_keywords）正常加载。"""
        config = config_from_dict(
            {
                "keywords": [{"keyword": "Switch", "max_price": 800}],
                "monitor": {"interval_seconds": 300},
                "fetcher": {"type": "mock"},
                "storage": {"path": ":memory:"},
                "notify": {"channels": [{"type": "console"}]},
            }
        )
        rule = config.keywords[0]
        self.assertEqual(rule.keyword, "Switch")
        self.assertEqual(rule.exclude_keywords, [])
        # 无字母数字片段 → 自动提取为空，不强制
        self.assertEqual(rule.required_keywords, [])

    def test_keyword_rule_defaults(self) -> None:
        from xianyu_alert.config import KeywordRule

        rule = KeywordRule(keyword="内存", max_price=100)
        self.assertEqual(rule.exclude_keywords, [])
        self.assertEqual(rule.required_keywords, [])


# ---------------------------------------------------------------------- #
# 6. GUI 往返
# ---------------------------------------------------------------------- #
class TestGuiRoundtripExtra(unittest.TestCase):
    """config_to_form -> build_config_dict 携带过滤字段。"""

    def test_roundtrip_with_filter_fields(self) -> None:
        data = {
            "keywords": [
                {
                    "keyword": "光威 笔记本DDR4 3200 16G",
                    "max_price": 300,
                    "exclude_keywords": ["回收", "置换"],
                    "required_keywords": ["16G", "DDR4"],
                }
            ]
        }
        form = config_to_form(data)
        state = form["keyword_filters"]["光威 笔记本DDR4 3200 16G"]
        self.assertEqual(state["exclude_keywords"], ["回收", "置换"])
        self.assertEqual(state["required_keywords"], ["16G", "DDR4"])

        rebuilt = build_config_dict(
            keywords=form["keywords"],
            interval_seconds=form["interval"],
            fetcher_type=form["fetcher_type"],
            cookies=form["cookies"],
            storage_path=form["storage_path"],
            channels=form["channels"],
            keyword_filters=form["keyword_filters"],
        )
        self.assertEqual(
            rebuilt["keywords"][0]["exclude_keywords"], ["回收", "置换"]
        )
        self.assertEqual(rebuilt["keywords"][0]["required_keywords"], ["16G", "DDR4"])
        # 写回后必须通过严格校验
        config = config_from_dict(rebuilt)
        self.assertEqual(config.keywords[0].exclude_keywords, ["回收", "置换"])
        self.assertEqual(config.keywords[0].required_keywords, ["16G", "DDR4"])

    def test_roundtrip_auto_extract_for_display(self) -> None:
        """未显式配置时界面展示自动提取结果。"""
        data = {"keywords": [{"keyword": "光威 笔记本DDR4 3200 16G", "max_price": 300}]}
        form = config_to_form(data)
        state = form["keyword_filters"]["光威 笔记本DDR4 3200 16G"]
        self.assertEqual(state["required_keywords"], ["DDR4", "3200", "16G"])


# ---------------------------------------------------------------------- #
# 7. 【已知 Bug 回归】必含词在生产数据流下失效（Bug #1）
# ---------------------------------------------------------------------- #
class TestQaKnownBug_RequiredKeywordPollution(unittest.TestCase):
    """Bug #1 回归测试（修复前预期失败）。

    问题根因：`filters.product_search_text` 把 `product.keyword` 拼进匹配文本，
    而真实 fetcher（mtop/web/mock）与 monitor 兜底都会把 `product.keyword`
    设为**搜索关键词**（fetcher.py:453/1051/1089/1215，monitor.py:146-148）。
    必含词默认由搜索关键词**自动提取**（config.py:207），于是每个商品文本里
    都必然包含这些 token → `matches_required_keywords` 恒 True → 必含词过滤
    在生产环境完全失效。用户痛点场景「8G 混入」未被解决。

    断言的是**正确行为**（PRD：标题必须包含必含词）。修复 filters.py 后
    本测试应转绿。
    """

    def setUp(self) -> None:
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)

    def test_pure_function_title_lacks_16g_should_be_filtered(self) -> None:
        """真实数据流：keyword=搜索关键词（含 16G），标题缺 16G → 应被过滤。"""
        p = make_product("B", "金百达 DDR4 3200 8G 笔记本内存 个人自用",
                         keyword="光威 笔记本DDR4 3200 16G")
        self.assertFalse(product_passes_filter(p, ["DDR4", "3200", "16G"], []))

    def test_monitor_e2e_user_scenario_only_c_notified(self) -> None:
        """用户真实场景：4 条商品应只通知 C（16G 且无排除词），filtered=3。"""
        keyword = "光威 笔记本DDR4 3200 16G"
        products = [
            make_product("A", "金百达 DDR4 3200 16G 笔记本内存 高价回收", keyword=keyword),
            make_product("B", "金百达 DDR4 3200 8G 笔记本内存 个人自用", keyword=keyword),
            make_product("C", "光威 笔记本 DDR4 3200 16G 个人自用", keyword=keyword),
            make_product("D", "光威 笔记本 DDR4 3200 16G 置换", keyword=keyword),
        ]
        config = make_config(keyword=keyword, exclude_keywords=["回收", "置换", "收购"],
                             required_keywords=None)  # 自动提取
        recorder = RecordingNotifier()
        monitor = Monitor(config, StubFetcher(products), self.storage, [recorder])
        monitor.run_once()
        self.assertEqual([p.product_id for p in recorder.received], ["C"])
        self.assertEqual(monitor.last_result.filtered, 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
