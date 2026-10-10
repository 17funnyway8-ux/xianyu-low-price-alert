"""配置解析的校验与回退测试（v1.10.8，M10 补强）。

重评显示 `xianyu_alert/config.py` 覆盖率 81%，缺口集中在各解析器的**守卫分支**：
`_parse_fetcher`(15) / `_parse_keywords`(10) / `_parse_monitor`(9) / `_parse_notify`(8) /
`load_config`(7) / `_parse_cookie_pool`(5) / `_parse_keepalive`(4)。

**实测出来的契约（本轮写测试时才核对清楚，值得记下来）**：
    - 结构性/取值范围错误 -> **直接抛 ConfigError**，且带**精确到字段的文案**（"能用的部分照用"
      只适用于少数点）。这样用户改错配置时立刻知道是哪一行、期望什么。
    - 只有**少数点**做静默回退并告警：`notify.quiet_hours` 格式非法、缺 config_version 时的迁移。
本文件把这两类行为分别钉住 —— 尤其是那些**用户可见的报错文案**。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from xianyu_alert.config import ConfigError, config_from_dict, load_config  # noqa: E402

BASE = {
    "keywords": [{"keyword": "Switch", "max_price": 1000}],
    # v1.11.3：间隔有安全下限 MIN_INTERVAL_SECONDS=120，正常配置请用 ≥120 的值
    "monitor": {"interval_seconds": 600},
}


def config_with(**sections) -> object:
    data = dict(BASE)
    data.update(sections)
    return config_from_dict(data)


class TestStrictValidation(unittest.TestCase):
    """结构性 / 取值范围错误必须**明确报错**（表驱动，逐条钉住文案）。"""

    CASES = [
        ({"keywords": "Switch", "monitor": {"interval_seconds": 60}}, "keywords"),
        ({"keywords": [], "monitor": {"interval_seconds": 60}}, "keywords"),
        ({"keywords": ["Switch"], "monitor": {"interval_seconds": 60}}, "必须是映射"),
        ({"keywords": [{"keyword": "", "max_price": 1}], "monitor": {"interval_seconds": 60}}, "keyword"),
        ({"keywords": [{"keyword": "A", "max_price": "贵"}], "monitor": {"interval_seconds": 60}}, "max_price"),
        ({"keywords": [{"keyword": "A", "max_price": 1}], "monitor": "oops"}, "monitor"),
        ({"keywords": [{"keyword": "A", "max_price": 1}], "monitor": {"interval_seconds": "很久"}}, "interval_seconds"),
        ({"keywords": [{"keyword": "A", "max_price": 1}], "monitor": {"interval_seconds": 0}}, "大于 0"),
        ({"keywords": [{"keyword": "A", "max_price": 1}], "monitor": {"interval_seconds": -5}}, "大于 0"),
        ({"keywords": [{"keyword": "A", "max_price": 1}], "monitor": {"interval_seconds": 60}, "fetcher": {"type": "不存在的抓取器"}}, "fetcher.type"),
        ({"keywords": [{"keyword": "A", "max_price": 1}], "monitor": {"interval_seconds": 60}, "notify": {"channels": "webhook"}}, "channels"),
        ({"keywords": [{"keyword": "A", "max_price": 1}], "monitor": {"interval_seconds": 60}, "notify": {"channels": [{"type": ""}]}}, "type"),
        ({"keywords": [{"keyword": "A", "max_price": 1}], "monitor": {"interval_seconds": 60, "cookie_pool": "abc"}}, "cookie_pool"),
    ]

    def test_each_dirty_config_raises_with_precise_message(self) -> None:
        for data, fragment in self.CASES:
            with self.subTest(fragment=fragment):
                with self.assertRaises(ConfigError) as ctx:
                    config_from_dict(data)
                self.assertIn(fragment, str(ctx.exception), f"报错文案应指向字段：{fragment}")

    def test_unknown_keys_are_ignored(self) -> None:
        """未知字段不报错（向前兼容：老版本读新配置不能崩）。"""
        cfg = config_with(monitor={"interval_seconds": 300, "未来字段": 1})
        self.assertEqual(cfg.monitor.interval_seconds, 300)


class TestDocumentedFallbacks(unittest.TestCase):
    """少数"回退 + 告警"的点必须真的回退，而不是抛错。"""

    def test_invalid_quiet_hours_falls_back_to_empty(self) -> None:
        cfg = config_with(notify={"quiet_hours": "25:99-01:00"})
        self.assertEqual(cfg.notify.quiet_hours, "")

    def test_valid_quiet_hours_kept(self) -> None:
        cfg = config_with(notify={"quiet_hours": "23:00-07:00"})
        self.assertEqual(cfg.notify.quiet_hours, "23:00-07:00")

    def test_dirty_numeric_notify_fields_fall_back(self) -> None:
        cfg = config_with(notify={"aggregate_seconds": "x", "retry_attempts": -1})
        self.assertEqual(cfg.notify.aggregate_seconds, 0)
        self.assertGreaterEqual(cfg.notify.retry_attempts, 1)

    def test_missing_optional_sections_use_defaults(self) -> None:
        cfg = config_from_dict({"keywords": [{"keyword": "A", "max_price": 1}]})
        self.assertGreater(cfg.monitor.interval_seconds, 0)
        self.assertTrue(str(cfg.storage.path))

    def test_missing_fetcher_type_defaults(self) -> None:
        cfg = config_from_dict({"keywords": [{"keyword": "A", "max_price": 1}]})
        self.assertIn(str(cfg.fetcher.type), ("mtop", "web", "mock"))


class TestLoadConfigFile(unittest.TestCase):
    """load_config 的文件级路径。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, content: str) -> str:
        path = os.path.join(self.tmp, "config.yaml")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(content)
        return path

    def test_missing_file_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(os.path.join(self.tmp, "nope.yaml"))

    def test_empty_file_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self._write(""))

    def test_yaml_not_a_mapping_raises(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self._write("- just\n- a\n- list\n"))

    def test_invalid_yaml_raises(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self._write("keywords: [{\n"))

    def test_valid_file_loads(self) -> None:
        cfg = load_config(self._write(yaml.safe_dump(BASE, allow_unicode=True)))
        self.assertEqual(cfg.monitor.interval_seconds, 600)

    def test_v0_config_is_migrated(self) -> None:
        """老配置（无 config_version）应被迁移到当前版本。"""
        cfg = load_config(self._write(yaml.safe_dump(BASE, allow_unicode=True)))
        self.assertGreaterEqual(int(getattr(cfg, "config_version", 0)), 1)


if __name__ == "__main__":
    unittest.main()
