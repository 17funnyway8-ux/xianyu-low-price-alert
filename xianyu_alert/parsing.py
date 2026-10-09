"""共享解析原语：商品 ID / 价格 / 发布时间。

这些函数同时服务于 mtop 与网页兜底两条采集路径，之前定义在 fetcher.py 里。
v1.9.6 提取为独立模块，使网页解析层（web_parse.py）可以复用而不必复制实现
——复制一份的代价是日后修 bug 只修一处，两条路径行为悄悄分叉。
"""

from __future__ import annotations

import re

_ID_PATTERNS = (
    re.compile(r"/items?/(\d{6,})"),
    re.compile(r"[?&]id=(\d{6,})"),
)
# 价格文本：¥1,299.00 / 1299 / 1299.5
_PRICE_PATTERN = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)")
#: 无法定价的关键词（闲鱼常见「面议 / 电议 / 私聊 / 咨询」文案，直接放弃解析）
_PRICE_BLOCK_WORDS = ("面议", "电议", "私聊", "咨询")
# 发布时间文案：3分钟前 / 2小时前 / 昨天 / 2024-05-01
_TIME_PATTERN = re.compile(
    r"(\d+\s*(?:秒|分钟|小时|天|周|个月|月|年)前|刚刚|今天\s*\d{1,2}:\d{2}|昨天\s*\d{1,2}:\d{2}"
    r"|昨天|前天|\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?|\d{1,2}-\d{1,2}\s*(?:发布)?)"
)


def extract_product_id(url: str) -> str:
    """从商品链接中提取数字商品 ID。

    Args:
        url: 商品链接（可能是相对路径）。

    Returns:
        商品 ID 字符串；提取不到时返回空串。
    """
    if not url:
        return ""
    for pattern in _ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return ""


def parse_price(text: str) -> float | None:
    """从任意文本中解析出第一个价格数字（v3 增强版）。

    支持：
        - 旧格式（保持既有行为）：`¥1,299.00` / `1299` / `1299.5`；
        - 万换算（移植 exe 资产）：`1.2万` -> 12000.0、`3.5万` -> 35000.0；
        - 关键词过滤：「面议 / 电议 / 私聊 / 咨询」等非定价文案返回 None。

    Args:
        text: 含价格的文本。

    Returns:
        解析出的价格；解析失败或命中过滤关键词时返回 None。
    """
    if not text:
        return None
    s = str(text).strip()
    # 去掉货币符号、千分位逗号与空白
    s = s.replace("￥", "").replace("¥", "").replace(",", "").replace(" ", "")
    if not s:
        return None
    # 「面议 / 电议 / 私聊 / 咨询」等无法定价的文案直接放弃
    if any(word in s for word in _PRICE_BLOCK_WORDS):
        return None
    multiplier = 1
    if "万" in s:
        multiplier = 10000
        s = s.replace("万", "")
    match = _PRICE_PATTERN.search(s)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "")) * multiplier
    except ValueError:  # pragma: no cover - 正则已保证可转换
        return None


def parse_publish_time(text: str) -> str:
    """尽力从文本中提取发布时间文案。

    Args:
        text: 商品卡片纯文本。

    Returns:
        发布时间文案；提取不到时返回空串。
    """
    if not text:
        return ""
    match = _TIME_PATTERN.search(text)
    return match.group(1).strip() if match else ""

