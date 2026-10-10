"""账号安全相关的 v1.11.4 增强测试。

覆盖四件事：
    1. **请求计量**（reqmeter）：近 10 分钟 / 近 1 小时 / 累计，供状态页与 /healthz 展示；
    2. **自动开始监控**（monitor.autostart）：重启后不再"静默不监控"；
    3. **默认 UA 与环境一致性**：默认值跟随当前 Chrome 主版本，且允许用户对齐自己的浏览器；
    4. **校验在架限流收紧**：1.5s → 3s、30 条 → 20 条。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import reqmeter as reqmeter_mod  # noqa: E402
from xianyu_alert.config import DEFAULT_USER_AGENT, config_from_dict  # noqa: E402
from xianyu_alert.gui.constants import (  # noqa: E402
    SOLD_CHECK_INTERVAL,
    SOLD_CHECK_MAX_ITEMS,
)
from xianyu_alert.reqmeter import RequestMeter  # noqa: E402


def make_config(**monitor_extra: object) -> dict:
    """构造一份最小可加载的配置字典。"""
    monitor = {"interval_seconds": 600}
    monitor.update(monitor_extra)
    return {
        "keywords": [{"keyword": "Switch", "max_price": 1000}],
        "monitor": monitor,
        "fetcher": {"type": "mock", "mock_products_per_round": 1},
        "storage": {"path": ":memory:"},
        "notify": {"channels": [{"type": "console"}]},
    }


class TestRequestMeter(unittest.TestCase):
    """计量器：窗口统计必须随注入时钟推进。"""

    def setUp(self) -> None:
        self.now = 10_000.0
        self.meter = RequestMeter(clock=lambda: self.now)

    def test_counts_by_window(self) -> None:
        self.meter.note("search")
        self.meter.note("search")
        self.now += 300          # +5 分钟
        self.meter.note("detail")
        snap = self.meter.snapshot()
        self.assertEqual(snap["total"], 3)
        self.assertEqual(snap["last_10min"], 3)
        self.assertEqual(snap["last_hour"], 3)
        self.assertEqual(snap["kinds"], {"search": 2, "detail": 1})

    def test_old_requests_fall_out_of_window(self) -> None:
        self.meter.note("search")
        self.now += 4000         # 超过 1 小时
        self.meter.note("search")
        snap = self.meter.snapshot()
        self.assertEqual(snap["total"], 2)      # 累计不衰减
        self.assertEqual(snap["last_hour"], 1)  # 窗口内只剩 1 次
        self.assertEqual(snap["last_10min"], 1)

    def test_ten_minute_window_is_narrower(self) -> None:
        self.meter.note("search")
        self.now += 900          # 15 分钟前的那次已出 10 分钟窗口
        self.meter.note("search")
        snap = self.meter.snapshot()
        self.assertEqual(snap["last_10min"], 1)
        self.assertEqual(snap["last_hour"], 2)

    def test_snapshot_shape_and_reset(self) -> None:
        snap = self.meter.snapshot()
        self.assertEqual(
            set(snap), {"total", "last_10min", "last_hour", "last_request_at", "kinds"}
        )
        self.assertIsNone(snap["last_request_at"])
        self.meter.note("search")
        self.assertIsNotNone(self.meter.snapshot()["last_request_at"])
        self.meter.reset()
        self.assertEqual(self.meter.snapshot()["total"], 0)

    def test_fetcher_counts_every_network_attempt(self) -> None:
        """抓取器每次真实出网（含重试）都要计入。"""
        import requests

        from xianyu_alert.fetcher.mtop import MtopFetcher

        class _Resp:
            status_code = 200
            headers: dict = {}

            def json(self) -> dict:
                return {"ret": ["SUCCESS::调用成功"], "data": {"resultList": []}}

        class _Session:
            def __init__(self) -> None:
                self.cookies = requests.Session().cookies
                self.calls = 0

            def post(self, url: str, **kwargs: object) -> object:
                self.calls += 1
                return _Resp()

        reqmeter_mod.REQ_METER.reset()
        session = _Session()
        fetcher = MtopFetcher(
            cookies="_m_h5_tk=abc_1700000000000; cookie2=x",
            pages=2,
            page_sleep=0.0,
            sleep_func=lambda _s: None,
            session=session,  # type: ignore[arg-type]
        )
        fetcher.fetch("Switch")
        self.assertEqual(session.calls, 2)
        self.assertEqual(reqmeter_mod.REQ_METER.snapshot()["total"], 2)
        reqmeter_mod.REQ_METER.reset()


class TestHourlyRequestCap(unittest.TestCase):
    """v1.11.8：每小时请求硬上限 —— 拦住任何"失控循环"的最后一道兜底。"""

    def setUp(self) -> None:
        from xianyu_alert.risk import RISK_GUARD

        RISK_GUARD.reset()
        reqmeter_mod.REQ_METER.reset()
        self.addCleanup(RISK_GUARD.reset)
        self.addCleanup(reqmeter_mod.REQ_METER.reset)
        from xianyu_alert.storage import Storage

        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)

    def _monitor(self, limit: int):
        from xianyu_alert.monitor import Monitor

        config = config_from_dict(make_config(max_requests_per_hour=limit))
        return Monitor(config, _NullFetcher(), self.storage, [])

    def test_round_skipped_when_hourly_cap_reached(self) -> None:
        monitor = self._monitor(limit=3)
        for _ in range(3):
            reqmeter_mod.REQ_METER.note("search")
        self.assertEqual(monitor.run_once(), 0)
        self.assertTrue(monitor.last_result.rate_limited)
        self.assertEqual(monitor.last_result.requests_last_hour, 3)
        self.assertEqual(monitor.fetcher.calls, [], "触发上限时不得发起任何请求")

    def test_round_runs_below_cap(self) -> None:
        monitor = self._monitor(limit=10)
        reqmeter_mod.REQ_METER.note("search")
        monitor.run_once()
        self.assertFalse(monitor.last_result.rate_limited)
        self.assertEqual(len(monitor.fetcher.calls), 1)

    def test_cap_can_be_disabled(self) -> None:
        monitor = self._monitor(limit=0)
        for _ in range(500):
            reqmeter_mod.REQ_METER.note("search")
        monitor.run_once()
        self.assertFalse(monitor.last_result.rate_limited)

    def test_keepalive_respects_cap(self) -> None:
        monitor = self._monitor(limit=2)
        for _ in range(2):
            reqmeter_mod.REQ_METER.note("search")
        config = config_from_dict(make_config(max_requests_per_hour=2))
        config.monitor.cookies = "cookie2=x; _m_h5_tk=abc_1700000000000"
        monitor.config = config
        self.assertFalse(monitor.keepalive_once())
        self.assertEqual(monitor.fetcher.calls, [])


class _NullFetcher:
    """记录调用、永不返回商品的假抓取器（mock 类型 → 不触发关键词间限速）。"""

    name = "mock"

    def __init__(self) -> None:
        self.pages = 1
        self.calls: list[str] = []

    def fetch(self, keyword: str) -> list:
        self.calls.append(keyword)
        return []


class TestRiskCooldownPersistence(unittest.TestCase):
    """v1.11.5：风控冷却必须**跨重启恢复** —— 否则重启等于把刚被限流的账号再捅一下。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="xy-risk-")
        self.path = os.path.join(self.tmp, "risk_cooldown.json")

    def test_cooldown_survives_restart(self) -> None:
        from xianyu_alert.risk import RiskGuard

        first = RiskGuard()
        first.enable_persistence(self.path)
        first.note_risk(interval_seconds=600, detail="RGV587")
        self.assertTrue(os.path.isfile(self.path))

        # 模拟进程重启：新的 Guard 从同一份文件恢复
        second = RiskGuard()
        second.enable_persistence(self.path)
        self.assertTrue(second.active())
        self.assertGreater(second.remaining(), 0)
        self.assertGreaterEqual(second.hits, 1)
        self.assertIn("RGV587", str(second.snapshot()["last_detail"]))

    def test_missing_or_broken_file_is_ignored(self) -> None:
        from xianyu_alert.risk import RiskGuard

        guard = RiskGuard()
        guard.enable_persistence(self.path)  # 文件不存在
        self.assertFalse(guard.active())
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        broken = RiskGuard()
        broken.enable_persistence(self.path)
        self.assertFalse(broken.active())

    def test_reset_clears_persisted_state(self) -> None:
        from xianyu_alert.risk import RiskGuard

        guard = RiskGuard()
        guard.enable_persistence(self.path)
        guard.note_risk(600)
        guard.reset()
        revived = RiskGuard()
        revived.enable_persistence(self.path)
        self.assertFalse(revived.active())


