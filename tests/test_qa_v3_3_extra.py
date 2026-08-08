"""QA 独立验证 v3.3 增量（6 项改进）补充用例。

本文件由 QA（严过关）独立编写，与工程师自测（test_gui_v3_3.py）错开，
重点覆盖：
  1. 新关键词默认规则：必含留空 + 预置排除词（含「收」）
  2. 旧配置（无 filters 字段）加载兼容不崩
  3. 日志「仅展示符合的低价」开关：混合商品逐条原因标注 / 关闭无明细
  4. build_search_payload 最新发布排序（sortField=create + sortValue=desc）
  5. GUI 移除「获取 Cookie」按钮后的残留检查 + COOKIE_MANUAL_HELP 提示词
  6. 企业微信机器人通道标签
"""

from __future__ import annotations

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import config_from_dict  # noqa: E402
from xianyu_alert.fetcher import Fetcher, build_search_payload  # noqa: E402
from xianyu_alert.gui import (  # noqa: E402
    CHANNEL_LABELS,
    COOKIE_MANUAL_HELP,
    PRESET_EXCLUDE_KEYWORDS,
    XianyuAlertGUI,
    build_config_dict,
    config_to_form,
)
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402

PRESET_EXPECTED = ["回收", "置换", "收购", "高价回收", "收"]


# ---------------------------------------------------------------------- #
# 0. 辅助：可控商品抓取器（返回测试者指定的商品序列）
# ---------------------------------------------------------------------- #
class StubFetcher(Fetcher):
    """按预定列表返回商品的抓取器（keyword 字段自动补齐）。"""

    name = "stub"

    def __init__(self, products: list) -> None:
        self._products = list(products)
        self.cookies = ""

    def fetch(self, keyword: str) -> list:
        out = []
        for p in self._products:
            item = Product(
                product_id=p["id"],
                title=p["title"],
                price=p["price"],
                url=f"https://www.goofish.com/item?id={p['id']}",
                keyword=keyword,
            )
            out.append(item)
        return out

    def set_cookies(self, cookie_str: str) -> None:
        self.cookies = cookie_str


def make_cfg(keywords: list) -> object:
    """构造通过 config_from_dict 校验的配置（mock 抓取器 + 内存库）。"""
    return config_from_dict(
        {
            "keywords": keywords,
            "monitor": {"interval_seconds": 60},
            "fetcher": {"type": "mock"},
            "storage": {"path": ":memory:"},
            "notify": {"channels": [{"type": "console"}]},
        }
    )


# ---------------------------------------------------------------------- #
# 1. PRESET_EXCLUDE_KEYWORDS 常量断言（v3.3 追加「收」）
# ---------------------------------------------------------------------- #
class TestPresetConstant(unittest.TestCase):
    def test_contains_all_preset_words(self) -> None:
        self.assertEqual(
            list(PRESET_EXCLUDE_KEYWORDS),
            PRESET_EXPECTED,
        )

    def test_shou_is_last(self) -> None:
        # 「收」是 v3.3 追加项，位于列表末尾
        self.assertEqual(PRESET_EXCLUDE_KEYWORDS[-1], "收")


# ---------------------------------------------------------------------- #
# 2. 新关键词默认过滤规则：必含留空 + 预置排除词
# ---------------------------------------------------------------------- #
class TestDefaultFilters(unittest.TestCase):
    def _gui(self) -> XianyuAlertGUI:
        return object.__new__(XianyuAlertGUI)

    def test_new_keyword_required_empty(self) -> None:
        """即使关键词含数字+单位，必含词也必须留空（不再自动提取）。"""
        gui = self._gui()
        filters = gui._default_filters("光威 笔记本DDR4 3200 16G")
        self.assertEqual(filters["required_keywords"], [])
        self.assertEqual(filters["exclude_keywords"], PRESET_EXPECTED)

    def test_new_keyword_exclude_preset(self) -> None:
        gui = self._gui()
        filters = gui._default_filters("Switch")
        self.assertIn("收", filters["exclude_keywords"])
        self.assertEqual(filters["exclude_keywords"], PRESET_EXPECTED)


