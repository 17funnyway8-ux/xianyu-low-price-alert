"""关键词过滤业务规则：排除词 + 必含词（v3.1 新增）。

纯函数模块，不依赖网络 / 存储 / GUI，便于单元测试。

规则优先级（Monitor 在 fetcher 返回后、价格阈值检查前调用）：
    1. 必含词缺失 → 跳过（required_keywords 非空时，商品文本必须包含全部）；
    2. 排除词命中 → 跳过（exclude_keywords 非空时，商品文本命中任一即跳过）。
二者可叠加：任一条件不满足即跳过。

匹配方式：商品文本（标题 + 关键词 + 可选的 seller/location 等字段）统一
lowercase 后做子串匹配，大小写不敏感（`16G` / `16g` 等价；中文不受影响）。
"""

from __future__ import annotations

import re
from typing import Iterable, List

from .models import Product

#: 从主关键词提取「数字 + 字母」组合片段的匹配模式。
#:   [A-Za-z]*\d+[A-Za-z]*  -> DDR4 / 16G / 8GB / 3200 / RTX3060 / usb3
#: 覆盖三种形态：字母+数字（DDR4）、数字+字母（16G）、纯数字（3200）。
#: 不用 `\b` 做边界：中文字符与字母同为 \w，`笔记本DDR4` 中 `\b` 不成立，
#: 会导致 DDR4 漏提、反而提出垃圾片段「4」；改为「字母/数字簇」整体匹配即可。
#: 纯中文词（如「笔记本」）不含数字，自然不会被提取，避免过滤过严误杀合法商品。
_REQUIRED_TOKEN_RE = re.compile(r"[A-Za-z]*\d+[A-Za-z]*")

#: Product 上可能存在的附加文本字段（fetcher 未来补充 seller/location 时自动纳入过滤）
_EXTRA_TEXT_FIELDS = ("seller", "location", "shop_name")


def normalize_keywords(values: Iterable[str]) -> List[str]:
    """规范化关键词列表：去空白、去空串、去重、保序。

    Args:
        values: 原始关键词可迭代对象（可为 None / 含非字符串）。

    Returns:
        规范化后的字符串列表。
    """
    seen: List[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        if text not in seen:
            seen.append(text)
    return seen


def extract_required_keywords(keyword: str) -> List[str]:
    """从主关键词自动提取「数字 + 可选单位」片段作为必含词默认值。

    例如：
        「光威 笔记本DDR4 3200 16G」 -> ["DDR4", "3200", "16G"]
        「Switch」                    -> []
        「iPhone 15」                 -> ["15"]
        「笔记本」                    -> []（纯中文不提取）

    Args:
        keyword: 主搜索关键词。

    Returns:
        提取出的片段列表（可能为空；已去重保序）。
    """
    tokens: List[str] = []
    for token in _REQUIRED_TOKEN_RE.findall(str(keyword or "")):
        # 纯单个数字（如「第4号」中的 4）太弱，作为必含词没有区分度，丢弃
        if len(token) == 1 and token.isdigit():
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def product_search_text(product: Product) -> str:
    """拼接商品的搜索文本并统一小写。

    参与匹配的字段：**标题**（必含）与 fetcher 未来可能补充的
    seller / location / shop_name（存在才加入）。

    注意：**不拼接 `product.keyword`**。真实 fetcher（mtop/web/mock）与
    monitor 兜底都会把 `keyword` 设为搜索关键词本身，而必含词默认正是从
    搜索关键词自动提取的——若把 keyword 拼进来，每个商品的匹配文本必然
    包含全部必含词，必含词过滤将永远失效（P0 回归：B「金百达 DDR4 3200 8G」
    被误通知）。同理，搜索关键词若含排除词，拼入后会误滤全部商品。

    Args:
        product: 商品对象。

    Returns:
        规范化小写文本，多个字段用空格分隔。
    """
    parts: List[str] = [product.title]
    for attr in _EXTRA_TEXT_FIELDS:
        value = getattr(product, attr, "")
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def matches_required_keywords(text: str, required_keywords: Iterable[str]) -> bool:
    """商品文本是否包含全部必含词（大小写不敏感）。

    Args:
        text: 商品搜索文本（未小写亦可，内部统一处理）。
        required_keywords: 必含词列表；为空表示不强制要求（返回 True）。

    Returns:
        True 表示全部命中（或必含词为空）。
    """
    lowered = str(text or "").lower()
    for token in normalize_keywords(required_keywords):
        if token.lower() not in lowered:
            return False
    return True


def hits_exclude_keywords(text: str, exclude_keywords: Iterable[str]) -> bool:
    """商品文本是否命中任一排除词（大小写不敏感）。

    Args:
        text: 商品搜索文本（未小写亦可，内部统一处理）。
        exclude_keywords: 排除词列表；为空表示不排除（返回 False）。

    Returns:
        True 表示命中任一排除词（应跳过该商品）。
    """
    lowered = str(text or "").lower()
    for token in normalize_keywords(exclude_keywords):
        if token.lower() in lowered:
            return True
    return False


def product_passes_filter(
    product: Product,
    required_keywords: Iterable[str],
    exclude_keywords: Iterable[str],
) -> bool:
    """综合过滤：必含词缺失或排除词命中任一 → False（应跳过）。

    Args:
        product: 商品对象。
        required_keywords: 必含词列表（空 = 不强制）。
        exclude_keywords: 排除词列表（空 = 不排除）。

    Returns:
        True 表示通过过滤（应继续参与价格阈值检查）。
    """
    text = product_search_text(product)
    if not matches_required_keywords(text, required_keywords):
        return False
    if hits_exclude_keywords(text, exclude_keywords):
        return False
    return True
