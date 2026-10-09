"""存储层类型化记录 + 配置版本迁移测试（v1.10.1）。

对应两个模块的改造（M09 存储层 8.2、M10 配置系统 8.2）：

    M09  行记录从裸 sqlite3.Row 变为**有类型、有默认值、有派生属性**的记录对象，
         同时保留字典式访问（既有调用点零改动）；并把 Web 层里那条裸 SQL 收敛回存储层。
    M10  配置结构带版本号，旧版配置可识别并迁移/提示（此前只能靠猜字段缺失的原因）。
"""

from __future__ import annotations

import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import CONFIG_VERSION, config_from_dict, migrate_config  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.records import BlacklistEntry, NotifiedRecord, SoldOutRecord  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_product(pid: str = "123456789", title: str = "测试商品", price: float = 12.5) -> Product:
    """构造测试商品。"""
    return Product(
        product_id=pid,
        title=title,
        price=price,
        url="https://www.goofish.com/item?id=" + pid,
        keyword="Switch",
        image_url="https://img.example/x.jpg",
    )


class TestRecordObject(unittest.TestCase):
    """记录对象本身的语义。"""

    def test_dict_style_access_kept(self) -> None:
        """既有调用点用的是 row["title"]，必须继续可用。"""
        rec = NotifiedRecord.from_row({"keyword": "k", "title": "T", "price": 1.5})
        self.assertEqual(rec["title"], "T")
        self.assertEqual(rec["keyword"], "k")

    def test_attribute_access_is_typed(self) -> None:
        rec = NotifiedRecord.from_row({"title": "T", "price": 3.0})
        self.assertEqual(rec.title, "T")
        self.assertEqual(rec.price, 3.0)

    def test_unknown_key_raises_keyerror(self) -> None:
        rec = NotifiedRecord()
        with self.assertRaises(KeyError):
            _ = rec["nope"]

    def test_get_returns_default(self) -> None:
        self.assertEqual(NotifiedRecord().get("nope", "d"), "d")

    def test_from_row_tolerates_partial_columns(self) -> None:
        """只 SELECT 了一部分列时，其余回落默认值而不是崩。"""
        rec = NotifiedRecord.from_row({"keyword": "only-this"})
        self.assertEqual(rec.keyword, "only-this")
        self.assertEqual(rec.title, "")

    def test_from_row_none(self) -> None:
        self.assertEqual(NotifiedRecord.from_row(None).title, "")

    def test_derived_properties(self) -> None:
        rec = NotifiedRecord(price=12.5, sold_out=1, title="")
        self.assertEqual(rec.price_text, "¥12.50")
        self.assertTrue(rec.is_sold_out)
        self.assertEqual(rec.display_title, "（无标题）")

    def test_to_dict(self) -> None:
        payload = NotifiedRecord(keyword="k").to_dict()
        self.assertEqual(payload["keyword"], "k")
        self.assertIn("image_url", payload)

    def test_keys_matches_dataclass_fields(self) -> None:
        self.assertIn("sold_reason", NotifiedRecord().keys())

    def test_records_are_immutable(self) -> None:
        """记录是快照：改字段应报错，避免把 DB 行当可变对象传递。"""
        rec = NotifiedRecord(title="T")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            rec.title = "other"  # type: ignore[misc]


class TestStorageReturnsTypedRecords(unittest.TestCase):
    """Storage 的 list_* 返回类型化记录，且字段值正确。"""

    def setUp(self) -> None:
        self.storage = Storage(":memory:")

    def tearDown(self) -> None:
        self.storage.close()

    def test_list_notified_returns_record_with_values(self) -> None:
        product = make_product()
        self.storage.save_seen(product)
        self.storage.mark_notified(product)
        rows = self.storage.list_notified()
        self.assertIsInstance(rows[0], NotifiedRecord)
        self.assertEqual(rows[0].title, "测试商品")
        self.assertEqual(rows[0].price, 12.5)
        self.assertEqual(rows[0]["image_url"], "https://img.example/x.jpg")

    def test_list_sold_out_returns_typed_record(self) -> None:
        product = make_product()
        self.storage.save_seen(product)
        self.storage.mark_notified(product)
        self.storage.mark_sold_out("Switch", product.product_id, reason="详情接口判定")
        rows = self.storage.list_sold_out()
        self.assertTrue(rows)
        self.assertIsInstance(rows[0], SoldOutRecord)
        self.assertTrue(rows[0].is_sold_out)
        self.assertEqual(rows[0].sold_reason, "详情接口判定")

    def test_list_blacklist_returns_typed_entry(self) -> None:
        self.storage.add_blacklist("123456789", keyword="Switch", reason="噪音")
        rows = self.storage.list_blacklist()
        self.assertIsInstance(rows[0], BlacklistEntry)
        self.assertEqual(rows[0].reason, "噪音")
        self.assertEqual(rows[0]["keyword"], "Switch")

    def test_find_keyword_by_product_id(self) -> None:
        """Web 层曾经的裸 SQL，现在由存储层提供具名方法。"""
        product = make_product()
        self.storage.save_seen(product)
        self.assertEqual(self.storage.find_keyword_by_product_id(product.product_id), "Switch")
        self.assertEqual(self.storage.find_keyword_by_product_id("nope"), "")
        self.assertEqual(self.storage.find_keyword_by_product_id(""), "")


class TestConfigVersion(unittest.TestCase):
    """M10：配置版本号与迁移。"""

    def _config(self, extra: dict | None = None) -> dict:
        data = {
            "keywords": [{"keyword": "Switch", "max_price": 1000}],
            "monitor": {"interval_seconds": 60},
        }
        data.update(extra or {})
        return data

    def test_default_version_when_absent(self) -> None:
        data = self._config()
        notes = migrate_config(data)
        self.assertEqual(data["config_version"], CONFIG_VERSION)
        self.assertTrue(any("迁移" in n for n in notes), "旧配置（无版本号）应给出迁移说明")

    def test_current_version_no_migration(self) -> None:
        data = self._config({"config_version": CONFIG_VERSION})
        notes = migrate_config(data)
        self.assertEqual(notes, [])

    def test_newer_version_warns_but_keeps_usable(self) -> None:
        """更新版本写的配置：警告但不拒绝启动（降级可用好过直接不跑）。"""
        data = self._config({"config_version": CONFIG_VERSION + 5})
        notes = migrate_config(data)
        self.assertTrue(any("更新版本" in n for n in notes))
        self.assertEqual(data["config_version"], CONFIG_VERSION)

    def test_dirty_version_value(self) -> None:
        data = self._config({"config_version": "不是数字"})
        notes = migrate_config(data)
        self.assertTrue(notes)
        self.assertEqual(data["config_version"], CONFIG_VERSION)

    def test_config_object_carries_version(self) -> None:
        config = config_from_dict(self._config())
        self.assertEqual(config.config_version, CONFIG_VERSION)

    def test_explicit_version_preserved(self) -> None:
        config = config_from_dict(self._config({"config_version": CONFIG_VERSION}))
        self.assertEqual(config.config_version, CONFIG_VERSION)


if __name__ == "__main__":
    unittest.main()