# ---------------------------------------------------------------------- #
# 3. 添加关键词流程 E2E：GUI 纯函数往返
#    _default_filters → build_config_dict → config_to_form
#    保存后 config 里确实是「空必含 + 预置排除」，重载后保持一致
# ---------------------------------------------------------------------- #
class TestAddKeywordRoundtrip(unittest.TestCase):
    def test_roundtrip_empty_required_preset_exclude(self) -> None:
        gui = object.__new__(XianyuAlertGUI)
        filters = gui._default_filters("DDR4 16G")
        self.assertEqual(filters, {"exclude_keywords": PRESET_EXPECTED, "required_keywords": []})

        # GUI 保存路径：build_config_dict 显式写出两个字段
        data = build_config_dict(
            keywords=[("DDR4 16G", 300.0)],
            interval_seconds=600,
            fetcher_type="mock",
            cookies="",
            storage_path=":memory:",
            channels={"console": {"enabled": True, "options": {}}},
            keyword_filters={"DDR4 16G": filters},
        )
        self.assertEqual(data["keywords"][0]["required_keywords"], [])
        self.assertEqual(data["keywords"][0]["exclude_keywords"], PRESET_EXPECTED)

        # 重载（config_from_dict）：必含仍为空、排除仍为预置词
        cfg = config_from_dict(data)
        self.assertEqual(cfg.keywords[0].required_keywords, [])
        self.assertEqual(cfg.keywords[0].exclude_keywords, PRESET_EXPECTED)

        # 重载（config_to_form 界面态）：与 config 解析一致
        form = config_to_form(data)
        self.assertEqual(form["keyword_filters"]["DDR4 16G"]["required_keywords"], [])
        self.assertEqual(
            form["keyword_filters"]["DDR4 16G"]["exclude_keywords"],
            PRESET_EXPECTED,
        )


# ---------------------------------------------------------------------- #
# 4. 旧配置兼容：无 filters 字段的 keyword 加载不崩
# ---------------------------------------------------------------------- #
class TestOldConfigCompat(unittest.TestCase):
    def test_keyword_without_filters_loads(self) -> None:
        """旧配置只有 keyword + max_price，不应崩溃。"""
        cfg = make_cfg([{"keyword": "Switch", "max_price": 1000}])
        self.assertEqual(len(cfg.keywords), 1)
        self.assertEqual(cfg.keywords[0].keyword, "Switch")
        self.assertEqual(cfg.keywords[0].exclude_keywords, [])
        # 未显式配置必含词 → 按旧行为自动提取（文档约定）
        self.assertEqual(cfg.keywords[0].required_keywords, [])

    def test_config_to_form_without_filters(self) -> None:
        form = config_to_form(
            {"keywords": [{"keyword": "Switch", "max_price": 500}]}
        )
        self.assertEqual(form["keywords"], [("Switch", 500.0)])
        self.assertIn("Switch", form["keyword_filters"])

    def test_keyword_explicit_empty_required_stays_empty(self) -> None:
        """显式写 required_keywords: [] 的关键词 → 保持为空（不自动提取）。"""
        cfg = make_cfg(
            [{"keyword": "DDR4 16G", "max_price": 300,
              "exclude_keywords": ["回收", "收"], "required_keywords": []}]
        )
        self.assertEqual(cfg.keywords[0].required_keywords, [])
        self.assertEqual(cfg.keywords[0].exclude_keywords, ["回收", "收"])


