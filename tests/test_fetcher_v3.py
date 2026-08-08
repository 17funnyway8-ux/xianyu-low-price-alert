"""fetcher v3 增强测试：parse_price 增强、mtop 多页抓取、翻页限速、页级容错、Cookie 过期检测。

全部使用 mock（FakeSession + sleep_func 注入），**不访问真实网络**。
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import config_from_dict  # noqa: E402
from xianyu_alert.cookie import cookie_expiry_status, cookie_token_timestamp  # noqa: E402
from xianyu_alert.fetcher import (  # noqa: E402
    PAGE_SLEEP,
    FetchError,
    MtopFetcher,
    build_fetcher,
    parse_price,
)

VALID_COOKIE = "cookie2=abc; _m_h5_tk=deadbeefcafe_1700000000000; _m_h5_tk_enc=xyz"
#: 2023-11-14 签发 → 早已过期
EXPIRED_COOKIE = "a=1; _m_h5_tk=abc_1700000000000"


# ---------------------------------------------------------------------- #
# 测试替身（与 test_mtop_fetcher 同款）
# ---------------------------------------------------------------------- #
class FakeCookieJar:
    """极简 cookie jar 替身。"""

    def __init__(self, initial: Optional[Dict[str, str]] = None) -> None:
        self._data: Dict[str, str] = dict(initial or {})

    def set(self, name: str, value: str, **_kwargs: Any) -> None:
        self._data[name] = value

    def get(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default)


class FakeResponse:
    """requests 响应替身。"""

    def __init__(self, payload: Any, status_code: int = 200, set_cookie_token: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.cookies = FakeCookieJar({"_m_h5_tk": set_cookie_token} if set_cookie_token else {})
        self.headers: Dict[str, str] = {}
        if set_cookie_token:
            self.headers["Set-Cookie"] = f"_m_h5_tk={set_cookie_token}; Path=/"

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """requests.Session 替身，按脚本依次返回响应。"""

    def __init__(self, script: List[Any]) -> None:
        self.script: List[Any] = list(script)
        self.calls: List[Dict[str, Any]] = []
        self.cookies = FakeCookieJar()
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if not self.script:
            raise AssertionError("FakeSession 脚本已耗尽")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def make_item(item_id: Any = "800123456789", title: Any = "任天堂 Switch", price: Any = "888.00") -> Dict[str, Any]:
    """构造一个符合闲鱼实测结构的 resultList 元素。"""
    ex_content: Dict[str, Any] = {"title": title, "area": "广东 深圳"}
    args: Dict[str, Any] = {"item_id": item_id, "price": price, "publishTime": "1700000000000"}
    return {"data": {"item": {"main": {"exContent": ex_content, "clickParam": {"args": args}}}}}


def success_response(items: List[Dict[str, Any]], set_cookie_token: str = "") -> FakeResponse:
    """构造一个成功的 mtop 响应。"""
    return FakeResponse(
        {"ret": ["SUCCESS::调用成功"], "data": {"resultList": items}},
        set_cookie_token=set_cookie_token,
    )


def make_fetcher(script: List[Any], cookies: str = VALID_COOKIE, **kwargs: Any) -> MtopFetcher:
    """构造一个注入了 FakeSession 的 MtopFetcher。"""
    session = FakeSession(script)
    kwargs.setdefault("sleep_func", lambda _seconds: None)
    return MtopFetcher(
        cookies=cookies,
        session=session,
        **kwargs,
    )


# ---------------------------------------------------------------------- #
# 1. parse_price 增强
# ---------------------------------------------------------------------- #
class TestParsePriceV3(unittest.TestCase):
    """parse_price：万换算 / 关键词过滤 / 旧格式兼容。"""

    def test_wan_conversion(self) -> None:
        self.assertAlmostEqual(parse_price("1.2万"), 12000.0)
        self.assertAlmostEqual(parse_price("3.5万"), 35000.0)
        self.assertAlmostEqual(parse_price("1万"), 10000.0)
        self.assertAlmostEqual(parse_price("￥2.8万"), 28000.0)

    def test_keyword_filter(self) -> None:
        for bad in ("面议", "电议", "私聊", "咨询", "价格面议", "电议 999"):
            self.assertIsNone(parse_price(bad), f"{bad} 应被过滤为 None")

    def test_legacy_formats_still_parse(self) -> None:
        """旧格式必须保持可解析（回归红线）。"""
        self.assertAlmostEqual(parse_price("¥1,299.00"), 1299.0)
        self.assertAlmostEqual(parse_price("1299"), 1299.0)
        self.assertAlmostEqual(parse_price("1299.5"), 1299.5)
        self.assertAlmostEqual(parse_price("￥ 1,299.50"), 1299.5)

    def test_empty_and_garbage(self) -> None:
        self.assertIsNone(parse_price(""))
        self.assertIsNone(parse_price(None))
        self.assertIsNone(parse_price("没有任何数字"))
        self.assertIsNone(parse_price("万"))  # 只有「万」没有数字


# ---------------------------------------------------------------------- #
# 2. mtop 多页抓取
# ---------------------------------------------------------------------- #
class TestMtopMultiPage(unittest.TestCase):
    """多页抓取 + 翻页限速 + 页级容错。"""

    def test_pages_default_one(self) -> None:
        """默认 pages=1 不改变现状。"""
        self.assertEqual(MtopFetcher(cookies=VALID_COOKIE).pages, 1)
        self.assertAlmostEqual(MtopFetcher(cookies=VALID_COOKIE).page_sleep, PAGE_SLEEP)

    def test_pages_clamped_min_one(self) -> None:
        self.assertEqual(MtopFetcher(cookies=VALID_COOKIE, pages=0).pages, 1)
        self.assertEqual(MtopFetcher(cookies=VALID_COOKIE, pages=-3).pages, 1)

    def test_two_pages_merged_and_deduped(self) -> None:
        """pages=2 时 _search 调两次、页间 sleep 一次、结果合并去重、页码递增。"""
        page1 = [make_item(item_id="1001"), make_item(item_id="1002")]
        page2 = [make_item(item_id="1002"), make_item(item_id="1003")]
        sleeps: List[float] = []
        fetcher = make_fetcher(
            [success_response(page1), success_response(page2)],
            pages=2,
            sleep_func=lambda s: sleeps.append(s),
        )
        products = fetcher.fetch("Switch")
        self.assertEqual([p.product_id for p in products], ["1001", "1002", "1003"])
        self.assertEqual(len(fetcher.session.calls), 2)  # type: ignore[attr-defined]
        self.assertEqual(sleeps, [PAGE_SLEEP], "两页之间应 sleep 一次且时长为 PAGE_SLEEP")
        self.assertEqual(json.loads(fetcher.session.calls[0]["data"]["data"])["pageNumber"], 1)  # type: ignore[attr-defined]
        self.assertEqual(json.loads(fetcher.session.calls[1]["data"]["data"])["pageNumber"], 2)  # type: ignore[attr-defined]

    def test_single_page_success_no_extra_requests(self) -> None:
        fetcher = make_fetcher([success_response([make_item(item_id="1500")])])
        products = fetcher.fetch("Switch")
        self.assertEqual(len(products), 1)
        self.assertEqual(len(fetcher.session.calls), 1)  # type: ignore[attr-defined]

    def test_single_page_failure_keeps_first_page(self) -> None:
        """第 2 页失败 → 保留第 1 页结果并 warning，不丢整轮。"""
        page1 = [make_item(item_id="2001")]
        fetcher = make_fetcher(
            [success_response(page1), OSError("page2 down")],
            pages=2, retries=1,
            sleep_func=lambda _s: None,
        )
        with self.assertLogs("xianyu_alert.fetcher", level="WARNING"):
            products = fetcher.fetch("Switch")
        self.assertEqual([p.product_id for p in products], ["2001"])
        self.assertEqual(len(fetcher.session.calls), 2)  # type: ignore[attr-defined]

    def test_fetch_error_on_second_page_tolerated(self) -> None:
        """第 2 页命中风控（FetchError）→ 仍保留第 1 页结果。"""
        page1 = [make_item(item_id="3001")]
        risk = FakeResponse({"ret": ["RGV587_ERROR::SM::被挤爆"]})
        fetcher = make_fetcher(
            [success_response(page1), risk],
            pages=2, retries=1,
            sleep_func=lambda _s: None,
        )
        with self.assertLogs("xianyu_alert.fetcher", level="WARNING"):
            products = fetcher.fetch("Switch")
        self.assertEqual([p.product_id for p in products], ["3001"])

    def test_all_pages_fail_raises(self) -> None:
        """全部页失败 → 抛 FetchError（含最后错误详情）。"""
        fetcher = make_fetcher(
            [OSError("down1"), OSError("down2")],
            pages=2, retries=1,
            sleep_func=lambda _s: None,
        )
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        self.assertIn("全部", str(ctx.exception))
        self.assertIn("down2", str(ctx.exception))

    def test_single_page_fail_raises_with_detail(self) -> None:
        """pages=1 时失败消息保留原错误详情（回归既有测试断言）。"""
        fetcher = make_fetcher([OSError("connection reset")], retries=1)
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        self.assertIn("connection reset", str(ctx.exception))

    def test_build_fetcher_passes_pages(self) -> None:
        config = config_from_dict({
            "keywords": [{"keyword": "Switch", "max_price": 800}],
            "monitor": {"interval_seconds": 300, "cookies": VALID_COOKIE},
            "fetcher": {"type": "mtop", "pages": 3, "page_sleep": 1.5},
        })
        fetcher = build_fetcher(config)
        self.assertEqual(fetcher.pages, 3)
        self.assertAlmostEqual(fetcher.page_sleep, 1.5)
        fetcher.close()

    def test_config_rejects_non_positive_pages(self) -> None:
        from xianyu_alert.config import ConfigError

        with self.assertRaises(ConfigError):
            config_from_dict({
                "keywords": [{"keyword": "Switch", "max_price": 800}],
                "fetcher": {"type": "mtop", "pages": 0},
            })


# ---------------------------------------------------------------------- #
# 3. Cookie 过期检测
# ---------------------------------------------------------------------- #
class TestCookieExpiry(unittest.TestCase):
    """cookie_token_timestamp / cookie_expiry_status / check_cookie_health。"""

    def test_token_timestamp_parsed(self) -> None:
        self.assertEqual(
            cookie_token_timestamp("a=1; _m_h5_tk=abc_1785488087003; b=2"),
            1785488087003,
        )

    def test_token_timestamp_missing(self) -> None:
        self.assertIsNone(cookie_token_timestamp("a=1; b=2"))
        self.assertIsNone(cookie_token_timestamp("_m_h5_tk=abc_12345"))  # 非 13 位
        self.assertIsNone(cookie_token_timestamp("_m_h5_tk=no_underscore"))
        self.assertIsNone(cookie_token_timestamp(""))

    def test_expiry_status_states(self) -> None:
        now = 1785488087003
        expired_ts = now - 25 * 3600 * 1000        # 25 小时前签发 → 已过期
        expiring_ts = now - 23 * 3600 * 1000 - 30 * 60 * 1000  # 23.5h 前签发 → 30 分钟后过期
        ok_ts = now - 10 * 3600 * 1000              # 10 小时前签发 → 14 小时后过期（正常）
        self.assertEqual(cookie_expiry_status(f"_m_h5_tk=x_{expired_ts}", now_ms=now), "expired")
        self.assertEqual(cookie_expiry_status(f"_m_h5_tk=x_{expiring_ts}", now_ms=now), "expiring")
        self.assertEqual(cookie_expiry_status(f"_m_h5_tk=x_{ok_ts}", now_ms=now), "ok")

    def test_expiry_status_edge_cases(self) -> None:
        self.assertEqual(cookie_expiry_status(""), "missing")
        self.assertEqual(cookie_expiry_status("a=1"), "no_token")
        self.assertEqual(cookie_expiry_status("_m_h5_tk=abc_170000"), "unknown")

    def test_check_cookie_health_expired(self) -> None:
        fetcher = make_fetcher([])
        ok, reason = fetcher.check_cookie_health()
        self.assertFalse(ok)  # VALID_COOKIE 时间戳 1700000000000 早已过期
        self.assertIn("过期", reason)

    def test_check_cookie_health_ok(self) -> None:
        future = int(time.time() * 1000) + 10 * 3600 * 1000
        cookie = f"a=1; _m_h5_tk=abc_{future}"
        fetcher = make_fetcher([], cookies=cookie)
        ok, _reason = fetcher.check_cookie_health()
        self.assertTrue(ok)

    def test_fetch_expired_cookie_still_works(self) -> None:
        """过期只 warning 不阻断（回归红线：既有行为不变）。"""
        fetcher = make_fetcher([success_response([make_item(item_id="4001")])])
        with self.assertLogs("xianyu_alert.fetcher", level="WARNING"):
            products = fetcher.fetch("Switch")
        self.assertEqual(len(products), 1)


# ---------------------------------------------------------------------- #
# 4. monitor 启动预检
# ---------------------------------------------------------------------- #
class TestMonitorPreflight(unittest.TestCase):
    """monitor.preflight_cookie 启动预检（过期 warning、mock 不提示）。"""

    def _monitor(self, fetcher_type: str, cookies: str):
        from xianyu_alert.fetcher import MockFetcher
        from xianyu_alert.monitor import Monitor
        from xianyu_alert.storage import Storage

        config = config_from_dict({
            "keywords": [{"keyword": "Switch", "max_price": 800}],
            "monitor": {"interval_seconds": 60, "cookies": cookies},
            "fetcher": {"type": fetcher_type},
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        })
        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        return Monitor(config, MockFetcher(), storage, [])

    def test_preflight_warns_expired(self) -> None:
        monitor = self._monitor("mtop", EXPIRED_COOKIE)
        with self.assertLogs("xianyu_alert.monitor", level="WARNING"):
            msg = monitor.preflight_cookie()
        self.assertIn("过期", msg)

    def test_preflight_warns_missing(self) -> None:
        monitor = self._monitor("mtop", "")
        with self.assertLogs("xianyu_alert.monitor", level="WARNING"):
            msg = monitor.preflight_cookie()
        self.assertIn("Cookie", msg)

    def test_preflight_empty_for_mock(self) -> None:
        monitor = self._monitor("mock", "")
        self.assertEqual(monitor.preflight_cookie(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
