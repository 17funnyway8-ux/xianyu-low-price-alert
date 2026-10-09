"""数据模型扩展字段测试（v1.10.3）：卖家 / 地区 / 原价。

M03 的问题是「字段偏少（无卖家、地区、库存、原价），扩展分析能力受限」。
本文件覆盖字段新增的**三条纪律**：
    1. 新字段一律追加在**末尾** —— 既有按位置传参的调用点不受影响；
    2. 归一化集中在 __post_init__（脏数据不炸，回退默认）；
    3. 一路打穿：模型 -> mtop 解析 -> 落库 -> 取出（含老库迁移）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.fetcher.mtop_api import parse_mtop_item  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_mtop_item() -> dict:
    """构造一条最小可解析的 mtop 搜索结果条目（含卖家/地区/原价）。"""
    return {
        "data": {
            "item": {
                "main": {
                    "exContent": {
                        "itemId": "810000000001",
                        "title": "Switch OLED 95新",
                        "price": "1299",
                        "picUrl": "//img.alicdn.com/a.jpg",
                        "area": "广东 深圳",
                        "userNickName": "卖家A",
                        "originalPrice": "1699",
                    },
                    "clickParam": {"args": {"price": "1299"}},
                }
            }
        }
    }


class TestProductFields(unittest.TestCase):
    """新字段本身的行为。"""

    def test_defaults_are_empty(self) -> None:
        product = Product("1", "标题", 10.0, "u")
        self.assertEqual(product.seller, "")
        self.assertEqual(product.location, "")
        self.assertIsNone(product.original_price)

    def test_positional_construction_still_works(self) -> None:
        """新字段追加在末尾 -> 老的位置参数调用不受影响。"""
        product = Product("1", "标题", 10.0, "u", "3分钟前", "Switch", "https://img/x.jpg")
        self.assertEqual(product.publish_time, "3分钟前")
        self.assertEqual(product.keyword, "Switch")
        self.assertEqual(product.image_url, "https://img/x.jpg")

    def test_normalisation_strips_and_validates(self) -> None:
        product = Product("1", "标题", 10.0, "u", seller="  卖家A  ", location=" 广东 ", original_price=199.9)
        self.assertEqual(product.seller, "卖家A")
        self.assertEqual(product.location, "广东")
        self.assertEqual(product.original_price, 199.9)

    def test_dirty_original_price_falls_back_to_none(self) -> None:
        for bad in ("不是数字", -5, 0):
            self.assertIsNone(Product("1", "标题", 10.0, "u", original_price=bad).original_price, bad)

    def test_discount_text(self) -> None:
        product = Product("1", "标题", 129.0, "u", original_price=199.0)
        self.assertEqual(product.discount_text, "原价 ¥199.00 → 现价 ¥129.00")

    def test_discount_text_empty_without_original(self) -> None:
        self.assertEqual(Product("1", "标题", 10.0, "u").discount_text, "")
        self.assertEqual(Product("1", "标题", 10.0, "u", original_price=5.0).discount_text, "")

    def test_from_dict_accepts_new_fields(self) -> None:
        product = Product.from_dict(
            {
                "product_id": "1",
                "title": "标题",
                "price": 10.0,
                "url": "u",
                "seller": "卖家B",
                "location": "北京",
                "original_price": 20.0,
            }
        )
        self.assertEqual(product.seller, "卖家B")
        self.assertEqual(product.location, "北京")
        self.assertEqual(product.original_price, 20.0)


class TestMtopParsing(unittest.TestCase):
    """解析层要真的把这些字段取出来（而不是只加了个字段没人填）。"""

    def test_extracts_seller_location_original_price(self) -> None:
        product = parse_mtop_item(make_mtop_item(), "Switch")
        self.assertIsNotNone(product)
        self.assertEqual(product.seller, "卖家A")
        self.assertEqual(product.location, "广东 深圳")
        self.assertEqual(product.original_price, 1699.0)

    def test_missing_fields_are_tolerated(self) -> None:
        item = make_mtop_item()
        ex = item["data"]["item"]["main"]["exContent"]
        for key in ("area", "userNickName", "originalPrice"):
            ex.pop(key, None)
        product = parse_mtop_item(item, "Switch")
        self.assertIsNotNone(product)
        self.assertEqual(product.seller, "")
        self.assertEqual(product.location, "")
        self.assertIsNone(product.original_price)


class TestStorageRoundTrip(unittest.TestCase):
    """落库与取出（含老库迁移）。"""

    def test_round_trip(self) -> None:
        storage = Storage(":memory:")
        try:
            product = Product(
                "123456789", "测试商品", 129.0, "u",
                keyword="Switch", seller="卖家A", location="广东 深圳", original_price=199.0,
            )
            storage.save_seen(product)
            storage.mark_notified(product)
            record = storage.list_notified()[0]
            self.assertEqual(record.seller, "卖家A")
            self.assertEqual(record.location, "广东 深圳")
            self.assertEqual(record.original_price, 199.0)
            self.assertEqual(record.original_price_text, "¥199.00")
        finally:
            storage.close()

    def test_migration_adds_columns_to_old_db(self) -> None:
        """模拟 v3.6 老库：打开即幂等补列，老数据仍可读。"""
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE product ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT NOT NULL, product_id TEXT NOT NULL,"
            "title TEXT NOT NULL DEFAULT '', price REAL NOT NULL DEFAULT 0, url TEXT NOT NULL DEFAULT '',"
            "publish_time TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL DEFAULT '',"
            "last_seen TEXT NOT NULL DEFAULT '', notified INTEGER NOT NULL DEFAULT 0,"
            "UNIQUE (keyword, product_id))"
        )
        conn.execute("INSERT INTO product (keyword, product_id, title, price) VALUES ('Switch','111','老数据',10)")
        conn.commit()
        conn.close()

        storage = Storage(db)
        try:
            columns = {row[1] for row in storage.conn.execute("PRAGMA table_info(product)")}
            for name in ("image_url", "sold_out", "seller", "location", "original_price"):
                self.assertIn(name, columns, f"迁移应补出列 {name}")
        finally:
            storage.close()


if __name__ == "__main__":
    unittest.main()
