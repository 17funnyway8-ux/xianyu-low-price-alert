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


if __name__ == "__main__":
    unittest.main()
