"""闲鱼网页搜索结果的解析层（网页兜底采集专用）。

为什么单独成一个模块
--------------------
网页兜底路径（mtop 不可用时的降级方案）是全项目**最容易碎**的一环：它依赖平台把商品数据
放在页面的什么位置。把它抽成**不依赖网络、不依赖实例状态的纯函数**之后：

1. 平台改版时**只改这一个文件**（选择器集中在 WEB_SELECTORS）；
2. 可以用 HTML 夹具做离线回归，不必真的联网（此前它几乎没有独立测试）；
3. 解析失败时能回答"**为什么 0 条**"——ParseReport 记录每级策略的命中与跳过原因。

策略顺序（从最稳到最脆）
------------------------
1. inline_json  —— script 里的初始数据（window.__INIT_DATA__ 等），结构最稳；
2. json_ld      —— schema.org 的 application/ld+json，跨站点通用约定；
3. dom_cards    —— 遍历"链接里带商品 ID"的 <a> 卡片；**刻意不依赖具体 class**，
                   因此平台改名时往往仍能命中，真正的脆弱点只在标题/价格/图片的提取。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import Product, normalize_image_url
from .parsing import extract_product_id, parse_price

logger = logging.getLogger(__name__)

#: 闲鱼站点根（相对链接补全用）
BASE_URL = "https://www.goofish.com"


@dataclass(frozen=True)
class WebSelectors:
    """网页卡片解析用到的全部选择器候选（**平台改版时只需要改这里**）。

    每一项都是"按顺序尝试"的候选列表：靠前的更精确、靠后的更宽松。
    把它们集中成数据而不是散落在代码里，是为了让改版修复变成一次小改动，
    并且可以被测试直接断言（见 tests/test_webfetcher.py）。
    """

    #: 图片地址可能落在这些属性上（懒加载站点常用 data-* 兜底）
    image_attrs: tuple[str, ...] = (
        "src",
        "data-src",
        "data-ks-lazyload",
        "data-lazy-src",
        "data-original",
    )
    #: 标题优先从这些属性取（比 class 稳定）
    title_attrs: tuple[str, ...] = ("title", "aria-label")
    #: 退而求其次：class 里含这些词的元素文本即标题
    title_class_patterns: tuple[str, ...] = ("title", "name", "desc", "main")
    #: 价格元素 class 里含这些词
    price_class_patterns: tuple[str, ...] = ("price", "price-num", "number")


#: 默认选择器表（模块级单例，测试可注入替换）
WEB_SELECTORS = WebSelectors()

#: 内联 JSON 的常见变量名
INLINE_JSON_PATTERNS = (
    r"window\.__(?:INIT_DATA|NEXT_DATA|PAGE_DATA|INITIAL_STATE)__\s*=\s*(\{.*?\})\s*[;<]",
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;",
)


@dataclass
class ParseReport:
    """一次网页解析的诊断报告（用来回答"为什么这一轮 0 条"）。

    网页路径最大的可用性问题不是"解析不出来"，而是**解析不出来时没有任何线索**。
    本报告把"扫描了多少链接、各自因为什么被跳过、最终用了哪级策略"记下来，
    0 条时会被调用方打成 WARNING，直接指向可能的原因。
    """

    source: str = "empty"
    html_bytes: int = 0
    anchors_scanned: int = 0
    skipped_no_product_id: int = 0
    skipped_duplicate: int = 0
    skipped_missing_title: int = 0
    skipped_missing_price: int = 0
    skipped_invalid: int = 0
    products: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """人类可读的一行摘要（用于日志 / 排障）。"""
        return (
            f"网页解析 {self.products} 条（策略={self.source}，HTML {self.html_bytes} 字节）："
            f"扫描链接 {self.anchors_scanned}，无商品ID {self.skipped_no_product_id}，"
            f"缺标题 {self.skipped_missing_title}，缺价格 {self.skipped_missing_price}，"
            f"重复 {self.skipped_duplicate}，非法 {self.skipped_invalid}"
        )

    def hint(self) -> str:
        """0 条时给出的排查建议（指向具体可动的地方）。"""
        if self.products:
            return ""
        if self.source in ("inline_json", "json_ld"):
            return "内联数据里没有商品字段，可能平台改了数据结构"
        if self.anchors_scanned == 0:
            return "页面里没有任何带链接的锚点，可能未登录 / 被风控拦截 / 返回的是空壳页"
        if self.skipped_missing_price >= self.skipped_missing_title:
            return "多数卡片缺价格：平台可能改了价格节点，检查 WEB_SELECTORS.price_class_patterns"
        return "多数卡片缺标题：平台可能改了标题节点，检查 WEB_SELECTORS.title_class_patterns"


def parse_inline_json(html: str, keyword: str) -> list[Product]:
    """从内联 JSON（script 里的初始数据）提取商品。

    Args:
        html: 页面 HTML。
        keyword: 当前关键词。

    Returns:
        商品列表（保持页面顺序，已去重）。
    """
    results: list[Product] = []
    seen: set[str] = set()
    for pattern in INLINE_JSON_PATTERNS:
        for match in re.finditer(pattern, html, re.DOTALL):
            try:
                data = json.loads(match.group(1))
            except (json.JSONDecodeError, IndexError):
                continue
            for item in walk_json_items(data):
                product = product_from_json_item(item, keyword)
                if product is not None and product.product_id not in seen:
                    seen.add(product.product_id)
                    results.append(product)
    return results


def walk_json_items(node: Any) -> list[dict[str, Any]]:
    """按**文档顺序**深度遍历 JSON，产出"看起来像商品"的 dict 节点。

    顺序很重要：商品在页面上的先后通常等于推荐/时间序，反转会改变展示与优先级判断。
    这里用显式栈 + 逆序压栈来保证顺序（也避免深 JSON 的递归深度问题）。
    """
    found: list[dict[str, Any]] = []
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            keys = set(current.keys())
            if keys & {"itemId", "id"} and keys & {"title", "name", "content"}:
                found.append(current)
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return found


def product_from_json_item(item: dict[str, Any], keyword: str) -> Product | None:
    """把内联 JSON 里的一个节点转成 Product（字段缺失 / 非法时返回 None）。"""
    raw_id = item.get("itemId") or item.get("id") or ""
    title = str(item.get("title") or item.get("name") or item.get("content") or "").strip()
    price_value = item.get("price") or item.get("priceText") or item.get("soldPrice") or ""
    price = _coerce_price(price_value)
    if not raw_id or not title or price is None:
        return None
    product_id = str(raw_id)
    url = f"{BASE_URL}/item?id={product_id}"
    image = item.get("picUrl") or item.get("imageUrl") or item.get("pic") or ""
    try:
        return Product(
            product_id=product_id,
            title=title[:200],
            price=price,
            url=url,
            publish_time=str(item.get("publishTime") or item.get("publish_time") or ""),
            keyword=keyword,
            image_url=normalize_image_url(image),
        )
    except ValueError:
        return None


def _coerce_price(value: Any) -> float | None:
    """把 JSON 里的价格字段（可能是 "12.5" / 12.5 / "¥12.5"）转成 float。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def parse_json_ld(html: str, keyword: str) -> list[Product]:
    """从 schema.org 的 application/ld+json 提取商品（跨站点通用约定）。

    Args:
        html: 页面 HTML。
        keyword: 当前关键词。

    Returns:
        商品列表。
    """
    results: list[Product] = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = _coerce_price(offers.get("price") if isinstance(offers, dict) else None)
            name = str(node.get("name") or "").strip()
            product_id = str(node.get("sku") or node.get("productID") or "").strip()
            if not product_id:
                product_id = extract_product_id(str(node.get("url") or ""))
            if not (name and price is not None and product_id):
                continue
            image = node.get("image") or ""
            if isinstance(image, list):
                image = image[0] if image else ""
            try:
                results.append(
                    Product(
                        product_id=product_id,
                        title=name[:200],
                        price=price,
                        url=str(node.get("url") or f"{BASE_URL}/item?id={product_id}"),
                        publish_time="",
                        keyword=keyword,
                        image_url=normalize_image_url(image),
                    )
                )
            except ValueError:
                continue
    return results




