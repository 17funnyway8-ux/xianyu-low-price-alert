"""SQLite 持久化：已见商品记录 + 已提醒去重 + 上一轮商品 ID 集合 + 临时黑名单 + 售出标记。

设计说明：
    - `product` 表按 (keyword, product_id) 唯一，记录首次/最近出现时间与是否已提醒；
    - `meta` 表存放跨重启的轮次状态，key 形如 `prev_ids:<keyword>`，value 为 JSON 数组；
    - `blacklist` 表（v3.6）记录用户**人工剔除**的商品（噪音/假货/非目标），
      按 product_id 全局唯一；被剔除商品不再提醒、不再出现在提醒记录，
      可在「黑名单管理」中恢复；
    - `product.sold_out` 字段（v3.7）记录「已售出/已下架」标记：
      由 GUI「标记已售出」按钮人工标记，或「校验在架」按钮调用闲鱼
      详情接口（mtop.taobao.idle.pc.detail）自动判定后写入；
      `list_notified` 默认排除已售出商品（提醒记录不再显示已卖掉/下架的商品），
      可通过 `include_sold=True` 查看。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Iterable, List, Optional, Set

from . import paths
from .models import Product

logger = logging.getLogger(__name__)

# 内存数据库标识（测试常用）
MEMORY_DB = ":memory:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS product (
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

CREATE INDEX IF NOT EXISTS idx_product_keyword ON product (keyword);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- v3.6 临时黑名单：用户人工剔除的商品（按 product_id 全局唯一）
CREATE TABLE IF NOT EXISTS blacklist (
    product_id TEXT PRIMARY KEY,
    keyword    TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
"""

_PREV_IDS_PREFIX = "prev_ids:"

#: v1.8 Cookie 过期提醒去抖状态 key 前缀（单条：`cookie_alert_state:<指纹>`）
_META_COOKIE_ALERT_PREFIX = "cookie_alert_state:"
#: v1.8 Cookie 池过期汇总去抖 key（value = JSON {"degraded":bool,"count":int,"at":str}）
_META_COOKIE_POOL_ALERT_KEY = "cookie_pool_alert_state"

#: v3.7 存量库迁移：旧 product 表没有 sold_out / sold_at / sold_reason 列，
#: 打开库时逐列补齐（幂等，ALTER TABLE 失败说明列已存在则忽略）。
_SOLD_OUT_MIGRATIONS = (
    ("sold_out", "INTEGER NOT NULL DEFAULT 0"),
    ("sold_at", "TEXT NOT NULL DEFAULT ''"),
    ("sold_reason", "TEXT NOT NULL DEFAULT ''"),
)


