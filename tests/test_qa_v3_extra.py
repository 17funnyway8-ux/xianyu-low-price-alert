"""QA 独立复核用例（v3 新增边界，与工程师自测用例互补）。

覆盖范围（逐项对应 QA 验证任务第 5 节）：
    1. parse_price：万换算边界 / ¥+万组合 / 面议过滤 / 非数字
    2. secure：dpapi1: 密文 round-trip / 损坏密文安全失败 / 空串 None 不崩
    3. check_cookie_health：过期 / 临期 / 正常 / 缺 token
    4. WebhookNotifier：Content-Type: application/json + payload 结构
    5. MtopFetcher 多页：第 2 页失败返回第 1 页 / pages=2 post 调用 2 次
    6. shortcut：PowerShell `$` 转义（反引号形态）
    7. GUI Cookie 六态纯函数：空 / 缺 token / 过期 / 临期 / 正常

全部用例无外网依赖（网络请求一律 mock）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import secure  # noqa: E402


def setUpModule() -> None:
    """密钥文件隔离到临时目录（Fernet 真实加解密所需，避免污染项目根）。"""
    _tmp = tempfile.TemporaryDirectory(prefix="xianyu-qa3x-")
    secure.set_key_file(os.path.join(_tmp.name, "secret.key"))
    globals()["_SECURE_TMP"] = _tmp


def tearDownModule() -> None:
    secure.set_key_file(None)
    _tmp = globals().get("_SECURE_TMP")
    if _tmp is not None:
        _tmp.cleanup()

from xianyu_alert.fetcher import FetchError, MtopFetcher, parse_price  # noqa: E402
from xianyu_alert.gui import (  # noqa: E402
    COOKIE_STATE_EXPIRED,
    COOKIE_STATE_EXPIRING,
    COOKIE_STATE_MISSING,
    COOKIE_STATE_NO_TOKEN,
    COOKIE_STATE_OK,
    cookie_status,
)
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.notifier import WebhookNotifier  # noqa: E402
from xianyu_alert.shortcut import _ps_escape  # noqa: E402

VALID_COOKIE = "cookie2=abc; _m_h5_tk=deadbeefcafe_1700000000000; x=1"


# ---------------------------------------------------------------------- #
# 1. parse_price 边界
# ---------------------------------------------------------------------- #
class TestQaParsePriceBoundary(unittest.TestCase):
    """万换算边界、¥+万组合、面议过滤、非数字。"""

    def test_wan_one_point_two(self) -> None:
        self.assertAlmostEqual(parse_price("1.2万"), 12000.0)

    def test_wan_zero_point_five(self) -> None:
        self.assertAlmostEqual(parse_price("0.5万"), 5000.0)

    def test_wan_with_yen_symbol(self) -> None:
        self.assertAlmostEqual(parse_price("¥1.2万"), 12000.0)
        self.assertAlmostEqual(parse_price("￥ 0.8万"), 8000.0)

    def test_wan_with_comma(self) -> None:
        # 千分位 + 万 组合
        self.assertAlmostEqual(parse_price("1,234.5万"), 12345000.0)

    def test_mianyi_returns_none(self) -> None:
        self.assertIsNone(parse_price("面议"))
        self.assertIsNone(parse_price("价格面议"))

    def test_non_numeric_returns_none(self) -> None:
        self.assertIsNone(parse_price("全新未拆封"))
        self.assertIsNone(parse_price("abc"))
        self.assertIsNone(parse_price("万"))  # 只有「万」无数字


# ---------------------------------------------------------------------- #
# 2. secure 边界
# ---------------------------------------------------------------------- #
class TestQaSecureBoundary(unittest.TestCase):
    """fernet1: 密文 round-trip / 损坏密文安全失败 / 遗留前缀 / 空输入。"""

    def test_fernet_roundtrip(self) -> None:
        plain = "_m_h5_tk=abc_1234567890123"
        cipher = secure.encrypt_text(plain)
        self.assertTrue(cipher.startswith(secure.FERNET_PREFIX))
        self.assertEqual(secure.decrypt_text(cipher), plain)

    def test_fernet_roundtrip_on_all_platforms(self) -> None:
        """Fernet 跨平台可用（无需 mock platform），加密后必然带前缀。"""
        plain = "_m_h5_tk=real_1785488087003; cookie2=1"
        cipher = secure.encrypt_text(plain)
        if not secure.is_encrypted(cipher):
            self.skipTest("加密不可用（降级明文）")
        self.assertEqual(secure.decrypt_text(cipher), plain)

    def test_legacy_dpapi_prefix_undecryptable(self) -> None:
        """dpapi1: 遗留密文 → 安全返回空串（提示重登），不抛异常。"""
        with self.assertLogs("xianyu_alert.secure", level="WARNING"):
            self.assertEqual(secure.decrypt_text(secure.PREFIX + "QUJDREVGRw=="), "")

    def test_corrupted_fernet_cipher_returns_empty_not_crash(self) -> None:
        # 篡改密文字节后解密必须安全失败（返回空串），不得抛异常
        with self.assertLogs("xianyu_alert.secure", level="WARNING"):
            result = secure.decrypt_text(secure.FERNET_PREFIX + "QUJDREVGRw==")
        self.assertEqual(result, "")

    def test_invalid_base64_returns_empty_not_crash(self) -> None:
        # 非法 base64 字符也应安全返回空串
        with self.assertLogs("xianyu_alert.secure", level="WARNING"):
            result = secure.decrypt_text(secure.FERNET_PREFIX + "!!!not-base64!!!")
        self.assertEqual(result, "")

    def test_empty_and_none_inputs_safe(self) -> None:
        self.assertEqual(secure.encrypt_text(""), "")
        self.assertEqual(secure.encrypt_text(None), "")
        self.assertEqual(secure.decrypt_text(""), "")
        self.assertEqual(secure.decrypt_text(None), "")
        self.assertEqual(secure.mask_cookie(""), "")
        self.assertEqual(secure.mask_cookie(None), "")


# ---------------------------------------------------------------------- #
# 3. check_cookie_health 边界
# ---------------------------------------------------------------------- #
class TestQaCookieHealthBoundary(unittest.TestCase):
    """过期 / 临期 / 正常 / 缺 token 判定。"""

    @staticmethod
    def _cookie(timestamp: int) -> str:
        return f"cookie2=1; _m_h5_tk=abc_{timestamp}"

    def test_expired(self) -> None:
        now = int(time.time() * 1000)
        fetcher = MtopFetcher(cookies=self._cookie(now - 25 * 3600 * 1000))
        ok, reason = fetcher.check_cookie_health()
        self.assertFalse(ok)
        self.assertIn("过期", reason)

    def test_expiring(self) -> None:
        now = int(time.time() * 1000)
        # 23.5 小时前签发 → 剩余 30 分钟 → 临期
        fetcher = MtopFetcher(cookies=self._cookie(now - 23 * 3600 * 1000 - 30 * 60 * 1000))
        ok, reason = fetcher.check_cookie_health()
        self.assertFalse(ok)
        self.assertIn("即将过期", reason)

    def test_ok(self) -> None:
        now = int(time.time() * 1000)
        fetcher = MtopFetcher(cookies=self._cookie(now - 10 * 3600 * 1000))
        ok, _reason = fetcher.check_cookie_health()
        self.assertTrue(ok)

    def test_no_token(self) -> None:
        fetcher = MtopFetcher(cookies="cookie2=1; _m_h5_tk_enc=xyz")
        ok, reason = fetcher.check_cookie_health()
        self.assertFalse(ok)
        self.assertIn("缺少", reason)


# ---------------------------------------------------------------------- #
# 4. WebhookNotifier Content-Type 与 payload
# ---------------------------------------------------------------------- #
class TestQaWebhook(unittest.TestCase):
    """Webhook：显式 Content-Type: application/json + payload 结构。"""

    def _product(self) -> Product:
        return Product(
            product_id="1", title="Switch", price=500.0,
            url="https://goofish.com/item?id=1", publish_time="2024-01-01",
            keyword="Switch",
        )

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_content_type_and_payload(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = mock.Mock(status_code=200)
        WebhookNotifier("https://qyapi.weixin.qq.com/webhook/send?key=k").notify([self._product()])
        args, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        payload = json.loads(kwargs["data"])
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("Switch", payload["text"]["content"])
        self.assertIn("500", payload["text"]["content"])

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_missing_url_channel_skipped_by_factory(self, mock_post: mock.MagicMock) -> None:
        from xianyu_alert.notifier import build_notifiers
        from xianyu_alert.config import config_from_dict

        config = config_from_dict({
            "keywords": [{"keyword": "Switch", "max_price": 1000}],
            "notify": {"channels": [{"type": "webhook", "url": ""}]},
        })
        with self.assertLogs("xianyu_alert.notifier", level="WARNING"):
            notifiers = build_notifiers(config)
        self.assertEqual([n.name for n in notifiers], ["console"])


# ---------------------------------------------------------------------- #
# 5. MtopFetcher 多页容错
# ---------------------------------------------------------------------- #
class TestQaMtopMultiPage(unittest.TestCase):
    """第 2 页失败返回第 1 页 / pages=2 post 调用 2 次。"""

    class FakeJar:
        def __init__(self) -> None:
            self._d: dict = {}

        def set(self, name: str, value: str, **_kw: object) -> None:
            self._d[name] = value

        def get(self, name: str, default: object = None) -> object:
            return self._d.get(name, default)

    class FakeResp:
        def __init__(self, payload: object, status: int = 200) -> None:
            self._payload = payload
            self.status_code = status
            self.cookies = TestQaMtopMultiPage.FakeJar()
            self.headers: dict = {}

        def json(self) -> object:
            return self._payload

    class FakeSession:
        def __init__(self, script: list) -> None:
            self.script = list(script)
            self.calls: list = []
            self.cookies = TestQaMtopMultiPage.FakeJar()

        def post(self, url: str, **kwargs: object) -> "TestQaMtopMultiPage.FakeResp":
            self.calls.append(kwargs)
            if not self.script:
                raise AssertionError("脚本耗尽")
            return self.script.pop(0)

        def close(self) -> None:
            pass

    @staticmethod
    def _item(item_id: str, price: str = "100") -> dict:
        return {
            "data": {"item": {"main": {
                "exContent": {"title": "Switch 商品", "area": "深圳"},
                "clickParam": {"args": {"item_id": item_id, "price": price, "publishTime": "1700000000000"}},
            }}}
        }

    @staticmethod
    def _ok(items: list) -> object:
        return TestQaMtopMultiPage.FakeResp(
            {"ret": ["SUCCESS::调用成功"], "data": {"resultList": items}}
        )

    def test_page2_failure_keeps_page1(self) -> None:
        session = self.FakeSession([
            self._ok([self._item("1001")]),
            self.FakeResp({"ret": ["RGV587_ERROR::SM::被挤爆"]}),
        ])
        fetcher = MtopFetcher(
            cookies=VALID_COOKIE, pages=2, retries=1,
            session=session, sleep_func=lambda _s: None,
        )
        with self.assertLogs("xianyu_alert.fetcher", level="WARNING"):
            products = fetcher.fetch("Switch")
        self.assertEqual([p.product_id for p in products], ["1001"])
        self.assertEqual(len(session.calls), 2)

    def test_pages2_calls_post_twice(self) -> None:
        session = self.FakeSession([
            self._ok([self._item("2001")]),
            self._ok([self._item("2002")]),
        ])
        fetcher = MtopFetcher(
            cookies=VALID_COOKIE, pages=2,
            session=session, sleep_func=lambda _s: None,
        )
        fetcher.fetch("Switch")
        self.assertEqual(len(session.calls), 2)

    def test_all_pages_fail_raises(self) -> None:
        session = self.FakeSession([
            self.FakeResp({"ret": ["FAIL_SYS_SESSION_EXPIRED"]}),
        ])
        fetcher = MtopFetcher(
            cookies=VALID_COOKIE, pages=1,
            session=session, sleep_func=lambda _s: None,
        )
        with self.assertRaises(FetchError):
            fetcher.fetch("Switch")


# ---------------------------------------------------------------------- #
# 6. shortcut 转义
# ---------------------------------------------------------------------- #
class TestQaShortcutEscape(unittest.TestCase):
    """PowerShell `$` 转义：`$env:USERPROFILE` → 反引号形态。"""

    def test_dollar_escaped(self) -> None:
        self.assertEqual(_ps_escape("$env:USERPROFILE"), "`$env:USERPROFILE")

    def test_dollar_in_path_escaped(self) -> None:
        self.assertEqual(_ps_escape(r"C:\Users\$fun\App"), r"C:\Users\`$fun\App")

    def test_quote_and_backtick_escaped(self) -> None:
        self.assertEqual(_ps_escape('a"b`c'), 'a`"b``c')

    def test_escape_order_backtick_first(self) -> None:
        # 反引号必须先转义，否则后面新增的反引号会被二次转义
        self.assertEqual(_ps_escape("`$"), "```$")

    @mock.patch("xianyu_alert.shortcut.subprocess.run")
    def test_script_contains_escaped_dollar(self, mock_run: mock.MagicMock) -> None:
        from xianyu_alert.shortcut import build_powershell_script

        script = build_powershell_script(
            target=r"C:\Users\$env:USERPROFILE\x.exe",
            args=r"--config $env:USERPROFILE\c.yaml",
            workdir=r"C:\Users\$env:USERPROFILE",
            lnk=r"C:\Users\fun\Desktop\test.lnk",
        )
        self.assertIn("`$env:USERPROFILE", script)
        # 属性值部分不允许出现裸 $
        for line in script.splitlines():
            if ".TargetPath" in line or ".Arguments" in line or ".WorkingDirectory" in line:
                value = line.split('"', 1)[1] if '"' in line else ""
                self.assertNotIn("$", value.replace("`$", ""))


# ---------------------------------------------------------------------- #
# 7. GUI Cookie 六态纯函数
# ---------------------------------------------------------------------- #
class TestQaGuiCookieSixStates(unittest.TestCase):
    """六态：空 / 缺 token / 过期 / 临期 / 正常。"""

    def test_empty_is_missing(self) -> None:
        state, _text = cookie_status("")
        self.assertEqual(state, COOKIE_STATE_MISSING)

    def test_no_token(self) -> None:
        state, _text = cookie_status("cookie2=1; unb=2")
        self.assertEqual(state, COOKIE_STATE_NO_TOKEN)

    def test_expired(self) -> None:
        now = int(time.time() * 1000)
        state, _text = cookie_status(f"_m_h5_tk=abc_{now - 25 * 3600 * 1000}")
        self.assertEqual(state, COOKIE_STATE_EXPIRED)

    def test_expiring(self) -> None:
        now = int(time.time() * 1000)
        state, _text = cookie_status(f"_m_h5_tk=abc_{now - 23 * 3600 * 1000 - 30 * 60 * 1000}")
        self.assertEqual(state, COOKIE_STATE_EXPIRING)

    def test_ok(self) -> None:
        now = int(time.time() * 1000)
        state, _text = cookie_status(f"_m_h5_tk=abc_{now + 10 * 3600 * 1000}")
        self.assertEqual(state, COOKIE_STATE_OK)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