class TestMonitorKeepaliveProbe(unittest.TestCase):
    """v1.11.5：monitor 侧保活也要"优先启用词 + 只抓 1 页"。"""

    class _Fetcher:
        """记录每次 fetch 的关键词与当时的 pages。"""

        name = "mtop"

        def __init__(self) -> None:
            self.pages = 3
            self.calls: list[tuple[str, int]] = []

        def fetch(self, keyword: str) -> list:
            self.calls.append((keyword, self.pages))
            return []

        def set_cookies(self, cookie: str) -> None:
            pass

    def _monitor(self, fetcher: object):
        from xianyu_alert.monitor import Monitor
        from xianyu_alert.storage import Storage

        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        config = config_from_dict(make_config())
        config.keywords[0].enabled = False          # 第一个词停用
        config.monitor.cookies = "cookie2=x; _m_h5_tk=abc_1700000000000"
        return Monitor(config, fetcher, storage, [])

    def test_prefers_enabled_keyword_and_fetches_one_page(self) -> None:
        fetcher = self._Fetcher()
        monitor = self._monitor(fetcher)
        self.assertTrue(monitor.keepalive_once())
        self.assertEqual(fetcher.calls, [("Switch", 1)])
        self.assertEqual(fetcher.pages, 3, "调用结束后必须还原 pages")

    def test_failed_keepalive_does_not_become_one_request_per_second(self) -> None:
        """v1.11.7 热修：保活失败后不得"每秒一次请求"。

        线上事故：保活判断挂在 1 秒分片睡眠上，失败时不更新 last_auth_at →
        keepalive_due 恒为真 → **每秒一次真实请求**（实测 80 秒 83 次），
        而且保活失败没有 arm 熔断，等于裸奔重试。
        """
        import time as time_mod

        from xianyu_alert.fetcher.base import FetchError
        from xianyu_alert.keepalive import keepalive_due
        from xianyu_alert.risk import RISK_GUARD

        class _FailFetcher(self._Fetcher):
            def fetch(self, keyword: str) -> list:
                self.calls.append((keyword, self.pages))
                raise FetchError("命中闲鱼风控", kind="risk")

        RISK_GUARD.reset()
        self.addCleanup(RISK_GUARD.reset)
        fetcher = _FailFetcher()
        monitor = self._monitor(fetcher)

        self.assertFalse(monitor.keepalive_once())
        self.assertEqual(len(fetcher.calls), 1)
        # 1) 熔断被 arm（保活也要 arm，否则其它入口继续裸奔）
        self.assertTrue(RISK_GUARD.active())
        self.assertGreaterEqual(RISK_GUARD.remaining(), 300)
        # 2) 即使没有熔断，间隔判定也必须把重试钉死在 keepalive_interval 上
        snapshot = monitor.auth_snapshot()
        self.assertGreater(float(snapshot["last_auth_at"]), 0.0)
        self.assertFalse(
            keepalive_due(
                now=time_mod.time(),
                last_auth_at=float(snapshot["last_auth_at"]),
                interval=int(snapshot["interval"]),
                enabled=True,
            ),
            "刚尝试过就不该立刻再试（否则 1 秒分片睡眠会变成每秒一次请求）",
        )

    def test_risk_skip_log_is_throttled(self) -> None:
        """v1.11.6：冷却期跳过日志每 5 分钟最多一条。

        保活判断挂在 1 秒分片睡眠上 —— 不降频就会每秒打一行（线上实测刷屏）。
        """
        from xianyu_alert.monitor import RISK_SKIP_LOG_INTERVAL
        from xianyu_alert.risk import RISK_GUARD

        fetcher = self._Fetcher()
        monitor = self._monitor(fetcher)
        RISK_GUARD.reset()
        RISK_GUARD.note_risk(600)
        self.addCleanup(RISK_GUARD.reset)

        with self.assertLogs("xianyu_alert.monitor", level="WARNING") as captured:
            for _ in range(50):          # 模拟 50 次 1 秒分片回调
                self.assertFalse(monitor.keepalive_once())
        skipped = [line for line in captured.output if "风控冷却中" in line]
        self.assertEqual(len(skipped), 1, "同一冷却窗口内只应打一条跳过日志")
        self.assertEqual(fetcher.calls, [], "冷却期内不得发起保活请求")
        self.assertGreaterEqual(RISK_SKIP_LOG_INTERVAL, 60)