def extract_image(node: Any, selectors: WebSelectors) -> str:
    """从卡片节点提取主图地址（按 selectors.image_attrs 顺序尝试属性）。"""
    if node is None or not hasattr(node, "find"):
        return ""
    for img in node.find_all("img"):
        for attr in selectors.image_attrs:
            value = img.get(attr)
            if value:
                normalized = normalize_image_url(value)
                if normalized:
                    return normalized
    return ""


def extract_title(anchor: Any, selectors: WebSelectors) -> str:
    """从卡片锚点提取标题（先属性、再按 class 关键词找子节点）。"""
    for attr in selectors.title_attrs:
        value = anchor.get(attr)
        if value:
            return str(value).strip()
    for pattern in selectors.title_class_patterns:
        node = anchor.find(attrs={"class": re.compile(pattern, re.I)})
        if node is not None:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return ""


def extract_price_text(anchor: Any, container: Any, selectors: WebSelectors) -> str:
    """从卡片（或父容器）提取价格文本。"""
    for scope in (anchor, container):
        if scope is None or not hasattr(scope, "find"):
            continue
        for pattern in selectors.price_class_patterns:
            node = scope.find(attrs={"class": re.compile(pattern, re.I)})
            if node is not None:
                text = node.get_text(" ", strip=True)
                if text:
                    return text
    return ""


