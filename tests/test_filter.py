"""v3.1 关键词过滤测试：排除词（exclude_keywords）+ 必含词（required_keywords）。

覆盖范围：
    1. extract_required_keywords：自动提取默认必含词（典型输入）；
    2. 纯函数匹配：product_search_text / matches_required_keywords /
       hits_exclude_keywords / product_passes_filter（含大小写不敏感）；
    3. Monitor 集成：排除词命中跳过、必含词缺失跳过、空列表无过滤、
       二者叠加、与价格阈值检查的先后交互；
    4. Config 解析：未显式配置自动提取、显式空列表关闭、非法类型报错；
    5. GUI 纯函数：parse_keyword_lines / add_preset_excludes /
       apply_filter_edit / keyword_filter_summary / config_to_form /
       build_config_dict 过滤字段往返。

全部 mock，不依赖外网。
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import Config, ConfigError, config_from_dict  # noqa: E402
from xianyu_alert.fetcher import Fetcher, MockFetcher  # noqa: E402
from xianyu_alert.filters import (  # noqa: E402
    extract_required_keywords,
    hits_exclude_keywords,
    matches_required_keywords,
    normalize_keywords,
    product_passes_filter,
    product_search_text,
)
from xianyu_alert.gui import (  # noqa: E402
    add_preset_excludes,
    apply_filter_edit,
    build_config_dict,
    config_to_form,
    keyword_filter_summary,
    parse_keyword_lines,
)
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.notifier import Notifier  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


# ---------------------------------------------------------------------- #
# 测试替身
# ---------------------------------------------------------------------- #
class RecordingNotifier(Notifier):
    """记录收到的商品，便于断言。"""

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
    """返回固定商品列表的抓取器，用于精确构造过滤数据。"""

    name = "stub"

    def __init__(self, products: List[Product]) -> None:
        self.products: List[Product] = products

    def fetch(self, keyword: str) -> List[Product]:
        return list(self.products)


def make_product(product_id: str, title: str, price: float = 100.0) -> Product:
    """构造测试商品（keyword 默认与标题无关，避免干扰必含词断言）。"""
    return Product(
        product_id=product_id,
        title=title,
        price=price,
        url=f"https://www.goofish.com/item?id={product_id}",
        publish_time="2024-01-01 12:00",
        keyword="测试关键词",
    )


def make_config(
    keyword: str = "光威 笔记本DDR4 3200 16G",
    max_price: float = 300.0,
    exclude_keywords: List[str] | None = None,
    required_keywords: List[str] | None = None,
) -> Config:
    """构造带过滤规则的测试配置。

    exclude_keywords / required_keywords 为 None 时**不写该字段**：
        - required_keywords=None → 触发 config 的自动提取默认值；
        - required_keywords=[]   → 显式关闭强制必含；
    exclude_keywords=None / []   → 等价（不排除）。
    """
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
# 1. 自动提取默认必含词
# ---------------------------------------------------------------------- #
class TestExtractRequiredKeywords(unittest.TestCase):
    """从主关键词提取「数字+字母」片段作为必含词默认值。"""

    def test_typical_ddr4_keyword(self) -> None:
        """「光威 笔记本DDR4 3200 16G」→ DDR4 / 3200 / 16G（用户的真实场景）。"""
        self.assertEqual(
            extract_required_keywords("光威 笔记本DDR4 3200 16G"),
            ["DDR4", "3200", "16G"],
        )

    def test_digit_letter_and_letter_digit(self) -> None:
        """数字+字母（16G）与字母+数字（DDR4）都要提取。"""
        self.assertEqual(extract_required_keywords("DDR4 8GB 3200"), ["DDR4", "8GB", "3200"])
        self.assertEqual(extract_required_keywords("16G"), ["16G"])
        self.assertEqual(extract_required_keywords("8GB"), ["8GB"])

    def test_pure_chinese_not_extracted(self) -> None:
        """纯中文词不提取，避免过滤过严。"""
        self.assertEqual(extract_required_keywords("笔记本"), [])
        self.assertEqual(extract_required_keywords("Switch"), [])
        self.assertEqual(extract_required_keywords(""), [])

    def test_digits_inside_chinese_run(self) -> None:
        """中文字符后的 DDR4 也应完整提取（不会退化成垃圾片段 4）。"""
        result = extract_required_keywords("光威 笔记本DDR4 3200 16G")
        self.assertNotIn("4", result)
        self.assertIn("DDR4", result)

    def test_phone_and_gpu(self) -> None:
        """手机 / 显卡类关键词也能提取。"""
        self.assertEqual(extract_required_keywords("iPhone 15"), ["15"])
        self.assertEqual(extract_required_keywords("RTX3060 12G"), ["RTX3060", "12G"])

    def test_single_digit_dropped(self) -> None:
        """纯单个数字（第4号的 4）无区分度，应被丢弃。"""
        self.assertEqual(extract_required_keywords("内存条 4"), [])
        self.assertEqual(extract_required_keywords("usb3.0"), ["usb3"])

    def test_dedup_preserves_order(self) -> None:
        """重复片段去重且保序。"""
        self.assertEqual(extract_required_keywords("16G 16G 8GB"), ["16G", "8GB"])

    def test_normalize_keywords(self) -> None:
        """规范化：去空白、去空串、去重、保序。"""
        self.assertEqual(normalize_keywords([" 回收 ", "", "回收", "置换"]), ["回收", "置换"])
        self.assertEqual(normalize_keywords(None), [])
        self.assertEqual(normalize_keywords([1, " 2 "]), ["1", "2"])


# ---------------------------------------------------------------------- #
# 2. 纯函数匹配
# ---------------------------------------------------------------------- #
class TestFilterPureFunctions(unittest.TestCase):
    """匹配函数的边界与大小写不敏感。"""

    def test_product_search_text_lowercases_and_joins(self) -> None:
        p = make_product("1", "金百达 DDR4 3200 16G", price=280)
        text = product_search_text(p)
        # 只含标题（+可选 seller/location），不含 product.keyword：
        # 真实数据流中 keyword 即搜索关键词，拼入会导致必含词过滤失效（P0）
        self.assertEqual(text, "金百达 ddr4 3200 16g")
        self.assertEqual(text, text.lower())

        # keyword 为搜索关键词本身时不得进入匹配文本（P0 回归根因）
        p2 = Product(
            "2",
            "金百达 DDR4 3200 8G 个人自用",
            280,
            "https://x/2",
            keyword="光威 笔记本DDR4 3200 16G",
        )
        text2 = product_search_text(p2)
        self.assertNotIn("光威", text2)
        self.assertNotIn("笔记本", text2)
        self.assertNotIn("16g", text2)  # 标题没有 16G，文本也不应有

    def test_required_all_present(self) -> None:
        self.assertTrue(matches_required_keywords("金百达 ddr4 3200 16g", ["16g", "DDR4", "3200"]))

    def test_required_missing_any(self) -> None:
        self.assertFalse(matches_required_keywords("金百达 ddr4 3200", ["16G"]))
        self.assertFalse(matches_required_keywords("金百达 16G", ["DDR4", "16G"]))

    def test_required_empty_is_noop(self) -> None:
        self.assertTrue(matches_required_keywords("任意标题", []))
        self.assertTrue(matches_required_keywords("", []))

    def test_required_case_insensitive(self) -> None:
        """16G / 16g 等价。"""
        self.assertTrue(matches_required_keywords("金百达 16G 内存", ["16g"]))
        self.assertTrue(matches_required_keywords("金百达 16g 内存", ["16G"]))

    def test_exclude_any_hits(self) -> None:
        self.assertTrue(hits_exclude_keywords("高价回收笔记本", ["回收", "置换"]))
        self.assertTrue(hits_exclude_keywords("置换全新", ["回收", "置换"]))

    def test_exclude_no_hit(self) -> None:
        self.assertFalse(hits_exclude_keywords("光威笔记本自用", ["回收", "置换"]))

    def test_exclude_empty_is_noop(self) -> None:
        self.assertFalse(hits_exclude_keywords("高价回收", []))

    def test_exclude_case_insensitive(self) -> None:
        self.assertTrue(hits_exclude_keywords("GOLD回收", ["回收"]))
        self.assertTrue(hits_exclude_keywords("gold回收", ["GOLD"]))

    def test_passes_no_filters(self) -> None:
        p = make_product("1", "光威 DDR4 3200 16G 自用", price=200)
        self.assertTrue(product_passes_filter(p, [], []))

    def test_passes_required_hit(self) -> None:
        p = make_product("1", "光威 DDR4 3200 16G 自用", price=200)
        self.assertTrue(product_passes_filter(p, ["DDR4", "3200", "16G"], []))

    def test_passes_exclude_miss(self) -> None:
        p = make_product("1", "光威 DDR4 3200 16G 自用", price=200)
        self.assertTrue(product_passes_filter(p, [], ["回收", "置换"]))

    def test_filtered_by_required(self) -> None:
        p = make_product("1", "金百达 DDR4 3200 8G", price=200)
        self.assertFalse(product_passes_filter(p, ["16G"], []))

    def test_filtered_by_exclude(self) -> None:
        p = make_product("1", "高价回收 光威 DDR4 3200 16G", price=200)
        self.assertFalse(product_passes_filter(p, [], ["回收"]))

    def test_filtered_by_both(self) -> None:
        """叠加：命中排除词 + 缺失必含词 → 跳过。"""
        p = make_product("1", "高价回收 金百达 DDR4 8G", price=200)
        self.assertFalse(product_passes_filter(p, ["16G"], ["回收"]))


# ---------------------------------------------------------------------- #
# 3. Monitor 集成：过滤发生在阈值检查前
# ---------------------------------------------------------------------- #
class TestMonitorFiltering(unittest.TestCase):
    """Monitor 端到端：排除词 / 必含词在 run_once 内生效。"""

    def setUp(self) -> None:
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)

    def _run(self, config: Config, products: List[Product]) -> int:
        """用固定商品列表跑一轮，返回通知数。"""
        recorder = RecordingNotifier()
        monitor = Monitor(config, StubFetcher(products), self.storage, [recorder])
        monitor.run_once()
        return recorder

    # ---- 排除词 ----
    def test_exclude_hit_skips(self) -> None:
        products = [
            make_product("1", "高价回收 光威 DDR4 3200 16G", price=200),
            make_product("2", "光威 DDR4 3200 16G 自用", price=200),
        ]
        config = make_config(exclude_keywords=["回收"], required_keywords=[])
        recorder = self._run(config, products)
        self.assertEqual([p.product_id for p in recorder.received], ["2"])

    def test_exclude_miss_passes(self) -> None:
        products = [make_product("1", "光威 DDR4 3200 16G 自用", price=200)]
        config = make_config(exclude_keywords=["回收"], required_keywords=[])
        recorder = self._run(config, products)
        self.assertEqual([p.product_id for p in recorder.received], ["1"])

    def test_exclude_empty_noop(self) -> None:
        """空排除词 = 等同无过滤（兼容旧配置）。"""
        products = [make_product("1", "高价回收 光威", price=200)]
        config = make_config(exclude_keywords=[], required_keywords=[])
        recorder = self._run(config, products)
        self.assertEqual([p.product_id for p in recorder.received], ["1"])

    def test_exclude_case_insensitive(self) -> None:
        products = [
            make_product("1", "光威 16G 内存条 GOLD", price=200),
            make_product("2", "光威 16G 内存条 gold", price=200),
        ]
        config = make_config(exclude_keywords=["GOLD"], required_keywords=[])
        recorder = self._run(config, products)
        self.assertEqual(recorder.received, [])

    # ---- 必含词 ----
    def test_required_all_hit_passes(self) -> None:
        # 真实数据流：fetcher 会把 product.keyword 设为搜索关键词本身；
        # 标题包含全部必含词 → 应通过。
        products = [
            Product(
                "1", "光威 DDR4 3200 16G 自用", 200,
                "https://x/1", keyword="光威 笔记本DDR4 3200 16G",
            )
        ]
        config = make_config(required_keywords=["16G", "DDR4", "3200"])
        recorder = self._run(config, products)
        self.assertEqual([p.product_id for p in recorder.received], ["1"])

    def test_required_missing_skips(self) -> None:
        # P0 回归：keyword 即使等于搜索关键词（含 16G），只要**标题**缺 16G
        # 就必须被过滤——匹配文本只能来自标题，不能来自 keyword。
        products = [
            Product(
                "1", "金百达 DDR4 3200 8G 台式机", 200,
                "https://x/1", keyword="光威 笔记本DDR4 3200 16G",
            )
        ]
        config = make_config(required_keywords=["16G", "DDR4", "3200"])
        recorder = self._run(config, products)
        self.assertEqual(recorder.received, [])

    def test_required_empty_noop(self) -> None:
        """空必含词 = 不强制要求（等同关闭功能）。"""
        products = [make_product("1", "金百达 DDR4 8G", price=200)]
        config = make_config(required_keywords=[])
        recorder = self._run(config, products)
        self.assertEqual([p.product_id for p in recorder.received], ["1"])

    def test_required_auto_extracted_from_keyword(self) -> None:
        """未显式配置 required_keywords → 自动提取（默认开启严格匹配）。"""
        products = [
            make_product("1", "光威 DDR4 3200 16G 自用", price=200),
            make_product("2", "光威 DDR4 3200 8G 自用", price=200),
        ]
        # 主关键词含 16G / DDR4 / 3200 → 自动提取为必含词
        config = make_config(required_keywords=None)
        recorder = self._run(config, products)
        self.assertEqual([p.product_id for p in recorder.received], ["1"])

    # ---- 叠加 ----
    def test_combined_exclude_and_required(self) -> None:
        products = [
            make_product("1", "高价回收 金百达 DDR4 8G", price=200),  # 排除命中 + 必含缺失
            make_product("2", "高价回收 光威 DDR4 3200 16G", price=200),  # 仅排除命中
            make_product("3", "光威 DDR4 3200 16G 自用", price=200),  # 全通过
        ]
        config = make_config(exclude_keywords=["回收"], required_keywords=["16G", "DDR4", "3200"])
        recorder = self._run(config, products)
        self.assertEqual([p.product_id for p in recorder.received], ["3"])

    # ---- 与阈值检查的交互 ----
    def test_filter_before_threshold(self) -> None:
        """过滤发生在阈值检查前：被过滤的低价商品不通知、不计入已见。"""
        products = [
            make_product("1", "高价回收 光威 DDR4 3200 16G", price=1.0),  # 低价但被排除
            make_product("2", "光威 DDR4 3200 16G 自用", price=1.0),  # 低价且通过
        ]
        config = make_config(exclude_keywords=["回收"], required_keywords=[])
        recorder = self._run(config, products)
        self.assertEqual([p.product_id for p in recorder.received], ["2"])
        # 被过滤的商品不应被标记已提醒
        self.assertFalse(self.storage.is_notified("测试关键词", "1"))
        self.assertTrue(self.storage.is_notified("测试关键词", "2"))

    def test_filtered_products_not_recorded_as_seen(self) -> None:
        """被过滤的商品不进已见集合：后续规则放宽时仍可当作新商品提醒。"""
        products = [make_product("1", "高价回收 光威 16G", price=200)]
        config = make_config(exclude_keywords=["回收"], required_keywords=[])
        self._run(config, products)
        self.assertEqual(self.storage.get_previous_round_ids("测试关键词"), set())

    def test_round_result_counts(self) -> None:
        """RoundResult 应统计 filtered 数量。"""
        products = [
            make_product("1", "高价回收 光威 16G", price=200),
            make_product("2", "光威 16G 自用", price=200),
        ]
        config = make_config(exclude_keywords=["回收"], required_keywords=[])
        monitor = Monitor(config, StubFetcher(products), self.storage, [])
        monitor.run_once()
        self.assertEqual(monitor.last_result.fetched, 2)
        self.assertEqual(monitor.last_result.filtered, 1)
        self.assertEqual(monitor.last_result.new_products, 1)


# ---------------------------------------------------------------------- #
# 4. Config 解析
# ---------------------------------------------------------------------- #
class TestConfigParsing(unittest.TestCase):
    """config_from_dict 对过滤字段的解析与校验。"""

    def test_auto_extract_defaults(self) -> None:
        """未显式配置 required_keywords → 自动提取。"""
        config = config_from_dict(
            {
                "keywords": [{"keyword": "光威 笔记本DDR4 3200 16G", "max_price": 300}],
                "notify": {"channels": [{"type": "console"}]},
            }
        )
        rule = config.keywords[0]
        self.assertEqual(rule.exclude_keywords, [])
        self.assertEqual(rule.required_keywords, ["DDR4", "3200", "16G"])

    def test_explicit_empty_disables(self) -> None:
        """显式空列表 required_keywords: [] → 不强制（关闭自动提取）。"""
        config = make_config(required_keywords=[])
        self.assertEqual(config.keywords[0].required_keywords, [])

    def test_explicit_values_parsed(self) -> None:
        config = make_config(
            exclude_keywords=["回收", "置换"],
            required_keywords=["16G", "DDR4"],
        )
        rule = config.keywords[0]
        self.assertEqual(rule.exclude_keywords, ["回收", "置换"])
        self.assertEqual(rule.required_keywords, ["16G", "DDR4"])

    def test_strings_normalized_deduped(self) -> None:
        config = config_from_dict(
            {
                "keywords": [
                    {
                        "keyword": "内存",
                        "max_price": 100,
                        "exclude_keywords": [" 回收 ", "回收", "置换", ""],
                        "required_keywords": ["16G", " 16G "],
                    }
                ],
                "notify": {"channels": [{"type": "console"}]},
            }
        )
        rule = config.keywords[0]
        self.assertEqual(rule.exclude_keywords, ["回收", "置换"])
        self.assertEqual(rule.required_keywords, ["16G"])

    def test_exclude_non_list_raises(self) -> None:
        with self.assertRaises(ConfigError):
            config_from_dict(
                {
                    "keywords": [{"keyword": "内存", "max_price": 100, "exclude_keywords": "回收"}],
                    "notify": {"channels": [{"type": "console"}]},
                }
            )

    def test_required_non_list_raises(self) -> None:
        with self.assertRaises(ConfigError):
            config_from_dict(
                {
                    "keywords": [{"keyword": "内存", "max_price": 100, "required_keywords": "16G"}],
                    "notify": {"channels": [{"type": "console"}]},
                }
            )

    def test_keyword_map_unchanged(self) -> None:
        """keyword_map 仍返回 {关键词: 阈值}，不受过滤字段影响。"""
        config = make_config(required_keywords=["16G"])
        self.assertEqual(config.keyword_map(), {"光威 笔记本DDR4 3200 16G": 300.0})


# ---------------------------------------------------------------------- #
# 5. GUI 纯函数
# ---------------------------------------------------------------------- #
class TestGuiFilterFunctions(unittest.TestCase):
    """GUI 过滤编辑相关纯函数与配置往返。"""

    def test_parse_keyword_lines(self) -> None:
        self.assertEqual(parse_keyword_lines("回收\n置换\n\n回收\n收购"), ["回收", "置换", "收购"])
        self.assertEqual(parse_keyword_lines(""), [])
        self.assertEqual(parse_keyword_lines(None), [])
        self.assertEqual(parse_keyword_lines("  16G  "), ["16G"])

    def test_add_preset_excludes(self) -> None:
        """v3.3：预置排除词含「收」，追加去重保序。"""
        result = add_preset_excludes(["回收"])
        self.assertEqual(result, ["回收", "置换", "收购", "高价回收", "收"])
        self.assertEqual(add_preset_excludes([]), ["回收", "置换", "收购", "高价回收", "收"])
        # 已含预置词时不重复
        result = add_preset_excludes(["回收", "置换", "收购", "高价回收", "收"])
        self.assertEqual(result, ["回收", "置换", "收购", "高价回收", "收"])

    def test_apply_filter_edit(self) -> None:
        current = {"exclude_keywords": ["回收"], "required_keywords": ["16G"]}
        result = apply_filter_edit(current, "回收\n置换", "16G\nDDR4")
        self.assertEqual(result["exclude_keywords"], ["回收", "置换"])
        self.assertEqual(result["required_keywords"], ["16G", "DDR4"])
        # 留空 = 清空
        result = apply_filter_edit(current, "", "")
        self.assertEqual(result["exclude_keywords"], [])
        self.assertEqual(result["required_keywords"], [])

    def test_keyword_filter_summary(self) -> None:
        self.assertEqual(
            keyword_filter_summary({"exclude_keywords": ["回收"], "required_keywords": ["16G"]}),
            "排除:回收 必含:16G",
        )
        self.assertEqual(keyword_filter_summary({"exclude_keywords": [], "required_keywords": []}), "—")
        self.assertEqual(keyword_filter_summary(None), "—")

    def test_config_to_form_reads_filters(self) -> None:
        form = config_to_form(
            {
                "keywords": [
                    {
                        "keyword": "光威 笔记本DDR4 3200 16G",
                        "max_price": 300,
                        "exclude_keywords": ["回收", "置换"],
                        "required_keywords": ["16G", "DDR4"],
                    }
                ]
            }
        )
        self.assertEqual(form["keywords"], [("光威 笔记本DDR4 3200 16G", 300.0)])
        self.assertEqual(
            form["keyword_filters"]["光威 笔记本DDR4 3200 16G"],
            {"exclude_keywords": ["回收", "置换"], "required_keywords": ["16G", "DDR4"]},
        )

    def test_config_to_form_auto_extract_for_display(self) -> None:
        """未显式配置 required_keywords 时，界面展示自动提取结果（与生效规则一致）。"""
        form = config_to_form(
            {"keywords": [{"keyword": "光威 笔记本DDR4 3200 16G", "max_price": 300}]}
        )
        state = form["keyword_filters"]["光威 笔记本DDR4 3200 16G"]
        self.assertEqual(state["exclude_keywords"], [])
        self.assertEqual(state["required_keywords"], ["DDR4", "3200", "16G"])

    def test_build_config_dict_writes_filters(self) -> None:
        data = build_config_dict(
            keywords=[("光威 笔记本DDR4 3200 16G", 300.0)],
            interval_seconds=300,
            fetcher_type="mock",
            cookies="",
            storage_path="state/x.db",
            channels={"console": {"enabled": True, "options": {}}},
            keyword_filters={
                "光威 笔记本DDR4 3200 16G": {
                    "exclude_keywords": ["回收", "置换"],
                    "required_keywords": ["16G"],
                }
            },
        )
        self.assertEqual(
            data["keywords"],
            [
                {
                    "keyword": "光威 笔记本DDR4 3200 16G",
                    "max_price": 300.0,
                    "exclude_keywords": ["回收", "置换"],
                    "required_keywords": ["16G"],
                }
            ],
        )
        # 写回后必须能通过严格校验
        config = config_from_dict(data)
        self.assertEqual(config.keywords[0].exclude_keywords, ["回收", "置换"])
        self.assertEqual(config.keywords[0].required_keywords, ["16G"])

    def test_build_config_dict_empty_filters_roundtrip_disables(self) -> None:
        """显式写入空列表 required_keywords: [] → 重新加载后仍为空（关闭强制）。"""
        data = build_config_dict(
            keywords=[("光威 笔记本DDR4 3200 16G", 300.0)],
            interval_seconds=300,
            fetcher_type="mock",
            cookies="",
            storage_path="state/x.db",
            channels={"console": {"enabled": True, "options": {}}},
            keyword_filters={
                "光威 笔记本DDR4 3200 16G": {
                    "exclude_keywords": [],
                    "required_keywords": [],
                }
            },
        )
        config = config_from_dict(data)
        self.assertEqual(config.keywords[0].required_keywords, [])
        self.assertEqual(config.keywords[0].exclude_keywords, [])

    def test_build_config_dict_without_filters_keeps_old_shape(self) -> None:
        """不传 keyword_filters（旧调用方）→ 不写过滤字段，保持向后兼容。"""
        data = build_config_dict(
            keywords=[("Switch", 800.0)],
            interval_seconds=300,
            fetcher_type="mock",
            cookies="",
            storage_path="state/x.db",
            channels={"console": {"enabled": True, "options": {}}},
        )
        self.assertEqual(data["keywords"], [{"keyword": "Switch", "max_price": 800.0}])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
