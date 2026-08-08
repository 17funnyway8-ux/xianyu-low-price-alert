"""secure 模块单元测试：Fernet 加解密、密钥文件、前缀兼容、加密保存与迁移。

对齐 macOS 适配设计文档 §3.2 / §5.1：
    - 新密文一律 `fernet1:` 前缀，真实 Fernet 往返（不再 mock crypt 层）；
    - `dpapi1:` 为遗留前缀 → 不可解密 → 返回空串 + warning「请重新登录」；
    - 密钥文件：首次生成 / 复用 / 损坏分支 / POSIX 权限；
    - `ensure_cookie_encrypted` 对存量明文自动升级为 `fernet1:`。

不依赖真实网络；密钥文件一律通过 `secure.set_key_file()` 指向临时目录隔离。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import secure  # noqa: E402
from xianyu_alert.cookie import ensure_cookie_encrypted, save_cookies_encrypted  # noqa: E402

PLAIN = "_m_h5_tk=deadbeef_1700000000000; cookie2=abc"

#: 测试期密钥目录（setUpModule 创建；任何测试不得把密钥重置到默认位置）
_KEY_DIR: str = ""


def _key_path(name: str = "secret.key") -> str:
    return os.path.join(_KEY_DIR, name)


def _reset_key() -> None:
    """把密钥文件指回测试临时目录（所有测试共用一套测试密钥）。"""
    secure.set_key_file(_key_path())


def setUpModule() -> None:
    global _KEY_DIR
    _tmp = tempfile.TemporaryDirectory(prefix="xianyu-secure-")
    _KEY_DIR = _tmp.name
    _reset_key()


def tearDownModule() -> None:
    secure.set_key_file(None)


def _write_yaml(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)


class TestMaskCookie(unittest.TestCase):
    """脱敏显示测试（S3）。"""

    def test_mask_keeps_head_tail(self) -> None:
        masked = secure.mask_cookie(PLAIN)
        self.assertIn(PLAIN[:8], masked)
        self.assertIn(PLAIN[-4:], masked)
        self.assertNotIn(PLAIN[8:-4], masked)

    def test_mask_short_and_empty(self) -> None:
        self.assertEqual(secure.mask_cookie(""), "")
        self.assertEqual(secure.mask_cookie(None), "")
        self.assertTrue(secure.mask_cookie("abc"))


class TestIsEncrypted(unittest.TestCase):
    """前缀判定测试（两种前缀都算密文）。"""

    def test_prefix_detection(self) -> None:
        self.assertTrue(secure.is_encrypted(secure.FERNET_PREFIX + "AAAA"))
        self.assertTrue(secure.is_encrypted(secure.PREFIX + "AAAA"))  # 遗留 dpapi1:
        self.assertFalse(secure.is_encrypted("plain"))
        self.assertFalse(secure.is_encrypted(""))
        self.assertFalse(secure.is_encrypted(None))


class TestFernetRoundTrip(unittest.TestCase):
    """Fernet 真实加解密往返一致性（不再 mock crypt 层）。"""

    def test_round_trip(self) -> None:
        cipher = secure.encrypt_text(PLAIN)
        self.assertTrue(cipher.startswith(secure.FERNET_PREFIX))
        self.assertEqual(secure.decrypt_text(cipher), PLAIN)

    def test_round_trip_unicode(self) -> None:
        """中文 Cookie 值往返一致（UTF-8）。"""
        plain = "_m_h5_tk=中文字符串_1700000000000"
        self.assertEqual(secure.decrypt_text(secure.encrypt_text(plain)), plain)

    def test_decrypt_plaintext_passthrough(self) -> None:
        """无前缀输入原样返回（兼容存量明文）。"""
        self.assertEqual(secure.decrypt_text("plain-cookie"), "plain-cookie")
        self.assertEqual(secure.decrypt_text(""), "")

    def test_encrypt_empty_returns_empty(self) -> None:
        self.assertEqual(secure.encrypt_text(""), "")
        self.assertEqual(secure.encrypt_text(None), "")

    def test_dpapi_prefix_undecryptable(self) -> None:
        """dpapi1: 遗留密文 → 返回空串 + warning「请重新登录」。"""
        with self.assertLogs("xianyu_alert.secure", level="WARNING") as logs:
            self.assertEqual(secure.decrypt_text(secure.PREFIX + "QUJDREVGRw=="), "")
        self.assertTrue(any("请重新登录" in line for line in logs.output))

    def test_fernet_corrupted_returns_empty(self) -> None:
        """fernet1: 损坏密文 → 安全返回空串，不抛异常。"""
        with self.assertLogs("xianyu_alert.secure", level="WARNING"):
            self.assertEqual(secure.decrypt_text(secure.FERNET_PREFIX + "!!!not-base64!!!"), "")

    def test_wrong_key_returns_empty(self) -> None:
        """密钥被替换后旧密文不可解 → 返回空串（提示重登），不崩。"""
        cipher = secure.encrypt_text(PLAIN)
        with tempfile.TemporaryDirectory() as tmp:
            other_key = os.path.join(tmp, "other.key")
            with open(other_key, "wb") as fp:
                # 写入「另一个合法密钥」，解密旧密文必然失败
                fp.write(b"cGJzLWtleS1vbmx5LWZvci10ZXN0aW5nLXB1cnBvc2VzLWhhbmRh==")
            secure.set_key_file(other_key)
            try:
                with self.assertLogs("xianyu_alert.secure", level="WARNING"):
                    self.assertEqual(secure.decrypt_text(cipher), "")
            finally:
                _reset_key()


class TestKeyFile(unittest.TestCase):
    """密钥文件：生成 / 复用 / 权限 / 损坏。"""

    def test_key_created_once_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = os.path.join(tmp, "secret.key")
            secure.set_key_file(key_path)
            try:
                secure.encrypt_text("x")
                self.assertTrue(os.path.isfile(key_path))
                first = open(key_path, "rb").read()
                secure.set_key_file(key_path)  # 重置缓存强制重读
                secure.encrypt_text("y")
                self.assertEqual(open(key_path, "rb").read(), first)
                # 同一密钥往返一致
                self.assertEqual(secure.decrypt_text(secure.encrypt_text("y")), "y")
            finally:
                _reset_key()

    def test_posix_permission_0600(self) -> None:
        if os.name != "posix":  # pragma: no cover - Windows 无 POSIX 权限语义
            self.skipTest("非 POSIX 环境，跳过权限测试")
        with tempfile.TemporaryDirectory() as tmp:
            key_path = os.path.join(tmp, "secret.key")
            secure.set_key_file(key_path)
            try:
                secure.encrypt_text("x")
                self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)
            finally:
                _reset_key()

    def test_corrupt_key_file_safe_failure(self) -> None:
        """密钥文件内容损坏 → 加密降级明文、解密返回空串，绝不抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            bad_key = os.path.join(tmp, "bad.key")
            with open(bad_key, "wb") as fp:
                fp.write(b"this is not a valid fernet key at all")
            secure.set_key_file(bad_key)
            try:
                with self.assertLogs("xianyu_alert.secure", level="WARNING"):
                    self.assertEqual(secure.encrypt_text(PLAIN), PLAIN)  # 降级明文
                    self.assertEqual(secure.decrypt_text(secure.FERNET_PREFIX + "QUJD"), "")  # 返回空
            finally:
                _reset_key()


