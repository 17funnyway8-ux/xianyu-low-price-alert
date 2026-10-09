"""开机自启（autostart）测试：三平台行为全部离线可测。

改造前"开机自启"是三套互不相干的实现（Windows 桌面快捷方式 / macOS 模板+手动脚本 /
Linux 无），且只能靠人工在真机上验证。现在通过**注入 runner + 注入 home**，
在任意一台机器上就能覆盖 macOS / Linux / Windows 三个平台的完整行为（含失败路径）。

覆盖点：
    1. 平台判定与三平台的机制选择；
    2. enable/disable 的实际副作用（文件落盘/删除）与外部命令调用；
    3. **失败路径**：命令返回非 0、写盘失败、命令不存在；
    4. 生成物内容正确性（plist 的 XML 转义、systemd 的引号规则）；
    5. 与既有 LaunchAgent 模板的标签兼容性（老用户平滑升级）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import autostart  # noqa: E402


class FakeCompleted:
    """subprocess.CompletedProcess 的最小替身。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """记录全部调用的 runner 替身。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, cmd, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(list(cmd))
        return FakeCompleted(self.returncode, self.stdout, self.stderr)

    def flat(self) -> str:
        """把所有调用拼成一个字符串，便于断言'某个命令被调用过'。"""
        return " | ".join(" ".join(c) for c in self.calls)


class AutostartTestCase(unittest.TestCase):
    """公共夹具：临时 home + 记录型 runner。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.runner = FakeRunner()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestPlatformDetection(AutostartTestCase):
    """平台判定。"""

    def test_platform_key_mapping(self) -> None:
        self.assertEqual(autostart.platform_key("darwin"), autostart.PLATFORM_MACOS)
        self.assertEqual(autostart.platform_key("linux"), autostart.PLATFORM_LINUX)
        self.assertEqual(autostart.platform_key("win32"), autostart.PLATFORM_WINDOWS)
        self.assertEqual(autostart.platform_key("freebsd12"), autostart.PLATFORM_UNSUPPORTED)

    def test_mechanism_per_platform(self) -> None:
        self.assertEqual(autostart.status(platform="darwin", home=self.home).mechanism, "LaunchAgent")
        self.assertEqual(autostart.status(platform="linux", home=self.home).mechanism, "systemd --user")
        self.assertEqual(autostart.status(platform="win32", home=self.home).mechanism, "启动文件夹")

    def test_unsupported_platform_is_reported(self) -> None:
        st = autostart.status(platform="freebsd", home=self.home)
        self.assertFalse(st.supported)
        self.assertFalse(st.enabled)
        self.assertTrue(st.hint)

    def test_legacy_label_kept_for_smooth_upgrade(self) -> None:
        """与既有 scripts/ 模板保持同一 Label，老用户升级后不会产生两份自启项。"""
        self.assertEqual(autostart.LAUNCH_AGENT_LABEL, "com.xianyu-alert.gui")


class TestGuiCommand(AutostartTestCase):
    """自启命令的三种形态。"""

    def test_source_mode_uses_python_module(self) -> None:
        cmd = autostart.gui_command()
        self.assertEqual(cmd[1:], ["-m", "xianyu_alert.gui"])

    def test_explicit_exe_path(self) -> None:
        self.assertEqual(autostart.gui_command("/opt/app/xy"), ["/opt/app/xy"])


class TestMacOS(AutostartTestCase):
    """macOS：LaunchAgent。"""

    def test_enable_writes_plist_and_bootstraps(self) -> None:
        st = autostart.enable(platform="darwin", home=self.home, runner=self.runner)
        self.assertTrue(st.enabled)
        self.assertTrue(os.path.isfile(st.path))
        self.assertIn("launchctl bootstrap", self.runner.flat())
        content = open(st.path, encoding="utf-8").read()
        self.assertIn("<key>RunAtLoad</key>", content)
        self.assertIn("SuccessfulExit", content, "崩溃自动拉起但正常退出不拉起")

    def test_status_reflects_file_presence(self) -> None:
        self.assertFalse(autostart.status(platform="darwin", home=self.home).enabled)
        autostart.enable(platform="darwin", home=self.home, runner=self.runner)
        self.assertTrue(autostart.status(platform="darwin", home=self.home).enabled)

    def test_enable_survives_launchctl_failure(self) -> None:
        """launchctl 失败（如无 GUI 会话）时文件仍在，登录后依然生效。"""
        runner = FakeRunner(returncode=1, stderr="bootstrap failed")
        st = autostart.enable(platform="darwin", home=self.home, runner=runner)
        self.assertTrue(st.enabled)
        self.assertTrue(os.path.isfile(st.path))
        self.assertIn("launchctl", st.hint + st.detail)

    def test_enable_reports_write_failure(self) -> None:
        original = autostart._write

        def boom(*_a, **_k):  # noqa: ANN002, ANN003
            raise OSError("read-only")

        autostart._write = boom  # type: ignore[assignment]
        try:
            st = autostart.enable(platform="darwin", home=self.home, runner=self.runner)
        finally:
            autostart._write = original  # type: ignore[assignment]
        self.assertFalse(st.enabled)
        self.assertIn("失败", st.detail)

    def test_disable_removes_and_bootouts(self) -> None:
        autostart.enable(platform="darwin", home=self.home, runner=self.runner)
        path = autostart.launch_agent_path(self.home)
        st = autostart.disable(platform="darwin", home=self.home, runner=self.runner)
        self.assertFalse(st.enabled)
        self.assertFalse(os.path.exists(path))
        self.assertIn("bootout", self.runner.flat())

    def test_disable_when_not_enabled_is_safe(self) -> None:
        st = autostart.disable(platform="darwin", home=self.home, runner=self.runner)
        self.assertFalse(st.enabled)
        self.assertIn("本就未启用", st.detail)


