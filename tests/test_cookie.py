"""cookie 模块单元测试：纯函数 + 配置写回 + Playwright 缺失降级。

不真跑 Playwright；相关路径用 mock 模拟。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import cli  # noqa: E402
from xianyu_alert.cookie import (  # noqa: E402
    PlaywrightUnavailable,
    acquire_via_playwright,
    acquire_via_prompt,
    build_cookie_header,
    save_cookies_to_config,
)

SAMPLE_CONFIG = {
    "keywords": [{"keyword": "Switch", "max_price": 1000}],
    "monitor": {"interval_seconds": 60, "user_agent": "", "cookies": ""},
    "fetcher": {"type": "mock"},
    "storage": {"path": "state/xianyu_alert.db"},
    "notify": {"channels": [{"type": "console"}]},
}


def _write_yaml(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)


def _read_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fp:
        return yaml.safe_load(fp)


class TestBuildCookieHeader(unittest.TestCase):
    """build_cookie_header 纯函数测试。"""

    def test_basic(self) -> None:
        cookies = [
            {"name": "_m_h5_tk", "value": "abc"},
            {"name": "cookie2", "value": "xyz"},
        ]
        self.assertEqual(build_cookie_header(cookies), "_m_h5_tk=abc; cookie2=xyz")

    def test_empty_list(self) -> None:
        self.assertEqual(build_cookie_header([]), "")
        self.assertEqual(build_cookie_header(None), "")  # type: ignore[arg-type]

    def test_skips_invalid_entries(self) -> None:
        cookies = [
            {"name": "a", "value": "1", "domain": ".goofish.com", "path": "/"},
            {"value": "no-name"},          # 缺 name，应跳过
            {"name": "", "value": "x"},    # 空 name，应跳过
            "not-a-dict",                  # 非 dict，应跳过
            {"name": "b"},                 # 缺 value，按空值处理
        ]
        self.assertEqual(build_cookie_header(cookies), "a=1; b=")


class TestSaveCookiesToConfig(unittest.TestCase):
    """save_cookies_to_config 写回测试（临时 YAML 文件）。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")
        _write_yaml(self.config_path, SAMPLE_CONFIG)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_updates_cookies_and_preserves_other_fields(self) -> None:
        save_cookies_to_config(self.config_path, "_m_h5_tk=test123; cookie2=val")
        data = _read_yaml(self.config_path)

        # monitor.cookies 已更新
        self.assertEqual(data["monitor"]["cookies"], "_m_h5_tk=test123; cookie2=val")
        # 其它字段全部保留
        self.assertEqual(data["keywords"], SAMPLE_CONFIG["keywords"])
        self.assertEqual(data["monitor"]["interval_seconds"], 60)
        self.assertEqual(data["fetcher"], SAMPLE_CONFIG["fetcher"])
        self.assertEqual(data["storage"], SAMPLE_CONFIG["storage"])
        self.assertEqual(data["notify"], SAMPLE_CONFIG["notify"])

    def test_strips_whitespace(self) -> None:
        save_cookies_to_config(self.config_path, "  a=1; b=2  ")
        self.assertEqual(_read_yaml(self.config_path)["monitor"]["cookies"], "a=1; b=2")

    def test_empty_cookie_raises(self) -> None:
        with self.assertRaises(ValueError):
            save_cookies_to_config(self.config_path, "   ")

    def test_missing_monitor_section_created(self) -> None:
        """原文件没有 monitor 节点时应自动创建。"""
        _write_yaml(self.config_path, {"keywords": SAMPLE_CONFIG["keywords"]})
        save_cookies_to_config(self.config_path, "a=1")
        data = _read_yaml(self.config_path)
        self.assertEqual(data["monitor"]["cookies"], "a=1")
        self.assertEqual(data["keywords"], SAMPLE_CONFIG["keywords"])

    def test_missing_file_creates_new(self) -> None:
        """配置文件不存在时应新建仅含 monitor.cookies 的文件。"""
        new_path = os.path.join(self.tmpdir.name, "new.yaml")
        save_cookies_to_config(new_path, "a=1")
        self.assertEqual(_read_yaml(new_path), {"monitor": {"cookies": "a=1"}})

    def test_result_loadable_by_load_config(self) -> None:
        """写回后的文件应仍能被 config.load_config 正常加载。"""
        from xianyu_alert.config import load_config

        save_cookies_to_config(self.config_path, "_m_h5_tk=tk; cookie2=c2")
        config = load_config(self.config_path)
        self.assertEqual(config.monitor.cookies, "_m_h5_tk=tk; cookie2=c2")
        self.assertEqual(config.keywords[0].keyword, "Switch")


