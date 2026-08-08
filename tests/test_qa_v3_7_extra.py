"""QA 独立验证 v3.7 增量边界用例（严过关 / Edward 独立编写，2026-08）。

本文件不依赖工程师的 test_gui_v3_7.py，聚焦「任务书第 5 节」要求的
补充边界场景，并作为任务 3（enabled E2E / 已售出 E2E / 存量库迁移实测 /
日志高亮闭环）的可重复执行断言：

  1. enabled 脏数据容错（"false" / "0" / 非 bool / None / 空串）
  2. GUI toggle 往返 + 行灰显 tags + 改名迁移 enabled
  3. 售出标记：mark 幂等、跨关键词全局生效、unmark 恢复、list_sold_out 排序、
     list_notified 与黑名单叠加
  4. 存量库迁移：旧 schema 建库 → 打开 → 迁移成功 + 数据保留 + 重复打开幂等
  5. log_tag_for_text：空文本 / 无前缀文本 / 各前缀映射 / monitor 前缀闭环 / 多前缀优先级
  6. monitor：停用词不打 fetched、failed_keywords 不含停用词
  7. fetcher 详情接口：payload 结构、parse_detail_sold_status 全分支、
     check_item_status 失败返回 None（Cookie 缺失 / ret 非 SUCCESS）
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xianyu_alert.gui as g  # noqa: E402
from xianyu_alert.config import config_from_dict  # noqa: E402
from xianyu_alert.fetcher import (  # noqa: E402
    FetchError,
    MtopFetcher,
    build_detail_payload,
    parse_detail_sold_status,
)
from xianyu_alert.gui import (  # noqa: E402
    LOG_TAG_DIM,
    LOG_TAG_NEW_ITEM,
    LOG_TAG_ROUND,
    LOG_TAG_SUMMARY,
    XianyuAlertGUI,
    log_tag_for_text,
    parse_enabled_flag,
)
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402


def make_config_dict(**overrides: object) -> dict:
    """构造能通过 config_from_dict 校验的最小配置字典（mock 抓取器）。"""
    data: dict = {
        "keywords": [{"keyword": "Switch", "max_price": 1000}],
        "monitor": {"interval_seconds": 60},
        "fetcher": {"type": "mock"},
        "storage": {"path": ":memory:"},
        "notify": {"channels": [{"type": "console"}]},
    }
    data.update(overrides)
    return data


def make_product(product_id: str, price: float = 50.0, title: str = "测试商品", keyword: str = "Switch") -> Product:
    """构造一条可用的 Product。"""
    return Product(
        product_id=product_id,
        title=title,
        price=price,
        url=f"https://www.goofish.com/item?id={product_id}",
        publish_time="2026-01-01 12:00:00",
        keyword=keyword,
    )


class FakeTree:
    """支持 values / tags / selection 的表格替身（v3.7 行灰显需要 tags）。"""

    def __init__(self) -> None:
        self._rows: dict = {}
        self._tags: dict = {}
        self._sel: list = []

    def insert(self, parent: str, index: str, values: object = None) -> str:
        iid = f"I{len(self._rows) + 1}"
        self._rows[iid] = list(values or [])
        return iid

    def get_children(self) -> list:
        return list(self._rows.keys())

    def item(self, iid: str, option: object = None, **kw: object) -> object:
        if "values" in kw:
            self._rows[iid] = list(kw["values"])
            return None
        if "tags" in kw:
            self._tags[iid] = kw["tags"]
            return None
        if option == "values":
            return tuple(self._rows.get(iid, []))
        if option is None:
            return {"values": tuple(self._rows.get(iid, []))}
        return None

    def delete(self, iid: str) -> None:
        self._rows.pop(iid, None)
        self._tags.pop(iid, None)

    def selection(self) -> list:
        return list(self._sel)

    def selection_set(self, iids: object) -> None:
        self._sel = list(iids) if isinstance(iids, (list, tuple)) else [iids]

    def tags_of(self, iid: str) -> tuple:
        return self._tags.get(iid, ())


class FakeVar:
    def __init__(self, v: str = "") -> None:
        self._v = v

    def get(self) -> str:
        return self._v

    def set(self, v: str) -> None:
        self._v = v


def make_gui_stub() -> SimpleNamespace:
    """构造 GUI 替身：绑定真实纯函数方法，避免真实 Tk 窗口。"""
    app = SimpleNamespace()
    app.tree_keywords = FakeTree()
    app.var_keyword = FakeVar()
    app.var_price = FakeVar()
    app._keyword_filters = {}
    app._append_log = lambda *a, **k: None
    app._refresh_keyword_empty_hint = lambda: None
    app._default_filters = lambda kw: {"exclude_keywords": [], "required_keywords": []}
    app._ensure_filters = XianyuAlertGUI._ensure_filters.__get__(app)
    app._filters_summary = lambda kw: "排除:无 必含:无"
    app._refresh_keyword_item = XianyuAlertGUI._refresh_keyword_item.__get__(app)
    app._collect_keywords = XianyuAlertGUI._collect_keywords.__get__(app)
    app._collect_keyword_rules = XianyuAlertGUI._collect_keyword_rules.__get__(app)
    app._apply_keyword_row_style = XianyuAlertGUI._apply_keyword_row_style.__get__(app)
    app.on_toggle_keyword = XianyuAlertGUI.on_toggle_keyword.__get__(app)
    app.on_update_keyword = XianyuAlertGUI.on_update_keyword.__get__(app)
    return app


# ---------------------------------------------------------------------- #
# 1. enabled：脏数据容错（与 config._parse_keywords 语义核对）
# ---------------------------------------------------------------------- #
class TestEnabledDirtyDataIndependent(unittest.TestCase):
    """独立复测：enabled 脏数据容错（"false" / 0 / 非 bool / None）。"""

    def test_string_false_variants(self) -> None:
        for raw in ("false", "0", "no", "off", ""):
            cfg = config_from_dict(make_config_dict(
                keywords=[{"keyword": "A", "max_price": 100, "enabled": raw}]
            ))
            self.assertFalse(cfg.keywords[0].enabled, f"enabled={raw!r} 应解析为停用")

    def test_string_true_variants(self) -> None:
        for raw in ("true", "1", "yes", "on"):
            cfg = config_from_dict(make_config_dict(
                keywords=[{"keyword": "A", "max_price": 100, "enabled": raw}]
            ))
            self.assertTrue(cfg.keywords[0].enabled, f"enabled={raw!r} 应解析为启用")

    def test_int_zero_and_one(self) -> None:
        cfg0 = config_from_dict(make_config_dict(
            keywords=[{"keyword": "A", "max_price": 100, "enabled": 0}]
        ))
        cfg1 = config_from_dict(make_config_dict(
            keywords=[{"keyword": "A", "max_price": 100, "enabled": 1}]
        ))
        self.assertFalse(cfg0.keywords[0].enabled)
        self.assertTrue(cfg1.keywords[0].enabled)

    def test_non_bool_dirty_does_not_raise(self) -> None:
        for raw in (None, {"weird": True}, ["list"], object()):
            with self.subTest(raw=type(raw).__name__):
                cfg = config_from_dict(make_config_dict(
                    keywords=[{"keyword": "A", "max_price": 100, "enabled": raw}]
                ))
                self.assertTrue(cfg.keywords[0].enabled, "未知类型应回退默认 True 且不抛异常")

    def test_config_missing_enabled_default_true(self) -> None:
        cfg = config_from_dict(make_config_dict(
            keywords=[{"keyword": "A", "max_price": 100}]
        ))
        self.assertTrue(cfg.keywords[0].enabled)


# ---------------------------------------------------------------------- #
# 2. GUI：toggle 往返 + 行灰显 + 改名迁移 enabled
# ---------------------------------------------------------------------- #
class TestEnabledGuiRoundtripIndependent(unittest.TestCase):
    """独立复测：GUI toggle 往返、灰显 tags、改名后 enabled 迁移。"""

    def _seed(self, app: SimpleNamespace, rows: list) -> str:
        iid = None
        for row in rows:
            iid = app.tree_keywords.insert("", "end", values=row)
        app.tree_keywords.selection_set([iid])
        return iid

    def test_toggle_roundtrip_and_collect(self) -> None:
        app = make_gui_stub()
        iid = self._seed(app, [("Switch", "1000", "启用✅", "—")])
        app._keyword_enabled = {"Switch": True}

        # 启用 → 停用
        app.on_toggle_keyword()
        self.assertFalse(app._keyword_enabled["Switch"])
        self.assertEqual(app._collect_keyword_rules(), [("Switch", 1000.0, False)])
        # 行灰显 tags
        self.assertEqual(app.tree_keywords.tags_of(iid), ("disabled",))
        # 状态列文案
        self.assertEqual(app.tree_keywords.item(iid, "values")[2], "停用⏸")
        # 二元组保持兼容
        self.assertEqual(app._collect_keywords(), [("Switch", 1000.0)])

        # 停用 → 启用
        app.on_toggle_keyword()
        self.assertTrue(app._keyword_enabled["Switch"])
        self.assertEqual(app._collect_keyword_rules(), [("Switch", 1000.0, True)])
        self.assertEqual(app.tree_keywords.tags_of(iid), ("enabled",))
        self.assertEqual(app.tree_keywords.item(iid, "values")[2], "启用✅")

    def test_toggle_no_selection_keeps_state(self) -> None:
        app = make_gui_stub()
        app.tree_keywords.insert("", "end", values=("Switch", "1000", "启用✅", "—"))
        app._keyword_enabled = {"Switch": True}
        with mock.patch("xianyu_alert.gui.messagebox.showinfo") as msg:
            app.on_toggle_keyword()
        self.assertTrue(app._keyword_enabled["Switch"], "未选中行时不应改变状态")
        msg.assert_called_once()

    def test_rename_keeps_disabled_state(self) -> None:
        """停用的关键词改名（错别字修正）后仍保持停用（v3.7 状态随行迁移）。"""
        app = make_gui_stub()
        iid = self._seed(app, [("Swtich", "1000", "停用⏸", "—")])
        app._keyword_enabled = {"Swtich": False}
        app._keyword_filters["Swtich"] = {"exclude_keywords": ["回收"], "required_keywords": []}
        app.var_keyword.set("Switch")
        app.var_price.set("1200")

        app.on_update_keyword()

        self.assertNotIn("Swtich", app._keyword_enabled, "旧名状态应迁移走")
        self.assertFalse(app._keyword_enabled["Switch"], "改名后仍应保持停用")
        self.assertIn("Switch", app._keyword_filters, "过滤规则应随行迁移")
        self.assertEqual(app._keyword_filters["Switch"]["exclude_keywords"], ["回收"])
        # 行值已更新（状态列仍停用）
        values = app.tree_keywords.item(iid, "values")
        self.assertEqual(values[0], "Switch")
        self.assertEqual(values[2], "停用⏸")

    def test_rename_without_prior_state_defaults_enabled(self) -> None:
        """旧关键词无 enabled 记录时，改名后默认启用。"""
        app = make_gui_stub()
        iid = self._seed(app, [("Old", "1000", "启用✅", "—")])
        app.var_keyword.set("New")
        app.var_price.set("800")
        app.on_update_keyword()
        self.assertTrue(app._keyword_enabled["New"])
        self.assertEqual(app.tree_keywords.item(iid, "values")[0], "New")


# ---------------------------------------------------------------------- #
# 3. 售出标记：幂等 / 全局生效 / 排序 / 黑名单叠加
# ---------------------------------------------------------------------- #
class TestSoldOutStorageIndependent(unittest.TestCase):
    """独立复测：mark 幂等、跨关键词全局、unmark 恢复、排序、黑名单叠加。"""

    def setUp(self) -> None:
        self.st = Storage(":memory:")
        for pid, price, keyword in (("1001", 10.0, "A"), ("1001", 20.0, "B"), ("1002", 30.0, "A")):
            self.st.mark_notified(make_product(pid, price=price, keyword=keyword))

    def tearDown(self) -> None:
        self.st.close()

    def test_mark_sold_out_idempotent(self) -> None:
        first = self.st.mark_sold_out("A", "1001", reason="人工标记")
        second = self.st.mark_sold_out("A", "1001", reason="人工标记")
        self.assertEqual(first, 1)
        self.assertEqual(second, 1, "重复标记不应抛异常且仍命中 1 行")
        self.assertTrue(self.st.is_sold_out("A", "1001"))

    def test_mark_sold_out_by_id_global_across_keywords(self) -> None:
        """同一 product_id 在多个关键词下：全局标记全部生效。"""
        self.st.mark_sold_out_by_id("1001", reason="详情接口判定")
        self.assertTrue(self.st.is_sold_out("A", "1001"))
        self.assertTrue(self.st.is_sold_out("B", "1001"), "跨关键词同 product_id 应全局生效")
        # list_notified 默认全排除
        ids = [r["product_id"] for r in self.st.list_notified()]
        self.assertNotIn("1001", ids)
        self.assertIn("1002", ids)
        # include_sold=True 时包含（B 行也包含）
        sold_rows = [r for r in self.st.list_notified(include_sold=True) if r["product_id"] == "1001"]
        self.assertEqual(len(sold_rows), 2)

    def test_unmark_restores(self) -> None:
        self.st.mark_sold_out_by_id("1001", reason="人工标记")
        affected = self.st.unmark_sold_out("1001")
        self.assertEqual(affected, 2)
        self.assertFalse(self.st.is_sold_out("A", "1001"))
        self.assertFalse(self.st.is_sold_out("B", "1001"))
        # 恢复后默认可见
        ids = [r["product_id"] for r in self.st.list_notified()]
        self.assertIn("1001", ids)
        # 对不存在的商品 unmark → 0（docstring「0 表示本来就没标记」场景）
        self.assertEqual(self.st.unmark_sold_out("no-such-id"), 0)
        # 已恢复的行再次 unmark 不抛异常、状态保持 False
        self.st.unmark_sold_out("1001")
        self.assertFalse(self.st.is_sold_out("A", "1001"))

    def test_list_sold_out_sorted_by_sold_at_desc(self) -> None:
        from datetime import datetime, timedelta

        # 1002 只在关键词 A 下（单行），1003 新插入单行 → 排序断言不含歧义
        self.st.mark_notified(make_product("1003", price=40.0, keyword="A"))
        now = datetime(2026, 8, 1, 12, 0, 0)
        self.st.mark_sold_out_by_id("1002", reason="r", ts=now)
        self.st.mark_sold_out_by_id("1003", reason="r", ts=now - timedelta(hours=1))
        rows = self.st.list_sold_out()
        self.assertEqual([r["product_id"] for r in rows], ["1002", "1003"])

    def test_list_notified_blacklist_and_sold_stacked(self) -> None:
        """已售出 + 黑名单叠加：两种排除互不干扰、全部生效。"""
        self.st.mark_sold_out_by_id("1001", reason="详情接口判定")
        self.st.add_blacklist("1002", keyword="A", reason="人工剔除")
        ids_default = [r["product_id"] for r in self.st.list_notified()]
        self.assertNotIn("1001", ids_default, "已售出应排除")
        self.assertNotIn("1002", ids_default, "黑名单应排除")
        # include_sold=True 仍排除黑名单（黑名单 NOT IN 无条件）
        ids_with_sold = [r["product_id"] for r in self.st.list_notified(include_sold=True)]
        self.assertIn("1001", ids_with_sold, "include_sold 时已售出可见")
        self.assertNotIn("1002", ids_with_sold, "include_sold 不能复活黑名单商品")

    def test_mark_sold_out_empty_pid_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.st.mark_sold_out("A", "")
        with self.assertRaises(ValueError):
            self.st.mark_sold_out_by_id(None)


# ---------------------------------------------------------------------- #
# 4. 存量库迁移：旧 schema → 打开 → 迁移成功 + 数据保留 + 重复打开幂等
# ---------------------------------------------------------------------- #
class TestStorageMigrationIndependent(unittest.TestCase):
    """独立实测：真实旧结构 SQLite 文件 → Storage 打开 → 自动迁移。"""

    OLD_SCHEMA = """
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
        UNIQUE (keyword, product_id)
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
    """

    def _build_old_db(self, path: str, rows: list) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(self.OLD_SCHEMA)
            conn.executemany(
                "INSERT INTO product (keyword, product_id, title, price, url, publish_time,"
                " first_seen, last_seen, notified) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def test_open_old_db_migrates_and_keeps_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "old.db")
            self._build_old_db(db, [
                ("Switch", "p1", "旧商品1", 50.0, "u1", "2026-01-01 12:00:00",
                 "2026-01-01 12:00:00", "2026-01-02 12:00:00", 1),
                ("Switch", "p2", "旧商品2", 80.0, "u2", "2026-01-01 12:00:00",
                 "2026-01-01 12:00:00", "2026-01-02 12:00:00", 1),
            ])
            st = Storage(db)  # 打开即应完成迁移
            try:
                # 新列存在
                cols = {row["name"] for row in st.conn.execute("PRAGMA table_info(product)")}
                self.assertIn("sold_out", cols)
                self.assertIn("sold_at", cols)
                self.assertIn("sold_reason", cols)
                # 数据保留
                rows = list(st.conn.execute(
                    "SELECT product_id, title, notified, sold_out FROM product ORDER BY product_id"
                ))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["title"], "旧商品1")
                self.assertEqual(rows[0]["sold_out"], 0, "迁移后旧数据 sold_out 应为 0")
                # 旧数据仍可被标记售出
                self.assertEqual(st.mark_sold_out("Switch", "p1", reason="迁移后标记"), 1)
                self.assertTrue(st.is_sold_out("Switch", "p1"))
            finally:
                st.close()

    def test_reopen_idempotent(self) -> None:
        """重复打开同一旧库（列已补齐）不报错、不重复 ALTER。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "old.db")
            self._build_old_db(db, [
                ("Switch", "p1", "旧商品", 50.0, "u1", "", "2026-01-01 12:00:00",
                 "2026-01-01 12:00:00", 1),
            ])
            st1 = Storage(db)
            st1.mark_sold_out("Switch", "p1", reason="第一次")
            st1.close()
            # 第二次打开：迁移函数应幂等（列已存在 → 跳过 ALTER）
            st2 = Storage(db)
            try:
                self.assertTrue(st2.is_sold_out("Switch", "p1"), "跨连接售出标记应保留")
                cols = {row["name"] for row in st2.conn.execute("PRAGMA table_info(product)")}
                self.assertIn("sold_out", cols)
                self.assertEqual(st2.list_sold_out()[0]["sold_reason"], "第一次")
            finally:
                st2.close()

    def test_new_db_open_no_error(self) -> None:
        """全新库（_SCHEMA 已含新列）打开不报错。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "new.db")
            st = Storage(db)
            try:
                cols = {row["name"] for row in st.conn.execute("PRAGMA table_info(product)")}
                self.assertIn("sold_out", cols)
                self.assertIn("sold_at", cols)
                self.assertIn("sold_reason", cols)
            finally:
                st.close()


# ---------------------------------------------------------------------- #
# 5. log_tag_for_text：空文本 / 无前缀 / 前缀映射 / monitor 闭环 / 多前缀
# ---------------------------------------------------------------------- #
class TestLogTagForTextIndependent(unittest.TestCase):
    """独立复测：日志高亮纯函数边界。"""

    def test_empty_text_falls_back_to_level(self) -> None:
        self.assertEqual(log_tag_for_text("INFO", ""), "INFO")
        self.assertEqual(log_tag_for_text("INFO", None), "INFO")

    def test_no_prefix_falls_back_to_level(self) -> None:
        self.assertEqual(log_tag_for_text("INFO", "普通日志行"), "INFO")
        self.assertEqual(log_tag_for_text("WARNING", "某某警告"), "WARNING")
        self.assertEqual(log_tag_for_text("ERROR", "某某错误"), "ERROR")
        self.assertEqual(log_tag_for_text("ALERT", "某某提醒"), "ALERT")

    def test_each_prefix_maps(self) -> None:
        self.assertEqual(log_tag_for_text("INFO", "🔔 关键词「Switch」：抓取 5"), LOG_TAG_NEW_ITEM)
        self.assertEqual(log_tag_for_text("INFO", "✨ 新出现 2 个商品"), LOG_TAG_NEW_ITEM)
        self.assertEqual(log_tag_for_text("INFO", "✅ 本轮完成：抓取 5 个"), LOG_TAG_SUMMARY)
        self.assertEqual(log_tag_for_text("INFO", "===== 第 1 轮监测开始 ====="), LOG_TAG_ROUND)
        self.assertEqual(log_tag_for_text("INFO", "🚫 关键词「A」：黑名单商品跳过 1 个"), LOG_TAG_DIM)
        self.assertEqual(log_tag_for_text("INFO", "⏸ 关键词「A」已停用，本轮跳过"), LOG_TAG_DIM)
        self.assertEqual(log_tag_for_text("INFO", "⚠️ 商品 123 无法判定"), "WARNING")
        self.assertEqual(log_tag_for_text("INFO", "❌ 校验在架线程异常"), "ERROR")

    def test_monitor_lines_close_the_loop(self) -> None:
        """monitor 实际输出的关键行 → 正确高亮 tag（前缀闭环）。"""
        hit_line = "🔔 关键词「Switch」：抓取 5，过滤 0，新出现 5，命中阈值(<1000.00)且未提醒 2"
        done_line = "✅ 本轮完成：抓取 5 个，新商品 5 个，通知 2 个，失败关键词 无"
        black_line = "🚫 关键词「Switch」：黑名单商品跳过 1 个（不提醒、不进提醒记录）"
        skip_line = "⏸ 关键词「Switch」已停用，本轮跳过（如需恢复请在配置中启用）"
        self.assertEqual(log_tag_for_text("INFO", hit_line), LOG_TAG_NEW_ITEM)
        self.assertEqual(log_tag_for_text("INFO", done_line), LOG_TAG_SUMMARY)
        self.assertEqual(log_tag_for_text("INFO", black_line), LOG_TAG_DIM)
        self.assertEqual(log_tag_for_text("INFO", skip_line), LOG_TAG_DIM)

    def test_multi_prefix_priority(self) -> None:
        """多前缀优先级：🚫/已停用 > 🔔 > ✅ > ⚠️/❌ > =====。"""
        # 🚫 压过 🔔（人工剔除优先弱化显示）
        self.assertEqual(log_tag_for_text("INFO", "🚫 已下架/售出（原命中 🔔 低价）"), LOG_TAG_DIM)
        # ✅ 本轮完成压过文本里的「失败」字样（完成行保持绿色）
        self.assertEqual(
            log_tag_for_text("INFO", "✅ 本轮完成：抓取 5 个，失败关键词 无"),
            LOG_TAG_SUMMARY,
        )
        # 🔔 命中压过「异常」字样（若同时出现）
        self.assertEqual(log_tag_for_text("INFO", "🔔 命中低价 5 个（无异常）"), LOG_TAG_NEW_ITEM)
        # ===== 轮次行不含错误字样时正确映射 ROUND
        self.assertEqual(log_tag_for_text("INFO", "===== 第 2 轮监测开始 ====="), LOG_TAG_ROUND)


# ---------------------------------------------------------------------- #
# 6. monitor：停用词不打 fetched、failed_keywords 不含停用词
# ---------------------------------------------------------------------- #
class TestMonitorDisabledKeywordIndependent(unittest.TestCase):
    """独立复测：停用词跳过（spy fetcher）。"""

    class SpyFetcher:
        name = "spy"

        def __init__(self) -> None:
            self.calls: list = []
            self.max_prices: list = []

        def fetch(self, keyword: str) -> list:
            self.calls.append(keyword)
            if keyword == "iPhone":
                raise Exception("模拟 iPhone 抓取失败")
            return [make_product("9001", price=10.0, keyword=keyword)]

        def set_cookies(self, _cookie: str) -> None:
            pass

        def set_max_price(self, price: object) -> None:
            self.max_prices.append(price)

        def close(self) -> None:
            pass

    class RecNotifier:
        name = "rec"

        def safe_notify(self, products: list) -> None:
            pass

    def _monitor(self) -> Monitor:
        self.st = Storage(":memory:")
        cfg = config_from_dict(make_config_dict(keywords=[
            {"keyword": "Disabled", "max_price": 100, "enabled": False},
            {"keyword": "iPhone", "max_price": 100, "enabled": True},
            {"keyword": "Active", "max_price": 100, "enabled": True},
        ]))
        self.fetcher = self.SpyFetcher()
        return Monitor(cfg, self.fetcher, self.st, [self.RecNotifier()])

    def tearDown(self) -> None:
        getattr(self, "st", None) and self.st.close()

    def test_disabled_not_fetched_not_counted(self) -> None:
        monitor = self._monitor()
        monitor.run_once()
        self.assertNotIn("Disabled", self.fetcher.calls, "停用词不应被 fetch")
        self.assertIn("iPhone", self.fetcher.calls)
        self.assertIn("Active", self.fetcher.calls)
        # fetched 只统计启用关键词（iPhone 失败不计、Active 计 1）
        self.assertEqual(monitor.last_result.fetched, 1)

    def test_failed_keywords_exclude_disabled(self) -> None:
        """failed_keywords 只含启用且失败的词，绝不含停用词。"""
        monitor = self._monitor()
        monitor.run_once()
        self.assertIn("iPhone", monitor.last_result.failed_keywords)
        self.assertNotIn("Disabled", monitor.last_result.failed_keywords)
        self.assertNotIn("Active", monitor.last_result.failed_keywords)

    def test_disabled_skip_log_emitted(self) -> None:
        monitor = self._monitor()
        with self.assertLogs("xianyu_alert.monitor", level="INFO") as ctx:
            monitor.run_once()
        self.assertTrue(
            any("已停用" in line for line in ctx.output),
            "应输出「⏸ 已停用跳过」日志",
        )


# ---------------------------------------------------------------------- #
# 7. fetcher 详情接口：payload / parse 全分支 / check_item_status 失败返回 None
# ---------------------------------------------------------------------- #
class TestFetcherDetailIndependent(unittest.TestCase):
    """独立复测：详情接口纯函数与失败语义。"""

    def test_build_detail_payload_structure(self) -> None:
        payload = build_detail_payload("123456")
        self.assertEqual(payload, {"itemId": "123456", "id": "123456"})
        # 空白输入返回空 id 但不抛
        empty = build_detail_payload("")
        self.assertEqual(empty["itemId"], "")

    def test_parse_detail_sold_status_branches(self) -> None:
        # 在线
        self.assertIs(parse_detail_sold_status({"itemDO": {"itemStatusStr": "在线", "itemStatus": 0}}), True)
        self.assertIs(parse_detail_sold_status({"itemDO": {"itemStatus": "0"}}), True)
        # 已售出 / 下架 / 删除 / 失效 / 违规 / 不存在
        for word in ("已售出", "已下架", "已删除", "失效", "违规", "不存在"):
            with self.subTest(word=word):
                self.assertIs(parse_detail_sold_status({"itemDO": {"itemStatusStr": word}}), False)
        self.assertIs(parse_detail_sold_status({"itemDO": {"itemStatus": 3}}), False)
        # 无法判定
        self.assertIsNone(parse_detail_sold_status(None))
        self.assertIsNone(parse_detail_sold_status("not-dict"))
        self.assertIsNone(parse_detail_sold_status({}))
        self.assertIsNone(parse_detail_sold_status({"itemDO": {}}))
        self.assertIsNone(parse_detail_sold_status({"itemDO": {"itemStatusStr": "未知文案"}}))
        self.assertIsNone(parse_detail_sold_status({"itemDO": {"itemStatus": "abc"}}))

    def _make_fetcher(self) -> MtopFetcher:
        return MtopFetcher(user_agent="test", cookies="", timeout=5)

    def test_check_item_status_missing_cookie_returns_none(self) -> None:
        fetcher = self._make_fetcher()
        # 源码捕获的是 FetchError（Cookie 缺失 / 无 token）；模拟真实异常类型
        with mock.patch.object(fetcher, "_check_cookies", side_effect=FetchError("缺少 Cookie")) as chk:
            self.assertIsNone(fetcher.check_item_status("123"))
            chk.assert_called_once()

    def test_check_item_status_ret_not_success_returns_none(self) -> None:
        fetcher = self._make_fetcher()
        result = {"ret": ["FAIL_SYS_TOKEN_EXOIRED"], "data": {"itemDO": {"itemStatusStr": "在线"}}}
        with mock.patch.object(fetcher, "_check_cookies", return_value=None), \
             mock.patch.object(fetcher, "_post_once", return_value=result) as post:
            self.assertIsNone(fetcher.check_item_status("123"))
            post.assert_called_once()
            # payload 应使用详情接口
            payload = post.call_args.args[0]
            self.assertIn("itemId", payload)

    def test_check_item_status_exception_returns_none(self) -> None:
        fetcher = self._make_fetcher()
        with mock.patch.object(fetcher, "_check_cookies", return_value=None), \
             mock.patch.object(fetcher, "_post_once", side_effect=Exception("网络错误")):
            self.assertIsNone(fetcher.check_item_status("123"))

    def test_check_item_status_online_returns_true(self) -> None:
        fetcher = self._make_fetcher()
        result = {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {"itemStatusStr": "在线", "itemStatus": 0}}}
        with mock.patch.object(fetcher, "_check_cookies", return_value=None), \
             mock.patch.object(fetcher, "_post_once", return_value=result):
            self.assertIs(fetcher.check_item_status("123"), True)


if __name__ == "__main__":
    unittest.main()
