"""MonitorService 单元测试（P1）：生命周期 / 日志环形缓冲 / SSE 广播 / 配置热重启。

沿用项目测试惯例：全 mock（MockFetcher）+ `XY_DATA_DIR` 临时目录 + 内存/临时 SQLite，
不访问外网。所有服务实例在 tearDown 中 shutdown，保证后台线程与日志 handler 干净释放。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import gui, secure  # noqa: E402
from xianyu_alert.config import ConfigError  # noqa: E402

from web.monitor_service import MonitorService, web_form_from_config  # noqa: E402

_TMP: tempfile.TemporaryDirectory


def setUpModule() -> None:
    """测试模块级隔离：独立数据目录 + 独立密钥文件。"""
    global _TMP
    _TMP = tempfile.TemporaryDirectory(prefix="xianyu-web-svc-")
    os.environ["XY_DATA_DIR"] = _TMP.name
    secure.set_key_file(os.path.join(_TMP.name, "secret.key"))


def tearDownModule() -> None:
    """恢复环境变量与密钥位置。"""
    secure.set_key_file(None)
    os.environ.pop("XY_DATA_DIR", None)
    _TMP.cleanup()


def make_mock_config_dict(storage_path: str = "state/xianyu_alert.db") -> dict:
    """构造 mock 离线抓取配置字典（与 config.poc.yaml 对齐）。"""
    return {
        "keywords": [{"keyword": "Switch", "max_price": 1000}],
        "monitor": {"interval_seconds": 60, "user_agent": "", "cookies": ""},
        "fetcher": {"type": "mock", "mock_products_per_round": 5, "mock_fail_rounds": [], "pages": 1},
        "storage": {"path": storage_path},
        "notify": {"channels": [{"type": "console"}]},
        "preset_exclude_keywords": ["回收", "置换"],
    }


def make_form(**overrides) -> dict:
    """构造一个合法的 Web 表单（web_form_from_config 的逆结构）。"""
    form = {
        "keywords": [
            {
                "keyword": "Switch",
                "max_price": 1000,
                "enabled": True,
                "exclude_keywords": [],
                "required_keywords": [],
            }
        ],
        "interval_seconds": 60,
        "fetcher_type": "mock",
        "pages": 1,
        "user_agent": "",
        "storage_path": "state/xianyu_alert.db",
        "channels": {"console": {"enabled": True, "options": {}}},
        "cookie_alert_enabled": True,
        "cookie_check_interval_seconds": 0,
        "preset_exclude_keywords": ["回收", "置换"],
    }
    form.update(overrides)
    return form


class MonitorServiceTestCase(unittest.TestCase):
    """MonitorService 核心行为测试。"""

    def setUp(self) -> None:
        # 每个测试独立 config + 独立 SQLite 文件，避免共享 db 导致「已提醒去重」
        # 跨测试污染（MockFetcher 商品 ID 确定性，重复 run_once 会命中已提醒）。
        self.storage_path = f"state/db_{uuid.uuid4().hex}.db"
        self.config_path = os.path.join(_TMP.name, f"config_{uuid.uuid4().hex}.yaml")
        gui.save_raw_config(
            self.config_path, make_mock_config_dict(storage_path=self.storage_path)
        )
        self.service = MonitorService(config_path=self.config_path)

    def tearDown(self) -> None:
        self.service.shutdown()

    # ------------------------------------------------------------------ #
    def test_init_creates_default_config_when_missing(self) -> None:
        """配置文件不存在时自动生成默认配置。"""
        missing_path = os.path.join(_TMP.name, "missing.yaml")
        service = MonitorService(config_path=missing_path)
        try:
            self.assertTrue(os.path.isfile(missing_path))
            self.assertIsNotNone(service.config)
            self.assertIsNotNone(service.storage)
        finally:
            service.shutdown()

    def test_status_defaults(self) -> None:
        """初始状态：未运行、轮数 0。"""
        st = self.service.status()
        self.assertFalse(st["running"])
        self.assertEqual(st["round_count"], 0)
        self.assertEqual(st["notified_count"], 0)
        self.assertIsNone(st["last_round_at"])
        self.assertEqual(st["fetcher_type"], "mock")
        self.assertEqual(st["keyword_count"], 1)

    def test_run_once_increments_counters(self) -> None:
        """run_once 后轮数与提醒数递增，产生提醒记录。"""
        result = self.service.run_once()
        self.assertTrue(result["ok"])
        st = self.service.status()
        self.assertEqual(st["round_count"], 1)
        self.assertIsNotNone(st["last_round_at"])
        # mock 抓取器第 1 轮必产出低价商品（索引 3 的倍数 < 1000）
        self.assertGreaterEqual(st["notified_count"], 1)
        self.assertGreater(len(self.service.storage.list_notified(limit=100)), 0)

    def test_start_stop_lifecycle(self) -> None:
        """start 后 running=True，stop 后 running=False 且线程退出。"""
        self.service.start()
        time.sleep(0.3)
        st = self.service.status()
        self.assertTrue(st["running"])
        result = self.service.stop()
        self.assertTrue(result["ok"])
        self.assertFalse(self.service.status()["running"])
        self.assertIsNone(self.service._thread)  # noqa: SLF001 - 测试断言线程已清理

    def test_start_is_idempotent(self) -> None:
        """重复 start 返回 ok 且不产生第二个线程。"""
        self.service.start()
        time.sleep(0.2)
        thread_id = id(self.service._thread)  # noqa: SLF001
        result = self.service.start()
        self.assertTrue(result["ok"])
        self.assertEqual(id(self.service._thread), thread_id)  # noqa: SLF001
        self.service.stop()

    def test_run_once_rejected_while_running(self) -> None:
        """monitor 运行时手动 run_once 返回 409 语义（ok=False）。"""
        self.service.start()
        time.sleep(0.2)
        result = self.service.run_once()
        self.assertFalse(result["ok"])
        self.service.stop()

    def test_log_ring_buffer_and_recent_logs(self) -> None:
        """日志环形缓冲：容量 2000、recent_logs 返回最新、最旧被裁剪。"""
        for i in range(2100):
            self.service.append_log("INFO", f"日志第 {i} 条", "12:00:00")
        self.assertEqual(len(self.service._logs), 2000)  # noqa: SLF001
        recent = self.service.recent_logs(limit=10)
        self.assertEqual(len(recent), 10)
        self.assertIn("日志第 2099 条", recent[-1]["text"])

    def test_recent_logs_limit(self) -> None:
        """recent_logs(limit<=0) 返回全部（当前容量内）。"""
        self.service.append_log("INFO", "a", "12:00:00")
        self.service.append_log("WARNING", "b", "12:00:01")
        self.assertEqual(len(self.service.recent_logs(limit=0)), 2)
        self.assertEqual(len(self.service.recent_logs(limit=1)), 1)

    def test_broadcaster_publish_subscribe(self) -> None:
        """SSE 广播器：跨线程发布可被 asyncio 订阅者收到。"""
        loop = asyncio.new_event_loop()
        try:
            sub_id, queue = self.service.broadcaster.subscribe(loop)
            try:
                self.service.broadcaster.publish(
                    {"level": "INFO", "text": "hello", "ts": "12:00:00"}
                )
                item = loop.run_until_complete(asyncio.wait_for(queue.get(), timeout=2))
                self.assertEqual(item["text"], "hello")
            finally:
                self.service.broadcaster.unsubscribe(sub_id)
            # 取消订阅后不再收到
            self.service.broadcaster.publish({"level": "INFO", "text": "gone", "ts": "12:00:00"})
            with self.assertRaises(asyncio.TimeoutError):
                loop.run_until_complete(asyncio.wait_for(queue.get(), timeout=0.2))
        finally:
            loop.close()

    def test_append_log_broadcasts(self) -> None:
        """append_log 同时写入缓冲并广播。"""
        loop = asyncio.new_event_loop()
        try:
            sub_id, queue = self.service.broadcaster.subscribe(loop)
            try:
                self.service.append_log("INFO", "广播消息", "12:00:00")
                item = loop.run_until_complete(asyncio.wait_for(queue.get(), timeout=2))
                self.assertEqual(item["text"], "广播消息")
            finally:
                self.service.broadcaster.unsubscribe(sub_id)
        finally:
            loop.close()

    # ------------------------------------------------------------------ #
    def test_apply_config_hot_restart(self) -> None:
        """保存配置：写盘 + 热重启；运行中则保持运行。"""
        self.service.start()
        time.sleep(0.3)
        self.assertTrue(self.service.status()["running"])

        result = self.service.apply_config(make_form(interval_seconds=120))
        self.assertTrue(result["ok"])
        self.assertTrue(result["restarted"])
        self.assertEqual(self.service.config.monitor.interval_seconds, 120)
        # 运行状态保持
        time.sleep(0.2)
        self.assertTrue(self.service.status()["running"])
        self.service.stop()

    def test_apply_config_when_not_running(self) -> None:
        """未运行时保存配置：restarted=False，配置生效。"""
        result = self.service.apply_config(make_form(interval_seconds=90))
        self.assertTrue(result["ok"])
        self.assertFalse(result["restarted"])
        self.assertEqual(self.service.config.monitor.interval_seconds, 90)

    def test_apply_config_preserves_cookie_from_base(self) -> None:
        """表单保存路径不得清空/明文写 Cookie（保留 base 中的字段）。"""
        # 先写入一个密文 Cookie 占位（模拟既有配置）
        data = make_mock_config_dict(storage_path=self.storage_path)
        from xianyu_alert import secure as sec

        data["monitor"]["cookies"] = sec.encrypt_text("_m_h5_tk=abc_1700000000000; cookie2=zz")
        data["monitor"]["cookies_encrypted"] = True
        gui.save_raw_config(self.config_path, data)
        service = MonitorService(config_path=self.config_path)
        try:
            result = service.apply_config(make_form())
            self.assertTrue(result["ok"])
            saved = gui.load_raw_config(self.config_path)
            self.assertIn("fernet1:", str(saved["monitor"].get("cookies", "")))
            self.assertNotIn("_m_h5_tk=abc_1700000000000; cookie2=zz", str(saved))
        finally:
            service.shutdown()

    def test_apply_config_validation_error(self) -> None:
        """非法表单（空关键词）抛 ConfigError，不落盘。"""
        before = gui.load_raw_config(self.config_path)
        with self.assertRaises(ConfigError):
            self.service.apply_config(make_form(keywords=[]))
        after = gui.load_raw_config(self.config_path)
        self.assertEqual(before, after)  # 文件未被修改

    def test_reload_if_external_changed(self) -> None:
        """外部修改 config.yaml（模拟 cli login）→ mtime 检测触发重载。"""
        self.assertFalse(self.service.reload_if_external_changed())
        data = gui.load_raw_config(self.config_path)
        data["monitor"]["interval_seconds"] = 300
        data["keywords"][0]["max_price"] = 800
        gui.save_raw_config(self.config_path, data)
        time.sleep(0.05)  # 确保 mtime 变化
        self.assertTrue(self.service.reload_if_external_changed())
        self.assertEqual(self.service.config.monitor.interval_seconds, 300)
        self.assertEqual(self.service.config.keywords[0].max_price, 800)

    def test_web_form_round_trip(self) -> None:
        """web_form_from_config 脱敏 + 可逆：表单含 keywords/cookie_health，无明文 cookies。"""
        data = make_mock_config_dict()
        form = web_form_from_config(data)
        self.assertEqual(form["keywords"][0]["keyword"], "Switch")
        self.assertEqual(form["keywords"][0]["max_price"], 1000)
        self.assertEqual(form["cookie_health"]["state"], "missing")
        self.assertNotIn("cookies", form)  # 永不回传明文 Cookie 字段

    # ------------------------------------------------------------------ #
    # P2-09：监测参数透传
    # ------------------------------------------------------------------ #
    def test_web_form_includes_page_params(self) -> None:
        """web_form_from_config 返回 page_size / page_sleep（读 fetcher 节点）。"""
        data = make_mock_config_dict()
        data["fetcher"]["page_size"] = 50
        data["fetcher"]["page_sleep"] = 1.5
        form = web_form_from_config(data)
        self.assertEqual(form["page_size"], 50)
        self.assertEqual(form["page_sleep"], 1.5)

    def test_config_from_web_form_persists_page_params(self) -> None:
        """config_from_web_form 显式写回 page_size/page_sleep/user_agent，保留其它 fetcher 字段。"""
        base = make_mock_config_dict()
        base["fetcher"]["mock_products_per_round"] = 7
        form = make_form(
            page_size=40,
            page_sleep=0.5,
            user_agent="Mozilla/5.0 (Test)",
        )
        out = web_form_from_config  # noqa: F841 - 引用避免误用
        data = _config_from_form(form, base)
        self.assertEqual(data["fetcher"]["page_size"], 40)
        self.assertEqual(data["fetcher"]["page_sleep"], 0.5)
        self.assertEqual(data["monitor"]["user_agent"], "Mozilla/5.0 (Test)")
        self.assertEqual(data["fetcher"]["mock_products_per_round"], 7)  # base deepcopy 保留

    def test_config_from_web_form_rejects_bad_page_params(self) -> None:
        """page_size 越界 / page_sleep 负数 → ConfigError（api.py 转 400）。"""
        from web.monitor_service import config_from_web_form

        base = make_mock_config_dict()
        with self.assertRaises(ConfigError):
            config_from_web_form(make_form(page_size=0), base)
        with self.assertRaises(ConfigError):
            config_from_web_form(make_form(page_size=101), base)
        with self.assertRaises(ConfigError):
            config_from_web_form(make_form(page_sleep=-1), base)

    # ------------------------------------------------------------------ #
    # P2-11：明细日志开关
    # ------------------------------------------------------------------ #
    def test_detail_only_toggle(self) -> None:
        """set_detail_only 切换 + status 回显 detail_only 字段。"""
        self.assertTrue(self.service.status()["detail_only"])  # 默认 true
        self.service.set_detail_only(False)
        self.assertFalse(self.service.status()["detail_only"])
        self.service.set_detail_only(True)
        self.assertTrue(self.service.status()["detail_only"])

    # ------------------------------------------------------------------ #
    # P2-03：校验在架（服务层，mock fetcher + 计时断言）
    # ------------------------------------------------------------------ #
    def test_check_shelf_rejects_wrong_fetcher_type(self) -> None:
        """fetcher 非 mtop → ok=False + 400 语义。"""
        result = self.service.start_check_shelf(["111"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], 400)
        self.assertIn("mtop", result["message"])

    def test_check_shelf_rejects_empty_ids(self) -> None:
        """ids 空 → ok=False。"""
        result = self.service.start_check_shelf([])
        self.assertFalse(result["ok"])

    def test_check_shelf_rejects_while_monitor_running(self) -> None:
        """monitor 运行时 → 409 语义。"""
        # 切 mtop 配置（同一服务实例，保留 storage 路径）
        self.service.apply_config(
            make_form(fetcher_type="mtop", storage_path=self.storage_path)
        )
        self.service.start()
        time.sleep(0.2)
        try:
            result = self.service.start_check_shelf(["111"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], 409)
        finally:
            self.service.stop()

    def test_check_shelf_worker_rate_limit_and_sold_marking(self) -> None:
        """mtop 配置 + mock check_item_status：限速 ≥1.5s/条 + False 标记售出 + 进度统计。"""
        from unittest import mock

        from web import monitor_service as ms

        # 预置 3 条已提醒商品记录（让 mark_sold_out_by_id 有行可更新）
        ids = ["111", "222", "333"]
        for pid in ids:
            product = gui.make_sample_product(keyword="Switch")
            product.product_id = pid
            product.title = f"商品 {pid}"
            self.service.storage.mark_notified(product)

        # 切 mtop 配置（同一服务实例，保留 storage 路径与既有记录）
        self.service.apply_config(
            make_form(fetcher_type="mtop", storage_path=self.storage_path)
        )

        call_times: list = []

        class _FakeFetcher:
            def __init__(self) -> None:
                self.calls = []

            def check_item_status(self, product_id, timeout=None):
                self.calls.append(product_id)
                call_times.append(time.monotonic())
                # ids[0] 在架，ids[1] 售出，其余无法判定
                if product_id == ids[0]:
                    return True
                if product_id == ids[1]:
                    return False
                return None

            def close(self) -> None:
                pass

        fake = _FakeFetcher()
        with mock.patch.object(ms, "build_fetcher", return_value=fake):
            result = self.service.start_check_shelf(ids)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 3)
        # 轮询直到完成
        deadline = time.time() + 12
        while time.time() < deadline and self.service.check_shelf_status()["running"]:
            time.sleep(0.2)
        st = self.service.check_shelf_status()
        self.assertFalse(st["running"])
        self.assertEqual(st["total"], 3)
        self.assertEqual(st["done"], 1)
        self.assertEqual(st["sold"], 1)
        self.assertEqual(st["unknown"], 1)
        # 限速断言：相邻调用间隔 ≥ SOLD_CHECK_INTERVAL（允许微小抖动）
        self.assertEqual(len(call_times), 3)
        for i in range(1, len(call_times)):
            gap = call_times[i] - call_times[i - 1]
            self.assertGreaterEqual(gap, ms.SOLD_CHECK_INTERVAL - 0.2)
        # 售出标记落库（reason=详情接口判定）
        sold_rows = [
            r
            for r in self.service.storage.list_notified(limit=100, include_sold=True)
            if str(r["product_id"]) == ids[1]
        ]
        self.assertTrue(sold_rows)
        self.assertEqual(sold_rows[0]["sold_reason"], ms.SOLD_REASON_DETAIL)

    def test_check_shelf_cancel(self) -> None:
        """cancel 中止：worker 退出且状态 cancelled=True。"""
        from unittest import mock

        from web import monitor_service as ms

        cfg = gui.load_raw_config(self.config_path)
        cfg["fetcher"]["type"] = "mtop"
        gui.save_raw_config(self.config_path, cfg)
        service = MonitorService(config_path=self.config_path)
        try:
            class _SlowFetcher:
                def check_item_status(self, product_id, timeout=None):
                    return True

                def close(self) -> None:
                    pass

            with mock.patch.object(ms, "build_fetcher", return_value=_SlowFetcher()):
                # 10 条 → 正常跑完要 15s；取消应在 2s 内生效
                result = service.start_check_shelf([str(i) for i in range(10)])
            self.assertTrue(result["ok"])
            time.sleep(0.5)
            cancel = service.cancel_check_shelf()
            self.assertTrue(cancel["ok"])
            self.assertTrue(cancel["cancelled"])
            deadline = time.time() + 6
            while time.time() < deadline and service.check_shelf_status()["running"]:
                time.sleep(0.1)
            st = service.check_shelf_status()
            self.assertFalse(st["running"])
            self.assertTrue(st["cancelled"])
            self.assertLess(st["done"], 10)  # 未跑完全部
        finally:
            service.shutdown()

    # ------------------------------------------------------------------ #
    # P2-01：Cookie 池（服务层）
    # ------------------------------------------------------------------ #
    def test_cookie_pool_action_add_write_encrypted_and_reload(self) -> None:
        """add 落盘 fernet1: 密文 + reload；运行中 monitor 下一轮换用新池（R1）。"""
        from xianyu_alert import cookie

        cookie_str = f"_m_h5_tk=abc_{int(time.time() * 1000)}; cookie2=xyz"
        result = self.service.cookie_pool_action(
            action="add", name="主账号", cookie=cookie_str
        )
        self.assertTrue(result["ok"])
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertIn("fernet1:", raw)
        self.assertNotIn("_m_h5_tk=abc_", raw.replace("fernet1:", ""))
        # 重新加载后池可用（health=ok 计入轮换）
        items = self.service._read_pool_plaintext()  # noqa: SLF001
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "主账号")
        self.assertTrue(cookie.cookie_has_token(items[0]["cookie"]))

    def test_cookie_pool_action_delete_toggle_set_default(self) -> None:
        """delete / toggle / set_default 语义正确且写盘。"""
        cookie_str = f"_m_h5_tk=abc_{int(time.time() * 1000)}; cookie2=xyz"
        self.service.cookie_pool_action(action="add", name="A", cookie=cookie_str)
        self.service.cookie_pool_action(action="add", name="B", cookie=cookie_str)
        # toggle A → 停用
        result = self.service.cookie_pool_action(action="toggle", name="A")
        self.assertTrue(result["ok"])
        items = self.service._read_pool_plaintext()  # noqa: SLF001
        self.assertFalse(items[0]["enabled"])
        # delete B
        result = self.service.cookie_pool_action(action="delete", name="B")
        self.assertTrue(result["ok"])
        items = self.service._read_pool_plaintext()  # noqa: SLF001
        self.assertEqual(len(items), 1)
        # set_default A（A 停用但 Cookie 健康 → 仍可设默认）
        result = self.service.cookie_pool_action(action="set_default", name="A")
        self.assertTrue(result["ok"])
        saved = gui.load_raw_config(self.config_path)
        self.assertIn("fernet1:", str(saved["monitor"].get("cookies", "")))

    def test_cookie_pool_action_missing_token_confirmation(self) -> None:
        """add 缺 _m_h5_tk → ok=False + 400；force=true → 成功。"""
        result = self.service.cookie_pool_action(action="add", name="X", cookie="cookie2=abc")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], 400)
        self.assertIn("_m_h5_tk", result["message"])
        result = self.service.cookie_pool_action(
            action="add", name="X", cookie="cookie2=abc", force_missing_token=True
        )
        self.assertTrue(result["ok"])

    def test_cookie_pool_refresh_rejects_invalid(self) -> None:
        """refresh_selected 非 ok → 400 且不落盘。"""
        cookie_str = f"_m_h5_tk=abc_{int(time.time() * 1000)}; cookie2=xyz"
        self.service.cookie_pool_action(action="add", name="A", cookie=cookie_str)
        result = self.service.cookie_pool_action(
            action="refresh_selected", name="A", cookie="cookie2=bad"
        )
        self.assertFalse(result["ok"])
        items = self.service._read_pool_plaintext()  # noqa: SLF001
        self.assertNotIn("bad", items[0]["cookie"])

    # ------------------------------------------------------------------ #
    # P2-05 / P2-04：清空记录 / 售出撤销（服务层）
    # ------------------------------------------------------------------ #
    def test_clear_records_returns_count_and_preserves_blacklist(self) -> None:
        """clear_records 返回删除数（≥已提醒条数）、保留 blacklist。"""
        self.service.run_once()
        rows = self.service.storage.list_notified(limit=100)
        self.assertGreater(len(rows), 0)
        pid = rows[0]["product_id"]
        self.service.storage.add_blacklist(pid, keyword="Switch", reason="测试")
        result = self.service.clear_records()
        self.assertTrue(result["ok"])
        # clear_all 删除全部 product 行（含未提醒的），故 ≥ 已提醒条数
        self.assertGreaterEqual(result["deleted"], len(rows))
        self.assertEqual(len(self.service.storage.list_notified(limit=100)), 0)
        self.assertGreaterEqual(len(self.service.storage.list_blacklist(limit=100)), 1)

    def test_clear_records_conflict_while_running(self) -> None:
        """monitor 运行时 clear_records → ok=False + 409。"""
        self.service.start()
        time.sleep(0.2)
        try:
            result = self.service.clear_records()
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], 409)
        finally:
            self.service.stop()

    def test_unmark_record_round_trip(self) -> None:
        """mark_sold_out_by_id → unmark_record 幂等恢复。"""
        self.service.run_once()
        rows = self.service.storage.list_notified(limit=100)
        pid = rows[0]["product_id"]
        self.service.storage.mark_sold_out_by_id(pid, reason="人工标记")
        result = self.service.unmark_record(pid)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["updated"], 1)
        # 幂等
        result = self.service.unmark_record(pid)
        self.assertTrue(result["ok"])


def _config_from_form(form: dict, base: dict) -> dict:
    """测试辅助：web.monitor_service.config_from_web_form（延迟导入避免循环）。"""
    from web.monitor_service import config_from_web_form

    return config_from_web_form(form, base)


if __name__ == "__main__":
    unittest.main()