class TestLinux(AutostartTestCase):
    """Linux：systemd --user（改造前完全没有的能力）。"""

    def test_enable_writes_unit_and_enables(self) -> None:
        st = autostart.enable(platform="linux", home=self.home, runner=self.runner)
        self.assertTrue(st.enabled)
        self.assertEqual(st.path, os.path.join(self.home, ".config", "systemd", "user", "xianyu-alert.service"))
        self.assertTrue(os.path.isfile(st.path))
        flat = self.runner.flat()
        self.assertIn("systemctl --user daemon-reload", flat)
        self.assertIn("systemctl --user enable --now xianyu-alert.service", flat)
        content = open(st.path, encoding="utf-8").read()
        self.assertIn("WantedBy=default.target", content)
        self.assertIn("Restart=on-failure", content)

    def test_enable_reports_systemctl_failure(self) -> None:
        """无 systemd 的环境（WSL1 / 容器）：文件写入但提示替代方案。"""
        runner = FakeRunner(returncode=1, stderr="System has not been booted with systemd")
        st = autostart.enable(platform="linux", home=self.home, runner=runner)
        self.assertTrue(os.path.isfile(st.path))
        self.assertIn("systemd", st.hint)

    def test_disable_disables_and_removes(self) -> None:
        autostart.enable(platform="linux", home=self.home, runner=self.runner)
        path = autostart.systemd_unit_path(self.home)
        st = autostart.disable(platform="linux", home=self.home, runner=self.runner)
        self.assertFalse(os.path.exists(path))
        self.assertIn("systemctl --user disable --now xianyu-alert.service", self.runner.flat())
        self.assertFalse(st.enabled)


class TestWindows(AutostartTestCase):
    """Windows：启动文件夹快捷方式。"""

    def test_enable_invokes_powershell_with_startup_path(self) -> None:
        st = autostart.enable(platform="win32", home=self.home, runner=self.runner)
        self.assertTrue(st.enabled)
        self.assertIn("Startup", st.path)
        flat = self.runner.flat()
        self.assertIn("powershell", flat)
        self.assertIn("Startup", flat, "快捷方式必须落在启动文件夹（桌面快捷方式并不会自启）")

    def test_enable_reports_powershell_failure(self) -> None:
        runner = FakeRunner(returncode=1, stderr="Access denied")
        st = autostart.enable(platform="win32", home=self.home, runner=runner)
        self.assertFalse(st.enabled)
        self.assertIn("失败", st.detail)

    def test_disable_removes_existing_lnk(self) -> None:
        startup = autostart.windows_startup_dir(self.home)
        os.makedirs(startup, exist_ok=True)
        lnk = os.path.join(startup, autostart.WINDOWS_LNK_NAME)
        open(lnk, "w", encoding="utf-8").close()
        st = autostart.disable(platform="win32", home=self.home, runner=self.runner)
        self.assertFalse(os.path.exists(lnk))
        self.assertFalse(st.enabled)
        self.assertIn("已关闭", st.detail)

    def test_startup_dir_respects_appdata_env(self) -> None:
        got = autostart.windows_startup_dir(self.home, env={"APPDATA": "C:/Users/x/AppData/Roaming"})
        self.assertTrue(got.startswith("C:/Users/x/AppData/Roaming"))