class TestEncryptedSave(unittest.TestCase):
    """save_cookies_encrypted 高电平保存测试（Fernet 真实加密）。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")
        _write_yaml(self.config_path, {"monitor": {"cookies": "old", "interval_seconds": 60}})

    def test_writes_prefix_and_flag(self) -> None:
        """加密保存：写入 fernet1: 密文 + cookies_encrypted 标记。"""
        with mock.patch.object(secure, "encrypt_text", return_value=secure.FERNET_PREFIX + "Zm9vYmFy"):
            save_cookies_encrypted(self.config_path, PLAIN)
        with open(self.config_path, "r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
        self.assertTrue(data["monitor"]["cookies"].startswith(secure.FERNET_PREFIX))
        self.assertTrue(data["monitor"]["cookies_encrypted"])
        self.assertEqual(data["monitor"]["interval_seconds"], 60)  # 其它字段保留

    def test_degraded_save_no_flag(self) -> None:
        """加密不可用（降级明文）时不写加密标记。"""
        with mock.patch.object(secure, "encrypt_text", side_effect=lambda p: p):
            save_cookies_encrypted(self.config_path, PLAIN)
        with open(self.config_path, "r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
        self.assertEqual(data["monitor"]["cookies"], PLAIN)
        self.assertNotIn("cookies_encrypted", data["monitor"])

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            save_cookies_encrypted(self.config_path, "   ")


class TestEnsureEncrypted(unittest.TestCase):
    """ensure_cookie_encrypted 存量明文迁移测试（Fernet）。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")

    def test_migrates_plaintext_to_encrypted(self) -> None:
        """明文 Cookie 就地转为 fernet1: 密文，其它字段保留。"""
        _write_yaml(self.config_path, {"monitor": {"cookies": "plain-value", "interval_seconds": 60}})
        with self.assertLogs("xianyu_alert.cookie", level="INFO"):
            migrated = ensure_cookie_encrypted(self.config_path)
        self.assertTrue(migrated)
        with open(self.config_path, "r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
        self.assertTrue(data["monitor"]["cookies"].startswith(secure.FERNET_PREFIX))
        self.assertTrue(data["monitor"]["cookies_encrypted"])
        self.assertEqual(data["monitor"]["interval_seconds"], 60)
        # 迁移后往返一致
        self.assertEqual(secure.decrypt_text(data["monitor"]["cookies"]), "plain-value")

    def test_already_encrypted_noop(self) -> None:
        _write_yaml(self.config_path, {"monitor": {"cookies": secure.FERNET_PREFIX + "AAA"}})
        self.assertFalse(ensure_cookie_encrypted(self.config_path))

    def test_legacy_dpapi_noop(self) -> None:
        """dpapi1: 遗留密文不迁移（不可解密，保持原样避免破坏）。"""
        _write_yaml(self.config_path, {"monitor": {"cookies": secure.PREFIX + "AAA"}})
        self.assertFalse(ensure_cookie_encrypted(self.config_path))

    def test_empty_cookie_noop(self) -> None:
        _write_yaml(self.config_path, {"monitor": {"cookies": ""}})
        self.assertFalse(ensure_cookie_encrypted(self.config_path))

    def test_missing_file_noop(self) -> None:
        self.assertFalse(ensure_cookie_encrypted(os.path.join(self.tmpdir.name, "nope.yaml")))

    def test_degraded_no_migration(self) -> None:
        """加密不可用（降级明文）时不迁移、不抛异常。"""
        _write_yaml(self.config_path, {"monitor": {"cookies": "plain"}})
        with mock.patch.object(secure, "encrypt_text", side_effect=lambda p: p):
            self.assertFalse(ensure_cookie_encrypted(self.config_path))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