class TestAutostart(unittest.TestCase):
    """monitor.autostart：配置为 true 时服务启动即接上监控。"""

    def _service(self, autostart: bool):
        from web.monitor_service.service import MonitorService

        tmp = tempfile.mkdtemp(prefix="xy-autostart-")
        path = os.path.join(tmp, "config.yaml")
        import yaml

        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(make_config(autostart=autostart), handle, allow_unicode=True)
        service = MonitorService(config_path=path)
        self.addCleanup(service.shutdown)
        return service

    def test_disabled_keeps_old_behaviour(self) -> None:
        service = self._service(autostart=False)
        self.assertFalse(service.autostart_if_configured())
        self.assertFalse(service.status()["running"])

    def test_enabled_starts_monitoring(self) -> None:
        service = self._service(autostart=True)
        self.assertTrue(service.autostart_if_configured())
        self.assertTrue(service.status()["running"])
        # 幂等：再调一次不应重复启动
        self.assertFalse(service.autostart_if_configured())


class TestUserAgent(unittest.TestCase):
    """UA 与环境一致性：默认跟随当前 Chrome 主版本，且用户可对齐自己的浏览器。"""

    def test_default_ua_is_recent_chrome(self) -> None:
        self.assertIn("Chrome/", DEFAULT_USER_AGENT)
        self.assertIn("Mozilla/5.0", DEFAULT_USER_AGENT)
        version = int(DEFAULT_USER_AGENT.split("Chrome/")[1].split(".")[0])
        self.assertGreaterEqual(version, 130, "默认 UA 不应停留在过旧的 Chrome 版本")

    def test_custom_ua_is_kept(self) -> None:
        mac_ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
        )
        config = config_from_dict(make_config(user_agent=mac_ua))
        self.assertEqual(config.monitor.user_agent, mac_ua)

    def test_empty_ua_falls_back_to_default(self) -> None:
        config = config_from_dict(make_config(user_agent=""))
        self.assertEqual(config.monitor.user_agent, DEFAULT_USER_AGENT)


