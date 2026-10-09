"""存储层的**类型化行记录**（v1.10.1）。

为什么需要：此前 storage 的 list_* 直接返回 sqlite3.Row，调用方必须懂它的语义
（row["字段"]、取不到就 KeyError、字段名只能靠猜），SQL 细节事实上泄漏到了上层 ——
web/api.py 甚至直接写了一条 SELECT。

这里把"一行"变成有名字、有类型、有默认值的对象，同时**保留字典式访问**，
因此既有 row["title"] 调用点零改动；新代码则能用 rec.title 获得类型与补全。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, ClassVar, TypeVar

T = TypeVar("T", bound="Record")


@dataclass(frozen=True)
class Record:
    """所有行记录的基类：提供字典式访问与安全取值。

    frozen=True 表示"记录是某一刻的快照"——想改就构造新的，避免把 DB 行当可变对象传来传去。
    """

    #: 子类可声明字段别名（DB 列名 -> 属性名）；默认同名
    COLUMN_ALIASES: ClassVar[dict[str, str]] = {}

    def __getitem__(self, key: str) -> Any:
        """按列名取字段（与 sqlite3.Row 一致，未知列抛 KeyError）。"""
        try:
            return getattr(self, self.COLUMN_ALIASES.get(key, key))
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key: str, default: Any = None) -> Any:
        """按列名取字段，取不到返回默认值（sqlite3.Row 没有这个方法，属于增强）。"""
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self) -> list[str]:
        """返回所有可用列名（便于调用方做通用处理）。"""
        return [f.name for f in fields(self)]

    def to_dict(self) -> dict[str, Any]:
        """转成普通字典（供 JSON 序列化 / 模板渲染）。"""
        return asdict(self)

    @classmethod
    def from_row(cls: type[T], row: Any) -> T:
        """从 sqlite3.Row / dict 构造记录。

        查询可能只 SELECT 了部分列（例如"只要 keyword"），因此缺列一律回落到字段默认值，
        不让调用方因为"这一次查询少选了一列"而崩。
        """
        available: dict[str, Any] = {}
        if row is None:
            available = {}
        elif isinstance(row, dict):
            available = dict(row)
        else:
            try:
                # sqlite3.Row 的 keys() 才是列名；直接迭代它得到的是**值**（这里踩过坑）
                columns = row.keys()
            except AttributeError:
                columns = [f.name for f in fields(cls)]
            try:
                available = {k: row[k] for k in columns}
            except Exception:  # noqa: BLE001 - 任意行式对象
                available = {}
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in available:
                kwargs[f.name] = available[f.name]
        return cls(**kwargs)  # type: ignore[return-value]


@dataclass(frozen=True)
class NotifiedRecord(Record):
    """已提醒商品记录（product 表）。"""

    keyword: str = ""
    product_id: str = ""
    title: str = ""
    price: float = 0.0
    url: str = ""
    publish_time: str = ""
    image_url: str = ""
    first_seen: str = ""
    last_seen: str = ""
    notified: int = 0
    sold_out: int = 0
    sold_at: str = ""
    sold_reason: str = ""
    #: v1.10.3 新增（同样追加在末尾）
    seller: str = ""
    location: str = ""
    original_price: float | None = None

    @property
    def is_sold_out(self) -> bool:
        """是否已标记售出/下架（把 0/1 变成可读判定）。"""
        return bool(self.sold_out)

    @property
    def price_text(self) -> str:
        """价格的展示文本（¥12.34）。"""
        return "¥" + format(float(self.price or 0), ".2f")

    @property
    def original_price_text(self) -> str:
        """原价展示（无原价返回空串）。"""
        return f"¥{float(self.original_price):.2f}" if self.original_price else ""

    @property
    def display_title(self) -> str:
        """展示用标题（空标题回落到占位，避免界面出现空白行）。"""
        return self.title or "（无标题）"


@dataclass(frozen=True)
class SoldOutRecord(NotifiedRecord):
    """已售出/下架记录（product 表 + sold_* 字段）。"""


@dataclass(frozen=True)
class BlacklistEntry(Record):
    """黑名单条目（blacklist 表）。"""

    product_id: str = ""
    keyword: str = ""
    reason: str = ""
    created_at: str = ""
