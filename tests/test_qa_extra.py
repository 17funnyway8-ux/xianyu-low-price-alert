"""QA 补充边界用例（独立验证，覆盖工程师原测试可能遗漏的点）。

覆盖范围：
    1. 价格恰好等于阈值时不触发（严格小于的边界）；
    2. prev_ids 跨进程持久化（文件型 SQLite，Storage 关闭后重开仍能读回）；
    3. format_message 在发布时间缺失时不抛异常且其余三要素齐全；
    4. 多通道场景下单通道 notify 抛异常，其余通道仍收到且 mark_notified 正常执行；
    5. MockFetcher 命中 fail_rounds 抛 FetchError 时 monitor 跳过该关键词且不崩溃。

全部使用 unittest，可被 `python -m unittest discover -s tests` 直接发现。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from typing import List
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import Config, config_from_dict  # noqa: E402
from xianyu_alert.fetcher import FetchError, Fetcher, MockFetcher  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.notifier import Notifier, format_message  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402

KEYWORD = "Switch"


# ---------------------------------------------------------------------- #
# 测试替身
# ---------------------------------------------------------------------- #
class StubFetcher(Fetcher):
    """返回固定商品列表的抓取器，用于精确构造边界数据。"""

    name = "stub"

    def __init__(self, products: List[Product]) -> None:
        self.products: List[Product] = products
        self.calls: int = 0

    def fetch(self, keyword: str) -> List[Product]:
        self.calls += 1
        return list(self.products)


class RecordingNotifier(Notifier):
    """记录收到的商品，便于断言通知内容。"""

    name = "recording"

    def __init__(self) -> None:
        self.received: List[Product] = []
        self.calls: int = 0

    def notify(self, products: List[Product]) -> None:
        self.calls += 1
        self.received.extend(products)


class ExplodingNotifier(Notifier):
    """notify 永远抛异常的通道，用于验证 safe_notify 的隔离性。"""

    name = "exploding"

    def __init__(self) -> None:
        self.calls: int = 0

    def notify(self, products: List[Product]) -> None:
        self.calls += 1
        raise RuntimeError("模拟通道故障：网络不可达")


def make_config(max_price: float, keywords: List[str] = None, **fetcher_opts) -> Config:
    """构造内存库测试配置。"""
    keywords = keywords or [KEYWORD]
    fetcher = {"type": "mock"}
    fetcher.update(fetcher_opts)
    return config_from_dict(
        {
            "keywords": [{"keyword": k, "max_price": max_price} for k in keywords],
            "monitor": {"interval_seconds": 60},
            "fetcher": fetcher,
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        }
    )


def make_product(product_id: str, price: float, **kwargs) -> Product:
    """构造测试商品。"""
    params = {
        "product_id": product_id,
        "title": kwargs.get("title", f"测试商品 {product_id}"),
        "price": price,
        "url": kwargs.get("url", f"https://www.goofish.com/item?id={product_id}"),
        "publish_time": kwargs.get("publish_time", "2024-01-01 12:00"),
        "keyword": kwargs.get("keyword", KEYWORD),
    }
    return Product(**params)


# ---------------------------------------------------------------------- #
# 1. 阈值边界：严格小于
# ---------------------------------------------------------------------- #
class TestPriceThresholdBoundary(unittest.TestCase):
    """验证价格阈值是严格小于（`<`）而非小于等于。"""

    def setUp(self) -> None:
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)

    def _run(self, price: float, max_price: float) -> int:
        """用给定价格跑一轮，返回触发通知数。"""
        fetcher = StubFetcher([make_product("100001", price)])
        monitor = Monitor(
            config=make_config(max_price),
            fetcher=fetcher,
            storage=self.storage,
            notifiers=[RecordingNotifier()],
        )
        return monitor.run_once()

    def test_price_equal_to_threshold_does_not_trigger(self) -> None:
        """价格恰好等于阈值时不应触发（边界值本身不算低价）。"""
        self.assertEqual(self._run(price=1000.0, max_price=1000.0), 0)

    def test_price_just_below_threshold_triggers(self) -> None:
        """价格比阈值低 0.01 元应触发。"""
        self.assertEqual(self._run(price=999.99, max_price=1000.0), 1)

    def test_price_just_above_threshold_does_not_trigger(self) -> None:
        """价格比阈值高 0.01 元不应触发。"""
        self.assertEqual(self._run(price=1000.01, max_price=1000.0), 0)

    def test_zero_price_triggers(self) -> None:
        """0 元商品（免费送）属于合法低价，应触发。"""
        self.assertEqual(self._run(price=0.0, max_price=1000.0), 1)

    def test_equal_threshold_not_marked_notified(self) -> None:
        """等于阈值的商品不应被写入已提醒表，后续降价仍有机会提醒。"""
        self._run(price=1000.0, max_price=1000.0)
        self.assertFalse(self.storage.is_notified(KEYWORD, "100001"))


# ---------------------------------------------------------------------- #
# 2. prev_ids 跨进程（文件型 SQLite）
# ---------------------------------------------------------------------- #
class TestPrevIdsCrossProcess(unittest.TestCase):
    """验证「上一轮 ID 集合」真正落盘，支持跨进程/跨重启去重。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = os.path.join(self.tmpdir.name, "sub", "qa_state.db")

    def test_prev_ids_survive_close_and_reopen(self) -> None:
        """Storage 关闭后新建实例，get_previous_round_ids 仍能读回上一轮集合。"""
        expected = {"111111", "222222", "333333"}

        first = Storage(self.db_path)
        first.set_previous_round_ids(KEYWORD, expected)
        first.close()

        second = Storage(self.db_path)
        self.addCleanup(second.close)
        self.assertEqual(second.get_previous_round_ids(KEYWORD), expected)

    def test_notified_flag_survives_reopen(self) -> None:
        """notified 去重标志同样跨重启有效。"""
        first = Storage(self.db_path)
        first.mark_notified(make_product("999999", 88.0))
        first.close()

        second = Storage(self.db_path)
        self.addCleanup(second.close)
        self.assertTrue(second.is_notified(KEYWORD, "999999"))

    def test_prev_ids_isolated_per_keyword_after_reopen(self) -> None:
        """不同关键词的上一轮集合互相隔离，重开后仍然隔离。"""
        first = Storage(self.db_path)
        first.set_previous_round_ids("Switch", {"1"})
        first.set_previous_round_ids("iPhone", {"2"})
        first.close()

        second = Storage(self.db_path)
        self.addCleanup(second.close)
        self.assertEqual(second.get_previous_round_ids("Switch"), {"1"})
        self.assertEqual(second.get_previous_round_ids("iPhone"), {"2"})

    def test_monitor_no_duplicate_alert_across_restart(self) -> None:
        """端到端：两个独立 Storage 实例（模拟两次进程启动）不应重复提醒同一商品。"""
        products = [make_product("100001", 88.0)]
        config = make_config(1000.0)

        storage1 = Storage(self.db_path)
        recorder1 = RecordingNotifier()
        first_round = Monitor(config, StubFetcher(products), storage1, [recorder1]).run_once()
        storage1.close()

        storage2 = Storage(self.db_path)
        self.addCleanup(storage2.close)
        recorder2 = RecordingNotifier()
        second_round = Monitor(config, StubFetcher(products), storage2, [recorder2]).run_once()

        self.assertEqual(first_round, 1)
        self.assertEqual(second_round, 0, "重启后同一商品不应再次提醒")
        self.assertEqual(len(recorder2.received), 0)


