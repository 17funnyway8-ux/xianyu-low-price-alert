"""Storage（SQLite 去重与状态存储）单元测试。全部使用内存数据库。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_product(pid: str = "1001", price: float = 199.0, keyword: str = "Switch") -> Product:
    """构造测试商品。"""
    return Product(
        product_id=pid,
        title=f"{keyword} 测试商品 {pid}",
        price=price,
        url=f"https://www.goofish.com/item?id={pid}",
        publish_time="2024-05-01 10:00",
        keyword=keyword,
    )


class TestStorage(unittest.TestCase):
    """使用 :memory: 数据库验证存储行为。"""

    def setUp(self) -> None:
        self.storage = Storage(":memory:")
        self.ts = datetime(2024, 5, 1, 12, 0, 0)

    def tearDown(self) -> None:
        self.storage.close()

    # ------------------------------------------------------------------ #
    def test_is_notified_default_false(self) -> None:
        """未记录的商品应返回 False。"""
        self.assertFalse(self.storage.is_notified("Switch", "not-exist"))

    def test_mark_notified(self) -> None:
        """mark_notified 后 is_notified 应为 True。"""
        product = make_product("1001")
        self.storage.mark_notified(product, self.ts)
        self.assertTrue(self.storage.is_notified("Switch", "1001"))
        self.assertEqual(self.storage.count_notified(), 1)
        self.assertEqual(self.storage.count_notified("Switch"), 1)
        self.assertEqual(self.storage.count_notified("iPhone"), 0)

    def test_mark_notified_idempotent(self) -> None:
        """重复 mark_notified 不应产生重复行（唯一约束生效）。"""
        product = make_product("1001")
        self.storage.mark_notified(product, self.ts)
        self.storage.mark_notified(product, self.ts)
        self.assertEqual(self.storage.count_notified(), 1)

    def test_same_id_different_keyword_is_separate(self) -> None:
        """同一 product_id 在不同关键词下互不影响。"""
        self.storage.mark_notified(make_product("1001", keyword="Switch"), self.ts)
        self.assertTrue(self.storage.is_notified("Switch", "1001"))
        self.assertFalse(self.storage.is_notified("iPhone", "1001"))

    def test_save_seen_does_not_mark_notified(self) -> None:
        """save_seen 只记录出现，不应把 notified 置 1。"""
        product = make_product("2002")
        self.storage.save_seen(product, self.ts)
        self.assertFalse(self.storage.is_notified("Switch", "2002"))
        row = self.storage.get_product("Switch", "2002")
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], product.title)
        self.assertAlmostEqual(float(row["price"]), 199.0)
        self.assertEqual(row["first_seen"], "2024-05-01 12:00:00")
        self.assertEqual(row["last_seen"], "2024-05-01 12:00:00")

    def test_save_seen_updates_last_seen_keeps_first_seen(self) -> None:
        """再次 save_seen 应更新 last_seen 而保留 first_seen。"""
        product = make_product("2002")
        self.storage.save_seen(product, self.ts)
        later = datetime(2024, 5, 2, 8, 30, 0)
        product.price = 188.0
        self.storage.save_seen(product, later)

        row = self.storage.get_product("Switch", "2002")
        self.assertEqual(row["first_seen"], "2024-05-01 12:00:00")
        self.assertEqual(row["last_seen"], "2024-05-02 08:30:00")
        self.assertAlmostEqual(float(row["price"]), 188.0)

    def test_save_seen_many(self) -> None:
        """批量写入应返回写入数量。"""
        products = [make_product(str(3000 + i)) for i in range(4)]
        count = self.storage.save_seen_many(products, self.ts)
        self.assertEqual(count, 4)
        self.assertEqual(self.storage.count_notified(), 0)

    def test_save_seen_does_not_reset_notified(self) -> None:
        """已提醒的商品再 save_seen，notified 标志不应被清零。"""
        product = make_product("4004")
        self.storage.mark_notified(product, self.ts)
        self.storage.save_seen(product, self.ts)
        self.assertTrue(self.storage.is_notified("Switch", "4004"))

    # ------------------------------------------------------------------ #
    def test_previous_round_ids_empty_by_default(self) -> None:
        """初始状态应为空集合。"""
        self.assertEqual(self.storage.get_previous_round_ids("Switch"), set())

    def test_previous_round_ids_roundtrip(self) -> None:
        """写入后应能原样读回，且按关键词隔离。"""
        self.storage.set_previous_round_ids("Switch", {"1", "2", "3"})
        self.assertEqual(self.storage.get_previous_round_ids("Switch"), {"1", "2", "3"})
        self.assertEqual(self.storage.get_previous_round_ids("iPhone"), set())

        self.storage.set_previous_round_ids("Switch", {"3", "4"})
        self.assertEqual(self.storage.get_previous_round_ids("Switch"), {"3", "4"})

    def test_clear_previous_round_ids(self) -> None:
        """清空后应回到空集合。"""
        self.storage.set_previous_round_ids("Switch", {"1"})
        self.storage.clear_previous_round_ids("Switch")
        self.assertEqual(self.storage.get_previous_round_ids("Switch"), set())

    # ------------------------------------------------------------------ #
    # v1.8 通用 meta 读写（Cookie 过期提醒去抖状态）
    # ------------------------------------------------------------------ #
    def test_meta_value_none_by_default(self) -> None:
        """未写入的 key 返回 None。"""
        self.assertIsNone(self.storage.get_meta_value("cookie_alert_state:abc"))

    def test_meta_value_roundtrip(self) -> None:
        """写入后原样读回，并按 key 隔离。"""
        self.storage.set_meta_value("cookie_alert_state:abc", "expired")
        self.assertEqual(self.storage.get_meta_value("cookie_alert_state:abc"), "expired")
        self.assertIsNone(self.storage.get_meta_value("cookie_alert_state:def"))

    def test_meta_value_upsert(self) -> None:
        """重复写入同一 key 覆盖旧值（INSERT OR REPLACE 语义）。"""
        self.storage.set_meta_value("cookie_alert_state:abc", "expired")
        self.storage.set_meta_value("cookie_alert_state:abc", "ok")
        self.assertEqual(self.storage.get_meta_value("cookie_alert_state:abc"), "ok")

    def test_meta_value_delete(self) -> None:
        """delete 幂等：删除后返回 None，重复删除不抛。"""
        self.storage.set_meta_value("cookie_alert_state:abc", "expired")
        self.storage.delete_meta_value("cookie_alert_state:abc")
        self.assertIsNone(self.storage.get_meta_value("cookie_alert_state:abc"))
        self.storage.delete_meta_value("cookie_alert_state:abc")  # 幂等

    def test_meta_value_persists_across_reopen(self) -> None:
        """去抖状态跨重启保持（meta 表持久化，C4）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "meta.db")
            with Storage(db_path) as st:
                st.set_meta_value("cookie_alert_state:abc", "expired")
            with Storage(db_path) as st2:
                self.assertEqual(st2.get_meta_value("cookie_alert_state:abc"), "expired")

    def test_meta_constants_prefix(self) -> None:
        """常量前缀（共享知识 3）：单条 `cookie_alert_state:` 前缀存在。"""
        from xianyu_alert.storage import _META_COOKIE_ALERT_PREFIX

        self.assertEqual(_META_COOKIE_ALERT_PREFIX, "cookie_alert_state:")


    def test_list_notified(self) -> None:
        """list_notified 只返回已提醒的记录。"""
        self.storage.mark_notified(make_product("5001"), self.ts)
        self.storage.save_seen(make_product("5002"), self.ts)
        rows = self.storage.list_notified()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_id"], "5001")

    def test_close_is_idempotent(self) -> None:
        """close 可重复调用。"""
        self.storage.close()
        self.storage.close()

    def test_file_backed_persistence(self) -> None:
        """文件库应在重开后保留状态（验证跨重启去重）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "sub", "state.db")
            with Storage(db_path) as st:
                st.mark_notified(make_product("6001"), self.ts)
                st.set_previous_round_ids("Switch", {"6001"})
            with Storage(db_path) as st2:
                self.assertTrue(st2.is_notified("Switch", "6001"))
                self.assertEqual(st2.get_previous_round_ids("Switch"), {"6001"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