class TestAcquireViaPrompt(unittest.TestCase):
    """acquire_via_prompt 交互测试（mock input）。"""

    @mock.patch("builtins.input", return_value="  _m_h5_tk=abc; cookie2=x  ")
    def test_returns_stripped_input(self, _mock_input: mock.MagicMock) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = acquire_via_prompt()
        self.assertEqual(result, "_m_h5_tk=abc; cookie2=x")

    @mock.patch("builtins.input", return_value="   ")
    def test_empty_input_raises(self, _mock_input: mock.MagicMock) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertRaises(ValueError):
            acquire_via_prompt()

    @mock.patch("builtins.input", return_value="cookie2=only")
    def test_missing_key_cookie_warns_but_accepts(self, _mock_input: mock.MagicMock) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertLogs("xianyu_alert.cookie", level="WARNING"):
            result = acquire_via_prompt()
        self.assertEqual(result, "cookie2=only")


class TestAcquireViaPlaywright(unittest.TestCase):
    """acquire_via_playwright 的 import 降级测试（不真跑浏览器）。"""

    def test_raises_playwright_unavailable_when_missing(self) -> None:
        """模拟 playwright 未安装：应抛 PlaywrightUnavailable 且提示安装命令。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object):
            if name.startswith("playwright"):
                raise ImportError("No module named 'playwright'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(PlaywrightUnavailable) as ctx:
                acquire_via_playwright(timeout=1)
        self.assertIn("pip install playwright", str(ctx.exception))
        self.assertIn("playwright install chromium", str(ctx.exception))


class TestPoolUsableAndResolve(unittest.TestCase):
    """v1.8：pool_usable_cookies / resolve_cookie_for_round 健康过滤（A5/C11）。"""

    @staticmethod
    def _expired_cookie() -> str:
        return "_m_h5_tk=abc_1000000000000; cookie2=v"  # 2001 年时间戳，必然过期

    @staticmethod
    def _expiring_cookie() -> str:
        import time

        ts = int(time.time() * 1000) - 23 * 3600 * 1000  # 剩余 < 1h
        return f"_m_h5_tk=abc_{ts}; cookie2=v"

    def _pool(self, items: list):
        """把 {name, cookie, enabled} 字典列表构造为 CookiePoolItem 列表（与 config 一致）。"""
        from xianyu_alert.config import CookiePoolItem

        return [CookiePoolItem(**item) for item in items]

    def _monitor(self, pool_items: list, single: str = ""):
        from types import SimpleNamespace

        return SimpleNamespace(cookie_pool=self._pool(pool_items), cookies=single)

    def test_pool_usable_only_ok_and_expiring(self) -> None:
        """pool_usable_cookies 保序且只含 ok/expiring；停用/空/过期被排除。"""
        from xianyu_alert.cookie import pool_usable_cookies

        ok_cookie = "_m_h5_tk=t; c=1"                      # 无时间戳 → ok（历史样本兼容）
        expiring = self._expiring_cookie()
        expired = self._expired_cookie()
        pool = self._pool(
            [
                {"name": "a", "cookie": ok_cookie, "enabled": True},
                {"name": "b", "cookie": expiring, "enabled": True},
                {"name": "c", "cookie": expired, "enabled": True},
                {"name": "d", "cookie": "no_token_cookie", "enabled": True},
                {"name": "e", "cookie": ok_cookie, "enabled": False},  # 停用 → 排除
                {"name": "f", "cookie": "", "enabled": True},          # 空 → 排除
            ]
        )
        result = pool_usable_cookies(pool)
        self.assertEqual(result, [ok_cookie, expiring])

    def test_resolve_skips_expired_and_rotates_healthy(self) -> None:
        """池混入 expired/no_token 条目时只返回有效条目（保序轮换）。"""
        from xianyu_alert.cookie import resolve_cookie_for_round

        ok_cookie = "_m_h5_tk=t; c=1"
        expiring = self._expiring_cookie()
        mon = self._monitor(
            [
                {"name": "expired1", "cookie": self._expired_cookie(), "enabled": True},
                {"name": "ok1", "cookie": ok_cookie, "enabled": True},
                {"name": "expiring1", "cookie": expiring, "enabled": True},
                {"name": "no_token", "cookie": "cookie2=x", "enabled": True},
            ]
        )
        # 健康条目 = [ok_cookie, expiring] → 轮换
        self.assertEqual(resolve_cookie_for_round(mon, 0), ok_cookie)
        self.assertEqual(resolve_cookie_for_round(mon, 1), expiring)
        self.assertEqual(resolve_cookie_for_round(mon, 2), ok_cookie)  # 取模循环

    def test_resolve_all_expired_falls_back_to_single_healthy(self) -> None:
        """池全部失效 → 回退单值（单值健康才用）。"""
        from xianyu_alert.cookie import resolve_cookie_for_round

        mon = self._monitor(
            [
                {"name": "e1", "cookie": self._expired_cookie(), "enabled": True},
                {"name": "e2", "cookie": "cookie2=no_token", "enabled": True},
            ],
            single="_m_h5_tk=t; single=1",
        )
        self.assertEqual(resolve_cookie_for_round(mon, 0), "_m_h5_tk=t; single=1")

    def test_resolve_all_invalid_returns_empty(self) -> None:
        """池全部失效且单值也不健康 → 返回空串 + C14 warning。"""
        from xianyu_alert.cookie import resolve_cookie_for_round

        mon = self._monitor(
            [
                {"name": "e1", "cookie": self._expired_cookie(), "enabled": True},
                {"name": "e2", "cookie": "cookie2=no_token", "enabled": True},
            ],
            single="cookie2=only",
        )
        with self.assertLogs("xianyu_alert.cookie", level="WARNING") as logs:
            result = resolve_cookie_for_round(mon, 0)
        self.assertEqual(result, "")
        self.assertTrue(any("本轮抓取将失败" in line for line in logs.output))

    def test_resolve_empty_pool_returns_single(self) -> None:
        """池为空 → 回退单值（向后兼容）。"""
        from xianyu_alert.cookie import resolve_cookie_for_round

        mon = self._monitor([], single="_m_h5_tk=t; s=1")
        self.assertEqual(resolve_cookie_for_round(mon, 0), "_m_h5_tk=t; s=1")
        self.assertEqual(resolve_cookie_for_round(self._monitor([], single=""), 0), "")


class TestSaveCookiesValidated(unittest.TestCase):
    """v1.8：save_cookies_validated 校验后保存（A3/C15/C20）。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")
        _write_yaml(self.config_path, SAMPLE_CONFIG)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_valid_cookie_saves(self) -> None:
        """无时间戳 `_m_h5_tk=t`（历史样本）按 ok 处理，正常保存。"""
        from xianyu_alert.cookie import save_cookies_validated

        save_cookies_validated(self.config_path, "_m_h5_tk=t; c=1")
        self.assertEqual(_read_yaml(self.config_path)["monitor"]["cookies"], "_m_h5_tk=t; c=1")

    def test_expired_cookie_rejected_and_not_saved(self) -> None:
        """过期 Cookie → ValueError 且 config 不变。"""
        from xianyu_alert.cookie import save_cookies_validated

        before = _read_yaml(self.config_path)
        with self.assertRaises(ValueError) as ctx:
            save_cookies_validated(self.config_path, "_m_h5_tk=abc_1000000000000; c=1")
        self.assertIn("已过期", str(ctx.exception))
        self.assertEqual(_read_yaml(self.config_path), before)

    def test_no_token_cookie_rejected_and_not_saved(self) -> None:
        """缺 _m_h5_tk → ValueError 且 config 不变。"""
        from xianyu_alert.cookie import save_cookies_validated

        before = _read_yaml(self.config_path)
        with self.assertRaises(ValueError) as ctx:
            save_cookies_validated(self.config_path, "cookie2=only")
        self.assertIn("缺少", str(ctx.exception))
        self.assertEqual(_read_yaml(self.config_path), before)

    def test_empty_cookie_rejected(self) -> None:
        """空串 → ValueError（missing）。"""
        from xianyu_alert.cookie import save_cookies_validated

        with self.assertRaises(ValueError):
            save_cookies_validated(self.config_path, "   ")