def _fmt_ts(ts: Optional[datetime] = None) -> str:
    """把 datetime 格式化为可读字符串；None 表示取当前时间。"""
    return (ts or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


class Storage:
    """基于 SQLite 的状态存储。

    Example:
        >>> st = Storage(":memory:")
        >>> st.is_notified("Switch", "123")
        False
        >>> st.close()
    """

    def __init__(self, db_path: str = MEMORY_DB) -> None:
        """初始化并建表。

        Args:
            db_path: SQLite 文件路径，或 `:memory:` 使用内存库。
                相对路径会锚定到 `paths.app_base_dir()`（frozen 后为 exe 同目录）。
        """
        self.db_path: str = db_path or MEMORY_DB
        if self.db_path != MEMORY_DB:
            # 相对路径统一锚定到应用根目录，保证 frozen 后 state/ 落在 exe 同目录
            self.db_path = paths.resolve_data_path(self.db_path)
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

        # check_same_thread=False：允许在不同线程（如信号处理）中收尾
        self.conn: sqlite3.Connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._closed: bool = False
        self._init_schema()

    # ------------------------------------------------------------------ #
    def _init_schema(self) -> None:
        """创建表结构（幂等），并补齐 v3.7 存量库迁移列。"""
        with self.conn:
            self.conn.executescript(_SCHEMA)
        self._migrate_sold_out_columns()

    def _migrate_sold_out_columns(self) -> None:
        """为旧版 `product` 表补齐 v3.7 的售出标记列（幂等）。

        旧库（v3.6 及以前）的 product 表没有 sold_out / sold_at / sold_reason，
        `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，必须显式 ALTER；
        列已存在时 ALTER 抛 OperationalError，捕获后忽略即可。
        """
        try:
            cur = self.conn.execute("PRAGMA table_info(product)")
            existing = {row["name"] for row in cur.fetchall()}
        except Exception:  # noqa: BLE001 - 表不存在等异常按无需迁移处理
            return
        for column, definition in _SOLD_OUT_MIGRATIONS:
            if column in existing:
                continue
            try:
                with self.conn:
                    self.conn.execute(f"ALTER TABLE product ADD COLUMN {column} {definition}")
                logger.info("已为 product 表补齐 v3.7 列：%s", column)
            except sqlite3.OperationalError:
                # 并发打开时可能已被其它连接补上
                pass

    # ------------------------------------------------------------------ #
    # 商品记录
    # ------------------------------------------------------------------ #
    def is_notified(self, keyword: str, product_id: str) -> bool:
        """判断某关键词下的某商品是否已经提醒过。

        Args:
            keyword: 关键词。
            product_id: 商品 ID。

        Returns:
            True 表示已提醒过（应跳过，避免重复通知）。
        """
        cur = self.conn.execute(
            "SELECT notified FROM product WHERE keyword = ? AND product_id = ?",
            (keyword, str(product_id)),
        )
        row = cur.fetchone()
        return bool(row["notified"]) if row is not None else False

    def save_seen(self, product: Product, round_ts: Optional[datetime] = None) -> None:
        """记录商品在本轮出现（更新 last_seen，首次插入时写 first_seen）。

        Args:
            product: 商品对象。
            round_ts: 本轮时间戳，默认当前时间。
        """
        ts = _fmt_ts(round_ts)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO product
                    (keyword, product_id, title, price, url, publish_time,
                     first_seen, last_seen, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT (keyword, product_id) DO UPDATE SET
                    title        = excluded.title,
                    price        = excluded.price,
                    url          = excluded.url,
                    publish_time = excluded.publish_time,
                    last_seen    = excluded.last_seen
                """,
                (
                    product.keyword,
                    product.product_id,
                    product.title,
                    float(product.price),
                    product.url,
                    product.publish_time,
                    ts,
                    ts,
                ),
            )

    def save_seen_many(self, products: Iterable[Product], round_ts: Optional[datetime] = None) -> int:
        """批量记录本轮出现的商品。

        Args:
            products: 商品可迭代对象。
            round_ts: 本轮时间戳。

        Returns:
            实际写入的商品数量。
        """
        count = 0
        for product in products:
            self.save_seen(product, round_ts)
            count += 1
        return count

    def mark_notified(self, product: Product, round_ts: Optional[datetime] = None) -> None:
        """将商品标记为「已提醒」（不存在则先插入）。

        Args:
            product: 商品对象。
            round_ts: 本轮时间戳。
        """
        ts = _fmt_ts(round_ts)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO product
                    (keyword, product_id, title, price, url, publish_time,
                     first_seen, last_seen, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (keyword, product_id) DO UPDATE SET
                    title        = excluded.title,
                    price        = excluded.price,
                    url          = excluded.url,
                    publish_time = excluded.publish_time,
                    last_seen    = excluded.last_seen,
                    notified     = 1
                """,
                (
                    product.keyword,
                    product.product_id,
                    product.title,
                    float(product.price),
                    product.url,
                    product.publish_time,
                    ts,
                    ts,
                ),
            )

    def get_product(self, keyword: str, product_id: str) -> Optional[sqlite3.Row]:
        """按 (keyword, product_id) 读取一条商品记录，不存在返回 None。"""
        cur = self.conn.execute(
            "SELECT * FROM product WHERE keyword = ? AND product_id = ?",
            (keyword, str(product_id)),
        )
        return cur.fetchone()

    def count_notified(self, keyword: Optional[str] = None) -> int:
        """统计已提醒的商品数量（可按关键词过滤）。"""
        if keyword is None:
            cur = self.conn.execute("SELECT COUNT(*) AS c FROM product WHERE notified = 1")
        else:
            cur = self.conn.execute(
                "SELECT COUNT(*) AS c FROM product WHERE notified = 1 AND keyword = ?",
                (keyword,),
            )
        row = cur.fetchone()
        return int(row["c"]) if row is not None else 0

    def list_notified(
        self,
        keyword: Optional[str] = None,
        limit: int = 100,
        include_sold: bool = False,
    ) -> List[sqlite3.Row]:
        """列出最近已提醒的商品记录（调试 / CLI / GUI 提醒记录展示用）。

        v3.6：自动排除已加入黑名单的商品（`product_id` 命中 `blacklist` 表），
        保证被用户人工剔除的商品不再出现在提醒记录中。
        v3.7：默认排除已标记「售出/下架」的商品（`sold_out = 1`），
        让提醒记录里不再显示已卖掉/下架的商品；`include_sold=True` 时
        包含已售出记录（GUI「显示已下架」开关使用）。

        Args:
            keyword: 仅列出该关键词下的记录；None 表示全部。
            limit: 最多返回条数。
            include_sold: True 时包含已售出/下架的商品；默认 False（隐藏）。

        Returns:
            已提醒且未进黑名单的商品记录列表（按 last_seen 倒序）。
        """
        sold_filter = "" if include_sold else "AND sold_out = 0"
        if keyword is None:
            cur = self.conn.execute(
                "SELECT * FROM product WHERE notified = 1 "
                "AND product_id NOT IN (SELECT product_id FROM blacklist) "
                f"{sold_filter} "
                "ORDER BY last_seen DESC LIMIT ?",
                (int(limit),),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM product WHERE notified = 1 AND keyword = ? "
                "AND product_id NOT IN (SELECT product_id FROM blacklist) "
                f"{sold_filter} "
                "ORDER BY last_seen DESC LIMIT ?",
                (keyword, int(limit)),
            )
        return list(cur.fetchall())

    # ------------------------------------------------------------------ #
    # 售出 / 下架标记（v3.7）：已卖掉或已下架的商品不再显示在提醒记录
    # ------------------------------------------------------------------ #
    def mark_sold_out(
        self,
        keyword: str,
        product_id: str,
        reason: str = "",
        ts: Optional[datetime] = None,
    ) -> int:
        """把某关键词下的一条商品标记为「已售出/下架」。

        Args:
            keyword: 商品命中的关键词。
            product_id: 商品 ID。
            reason: 标记原因（如「人工标记」「详情接口判定」），可为空串。
            ts: 标记时间，默认当前时间。

        Returns:
            实际更新的行数（0 表示该记录不存在，幂等）。
        """
        pid = str(product_id or "").strip()
        if not pid:
            raise ValueError("product_id 不能为空")
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE product SET sold_out = 1, sold_at = ?, sold_reason = ?
                WHERE keyword = ? AND product_id = ?
                """,
                (_fmt_ts(ts), str(reason or "").strip(), str(keyword or "").strip(), pid),
            )
            return int(cur.rowcount)

    def mark_sold_out_by_id(self, product_id: str, reason: str = "", ts: Optional[datetime] = None) -> int:
        """把某个 product_id 的全部记录标记为「已售出/下架」（全局）。

        详情接口校验 / GUI「标记已售出」使用：同一商品可能在多个关键词下
        各有一条记录，一起标记保证提醒记录整体隐藏。

        Args:
            product_id: 商品 ID（全局唯一）。
            reason: 标记原因，可为空串。
            ts: 标记时间，默认当前时间。

        Returns:
            实际更新的行数（0 表示该商品没有任何记录）。
        """
        pid = str(product_id or "").strip()
        if not pid:
            raise ValueError("product_id 不能为空")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE product SET sold_out = 1, sold_at = ?, sold_reason = ? WHERE product_id = ?",
                (_fmt_ts(ts), str(reason or "").strip(), pid),
            )
            return int(cur.rowcount)

    def unmark_sold_out(self, product_id: str) -> int:
        """把某个 product_id 的全部记录恢复为「在架」（撤销售出标记）。

        Args:
            product_id: 商品 ID。

        Returns:
            实际更新的行数（0 表示本来就没标记）。
        """
        pid = str(product_id or "").strip()
        if not pid:
            return 0
        with self.conn:
            cur = self.conn.execute(
                "UPDATE product SET sold_out = 0, sold_at = '', sold_reason = '' WHERE product_id = ?",
                (pid,),
            )
            return int(cur.rowcount)

    def is_sold_out(self, keyword: str, product_id: str) -> bool:
        """判断某关键词下的某商品是否已标记为「售出/下架」。"""
        cur = self.conn.execute(
            "SELECT sold_out FROM product WHERE keyword = ? AND product_id = ?",
            (str(keyword or "").strip(), str(product_id or "").strip()),
        )
        row = cur.fetchone()
        return bool(row["sold_out"]) if row is not None else False

    def list_sold_out(self, limit: int = 500) -> List[sqlite3.Row]:
        """列出全部已标记「售出/下架」的商品记录（GUI「显示已下架」用）。

        Args:
            limit: 最多返回条数。

        Returns:
            已售出商品记录列表（按 sold_at 倒序）。
        """
        cur = self.conn.execute(
            "SELECT * FROM product WHERE sold_out = 1 ORDER BY sold_at DESC, last_seen DESC LIMIT ?",
            (int(limit),),
        )
        return list(cur.fetchall())

    # ------------------------------------------------------------------ #
    # 临时黑名单（v3.6）：用户人工剔除的商品，不再提醒 / 不再进提醒记录
    # ------------------------------------------------------------------ #
    def add_blacklist(self, product_id: str, keyword: str = "", reason: str = "") -> None:
        """把商品加入黑名单（幂等：已存在则更新关键词与原因）。

        被加入黑名单的商品在 `monitor.run_once` 中会被跳过（不通知、
        不进 notified），在 `list_notified` / GUI 提醒记录中不再出现；
        可在 GUI「黑名单管理」或 `remove_blacklist` 中恢复。

        Args:
            product_id: 商品 ID（黑名单主键，全局唯一）。
            keyword: 商品命中的关键词（记录用途，可为空串）。
            reason: 加入原因（默认空串，GUI 可让用户填写）。

        Raises:
            ValueError: product_id 为空。
        """
        pid = str(product_id or "").strip()
        if not pid:
            raise ValueError("product_id 不能为空")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO blacklist (product_id, keyword, reason, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (product_id) DO UPDATE SET
                    keyword = excluded.keyword,
                    reason  = excluded.reason
                """,
                (pid, str(keyword or "").strip(), str(reason or "").strip(), _fmt_ts()),
            )

    def is_blacklisted(self, product_id: str) -> bool:
        """判断商品是否已在黑名单中。

        Args:
            product_id: 商品 ID。

        Returns:
            True 表示该商品已被人工剔除（应跳过提醒）。
        """
        pid = str(product_id or "").strip()
        if not pid:
            return False
        cur = self.conn.execute("SELECT 1 FROM blacklist WHERE product_id = ?", (pid,))
        return cur.fetchone() is not None

    def remove_blacklist(self, product_id: str) -> int:
        """把商品移出黑名单（恢复提醒）。

        Args:
            product_id: 商品 ID。

        Returns:
            实际删除的条数（0 表示本来就不在黑名单中）。
        """
        pid = str(product_id or "").strip()
        if not pid:
            return 0
        with self.conn:
            cur = self.conn.execute("DELETE FROM blacklist WHERE product_id = ?", (pid,))
            return int(cur.rowcount)

    def list_blacklist(self, limit: int = 500) -> List[sqlite3.Row]:
        """列出全部黑名单商品（GUI「黑名单管理」展示用）。

        Args:
            limit: 最多返回条数。

        Returns:
            黑名单记录列表（按加入时间倒序），字段含
            product_id / keyword / reason / created_at。
        """
        cur = self.conn.execute(
            "SELECT * FROM blacklist ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
        return list(cur.fetchall())

    # ------------------------------------------------------------------ #
    # 轮次状态（用于「新商品」判定，跨重启保持）
    # ------------------------------------------------------------------ #
    def get_previous_round_ids(self, keyword: str) -> Set[str]:
        """读取某关键词「上一轮出现过的商品 ID 集合」。

        Args:
            keyword: 关键词。

        Returns:
            商品 ID 集合；从未记录过时返回空集合。
        """
        cur = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_PREV_IDS_PREFIX + keyword,)
        )
        row = cur.fetchone()
        if row is None or not row["value"]:
            return set()
        try:
            data = json.loads(row["value"])
        except json.JSONDecodeError:
            logger.warning("关键词 %s 的上一轮 ID 集合解析失败，已按空集合处理", keyword)
            return set()
        if not isinstance(data, list):
            return set()
        return {str(x) for x in data}

    def set_previous_round_ids(self, keyword: str, product_ids: Iterable[str]) -> None:
        """写入某关键词「本轮出现过的商品 ID 集合」，供下一轮比对。

        Args:
            keyword: 关键词。
            product_ids: 本轮出现的商品 ID 集合。
        """
        payload = json.dumps(sorted({str(pid) for pid in product_ids}), ensure_ascii=False)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                (_PREV_IDS_PREFIX + keyword, payload),
            )

    def clear_previous_round_ids(self, keyword: str) -> None:
        """清空某关键词的上一轮 ID 集合（测试 / 重置用）。"""
        with self.conn:
            self.conn.execute("DELETE FROM meta WHERE key = ?", (_PREV_IDS_PREFIX + keyword,))

    # ------------------------------------------------------------------ #
    # 通用 meta 读写（v1.8：Cookie 过期提醒去抖状态，跨重启有效）
    # ------------------------------------------------------------------ #
    def get_meta_value(self, key: str) -> Optional[str]:
        """读取 meta 表 key 的 value。

        Args:
            key: meta 表主键（如 `cookie_alert_state:<指纹>`）。

        Returns:
            存储的字符串值；不存在返回 None。
        """
        cur = self.conn.execute("SELECT value FROM meta WHERE key = ?", (str(key),))
        row = cur.fetchone()
        return str(row["value"]) if row is not None else None

    def set_meta_value(self, key: str, value: str) -> None:
        """写入 / 更新 meta 表（INSERT OR REPLACE 语义，对齐 set_previous_round_ids）。

        Args:
            key: meta 表主键。
            value: 要存储的字符串值。
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                (str(key), str(value)),
            )

    def delete_meta_value(self, key: str) -> None:
        """删除 meta 表 key（幂等；不存在时无操作）。

        Args:
            key: meta 表主键。
        """
        with self.conn:
            self.conn.execute("DELETE FROM meta WHERE key = ?", (str(key),))

    # ------------------------------------------------------------------ #
    def clear_all(self) -> int:
        """清空全部去重记录（product 表 + meta 表）。

        用于图形界面的「清空去重记录」，清空后所有商品都会被视为「从未提醒过」，
        下一轮监测会重新提醒符合条件的商品。

        v3.6 注意：**不**清空 `blacklist` 表——黑名单是用户主动人工剔除的
        长期偏好，清空去重历史不应撤销；如需移除请在「黑名单管理」中单独恢复。

        Returns:
            被删除的 product 记录条数。
        """
        cur = self.conn.execute("SELECT COUNT(*) AS c FROM product")
        row = cur.fetchone()
        deleted = int(row["c"]) if row is not None else 0
        with self.conn:
            self.conn.execute("DELETE FROM product")
            self.conn.execute("DELETE FROM meta")
        logger.info("已清空去重记录，共删除 %d 条商品记录", deleted)
        return deleted

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """关闭数据库连接（幂等）。"""
        if not self._closed:
            try:
                self.conn.close()
            finally:
                self._closed = True

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
