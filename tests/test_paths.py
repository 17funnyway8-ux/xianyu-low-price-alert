"""paths 模块单元测试：mock sys.frozen 锁定三种行为（正常 / frozen / resource）。

对齐 v3 设计文档 D3：源码模式锚定项目根；frozen 模式锚定 exe 同目录。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import paths  # noqa: E402


def _project_root() -> str:
    """项目根 = 本测试文件的上上级目录。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPathsNormal(unittest.TestCase):
    """源码模式（未 frozen）。"""

    def test_app_base_dir_is_project_root(self) -> None:
        self.assertEqual(os.path.abspath(paths.app_base_dir()), _project_root())

    def test_resource_path_under_project_root(self) -> None:
        self.assertEqual(paths.resource_path("icon.ico"), os.path.join(_project_root(), "icon.ico"))

    def test_default_config_path(self) -> None:
        self.assertEqual(paths.default_config_path(), os.path.join(_project_root(), "config.yaml"))

    def test_resolve_data_path_anchors_to_base(self) -> None:
        self.assertEqual(
            paths.resolve_data_path("state/x.db"),
            os.path.join(_project_root(), "state", "x.db"),
        )

    def test_resolve_data_path_absolute_passthrough(self) -> None:
        abs_path = os.path.join(tempfile.gettempdir(), "x.db")
        self.assertEqual(paths.resolve_data_path(abs_path), os.path.abspath(abs_path))

    def test_resolve_data_path_empty_returns_base(self) -> None:
        self.assertEqual(paths.resolve_data_path(""), paths.app_base_dir())

    def test_default_state_dir(self) -> None:
        with mock.patch("xianyu_alert.paths.app_base_dir", return_value=_project_root()):
            state_dir = paths.default_state_dir()
        self.assertEqual(state_dir, os.path.join(_project_root(), "state"))


class TestPathsFrozen(unittest.TestCase):
    """frozen 模式（sys.frozen=True + 假 executable，平台固定 win32 保证确定性）。

    对齐 macOS 适配设计文档 §5.1：既有 frozen 用例显式 mock `sys.platform='win32'`，
    避免在 macOS 开发机上被 `data_dir()` 的 darwin 分支（Application Support）干扰。
    """

    def setUp(self) -> None:
        # 用临时目录模拟 exe 所在目录（含中文/空格，覆盖真实场景）
        self.exe_dir = tempfile.mkdtemp(prefix="闲鱼 exe ")
        self.exe_path = os.path.join(self.exe_dir, "xianyu_alert.exe")

    def test_app_base_dir_is_exe_dir(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", self.exe_path), \
             mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(paths.app_base_dir(), self.exe_dir)

    def test_default_config_path_is_exe_dir(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", self.exe_path), \
             mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(paths.default_config_path(), os.path.join(self.exe_dir, "config.yaml"))

    def test_resolve_data_path_anchors_to_exe_dir(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", self.exe_path), \
             mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(
                paths.resolve_data_path("state/x.db"),
                os.path.join(self.exe_dir, "state", "x.db"),
            )

    def test_default_state_dir_under_exe_dir(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", self.exe_path), \
             mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(paths.default_state_dir(), os.path.join(self.exe_dir, "state"))


class TestPathsResource(unittest.TestCase):
    """resource_path 的 _MEIPASS 分支（frozen 打包资源）。"""

    def test_resource_uses_meipass_when_present(self) -> None:
        meipass = os.path.join(tempfile.gettempdir(), "_MEI12345")
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "_MEIPASS", meipass, create=True), \
             mock.patch.object(sys, "executable", os.path.join(tempfile.gettempdir(), "x.exe")):
            self.assertEqual(paths.resource_path("icon.ico"), os.path.join(meipass, "icon.ico"))

    def test_is_frozen_flag(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertTrue(paths.is_frozen())
        # 未设置 sys.frozen 时应安全返回 False
        self.assertFalse(paths.is_frozen())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
