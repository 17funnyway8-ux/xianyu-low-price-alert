"""监控调度器 v1.10 新能力测试：过滤原因 / 配置热更 / 轮次指标 / 单一时间源。

对应两个模块的改造（M05 监控调度 8.1、M04 关键词过滤 8.2）：

    M04  过滤规则不可热更 -> 轮次边界按 mtime 热更；过滤判定回传"为什么被过滤"
    M05  保活与轮次两套时间节奏并存 -> 保活并入主循环，只有一个时间源；补轮次级指标

全部离线（MockFetcher + 临时配置 + 内存库），不访问外网。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from xianyu_alert.config import load_config  # noqa: E402
from xianyu_alert.fetcher import MockFetcher  # noqa: E402
from xianyu_alert.filters import filter_decision, product_passes_filter  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_product(title: str, price: float = 10.0, pid: str = "123456789") -> Product:
    """构造测试商品。"""
    return Product(
        product_id=pid,
        title=title,
        price=price,
        url="https://www.goofish.com/item?id=" + pid,
        keyword="Switch",
    )


def make_config_dict(keyword: str = "Switch", interval: int = 60) -> dict:
    """最小可用配置字典。"""
    return {
        "keywords": [{"keyword": keyword, "max_price": 1000}],
        "monitor": {"interval_seconds": interval},
        "fetcher": {"type": "mock", "mock_products_per_round": 3},
        "storage": {"path": ":memory:"},
        "notify": {"channels": []},
    }


class TestFilterDecision(unittest.TestCase):
    """M04：过滤判定必须回传原因，且与既有布尔接口语义一致。"""

    def test_ok(self) -> None:
        product = make_product("Switch OLED 95新")
        decision = filter_decision(product, ["OLED"], [])
        self.assertTrue(decision.passed)
        self.assertEqual(decision.reason, "ok")

    def test_missing_required(self) -> None:
        decision = filter_decision(make_product("Switch 手柄"), ["OLED"], [])
        self.assertFalse(decision.passed)
        self.assertEqual(decision.reason, "missing_required")
        self.assertIn("OLED", decision.detail)

    def test_excluded(self) -> None:
        decision = filter_decision(make_product("Switch 高仿 假货"), [], ["假货"])
        self.assertFalse(decision.passed)
        self.assertEqual(decision.reason, "excluded")

    def test_case_insensitive_matches_boolean_api(self) -> None:
        """大小写不敏感语义来自既有 helpers，两个接口必须完全一致。"""
        product = make_product("SWITCH OLED 便携版")
        self.assertEqual(
            filter_decision(product, ["oled"], []).passed,
            product_passes_filter(product, ["oled"], []),
        )
        self.assertFalse(filter_decision(product, [], ["OLed"]).passed)

    def test_precedence_required_before_excluded(self) -> None:
        """两个条件同时不满足时，与 _item_reason 同口径：先报缺必含词。"""
        decision = filter_decision(make_product("Switch 假货"), ["OLED"], ["假货"])
        self.assertEqual(decision.reason, "missing_required")

    def test_to_dict(self) -> None:
        payload = filter_decision(make_product("Switch"), [], []).to_dict()
        self.assertEqual(set(payload), {"passed", "reason", "detail"})


class MonitorSchedulerCase(unittest.TestCase):
    """公共夹具：临时配置 + MockFetcher + 内存库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.config_path = os.path.join(self.tmp, "config.yaml")
        self.write_config(make_config_dict())
        self.config = load_config(self.config_path)
        self.storage = Storage(":memory:")
        self.monitor = Monitor(
            self.config, MockFetcher(products_per_round=3), self.storage, [],
            config_path=self.config_path,
        )

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def write_config(self, data: dict) -> None:
        with open(self.config_path, "w", encoding="utf-8") as fp:
            yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)


class TestFilterReasonsInRound(MonitorSchedulerCase):
    """M04：一轮结束后能看出"因何被过滤"。"""

    def test_reasons_aggregated(self) -> None:
        """必含词不可能命中 -> 本轮抓到的商品全部记为 missing_required。"""
        data = make_config_dict()
        data["keywords"][0]["required_keywords"] = ["绝不可能出现的必含词"]
        self.write_config(data)
        self.monitor.config = load_config(self.config_path)
        self.monitor.run_once()
        reasons = self.monitor.last_result.filtered_reasons
        self.assertEqual(reasons.get("missing_required"), 3, "3 条商品都应记为缺必含词")
        self.assertGreater(self.monitor.last_result.filtered, 0)

    def test_reasons_empty_when_nothing_filtered(self) -> None:
        self.monitor.run_once()
        self.assertEqual(self.monitor.last_result.filtered_reasons, {})


