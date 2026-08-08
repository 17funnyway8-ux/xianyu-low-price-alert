"""GUI v3 纯函数测试：Cookie 六态判定、翻页字段校验、新通道完整性、空状态引导、关于文案。

沿用 test_gui.py 的「抽纯函数测试」模式，**不真正显示窗口**。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import secure  # noqa: E402
from xianyu_alert.gui import (  # noqa: E402
    CHANNEL_FIELDS,
    CHANNEL_ORDER,
    CHANNEL_REQUIRED_FIELDS,
    COOKIE_STATE_EXPIRED,
    COOKIE_STATE_EXPIRING,
    COOKIE_STATE_MISSING,
    COOKIE_STATE_NO_TOKEN,
    COOKIE_STATE_OK,
    COOKIE_STATE_UNDECRYPTABLE,
    about_text,
    build_config_dict,
    channel_is_complete,
    config_to_form,
    cookie_status,
    default_channel_options,
    empty_state_hint,
    first_use_guide,
    validate_pages,
)

#: 未来时间戳（正常）
FUTURE_COOKIE = "_m_h5_tk=abc_9999999999999; cookie2=1"
#: 2023 年签发（已过期）
EXPIRED_COOKIE = "_m_h5_tk=abc_1700000000000; cookie2=1"


# ---------------------------------------------------------------------- #
# 1. Cookie 六态判定（U3）
# ---------------------------------------------------------------------- #
class TestCookieStatusSixStates(unittest.TestCase):
    """状态灯六态：未配置 / 缺 token / 已过期 / 即将过期 / 无法解密 / 正常。"""

    def test_missing(self) -> None:
        state, text = cookie_status("")
        self.assertEqual(state, COOKIE_STATE_MISSING)
        self.assertIn("未配置", text)

    def test_no_token(self) -> None:
        state, text = cookie_status("cookie2=1; unb=2")
        self.assertEqual(state, COOKIE_STATE_NO_TOKEN)
        self.assertIn("_m_h5_tk", text)

    def test_ok(self) -> None:
        state, text = cookie_status(FUTURE_COOKIE)
        self.assertEqual(state, COOKIE_STATE_OK)
        self.assertIn("✅", text)

    def test_expired(self) -> None:
        state, text = cookie_status(EXPIRED_COOKIE)
        self.assertEqual(state, COOKIE_STATE_EXPIRED)
        self.assertIn("过期", text)

    def test_expiring(self) -> None:
        now = int(time.time() * 1000)
        # 23.5 小时前签发 → 剩余 30 分钟过期 → 临期
        expiring_cookie = f"_m_h5_tk=abc_{now - 23 * 3600 * 1000 - 30 * 60 * 1000}"
        state, text = cookie_status(expiring_cookie)
        self.assertEqual(state, COOKIE_STATE_EXPIRING)
        self.assertIn("即将过期", text)

    def test_undecryptable(self) -> None:
        """dpapi1: 密文且解密失败 → 无法解密。"""
        with mock.patch.object(secure, "decrypt_text", return_value=""):
            state, text = cookie_status(secure.PREFIX + "AAAA")
        self.assertEqual(state, COOKIE_STATE_UNDECRYPTABLE)
        self.assertIn("无法解密", text)

    def test_encrypted_ok_after_decrypt(self) -> None:
        """dpapi1: 密文解密成功 → 按明文继续判定。"""
        with mock.patch.object(secure, "decrypt_text", return_value=FUTURE_COOKIE):
            state, _text = cookie_status(secure.PREFIX + "Zm9v")
        self.assertEqual(state, COOKIE_STATE_OK)

    def test_legacy_short_timestamp_is_ok(self) -> None:
        """旧测试样本 `_m_h5_tk=deadbeef_170000`（6 位后缀）仍判定为 OK（兼容红线）。"""
        state, text = cookie_status("cookie2=abc; _m_h5_tk=deadbeef_170000; x=1")
        self.assertEqual(state, COOKIE_STATE_OK)
        self.assertIn("✅", text)


# ---------------------------------------------------------------------- #
# 2. 翻页字段校验
# ---------------------------------------------------------------------- #
class TestValidatePages(unittest.TestCase):
    """抓取页数输入校验。"""

    def test_ok(self) -> None:
        self.assertEqual(validate_pages("1"), 1)
        self.assertEqual(validate_pages(" 3 "), 3)
        self.assertEqual(validate_pages("2.0"), 2)

    def test_invalid(self) -> None:
        for bad in ("", "abc", "0", "-1", "1.5"):
            with self.assertRaises(ValueError):
                validate_pages(bad)


# ---------------------------------------------------------------------- #
# 3. 新通道（bark / webhook）表单与完整性
# ---------------------------------------------------------------------- #
class TestChannelV3(unittest.TestCase):
    """bark / webhook 通道的 UI 定义与完整性判定。"""

    def test_bark_and_webhook_in_order_and_fields(self) -> None:
        self.assertIn("bark", CHANNEL_ORDER)
        self.assertIn("webhook", CHANNEL_ORDER)
        self.assertEqual(CHANNEL_REQUIRED_FIELDS["bark"], ("url",))
        self.assertEqual(CHANNEL_REQUIRED_FIELDS["webhook"], ("url",))
        bark_fields = {name for name, _label, _secret, _default in CHANNEL_FIELDS["bark"]}
        webhook_fields = {name for name, _label, _secret, _default in CHANNEL_FIELDS["webhook"]}
        self.assertIn("url", bark_fields)
        self.assertIn("url", webhook_fields)

    def test_channel_complete(self) -> None:
        self.assertTrue(channel_is_complete("bark", {"url": "https://api.day.app/KEY/"}))
        self.assertFalse(channel_is_complete("bark", {}))
        self.assertTrue(channel_is_complete("webhook", {"url": "https://qyapi.weixin.qq.com/x"}))
        self.assertFalse(channel_is_complete("webhook", {"url": "  "}))

    def test_default_options(self) -> None:
        self.assertIn("url", default_channel_options("bark"))
        self.assertIn("url", default_channel_options("webhook"))

    def test_build_config_dict_keeps_new_channels(self) -> None:
        data = build_config_dict(
            keywords=[("Switch", 800.0)],
            interval_seconds=300,
            fetcher_type="mock",
            cookies="",
            storage_path="state/x.db",
            channels={
                "bark": {"enabled": True, "options": {"url": "https://api.day.app/KEY/"}},
                "webhook": {"enabled": True, "options": {"url": "https://qyapi.weixin.qq.com/x"}},
                "console": {"enabled": False, "options": {}},
            },
        )
        types = [c["type"] for c in data["notify"]["channels"]]
        self.assertEqual(types, ["bark", "webhook"])

    def test_build_config_dict_writes_pages(self) -> None:
        data = build_config_dict(
            keywords=[("A", 1.0)],
            interval_seconds=300,
            fetcher_type="mock",
            cookies="",
            storage_path="a.db",
            channels={"console": {"enabled": True, "options": {}}},
            pages=3,
        )
        self.assertEqual(data["fetcher"]["pages"], 3)

    def test_config_to_form_reads_pages(self) -> None:
        form = config_to_form({"fetcher": {"type": "mtop", "pages": 2}})
        self.assertEqual(form["pages"], 2)


# ---------------------------------------------------------------------- #
# 4. 空状态引导 / 首次使用引导 / 关于
# ---------------------------------------------------------------------- #
class TestGuideTexts(unittest.TestCase):
    """U1 空状态引导、U2 首次使用引导、U5 关于对话框。"""

    def test_empty_state_hint(self) -> None:
        self.assertEqual(empty_state_hint(True), "")
        self.assertIn("还没有关键词", empty_state_hint(False))

    def test_first_use_guide(self) -> None:
        self.assertIn("获取 Cookie", first_use_guide("mtop", COOKIE_STATE_MISSING))
        self.assertEqual(first_use_guide("mock", COOKIE_STATE_MISSING), "")
        self.assertEqual(first_use_guide("mtop", COOKIE_STATE_OK), "")
        self.assertEqual(first_use_guide("web", COOKIE_STATE_MISSING), "")

    def test_about_text(self) -> None:
        from xianyu_alert import __version__

        text = about_text()
        self.assertIn("闲鱼低价提醒工具", text)
        # 版本号随 __version__ 自动更新（v3.2: 1.3.0；v3.3: 1.4.0；v3.4: 1.4.1）
        self.assertIn(f"v{__version__}", text)
        self.assertIn("作者", text)
        self.assertIn("免责声明", text)


# ---------------------------------------------------------------------- #
# 5. Cookie 加密保存（build_config_dict 的 encrypt_cookies 分支）
# ---------------------------------------------------------------------- #
class TestEncryptCookiesInConfig(unittest.TestCase):
    """GUI 保存路径：encrypt_cookies=True 时 Cookie 以密文落盘。"""

    def test_encrypt_cookies_true(self) -> None:
        with mock.patch.object(secure, "encrypt_text", return_value=secure.PREFIX + "Zm9vYmFy"):
            data = build_config_dict(
                keywords=[("A", 1.0)],
                interval_seconds=300,
                fetcher_type="mtop",
                cookies="_m_h5_tk=abc_1",
                storage_path="a.db",
                channels={"console": {"enabled": True, "options": {}}},
                encrypt_cookies=True,
            )
        self.assertTrue(data["monitor"]["cookies"].startswith(secure.PREFIX))
        self.assertTrue(data["monitor"]["cookies_encrypted"])

    def test_encrypt_cookies_false_keeps_plaintext(self) -> None:
        data = build_config_dict(
            keywords=[("A", 1.0)],
            interval_seconds=300,
            fetcher_type="mock",
            cookies="_m_h5_tk=abc_1",
            storage_path="a.db",
            channels={"console": {"enabled": True, "options": {}}},
        )
        self.assertEqual(data["monitor"]["cookies"], "_m_h5_tk=abc_1")
        self.assertNotIn("cookies_encrypted", data["monitor"])

    def test_encrypt_degraded_keeps_plaintext(self) -> None:
        """DPAPI 不可用（降级明文）时不写加密标记。"""
        with mock.patch.object(secure, "encrypt_text", side_effect=lambda p: p):
            data = build_config_dict(
                keywords=[("A", 1.0)],
                interval_seconds=300,
                fetcher_type="mtop",
                cookies="_m_h5_tk=abc_1",
                storage_path="a.db",
                channels={"console": {"enabled": True, "options": {}}},
                encrypt_cookies=True,
            )
        self.assertEqual(data["monitor"]["cookies"], "_m_h5_tk=abc_1")
        self.assertNotIn("cookies_encrypted", data["monitor"])

    def test_config_to_form_decrypts_cookie(self) -> None:
        """config_to_form 读到 dpapi1: 密文时自动解密供界面编辑。"""
        with mock.patch.object(secure, "decrypt_text", return_value="_m_h5_tk=abc_1"):
            form = config_to_form({"monitor": {"cookies": secure.PREFIX + "Zm9v"}})
        self.assertEqual(form["cookies"], "_m_h5_tk=abc_1")

    def test_config_to_form_undecryptable_flag(self) -> None:
        """密文无法解密 → cookies 置空并标记 undecryptable。"""
        with mock.patch.object(secure, "decrypt_text", return_value=""):
            form = config_to_form({"monitor": {"cookies": secure.PREFIX + "AAAA"}})
        self.assertEqual(form["cookies"], "")
        self.assertTrue(form["cookies_undecryptable"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