# ---------------------------------------------------------------------- #
# 3. format_message 缺失发布时间
# ---------------------------------------------------------------------- #
class TestFormatMessageMissingPublishTime(unittest.TestCase):
    """验证发布时间缺失时通知文案依然健壮。"""

    def test_empty_publish_time_does_not_raise(self) -> None:
        """publish_time 为空串时不抛异常，且四要素字段名齐全。"""
        product = make_product("100001", 88.0, publish_time="")
        text = format_message(product)

        self.assertIn("商品名称: 测试商品 100001", text)
        self.assertIn("价格: ¥88.00", text)
        self.assertIn("商品链接: https://www.goofish.com/item?id=100001", text)
        self.assertIn("发布时间:", text)

    def test_empty_publish_time_falls_back_to_unknown(self) -> None:
        """发布时间缺失应展示占位文案「未知」，而不是空白。"""
        product = make_product("100001", 88.0, publish_time="")
        self.assertIn("发布时间: 未知", format_message(product))

    def test_whitespace_publish_time_falls_back(self) -> None:
        """纯空白发布时间会被 Product 归一化为空串，同样回退到「未知」。"""
        product = make_product("100001", 88.0, publish_time="   ")
        self.assertIn("发布时间: 未知", format_message(product))

    def test_missing_url_falls_back(self) -> None:
        """链接缺失时展示「无」，不应出现空字段。"""
        product = make_product("100001", 88.0, url="")
        self.assertIn("商品链接: 无", format_message(product))

    def test_all_four_elements_present_when_complete(self) -> None:
        """信息完整时四要素都应出现在文案中。"""
        product = make_product("100001", 88.0, publish_time="2024-05-01 09:30")
        text = format_message(product)
        for expected in (
            "商品名称: 测试商品 100001",
            "价格: ¥88.00",
            "商品链接: https://www.goofish.com/item?id=100001",
            "发布时间: 2024-05-01 09:30",
        ):
            self.assertIn(expected, text)


