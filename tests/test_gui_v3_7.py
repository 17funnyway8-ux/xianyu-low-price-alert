"""GUI / 核心模块 v3.7 增量测试。

覆盖（不真正显示窗口，纯函数 + 最小实例 + 线程级断言）：
  1. 关键词启用/停用（enabled）
     - config 解析：缺省 True、显式 false、脏数据容错
     - monitor 跳过停用关键词（不 fetch、不计入 fetched）
     - GUI：config_to_form / build_config_dict 往返携带 enabled、
       _collect_keyword_rules 携带状态、on_toggle_keyword 切换
  2. 日志高亮（v3.7）
     - log_tag_for_text 前缀 → tag 映射纯函数
     - monitor 关键行带 emoji 前缀（命中🔔 / 完成✅ / 停用⏸ / 黑名单🚫）
  3. 已售出/下架（需求 3，方案 B：详情接口校验）
     - storage：售出标记 CRUD、list_notified 排除、include_sold、存量库迁移
     - monitor：last_seen 刷新逻辑（商品再次出现更新时间）
     - fetcher：parse_detail_sold_status 纯函数、check_item_status 集成
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xianyu_alert.gui as g  # noqa: E402
from xianyu_alert.config import ConfigError, config_from_dict  # noqa: E402
from xianyu_alert.fetcher import (  # noqa: E402
    MTOP_DETAIL_API_NAME,
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
    build_config_dict,
    config_to_form,
    keyword_status_text,
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


def make_product(product_id: str, price: float = 50.0, title: str = "测试商品") -> Product:
    """构造一条可用的 Product。"""
    return Product(
        product_id=product_id,
        title=title,
        price=price,
        url=f"https://www.goofish.com/item?id={product_id}",
        publish_time="2026-01-01 12:00:00",
        keyword="Switch",
    )


# ---------------------------------------------------------------------- #
# 1. 关键词启用/停用（enabled）
# ---------------------------------------------------------------------- #
class TestKeywordEnabledConfig(unittest.TestCase):
    """config 解析 enabled 字段。"""

    def test_default_enabled_true(self) -> None:
        """旧 config 不写 enabled → 缺省 True（向后兼容）。"""
        cfg = config_from_dict(make_config_dict())
        self.assertTrue(cfg.keywords[0].enabled)

    def test_explicit_disabled(self) -> None:
        """显式 enabled: false → 解析为 False。"""
        data = make_config_dict(keywords=[
            {"keyword": "Switch", "max_price": 1000, "enabled": False},
            {"keyword": "iPhone", "max_price": 2000, "enabled": True},
        ])
        cfg = config_from_dict(data)
        self.assertFalse(cfg.keywords[0].enabled)
        self.assertTrue(cfg.keywords[1].enabled)

    def test_dirty_enabled_values(self) -> None:
        """脏数据容错：字符串 "false" / 0 / 未知类型不抛异常。"""
        data = make_config_dict(keywords=[
            {"keyword": "A", "max_price": 100, "enabled": "false"},
            {"keyword": "B", "max_price": 100, "enabled": 0},
            {"keyword": "C", "max_price": 100, "enabled": "yes"},
            {"keyword": "D", "max_price": 100, "enabled": {"weird": True}},
        ])
        cfg = config_from_dict(data)
        self.assertFalse(cfg.keywords[0].enabled, "字符串 false 应解析为停用")
        self.assertFalse(cfg.keywords[1].enabled, "整数 0 应解析为停用")
        self.assertTrue(cfg.keywords[2].enabled, "字符串 yes 应解析为启用")
        self.assertTrue(cfg.keywords[3].enabled, "未知类型回退默认 True")


class TestKeywordEnabledGui(unittest.TestCase):
    """GUI 纯函数与状态切换。"""

    def test_parse_enabled_flag(self) -> None:
        self.assertTrue(parse_enabled_flag(None, default=True))
        self.assertFalse(parse_enabled_flag(False))
        self.assertTrue(parse_enabled_flag("true"))
        self.assertFalse(parse_enabled_flag("false"))
        self.assertFalse(parse_enabled_flag(0))
        self.assertTrue(parse_enabled_flag(1))
        self.assertFalse(parse_enabled_flag("off"))
        self.assertTrue(parse_enabled_flag("on"))

    def test_keyword_status_text(self) -> None:
        self.assertEqual(keyword_status_text(True), "启用✅")
        self.assertEqual(keyword_status_text(False), "停用⏸")

    def test_config_to_form_carries_enabled(self) -> None:
        """config_to_form 读取 enabled 并在 form 中返回 keyword_enabled。"""
        data = make_config_dict(keywords=[
            {"keyword": "Switch", "max_price": 1000, "enabled": False},
        ])
        form = config_to_form(data)
        self.assertEqual(form["keyword_enabled"], {"Switch": False})
        # 缺省启用
        form2 = config_to_form({"keywords": [{"keyword": "Switch", "max_price": 100}]})
        self.assertEqual(form2["keyword_enabled"], {"Switch": True})

    def test_build_config_dict_carries_enabled(self) -> None:
        """build_config_dict 写入 enabled；不传 keyword_enabled 时不写该字段。"""
        data = build_config_dict(
            keywords=[("Switch", 1000.0), ("iPhone", 2000.0)],
            interval_seconds=60,
            fetcher_type="mock",
            cookies="",
            storage_path="state/x.db",
            channels={},
            keyword_enabled={"Switch": False, "iPhone": True},
        )
        by_name = {entry["keyword"]: entry for entry in data["keywords"]}
        self.assertIs(by_name["Switch"]["enabled"], False)
        self.assertIs(by_name["iPhone"]["enabled"], True)

        # 不传 keyword_enabled → 不写 enabled（向后兼容旧保存路径）
        data2 = build_config_dict(
            keywords=[("Switch", 1000.0)],
            interval_seconds=60,
            fetcher_type="mock",
            cookies="",
            storage_path="state/x.db",
            channels={},
        )
        self.assertNotIn("enabled", data2["keywords"][0])

    def test_roundtrip_enabled_through_config(self) -> None:
        """GUI 保存路径往返：build_config_dict → config_from_dict 携带 enabled。"""
        data = build_config_dict(
            keywords=[("Switch", 1000.0)],
            interval_seconds=60,
            fetcher_type="mock",
            cookies="",
            storage_path=":memory:",
            channels={},
            keyword_enabled={"Switch": False},
        )
        cfg = config_from_dict(data)
        self.assertFalse(cfg.keywords[0].enabled)

    def test_collect_keyword_rules_carries_enabled(self) -> None:
        """_collect_keyword_rules 返回含 enabled 的三元组；_collect_keywords 保持二元组。"""
        gui = object.__new__(XianyuAlertGUI)

        class FakeTree:
            def __init__(self) -> None:
                self.rows: dict = {"i1": ("Switch", "1000", "启用✅", "—")}

            def get_children(self) -> list:
                return list(self.rows.keys())

            def item(self, iid: str, option: object = None) -> object:
                return self.rows[iid]

        gui.tree_keywords = FakeTree()
        gui._keyword_enabled = {"Switch": False}
        # 既有兼容：_collect_keywords 仍返回 (keyword, price)
        self.assertEqual(gui._collect_keywords(), [("Switch", 1000.0)])
        # v3.7 新增：_collect_keyword_rules 携带 enabled
        self.assertEqual(gui._collect_keyword_rules(), [("Switch", 1000.0, False)])

    def test_toggle_keyword(self) -> None:
        """on_toggle_keyword 切换选中行启用状态。"""
        gui = object.__new__(XianyuAlertGUI)

        class FakeTree:
            def __init__(self) -> None:
                self.rows: dict = {"i1": ("Switch", "1000", "启用✅", "—")}
                self.sel: list = ["i1"]
                self.tags: dict = {}

            def get_children(self) -> list:
                return list(self.rows.keys())

            def item(self, iid: str, option: object = None, **kw: object) -> object:
                if "values" in kw:
                    self.rows[iid] = tuple(kw["values"])
                    return None
                if "tags" in kw:
                    self.tags[iid] = kw["tags"]
                    return None
                return self.rows[iid]

            def selection(self) -> list:
                return list(self.sel)

        gui.tree_keywords = FakeTree()
        gui._keyword_enabled = {"Switch": True}
        gui._append_log = lambda *a, **k: None
        gui.on_toggle_keyword()
        self.assertFalse(gui._keyword_enabled["Switch"], "启用 → 停用")
        gui.on_toggle_keyword()
        self.assertTrue(gui._keyword_enabled["Switch"], "停用 → 启用")


class TestKeywordEnabledMonitor(unittest.TestCase):
    """monitor 跳过停用关键词（不 fetch、不计入 fetched）。"""

    class CountingFetcher:
        name = "counting"

        def __init__(self) -> None:
            self.calls: list = []

        def fetch(self, keyword: str) -> list:
            self.calls.append(keyword)
            return []

        def set_cookies(self, _cookie: str) -> None:
            pass

        def set_max_price(self, _max_price: object) -> None:
            pass

        def close(self) -> None:
            pass

    class RecNotifier:
        name = "rec"

        def safe_notify(self, products: list) -> None:
            pass

    def test_disabled_keyword_not_fetched(self) -> None:
        st = Storage(":memory:")
        try:
            cfg = config_from_dict(make_config_dict(keywords=[
                {"keyword": "Switch", "max_price": 1000, "enabled": False},
                {"keyword": "iPhone", "max_price": 2000, "enabled": True},
            ]))
            fetcher = self.CountingFetcher()
            monitor = Monitor(cfg, fetcher, st, [self.RecNotifier()])
            count = monitor.run_once()
            self.assertEqual(count, 0)
            self.assertEqual(fetcher.calls, ["iPhone"], "停用关键词不应被 fetch")
            self.assertEqual(monitor.last_result.fetched, 0)
        finally:
            st.close()

    def test_all_disabled_no_fetch_and_skipped_log(self) -> None:
        st = Storage(":memory:")
        try:
            cfg = config_from_dict(make_config_dict(keywords=[
                {"keyword": "Switch", "max_price": 1000, "enabled": False},
            ]))
            fetcher = self.CountingFetcher()
            monitor = Monitor(cfg, fetcher, st, [self.RecNotifier()])
            with self.assertLogs("xianyu_alert.monitor", level="INFO") as ctx:
                monitor.run_once()
            joined = "\n".join(ctx.output)
            self.assertIn("已停用", joined, "停用关键词应打「已停用」日志")
            self.assertEqual(fetcher.calls, [])
        finally:
            st.close()


# ---------------------------------------------------------------------- #
# 2. 日志高亮（v3.7）
# ---------------------------------------------------------------------- #
class TestLogHighlight(unittest.TestCase):
    """log_tag_for_text 前缀 → tag 映射。"""

    def test_new_item_prefixes(self) -> None:
        self.assertEqual(log_tag_for_text("INFO", "🔔 关键词「Switch」：… 命中阈值"), LOG_TAG_NEW_ITEM)
        self.assertEqual(log_tag_for_text("INFO", "关键词「Switch」：… 新出现 5 个"), LOG_TAG_NEW_ITEM)
        self.assertEqual(log_tag_for_text("INFO", "✨ 发现新商品 3 个"), LOG_TAG_NEW_ITEM)
        self.assertEqual(log_tag_for_text("INFO", "  [明细] ✅ 命中低价 ¥100 测试"), LOG_TAG_NEW_ITEM)

    def test_summary_prefixes(self) -> None:
        self.assertEqual(log_tag_for_text("INFO", "✅ 本轮完成：抓取 5 个"), LOG_TAG_SUMMARY)
        self.assertEqual(log_tag_for_text("INFO", "本轮完成：抓取 5 个"), LOG_TAG_SUMMARY)
        self.assertEqual(log_tag_for_text("INFO", "配置已保存到 config.yaml"), LOG_TAG_SUMMARY)

    def test_dim_and_warning_and_error(self) -> None:
        self.assertEqual(log_tag_for_text("INFO", "🚫 关键词「Switch」：黑名单商品跳过"), LOG_TAG_DIM)
        self.assertEqual(log_tag_for_text("INFO", "⏸ 关键词「Switch」已停用"), LOG_TAG_DIM)
        self.assertEqual(log_tag_for_text("INFO", "⚠️ Cookie 即将过期"), "WARNING")
        self.assertEqual(log_tag_for_text("WARNING", "something"), "WARNING")
        self.assertEqual(log_tag_for_text("INFO", "❌ Cookie 已过期"), "ERROR")
        self.assertEqual(log_tag_for_text("ERROR", "boom"), "ERROR")
        self.assertEqual(log_tag_for_text("INFO", "监控线程异常退出"), "ERROR")

    def test_round_prefix(self) -> None:
        self.assertEqual(log_tag_for_text("INFO", "===== 第 1 轮监测开始 ====="), LOG_TAG_ROUND)
        self.assertEqual(log_tag_for_text("INFO", "第 2 轮监测开始"), LOG_TAG_ROUND)

    def test_fallback_to_level(self) -> None:
        self.assertEqual(log_tag_for_text("INFO", "普通信息"), "INFO")
        self.assertEqual(log_tag_for_text("DEBUG", "调试"), "DEBUG")
        self.assertEqual(log_tag_for_text("ALERT", "某提醒"), "ALERT")

    def test_monitor_emits_highlight_prefixes(self) -> None:
        """monitor 关键日志行带 emoji 前缀（与 GUI 高亮映射一致）。"""
        st = Storage(":memory:")
        try:
            cfg = config_from_dict(make_config_dict(keywords=[
                {"keyword": "Switch", "max_price": 1000, "enabled": False},
            ]))
            fetcher = mock.Mock()
            fetcher.fetch.return_value = []
            monitor = Monitor(cfg, fetcher, st, [])
            with self.assertLogs("xianyu_alert.monitor", level="INFO") as ctx:
                monitor.run_once()
            joined = "\n".join(ctx.output)
            self.assertIn("⏸", joined)
            self.assertIn("✅ 本轮完成", joined)
        finally:
            st.close()


# ---------------------------------------------------------------------- #
# 3. 已售出/下架（需求 3）
# ---------------------------------------------------------------------- #
class TestSoldOutStorage(unittest.TestCase):
    """storage 售出标记 CRUD + list 排除 + 存量库迁移。"""

    def test_mark_and_unmark(self) -> None:
        st = Storage(":memory:")
        try:
            p = make_product("S-1")
            st.mark_notified(p)
            self.assertFalse(st.is_sold_out("Switch", "S-1"))
            self.assertEqual(st.mark_sold_out("Switch", "S-1", reason="人工标记"), 1)
            self.assertTrue(st.is_sold_out("Switch", "S-1"))
            # 全局标记（同 product_id 多关键词）
            self.assertEqual(st.mark_sold_out_by_id("S-1", reason="详情接口判定"), 1)
            # 恢复
            self.assertEqual(st.unmark_sold_out("S-1"), 1)
            self.assertFalse(st.is_sold_out("Switch", "S-1"))
            # 不存在的记录返回 0（幂等）
            self.assertEqual(st.mark_sold_out("Switch", "NOPE"), 0)
            self.assertEqual(st.unmark_sold_out("NOPE"), 0)
        finally:
            st.close()

    def test_mark_empty_pid_raises(self) -> None:
        st = Storage(":memory:")
        try:
            with self.assertRaises(ValueError):
                st.mark_sold_out("Switch", "  ")
            with self.assertRaises(ValueError):
                st.mark_sold_out_by_id("  ")
        finally:
            st.close()

    def test_list_notified_excludes_sold(self) -> None:
        st = Storage(":memory:")
        try:
            p1 = make_product("A-ON", price=10.0)
            p2 = make_product("B-SOLD", price=20.0)
            st.mark_notified(p1)
            st.mark_notified(p2)
            self.assertEqual(len(st.list_notified()), 2)

            st.mark_sold_out_by_id("B-SOLD", reason="详情接口判定")
            rows = st.list_notified()
            self.assertEqual({r["product_id"] for r in rows}, {"A-ON"}, "已售出默认隐藏")
            self.assertEqual(len(st.list_notified(keyword="Switch")), 1)

            rows_include = st.list_notified(include_sold=True)
            self.assertEqual({r["product_id"] for r in rows_include}, {"A-ON", "B-SOLD"})
            # list_sold_out 只含已售出
            sold_rows = st.list_sold_out()
            self.assertEqual({r["product_id"] for r in sold_rows}, {"B-SOLD"})
        finally:
            st.close()

    def test_migration_adds_sold_out_column(self) -> None:
        """旧版 product 表（无 sold_out 列）打开后自动补列。"""
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE product (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                product_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL DEFAULT 0,
                url TEXT NOT NULL DEFAULT '',
                publish_time TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL DEFAULT '',
                last_seen TEXT NOT NULL DEFAULT '',
                notified INTEGER NOT NULL DEFAULT 0,
                UNIQUE (keyword, product_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO product (keyword, product_id, title, price, notified) "
            "VALUES ('Switch', 'OLD-1', '旧商品', 100, 1)"
        )
        conn.commit()
        conn.close()

        st = Storage(":memory:")
        # 先建新 schema 再模拟旧库：直接验证迁移逻辑可独立运行
        st.close()
        # 用旧 schema 文件库验证：建临时旧库文件 → Storage 打开自动迁移
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            old_db = os.path.join(tmp, "old.db")
            old_conn = sqlite3.connect(old_db)
            old_conn.executescript(
                """
                CREATE TABLE product (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    price REAL NOT NULL DEFAULT 0,
                    url TEXT NOT NULL DEFAULT '',
                    publish_time TEXT NOT NULL DEFAULT '',
                    first_seen TEXT NOT NULL DEFAULT '',
                    last_seen TEXT NOT NULL DEFAULT '',
                    notified INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (keyword, product_id)
                );
                """
            )
            old_conn.execute(
                "INSERT INTO product (keyword, product_id, title, price, notified) "
                "VALUES ('Switch', 'OLD-1', '旧商品', 100, 1)"
            )
            old_conn.commit()
            old_conn.close()

            st2 = Storage(old_db)
            try:
                cols = {row["name"] for row in st2.conn.execute("PRAGMA table_info(product)")}
                self.assertIn("sold_out", cols)
                self.assertIn("sold_at", cols)
                self.assertIn("sold_reason", cols)
                st2.mark_sold_out("Switch", "OLD-1", reason="迁移后标记")
                self.assertTrue(st2.is_sold_out("Switch", "OLD-1"))
                self.assertEqual(len(st2.list_notified()), 0, "旧记录标记后默认隐藏")
            finally:
                st2.close()

    def test_save_seen_refreshes_last_seen(self) -> None:
        """monitor/storage：商品再次出现时 last_seen 刷新（方案 C 判定的基础）。"""
        st = Storage(":memory:")
        try:
            p = make_product("R-1", price=50.0, title="反复出现")
            st.save_seen(p, round_ts=__import__("datetime").datetime(2026, 1, 1, 10, 0, 0))
            row1 = st.get_product("Switch", "R-1")
            self.assertEqual(row1["last_seen"], "2026-01-01 10:00:00")

            st.save_seen(p, round_ts=__import__("datetime").datetime(2026, 1, 2, 12, 30, 0))
            row2 = st.get_product("Switch", "R-1")
            self.assertEqual(row2["last_seen"], "2026-01-02 12:30:00", "last_seen 应刷新")
            self.assertEqual(row2["first_seen"], "2026-01-01 10:00:00", "first_seen 保持不变")
        finally:
            st.close()


class TestSoldOutFetcher(unittest.TestCase):
    """详情接口判定：parse_detail_sold_status 纯函数 + check_item_status 集成。"""

    def test_parse_detail_sold_status(self) -> None:
        # 在线
        self.assertTrue(parse_detail_sold_status({"itemDO": {"itemStatusStr": "在线", "itemStatus": 0}}))
        # 已售出 / 下架
        self.assertFalse(parse_detail_sold_status({"itemDO": {"itemStatusStr": "已售出"}}))
        self.assertFalse(parse_detail_sold_status({"itemDO": {"itemStatusStr": "已下架"}}))
        self.assertFalse(parse_detail_sold_status({"itemDO": {"itemStatus": 1}}))
        self.assertFalse(parse_detail_sold_status({"itemDO": {"itemStatusStr": "已删除"}}))
        # 无法判定
        self.assertIsNone(parse_detail_sold_status(None))
        self.assertIsNone(parse_detail_sold_status({}))
        self.assertIsNone(parse_detail_sold_status({"itemDO": {}}))
        self.assertIsNone(parse_detail_sold_status({"itemDO": {"title": "无状态"}}))

    def test_build_detail_payload(self) -> None:
        payload = build_detail_payload("123456")
        self.assertEqual(payload, {"itemId": "123456", "id": "123456"})

    def test_check_item_status_maps_online(self) -> None:
        fetcher = MtopFetcher(cookies="_m_h5_tk=abc_1700000000000; cookie2=x", retries=1)
        result = {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {"itemStatusStr": "在线", "itemStatus": 0}}}
        with mock.patch.object(fetcher, "_post_once", return_value=result) as post:
            status = fetcher.check_item_status("123456")
        self.assertIs(status, True)
        post.assert_called_once()
        # 请求确实发往详情接口
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs.get("api_name"), MTOP_DETAIL_API_NAME)
        fetcher.close()

    def test_check_item_status_maps_sold(self) -> None:
        fetcher = MtopFetcher(cookies="_m_h5_tk=abc_1700000000000; cookie2=x", retries=1)
        result = {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {"itemStatusStr": "已售出"}}}
        with mock.patch.object(fetcher, "_post_once", return_value=result):
            self.assertIs(fetcher.check_item_status("123456"), False)
        fetcher.close()

    def test_check_item_status_unknown_on_error(self) -> None:
        fetcher = MtopFetcher(cookies="_m_h5_tk=abc_1700000000000; cookie2=x", retries=1)
        with mock.patch.object(fetcher, "_post_once", side_effect=Exception("网络错误")):
            self.assertIsNone(fetcher.check_item_status("123456"))
        fetcher.close()


class TestSoldOutGuiHelpers(unittest.TestCase):
    """GUI 售出相关纯逻辑（无窗口）。"""

    def test_alert_row_sold_insert_marker(self) -> None:
        """_insert_alert_row 对 sold 行打 sold tag 并记录 _alert_sold。"""
        gui = object.__new__(XianyuAlertGUI)

        class FakeTree:
            def __init__(self) -> None:
                self.rows: dict = {}
                self.tags: dict = {}
                self.next_iid: int = 0

            def insert(self, _parent: str, _index: object, values: tuple = ()) -> str:
                iid = f"i{self.next_iid}"
                self.next_iid += 1
                self.rows[iid] = tuple(values)
                return iid

            def item(self, iid: str, option: object = None, **kw: object) -> object:
                if "values" in kw:
                    self.rows[iid] = tuple(kw["values"])
                    return None
                if "tags" in kw:
                    self.tags[iid] = kw["tags"]
                    return None
                return self.rows[iid]

        gui.tree_alerts = FakeTree()
        gui._alert_urls = {}
        gui._alert_product_ids = {}
        gui._alert_sold = {}
        gui._insert_alert_row(
            {"time": "t", "keyword": "Switch", "title": "x", "price": "¥1",
             "publish": "p", "url": "u", "product_id": "P1", "sold": True}
        )
        gui._insert_alert_row(
            {"time": "t", "keyword": "Switch", "title": "y", "price": "¥2",
             "publish": "p", "url": "u", "product_id": "P2", "sold": False}
        )
        items = gui.tree_alerts.get_children() if hasattr(gui.tree_alerts, "get_children") else list(gui.tree_alerts.rows)
        self.assertEqual(gui._alert_sold.get("i0"), True)
        self.assertEqual(gui._alert_sold.get("i1"), False)
        self.assertEqual(gui.tree_alerts.tags.get("i0"), ("sold",))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
