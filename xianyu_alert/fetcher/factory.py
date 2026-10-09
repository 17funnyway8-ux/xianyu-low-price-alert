"""build_fetcher：按配置选择抓取器实现。"""

from __future__ import annotations

import logging

from ..config import Config
from ..parsing import (  # noqa: F401 - 再导出：既有调用点与测试无需改动
    extract_product_id,
    parse_price,
    parse_publish_time,
)
from .base import (
    Fetcher,
)
from .mock import (
    MockFetcher,
)
from .mtop import (
    MtopFetcher,
)
from .web import (
    WebFetcher,
)

logger = logging.getLogger(__name__)

# 闲鱼 Web 站点
def build_fetcher(config: Config) -> Fetcher:
    """根据配置构建抓取器。

    多 Cookie 池（v3.2）：mtop 分支初始 Cookie 按「池优先、单值兜底」
    策略解析——池中启用条目非空取第 0 条，否则回退 `monitor.cookies`。
    轮换由 monitor 每轮调用 `fetcher.set_cookies()` 完成，这里只负责首轮。

    Args:
        config: 全局配置。

    Returns:
        Fetcher 实例：
            fetcher.type == "mtop" -> MtopFetcher（真实抓取，推荐）
            fetcher.type == "mock" -> MockFetcher（离线演示）
            其它（"web"）          -> WebFetcher（旧版 HTML 解析，已废弃）
    """
    ftype = config.fetcher.type
    if ftype == "mock":
        return MockFetcher(
            products_per_round=config.fetcher.mock_products_per_round,
            fail_rounds=config.fetcher.mock_fail_rounds,
        )
    if ftype == "mtop":
        from ..cookie import resolve_cookie_for_round

        return MtopFetcher(
            cookies=resolve_cookie_for_round(config.monitor, 0),
            user_agent=config.monitor.user_agent,
            page_size=config.fetcher.page_size,
            pages=config.fetcher.pages,
            page_sleep=config.fetcher.page_sleep,
        )
    return WebFetcher(
        user_agent=config.monitor.user_agent,
        cookies=config.monitor.cookies,
    )

__all__ = [
    "build_fetcher",
]
