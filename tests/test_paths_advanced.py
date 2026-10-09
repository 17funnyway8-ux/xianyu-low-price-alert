"""paths.data_dir() 扩展测试（macOS 适配设计文档 §3.3 / §5.2）。

覆盖：
    - `XY_DATA_DIR` 优先级（含 `~` 展开 / 相对路径 / 绝对路径直通）；
    - frozen + darwin → `~/Library/Application Support/闲鱼低价提醒工具/`；
    - frozen + win32 → exe 目录（现状回归）；
    - 源码模式 → 项目根；
    - `ensure_data_dir()` 幂等创建；
    - `default_config_path` / `resolve_data_path` / `default_state_dir`
      统一锚定 `data_dir()`。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import paths  # noqa: E402

APP_DIR_NAME = "闲鱼低价提醒工具"


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDataDirEnvVar(unittest.TestCase):
    """XY_DATA_DIR 环境变量优先。"""

    def tearDown(self) -> None:
        os.environ.pop("XY_DATA_DIR", None)

    def test_env_priority_over_source(self) -> None:
        target = os.path.join(tempfile.gettempdir(), "xydata")
        with mock.patch.dict(os.environ, {"XY_DATA_DIR": target}):
            self.assertEqual(paths.data_dir(), os.path.abspath(target))

    def test_env_tilde_expansion(self) -> None:
        with mock.patch.dict(os.environ, {"XY_DATA_DIR": "~/xianyu-data"}):
            self.assertEqual(paths.data_dir(), os.path.abspath(os.path.expanduser("~/xianyu-data")))

    def test_env_absolute_passthrough(self) -> None:
        target = os.path.join(tempfile.gettempdir(), "a", "b")
        with mock.patch.dict(os.environ, {"XY_DATA_DIR": target}):
            self.assertEqual(paths.data_dir(), os.path.abspath(target))

    def test_env_wins_over_frozen(self) -> None:
        """即使 frozen+darwin，环境变量仍优先。"""
        exe_dir = tempfile.mkdtemp(prefix="exe ")
        target = os.path.join(tempfile.gettempdir(), "xydata-env")
        with mock.patch.dict(os.environ, {"XY_DATA_DIR": target}), \
             mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", os.path.join(exe_dir, "app")), \
             mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(paths.data_dir(), os.path.abspath(target))


class TestDataDirDarwin(unittest.TestCase):
    """frozen + darwin → Application Support。"""

    def test_frozen_darwin_app_support(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "/Applications/闲鱼低价提醒工具.app/Contents/MacOS/x"), \
             mock.patch.object(sys, "platform", "darwin"):
            expected = os.path.join(
                os.path.expanduser("~"), "Library", "Application Support", APP_DIR_NAME
            )
            self.assertEqual(paths.data_dir(), expected)

    def test_frozen_darwin_config_path(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "/Applications/x.app/Contents/MacOS/x"), \
             mock.patch.object(sys, "platform", "darwin"):
            expected = os.path.join(
                os.path.expanduser("~"), "Library", "Application Support", APP_DIR_NAME, "config.yaml"
            )
            self.assertEqual(paths.default_config_path(), expected)

    def test_frozen_darwin_state_dir(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "/Applications/x.app/Contents/MacOS/x"), \
             mock.patch.object(sys, "platform", "darwin"):
            expected = os.path.join(
                os.path.expanduser("~"), "Library", "Application Support", APP_DIR_NAME, "state"
            )
            self.assertEqual(paths.default_state_dir(), expected)

    def test_frozen_darwin_resolve_data_path(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "/Applications/x.app/Contents/MacOS/x"), \
             mock.patch.object(sys, "platform", "darwin"):
            expected = os.path.join(
                os.path.expanduser("~"), "Library", "Application Support", APP_DIR_NAME, "state", "x.db"
            )
            self.assertEqual(paths.resolve_data_path("state/x.db"), expected)


class TestDataDirFrozenWin32(unittest.TestCase):
    """frozen + win32 → exe 目录（现状回归，防 mac 分支误伤）。"""

    def test_frozen_win32_exe_dir(self) -> None:
        exe_dir = tempfile.mkdtemp(prefix="闲鱼 exe ")
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", os.path.join(exe_dir, "xianyu_alert.exe")), \
             mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(paths.data_dir(), exe_dir)


class TestDataDirSource(unittest.TestCase):
    """源码模式 → 项目根（行为不变）。"""

    def test_source_project_root(self) -> None:
        os.environ.pop("XY_DATA_DIR", None)
        self.assertEqual(paths.data_dir(), _project_root())

    def test_source_default_config_path(self) -> None:
        os.environ.pop("XY_DATA_DIR", None)
        self.assertEqual(paths.default_config_path(), os.path.join(_project_root(), "config.yaml"))

    def test_source_resolve_relative(self) -> None:
        os.environ.pop("XY_DATA_DIR", None)
        self.assertEqual(
            paths.resolve_data_path("state/x.db"), os.path.join(_project_root(), "state", "x.db")
        )

    def test_source_resolve_absolute(self) -> None:
        abs_path = os.path.join(tempfile.gettempdir(), "x.db")
        self.assertEqual(paths.resolve_data_path(abs_path), os.path.abspath(abs_path))

    def test_source_resolve_empty(self) -> None:
        os.environ.pop("XY_DATA_DIR", None)
        self.assertEqual(paths.resolve_data_path(""), _project_root())


class TestEnsureDataDir(unittest.TestCase):
    """ensure_data_dir 幂等创建。"""

    def tearDown(self) -> None:
        os.environ.pop("XY_DATA_DIR", None)

    def test_creates_dir_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "nested", "data")
            with mock.patch.dict(os.environ, {"XY_DATA_DIR": target}):
                self.assertEqual(paths.ensure_data_dir(), target)
                self.assertTrue(os.path.isdir(target))
                # 幂等：再次调用不报错
                self.assertEqual(paths.ensure_data_dir(), target)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
