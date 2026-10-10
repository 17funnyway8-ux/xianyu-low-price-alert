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
        """Qt 版此前是**另一份复制**，必须与 gui.constants 同步。"""
        from xianyu_alert.gui_qt import workers

        self.assertEqual(workers.SOLD_CHECK_INTERVAL, SOLD_CHECK_INTERVAL)
        self.assertEqual(workers.SOLD_CHECK_MAX_ITEMS, SOLD_CHECK_MAX_ITEMS)


if __name__ == "__main__":
    unittest.main()
