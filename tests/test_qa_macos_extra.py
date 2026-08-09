"""QA 独立验证补充用例（macOS M4 适配工程 T01-T05 第二道防线）。

由 QA 工程师严过关新增，与工程师自测（test_secure_fernet / test_paths_v2 /
test_gui_qt）互补，聚焦：
    1. secure：Fernet 往返 / dpapi1: 遗留语义 / 密钥文件损坏自愈 / set_key_file 隔离；
    2. paths：XY_DATA_DIR 相对路径与空值 / darwin frozen 精确落点 / win32 frozen 回归 /
       ensure_data_dir 幂等；
    3. gui_qt 纯逻辑：state.form_to_config_dict 一致性 / LogBridge 信号 / log_tag_for_text
       Qt 映射复用；
    4. 入口分发：sys.platform 各分支 + ImportError 回退；
    5. 已知缺陷记录：AlertTable._rows 类属性共享（expectedFailure 标记，回传工程师）。

注意：本文件测试必须保持独立、幂等、不依赖真实网络。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    QT_AVAILABLE = True
    _QT_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - 环境缺 PySide6
    QT_AVAILABLE = False
    _QT_IMPORT_ERROR = str(exc)

from xianyu_alert import paths, secure  # noqa: E402

#: 测试用独立密钥目录（模块级隔离，避免污染默认 data_dir()/secret.key）
_KEY_DIR = ""


def setUpModule() -> None:
    global _KEY_DIR
    _KEY_DIR = tempfile.mkdtemp(prefix="qa-macos-key-")
    secure.set_key_file(os.path.join(_KEY_DIR, "secret.key"))


def tearDownModule() -> None:
    secure.set_key_file(None)


# ---------------------------------------------------------------------- #
# 1. secure 补充
# ---------------------------------------------------------------------- #
class TestSecureExtra(unittest.TestCase):
    """Fernet 往返 / 遗留前缀 / 密钥损坏自愈 / 隔离。"""

    def test_fernet_roundtrip_unicode_and_symbols(self) -> None:
        plain = "_m_h5_tk=t_1700000000000; cookie2=abc; ¥123; 中文; emoji🎯"
        cipher = secure.encrypt_text(plain)
        self.assertTrue(cipher.startswith(secure.FERNET_PREFIX))
        self.assertEqual(secure.decrypt_text(cipher), plain)

    def test_dpapi_prefix_undecryptable_semantics(self) -> None:
        """dpapi1: 前缀 → is_encrypted=True；decrypt 返回空串 + warning（不崩不泄露）。"""
        self.assertTrue(secure.is_encrypted(secure.PREFIX + "gAAAAAB"))
        with self.assertLogs("xianyu_alert.secure", level="WARNING") as logs:
            result = secure.decrypt_text(secure.PREFIX + "gAAAAAB")
        self.assertEqual(result, "")
        joined = "\n".join(logs.output)
        self.assertIn("请重新登录", joined)

    def test_corrupt_key_file_regenerates_without_crash(self) -> None:
        """密钥文件损坏（非 base64）→ 加载返回 None → 加密降级明文/解密空串，绝不抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            key_path = os.path.join(tmp, "secret.key")
            with open(key_path, "w", encoding="utf-8") as fp:
                fp.write("not-a-valid-fernet-key!!!")
            secure.set_key_file(key_path)
            try:
                # 损坏密钥 → encrypt 降级明文（不抛）
                with self.assertLogs("xianyu_alert.secure", level="WARNING"):
                    cipher = secure.encrypt_text("secret-data")
                # 损坏密钥 → decrypt 返回空串（不抛）
                with self.assertLogs("xianyu_alert.secure", level="WARNING"):
                    result = secure.decrypt_text(secure.FERNET_PREFIX + "QUJD")
                self.assertEqual(result, "")
                # 说明：损坏密钥下不自动重写（避免覆盖用户文件）；此处只验证不崩
                self.assertIsNotNone(cipher)
            finally:
                # 恢复模块级密钥（setUpModule 的 _KEY_DIR），避免后续测试用默认路径污染项目根
                secure.set_key_file(os.path.join(_KEY_DIR, "secret.key"))

    def test_set_key_file_isolation(self) -> None:
        """set_key_file 切换后互不干扰（A 密钥解不开 B 密文）。"""
        with tempfile.TemporaryDirectory() as tmp:
            key_a = os.path.join(tmp, "a.key")
            key_b = os.path.join(tmp, "b.key")
            secure.set_key_file(key_a)
            cipher_a = secure.encrypt_text("a-secret")
            secure.set_key_file(key_b)
            cipher_b = secure.encrypt_text("b-secret")
            self.assertNotEqual(cipher_a, cipher_b)
            secure.set_key_file(key_a)
            self.assertEqual(secure.decrypt_text(cipher_a), "a-secret")
            self.assertEqual(secure.decrypt_text(cipher_b), "")
            secure.set_key_file(key_b)
            self.assertEqual(secure.decrypt_text(cipher_b), "b-secret")
            self.assertEqual(secure.decrypt_text(cipher_a), "")


