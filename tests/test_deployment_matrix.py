"""部署形态矩阵测试（v1.10.4，M12）。

测评原话：「形态判断分支多，新增部署形态（如自定义卷）时容易漏」。
本文件的做法是**把矩阵逐条枚举**：每种形态都断言 kind / 数据目录 / 与 paths 的一致性，
因此往 FORMS 里加形态时，如果不补期望表，测试会立刻提醒。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import paths  # noqa: E402
from xianyu_alert.deployment import FORMS, describe_deployment, detect_deployment  # noqa: E402


class TestMatrixCoverage(unittest.TestCase):
    """矩阵自身的不变量。"""

    def test_forms_are_unique_and_ordered(self) -> None:
        kinds = [form.kind for form in FORMS]
        self.assertEqual(len(kinds), len(set(kinds)), "形态标识不能重复")
        self.assertEqual(kinds[0], "custom", "XY_DATA_DIR 必须最先匹配（最高优先级）")
        self.assertEqual(kinds[-1], "source", "源码形态必须是兜底")

    def test_every_form_is_reachable(self) -> None:
        """矩阵里每条形态都必须能在某种输入下被命中（否则就是死代码）。"""
        reachable = set()
        for env in ({}, {"XY_DATA_DIR": "/tmp/x"}):
            for platform in ("darwin", "linux", "win32"):
                for frozen in (True, False):
                    reachable.add(detect_deployment(env, platform, frozen).kind)
        self.assertEqual(reachable, {form.kind for form in FORMS})

    def test_last_form_matches_everything(self) -> None:
        self.assertTrue(FORMS[-1].matches({}, "weird-platform", False))


#: 形态矩阵的**期望表**：新增形态时这里必须同步补一行
EXPECTED = {
    "custom": {"label": "自定义数据目录", "env_override": True},
    "macos-app": {"label": "macOS 应用包", "env_override": False},
    "portable": {"label": "绿色便携版", "env_override": False},
    "source": {"label": "源码运行", "env_override": False},
}


class TestDetectDeployment(unittest.TestCase):
    """逐形态断言。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.custom = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_matrix_rows(self) -> None:
        cases = [
            ({"XY_DATA_DIR": self.custom}, "darwin", True),
            ({"XY_DATA_DIR": self.custom}, "linux", False),
            ({}, "darwin", True),
            ({}, "win32", True),
            ({}, "linux", True),
            ({}, "darwin", False),
        ]
        for env, platform, frozen in cases:
            info = detect_deployment(env, platform, frozen)
            self.assertIn(info.kind, EXPECTED, f"矩阵出现未登记的形态：{info.kind}")
            self.assertEqual(info.label, EXPECTED[info.kind]["label"])
            self.assertEqual(info.env_override, EXPECTED[info.kind]["env_override"])
            self.assertTrue(os.path.isabs(info.data_dir), "数据目录必须是绝对路径")

    def test_custom_wins_over_everything(self) -> None:
        info = detect_deployment({"XY_DATA_DIR": self.custom}, "darwin", True)
        self.assertEqual(info.kind, "custom")
        self.assertEqual(info.data_dir, os.path.abspath(self.custom))

    def test_custom_supports_tilde_and_relative(self) -> None:
        info = detect_deployment({"XY_DATA_DIR": "~/xy-test-dir"}, "linux", False)
        self.assertTrue(info.data_dir.startswith(os.path.expanduser("~")))
        self.assertTrue(os.path.isabs(info.data_dir))

    def test_macos_app_uses_user_writable_dir(self) -> None:
        info = detect_deployment({}, "darwin", True)
        self.assertEqual(info.kind, "macos-app")
        self.assertIn("Application Support", info.data_dir)
        self.assertTrue(info.data_dir.startswith(os.path.expanduser("~")))

    def test_portable_sits_next_to_executable(self) -> None:
        info = detect_deployment({}, "win32", True)
        self.assertEqual(info.kind, "portable")
        self.assertEqual(info.data_dir, os.path.dirname(os.path.abspath(sys.executable)))

    def test_source_uses_project_root(self) -> None:
        info = detect_deployment({}, "linux", False)
        self.assertEqual(info.kind, "source")
        self.assertEqual(info.data_dir, paths.project_root())

    def test_to_dict_and_describe(self) -> None:
        info = detect_deployment({}, "linux", False)
        payload = info.to_dict()
        self.assertEqual(set(payload), {"kind", "label", "data_dir", "frozen", "platform", "env_override"})
        self.assertIn("部署形态", info.describe())


class TestPathsDelegatesToMatrix(unittest.TestCase):
    """paths.data_dir() 必须与矩阵同源（否则又会出现两套判定）。"""

    def test_data_dir_equals_matrix_result(self) -> None:
        self.assertEqual(paths.data_dir(), detect_deployment().data_dir)

    def test_env_override_flows_through(self) -> None:
        tmp = tempfile.mkdtemp()
        original = os.environ.get("XY_DATA_DIR")
        os.environ["XY_DATA_DIR"] = tmp
        try:
            self.assertEqual(paths.data_dir(), os.path.abspath(tmp))
            self.assertEqual(paths.default_config_path(), os.path.join(os.path.abspath(tmp), "config.yaml"))
        finally:
            if original is None:
                os.environ.pop("XY_DATA_DIR", None)
            else:
                os.environ["XY_DATA_DIR"] = original

    def test_describe_deployment_is_one_line(self) -> None:
        text = describe_deployment()
        self.assertIn("数据目录", text)
        self.assertNotIn("\n", text)


if __name__ == "__main__":
    unittest.main()
