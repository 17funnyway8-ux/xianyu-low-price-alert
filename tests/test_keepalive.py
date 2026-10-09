"""空闲保活专项测试（v1.10.5，M07）。

重评时发现：`xianyu_alert/keepalive.py` 虽然被间接覆盖到 87%，但**没有专项测试文件**，
缺口正好在**线程生命周期**（start/stop/_loop）—— 而这恰恰是 v1.9.1 出过 bug 的地方
（保活线程接线错误导致界面误报"未运行"）。

因此本文件的重点是：
    1. `keepalive_due` 纯函数的全部边界（开关/下限/从未鉴权/未到期/到期）；
    2. `tick_once` 的三条异常路径（配置读取失败 / probe 返回 False / probe 抛异常）；
    3. **线程真的在跑、且能干净停下**（start → 多次巡检 → stop）。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.keepalive import (  # noqa: E402
    DEFAULT_KEEPALIVE_INTERVAL,
    MIN_KEEPALIVE_INTERVAL,
    CookieKeeper,
    keepalive_due,
)


class TestKeepaliveDue(unittest.TestCase):
    """纯函数判定：所有分支都要有断言。"""

    def test_disabled_never_due(self) -> None:
        self.assertFalse(
            keepalive_due(now=10_000, last_auth_at=0, interval=1800, enabled=False)
        )

    def test_interval_below_minimum_is_treated_as_off(self) -> None:
        """低于下限视为关闭（防止误配成 1 秒把请求打爆）。"""
        self.assertFalse(
            keepalive_due(now=10_000, last_auth_at=0, interval=MIN_KEEPALIVE_INTERVAL - 1)
        )

    def test_never_authenticated_is_due(self) -> None:
        self.assertTrue(keepalive_due(now=10_000, last_auth_at=0, interval=1800))

    def test_not_due_yet(self) -> None:
        self.assertFalse(keepalive_due(now=1_000, last_auth_at=900, interval=1800))

    def test_due_after_interval(self) -> None:
        self.assertTrue(keepalive_due(now=3_000, last_auth_at=900, interval=1800))

    def test_exactly_at_interval_is_due(self) -> None:
        self.assertTrue(keepalive_due(now=2_700, last_auth_at=900, interval=1800))

    def test_default_interval_is_sane(self) -> None:
        self.assertGreaterEqual(DEFAULT_KEEPALIVE_INTERVAL, MIN_KEEPALIVE_INTERVAL)


class TestTickOnce(unittest.TestCase):
    """单次巡检的三条结果路径。"""

    def _keeper(self, probe, settings) -> CookieKeeper:
        return CookieKeeper(probe=probe, settings=settings)

    def test_not_due_does_not_call_probe(self) -> None:
        calls = []
        keeper = self._keeper(lambda: calls.append(1) or True,
                              lambda: {"enabled": True, "interval": 1800, "last_auth_at": time.time()})
        self.assertFalse(keeper.tick_once())
        self.assertEqual(calls, [], "未到期不应发请求")

    def test_due_calls_probe_and_counts_success(self) -> None:
        keeper = self._keeper(lambda: True, lambda: {"enabled": True, "interval": 1800, "last_auth_at": 0})
        self.assertTrue(keeper.tick_once())
        self.assertEqual(keeper.success_count, 1)
        self.assertEqual(keeper.failure_count, 0)
        self.assertGreater(keeper.last_attempt_at, 0)

    def test_probe_false_counts_failure(self) -> None:
        keeper = self._keeper(lambda: False, lambda: {"enabled": True, "interval": 1800, "last_auth_at": 0})
        self.assertTrue(keeper.tick_once(), "执行了保活（只是结果失败）")
        self.assertEqual(keeper.failure_count, 1)
        self.assertEqual(keeper.success_count, 0)

    def test_probe_exception_is_swallowed(self) -> None:
        def boom() -> bool:
            raise RuntimeError("网络炸了")

        keeper = self._keeper(boom, lambda: {"enabled": True, "interval": 1800, "last_auth_at": 0})
        self.assertTrue(keeper.tick_once(), "异常不应打断线程")
        self.assertEqual(keeper.failure_count, 1)

    def test_settings_exception_is_swallowed(self) -> None:
        def bad_settings() -> dict:
            raise RuntimeError("配置读取失败")

        keeper = self._keeper(lambda: True, bad_settings)
        self.assertFalse(keeper.tick_once())
        self.assertEqual(keeper.success_count, 0)

    def test_disabled_settings_skip(self) -> None:
        calls = []
        keeper = self._keeper(lambda: calls.append(1) or True,
                              lambda: {"enabled": False, "interval": 1800, "last_auth_at": 0})
        self.assertFalse(keeper.tick_once())
        self.assertEqual(calls, [])


class TestKeeperLifecycle(unittest.TestCase):
    """线程生命周期 —— v1.9.1 出过 bug 的地方，必须有直接断言。"""

    def test_tick_is_clamped_to_minimum(self) -> None:
        keeper = CookieKeeper(probe=lambda: True, settings=dict, tick=0.01)
        self.assertGreaterEqual(keeper._tick, 5.0, "巡检间隔应被下限保护")

    def test_start_stop_and_running_flag(self) -> None:
        calls = []
        keeper = CookieKeeper(probe=lambda: calls.append(1) or True,
                              settings=lambda: {"enabled": True, "interval": MIN_KEEPALIVE_INTERVAL,
                                                "last_auth_at": 0})
        self.assertFalse(keeper.running, "未启动时 running 必须为 False")
        keeper.start()
        try:
            self.assertTrue(keeper.running, "启动后 running 必须为 True（v1.9.1 的教训）")
        finally:
            keeper.stop()
        self.assertFalse(keeper.running)
        self.assertEqual(keeper.snapshot()["running"], False)

    def test_start_is_idempotent(self) -> None:
        keeper = CookieKeeper(probe=lambda: True, settings=lambda: {"enabled": False})
        keeper.start()
        first = keeper._thread
        keeper.start()
        try:
            self.assertIs(keeper._thread, first, "重复 start 不应起第二个线程")
        finally:
            keeper.stop()

    def test_stop_is_idempotent(self) -> None:
        keeper = CookieKeeper(probe=lambda: True, settings=lambda: {"enabled": False})
        keeper.stop()
        keeper.stop()
        self.assertFalse(keeper.running)

    def test_loop_repeatedly_ticks(self) -> None:
        """线程真的在按 tick 巡检（把 tick 调小以免测试变慢）。"""
        calls = []
        keeper = CookieKeeper(probe=lambda: calls.append(1) or True,
                              settings=lambda: {"enabled": True, "interval": MIN_KEEPALIVE_INTERVAL,
                                                "last_auth_at": 0})
        keeper._tick = 0.05  # 绕过下限，仅为测试提速
        keeper.start()
        try:
            deadline = time.time() + 2.0
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.02)
        finally:
            keeper.stop()
        self.assertGreaterEqual(len(calls), 2, "线程应周期性地执行保活")

    def test_snapshot_shape(self) -> None:
        keeper = CookieKeeper(probe=lambda: True, settings=dict)
        payload = keeper.snapshot()
        self.assertEqual(
            set(payload),
            {"running", "success_count", "failure_count", "last_attempt_at"},
        )


if __name__ == "__main__":
    unittest.main()
