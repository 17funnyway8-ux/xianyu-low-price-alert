"""WebFetcher：网页兜底采集（解析逻辑在 web_parse.py）。"""

from __future__ import annotations

import contextlib
import logging
import time
from urllib.parse import quote_plus

import requests

from .. import web_parse
from ..config import DEFAULT_USER_AGENT
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
    BASE_URL,
    SEARCH_URL_TEMPLATE,
)

logger = logging.getLogger(__name__)

# 闲鱼 Web 站点
class WebFetcher(Fetcher):
    """抓取闲鱼（goofish.com）网页搜索结果。

    Attributes:
        user_agent: 请求 UA。
        cookies: 原始 Cookie 字符串（形如 `k1=v1; k2=v2`），用于携带登录态。
        timeout: 单次请求超时（秒）。
        retries: 失败重试次数（指数退避）。
    """

    name = "web"

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        cookies: str = "",
        timeout: float = 10.0,
        retries: int = 3,
        backoff_base: float = 1.5,
        session: requests.Session | None = None,
    ) -> None:
        """初始化 Web 抓取器。

        Args:
            user_agent: 浏览器 UA。
            cookies: Cookie 字符串，可为空。
            timeout: 请求超时秒数。
            retries: 总尝试次数（>=1）。
            backoff_base: 指数退避基数，第 n 次失败后 sleep backoff_base**n 秒。
            session: 可注入的 requests.Session（便于测试）。
        """
        self.user_agent: str = user_agent or DEFAULT_USER_AGENT
        self.cookies: str = cookies or ""
        self.timeout: float = float(timeout)
        self.retries: int = max(1, int(retries))
        self.backoff_base: float = float(backoff_base)
        self.session: requests.Session = session or requests.Session()

    def set_cookies(self, cookie_str: str) -> None:
        """轮换时替换 Cookie（v3.2 多 Cookie 池；WebFetcher 已废弃仍保持兼容）。"""
        self.cookies = str(cookie_str or "")

    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        """构造请求头。"""
        headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": BASE_URL + "/",
            "Connection": "keep-alive",
        }
        if self.cookies:
            headers["Cookie"] = self.cookies
        return headers

    def _request(self, url: str) -> str:
        """带重试的 GET 请求。

        Args:
            url: 目标 URL。

        Returns:
            响应文本。

        Raises:
            FetchError: 重试耗尽仍失败。
        """
        last_error: BaseException | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, headers=self._headers(), timeout=self.timeout)
                if response.status_code != 200:
                    raise FetchError(f"HTTP {response.status_code}")
                return response.text
            except Exception as exc:  # noqa: BLE001 - 统一转换为 FetchError
                last_error = exc
                if attempt < self.retries:
                    delay = self.backoff_base ** attempt
                    logger.warning(
                        "[web] 请求失败（第 %d/%d 次）：%s，%.1fs 后重试",
                        attempt, self.retries, exc, delay,
                    )
                    time.sleep(delay)
        raise FetchError(f"请求 {url} 失败，已重试 {self.retries} 次：{last_error}")

    def close(self) -> None:
        """关闭内部 session（覆盖基类的 no-op）。

        v1.8.1 补充：WebFetcher 在 __init__ 里自建 requests.Session，
        但原先没有覆盖 close() —— 基类 close 是空实现，于是每轮 run_once
        都会泄漏一个持有 keep-alive 连接的会话。
        """
        with contextlib.suppress(Exception): # 关闭失败不影响主流程
            self.session.close()

    # ------------------------------------------------------------------ #
    def fetch(self, keyword: str) -> list[Product]:
        """抓取指定关键词的商品列表。

        Args:
            keyword: 搜索关键词。

        Returns:
            Product 列表；页面能取到但解析不出商品时返回空列表。

        Raises:
            FetchError: 网络请求失败。
        """
        url = SEARCH_URL_TEMPLATE.format(keyword=quote_plus(keyword))
        logger.info("[web] 抓取关键词 %s -> %s", keyword, url)
        html = self._request(url)

        products = self.parse(html, keyword)
        if not products:
            logger.warning(
                "[web] 关键词「%s」未解析到任何商品。"
                "闲鱼为 JS 渲染 + 强反爬站点，纯 HTTP 抓取常常拿不到数据，"
                "请检查 monitor.cookies 是否配置了有效登录态，或改用 fetcher.type=mock 演示。",
                keyword,
            )
        return products

    # ------------------------------------------------------------------ #
    def parse(self, html: str, keyword: str) -> list[Product]:
        """解析搜索结果页 HTML。

        解析策略（两级兜底）：
            1. 优先从内联脚本里的 JSON（`window.__INIT_DATA__` 之类）提取商品；
            2. 回退到 BeautifulSoup 遍历带商品链接的 <a> 卡片。

        Args:
            html: 页面 HTML。
            keyword: 当前关键词（写入 Product.keyword）。

        Returns:
            去重后的 Product 列表。
        """
        if not html:
            return []

        products, report = web_parse.parse_search_html(html, keyword)
        if products:
            logger.debug('[web] %s', report.summary())
        else:
            # 0 条是最需要排障的时刻：把"扫描了多少、为何被跳过"和排查建议一并打出来
            logger.warning('[web] %s | %s', report.summary(), report.hint())
        return products

# ---------------------------------------------------------------------- #
# Mock 抓取器
# ---------------------------------------------------------------------- #

__all__ = [
    "WebFetcher",
]
