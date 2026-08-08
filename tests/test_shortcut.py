"""shortcut 模块单元测试：PowerShell 命令构造与转义、subprocess 调用形态。

对齐 v3 设计文档 A4/C11：路径含中文/空格/`$`/`"` 时不报错不注入；
subprocess 一律参数化列表调用。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import shortcut  # noqa: E402


class TestPsEscape(unittest.TestCase):
    """PowerShell 字符串转义正确性。"""

    def test_dollar_quote_escaped(self) -> None:
        escaped = shortcut._ps_escape(r'C:\My $Folder\闲鱼 "工具".exe')
        # 所有裸双引号都被反引号转义
        self.assertNotIn('"', escaped.replace('`"', ""))
        self.assertIn("`$", escaped)
        self.assertIn('`"', escaped)

    def test_backtick_escaped(self) -> None:
        escaped = shortcut._ps_escape(r"C:\a`b\c")
        self.assertIn("``", escaped)

    def test_plain_text_unchanged(self) -> None:
        self.assertEqual(shortcut._ps_escape("C:/plain/path"), "C:/plain/path")


class TestBuildScript(unittest.TestCase):
    """PowerShell 脚本构造（关键字段已转义）。"""

    def test_script_contains_escaped_values(self) -> None:
        script = shortcut.build_powershell_script(
            target=r"C:\My $Folder\app.exe",
            args="--gui",
            workdir=r"C:\My $Folder",
            lnk=r"C:\Users\fun\Desktop\闲鱼 工具.lnk",
            icon=r"C:\My $Folder\icon.ico",
        )
        self.assertIn("`$", script)  # $ 被转义
        self.assertIn('TargetPath = "C:\\My `$Folder\\app.exe"', script)
        self.assertIn('Arguments = "--gui"', script)
        self.assertIn("CreateShortcut", script)
        self.assertIn("IconLocation", script)
        self.assertIn("$lnk.Save()", script)

    def test_quote_in_target_escaped(self) -> None:
        script = shortcut.build_powershell_script(
            target=r'C:\we"ird\app.exe',
            args="",
            workdir=r"C:\we" + '`' + r'"ird',
            lnk=r"C:\x.lnk",
        )
        # 目标里的双引号被反引号转义，不会提前结束 PowerShell 字符串
        self.assertIn('we`"ird', script)


class TestPlatformSupport(unittest.TestCase):
    """平台判断（macOS 适配设计文档 §3.4）：非 Windows 显式不支持。"""

    def test_supported_true_on_win32(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(shortcut.supported())

    def test_supported_false_on_darwin(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertFalse(shortcut.supported())

    def test_supported_false_on_linux(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(shortcut.supported())

    def test_create_shortcut_none_on_darwin(self) -> None:
        """macOS 上 create_shortcut 直接返回 None（不调用 PowerShell）。"""
        called: dict = {"run": False}

        def fake_run(cmd, **kwargs):  # pragma: no cover - 不应被调用
            called["run"] = True
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(shortcut, "desktop_dir", return_value=tempfile.gettempdir()), \
             self.assertLogs("xianyu_alert.shortcut", level="WARNING"):
            result = shortcut.create_shortcut(exe_path=r"C:\x.exe", runner=fake_run)
        self.assertIsNone(result)
        self.assertFalse(called["run"], "非 Windows 平台不应执行 PowerShell")


class TestCreateShortcut(unittest.TestCase):
    """create_shortcut 主流程（注入 fake runner，不真调 PowerShell）。"""

    def test_success_returns_lnk_path_and_list_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lnk_path = os.path.join(tmp, "闲鱼低价提醒工具.lnk")
            with open(lnk_path, "w", encoding="utf-8") as fp:
                fp.write("")
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                return subprocess.CompletedProcess(cmd, 0)

            with mock.patch.object(shortcut, "desktop_dir", return_value=tmp), \
                 mock.patch("os.path.isfile", return_value=True):
                result = shortcut.create_shortcut(
                    exe_path=r"C:\My $Folder\app.exe", runner=fake_run
                )

            self.assertEqual(result, lnk_path)
            # subprocess 必须是参数化列表（杜绝 shell 拼接注入）
            self.assertIsInstance(captured["cmd"], list)
            self.assertEqual(captured["cmd"][0], "powershell")
            self.assertIn("-Command", captured["cmd"])
            script = captured["cmd"][-1]
            self.assertIn("`$", script)

    def test_non_frozen_target_is_pythonw(self) -> None:
        """源码模式：目标指向 pythonw.exe（无控制台窗口），参数为 run_gui.pyw。"""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(sys, "frozen", False, create=True), \
             mock.patch.object(shortcut, "desktop_dir", return_value=tempfile.gettempdir()), \
             mock.patch("os.path.isfile", return_value=True):
            result = shortcut.create_shortcut(runner=fake_run)

        self.assertIsNotNone(result)
        self.assertIn("pythonw.exe", captured["cmd"][-1])
        self.assertIn("run_gui.pyw", captured["cmd"][-1])

    def test_frozen_target_is_sys_executable(self) -> None:
        """frozen 模式：目标指向当前 exe（sys.executable）。"""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)

        exe = os.path.join(tempfile.gettempdir(), "xianyu_alert.exe")
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", exe), \
             mock.patch.object(shortcut, "desktop_dir", return_value=tempfile.gettempdir()), \
             mock.patch("os.path.isfile", return_value=True):
            result = shortcut.create_shortcut(runner=fake_run)

        self.assertIsNotNone(result)
        self.assertIn(exe, captured["cmd"][-1])

    def test_failure_returns_none(self) -> None:
        """PowerShell 调用失败 → 返回 None（不抛异常）。"""

        def broken_run(cmd, **kwargs):
            raise OSError("powershell 不存在")

        with mock.patch.object(shortcut, "desktop_dir", return_value=tempfile.gettempdir()), \
             mock.patch("os.path.isfile", return_value=True):
            self.assertIsNone(shortcut.create_shortcut(runner=broken_run))

    def test_default_name(self) -> None:
        """默认快捷方式名含 .lnk 后缀。"""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(shortcut, "desktop_dir", return_value=tempfile.gettempdir()), \
             mock.patch("os.path.isfile", return_value=True):
            shortcut.create_shortcut(exe_path=r"C:\x.exe", runner=fake_run)

        self.assertIn("闲鱼低价提醒工具.lnk", captured["cmd"][-1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