# ---------------------------------------------------------------------- #
# 4. 多通道容错
# ---------------------------------------------------------------------- #
class TestMultiChannelFaultTolerance(unittest.TestCase):
    """单通道故障不得影响其它通道与去重落库。"""

    def setUp(self) -> None:
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.product = make_product("100001", 88.0)

    def test_failing_channel_does_not_block_others(self) -> None:
        """中间通道抛异常时，前后通道仍应收到通知。"""
        before, boom, after = RecordingNotifier(), ExplodingNotifier(), RecordingNotifier()
        monitor = Monitor(
            config=make_config(1000.0),
            fetcher=StubFetcher([self.product]),
            storage=self.storage,
            notifiers=[before, boom, after],
        )

        notified = monitor.run_once()

        self.assertEqual(notified, 1)
        self.assertEqual(boom.calls, 1, "故障通道应被调用过")
        self.assertEqual(len(before.received), 1)
        self.assertEqual(len(after.received), 1, "故障通道之后的通道不应被跳过")

    def test_mark_notified_still_executes_when_channel_fails(self) -> None:
        """通道失败不影响 mark_notified，避免下一轮重复轰炸。"""
        monitor = Monitor(
            config=make_config(1000.0),
            fetcher=StubFetcher([self.product]),
            storage=self.storage,
            notifiers=[ExplodingNotifier()],
        )

        monitor.run_once()

        self.assertTrue(self.storage.is_notified(KEYWORD, "100001"))

    def test_all_channels_failing_does_not_crash(self) -> None:
        """所有通道都失败时主流程仍应正常返回。"""
        monitor = Monitor(
            config=make_config(1000.0),
            fetcher=StubFetcher([self.product]),
            storage=self.storage,
            notifiers=[ExplodingNotifier(), ExplodingNotifier()],
        )

        self.assertEqual(monitor.run_once(), 1)

    def test_safe_notify_swallows_exception_and_returns_false(self) -> None:
        """safe_notify 应吞掉异常并返回 False，而不是向上抛出。"""
        boom = ExplodingNotifier()
        self.assertFalse(boom.safe_notify([self.product]))

    def test_monitor_uses_safe_notify_not_notify(self) -> None:
        """monitor 必须走 safe_notify，否则单通道异常会中断整轮。"""
        recorder = RecordingNotifier()
        with mock.patch.object(
            recorder, "safe_notify", wraps=recorder.safe_notify
        ) as spy:
            monitor = Monitor(
                config=make_config(1000.0),
                fetcher=StubFetcher([self.product]),
                storage=self.storage,
                notifiers=[recorder],
            )
            monitor.run_once()

        spy.assert_called_once()
        self.assertEqual(len(spy.call_args[0][0]), 1)