def parse_dom(
    html: str, keyword: str, selectors: WebSelectors = WEB_SELECTORS
) -> tuple[list[Product], ParseReport]:
    """DOM 兜底：遍历"链接里带商品 ID"的卡片。

    **刻意不依赖具体卡片 class** —— 只要平台仍然把商品 ID 放在链接里，就能定位到卡片；
    真正脆弱的是卡片内部的标题/价格/图片，这几项由 selectors 决定。

    Args:
        html: 页面 HTML。
        keyword: 当前关键词。
        selectors: 选择器表（测试可注入）。

    Returns:
        (商品列表, 诊断报告)。
    """
    report = ParseReport(source="dom", html_bytes=len(html))
    soup = BeautifulSoup(html, "html.parser")
    results: list[Product] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        report.anchors_scanned += 1
        href = str(anchor["href"])
        product_id = extract_product_id(href)
        if not product_id:
            report.skipped_no_product_id += 1
            continue
        if product_id in seen:
            report.skipped_duplicate += 1
            continue

        container = anchor.parent
        container_text = container.get_text(" ", strip=True) if container is not None else ""
        card_text = anchor.get_text(" ", strip=True)
        full_text = card_text or container_text

        title = extract_title(anchor, selectors) or full_text
        if not title:
            report.skipped_missing_title += 1
            continue
        price = parse_price(extract_price_text(anchor, container, selectors) or full_text)
        if price is None:
            report.skipped_missing_price += 1
            continue

        seen.add(product_id)
        url = href if href.startswith("http") else urljoin(BASE_URL, href)
        try:
            results.append(
                Product(
                    product_id=product_id,
                    title=title[:200],
                    price=price,
                    url=url,
                    publish_time="",
                    keyword=keyword,
                    image_url=extract_image(anchor, selectors) or extract_image(container, selectors),
                )
            )
        except ValueError:
            report.skipped_invalid += 1

    report.products = len(results)
    return results, report


def parse_search_html(
    html: str, keyword: str, selectors: WebSelectors = WEB_SELECTORS
) -> tuple[list[Product], ParseReport]:
    """解析搜索结果页（三级策略，纯函数）。

    Args:
        html: 页面 HTML。
        keyword: 当前关键词。
        selectors: 选择器表（测试可注入）。

    Returns:
        (商品列表, 诊断报告)；报告里的 source 指出最终生效的策略。
    """
    if not html:
        return [], ParseReport(source="empty", html_bytes=0)

    errors: list[str] = []

    products = parse_inline_json(html, keyword)
    if products:
        return products, ParseReport(
            source="inline_json", html_bytes=len(html), products=len(products)
        )

    try:
        products = parse_json_ld(html, keyword)
    except Exception as exc:  # noqa: BLE001 - 兜底策略不允许把整轮抓取带崩
        errors.append(f"json_ld 异常：{exc}")
        products = []
    if products:
        return products, ParseReport(
            source="json_ld", html_bytes=len(html), products=len(products), notes=errors
        )

    results, report = parse_dom(html, keyword, selectors)
    report.notes.extend(errors)
    return results, report