class TestSaveCookiesValidatedEncrypted(unittest.TestCase):
    """v1.8 Web：save_cookies_validated_encrypted 校验 → 内存加密 → 单次原子写盘。

    QA FINDING-1 回归：磁盘上**不存在明文持久化窗口**——加密不可用 / 失败时
    config.yaml 保持原样，绝不写明文（设计 §4.2 / 共享知识 7）。
    """

    def setUp(self) -> None:
        from xianyu_alert import secure

        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")
        _write_yaml(self.config_path, SAMPLE_CONFIG)
        # 独立密钥文件，避免触碰默认 data_dir 的密钥
        secure.set_key_file(os.path.join(self.tmpdir.name, "secret.key"))

    def tearDown(self) -> None:
        from xianyu_alert import secure

        secure.set_key_file(None)
        self.tmpdir.cleanup()

    def _load(self) -> dict:
        return _read_yaml(self.config_path)

    def test_valid_cookie_saves_encrypted(self) -> None:
        """无时间戳 `_m_h5_tk=t`（历史样本）按 ok 处理 → fernet1: 密文落盘且无明文。"""
        from xianyu_alert.cookie import save_cookies_validated_encrypted

        save_cookies_validated_encrypted(self.config_path, "_m_h5_tk=t; c=1")
        data = self._load()
        cookies = str(data["monitor"]["cookies"])
        self.assertTrue(cookies.startswith("fernet1:"))
        self.assertTrue(data["monitor"]["cookies_encrypted"])
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertNotIn("_m_h5_tk=t; c=1", raw.replace("fernet1:", ""))

    def test_valid_cookie_with_timestamp_saves_encrypted(self) -> None:
        """带当前时间戳的 Cookie → fernet1: 密文落盘。"""
        import time as _time

        from xianyu_alert.cookie import save_cookies_validated_encrypted

        cookie = f"_m_h5_tk=abc_{int(_time.time() * 1000)}; cookie2=xyz"
        save_cookies_validated_encrypted(self.config_path, cookie)
        cookies = str(self._load()["monitor"]["cookies"])
        self.assertTrue(cookies.startswith("fernet1:"))
        self.assertNotIn(cookie, open(self.config_path, "r", encoding="utf-8").read())

    def test_expired_cookie_rejected_and_not_saved(self) -> None:
        """过期 Cookie → ValueError 且 config 不变。"""
        from xianyu_alert.cookie import save_cookies_validated_encrypted

        before = self._load()
        with self.assertRaises(ValueError) as ctx:
            save_cookies_validated_encrypted(self.config_path, "_m_h5_tk=abc_1000000000000; c=1")
        self.assertIn("已过期", str(ctx.exception))
        self.assertEqual(self._load(), before)

    def test_no_token_cookie_rejected_and_not_saved(self) -> None:
        """缺 _m_h5_tk → ValueError 且 config 不变。"""
        from xianyu_alert.cookie import save_cookies_validated_encrypted

        before = self._load()
        with self.assertRaises(ValueError) as ctx:
            save_cookies_validated_encrypted(self.config_path, "cookie2=only")
        self.assertIn("缺少", str(ctx.exception))
        self.assertEqual(self._load(), before)

    def test_empty_cookie_rejected(self) -> None:
        """空串 → ValueError（missing）。"""
        from xianyu_alert.cookie import save_cookies_validated_encrypted

        with self.assertRaises(ValueError):
            save_cookies_validated_encrypted(self.config_path, "   ")

    def test_encrypt_degraded_rejected_no_plaintext(self) -> None:
        """加密降级返回明文（cryptography 缺失）→ ValueError 拒绝保存，config 不变。"""
        from xianyu_alert import secure

        from xianyu_alert.cookie import save_cookies_validated_encrypted

        before = self._load()
        with mock.patch.object(
            secure, "encrypt_text", return_value="_m_h5_tk=t; c=1"
        ):
            with self.assertRaises(ValueError) as ctx:
                save_cookies_validated_encrypted(self.config_path, "_m_h5_tk=t; c=1")
        self.assertIn("加密不可用", str(ctx.exception))
        self.assertEqual(self._load(), before)

    def test_encrypt_exception_no_write(self) -> None:
        """encrypt_text 抛异常（IO 级故障）→ 异常上抛，config 不变（无明文残留）。"""
        from xianyu_alert import secure

        from xianyu_alert.cookie import save_cookies_validated_encrypted

        before = self._load()
        with mock.patch.object(
            secure, "encrypt_text", side_effect=OSError("encrypt boom")
        ):
            with self.assertRaises(OSError):
                save_cookies_validated_encrypted(self.config_path, "_m_h5_tk=t; c=1")
        self.assertEqual(self._load(), before)

    def test_preserves_other_fields(self) -> None:
        """仅更新 monitor.cookies / cookies_encrypted，其余字段保留。"""
        from xianyu_alert.cookie import save_cookies_validated_encrypted

        save_cookies_validated_encrypted(self.config_path, "_m_h5_tk=t; c=1")
        data = self._load()
        self.assertEqual(data["keywords"], SAMPLE_CONFIG["keywords"])
        self.assertEqual(data["fetcher"], SAMPLE_CONFIG["fetcher"])
        self.assertEqual(data["notify"], SAMPLE_CONFIG["notify"])
        self.assertEqual(data["monitor"]["interval_seconds"], 60)


