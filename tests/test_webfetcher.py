"""网页兜底采集解析层测试（tests/test_webfetcher.py）。

这是 M02（网页兜底采集）此前**完全缺失**的独立测试。它的价值不只是"覆盖代码"：
网页路径依赖平台 DOM 结构，是全项目最容易碎的一环，因此这里用**HTML 夹具**
把"平台长什么样"固化成可回归的输入，平台改版时可以拿新页面直接替换夹具来定位问题。

覆盖点：
    1. 三级策略各自命中（inline_json / json_ld / dom_cards）；
    2. 选择器表可被注入替换（改版修复只需改数据，不改逻辑）；
    3. 解析失败时**有诊断**：ParseReport 说明扫描量、跳过原因与排查建议；
    4. 异常输入不崩（空 HTML、垃圾 HTML、非法价格、重复商品）。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.fetcher import WebFetcher  # noqa: E402
from xianyu_alert.web_parse import (  # noqa: E402
    WEB_SELECTORS,
    WebSelectors,
    parse_search_html,
)

# ---------------------------------------------------------------------- #
# HTML 夹具：把"平台长什么样"固化成可回归的输入
# ---------------------------------------------------------------------- #

INLINE_JSON_HTML = """
<html><body><script>
window.__INIT_DATA__ = {"data":{"itemList":[
  {"itemId":"810000000001","title":"Switch OLED 95新","price":"1299","picUrl":"//img.alicdn.com/a.jpg","publishTime":"3分钟前"},
  {"itemId":"810000000002","title":"Switch 游戏卡带","priceText":"88.5","picUrl":"http://img.alicdn.com/b.jpg"}
]}};
</script></body></html>
"""

JSON_LD_HTML = """
<html><head><script type="application/ld+json">
{"@type":"Product","name":"Switch Lite 日版","sku":"820000000001",
 "image":["https://img.alicdn.com/c.jpg"],
 "offers":{"@type":"Offer","price":"699"},"url":"https://www.goofish.com/item?id=820000000001"}
</script></head><body></body></html>
"""

DOM_CARDS_HTML = """
<html><body>
  <div class="card"><a href="/item?id=830000000001" title="DOM 商品一">
      <span class="price-num">¥199</span><img src="https://img.alicdn.com/d.jpg"></a></div>
  <div class="card"><a href="/item?id=830000000002" aria-label="DOM 商品二">
      <span class="price">88</span><img data-src="//img.alicdn.com/e.jpg"></a></div>
  <a href="/item?id=830000000001" title="重复商品"><span class="price">1</span></a>
  <a href="/user/profile" title="非商品链接"><span class="price">1</span></a>
</body></html>
"""

#: 平台改版夹具：价格节点换了名字（class=cost-tag）——应被"整卡文本兜底"救回
RENAMED_STRUCTURE_HTML = """
<html><body>
  <a href="/item?id=840000000001" title="改版后的商品">
    <span class="cost-tag">¥150</span><img src="https://img.alicdn.com/f.jpg"></a>
</body></html>
"""

#: 真正的"解析不出来"夹具：卡片里连数字都没有（无法兜底）
NO_PRICE_HTML = """
<html><body>
  <a href="/item?id=860000000001" title="价格面议"><span class="cost-tag">面议</span></a>