class TestShelfCheckThrottle(unittest.TestCase):
    """校验在架的限流参数（v1.11.4 收紧）。"""

    def test_interval_tightened(self) -> None:
        self.assertGreaterEqual(SOLD_CHECK_INTERVAL, 3.0)

    def test_max_items_lowered(self) -> None:
        self.assertLessEqual(SOLD_CHECK_MAX_ITEMS, 20)

    def test_qt_worker_matches_gui_constants(self) -> None:
        """Qt 版此前是**另一份硬编码副本**，必须与 gui.constants 同步。

        这里**解析源码文本**而不是 import：主测试矩阵（ubuntu / macos / windows）
        没有装 PySide6，import 会在 CI 直接炸（本地因为装了 Qt 反而看不出来）。
        """
        import re
        from pathlib import Path

        import xianyu_alert.gui_qt as gui_qt_pkg

        source = (Path(gui_qt_pkg.__file__).parent / "workers.py").read_text(encoding="utf-8")
        interval = float(re.search(r"^SOLD_CHECK_INTERVAL = ([0-9.]+)", source, re.M).group(1))
        items = int(re.search(r"^SOLD_CHECK_MAX_ITEMS = ([0-9]+)", source, re.M).group(1))
        self.assertEqual(interval, SOLD_CHECK_INTERVAL)
        self.assertEqual(items, SOLD_CHECK_MAX_ITEMS)


if __name__ == "__main__":
    unittest.main()