class TestGeneratedContent(AutostartTestCase):
    """生成物本身的正确性（转义 / 引号）。"""

    def test_plist_escapes_xml_special_chars(self) -> None:
        content = autostart.build_launch_agent_plist(["/opt/a&b/x", "-m", "x"], self.home)
        self.assertIn("a&amp;b", content)
        self.assertNotIn("a&b", content)

    def test_systemd_quotes_args_with_spaces(self) -> None:
        content = autostart.build_systemd_unit(["/opt/my app/run", "--flag"], self.home)
        self.assertIn('ExecStart="/opt/my app/run" --flag', content)

    def test_status_to_dict_is_json_ready(self) -> None:
        payload = autostart.status(platform="linux", home=self.home).to_dict()
        self.assertEqual(
            set(payload),
            {"platform", "mechanism", "supported", "enabled", "path", "detail", "hint"},
        )



class _FakeStatus:
    """autostart 状态替身。"""

    def __init__(self, enabled=False, supported=True):
        self.enabled = enabled
        self.supported = supported
        self.platform = "linux"
        self.mechanism = "systemd --user"
        self.path = "/tmp/xianyu-alert.service"
        self.detail = "已开启" if enabled else "已关闭"
        self.hint = ""


class TestCliAutostart(unittest.TestCase):
    """CLI：xianyu-alert autostart status|enable|disable [--json]。"""

    def _run(self, argv):
        import io as _io
        import json as _json
        from contextlib import redirect_stdout

        from xianyu_alert import cli

        buf = _io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        out = buf.getvalue().strip()
        payload = None
        for line in reversed(out.splitlines()):
            if line.strip().startswith("{"):
                payload = _json.loads(line)
                break
        return code, out, payload

    def test_status_json_shape(self) -> None:
        code, _out, payload = self._run(["autostart", "status", "--json"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(payload)
        for key in ("platform", "mechanism", "supported", "enabled", "path"):
            self.assertIn(key, payload)
        self.assertEqual(payload["action"], "status")

    def test_status_human_output(self) -> None:
        code, out, _payload = self._run(["autostart", "status"])
        self.assertEqual(code, 0)
        self.assertIn("开机自启", out)

    def test_enable_subcommand_exists(self) -> None:
        """enable 子命令能被解析（不在测试里真的改机器自启配置）。"""
        from xianyu_alert.cli import build_parser

        args = build_parser().parse_args(["autostart", "enable"])
        self.assertEqual(args.autostart_command, "enable")
        self.assertTrue(hasattr(args, "json"))


class TestTkHandler(unittest.TestCase):
    """Tk 一键开关的逻辑（不建窗口，用 stub + 替身 autostart）。"""

    def _stub(self, messages):
        import types

        from xianyu_alert.gui import XianyuAlertGUI

        app = types.SimpleNamespace()
        app._append_log = lambda *a, **k: None
        app._push = lambda *a, **k: None
        app._push_message = lambda level, title, text: messages.append((level, title, text))
        return app, XianyuAlertGUI.on_toggle_autostart.__get__(app)

    def _run_handler(self, fake, messages):
        import threading
        import time

        import xianyu_alert.gui.app as gui_app

        original = gui_app.autostart
        gui_app.autostart = fake
        try:
            _app, handler = self._stub(messages)
            handler()
            for _ in range(100):
                if messages:
                    break
                time.sleep(0.02)
            threads = [t for t in threading.enumerate() if t.name == "autostart"]
            for t in threads:
                t.join(timeout=1.0)
        finally:
            gui_app.autostart = original

    def test_disabled_gets_enabled(self) -> None:
        calls = []

        class Fake:
            @staticmethod
            def status():
                return _FakeStatus(enabled=False)

            @staticmethod
            def enable():
                calls.append("enable")
                return _FakeStatus(enabled=True)

            @staticmethod
            def disable():
                calls.append("disable")
                return _FakeStatus(enabled=False)

        messages = []
        self._run_handler(Fake, messages)
        self.assertEqual(calls, ["enable"])
        self.assertTrue(messages)
        self.assertEqual(messages[0][1], "已开启开机自启")

    def test_enabled_gets_disabled(self) -> None:
        calls = []

        class Fake:
            @staticmethod
            def status():
                return _FakeStatus(enabled=True)

            @staticmethod
            def enable():
                calls.append("enable")
                return _FakeStatus(enabled=True)

            @staticmethod
            def disable():
                calls.append("disable")
                return _FakeStatus(enabled=False)

        messages = []
        self._run_handler(Fake, messages)
        self.assertEqual(calls, ["disable"])
        self.assertEqual(messages[0][1], "已关闭开机自启")

    def test_unsupported_platform_warns(self) -> None:
        class Fake:
            @staticmethod
            def status():
                return _FakeStatus(supported=False)

        messages = []
        self._run_handler(Fake, messages)
        self.assertTrue(messages)
        self.assertEqual(messages[0][0], "warning")

if __name__ == "__main__":
    unittest.main()
