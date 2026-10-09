"""CLI 子命令与错误路径测试（v1.10.6，M14 补强）。

重评显示 `xianyu_alert/cli.py` 覆盖率 70%，缺口集中在：
    cmd_once / cmd_list / cmd_shortcut / cmd_secure / cmd_run 的**实际执行**，
    以及 main() 的**三条错误出口**（ConfigError→2 / KeyboardInterrupt→0 / 未预期异常→1）。

这些恰恰是"用户真按下去会发生什么"的路径，属于必须覆盖的部分。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from xianyu_alert import cli  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_config(tmp: str, **monitor_extra: object) -> str:
    """最小可用配置（mock 抓取器 + 临时库）。"""
    monitor: dict = {"interval_seconds": 1}
    monitor.update(monitor_extra)
    cfg = {
        "keywords": [{"keyword": "Switch", "max_price": 1000}],
        "monitor": monitor,
        "fetcher": {"type": "mock", "mock_products_per_round": 3},
        "storage": {"path": os.path.join(tmp, "state", "x.db")},
        "notify": {"channels": []},
    }
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(cfg, fp, allow_unicode=True, sort_keys=False)
    return path


class CliCase(unittest.TestCase):
    """公共夹具。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.path = make_config(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def last_json(text: str) -> dict:
        for line in reversed(text.strip().splitlines()):
            if line.strip().startswith("{"):
                return json.loads(line)
        raise AssertionError("输出中没有 JSON：" + text[:120])


class TestOnceAndList(CliCase):
    """once / list 的真实执行（不只是参数解析）。"""

    def test_once_runs_a_round(self) -> None:
        code, out, _err = self.run_cli(["once", "-c", self.path, "--json"])
        self.assertEqual(code, 0)
        payload = self.last_json(out)
        self.assertTrue(payload["ok"])
        self.assertIn("notified", payload)

    def test_list_empty(self) -> None:
        code, out, _err = self.run_cli(["list", "-c", self.path])
        self.assertEqual(code, 0)
        self.assertIn("暂无已提醒记录", out)

    def test_list_with_records(self) -> None:
        storage = Storage(os.path.join(self.tmp, "state", "x.db"))
        try:
            product = Product("810000000001", "测试商品", 129.0, "https://x/item?id=810000000001", keyword="Switch")
            storage.save_seen(product)
            storage.mark_notified(product)
        finally:
            storage.close()
        code, out, _err = self.run_cli(["list", "-c", self.path])
        self.assertEqual(code, 0)
        self.assertIn("测试商品", out)
        self.assertIn("129.00", out)


class TestShortcut(CliCase):
    """shortcut 子命令（非 Windows 平台应给出明确说明而不是崩）。"""

    @unittest.skipIf(sys.platform == "win32", "Windows 上会真的去创建快捷方式，走另一条契约")
    def test_shortcut_on_non_windows_reports_clearly(self) -> None:
        """非 Windows：退出码 1 + 明确说明（而不是静默"成功"）。

        这是 CLI 的既有契约 —— "没做成" 就该是非 0，否则脚本无法判断。
        """
        code, out, err = self.run_cli(["shortcut", "-c", self.path])
        self.assertEqual(code, 1)
        self.assertIn("仅 Windows", out + err)

    @unittest.skipUnless(sys.platform == "win32", "仅 Windows 才会真的创建")
    def test_shortcut_on_windows_attempts_creation(self) -> None:
        """Windows：真的会去创建快捷方式。

        CI 的 Windows runner 没有交互桌面会话，创建可能失败；无论成败都要求
        **有明确输出**，失败时为非 0 —— 不允许静默通过。
        """
        code, out, err = self.run_cli(["shortcut", "-c", self.path])
        self.assertIn(code, (0, 1))
        self.assertTrue((out + err).strip(), "无论成败都要有输出")
        if code != 0:
            self.assertIn("失败", out + err)


class TestSecureCommand(CliCase):
    """secure rotate 的 CLI 路径。"""

    def test_secure_status_json(self) -> None:
        code, out, _err = self.run_cli(["secure", "status", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("key_exists", self.last_json(out))

    def test_secure_rotate_without_ciphertext(self) -> None:
        code, out, _err = self.run_cli(["secure", "rotate", "-c", self.path, "--json"])
        self.assertEqual(code, 0, out)
        payload = self.last_json(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["rotated"], 0, "配置里没有密文时应是 no-op")


class TestMainErrorPaths(CliCase):
    """main() 的三条错误出口。"""

    def test_config_error_exit_code_2(self) -> None:
        bad = os.path.join(self.tmp, "bad.yaml")
        with open(bad, "w", encoding="utf-8") as fp:
            fp.write("keywords: []\n")
        code, _out, _err = self.run_cli(["list", "-c", bad])
        self.assertEqual(code, 2, "配置错误应返回 2")

    def test_keyboard_interrupt_exit_code_0(self) -> None:
        with mock.patch.object(cli, "cmd_list", side_effect=KeyboardInterrupt):
            code, _out, _err = self.run_cli(["list", "-c", self.path])
        self.assertEqual(code, 0, "用户中断应视为正常退出")

    def test_unexpected_error_exit_code_1(self) -> None:
        with mock.patch.object(cli, "cmd_list", side_effect=RuntimeError("boom")):
            code, _out, _err = self.run_cli(["list", "-c", self.path])
        self.assertEqual(code, 1, "未预期异常应返回 1")

    def test_no_command_prints_help(self) -> None:
        code, out, _err = self.run_cli([])
        self.assertEqual(code, 1)
        self.assertIn("usage", out.lower())

    def test_instance_lock_conflict_returns_2(self) -> None:
        """run/once/gui 走单实例锁；冲突时返回 2 且不抢锁。"""
        with mock.patch.object(cli, "acquire_instance_lock", return_value=None), \
             mock.patch.object(cli, "lock_holder_pid", return_value="12345"):
            code, _out, err = self.run_cli(["once", "-c", self.path])
        self.assertEqual(code, 2)
        self.assertIn("已有实例运行中", err)


class TestRunCommand(CliCase):
    """run 子命令（用 max_rounds 限制轮数，避免真的死循环）。"""

    def test_run_max_rounds(self) -> None:
        """跑满指定轮数后正常退出（运行日志走 logging，这里断言退出码与通知输出）。"""
        code, out, _err = self.run_cli(["run", "-c", self.path, "--max-rounds", "1"])
        self.assertEqual(code, 0, out)
        self.assertIn("闲鱼低价提醒", out, "mock 抓取器应产出可通知的商品")


if __name__ == "__main__":
    unittest.main()