# ---------------------------------------------------------------------- #
# 5. 日志开关 E2E：混合商品逐条原因标注
#    构造：1 命中 + 1 必含缺失 + 1 排除命中 + 1 超阈值
# ---------------------------------------------------------------------- #
class TestLogDetailReasons(unittest.TestCase):
    """log_item_details=True 时逐条标注原因；False（默认）时无明细。"""

    RULE = {
        "keyword": "Switch",
        "max_price": 500,
        "exclude_keywords": ["回收", "收"],
        "required_keywords": ["16G"],
    }

    PRODUCTS = [
        # 命中低价：含必含、不碰排除、低于阈值、未出现 → ✅ 命中低价
        {"id": "1001", "title": "Switch OLED 全新 16G", "price": 300},
        # 必含缺失：标题无 16G → ⛔ 必含词缺失（优先级最高）
        {"id": "1002", "title": "Switch OLED 白色", "price": 300},
        # 排除命中：标题含「回收」（含 16G，先通过必含检查）→ ⛔ 排除词命中
        {"id": "1003", "title": "Switch 回收 16G", "price": 100},
        # 超阈值：通过过滤但价格 >= 500 → ⏭ 超阈值
        {"id": "1004", "title": "Switch 全新 16G 顶配", "price": 800},
    ]

    def _monitor(self, products: list):
        cfg = make_cfg([self.RULE])
        storage = Storage(":memory:")
        fetcher = StubFetcher(products)
        monitor = Monitor(cfg, fetcher, storage, [])
        return monitor, storage

    def test_mixed_reasons_all_correct(self) -> None:
        monitor, storage = self._monitor(self.PRODUCTS)
        try:
            with self.assertLogs("xianyu_alert.monitor", level="INFO") as cm:
                monitor.run_once(log_item_details=True)
        finally:
            storage.close()

        detail_lines = [ln for ln in cm.output if "[明细]" in ln]
        self.assertEqual(len(detail_lines), 4, f"每条商品都应有明细：{detail_lines}")

        text = "\n".join(detail_lines)
        # 1 命中低价（日志格式为「[明细] 原因 价格 —— 标题」，不含 product_id，用唯一标题断言）
        self.assertIn("✅ 命中低价", text)
        self.assertIn("Switch OLED 全新 16G", text)
        # 必含缺失
        self.assertIn("⛔ 必含词缺失", text)
        self.assertIn("Switch OLED 白色", text)
        # 排除命中
        self.assertIn("⛔ 排除词命中", text)
        self.assertIn("Switch 回收 16G", text)
        # 超阈值
        self.assertIn("⏭ 超阈值", text)
        self.assertIn("Switch 全新 16G 顶配", text)

    def test_default_off_no_detail_lines(self) -> None:
        """默认（False）不输出 [明细] 行——旧行为保持不变。"""
        monitor, storage = self._monitor(self.PRODUCTS)
        try:
            with self.assertLogs("xianyu_alert.monitor", level="INFO") as cm:
                monitor.run_once(log_item_details=False)
        finally:
            storage.close()
        detail_lines = [ln for ln in cm.output if "[明细]" in ln]
        self.assertEqual(detail_lines, [])

    def test_previous_round_reason(self) -> None:
        """第二轮出现同一商品 → 🔁 上一轮已出现（不重复）。"""
        monitor, storage = self._monitor(self.PRODUCTS)
        try:
            with self.assertLogs("xianyu_alert.monitor", level="INFO") as cm:
                monitor.run_once(log_item_details=True)  # 第 1 轮
                monitor.run_once(log_item_details=True)  # 第 2 轮（同一批商品）
        finally:
            storage.close()
        # 第 2 轮的 1001（上轮命中低价的商品）应标为「上一轮已出现」
        second_round = [ln for ln in cm.output if "[明细]" in ln][4:]
        text = "\n".join(second_round)
        self.assertIn("🔁 上一轮已出现", text)
        self.assertIn("Switch OLED 全新 16G", text)

    def test_already_notified_reason(self) -> None:
        """已提醒过的商品（不在上一轮集合中）→ 🔁 已提醒过。"""
        monitor, storage = self._monitor(self.PRODUCTS)
        try:
            hit = next(p for p in self.PRODUCTS if p["id"] == "1001")
            storage.mark_notified(
                Product(product_id="1001", title=hit["title"], price=hit["price"], url="", keyword="Switch")
            )
            with self.assertLogs("xianyu_alert.monitor", level="INFO") as cm:
                monitor.run_once(log_item_details=True)
        finally:
            storage.close()
        detail_lines = [ln for ln in cm.output if "[明细]" in ln]
        text = "\n".join(detail_lines)
        self.assertIn("🔁 已提醒过", text)
        self.assertIn("Switch OLED 全新 16G", text)


