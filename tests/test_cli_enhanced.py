"""CLI 增强（v1.9.3）测试：--json 输出 / config validate / cookie keepalive。

这些是"给人以外的消费者"用的接口（脚本、cron、NAS 巡检），因此重点验证：
    1. 输出必须是**可解析的 JSON**，且不含凭据明文/令牌哈希；
    2. 退出码语义必须明确（0 成功、1 用法或校验失败）；
    3. 写入型命令必须真正落盘且保留其它字段。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from xianyu_alert import cli  # noqa: E402

COOKIE = "cookie2=abc; unb=1; sgcookie=sg; _m_h5_tk=deadbeefcafe_1791559226130; havana_lgc_exp=1794133406207"


def make_config(tmp: str, **monitor_extra: object) -> str:
    """写一份最小可用配置（mock 抓取器 + 临时库）。"""
    monitor: dict = {"interval_seconds": 60, "cookies": COOKIE}
    monitor.update(monitor_extra)
    cfg = {
        "keywords": [{"keyword": "Switch", "max_price": 1000}],
        "monitor": monitor,
        "fetcher": {"type": "mock", "mock_products_per_round": 1},
        "storage": {"path": os.path.join(tmp, "state", "x.db")},
        "notify": {"channels": [{"type": "console"}]},
    }
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(cfg, fp, allow_unicode=True, sort_keys=False)
    return path


class CliTestCase(unittest.TestCase):
    """公共夹具：临时目录 + 标准输出捕获。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.path = make_config(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        """执行 CLI，返回 (退出码, stdout, stderr)。"""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def last_json(text: str) -> dict:
        """取最后一行 JSON（CLI 只打印一行，这里容忍日志前缀）。"""
        for line in reversed(text.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        raise AssertionError(f"输出中没有 JSON：{text[:200]!r}")


class TestConfigValidate(CliTestCase):
    """config validate。"""

    def test_json_ok(self) -> None:
        code, out, _ = self.run_cli(["config", "validate", "-c", self.path, "--json"])
        self.assertEqual(code, 0)
        data = self.last_json(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["keyword_count"], 1)
        self.assertEqual(data["fetcher_type"], "mock")
        self.assertIn("keepalive_enabled", data)

    def test_json_invalid_reports_reason(self) -> None:
        bad = os.path.join(self.tmp, "bad.yaml")
        with open(bad, "w", encoding="utf-8") as fp:
            fp.write("monitor:\n  interval_seconds: 不是数字\nkeywords: []\n")
        code, out, _ = self.run_cli(["config", "validate", "-c", bad, "--json"])
        self.assertEqual(code, 1)
        data = self.last_json(out)
        self.assertFalse(data["ok"])
        self.assertTrue(data["error"])

    def test_human_output_mentions_key_fields(self) -> None:
        code, out, _ = self.run_cli(["config", "validate", "-c", self.path])
        self.assertEqual(code, 0)
        self.assertIn("配置有效", out)
        self.assertIn("Switch", out)
        self.assertIn("空闲保活", out)


class TestCookieStatusJson(CliTestCase):
    """cookie status --json。"""

    def test_layers_and_no_secret_leak(self) -> None:
        code, out, _ = self.run_cli(["cookie", "status", "-c", self.path, "--json"])
        self.assertEqual(code, 0)
        data = self.last_json(out)
        self.assertEqual(len(data["diagnosis"]["layers"]), 4)
        self.assertIn("keepalive", data)
        self.assertEqual(data["pool_count"], 0)

    def test_pool_entries_are_masked_in_json(self) -> None:
        """有 Cookie 池时也要走通（曾被 ruff 抓到"局部导入晚于使用"的运行时陷阱）。"""
        path = make_config(self.tmp, cookie_pool=[
            {"name": "小号", "cookie": COOKIE, "enabled": True},
            {"name": "停用号", "cookie": COOKIE, "enabled": False},
        ])
        code, out, _ = self.run_cli(["cookie", "status", "-c", path, "--json"])
        self.assertEqual(code, 0)
        data = self.last_json(out)
        self.assertEqual(data["pool_count"], 2)
        self.assertEqual([p["name"] for p in data["pool"]], ["小号", "停用号"])
        self.assertFalse(data["pool"][1]["enabled"])
        self.assertNotIn("deadbeefcafe", out)

    def test_token_hash_not_leaked(self) -> None:
        _code, out, _ = self.run_cli(["cookie", "status", "-c", self.path, "--json"])
        # 令牌哈希与 cookie2 明文都不允许出现在输出里
        self.assertNotIn("deadbeefcafe", out)
        self.assertNotIn("cookie2=abc", out)


class TestListJson(CliTestCase):
    """list --json。"""

    def test_empty_returns_zero_total(self) -> None:
        code, out, _ = self.run_cli(["list", "-c", self.path, "--json"])
        self.assertEqual(code, 0)
        data = self.last_json(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["records"], [])


class TestCookieKeepalive(CliTestCase):
    """cookie keepalive：查看 / 开关 / 改间隔。"""

    def test_show_defaults(self) -> None:
        code, out, _ = self.run_cli(["cookie", "keepalive", "-c", self.path])
        self.assertEqual(code, 0)
        self.assertIn("空闲保活", out)

    def test_disable_persists_and_json(self) -> None:
        code, out, _ = self.run_cli(["cookie", "keepalive", "-c", self.path, "--disable", "--json"])
        self.assertEqual(code, 0)
        data = self.last_json(out)
        self.assertTrue(data["changed"])
        self.assertFalse(data["keepalive_enabled"])
        raw = yaml.safe_load(open(self.path, encoding="utf-8"))
        self.assertFalse(raw["monitor"]["keepalive_enabled"])
        # 其它字段必须原样保留（尤其是 Cookie 密文/明文形态）
        self.assertEqual(raw["monitor"]["cookies"], COOKIE)
        self.assertEqual(raw["keywords"][0]["keyword"], "Switch")

    def test_enable_with_interval(self) -> None:
        code, out, _ = self.run_cli(
            ["cookie", "keepalive", "-c", self.path, "--enable", "--interval", "900", "--json"]
        )
        self.assertEqual(code, 0)
        data = self.last_json(out)
        self.assertTrue(data["keepalive_enabled"])
        self.assertEqual(data["keepalive_interval_seconds"], 900)

    def test_rejects_too_short_interval(self) -> None:
        code, _out, err = self.run_cli(["cookie", "keepalive", "-c", self.path, "--interval", "10"])
        self.assertEqual(code, 1)
        self.assertIn("间隔过短", err)

    def test_conflicting_flags_rejected_by_argparse(self) -> None:
        # --enable/--disable 由 argparse 互斥组拦截（SystemExit 2），
        # 不会走到业务分支 —— 这里明确断言这一契约。
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["cookie", "keepalive", "-c", self.path, "--enable", "--disable"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
