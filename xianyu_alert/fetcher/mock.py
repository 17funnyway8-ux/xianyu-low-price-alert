"""MockFetcher：确定性伪造数据，供测试与离线演示。"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from ..models import Product
from ..parsing import (  # noqa: F401 - 再导出：既有调用点与测试无需改动
    extract_product_id,
    parse_price,
    parse_publish_time,
)
from .base import (
    Fetcher,
    FetchError,
)
from .constants import (
    ITEM_URL_TEMPLATE,
)

logger = logging.getLogger(__name__)

# 闲鱼 Web 站点
class MockFetcher(Fetcher):
    """确定性伪造抓取器，用于单元测试与离线演示。

    生成规则（保证可复现）：
        - 每个关键词维护独立的轮次计数 round_no（从 1 开始）；
        - 第 r 轮返回商品索引窗口 [r-1, r-1+products_per_round)，
          因此相邻两轮会有 products_per_round-1 个重叠商品，
          可用于验证「上一轮已出现的商品不算新」；
        - 商品索引 i 为 3 的倍数时生成「低价商品」（10~199 元），
          否则生成「高价商品」（2000~5000 元），
          从而稳定产出可触发阈值与不可触发阈值两类样本；
        - 所有随机数用 `关键词#索引` 作种子，结果完全确定。

    Attributes:
        products_per_round: 每轮返回的商品数量。
        fail_rounds: 需要抛出 FetchError 的轮次编号集合（从 1 开始）。
    """

    name = "mock"

    #: 低价商品价格区间
    CHEAP_RANGE = (10.0, 199.0)
    #: 高价商品价格区间
    EXPENSIVE_RANGE = (2000.0, 5000.0)

    def __init__(
        self,
        products_per_round: int = 5,
        fail_rounds: Sequence[int] | None = None,
        round_provider: Any | None = None,
    ) -> None:
        """初始化 Mock 抓取器。

        Args:
            products_per_round: 每轮生成的商品数量（>=1）。
            fail_rounds: 抛出 FetchError 的轮次列表，例如 [2] 表示第 2 轮失败。
            round_provider: 可选 callable，签名 `(keyword: str) -> int`，
                用于外部注入轮次；不传则内部自增计数。
        """
        self.products_per_round: int = max(1, int(products_per_round))
        self.fail_rounds: set = {int(x) for x in (fail_rounds or [])}
        self.round_provider = round_provider
        self._rounds: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    def current_round(self, keyword: str) -> int:
        """返回某关键词当前已进行的轮次（尚未 fetch 时为 0）。"""
        return self._rounds.get(keyword, 0)

    def reset(self) -> None:
        """重置所有关键词的轮次计数。"""
        self._rounds.clear()

    def _next_round(self, keyword: str) -> int:
        """推进并返回下一轮次编号。"""
        if self.round_provider is not None:
            return int(self.round_provider(keyword))
        self._rounds[keyword] = self._rounds.get(keyword, 0) + 1
        return self._rounds[keyword]

    # ------------------------------------------------------------------ #
    def make_product(self, keyword: str, index: int) -> Product:
        """根据 (关键词, 索引) 确定性地生成一个商品。

        Args:
            keyword: 关键词。
            index: 全局商品索引（>=0）。

        Returns:
            确定性生成的 Product。
        """
        rnd = random.Random(f"{keyword}#{index}")
        is_cheap = index % 3 == 0
        low, high = self.CHEAP_RANGE if is_cheap else self.EXPENSIVE_RANGE
        price = round(rnd.uniform(low, high), 2)

        product_id = str(1000000 + rnd.randrange(0, 899999) + index)
        suffix = "捡漏特价" if is_cheap else "个人闲置"
        title = f"{keyword} {suffix} 第{index}号 九成新"
        publish_time = (datetime(2024, 1, 1, 12, 0, 0) + timedelta(minutes=index * 7)).strftime(
            "%Y-%m-%d %H:%M"
        )
        return Product(
            product_id=product_id,
            title=title,
            price=price,
            url=ITEM_URL_TEMPLATE.format(product_id=product_id),
            publish_time=publish_time,
            keyword=keyword,
        )

    def fetch(self, keyword: str) -> list[Product]:
        """生成本轮的伪造商品列表。

        Args:
            keyword: 搜索关键词。

        Returns:
            本轮商品列表（按索引升序）。

        Raises:
            FetchError: 当前轮次在 fail_rounds 中，用于模拟抓取失败。
        """
        round_no = self._next_round(keyword)
        if round_no in self.fail_rounds:
            raise FetchError(f"[mock] 模拟第 {round_no} 轮抓取失败（关键词：{keyword}）")

        start = round_no - 1
        products = [
            self.make_product(keyword, index)
            for index in range(start, start + self.products_per_round)
        ]
        logger.info("[mock] 关键词「%s」第 %d 轮生成 %d 个商品", keyword, round_no, len(products))
        return products


# ---------------------------------------------------------------------- #
# 工厂
# ---------------------------------------------------------------------- #

__all__ = [
    "MockFetcher",
]