# ---------------------------------------------------------------------- #
# 6. 最新发布排序（需求 6）：sortField=create + sortValue=desc
# ---------------------------------------------------------------------- #
class TestSortPayload(unittest.TestCase):
    def test_payload_has_newest_sort(self) -> None:
        payload = build_search_payload("测试")
        self.assertEqual(payload["sortField"], "create")
        self.assertEqual(payload["sortValue"], "desc")

    def test_payload_keyword_ok(self) -> None:
        payload = build_search_payload("Switch", page_number=2, rows_per_page=15)
        self.assertEqual(payload["keyword"], "Switch")
        self.assertEqual(payload["pageNumber"], 2)
        self.assertEqual(payload["rowsPerPage"], 15)
        self.assertIs(payload["sortValue"], "desc")


# ---------------------------------------------------------------------- #
# 7. GUI 移除「获取 Cookie」按钮的残留检查 + 手动帮助
# ---------------------------------------------------------------------- #
class TestCookieRemoval(unittest.TestCase):
    def test_no_on_get_cookie_method(self) -> None:
        """XianyuAlertGUI 不应再有 on_get_cookie 方法。"""
        self.assertFalse(hasattr(XianyuAlertGUI, "on_get_cookie"))

    def test_no_playwright_import_in_gui(self) -> None:
        """gui 模块不应 import acquire_via_playwright（cookie.py 保留）。"""
        import xianyu_alert.gui as gui_module

        source = inspect.getsource(gui_module)
        self.assertNotIn("acquire_via_playwright", source.split("def ")[0])  # 顶层 import 区无引用
        self.assertNotIn("from .cookie import acquire_via_playwright", source)
        self.assertNotIn("import acquire_via_playwright", source)

    def test_cookie_manual_help_has_token_hint(self) -> None:
        self.assertIn("_m_h5_tk", COOKIE_MANUAL_HELP)
        self.assertIn("goofish.com", COOKIE_MANUAL_HELP)

    def test_manual_help_button_text_in_source(self) -> None:
        import xianyu_alert.gui as gui_module

        source = inspect.getsource(gui_module)
        self.assertIn("❓ 如何获取 Cookie？", source)
        self.assertNotIn("on_get_cookie(", source)


# ---------------------------------------------------------------------- #
# 8. 企业微信机器人通道标签（需求 3）
# ---------------------------------------------------------------------- #
class TestWebhookLabel(unittest.TestCase):
    def test_webhook_label_is_wecom_robot(self) -> None:
        self.assertEqual(CHANNEL_LABELS["webhook"], "企业微信机器人（Webhook）")


# ---------------------------------------------------------------------- #
# 9. 版本与更新日志
# ---------------------------------------------------------------------- #
class TestVersion(unittest.TestCase):
    def test_version_160(self) -> None:
        from xianyu_alert import __version__

        # v1.8 更新项：Cookie 自动刷新 + 进程单实例锁
        # （由 1.7.0 升级到 1.8.0，故旧断言同步更新）
        self.assertEqual(__version__, "1.8.0")

    def test_update_log_has_v180(self) -> None:
        from xianyu_alert.gui import UPDATE_LOG

        self.assertIn("v1.8.0", UPDATE_LOG)

    def test_update_log_has_v170(self) -> None:
        from xianyu_alert.gui import UPDATE_LOG

        self.assertIn("v1.7.0", UPDATE_LOG)

    def test_update_log_has_v140(self) -> None:
        from xianyu_alert.gui import UPDATE_LOG

        self.assertIn("v1.4.0", UPDATE_LOG)

    def test_update_log_has_v141(self) -> None:
        from xianyu_alert.gui import UPDATE_LOG

        self.assertIn("v1.4.1", UPDATE_LOG)

    def test_update_log_has_v150(self) -> None:
        from xianyu_alert.gui import UPDATE_LOG

        self.assertIn("v1.5.0", UPDATE_LOG)

    def test_update_log_has_v160(self) -> None:
        from xianyu_alert.gui import UPDATE_LOG

        self.assertIn("v1.6.0", UPDATE_LOG)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
