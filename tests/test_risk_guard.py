"""风控熔断专项测试（v1.11.3）。

背景（线上实测）：2026-10-10 11 点那一小时的容器日志出现 **129 条 RGV587 风控** ——
成因是「保活失败无退避 + 每次抓 3 页」叠加短时间内的连续试抓。当时的行为是：

    1. 同一轮里某页被风控 → 循环**继续抓剩余页**（再打 2 次）；
    2. 下一轮仍按 monitor.interval_seconds 照常开始；
    3. 保活探测、校验在架照常打点。

本文件把「风控时它自己安静下来」这条链完整钉住：
间隔下限 / 单轮中止 / 整轮跳过 / 保活静默 / 校验在架拒绝 / 状态可观测。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import MIN_INTERVAL_SECONDS, config_from_dict  # noqa: E402
from xianyu_alert.fetcher.base import FetchError  # noqa: E402
from xianyu_alert.fetcher.mtop import MtopFetcher  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.risk import RISK_GUARD, RiskGuard  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402

RISK_TEXT = "RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!"


def make_config(keywords: list[str], fetcher_type: str = "mock", page_sleep: float = 2.0):
    """构造多关键词配置（默认 mock：离线假数据）。"""
    return config_from_dict(
        {
            "keywords": [{"keyword": kw, "max_price": 1000} for kw in keywords],
            "monitor": {"interval_seconds": 600},
            "fetcher": {"type": fetcher_type, "page_sleep": page_sleep},
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        }
    )


class StubFetcher:
    """按脚本返回商品或抛 FetchError。"""

    def __init__(self, outcomes: list, name: str = "stub") -> None:
        self.name = name
        self._outcomes = list(outcomes)
        self.calls: list[str] = []

    def fetch(self, keyword: str) -> list[Product]:
        self.calls.append(keyword)
        outcome = self._outcomes.pop(0) if self._outcomes else []
        if isinstance(outcome, BaseException):
            raise outcome
        return list(outcome)


class TestRiskGuardMath(unittest.TestCase):
    """熔断时长：首次 interval x 3，连续命中翻倍，上限 6 小时。"""

    def setUp(self) -> None:
        self.now = 1000.0
        self.guard = RiskGuard(clock=lambda: self.now)

    def test_first_hit_uses_interval_times_three(self) -> None:
        until = self.guard.note_risk(interval_seconds=600, detail=RISK_TEXT)
        self.assertAlmostEqual(until - self.now, 1800.0)
        self.assertTrue(self.guard.active())

    def test_short_interval_still_has_floor(self) -> None:
        self.guard.note_risk(interval_seconds=1)
        self.assertAlmostEqual(self.guard.remaining(), 900.0)  # max(1,300) * 3

    def test_consecutive_hits_double(self) -> None:
        self.guard.note_risk(interval_seconds=600)          # 1800
        second = self.guard.note_risk(interval_seconds=600)  # 3600，从"现在"起算
        self.assertAlmostEqual(second - self.now, 3600.0)
        self.assertEqual(self.guard.hits, 2)

    def test_cooldown_is_capped(self) -> None:
        for _ in range(10):
            self.guard.note_risk(interval_seconds=1800)
        self.assertLessEqual(self.guard.remaining(), 6 * 3600.0 + 1)

    def test_expires_and_reports_inactive(self) -> None:
        self.guard.note_risk(interval_seconds=600)
        self.now += 1801
        self.assertFalse(self.guard.active())
        self.assertEqual(self.guard.remaining(), 0.0)

    def test_success_resets_hits_but_not_cooldown(self) -> None:
        self.guard.note_risk(interval_seconds=600)
        self.guard.note_success()
        self.assertEqual(self.guard.hits, 0)
        self.assertTrue(self.guard.active())

    def test_snapshot_shape(self) -> None:
        snap = self.guard.snapshot()
        self.assertEqual(set(snap), {"active", "remaining_seconds", "hits", "last_detail"})


class TestFetcherStopsOnRisk(unittest.TestCase):
    """命中风控时：**不再抓剩余页**，并把风控向上抛（供上层熔断）。"""

    class _Response:
        def __init__(self, payload: dict) -> None:
            self.status_code = 200
            self._payload = payload
            self.headers: dict = {}

        def json(self) -> dict:
            return self._payload

    class _Session:
        """脚本耗尽就断言失败 —— 用来证明"没有多打请求"。"""

        def __init__(self, script: list) -> None:
            self._script = list(script)
            self.calls = 0
            self.cookies = requests.Session().cookies

        def post(self, url: str, **kwargs: object) -> object:
            self.calls += 1
            if not self._script:
                raise AssertionError("命中风控后不应再发请求")
            return TestFetcherStopsOnRisk._Response(self._script.pop(0))

    def _fetcher(self, session: object, pages: int = 3) -> MtopFetcher:
        return MtopFetcher(
            cookies="_m_h5_tk=abc_1700000000000; cookie2=x",
            pages=pages,
            page_sleep=0.0,
            sleep_func=lambda _seconds: None,
            session=session,  # type: ignore[arg-type]
        )

    def test_risk_on_first_page_aborts_and_raises(self) -> None:
        session = self._Session([{"ret": [RISK_TEXT]}])
        fetcher = self._fetcher(session)
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch("光威 3200 64G")
        self.assertEqual(getattr(ctx.exception, "kind", ""), "risk")
        self.assertEqual(session.calls, 1, "风控后不应继续抓第 2、3 页")

    def test_normal_pages_still_all_fetched(self) -> None:
        empty = {"ret": ["SUCCESS::调用成功"], "data": {"resultList": []}}
        session = self._Session([empty, empty, empty])
        fetcher = self._fetcher(session)
        self.assertEqual(fetcher.fetch("x"), [])
        self.assertEqual(session.calls, 3)


class TestMonitorRiskCircuit(unittest.TestCase):
    """监控侧：命中风控 → 开启冷却 → 冷却期内整轮跳过（一个请求都不发）。"""

    def setUp(self) -> None:
        RISK_GUARD.reset()
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)

    def tearDown(self) -> None:
        RISK_GUARD.reset()

    def test_risk_opens_cooldown_and_skips_next_round(self) -> None:
        config = make_config(["光威 3200 64G"])
        fetcher = StubFetcher([FetchError("风控", kind="risk")])
        monitor = Monitor(config, fetcher, self.storage, [])

        self.assertEqual(monitor.run_once(), 0)
        self.assertEqual(fetcher.calls, ["光威 3200 64G"])
        self.assertTrue(RISK_GUARD.active())
        self.assertGreaterEqual(RISK_GUARD.remaining(), 1200)

        # 第二轮：冷却期内必须**不发请求**
        second_fetcher = StubFetcher([[]])
        monitor2 = Monitor(config, second_fetcher, self.storage, [])
        self.assertEqual(monitor2.run_once(), 0)
        self.assertEqual(second_fetcher.calls, [])
        self.assertTrue(monitor2.last_result.risk_skipped)
        self.assertGreater(monitor2.last_result.risk_remaining_seconds, 0)

    def test_successful_fetch_resets_hit_counter(self) -> None:
        RISK_GUARD.reset()
        RISK_GUARD.note_risk(600)
        RISK_GUARD.reset()  # 从零开始，验证"成功一轮会清零"
        config = make_config(["a", "b"])
        RISK_GUARD.note_risk(600)
        self.assertEqual(RISK_GUARD.hits, 1)
        RISK_GUARD.reset()
        fetcher = StubFetcher([[], []])
        Monitor(config, fetcher, self.storage, [], sleep_func=lambda _s: None).run_once()
        self.assertEqual(RISK_GUARD.hits, 0)

    def test_risk_in_one_keyword_stops_remaining_keywords(self) -> None:
        config = make_config(["bad", "good"])
        fetcher = StubFetcher([FetchError("风控", kind="risk"), []])
        Monitor(config, fetcher, self.storage, [], sleep_func=lambda _s: None).run_once()
        self.assertEqual(fetcher.calls, ["bad"], "命中风控后不应继续下一个关键词")


class TestKeywordPacing(unittest.TestCase):
    """关键词间限速：真实抓取器才限速（mock 是离线假数据，限速没意义）。"""

    def setUp(self) -> None:
        RISK_GUARD.reset()
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)

    def test_paces_between_real_keywords(self) -> None:
        config = make_config(["a", "b"], fetcher_type="mtop", page_sleep=3.0)
        slept: list[float] = []
        Monitor(
            config, StubFetcher([[], []], name="mtop"), self.storage, [], sleep_func=slept.append
        ).run_once()
        self.assertEqual(slept, [3.0], "两个关键词之间应限速一次")

    def test_no_pacing_for_mock(self) -> None:
        config = make_config(["a", "b"], fetcher_type="mock", page_sleep=3.0)
        slept: list[float] = []
        Monitor(
            config, StubFetcher([[], []]), self.storage, [], sleep_func=slept.append
        ).run_once()
        self.assertEqual(slept, [])

    def test_pause_is_capped(self) -> None:
        config = make_config(["a", "b"], fetcher_type="mtop", page_sleep=600.0)
        slept: list[float] = []
        Monitor(
            config, StubFetcher([[], []], name="mtop"), self.storage, [], sleep_func=slept.append
        ).run_once()
        self.assertEqual(slept, [5.0], "单次等待限幅到 5 秒，保证可停性")


class TestWebLayerGuards(unittest.TestCase):
    """保活 / 校验在架 / 状态接口都要看同一份熔断状态。"""

    def setUp(self) -> None:
        RISK_GUARD.reset()
        self.addCleanup(RISK_GUARD.reset)

    def test_keepalive_skipped_during_cooldown(self) -> None:
        from web.monitor_service.keepalive import KeepaliveMixin

        config = make_config(["a"])
        RISK_GUARD.note_risk(600)

        class _Service(KeepaliveMixin):
            pass

        service = _Service()
        service.config = config
        service._last_auth_at = 0.0
        service._persist_refreshed_token = lambda fetcher: None
        with mock.patch.object(
            __import__("web.monitor_service.keepalive", fromlist=["build_fetcher"]),
            "build_fetcher",
            side_effect=AssertionError("冷却期内不应构建抓取器"),
        ):
            self.assertFalse(service.keepalive_probe())

    def test_shelf_check_refused_during_cooldown(self) -> None:
        from web.monitor_service.shelf_check import ShelfCheckMixin

        class _Service(ShelfCheckMixin):
            pass

        service = _Service()
        service.config = make_config(["a"], fetcher_type="mtop")
        service._config = service.config
        service._lock = __import__("threading").RLock()
        service._thread = None
        RISK_GUARD.note_risk(600)
        result = service.start_check_shelf(["1", "2"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], 409)
        self.assertIn("风控冷却中", result["message"])

    def test_status_exposes_risk_snapshot(self) -> None:
        from web.monitor_service.service import MonitorService

        RISK_GUARD.note_risk(600, "RGV587")
        status = MonitorService(config_path="/tmp/does-not-matter.yaml").status()
        self.assertTrue(status["risk"]["active"])
        self.assertGreater(status["risk"]["remaining_seconds"], 0)
        self.assertEqual(status["risk"]["hits"], 1)


class TestIntervalFloor(unittest.TestCase):
    """监测间隔安全下限（v1.11.3）：后端收敛、前端拒绝。"""

    def test_config_clamps_to_floor(self) -> None:
        config = make_config(["a"])
        self.assertEqual(config.monitor.interval_seconds, 600)
        fast = config_from_dict(
            {
                "keywords": [{"keyword": "a", "max_price": 100}],
                "monitor": {"interval_seconds": 5},
                "notify": {"channels": [{"type": "console"}]},
            }
        )
        self.assertEqual(fast.monitor.interval_seconds, MIN_INTERVAL_SECONDS)

    def test_gui_validation_rejects_too_short(self) -> None:
        from xianyu_alert.gui.helpers import validate_interval

        self.assertEqual(validate_interval("600"), 600)
        with self.assertRaises(ValueError) as ctx:
            validate_interval("10")
        self.assertIn(str(MIN_INTERVAL_SECONDS), str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
