"""v1.9 Cookie 分层诊断 / 可用性规则 / 空闲保活 单元测试。

覆盖本次改造的核心契约：
    1. _m_h5_tk 内嵌时间戳语义 = 过期时刻（旧实现误当签发时刻 + 叠加 TTL）；
    2. 分层诊断输出的四层结构与可用性规则（令牌过期 ≠ 不可用）；
    3. cookie.py 兼容 API（detect_cookie_health / cookie_is_usable / cookie_expiry_status）；
    4. 空闲保活的触发判定（纯函数）。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import credential as C  # noqa: E402
from xianyu_alert.cookie import (  # noqa: E402
    cookie_expiry_status,
    cookie_is_usable,
    detect_cookie_health,
)
from xianyu_alert.keepalive import MIN_KEEPALIVE_INTERVAL, keepalive_due  # noqa: E402

NOW = 1785488087003
HOUR = 3600 * 1000
DAY = 24 * HOUR


def make_cookie(token_off: int | None = 3 * 3600 * 1000,
                havana_off: int | None = 30 * DAY,
                session: bool = True) -> str:
    """按"相对当前时间的偏移"构造真实形态的 Cookie。"""
    parts = []
    if session:
        parts.append("cookie2=abc")
        parts.append("unb=123")
        parts.append("sgcookie=sg")
    if token_off is not None:
        parts.append(f"_m_h5_tk=tokenhash_{NOW + token_off}")
        parts.append("_m_h5_tk_enc=enc")
    if havana_off is not None:
        parts.append(f"havana_lgc_exp={NOW + havana_off}")
    parts.append("tfstk=tf")
    return "; ".join(parts)


class TestTokenTimestampSemantics(unittest.TestCase):
    """内嵌时间戳是"过期时刻"（本次修正的核心）。"""

    def test_parses_13_digit_suffix(self) -> None:
        self.assertEqual(C.token_expires_at_ms(f"_m_h5_tk=tok_{NOW}"), NOW)

    def test_rejects_non_13_digit(self) -> None:
        self.assertIsNone(C.token_expires_at_ms("_m_h5_tk=tok_123"))
        self.assertIsNone(C.token_expires_at_ms("cookie2=abc"))

    def test_havana_exp_parsed(self) -> None:
        ck = f"havana_lgc_exp={NOW}"
        self.assertEqual(C.havana_expires_at_ms(ck), NOW)

    def test_renewal_window_scales_with_observed_ttl(self) -> None:
        """续期窗口 = max(15 分钟, 实测 TTL/6)，未知时 30 分钟兜底。"""
        self.assertEqual(C.renewal_window_ms(None), C.TOKEN_RENEWAL_WINDOW_FALLBACK_MS)
        self.assertEqual(C.renewal_window_ms(4 * 3600 * 1000), 40 * 60 * 1000)
        self.assertEqual(C.renewal_window_ms(60 * 60 * 1000), C.TOKEN_RENEWAL_WINDOW_MIN_MS)


class TestLayeredDiagnosis(unittest.TestCase):
    """分层结构与可用性规则。"""

    def test_layers_order_and_keys(self) -> None:
        diag = C.diagnose_cookie(make_cookie(), now_ms=NOW)
        self.assertEqual([layer.key for layer in diag.layers], ["session", "havana", "token", "risk"])

    def test_token_expired_is_still_usable(self) -> None:
        """令牌过期 5 小时、登录态完好 → 仍可用（服务端会下发新令牌自愈）。"""
        diag = C.diagnose_cookie(make_cookie(token_off=-5 * HOUR), now_ms=NOW)
        self.assertEqual(diag.state, C.STATE_TOKEN_EXPIRED)
        self.assertTrue(diag.usable)
        self.assertLess(diag.token_remaining_ms or 0, 0)

    def test_havana_remaining_reported(self) -> None:
        diag = C.diagnose_cookie(make_cookie(), now_ms=NOW)
        self.assertAlmostEqual((diag.havana_remaining_ms or 0) / DAY, 30, delta=0.01)
        self.assertEqual(diag.layer("havana").state, "ok")

    def test_havana_expired_flagged(self) -> None:
        diag = C.diagnose_cookie(make_cookie(havana_off=-HOUR), now_ms=NOW)
        self.assertEqual(diag.state, C.STATE_HAVANA_EXPIRED)
        self.assertIn("免扫码", diag.reason)

    def test_session_missing_is_error_but_still_tried(self) -> None:
        diag = C.diagnose_cookie(make_cookie(session=False), now_ms=NOW)
        self.assertEqual(diag.state, C.STATE_SESSION_MISSING)
        self.assertEqual(diag.severity, "error")
        self.assertTrue(diag.usable)

    def test_empty_cookie_not_usable(self) -> None:
        diag = C.diagnose_cookie("", now_ms=NOW)
        self.assertEqual(diag.state, C.STATE_MISSING)
        self.assertFalse(diag.usable)

    def test_reason_keeps_legacy_keywords(self) -> None:
        """通知/界面文案保留「过期 / 即将过期 / _m_h5_tk」等关键词。"""
        self.assertIn("过期", C.diagnose_cookie(make_cookie(token_off=-HOUR), now_ms=NOW).reason)
        self.assertIn("即将过期", C.diagnose_cookie(make_cookie(token_off=60 * 1000), now_ms=NOW).reason)
        self.assertIn("_m_h5_tk", C.diagnose_cookie(make_cookie(token_off=None), now_ms=NOW).reason)


class TestCookieCompatApi(unittest.TestCase):
    """cookie.py 兼容层（旧调用方 / 界面 / 保存校验）。"""

    def test_expiry_status_direct_comparison(self) -> None:
        # v1.9：内嵌时间戳直接与 now 比较（过期时刻语义，不再叠加 TTL）
        self.assertEqual(cookie_expiry_status(f"_m_h5_tk=tok_{NOW + 3 * HOUR}", now_ms=NOW), "ok")
        self.assertEqual(cookie_expiry_status(f"_m_h5_tk=tok_{NOW - HOUR}", now_ms=NOW), "expired")
        self.assertEqual(cookie_expiry_status(f"_m_h5_tk=tok_{NOW + 60000}", now_ms=NOW), "expiring")
        self.assertEqual(cookie_expiry_status("", now_ms=NOW), "missing")
        self.assertEqual(cookie_expiry_status("cookie2=x", now_ms=NOW), "no_token")

    def test_health_states(self) -> None:
        # detect_cookie_health 不带 now 参数 → 用真实时间构造样本
        real = int(time.time() * 1000)
        far = real + 30 * 24 * 3600 * 1000
        ok_cookie = f"cookie2=abc; unb=1; _m_h5_tk=tok_{real + 3 * 3600 * 1000}; havana_lgc_exp={far}"
        expired = f"cookie2=abc; unb=1; _m_h5_tk=tok_{real - 3600 * 1000}; havana_lgc_exp={far}"
        self.assertEqual(detect_cookie_health(ok_cookie)[0], "ok")
        self.assertEqual(detect_cookie_health(expired)[0], "expired")
        self.assertEqual(detect_cookie_health(make_cookie(session=False))[0], "session_missing")
        self.assertEqual(detect_cookie_health("")[0], "missing")

    def test_usable_ignores_token_layer(self) -> None:
        self.assertTrue(cookie_is_usable(make_cookie(token_off=-5 * HOUR)))
        self.assertTrue(cookie_is_usable(make_cookie(token_off=None)))
        self.assertFalse(cookie_is_usable(""))
        self.assertFalse(cookie_is_usable("dpapi1:broken"))


class TestKeepaliveDue(unittest.TestCase):
    """空闲保活触发判定（纯函数）。"""

    def test_disabled_never_due(self) -> None:
        self.assertFalse(keepalive_due(now=10000, last_auth_at=0, interval=1800, enabled=False))

    def test_too_short_interval_disabled(self) -> None:
        self.assertFalse(
            keepalive_due(now=10000, last_auth_at=0, interval=MIN_KEEPALIVE_INTERVAL - 1)
        )

    def test_never_authed_is_due(self) -> None:
        self.assertTrue(keepalive_due(now=10000, last_auth_at=0, interval=1800))

    def test_due_after_interval(self) -> None:
        self.assertFalse(keepalive_due(now=10000, last_auth_at=10000 - 60, interval=1800))
        self.assertTrue(keepalive_due(now=10000, last_auth_at=10000 - 1800, interval=1800))


if __name__ == "__main__":
    unittest.main()