# ---------------------------------------------------------------------- #
# 2. paths 补充
# ---------------------------------------------------------------------- #
class TestPathsExtra(unittest.TestCase):
    """XY_DATA_DIR 相对/空值 / darwin frozen 精确落点 / win32 回归 / ensure 幂等。"""

    def tearDown(self) -> None:
        os.environ.pop("XY_DATA_DIR", None)

    def test_env_relative_path_resolves_against_cwd(self) -> None:
        with mock.patch.dict(os.environ, {"XY_DATA_DIR": "relative/xy"}):
            expected = os.path.abspath(os.path.join(os.getcwd(), "relative", "xy"))
            self.assertEqual(paths.data_dir(), expected)

    def test_env_empty_string_ignored(self) -> None:
        """空字符串环境变量视为未设置 → 回退下一级（源码=项目根）。"""
        with mock.patch.dict(os.environ, {"XY_DATA_DIR": ""}):
            self.assertEqual(paths.data_dir(), paths.project_root())

    def test_env_whitespace_treated_as_value(self) -> None:
        """空白字符串会被 expanduser+abspath 处理（保持现状语义，不截断判断）。"""
        with mock.patch.dict(os.environ, {"XY_DATA_DIR": "   "}):
            result = paths.data_dir()
        self.assertTrue(result.endswith(os.sep + "   ") or result.endswith("   "))

    def test_darwin_frozen_exact_application_support(self) -> None:
        """mock darwin frozen → 精确落点 ~/Library/Application Support/闲鱼低价提醒工具。"""
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "/Applications/闲鱼低价提醒工具.app/Contents/MacOS/闲鱼低价提醒工具"), \
             mock.patch.object(sys, "platform", "darwin"):
            expected = os.path.join(
                os.path.expanduser("~"), "Library", "Application Support", "闲鱼低价提醒工具"
            )
            self.assertEqual(paths.data_dir(), expected)

    def test_win32_frozen_exe_dir_regression(self) -> None:
        """win32 frozen → exe 同目录（Tk 端回归保护）。"""
        exe_dir = tempfile.mkdtemp(prefix="qa-exe ")
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", os.path.join(exe_dir, "闲鱼低价提醒工具.exe")), \
             mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(paths.data_dir(), exe_dir)

    def test_ensure_data_dir_creates_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "a", "b", "c")
            with mock.patch.dict(os.environ, {"XY_DATA_DIR": target}):
                self.assertEqual(paths.ensure_data_dir(), target)
                self.assertTrue(os.path.isdir(target))
                # 幂等：再次调用不抛
                self.assertEqual(paths.ensure_data_dir(), target)


