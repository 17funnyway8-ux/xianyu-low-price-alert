"""抓取器基类与异常：Fetcher ABC / FetchError（v1.9.9 从 fetcher.py 拆出）。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..models import Product
from ..parsing import (  # noqa: F401 - 再导出：既有调用点与测试无需改动
    extract_product_id,
    parse_price,
    parse_publish_time,
)

logger = logging.getLogger(__name__)

# 闲鱼 Web 站点
class FetchError(RuntimeError):
    """抓取失败时抛出，并携带**失败层次**分类。

    Attributes:
        kind: 失败层次，决定「该怎么修」——
            - ``token``   : 令牌层（`_m_h5_tk` 过期）。本应自动续期重试，
              连续失败才说明登录态也失效了。
            - ``session`` : 会话层（登录态失效）。**必须**重新登录。
            - ``risk``    : 风控层（被识别为异常流量）。重新登录**无效**，
              只能降速 / 换账号 / 换出口网络。
            - ``config``  : 配置层（Cookie 缺失或不含 `_m_h5_tk`）。
            - ``network`` : 网络与协议层（超时、HTTP 非 200、响应非 JSON）。
            - ``unknown`` : 业务返回码无法归类。

    为什么要分类（2026-09-24 实测）：三种"失效"的修复手段完全不同，
    而真实场景中它们会**同时出现** —— 同一份 Cookie 打两个接口，
    搜索接口报 `RGV587_ERROR`（风控）、详情接口报 `FAIL_SYS_TOKEN_EXOIRED`（令牌）。
    笼统地提示「请重新登录」会把排查方向带偏（风控时重登根本没用）。
    """

    def __init__(self, message: str = "", kind: str = "network") -> None:
        super().__init__(message)
        self.kind: str = str(kind or "network")


# ---------------------------------------------------------------------- #
# 抽象基类
# ---------------------------------------------------------------------- #
class Fetcher(ABC):
    """抓取器抽象基类。"""

    #: 抓取器名称，用于日志
    name: str = "fetcher"

    @abstractmethod
    def fetch(self, keyword: str) -> list[Product]:
        """按关键词抓取最新商品列表。

        Args:
            keyword: 搜索关键词。

        Returns:
            Product 列表（可能为空列表）。

        Raises:
            FetchError: 抓取过程发生不可恢复的错误。
        """
        raise NotImplementedError

    def close(self) -> None:
        """释放资源（默认无操作，子类可覆盖）。"""
        return

    def set_cookies(self, cookie_str: str) -> None:
        """切换抓取器使用的 Cookie（v3.2 多 Cookie 池轮换）。

        默认实现为无操作；需要跟随轮换更新请求态的抓取器
        （MtopFetcher / WebFetcher）应覆盖此方法。
        fetcher 契约保持「单 Cookie」不变，轮换由 monitor 层
        每轮挑选后调用本方法注入，避免改动 fetch() 签名。

        Args:
            cookie_str: 新的 Cookie 请求头字符串（可为空串）。
        """
        return

    def set_max_price(self, max_price: float | None) -> None:
        """设置抓取时的价格上限（v3.4 服务端价格筛选）。

        默认实现为无操作；MtopFetcher 覆盖此方法把阈值写入请求体，
        使接口服务端直接按 `priceRange:0,{max_price};` 筛选，与
        闲鱼网页「最新发布 + 价格<360」的行为一致。
        fetcher 契约保持 `fetch(keyword)` 不变，阈值由 monitor 层
        每轮调用本方法注入，避免改动 fetch() 签名。

        Args:
            max_price: 价格上限（元）；None 表示不过滤价格。
        """
        return


# ---------------------------------------------------------------------- #
# 工具函数
# ---------------------------------------------------------------------- #

# ---------------------------------------------------------------------- #
# mtop 工具函数（纯函数，便于单测）
# ---------------------------------------------------------------------- #

__all__ = [
    "FetchError",
    "Fetcher",
]
