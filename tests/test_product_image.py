"""商品主图（image_url）特性单元测试（v1.8.3）。

覆盖四层：
    1. normalize_image_url：协议相对 / 明文 http / 脏数据的归一化规则；
    2. Product：字段默认值、构造时归一化、from_dict / to_dict 往返；
    3. Storage：新列建表、落库与读取、空图不覆盖已有图；
    4. 旧库迁移：v1.8.2 及以前的库没有 image_url 列，打开时应自动 ALTER 补齐。

全部使用内存库或临时文件库，**不访问网络**。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.models import Product, normalize_image_url  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_product(pid: str = "1001", image_url: str = "", keyword: str = "Switch") -> Product:
    """构造带主图的测试商品。"""
    return Product(
        product_id=pid,
        title=f"{keyword} 测试商品 {pid}",
        price=199.0,
        url=f"https://www.goofish.com/item?id={pid}",
        publish_time="2024-05-01 10:00",
        keyword=keyword,
        image_url=image_url,
    )


class TestNormalizeImageUrl(unittest.TestCase):
    """主图 URL 归一化规则。"""

    def test_protocol_relative_upgraded_to_https(self) -> None:
        """协议相对地址（站点最常见的形态）补全为 https。"""
        self.assertEqual(
            normalize_image_url("//img.alicdn.com/a.jpg"),
            "https://img.alicdn.com/a.jpg",
        )

    def test_plain_http_upgraded_to_https(self) -> None:
        """明文 http 升级为 https，避免 HTTPS 页面混合内容拦截。"""
        self.assertEqual(
            normalize_image_url("http://img.alicdn.com/a.jpg"),
            "https://img.alicdn.com/a.jpg",
        )

    def test_https_kept_as_is(self) -> None:
        """已是 https 的原样返回。"""
        self.assertEqual(
            normalize_image_url("https://img.alicdn.com/a.jpg"),
            "https://img.alicdn.com/a.jpg",
        )

    def test_whitespace_trimmed(self) -> None:
        """两侧空白被裁剪。"""
        self.assertEqual(normalize_image_url("  //img.alicdn.com/a.jpg  "), "https://img.alicdn.com/a.jpg")

    def test_empty_values_return_blank(self) -> None:
        """None / 空串 / 纯空白都归一化为空串（前端按「无图」处理）。"""
        for value in (None, "", "   "):
            self.assertEqual(normalize_image_url(value), "")

    def test_non_http_values_return_blank(self) -> None:
        """非 http(s) 的脏数据（data: / ftp: / 数字）一律丢弃。"""
        for value in ("data:image/png;base64,AAAA", "ftp://x/a.jpg", 12345):
            self.assertEqual(normalize_image_url(value), "")


class TestProductImageField(unittest.TestCase):
    """Product 模型的 image_url 字段。"""

    def test_default_is_blank(self) -> None:
        """未提供主图时为空串（兼容旧调用方）。"""
        product = Product(product_id="1", title="t", price=1.0, url="https://x")
        self.assertEqual(product.image_url, "")

    def test_normalized_on_construction(self) -> None:
        """构造时自动归一化。"""
        self.assertEqual(
            make_product(image_url="//img.alicdn.com/a.jpg").image_url,
            "https://img.alicdn.com/a.jpg",
        )

    def test_from_dict_and_to_dict_roundtrip(self) -> None:
        """from_dict / to_dict 往返保留主图。"""
        product = Product.from_dict(
            {"product_id": "1", "title": "t", "price": 1.0, "url": "https://x",
             "image_url": "//img.alicdn.com/a.jpg"},
            keyword="Switch",
        )
        self.assertEqual(product.image_url, "https://img.alicdn.com/a.jpg")
        self.assertEqual(product.to_dict()["image_url"], "https://img.alicdn.com/a.jpg")

    def test_from_dict_without_image(self) -> None:
        """旧格式字典（无 image_url 键）仍可构造。"""
        product = Product.from_dict({"product_id": "1", "title": "t", "price": 1.0, "url": "https://x"})
        self.assertEqual(product.image_url, "")


class TestStorageImageColumn(unittest.TestCase):
    """存储层的主图落库与读取。"""

    def setUp(self) -> None:
        self.storage = Storage(":memory:")

    def tearDown(self) -> None:
        self.storage.close()

    def test_column_exists(self) -> None:
        """新库 product 表含 image_url 列。"""
        cols = {row["name"] for row in self.storage.conn.execute("PRAGMA table_info(product)").fetchall()}
        self.assertIn("image_url", cols)

    def test_save_seen_persists_image(self) -> None:
        """save_seen 落库后能读回归一化后的主图。"""
        self.storage.save_seen(make_product(image_url="//img.alicdn.com/a.jpg"))
        row = self.storage.get_product("Switch", "1001")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["image_url"], "https://img.alicdn.com/a.jpg")

    def test_mark_notified_exposes_image_in_list(self) -> None:
        """mark_notified 后 list_notified 返回的字典含 image_url（前端直接渲染）。"""
        self.storage.mark_notified(make_product(image_url="//img.alicdn.com/b.jpg"))
        rows = self.storage.list_notified(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["image_url"], "https://img.alicdn.com/b.jpg")

    def test_empty_image_does_not_overwrite_existing(self) -> None:
        """后续轮次若未解析到主图，不得把已存的主图清空。"""
        self.storage.save_seen(make_product(image_url="//img.alicdn.com/a.jpg"))
        self.storage.save_seen(make_product(image_url=""))
        row = self.storage.get_product("Switch", "1001")
        assert row is not None
        self.assertEqual(row["image_url"], "https://img.alicdn.com/a.jpg")

    def test_legacy_db_without_column_is_migrated(self) -> None:
        """v1.8.2 及以前的库没有 image_url 列，打开时自动补齐且旧记录为空串。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "legacy.db")
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE product (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword       TEXT    NOT NULL,
                    product_id    TEXT    NOT NULL,
                    title         TEXT    NOT NULL DEFAULT '',
                    price         REAL    NOT NULL DEFAULT 0,
                    url           TEXT    NOT NULL DEFAULT '',
                    publish_time  TEXT    NOT NULL DEFAULT '',
                    first_seen    TEXT    NOT NULL DEFAULT '',
                    last_seen     TEXT    NOT NULL DEFAULT '',
                    notified      INTEGER NOT NULL DEFAULT 0,
                    sold_out      INTEGER NOT NULL DEFAULT 0,
                    sold_at       TEXT    NOT NULL DEFAULT '',
                    sold_reason   TEXT    NOT NULL DEFAULT '',
                    UNIQUE (keyword, product_id)
                );
                """
            )
            conn.execute(
                "INSERT INTO product (keyword, product_id, title, price, url, first_seen, last_seen, notified)"
                " VALUES ('Switch', 'old1', '旧版本记录', 100.0, 'https://x', '', '', 1)"
            )
            conn.commit()
            conn.close()

            storage = Storage(db_path)
            try:
                cols = {row["name"] for row in storage.conn.execute("PRAGMA table_info(product)").fetchall()}
                self.assertIn("image_url", cols)
                rows = storage.list_notified(limit=10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["image_url"], "")
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