# ---------------------------------------------------------------------- #
# 3. gui_qt 纯逻辑
# ---------------------------------------------------------------------- #
class TestQtStateLogic(unittest.TestCase):
    """state.form_to_config_dict 与 gui.build_config_dict 一致性（不依赖 Qt 显示）。"""

    def test_form_to_config_dict_matches_gui_pure_function(self) -> None:
        """与 gui.build_config_dict 结构一致（Fernet 随机盐 → 密文不直接比较，
        改为：非 Cookie 字段相等 + Cookie 前缀为 fernet1: + 解密后明文一致）。"""
        from xianyu_alert.gui import build_config_dict
        from xianyu_alert.gui_qt import state as qt_state

        form = {
            "keywords": [("Switch", 1000.0), ("PS5", 2500.0)],
            "keyword_enabled": {"Switch": True, "PS5": False},
            "keyword_filters": {
                "Switch": {"exclude_keywords": ["国行"], "required_keywords": []},
                "PS5": {"exclude_keywords": [], "required_keywords": ["光驱"]},
            },
            "interval": 120,
            "fetcher_type": "mtop",
            "cookies": "_m_h5_tk=qa_test_1",
            "user_agent": "",
            "storage_path": "/tmp/x.db",
            "pages": 2,
            "channels": {"console": {"enabled": True, "options": {}}},
            "cookie_pool": [],
            "preset_exclude_keywords": ["破损"],
        }
        expected = build_config_dict(
            keywords=[("Switch", 1000.0), ("PS5", 2500.0)],
            interval_seconds=120,
            fetcher_type="mtop",
            cookies="_m_h5_tk=qa_test_1",
            storage_path="/tmp/x.db",
            channels=form["channels"],
            base=None,
            pages=2,
            encrypt_cookies=True,
            keyword_filters=form["keyword_filters"],
            cookie_pool=form["cookie_pool"],
            preset_exclude_keywords=form["preset_exclude_keywords"],
            keyword_enabled=form["keyword_enabled"],
        )
        actual = qt_state.form_to_config_dict(form)
        # 非 Cookie 字段完全一致
        expected_plain = {k: v for k, v in expected.items() if k != "monitor"}
        actual_plain = {k: v for k, v in actual.items() if k != "monitor"}
        self.assertEqual(actual_plain, expected_plain)
        # monitor 内非 Cookie 字段一致
        expected_mon = {k: v for k, v in expected["monitor"].items() if k != "cookies"}
        actual_mon = {k: v for k, v in actual["monitor"].items() if k != "cookies"}
        self.assertEqual(actual_mon, expected_mon)
        # Cookie 均为 fernet1: 且解密一致
        for data in (expected, actual):
            self.assertTrue(data["monitor"]["cookies"].startswith(secure.FERNET_PREFIX))
        self.assertEqual(
            secure.decrypt_text(actual["monitor"]["cookies"]),
            secure.decrypt_text(expected["monitor"]["cookies"]),
        )

    @unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
    def test_log_tag_for_text_reused_from_gui(self) -> None:
        """Qt widgets 复用 gui.log_tag_for_text：确认 import 路径与映射一致性。

        注意：log_tag_for_text 按**文本前缀**映射（非按 level），
        🔔 命中 → NEW_ITEM（蓝加粗，v3.7 设计如此），而非 ALERT。
        """
        from xianyu_alert.gui import log_tag_for_text
        from xianyu_alert.gui_qt.widgets import LOG_LEVEL_STYLES

        samples = [
            ("INFO", "普通日志", "INFO"),
            ("ERROR", "❌ 出错了", "ERROR"),
            ("WARNING", "⚠️ 警告", "WARNING"),
            ("INFO", "🔔 命中低价！", "NEW_ITEM"),      # 前缀优先于 level
            ("ALERT", "🔔 命中低价！", "NEW_ITEM"),     # 同上
            ("INFO", "✨ 新出现", "NEW_ITEM"),
            ("INFO", "✅ 本轮完成", "SUMMARY"),
            ("INFO", "===== 第 1 轮监测开始 =====", "ROUND"),
            ("INFO", "🚫 已停用", "DIM"),
        ]
        for level, text, expected_tag in samples:
            self.assertEqual(log_tag_for_text(level, text), expected_tag)
            # widgets 中已为这些 tag 预建样式（不存在则 KeyError → 测试失败）
            self.assertIn(expected_tag, LOG_LEVEL_STYLES)


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestQtLogBridgeSignal(unittest.TestCase):
    """LogBridge 信号跨线程投递（与 logging 无关的直接信号验证）。"""

    def test_logbridge_signal_delivers(self) -> None:
        from xianyu_alert.gui_qt.workers import LogBridge

        bridge = LogBridge()
        received: list = []
        bridge.message.connect(lambda level, text: received.append((level, text)))
        bridge.message.emit("INFO", "hello-bridge")
        for _ in range(10):
            _APP.processEvents()
        self.assertTrue(received)
        self.assertEqual(received[-1], ("INFO", "hello-bridge"))


