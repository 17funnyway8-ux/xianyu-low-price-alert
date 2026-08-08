"""Monitor 核心逻辑测试：MockFetcher + 内存 SQLite，不访问外网。"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import Config, config_from_dict  # noqa: E402
from xianyu_alert.fetcher import FetchError, MockFetcher  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.notifier import ConsoleNotifier, Notifier  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402

KEYWORD = "Switch"
MAX_PRICE = 1000.0


class RecordingNotifier(Notifier):
    """记录收到的商品，便于断言。"""

    name = "recording"

    def __init__(self) -> None:
        self.received: List[Product] = []
        self.calls: int = 0

    def notify(self, products: List[Product]) -> None:
        self.calls += 1
        self.received.extend(products)


def make_config(
    max_price: float = MAX_PRICE,
    keywords: List[str] = None,
    interval: int = 60,
) -> Config:
    """构造测试配置。"""
    keywords = keywords or [KEYWORD]
    return config_from_dict(
        {
            "keywords": [{"keyword": k, "max_price": max_price} for k in keywords],
            "monitor": {"interval_seconds": interval},
            "fetcher": {"type": "mock"},
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        }
    )


class TestMockFetcher(unittest.TestCase):
    """MockFetcher 的确定性与轮次窗口行为。"""

    def test_deterministic(self) -> None:
        """相同 (关键词, 索引) 应生成完全相同的商品。"""
        a = MockFetcher().make_product(KEYWORD, 0)
        b = MockFetcher().make_product(KEYWORD, 0)
        self.assertEqual(a, b)

    def test_cheap_and_expensive_mix(self) -> None:
        """每轮应同时包含低价与高价商品。"""
        products = MockFetcher(products_per_round=5).fetch(KEYWORD)
        self.assertEqual(len(products), 5)
        self.assertTrue(any(p.price < MAX_PRICE for p in products))
        self.assertTrue(any(p.price >= MAX_PRICE for p in products))
        self.assertTrue(all(p.keyword == KEYWORD for p in products))
        self.assertTrue(all(p.url.startswith("https://www.goofish.com/item?id=") for p in products))
        self.assertTrue(all(p.publish_time for p in products))

    def test_round_window_overlap(self) -> None:
        """相邻两轮应有 n-1 个重叠商品（用于验证「新商品」判定）。"""
        fetcher = MockFetcher(products_per_round=5)
        first = {p.product_id for p in fetcher.fetch(KEYWORD)}
        second = {p.product_id for p in fetcher.fetch(KEYWORD)}
        self.assertEqual(len(first & second), 4)
        self.assertEqual(len(second - first), 1)

    def test_fail_rounds(self) -> None:
        """指定轮次应抛出 FetchError。"""
        fetcher = MockFetcher(products_per_round=3, fail_rounds=[1])
        with self.assertRaises(FetchError):
            fetcher.fetch(KEYWORD)
        self.assertEqual(len(fetcher.fetch(KEYWORD)), 3)  # 第 2 轮恢复正常

    def test_round_provider_injection(self) -> None:
        """可通过 round_provider 固定轮次。"""
        fetcher = MockFetcher(products_per_round=3, round_provider=lambda kw: 1)
        first = [p.product_id for p in fetcher.fetch(KEYWORD)]
        second = [p.product_id for p in fetcher.fetch(KEYWORD)]
        self.assertEqual(first, second)


class TestMonitor(unittest.TestCase):
    """Monitor 端到端逻辑测试。"""

    def setUp(self) -> None:
        self.config = make_config()
        self.storage = Storage(":memory:")
        self.fetcher = MockFetcher(products_per_round=5)
        self.recorder = RecordingNotifier()
        self.monitor = Monitor(self.config, self.fetcher, self.storage, [self.recorder])

    def tearDown(self) -> None:
        self.storage.close()

    # ------------------------------------------------------------------ #
    def test_first_round_notifies_cheap_new_products(self) -> None:
        """第一轮：低于阈值的新商品应触发通知并被标记 notified。"""
        count = self.monitor.run_once()

        self.assertGreater(count, 0)
        self.assertEqual(count, len(self.recorder.received))
        self.assertEqual(self.monitor.last_result.fetched, 5)
        self.assertEqual(self.monitor.last_result.new_products, 5)

        for product in self.recorder.received:
            self.assertLess(product.price, MAX_PRICE)
            self.assertTrue(self.storage.is_notified(KEYWORD, product.product_id))
        self.assertEqual(self.storage.count_notified(KEYWORD), count)

    def test_expensive_products_not_notified(self) -> None:
        """高于阈值的商品不应被通知，但仍应被记录为已见。"""
        self.monitor.run_once()
        notified_ids = {p.product_id for p in self.recorder.received}

        expensive = [
            p for p in MockFetcher(products_per_round=5).fetch(KEYWORD) if p.price >= MAX_PRICE
        ]
        self.assertTrue(expensive)
        for product in expensive:
            self.assertNotIn(product.product_id, notified_ids)
            self.assertFalse(self.storage.is_notified(KEYWORD, product.product_id))
            self.assertIsNotNone(self.storage.get_product(KEYWORD, product.product_id))

    def test_previous_round_products_are_not_new(self) -> None:
        """上一轮已出现、本轮仍在的商品不算「新」，不应重复通知。"""
        self.monitor.run_once()
        first_round_ids = {p.product_id for p in self.recorder.received}
        self.recorder.received.clear()

        self.monitor.run_once()
        # 第 2 轮只有 1 个真正的新商品（窗口右移一格）
        self.assertEqual(self.monitor.last_result.new_products, 1)
        for product in self.recorder.received:
            self.assertNotIn(product.product_id, first_round_ids)

    def test_dedup_by_notified_flag(self) -> None:
        """即使上一轮记录被清空，已提醒过的商品也不会再次通知（去重生效）。"""
        first_count = self.monitor.run_once()
        self.assertGreater(first_count, 0)
        self.recorder.received.clear()

        # 构造「同一批商品再次出现且被判定为新商品」的极端场景
        self.storage.clear_previous_round_ids(KEYWORD)
        replay_monitor = Monitor(
            self.config,
            MockFetcher(products_per_round=5, round_provider=lambda kw: 1),
            self.storage,
            [self.recorder],
        )
        second_count = replay_monitor.run_once()

        self.assertEqual(replay_monitor.last_result.new_products, 5)  # 都被视为「新出现」
        self.assertEqual(second_count, 0)                             # 但已提醒过，全部去重
        self.assertEqual(self.recorder.received, [])
        self.assertEqual(self.storage.count_notified(KEYWORD), first_count)

    def test_threshold_boundary_exclusive(self) -> None:
        """价格等于阈值不触发（严格小于）。"""

        class FixedFetcher(MockFetcher):
            """返回一个价格恰好等于阈值、一个略低于阈值的商品。"""

            def fetch(self, keyword: str) -> List[Product]:
                return [
                    Product("9001", "刚好等于阈值", MAX_PRICE, "https://x/9001", "刚刚", keyword),
                    Product("9002", "略低于阈值", MAX_PRICE - 0.01, "https://x/9002", "刚刚", keyword),
                ]

        monitor = Monitor(self.config, FixedFetcher(), self.storage, [self.recorder])
        count = monitor.run_once()
        self.assertEqual(count, 1)
        self.assertEqual(self.recorder.received[0].product_id, "9002")

    def test_fetch_failure_does_not_crash(self) -> None:
        """抓取失败应被捕获，本轮通知数为 0 并记录失败关键词。"""
        monitor = Monitor(
            self.config,
            MockFetcher(products_per_round=5, fail_rounds=[1]),
            self.storage,
            [self.recorder],
        )
        count = monitor.run_once()
        self.assertEqual(count, 0)
        self.assertEqual(monitor.last_result.failed_keywords, [KEYWORD])
        self.assertEqual(self.recorder.received, [])

    def test_notifier_exception_does_not_break_flow(self) -> None:
        """单个通知通道抛异常不应影响标记与其它通道。"""

        class BrokenNotifier(Notifier):
            name = "broken"

            def notify(self, products: List[Product]) -> None:
                raise RuntimeError("boom")

        monitor = Monitor(
            self.config, self.fetcher, self.storage, [BrokenNotifier(), self.recorder]
        )
        with self.assertLogs("xianyu_alert.notifier", level="WARNING"):
            count = monitor.run_once()
        self.assertGreater(count, 0)
        self.assertEqual(len(self.recorder.received), count)

    def test_multiple_keywords_isolated(self) -> None:
        """多关键词应各自独立计算新商品与去重。"""
        config = make_config(keywords=["Switch", "iPhone"])
        monitor = Monitor(config, MockFetcher(products_per_round=5), self.storage, [self.recorder])
        count = monitor.run_once()

        self.assertEqual(monitor.last_result.fetched, 10)
        self.assertGreater(count, 0)
        keywords = {p.keyword for p in self.recorder.received}
        self.assertEqual(keywords, {"Switch", "iPhone"})

    def test_console_notifier_end_to_end(self) -> None:
        """使用 ConsoleNotifier 跑一轮，stdout 应含四要素。"""
        monitor = Monitor(self.config, self.fetcher, self.storage, [ConsoleNotifier()])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            count = monitor.run_once()
        output = buffer.getvalue()

        self.assertGreater(count, 0)
        self.assertIn("商品名称:", output)
        self.assertIn("价格: ¥", output)
        self.assertIn("商品链接: https://www.goofish.com/item?id=", output)
        self.assertIn("发布时间:", output)

    def test_run_forever_with_max_rounds(self) -> None:
        """run_forever 应在 max_rounds 后退出且不 sleep 最后一轮。"""
        config = make_config(interval=1)
        monitor = Monitor(config, MockFetcher(products_per_round=5), self.storage, [self.recorder])
        total = monitor.run_forever(max_rounds=2)
        self.assertGreaterEqual(total, 1)
        self.assertEqual(self.storage.count_notified(KEYWORD), total)

    def test_summary(self) -> None:
        """summary 应返回累计已提醒数量。"""
        count = self.monitor.run_once()
        self.assertEqual(self.monitor.summary(), {"total_notified": count})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
