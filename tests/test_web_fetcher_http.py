"""WebFetcher 的 HTTP 路径测试（v1.10.5，M02 补强）。

重评时发现：`xianyu_alert/fetcher/web.py` 覆盖率只有 **56%**（web_parse 有 89%），
缺口在**网络层**：重试循环、非 200、异常退化、会话关闭都几乎没有断言。

这些路径恰恰是最容易在真实环境出问题的地方（超时、被限流、连接泄漏），
因此本文件用**注入的假 session** 全部离线覆盖，不依赖网络与真实等待。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.fetcher import FetchError  # noqa: E402
from xianyu_alert.fetcher.web import WebFetcher  # noqa: E402


class FakeResponse:
    """requests.Response 的最小替身。"""

    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class FakeSession:
    """记录调用并按脚本返回/抛错的假 session。"""

    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.closed = 0
        self.headers_seen: list[dict] = []

    def get(self, url, headers=None, timeout=None):  # noqa: ANN001, ANN003
        self.calls.append(url)
        self.headers_seen.append(dict(headers or {}))
        item = self.results.pop(0) if self.results else FakeResponse(200, "")
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed += 1


def make_fetcher(results: list, **kwargs) -> tuple[WebFetcher, FakeSession]:
    """构造带假 session 的 WebFetcher（backoff=0 -> 测试不真的等待）。"""
    session = FakeSession(results)
    fetcher = WebFetcher(session=session, retries=kwargs.pop("retries", 3), backoff_base=0.0, **kwargs)
    return fetcher, session


class TestRequestSuccess(unittest.TestCase):
    """正常路径。"""

    def test_returns_text_on_200(self) -> None:
        fetcher, session = make_fetcher([FakeResponse(200, "<html>ok</html>")])
        self.assertEqual(fetcher._request("https://x"), "<html>ok</html>")
        self.assertEqual(len(session.calls), 1)

    def test_passes_timeout_and_headers(self) -> None:
        fetcher, session = make_fetcher([FakeResponse(200, "x")], cookies="cookie2=abc", timeout=7.5)
        fetcher._request("https://x")
        self.assertIn("Cookie", session.headers_seen[0])
        self.assertEqual(session.headers_seen[0]["Cookie"], "cookie2=abc")


class TestRequestFailure(unittest.TestCase):
    """失败与重试。"""

    def test_non_200_retries_then_raises(self) -> None:
        fetcher, session = make_fetcher([FakeResponse(404, "nope")] * 3)
        with self.assertRaises(FetchError) as ctx:
            fetcher._request("https://x")
        self.assertEqual(len(session.calls), 3, "应按 retries 次尝试")
        self.assertIn("404", str(ctx.exception))

    def test_transient_error_then_success(self) -> None:
        fetcher, session = make_fetcher([ConnectionError("超时"), FakeResponse(200, "ok")])
        self.assertEqual(fetcher._request("https://x"), "ok")
        self.assertEqual(len(session.calls), 2, "第二次应成功")

    def test_single_retry_does_not_retry(self) -> None:
        fetcher, session = make_fetcher([FakeResponse(500, "")], retries=1)
        with self.assertRaises(FetchError):
            fetcher._request("https://x")
        self.assertEqual(len(session.calls), 1)

    def test_exhausted_retries_message_mentions_attempts(self) -> None:
        fetcher, _session = make_fetcher([ConnectionError("boom")] * 2, retries=2)
        with self.assertRaises(FetchError) as ctx:
            fetcher._request("https://x")
        self.assertIn("2", str(ctx.exception))


class TestFetch(unittest.TestCase):
    """fetch() 的 URL 组装与空结果处理。"""

    def test_builds_search_url_with_quoted_keyword(self) -> None:
        fetcher, session = make_fetcher([FakeResponse(200, "<html></html>")])
        fetcher.fetch("Switch OLED")
        self.assertTrue(session.calls[0].startswith("https://"))
        self.assertIn("Switch+OLED", session.calls[0], "关键词需要 URL 编码")

    def test_unparsable_page_returns_empty_and_warns(self) -> None:
        fetcher, _session = make_fetcher([FakeResponse(200, "<html><body>请登录</body></html>")])
        with self.assertLogs("xianyu_alert.fetcher.web", level="WARNING") as logs:
            products = fetcher.fetch("Switch")
        self.assertEqual(products, [])
        self.assertTrue(any("未解析到任何商品" in line for line in logs.output))

    def test_parsable_page_returns_products(self) -> None:
        html = (
            '<html><body><a href="/item?id=810000000001" title="商品A">'
            '<span class="price">129</span></a></body></html>'
        )
        fetcher, _session = make_fetcher([FakeResponse(200, html)])
        products = fetcher.fetch("Switch")
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].product_id, "810000000001")

    def test_http_failure_propagates_as_fetch_error(self) -> None:
        fetcher, _session = make_fetcher([FakeResponse(403, "forbidden")], retries=1)
        with self.assertRaises(FetchError):
            fetcher.fetch("Switch")


class TestSessionLifecycle(unittest.TestCase):
    """会话关闭（v1.8.1 修过的连接泄漏点）。"""

    def test_close_closes_session(self) -> None:
        fetcher, session = make_fetcher([])
        fetcher.close()
        self.assertEqual(session.closed, 1)

    def test_close_tolerates_exception(self) -> None:
        class Boom(FakeSession):
            def close(self) -> None:
                raise RuntimeError("already closed")

        fetcher = WebFetcher(session=Boom([]), backoff_base=0.0)
        try:
            fetcher.close()
        except Exception as exc:  # pragma: no cover - 不应发生
            self.fail(f"close 不应抛异常：{exc}")

    def test_set_cookies_reflected_in_headers(self) -> None:
        fetcher, session = make_fetcher([FakeResponse(200, "x")])
        fetcher.set_cookies("cookie2=xyz; unb=1")
        fetcher._request("https://x")
        self.assertIn("cookie2=xyz", session.headers_seen[0]["Cookie"])


if __name__ == "__main__":
    unittest.main()