</body></html>
"""

GARBAGE_HTML = "<html><body><p>请登录后查看</p></body></html>"


class TestInlineJsonStrategy(unittest.TestCase):
    """策略一：内联 JSON（结构最稳）。"""

    def test_parses_items(self) -> None:
        products, report = parse_search_html(INLINE_JSON_HTML, "Switch")
        self.assertEqual(report.source, "inline_json")
        self.assertEqual(len(products), 2)
        self.assertEqual(products[0].product_id, "810000000001")
        self.assertEqual(products[0].price, 1299.0)
        self.assertEqual(products[0].keyword, "Switch")
        self.assertTrue(products[0].image_url.startswith("https://"), "协议相对地址应补全为 https")

    def test_ignores_items_without_price(self) -> None:
        html = INLINE_JSON_HTML.replace('"price":"1299"', '"price":""')
        products, report = parse_search_html(html, "Switch")
        ids = [p.product_id for p in products]
        self.assertNotIn("810000000001", ids, "缺价格的内联条目应被跳过")
        self.assertIn("810000000002", ids)


class TestJsonLdStrategy(unittest.TestCase):
    """策略二：schema.org 结构化数据（跨站点通用约定）。"""

    def test_parses_schema_org_product(self) -> None:
        products, report = parse_search_html(JSON_LD_HTML, "Switch")
        self.assertEqual(report.source, "json_ld")
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].product_id, "820000000001")
        self.assertEqual(products[0].price, 699.0)
        self.assertEqual(products[0].title, "Switch Lite 日版")

    def test_broken_json_ld_is_skipped(self) -> None:
        html = '<script type="application/ld+json">{不是合法 JSON}</script>'
        products, report = parse_search_html(html, "kw")
        self.assertEqual(products, [])
        self.assertNotEqual(report.source, "json_ld")

    def test_json_ld_from_url_when_sku_missing(self) -> None:
        html = JSON_LD_HTML.replace('"sku":"820000000001",', "")
        products, _report = parse_search_html(html, "Switch")
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].product_id, "820000000001", "应能从 url 反推商品 ID")


class TestDomStrategy(unittest.TestCase):
    """策略三：DOM 卡片（刻意不依赖具体 class）。"""

    def test_parses_cards_and_dedupes(self) -> None:
        products, report = parse_search_html(DOM_CARDS_HTML, "Switch")
        self.assertEqual(report.source, "dom")
        self.assertEqual([p.product_id for p in products],
                         ["830000000001", "830000000002"])
        self.assertGreaterEqual(report.skipped_duplicate, 1, "重复商品应计入诊断")
        self.assertGreaterEqual(report.skipped_no_product_id, 1, "非商品链接应计入诊断")

    def test_title_from_aria_label_and_lazy_image(self) -> None:
        products, _report = parse_search_html(DOM_CARDS_HTML, "Switch")
        second = products[1]
        self.assertEqual(second.title, "DOM 商品二", "标题应支持 aria-label")
        self.assertTrue(second.image_url.endswith("/e.jpg"), "应支持 data-src 懒加载图")

    def test_relative_url_is_absolutized(self) -> None:
        products, _report = parse_search_html(DOM_CARDS_HTML, "Switch")
        self.assertTrue(products[0].url.startswith("https://www.goofish.com/item?id="))

    def test_name_class_fallback_for_title(self) -> None:
        html = '<a href="/item?id=850000000001"><span class="item-name">兜底标题</span><span class="price">9</span></a>'
        products, _report = parse_search_html(html, "kw")
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].title, "兜底标题")


class TestDiagnostics(unittest.TestCase):
    """解析失败时的诊断能力 —— 网页路径此前最大的可用性缺口。"""

    def test_renamed_price_node_survives_via_text_fallback(self) -> None:
        """价格 class 改名后，整卡文本兜底仍能救回价格 —— 这是刻意的健壮性设计。"""
        products, report = parse_search_html(RENAMED_STRUCTURE_HTML, "kw")
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].price, 150.0)
        self.assertEqual(report.source, "dom")

    def test_unparseable_card_reports_missing_price(self) -> None:
        """连文本兜底都拿不到价格时：必须 0 条 + 明确告知原因与建议。"""
        products, report = parse_search_html(NO_PRICE_HTML, "kw")
        self.assertEqual(products, [])
        self.assertEqual(report.anchors_scanned, 1)
        self.assertEqual(report.skipped_missing_price, 1, "应记录'缺价格'这一具体原因")
        self.assertTrue(report.hint(), "0 条时必须给出排查建议")

    def test_injectable_selectors_fix_renamed_structure(self) -> None:
        """改版修复 = 改数据（选择器表），不改逻辑。"""
        patched = WebSelectors(price_class_patterns=("cost-tag",))
        products, _report = parse_search_html(RENAMED_STRUCTURE_HTML, "kw", patched)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].price, 150.0)

    def test_empty_shell_page_gives_login_hint(self) -> None:
        products, report = parse_search_html(GARBAGE_HTML, "kw")
        self.assertEqual(products, [])
        self.assertEqual(report.anchors_scanned, 0)
        self.assertIn("未登录", report.hint())

    def test_summary_contains_counters(self) -> None:
        _products, report = parse_search_html(DOM_CARDS_HTML, "kw")
        text = report.summary()
        self.assertIn("策略=dom", text)
        self.assertIn("扫描链接", text)

    def test_empty_html(self) -> None:
        products, report = parse_search_html("", "kw")
        self.assertEqual(products, [])
        self.assertEqual(report.source, "empty")
        self.assertEqual(report.html_bytes, 0)


class TestWebFetcherIntegration(unittest.TestCase):
    """WebFetcher.parse 委托解析层（保持对外行为不变）。"""

    def test_parse_delegates(self) -> None:
        fetcher = WebFetcher()
        products = fetcher.parse(INLINE_JSON_HTML, "Switch")
        self.assertEqual(len(products), 2)

    def test_parse_empty_returns_empty(self) -> None:
        self.assertEqual(WebFetcher().parse("", "kw"), [])

    def test_parse_never_raises_on_garbage(self) -> None:
        try:
            result = WebFetcher().parse("<html><body>" + "x" * 500 + "</body></html>", "kw")
        except Exception as exc:  # pragma: no cover - 兜底路径不允许抛异常
            self.fail(f"网页解析不应抛异常，却抛了 {type(exc).__name__}: {exc}")
        self.assertEqual(result, [])

    def test_default_selector_table_is_reachable(self) -> None:
        self.assertTrue(WEB_SELECTORS.image_attrs)
        self.assertTrue(WEB_SELECTORS.price_class_patterns)


if __name__ == "__main__":
    unittest.main()