class TestCliLogin(unittest.TestCase):
    """cli login 子命令端到端测试（不触网、不开浏览器）。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")
        _write_yaml(self.config_path, SAMPLE_CONFIG)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_login_with_cookie_string(self) -> None:
        """脚本模式：--cookie-string 直接存盘。"""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(
                ["login", "--config", self.config_path, "--cookie-string", "_m_h5_tk=t; c=1"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(_read_yaml(self.config_path)["monitor"]["cookies"], "_m_h5_tk=t; c=1")
        self.assertIn("已写入", buffer.getvalue())

    def test_login_with_empty_cookie_string_fails(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["login", "--config", self.config_path, "--cookie-string", "  "])
        self.assertEqual(code, 2)
        self.assertEqual(_read_yaml(self.config_path)["monitor"]["cookies"], "")

    @mock.patch("xianyu_alert.cli.acquire_via_playwright")
    def test_login_playwright_success(self, mock_acquire: mock.MagicMock) -> None:
        """半自动模式：playwright 成功返回 Cookie 时应写盘。"""
        mock_acquire.return_value = "_m_h5_tk=auto; cookie2=pw"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["login", "--config", self.config_path])
        self.assertEqual(code, 0)
        self.assertEqual(
            _read_yaml(self.config_path)["monitor"]["cookies"], "_m_h5_tk=auto; cookie2=pw"
        )
        self.assertIn("_m_h5_tk", buffer.getvalue())

    @mock.patch("xianyu_alert.cli.acquire_via_prompt")
    @mock.patch("xianyu_alert.cli.acquire_via_playwright")
    def test_login_fallback_to_prompt(
        self, mock_acquire: mock.MagicMock, mock_prompt: mock.MagicMock
    ) -> None:
        """playwright 缺失时应打印安装提示并降级到粘贴模式。"""
        mock_acquire.side_effect = PlaywrightUnavailable(
            "未安装 Playwright。请先执行：pip install playwright / playwright install chromium"
        )
        mock_prompt.return_value = "_m_h5_tk=pasted"

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["login", "--config", self.config_path])

        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("pip install playwright", output)
        self.assertIn("手动粘贴", output)
        self.assertEqual(_read_yaml(self.config_path)["monitor"]["cookies"], "_m_h5_tk=pasted")

    @mock.patch("xianyu_alert.cli.acquire_via_playwright")
    def test_login_timeout_returns_nonzero(self, mock_acquire: mock.MagicMock) -> None:
        """登录超时应返回非 0 退出码且不改配置。"""
        from xianyu_alert.cookie import LoginTimeout

        mock_acquire.side_effect = LoginTimeout("登录超时，请重试或改用手动模式")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["login", "--config", self.config_path])
        self.assertEqual(code, 3)
        self.assertEqual(_read_yaml(self.config_path)["monitor"]["cookies"], "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
