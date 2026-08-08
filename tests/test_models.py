"""Product 数据模型单元测试。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.models import ModelError, Product  # noqa: E402


class TestProduct(unittest.TestCase):
    """Product 构造、校验与辅助方法测试。"""

    def test_basic_construction(self) -> None:
        """基本构造字段应原样保留。"""
        product = Product(
            product_id="123456789",
            title="Switch 游戏机 九成新",
            price=899.5,
            url="https://www.goofish.com/item?id=123456789",
            publish_time="2024-05-01 10:00",
            keyword="Switch",
        )
        self.assertEqual(product.product_id, "123456789")
        self.assertEqual(product.title, "Switch 游戏机 九成新")
        self.assertAlmostEqual(product.price, 899.5)
        self.assertEqual(product.keyword, "Switch")
        self.assertEqual(product.price_text, "¥899.50")

    def test_price_coerced_to_float(self) -> None:
        """字符串价格应被转换为 float。"""
        product = Product(product_id="1", title="t", price="199", url="u")
        self.assertIsInstance(product.price, float)
        self.assertAlmostEqual(product.price, 199.0)

    def test_defaults(self) -> None:
        """publish_time / keyword 默认应为空串。"""
        product = Product(product_id="1", title="t", price=1.0, url="u")
        self.assertEqual(product.publish_time, "")
        self.assertEqual(product.keyword, "")

    def test_invalid_fields(self) -> None:
        """非法字段应抛出 ModelError。"""
        with self.assertRaises(ModelError):
            Product(product_id="", title="t", price=1.0, url="u")
        with self.assertRaises(ModelError):
            Product(product_id="1", title="   ", price=1.0, url="u")
        with self.assertRaises(ModelError):
            Product(product_id="1", title="t", price=-5, url="u")
        with self.assertRaises(ModelError):
            Product(product_id="1", title="t", price="abc", url="u")

    def test_from_dict(self) -> None:
        """from_dict 应支持补齐 keyword 并容忍缺省可选字段。"""
        product = Product.from_dict(
            {"product_id": "999", "title": "iPhone 12", "price": 1200},
            keyword="iPhone",
        )
        self.assertEqual(product.keyword, "iPhone")
        self.assertEqual(product.url, "")
        self.assertEqual(product.publish_time, "")

    def test_from_dict_missing_field(self) -> None:
        """缺少必填字段应抛出 ModelError。"""
        with self.assertRaises(ModelError):
            Product.from_dict({"product_id": "1", "title": "t"})
        with self.assertRaises(ModelError):
            Product.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_to_dict_roundtrip(self) -> None:
        """to_dict -> from_dict 应还原等价对象。"""
        original = Product("111", "标题", 88.0, "https://x", "刚刚", "Switch")
        restored = Product.from_dict(original.to_dict())
        self.assertEqual(original, restored)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