# ---------------------------------------------------------------------- #
# 5. 抓取失败容错
# ---------------------------------------------------------------------- #
class TestFetchFailureTolerance(unittest.TestCase):
    """抓取失败时 monitor 应跳过该关键词且不崩溃。"""

    def setUp(self) -> None:
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)

    def test_mock_fetcher_raises_on_fail_round(self) -> None:
        """fail_rounds 命中时 MockFetcher 应抛出 FetchError。"""
        fetcher = MockFetcher(products_per_round=5, fail_rounds=[1])
        with self.assertRaises(FetchError):
            fetcher.fetch(KEYWORD)

    def test_monitor_returns_zero_and_records_failed_keyword(self) -> None:
        """抓取失败时本轮触发数为 0，并记录失败关键词。"""
        config = make_config(1000.0, mock_fail_rounds=[1])
        monitor = Monitor(
            config=config,
            fetcher=MockFetcher(products_per_round=5, fail_rounds=[1]),
            storage=self.storage,
            notifiers=[RecordingNotifier()],
        )

        self.assertEqual(monitor.run_once(), 0)
        self.assertEqual(monitor.last_result.failed_keywords, [KEYWORD])
        self.assertEqual(monitor.last_result.fetched, 0)

    def test_failed_keyword_does_not_block_other_keywords(self) -> None:
        """一个关键词抓取失败不应影响其它关键词的正常提醒。"""

        class SelectiveFetcher(Fetcher):
            """指定关键词失败，其余正常返回。"""

            name = "selective"

            def fetch(self, keyword: str) -> List[Product]:
                if keyword == "Switch":
                    raise FetchError("模拟抓取失败")
                return [make_product("200001", 88.0, keyword=keyword)]

        monitor = Monitor(
            config=make_config(1000.0, keywords=["Switch", "iPhone"]),
            fetcher=SelectiveFetcher(),
            storage=self.storage,
            notifiers=[RecordingNotifier()],
        )

        self.assertEqual(monitor.run_once(), 1)
        self.assertEqual(monitor.last_result.failed_keywords, ["Switch"])

    def test_unexpected_exception_is_also_tolerated(self) -> None:
        """非 FetchError 的未预期异常同样不应让整轮崩溃。"""

        class BrokenFetcher(Fetcher):
            """抛出非 FetchError 异常的抓取器。"""

            name = "broken"

            def fetch(self, keyword: str) -> List[Product]:
                raise ValueError("未预期的解析错误")

        monitor = Monitor(
            config=make_config(1000.0),
            fetcher=BrokenFetcher(),
            storage=self.storage,
            notifiers=[RecordingNotifier()],
        )

        self.assertEqual(monitor.run_once(), 0)
        self.assertEqual(monitor.last_result.failed_keywords, [KEYWORD])

    def test_failed_round_does_not_wipe_prev_ids(self) -> None:
        """抓取失败的一轮不应清空 prev_ids，否则下一轮会误判为全部新商品。"""
        self.storage.set_previous_round_ids(KEYWORD, {"100001"})

        class AlwaysFailFetcher(Fetcher):
            """总是失败的抓取器。"""

            name = "always-fail"

            def fetch(self, keyword: str) -> List[Product]:
                raise FetchError("模拟抓取失败")

        monitor = Monitor(
            config=make_config(1000.0),
            fetcher=AlwaysFailFetcher(),
            storage=self.storage,
            notifiers=[RecordingNotifier()],
        )
        monitor.run_once()

        self.assertEqual(self.storage.get_previous_round_ids(KEYWORD), {"100001"})

    def test_run_forever_survives_failing_round(self) -> None:
        """run_forever 在单轮异常后应继续跑完剩余轮次。"""
        config = make_config(1000.0)
        config.monitor.interval_seconds = 0
        monitor = Monitor(
            config=config,
            fetcher=MockFetcher(products_per_round=5, fail_rounds=[1]),
            storage=self.storage,
            notifiers=[RecordingNotifier()],
        )

        with mock.patch("xianyu_alert.monitor.time.sleep"):
            total = monitor.run_forever(max_rounds=2)

        self.assertGreater(total, 0, "第 2 轮应正常抓取并产生通知")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