class TestConfigHotReload(MonitorSchedulerCase):
    """M04：轮次边界热更——改配置后下一轮立即生效，不必重启。"""

    def test_reload_detects_change(self) -> None:
        self.assertFalse(self.monitor.reload_if_config_changed(), "未改动时不应重载")
        self.write_config(make_config_dict(keyword="显卡"))
        self.assertTrue(self.monitor.config.keywords[0].keyword == "Switch")
        self.assertTrue(self.monitor.reload_if_config_changed())
        self.assertEqual(self.monitor.config.keywords[0].keyword, "显卡")
        self.assertFalse(self.monitor.reload_if_config_changed(), "重载后 mtime 已同步")

    def test_reload_keeps_old_config_on_error(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as fp:
            fp.write("keywords: []\nmonitor: {interval_seconds: 不是数字}\n")
        self.assertFalse(self.monitor.reload_if_config_changed())
        self.assertEqual(self.monitor.config.keywords[0].keyword, "Switch", "热更失败必须沿用旧配置")

    def test_reload_disabled_without_path(self) -> None:
        monitor = Monitor(self.config, MockFetcher(), self.storage, [])
        self.assertFalse(monitor.reload_if_config_changed())

    def test_run_forever_applies_reload(self) -> None:
        """跑两轮，中间改配置 -> 第二轮应使用新间隔（热更确实接进了循环）。"""
        self.write_config(make_config_dict(keyword="Switch", interval=1))
        self.monitor.config = load_config(self.config_path)
        rounds = {"n": 0}
        original = self.monitor.reload_if_config_changed

        def counting() -> bool:
            rounds["n"] += 1
            changed = original()
            if rounds["n"] == 1:
                self.write_config(make_config_dict(keyword="显卡", interval=1))
            return changed

        self.monitor.reload_if_config_changed = counting  # type: ignore[method-assign]
        with mock.patch.object(self.monitor, "_interruptible_sleep", return_value=True):
            self.monitor.run_forever(max_rounds=2)
        self.assertGreaterEqual(rounds["n"], 2, "每轮边界都应检查热更")


class TestSingleTimeSource(MonitorSchedulerCase):
    """M05：保活节奏并入主循环，不再与轮次睡眠各走一套。"""

    def test_keepalive_skipped_when_disabled(self) -> None:
        self.monitor.config.monitor.keepalive_enabled = False
        with mock.patch.object(self.monitor, "keepalive_once") as probe:
            self.monitor._maybe_keepalive()
        probe.assert_not_called()

    def test_keepalive_fires_when_due(self) -> None:
        self.monitor.config.monitor.keepalive_enabled = True
        self.monitor.config.monitor.keepalive_interval_seconds = 1800
        self.monitor._last_auth_at = 0.0  # 很久没鉴权 -> 到期
        with mock.patch.object(self.monitor, "keepalive_once", return_value=True) as probe:
            self.assertTrue(self.monitor._maybe_keepalive())
        probe.assert_called_once()

    def test_keepalive_not_due_right_after_round(self) -> None:
        import time as _time

        self.monitor.config.monitor.keepalive_enabled = True
        self.monitor.config.monitor.keepalive_interval_seconds = 1800
        self.monitor._last_auth_at = _time.time()  # 刚鉴权过
        with mock.patch.object(self.monitor, "keepalive_once") as probe:
            self.assertFalse(self.monitor._maybe_keepalive())
        probe.assert_not_called()

    def test_interruptible_sleep_returns_false_on_stop(self) -> None:
        self.monitor._stop = True
        self.assertFalse(self.monitor._interruptible_sleep(5.0), "已请求停止时应立刻返回")

    def test_interruptible_sleep_keeps_alive(self) -> None:
        with mock.patch.object(self.monitor, "_maybe_keepalive") as probe:
            self.assertTrue(self.monitor._interruptible_sleep(0.05))
        probe.assert_called()

    def test_run_forever_checks_keepalive_each_round(self) -> None:
        with mock.patch.object(self.monitor, "_interruptible_sleep", return_value=True), mock.patch.object(
            self.monitor, "_maybe_keepalive"
        ) as probe:
            self.monitor.run_forever(max_rounds=2)
        self.assertGreaterEqual(probe.call_count, 2, "保活检查应与轮次同频（单一时间源）")


class TestRoundMetrics(MonitorSchedulerCase):
    """M05：轮次级指标（耗时 / 抓取 / 过滤 / 命中）。"""

    def test_metrics_after_one_round(self) -> None:
        self.monitor.run_once()
        snap = self.monitor.metrics()
        self.assertEqual(snap["totals"]["rounds"], 1)
        last = snap["last"]
        self.assertIsNotNone(last)
        self.assertGreaterEqual(last["duration_ms"], 0.0)
        self.assertIn("filtered_reasons", last)

    def test_metrics_empty_before_any_round(self) -> None:
        snap = self.monitor.metrics()
        self.assertIsNone(snap["last"])
        self.assertEqual(snap["totals"]["rounds"], 0)

    def test_metrics_records_failures(self) -> None:
        from xianyu_alert.fetcher import MockFetcher as _Mock

        self.monitor.fetcher = _Mock(fail_rounds={1})
        self.monitor.run_once()
        snap = self.monitor.metrics()
        self.assertEqual(snap["totals"]["failed_keywords"], 1)


if __name__ == "__main__":
    unittest.main()