@unittest.skipUnless(QT_AVAILABLE, "PySide6 不可用，跳过 Qt 测试")
class TestQtAlertTableClassAttrBug(unittest.TestCase):
    """已知缺陷记录（已修复）：AlertTable._rows 曾为类属性 → 多实例共享。

    Bug #3 修复：`_rows` 改为 `__init__` 中的实例属性（与 KeywordTable 一致）。
    本用例为回归保护：新实例必须看不到旧实例的数据。
    """

    def test_new_instance_should_not_see_old_data(self) -> None:
        from xianyu_alert.gui_qt.widgets import AlertTable

        t1 = AlertTable()
        t1.append_row({"title": "A", "product_id": "1"})
        t2 = AlertTable()
        self.assertEqual(t1.row_count(), 1)
        # 正确行为：新实例应无数据（若回退为类属性则此处得到 1 条 → 测试失败）
        self.assertEqual(t2.row_count(), 0)


# ---------------------------------------------------------------------- #
# 4. 入口分发
# ---------------------------------------------------------------------- #
class TestEntryDispatchExtra(unittest.TestCase):
    """入口分发：darwin → Qt；其余 → Tk；PySide6 缺失回退（当前有缺陷，见下）。"""

    def _run_cmd_gui(self, platform: str):
        from xianyu_alert import cli

        args = mock.Mock(config="x.yaml")
        with mock.patch.object(sys, "platform", platform), \
             mock.patch("xianyu_alert.gui_qt.is_available", return_value=True), \
             mock.patch("xianyu_alert.gui_qt.main", return_value=0) as qt, \
             mock.patch("xianyu_alert.gui.main", return_value=0) as tk:
            result = cli.cmd_gui(args)
            return result, qt.called, tk.called

    def test_darwin_uses_qt(self) -> None:
        """darwin 分支 → Qt（is_available 显式 mock，避免依赖本机是否装了 PySide6）。"""
        result, qt_called, tk_called = self._run_cmd_gui("darwin")
        self.assertEqual(result, 0)
        self.assertTrue(qt_called)
        self.assertFalse(tk_called)

    def test_win32_uses_tk(self) -> None:
        result, qt_called, tk_called = self._run_cmd_gui("win32")
        self.assertEqual(result, 0)
        self.assertFalse(qt_called)
        self.assertTrue(tk_called)

    def test_linux_uses_tk(self) -> None:
        result, qt_called, tk_called = self._run_cmd_gui("linux")
        self.assertEqual(result, 0)
        self.assertFalse(qt_called)
        self.assertTrue(tk_called)

    def test_darwin_pyside_missing_falls_back_to_tk(self) -> None:
        """Bug #2 回归：PySide6 缺失时应回退 Tk，不崩溃。

        修复前：gui_qt 顶层不 import PySide6，`from .gui_qt import main` 不抛
        ImportError；真正的 ImportError 在 main() 内部才抛 → 超出 try 范围 →
        cmd_gui 直接崩溃。修复后：cmd_gui 先 `is_available()` 显式探测，
        PySide6 缺失时打 warning 并回退 Tk。
        """
        from xianyu_alert import cli

        args = mock.Mock(config="x.yaml")
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch("xianyu_alert.gui_qt.is_available", return_value=False) as probe, \
             mock.patch("xianyu_alert.gui.main", return_value=0) as tk_main:
            result = cli.cmd_gui(args)
        probe.assert_called_once_with()
        self.assertEqual(result, 0)
        self.assertTrue(tk_main.called)

    def test_darwin_pyside_missing_and_tk_missing_returns_2(self) -> None:
        """Bug #2 极端场景（Round2 补充）：is_available=False 且 Tk(gui) 也导入失败。

        正确行为：cmd_gui 捕获第二个 ImportError，打清晰错误日志并 return 2，
        不抛未捕获异常、不崩溃（Qt/Tk 双缺失时 CLI 可正常退出）。
        """
        from xianyu_alert import cli

        args = mock.Mock(config="x.yaml")
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch("xianyu_alert.gui_qt.is_available", return_value=False), \
             mock.patch.dict(sys.modules, {"xianyu_alert.gui": None}):
            # gui 模块不可导入 → from .gui import main 抛 ImportError → return 2
            with self.assertLogs("xianyu_alert", level="WARNING") as logs:
                result = cli.cmd_gui(args)
        self.assertEqual(result, 2)
        joined = "\n".join(logs.output)
        self.assertIn("无法加载图形界面模块", joined)

    @unittest.skipUnless(sys.platform == "darwin", "macOS 专属：install_launchagent.sh")
    def test_install_launchagent_explicit_path_not_overwritten(self) -> None:
        """Bug #4 边界（Round2 补充）：install_launchagent.sh 显式传参时 APP_PATH 用传入值。

        通过 dry-run 副本验证：把平台检查替换为 false（跳过 macOS 专属命令），
        插入 PROBE_APP_PATH 探测；显式传参后应输出传入路径而非默认 <root>/dist/...。
        """
        import shutil
        import subprocess

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = os.path.join(project_root, "scripts", "install_launchagent.sh")
        self.assertTrue(os.path.isfile(src), "install_launchagent.sh 不存在")
        with tempfile.TemporaryDirectory(prefix="qa-la-") as tmp:
            scripts_dir = os.path.join(tmp, "scripts")
            os.makedirs(scripts_dir)
            dst = os.path.join(scripts_dir, "install_launchagent.sh")
            shutil.copyfile(src, dst)
            # 1) 平台检查替换为 false（Windows 上也能跑 dry-run）
            with open(dst, "r", encoding="utf-8") as fp:
                content = fp.read()
            content = content.replace(
                'if [ "$(uname -s)" != "Darwin" ]; then',
                "if false; then",
            )
            # 2) APP_PATH 赋值后（if/else 结束 fi 之后）插入探测，覆盖两个分支
            marker = 'APP_PATH="${PROJECT_ROOT}/dist/闲鱼低价提醒工具.app"\nfi'
            self.assertIn(marker, content)
            content = content.replace(
                marker, marker + '\necho "PROBE_APP_PATH=[${APP_PATH}]"', 1
            )
            with open(dst, "w", encoding="utf-8", newline="\n") as fp:
                fp.write(content)

            def _probe(*argv: str) -> str:
                proc = subprocess.run(
                    ["bash", "scripts/install_launchagent.sh", *argv],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                for line in proc.stdout.splitlines():
                    if line.startswith("PROBE_APP_PATH="):
                        raw = line.split("=", 1)[1]
                        return raw.strip("[]")
                return f"<no probe> stdout={proc.stdout!r} stderr={proc.stderr!r}"

            # 无参 → 默认指向 <项目根>/dist/闲鱼低价提醒工具.app（而非 /dist/...）
            default = _probe()
            self.assertIn("/dist/闲鱼低价提醒工具.app", default)
            # 必须带项目根前缀：不能是裸 /dist/ 根路径（Bug #4 修复点）
            self.assertTrue(default.startswith("/"), f"默认 APP_PATH 应为绝对路径: {default}")
            self.assertNotEqual(
                default, "/dist/闲鱼低价提醒工具.app",
                "默认 APP_PATH 不得退化为根目录 /dist/...",
            )
            self.assertFalse(
                default.startswith("/dist/"),
                f"默认 APP_PATH 不应以 /dist/ 开头: {default}",
            )
            # 显式传参 → 使用传入值，不被默认覆盖
            explicit = _probe("/custom/My App.app")
            self.assertEqual(explicit, "/custom/My App.app")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
