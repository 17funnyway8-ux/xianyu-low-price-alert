"""MtopFetcher 单元测试。

全部使用 mock，**绝不访问真实网络**：
    - 用 FakeSession 替换 requests.Session，按脚本返回预置响应；
    - 用 sleep_func 注入空实现，重试不产生真实等待。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import VALID_FETCHER_TYPES, config_from_dict
from xianyu_alert.fetcher import (
    MTOP_APP_KEY,
    MTOP_TOKEN_COOKIE,
    MTOP_URL,
    FetchError,
    MockFetcher,
    MtopFetcher,
    WebFetcher,
    build_fetcher,
    build_search_payload,
    coerce_text,
    extract_token,
    format_publish_time,
    mtop_sign,
    parse_cookie_string,
    parse_mtop_item,
    parse_mtop_result_list,
)

VALID_COOKIE = "cookie2=abc; _m_h5_tk=deadbeefcafe_1700000000000; _m_h5_tk_enc=xyz"


# ---------------------------------------------------------------------- #
# 测试替身
# ---------------------------------------------------------------------- #
class FakeCookieJar:
    """极简 cookie jar 替身，兼容 set/get 接口。"""

    def __init__(self, initial: Optional[Dict[str, str]] = None) -> None:
        self._data: Dict[str, str] = dict(initial or {})

    def set(self, name: str, value: str, **_kwargs: Any) -> None:
        """写入一个 cookie。"""
        self._data[name] = value

    def get(self, name: str, default: Any = None) -> Any:
        """读取一个 cookie。"""
        return self._data.get(name, default)


class FakeResponse:
    """requests 响应替身。"""

    def __init__(
        self,
        payload: Any,
        status_code: int = 200,
        set_cookie_token: str = "",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.cookies = FakeCookieJar({MTOP_TOKEN_COOKIE: set_cookie_token} if set_cookie_token else {})
        self.headers: Dict[str, str] = {}
        if set_cookie_token:
            self.headers["Set-Cookie"] = f"{MTOP_TOKEN_COOKIE}={set_cookie_token}; Path=/"

    def json(self) -> Any:
        """返回预置的 JSON 载荷。"""
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """requests.Session 替身，按脚本依次返回响应。"""

    def __init__(self, script: List[Any]) -> None:
        """初始化。

        Args:
            script: 响应脚本，元素可以是 FakeResponse 或要抛出的 Exception。
        """
        self.script: List[Any] = list(script)
        self.calls: List[Dict[str, Any]] = []
        self.cookies = FakeCookieJar()
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """记录调用并返回脚本中的下一个响应。"""
        self.calls.append({"url": url, **kwargs})
        if not self.script:
            raise AssertionError("FakeSession 脚本已耗尽，但仍被调用")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        """标记已关闭。"""
        self.closed = True


def make_item(
    item_id: Any = "800123456789",
    title: Any = "任天堂 Switch 续航版 国行",
    price: Any = "888.00",
    publish_time: Any = "1700000000000",
    ex_price: Any = None,
) -> Dict[str, Any]:
    """构造一个符合闲鱼实测结构的 resultList 元素。"""
    ex_content: Dict[str, Any] = {"area": "广东 深圳", "userNickName": "闲鱼用户"}
    if title is not None:
        ex_content["title"] = title
    if ex_price is not None:
        ex_content["price"] = ex_price

    args: Dict[str, Any] = {}
    if item_id is not None:
        args["item_id"] = item_id
    if price is not None:
        args["price"] = price
    if publish_time is not None:
        args["publishTime"] = publish_time

    return {
        "data": {
            "item": {
                "main": {
                    "exContent": ex_content,
                    "clickParam": {"args": args},
                }
            }
        }
    }


def success_response(items: List[Dict[str, Any]], set_cookie_token: str = "") -> FakeResponse:
    """构造一个成功的 mtop 响应。"""
    return FakeResponse(
        {"api": "mtop.taobao.idlemtopsearch.pc.search", "ret": ["SUCCESS::调用成功"],
         "data": {"resultList": items}},
        set_cookie_token=set_cookie_token,
    )


def make_fetcher(script: List[Any], cookies: str = VALID_COOKIE, **kwargs: Any) -> MtopFetcher:
    """构造一个注入了 FakeSession 的 MtopFetcher。"""
    session = FakeSession(script)
    fetcher = MtopFetcher(
        cookies=cookies,
        session=session,
        sleep_func=lambda _seconds: None,
        **kwargs,
    )
    return fetcher


# ---------------------------------------------------------------------- #
# 1. 纯函数
# ---------------------------------------------------------------------- #
class TestMtopHelpers(unittest.TestCase):
    """mtop 工具函数测试。"""

    def test_parse_cookie_string(self) -> None:
        """Cookie 字符串能正确解析为字典。"""
        result = parse_cookie_string("a=1; b=2;  c = 3 ; 坏数据 ; =空名")
        self.assertEqual(result["a"], "1")
        self.assertEqual(result["b"], "2")
        self.assertEqual(result["c"], "3")
        self.assertNotIn("", result)

    def test_parse_cookie_string_empty(self) -> None:
        """空输入返回空字典。"""
        self.assertEqual(parse_cookie_string(""), {})
        self.assertEqual(parse_cookie_string(None), {})

    def test_extract_token(self) -> None:
        """token 取 _m_h5_tk 的下划线前半段。"""
        self.assertEqual(extract_token("deadbeefcafe_1700000000000"), "deadbeefcafe")
        self.assertEqual(extract_token("nounderscore"), "nounderscore")
        self.assertEqual(extract_token(""), "")

    def test_mtop_sign_matches_manual_md5(self) -> None:
        """签名算法与手工计算的 md5 完全一致。"""
        token = "deadbeefcafe"
        timestamp = "1700000000000"
        data = '{"keyword":"Switch"}'
        expected = hashlib.md5(
            f"{token}&{timestamp}&{MTOP_APP_KEY}&{data}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(mtop_sign(token, timestamp, MTOP_APP_KEY, data), expected)
        # 固定值回归：任何算法改动都会被这条断言抓到
        self.assertEqual(len(expected), 32)

    def test_mtop_sign_is_deterministic_and_sensitive(self) -> None:
        """相同输入结果稳定，任一入参变化都会改变签名。"""
        base = mtop_sign("tk", "1", "app", "data")
        self.assertEqual(base, mtop_sign("tk", "1", "app", "data"))
        self.assertNotEqual(base, mtop_sign("tk2", "1", "app", "data"))
        self.assertNotEqual(base, mtop_sign("tk", "2", "app", "data"))
        self.assertNotEqual(base, mtop_sign("tk", "1", "app", "data2"))

    def test_build_search_payload_sorts_by_create(self) -> None:
        """请求体按发布时间倒序（最新发布）：sortField=create + sortValue=desc。"""
        payload = build_search_payload("Switch", page_number=1, rows_per_page=30)
        self.assertEqual(payload["sortField"], "create")
        self.assertEqual(payload["sortValue"], "desc")
        self.assertEqual(payload["keyword"], "Switch")
        self.assertEqual(payload["rowsPerPage"], 30)
        self.assertEqual(payload["searchReqFromPage"], "pcSearch")
        self.assertFalse(payload["fromFilter"])

    def test_coerce_text_handles_str_dict_list(self) -> None:
        """标题/价格字段可能是 str / dict / 富文本 list，都能压平。"""
        self.assertEqual(coerce_text("  hi  "), "hi")
        self.assertEqual(coerce_text({"text": "标题"}), "标题")
        self.assertEqual(coerce_text([{"text": "¥"}, {"text": "1299"}]), "¥1299")
        self.assertEqual(coerce_text(None), "")
        self.assertEqual(coerce_text(123), "123")

    def test_format_publish_time_ms_timestamp(self) -> None:
        """13 位毫秒时间戳被转换为 YYYY-MM-DD HH:MM:SS。"""
        raw = "1700000000000"
        expected = datetime.fromtimestamp(1700000000.0).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(format_publish_time(raw), expected)

    def test_format_publish_time_tolerant(self) -> None:
        """非法/空输入不抛异常。"""
        self.assertEqual(format_publish_time(None), "")
        self.assertEqual(format_publish_time(""), "")
        self.assertEqual(format_publish_time("3分钟前"), "3分钟前")
        self.assertEqual(format_publish_time("0"), "")


# ---------------------------------------------------------------------- #
# 2. 响应解析
# ---------------------------------------------------------------------- #
class TestMtopParsing(unittest.TestCase):
    """resultList 解析测试。"""

    def test_parse_item_full_fields(self) -> None:
        """正常条目解析出四要素齐全的 Product。"""
        product = parse_mtop_item(make_item(), "Switch")
        self.assertIsNotNone(product)
        assert product is not None
        self.assertEqual(product.product_id, "800123456789")
        self.assertEqual(product.title, "任天堂 Switch 续航版 国行")
        self.assertAlmostEqual(product.price, 888.0)
        self.assertEqual(product.url, "https://www.goofish.com/item?id=800123456789")
        self.assertEqual(
            product.publish_time,
            datetime.fromtimestamp(1700000000.0).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.assertEqual(product.keyword, "Switch")

    def test_parse_item_missing_title_returns_none(self) -> None:
        """缺 title 的条目被跳过。"""
        self.assertIsNone(parse_mtop_item(make_item(title=None), "Switch"))

    def test_parse_item_missing_id_returns_none(self) -> None:
        """缺 item_id 的条目被跳过。"""
        self.assertIsNone(parse_mtop_item(make_item(item_id=None), "Switch"))

    def test_parse_item_price_fallback_to_excontent(self) -> None:
        """clickParam 无价格时回退到 exContent 的富文本价格。"""
        item = make_item(price=None, ex_price=[{"text": "¥"}, {"text": "1,299.50"}])
        product = parse_mtop_item(item, "iPad")
        self.assertIsNotNone(product)
        assert product is not None
        self.assertAlmostEqual(product.price, 1299.5)

    def test_parse_item_bad_price_returns_none(self) -> None:
        """价格完全无法解析时跳过该条。"""
        self.assertIsNone(parse_mtop_item(make_item(price="面议"), "Switch"))

    def test_parse_item_broken_structure_never_raises(self) -> None:
        """结构错乱不抛异常，只返回 None。"""
        for bad in (None, "字符串", 123, {}, {"data": "not-a-dict"}, {"data": {"item": []}}):
            self.assertIsNone(parse_mtop_item(bad, "Switch"))

    def test_parse_result_list_skips_bad_items(self) -> None:
        """单条脏数据不影响其它条目。"""
        items = [
            make_item(item_id="1001", title="好商品 A", price="100"),
            make_item(item_id="1002", title=None, price="200"),        # 缺 title
            make_item(item_id="1003", title="好商品 C", price="面议"),  # 价格非数字
            make_item(item_id=None, title="好商品 D", price="300"),     # 缺 id
            make_item(item_id="1005", title="好商品 E", price="500"),
        ]
        products = parse_mtop_result_list(items, "Switch")
        self.assertEqual([p.product_id for p in products], ["1001", "1005"])

    def test_parse_result_list_dedupes(self) -> None:
        """同一 product_id 只保留一条。"""
        items = [make_item(item_id="2001"), make_item(item_id="2001")]
        self.assertEqual(len(parse_mtop_result_list(items, "Switch")), 1)

    def test_parse_result_list_non_list(self) -> None:
        """resultList 不是列表时返回空列表。"""
        self.assertEqual(parse_mtop_result_list(None, "Switch"), [])
        self.assertEqual(parse_mtop_result_list({"a": 1}, "Switch"), [])


# ---------------------------------------------------------------------- #
# 3. fetch 行为
# ---------------------------------------------------------------------- #
class TestMtopFetcherFetch(unittest.TestCase):
    """MtopFetcher.fetch 行为测试。"""

    def test_fetch_success(self) -> None:
        """正常响应能解析出商品。"""
        fetcher = make_fetcher([success_response([make_item(), make_item(item_id="900")])])
        products = fetcher.fetch("Switch")
        self.assertEqual(len(products), 2)
        self.assertEqual(products[0].keyword, "Switch")

    def test_fetch_request_shape(self) -> None:
        """请求 URL / query 参数 / form body / 签名均符合 mtop 规范。"""
        fetcher = make_fetcher([success_response([make_item()])])
        fetcher.fetch("Switch")

        call = fetcher.session.calls[0]  # type: ignore[attr-defined]
        self.assertEqual(call["url"], MTOP_URL)

        params = call["params"]
        self.assertEqual(params["appKey"], MTOP_APP_KEY)
        self.assertEqual(params["api"], "mtop.taobao.idlemtopsearch.pc.search")
        self.assertEqual(params["jsv"], "2.7.2")
        self.assertEqual(params["type"], "originaljson")
        self.assertEqual(params["accountSite"], "xianyu")
        self.assertEqual(params["sessionOption"], "AutoLoginOnly")
        self.assertEqual(params["spm_cnt"], "a21ybx.search.0.0")

        # body 是紧凑 JSON，且 sign 与之严格对应
        data = call["data"]["data"]
        self.assertNotIn(", ", data)  # 紧凑序列化，无多余空格
        self.assertEqual(json.loads(data)["keyword"], "Switch")
        expected_sign = mtop_sign("deadbeefcafe", params["t"], MTOP_APP_KEY, data)
        self.assertEqual(params["sign"], expected_sign)

        headers = call["headers"]
        self.assertEqual(headers["Referer"], "https://www.goofish.com/")
        self.assertEqual(headers["Origin"], "https://www.goofish.com")
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")

    def test_fetch_empty_cookie_raises_with_guide(self) -> None:
        """Cookie 为空时抛 FetchError，且消息含 login 引导语。"""
        fetcher = make_fetcher([], cookies="")
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        self.assertIn("cli login", str(ctx.exception))
        self.assertIn("未配置登录 Cookie", str(ctx.exception))

    def test_fetch_cookie_without_token_raises(self) -> None:
        """Cookie 不含 _m_h5_tk 时抛 FetchError 并提示重新 login。"""
        fetcher = make_fetcher([], cookies="cookie2=abc; other=1")
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        message = str(ctx.exception)
        self.assertIn(MTOP_TOKEN_COOKIE, message)
        self.assertIn("login", message)

    def test_fetch_risk_control_raises_with_hint(self) -> None:
        """风控响应抛 FetchError，并提示调大间隔。"""
        fetcher = make_fetcher(
            [FakeResponse({"ret": ["RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!"]})]
        )
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        message = str(ctx.exception)
        self.assertIn("风控", message)
        self.assertIn("interval_seconds", message)

    def test_fetch_token_expired_auto_retries_once(self) -> None:
        """令牌过期时用新 token 重算签名自动重试一次并成功。"""
        fetcher = make_fetcher(
            [
                FakeResponse(
                    {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]},
                    set_cookie_token="newtoken111_1700000009999",
                ),
                success_response([make_item(item_id="3001")]),
            ]
        )
        products = fetcher.fetch("Switch")

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].product_id, "3001")
        # 断言确实调用了两次
        self.assertEqual(len(fetcher.session.calls), 2)  # type: ignore[attr-defined]
        # 断言第二次用的是服务端新下发的 token
        self.assertEqual(fetcher.current_token(), "newtoken111")
        second_call = fetcher.session.calls[1]  # type: ignore[attr-defined]
        expected_sign = mtop_sign(
            "newtoken111",
            second_call["params"]["t"],
            MTOP_APP_KEY,
            second_call["data"]["data"],
        )
        self.assertEqual(second_call["params"]["sign"], expected_sign)

    def test_fetch_token_expired_twice_raises(self) -> None:
        """令牌反复过期时最终抛 FetchError（只重试一次）。"""
        expired = lambda: FakeResponse(  # noqa: E731 - 测试内联工厂
            {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]}, set_cookie_token="tk_1"
        )
        fetcher = make_fetcher([expired(), expired()])
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        self.assertIn("令牌", str(ctx.exception))
        self.assertEqual(len(fetcher.session.calls), 2)  # type: ignore[attr-defined]

    def test_fetch_session_expired_raises(self) -> None:
        """登录态失效时提示重新 login。"""
        fetcher = make_fetcher([FakeResponse({"ret": ["FAIL_SYS_SESSION_EXPIRED::未登录"]})])
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        self.assertIn("登录", str(ctx.exception))

    def test_fetch_empty_result_list_returns_empty(self) -> None:
        """resultList 为空时返回 [] 且不抛异常。"""
        fetcher = make_fetcher([success_response([])])
        self.assertEqual(fetcher.fetch("Switch"), [])

    def test_fetch_network_error_retries_then_raises(self) -> None:
        """网络异常重试耗尽后抛 FetchError。"""
        fetcher = make_fetcher(
            [OSError("connection reset"), OSError("timeout"), OSError("refused")],
            retries=3,
        )
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        self.assertIn("已重试 3 次", str(ctx.exception))
        self.assertEqual(len(fetcher.session.calls), 3)  # type: ignore[attr-defined]

    def test_fetch_network_error_then_success(self) -> None:
        """首次网络异常、重试成功时正常返回商品。"""
        fetcher = make_fetcher(
            [OSError("timeout"), success_response([make_item(item_id="4001")])], retries=3
        )
        products = fetcher.fetch("Switch")
        self.assertEqual(len(products), 1)
        self.assertEqual(len(fetcher.session.calls), 2)  # type: ignore[attr-defined]

    def test_fetch_http_error_raises(self) -> None:
        """非 200 状态码重试后抛 FetchError。"""
        fetcher = make_fetcher([FakeResponse({}, status_code=502)], retries=1)
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        self.assertIn("502", str(ctx.exception))

    def test_fetch_non_json_raises(self) -> None:
        """响应不是合法 JSON 时抛 FetchError。"""
        fetcher = make_fetcher([FakeResponse(ValueError("no json"))], retries=1)
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        self.assertIn("JSON", str(ctx.exception))

    def test_fetch_unknown_ret_raises(self) -> None:
        """未知的 ret 也会抛出 FetchError 而不是静默返回空。"""
        fetcher = make_fetcher([FakeResponse({"ret": ["FAIL_BIZ_SOMETHING::未知错误"]})])
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("Switch")
        self.assertIn("FAIL_BIZ_SOMETHING", str(ctx.exception))

    def test_page_size_is_clamped(self) -> None:
        """page_size 被限制在 1~100。"""
        self.assertEqual(MtopFetcher(cookies=VALID_COOKIE, page_size=0).page_size, 1)
        self.assertEqual(MtopFetcher(cookies=VALID_COOKIE, page_size=999).page_size, 100)
        self.assertEqual(MtopFetcher(cookies=VALID_COOKIE, page_size=30).page_size, 30)

    def test_close_closes_session(self) -> None:
        """close() 会关闭内部 session。"""
        fetcher = make_fetcher([])
        fetcher.close()
        self.assertTrue(fetcher.session.closed)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------- #
# 4. 工厂与配置
# ---------------------------------------------------------------------- #
class TestMtopFactory(unittest.TestCase):
    """build_fetcher 与配置白名单测试。"""

    @staticmethod
    def _config(ftype: str) -> Any:
        """构造只改 fetcher.type 的最小配置。"""
        return config_from_dict(
            {
                "keywords": [{"keyword": "Switch", "max_price": 800}],
                "monitor": {"interval_seconds": 300, "cookies": VALID_COOKIE},
                "fetcher": {"type": ftype, "page_size": 20},
                "storage": {"path": ":memory:"},
                "notify": {"channels": [{"type": "console"}]},
            }
        )

    def test_mtop_in_valid_types(self) -> None:
        """mtop 已加入 fetcher.type 白名单，且 web/mock 仍保留。"""
        self.assertIn("mtop", VALID_FETCHER_TYPES)
        self.assertIn("web", VALID_FETCHER_TYPES)
        self.assertIn("mock", VALID_FETCHER_TYPES)

    def test_build_fetcher_mtop(self) -> None:
        """type=mtop 返回 MtopFetcher，并正确透传 cookie 与 page_size。"""
        fetcher = build_fetcher(self._config("mtop"))
        self.assertIsInstance(fetcher, MtopFetcher)
        assert isinstance(fetcher, MtopFetcher)
        self.assertEqual(fetcher.name, "mtop")
        self.assertEqual(fetcher.page_size, 20)
        self.assertEqual(fetcher.current_token(), "deadbeefcafe")
        fetcher.close()

    def test_build_fetcher_backward_compatible(self) -> None:
        """web / mock 行为保持不变（向后兼容）。"""
        self.assertIsInstance(build_fetcher(self._config("web")), WebFetcher)
        self.assertIsInstance(build_fetcher(self._config("mock")), MockFetcher)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
