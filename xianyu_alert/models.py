"""数据模型定义。

目前只有一个核心实体：Product（闲鱼商品）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict


class ModelError(ValueError):
    """数据模型构造/校验失败时抛出。"""


@dataclass
class Product:
    """闲鱼商品。

    Attributes:
        product_id: 商品在闲鱼站内的唯一 ID（从商品链接中提取的数字串）。
        title: 商品标题。
        price: 商品价格（单位：元）。
        url: 商品详情页链接。
        publish_time: 发布时间（原样保留站点文案，解析不到时为空串）。
        keyword: 命中的搜索关键词（用于区分不同关键词下的同一商品）。
    """

    product_id: str
    title: str
    price: float
    url: str
    publish_time: str = ""
    keyword: str = ""

    def __post_init__(self) -> None:
        """轻量校验 + 类型归一化。"""
        self.product_id = str(self.product_id).strip()
        if not self.product_id:
            raise ModelError("product_id 不能为空")

        self.title = str(self.title).strip()
        if not self.title:
            raise ModelError("title 不能为空")

        try:
            self.price = float(self.price)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 防御性分支
            raise ModelError(f"price 必须可转换为 float，当前值：{self.price!r}") from exc
        if self.price < 0:
            raise ModelError(f"price 不能为负数：{self.price}")

        self.url = str(self.url).strip()
        self.publish_time = str(self.publish_time or "").strip()
        self.keyword = str(self.keyword or "").strip()

    # ------------------------------------------------------------------ #
    # 构造 / 序列化辅助
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, data: Dict[str, Any], keyword: str = "") -> "Product":
        """从字典构造 Product。

        Args:
            data: 至少包含 product_id / title / price / url 的字典。
            keyword: 若 data 中未带 keyword，则使用该参数补齐。

        Returns:
            构造好的 Product 实例。

        Raises:
            ModelError: 缺少必填字段或字段非法。
        """
        if not isinstance(data, dict):
            raise ModelError(f"from_dict 需要 dict，收到 {type(data).__name__}")

        missing = [k for k in ("product_id", "title", "price") if k not in data]
        if missing:
            raise ModelError(f"缺少必填字段：{', '.join(missing)}")

        return cls(
            product_id=data["product_id"],
            title=data["title"],
            price=data["price"],
            url=data.get("url", ""),
            publish_time=data.get("publish_time", ""),
            keyword=data.get("keyword") or keyword,
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为普通字典（便于 JSON 序列化 / 日志输出）。"""
        return asdict(self)

    @property
    def price_text(self) -> str:
        """价格的展示文案，例如 `¥199.00`。"""
        return f"¥{self.price:.2f}"

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志可读性
        return f"<Product {self.product_id} {self.title[:20]} {self.price_text}>"
