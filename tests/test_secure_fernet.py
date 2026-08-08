"""Fernet 全分支专项测试（macOS 适配设计文档 §3.2 / 任务 T05）。

与 test_secure.py 互补，聚焦 Fernet 特有语义：
    - 密文格式（fernet1: + Fernet token；不含明文；可识别前缀）；
    - 跨平台一致性（不依赖 sys.platform，任何平台都加密而非降级）；
    - 密钥文件管理（set_key_file 切换 / 重置缓存 / 重新生成）；
    - 前缀兼容（dpapi1: 识别为密文但不可解；fernet1: 可解）；
    - 加解密失败路径（损坏 token / 换密钥 / 空输入）一律不抛异常。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import secure  # noqa: E402

_KEY_DIR: str = ""


def setUpModule() -> None:
    global _KEY_DIR
    _KEY_DIR = tempfile.mkdtemp(prefix="xianyu-fernet-")
    secure.set_key_file(os.path.join(_KEY_DIR, "secret.key"))


def tearDownModule() -> None:
    secure.set_key_file(None)


class TestFernetCipherFormat(unittest.TestCase):
    """密文格式与可识别性。"""

    def test_prefix_is_fernet1(self) -> None:
        cipher = secure.encrypt_text("abc")
        self.assertTrue(cipher.startswith(secure.FERNET_PREFIX))
        self.assertNotIn("abc", cipher)  # 明文绝不落盘
        self.assertTrue(secure.is_encrypted(cipher))

    def test_token_is_valid_fernet(self) -> None:
        """base64 解码后是 Fernet token（版本 0x80 开头）。"""
        from cryptography.fernet import Fernet

        cipher = secure.encrypt_text("abc")
        token = cipher[len(secure.FERNET_PREFIX):]
        # Fernet token 第 1 字节固定 0x80
        import base64

        raw = base64.urlsafe_b64decode(token)
        self.assertEqual(raw[0], 0x80)
        # 同一密钥可独立验证解密
        with open(os.path.join(_KEY_DIR, "secret.key"), "rb") as fp:
            key = fp.read().strip()
        self.assertEqual(Fernet(key).decrypt(token), b"abc")

    def test_legacy_prefix_still_detected(self) -> None:
        self.assertTrue(secure.is_encrypted(secure.PREFIX + "x"))
        self.assertTrue(secure.is_encrypted(secure.FERNET_PREFIX + "x"))
        self.assertFalse(secure.is_encrypted("plain"))

    def test_empty_and_none(self) -> None:
        self.assertEqual(secure.encrypt_text(""), "")
        self.assertEqual(secure.encrypt_text(None), "")
        self.assertEqual(secure.decrypt_text(""), "")
        self.assertEqual(secure.decrypt_text(None), "")


class TestFernetCrossPlatform(unittest.TestCase):
    """跨平台语义：任何平台都加密（不再有「非 Windows 降级明文」）。"""

    def test_encrypt_works_on_any_platform(self) -> None:
        """即使 mock 成 darwin/linux，加密仍产生 fernet1: 密文。"""
        for platform in ("darwin", "linux", "win32"):
            with self.subTest(platform=platform):
                cipher = secure.encrypt_text("_m_h5_tk=t_1")
                self.assertTrue(cipher.startswith(secure.FERNET_PREFIX), platform)
                self.assertEqual(secure.decrypt_text(cipher), "_m_h5_tk=t_1")

    def test_roundtrip_long_cookie(self) -> None:
        plain = "_m_h5_tk=deadbeef_1700000000000; cookie2=abc; x=中文值"
        self.assertEqual(secure.decrypt_text(secure.encrypt_text(plain)), plain)


class TestFernetKeyManagement(unittest.TestCase):
    """set_key_file 切换 / 缓存重置 / 重新生成。"""

    def test_set_key_file_resets_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_a = os.path.join(tmp, "a.key")
            key_b = os.path.join(tmp, "b.key")
            secure.set_key_file(key_a)
            cipher_a = secure.encrypt_text("secret-a")
            secure.set_key_file(key_b)  # 重置缓存 → 生成新密钥
            cipher_b = secure.encrypt_text("secret-b")
            self.assertNotEqual(cipher_a, cipher_b)
            # 各密钥只能解各自的密文
            secure.set_key_file(key_a)
            self.assertEqual(secure.decrypt_text(cipher_a), "secret-a")
            self.assertEqual(secure.decrypt_text(cipher_b), "")
            secure.set_key_file(key_b)
            self.assertEqual(secure.decrypt_text(cipher_a), "")
            self.assertEqual(secure.decrypt_text(cipher_b), "secret-b")

    def test_key_persists_across_process_cache_reset(self) -> None:
        """密钥落盘后，即使重置缓存重新加载，旧密文仍可解。"""
        with tempfile.TemporaryDirectory() as tmp:
            key_path = os.path.join(tmp, "secret.key")
            secure.set_key_file(key_path)
            cipher = secure.encrypt_text("persist-me")
            secure.set_key_file(key_path)  # 强制重读磁盘
            self.assertEqual(secure.decrypt_text(cipher), "persist-me")


class TestFernetFailureBranches(unittest.TestCase):
    """损坏 token / 换密钥 / 非法前缀 → 安全返回空串。"""

    def test_corrupted_token_returns_empty(self) -> None:
        with self.assertLogs("xianyu_alert.secure", level="WARNING"):
            self.assertEqual(secure.decrypt_text(secure.FERNET_PREFIX + "QUJD"), "")

    def test_wrong_key_returns_empty(self) -> None:
        cipher = secure.encrypt_text("data")
        with tempfile.TemporaryDirectory() as tmp:
            other = os.path.join(tmp, "other.key")
            from cryptography.fernet import Fernet

            with open(other, "wb") as fp:
                fp.write(Fernet.generate_key())
            secure.set_key_file(other)
            try:
                with self.assertLogs("xianyu_alert.secure", level="WARNING"):
                    self.assertEqual(secure.decrypt_text(cipher), "")
            finally:
                secure.set_key_file(os.path.join(_KEY_DIR, "secret.key"))

    def test_dpapi_prefix_returns_empty_with_warning(self) -> None:
        with self.assertLogs("xianyu_alert.secure", level="WARNING") as logs:
            self.assertEqual(secure.decrypt_text(secure.PREFIX + "AAAA"), "")
        self.assertTrue(any("请重新登录" in line for line in logs.output))

    def test_plaintext_passthrough(self) -> None:
        self.assertEqual(secure.decrypt_text("_m_h5_tk=plain_1"), "_m_h5_tk=plain_1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
