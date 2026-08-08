"""Web API 单元测试（P1）：FastAPI TestClient + mock fetcher + 临时目录。

覆盖：/healthz、config GET/PUT、monitor start/stop/status/run_once、
cookie save（校验拒绝/通过 + 加密落盘）、notify test、records（含售出/黑名单）、
静态页、SSE 日志流。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from xianyu_alert import gui, secure  # noqa: E402

from web import api as web_api  # noqa: E402
from web.monitor_service import MonitorService  # noqa: E402

_TMP: tempfile.TemporaryDirectory


def setUpModule() -> None:
    global _TMP
    _TMP = tempfile.TemporaryDirectory(prefix="xianyu-web-api-")
    os.environ["XY_DATA_DIR"] = _TMP.name
    secure.set_key_file(os.path.join(_TMP.name, "secret.key"))


def tearDownModule() -> None:
    secure.set_key_file(None)
    os.environ.pop("XY_DATA_DIR", None)
    _TMP.cleanup()


def make_mock_config_dict() -> dict:
    return {
        "keywords": [{"keyword": "Switch", "max_price": 1000}],
        "monitor": {"interval_seconds": 60, "user_agent": "", "cookies": ""},
        "fetcher": {"type": "mock", "mock_products_per_round": 5, "mock_fail_rounds": [], "pages": 1},
        "storage": {"path": "state/xianyu_alert.db"},
        "notify": {"channels": [{"type": "console"}]},
        "preset_exclude_keywords": ["回收", "置换"],
    }


def valid_cookie() -> str:
    """构造一个当前有效的 Cookie（_m_h5_tk 时间戳为当前毫秒，24h 内）。"""
    return f"_m_h5_tk=abc_{int(time.time() * 1000)}; cookie2=xyz"


def make_form(**overrides) -> dict:
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


class WebApiTestCase(unittest.TestCase):
    """FastAPI TestClient 集成测试。"""

    def setUp(self) -> None:
        # 每个测试独立 config + 独立 SQLite 文件，避免共享 db 跨测试污染
        self.storage_path = f"state/db_{uuid.uuid4().hex}.db"
        self.config_path = os.path.join(_TMP.name, f"config_{uuid.uuid4().hex}.yaml")
        gui.save_raw_config(self.config_path, make_mock_config_dict())
        # make_mock_config_dict 使用唯一 storage 路径
        cfg = gui.load_raw_config(self.config_path)
        cfg["storage"]["path"] = self.storage_path
        gui.save_raw_config(self.config_path, cfg)
        self.service = MonitorService(config_path=self.config_path)
        web_api.app.dependency_overrides[web_api.get_service] = lambda: self.service
        self.client = TestClient(web_api.app)

    def tearDown(self) -> None:
        self.service.shutdown()
        web_api.app.dependency_overrides.clear()

    # ------------------------------------------------------------------ #
    def test_healthz(self) -> None:
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("monitor_running", data)
        self.assertIn("last_round_at", data)

    def test_index_returns_html(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("闲鱼低价提醒工具", resp.text)

    def test_static_assets(self) -> None:
        resp = self.client.get("/static/app.js")
        self.assertEqual(resp.status_code, 200)
        resp_css = self.client.get("/static/style.css")
        self.assertEqual(resp_css.status_code, 200)

    # ------------------------------------------------------------------ #
    def test_get_config_masked(self) -> None:
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["keywords"][0]["keyword"], "Switch")
        self.assertEqual(data["cookie_health"]["state"], "missing")
        self.assertNotIn("cookies", data)  # 不回传明文 Cookie

    def test_put_config_valid(self) -> None:
        resp = self.client.put("/api/config", json={"form": make_form(interval_seconds=120)})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        # 已写盘
        saved = gui.load_raw_config(self.config_path)
        self.assertEqual(saved["monitor"]["interval_seconds"], 120)
        # 服务内生效
        self.assertEqual(self.service.config.monitor.interval_seconds, 120)

    def test_put_config_invalid_400(self) -> None:
        resp = self.client.put("/api/config", json={"form": make_form(keywords=[])})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("keywords", data["message"])  # 中文原因

    # ------------------------------------------------------------------ #
    def test_monitor_start_stop_status(self) -> None:
        resp = self.client.post("/api/monitor/start")
        self.assertEqual(resp.status_code, 200)
        time.sleep(0.3)
        st = self.client.get("/api/monitor/status").json()
        self.assertTrue(st["running"])
        resp = self.client.post("/api/monitor/stop")
        self.assertEqual(resp.status_code, 200)
        st = self.client.get("/api/monitor/status").json()
        self.assertFalse(st["running"])

    def test_monitor_run_once(self) -> None:
        resp = self.client.post("/api/monitor/run_once")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["notified"], 1)  # mock 第 1 轮必有低价命中

    def test_monitor_run_once_conflict_while_running(self) -> None:
        self.client.post("/api/monitor/start")
        time.sleep(0.2)
        resp = self.client.post("/api/monitor/run_once")
        self.assertEqual(resp.status_code, 409)
        self.client.post("/api/monitor/stop")

    # ------------------------------------------------------------------ #
    def test_cookie_save_reject_invalid(self) -> None:
        """缺 _m_h5_tk 的 Cookie 被拒绝（400 + 中文原因），不落盘。"""
        resp = self.client.post("/api/cookie/save", json={"cookie": "cookie2=abc"})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("Cookie", data["message"])
        # 未落盘：config 中 cookies 仍为空
        saved = gui.load_raw_config(self.config_path)
        self.assertEqual(saved["monitor"].get("cookies", ""), "")

    def test_cookie_save_accept_and_encrypt(self) -> None:
        """有效 Cookie 保存成功：Fernet 密文落盘 + 脱敏回显 + 触发重载。"""
        cookie = valid_cookie()
        resp = self.client.post("/api/cookie/save", json={"cookie": cookie})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("masked", data)
        # 落盘为 fernet1: 密文，绝不含明文
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertIn("fernet1:", raw)
        self.assertNotIn("_m_h5_tk=abc_", raw.replace("fernet1:", ""))
        # 服务内存中已解密可读（cookie_status 不再是 missing）
        form = web_api.web_form_from_config(gui.load_raw_config(self.config_path))
        self.assertEqual(form["cookie_health"]["state"], "ok")

    def test_cookie_save_empty_400(self) -> None:
        resp = self.client.post("/api/cookie/save", json={"cookie": ""})
        self.assertEqual(resp.status_code, 400)

    def test_cookie_save_encrypt_exception_no_plaintext(self) -> None:
        """QA FINDING-1 回归：加密步骤抛异常 → API 500 且 config.yaml 无明文残留。"""
        from unittest import mock

        cookie = valid_cookie()
        with mock.patch(
            "xianyu_alert.secure.encrypt_text", side_effect=OSError("encrypt boom")
        ):
            resp = self.client.post("/api/cookie/save", json={"cookie": cookie})
        self.assertEqual(resp.status_code, 500)
        saved = gui.load_raw_config(self.config_path)
        self.assertEqual(saved["monitor"].get("cookies", ""), "")  # 原样，无明文
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertNotIn("_m_h5_tk=abc_", raw)

    def test_cookie_save_encrypt_degraded_rejected_no_plaintext(self) -> None:
        """QA FINDING-1 回归：加密降级返回明文 → API 400 且 config.yaml 无明文残留。"""
        from unittest import mock

        cookie = valid_cookie()
        with mock.patch("xianyu_alert.secure.encrypt_text", return_value=cookie):
            resp = self.client.post("/api/cookie/save", json={"cookie": cookie})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("加密不可用", resp.json()["message"])
        saved = gui.load_raw_config(self.config_path)
        self.assertEqual(saved["monitor"].get("cookies", ""), "")
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertNotIn("_m_h5_tk=abc_", raw)

    # ------------------------------------------------------------------ #
    def test_notify_test_console(self) -> None:
        resp = self.client.post(
            "/api/notify/test", json={"channel_type": "console", "options": {}}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    def test_notify_test_incomplete_400(self) -> None:
        """参数不完整的通道（如缺 sendkey 的 serverchan）→ 400。"""
        resp = self.client.post(
            "/api/notify/test", json={"channel_type": "serverchan", "options": {}}
        )
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------------ #
    def test_records_list_after_run_once(self) -> None:
        """run_once 产生提醒记录后，/api/records 可读取。"""
        self.client.post("/api/monitor/run_once")
        resp = self.client.get("/api/records")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertGreater(len(data["records"]), 0)
        record = data["records"][0]
        for key in ("keyword", "title", "price", "url", "product_id", "time", "publish"):
            self.assertIn(key, record)

    def test_records_include_sold(self) -> None:
        """默认排除已售出；include_sold=true 后可见。"""
        self.client.post("/api/monitor/run_once")
        records = self.client.get("/api/records").json()["records"]
        self.assertGreater(len(records), 0)
        pid = records[0]["product_id"]
        # 标记售出
        resp = self.client.post(f"/api/records/{pid}/sold")
        self.assertEqual(resp.status_code, 200)
        # 默认列表不再包含该商品
        default_ids = {r["product_id"] for r in self.client.get("/api/records").json()["records"]}
        self.assertNotIn(pid, default_ids)
        # include_sold=true 包含
        sold_ids = {
            r["product_id"]
            for r in self.client.get("/api/records?include_sold=true").json()["records"]
        }
        self.assertIn(pid, sold_ids)

    def test_records_blacklist(self) -> None:
        """加入黑名单后从提醒记录消失。"""
        self.client.post("/api/monitor/run_once")
        records = self.client.get("/api/records").json()["records"]
        self.assertGreater(len(records), 0)
        pid = records[0]["product_id"]
        resp = self.client.post(
            f"/api/records/{pid}/blacklist", json={"reason": "人工剔除"}
        )
        self.assertEqual(resp.status_code, 200)
        ids = {r["product_id"] for r in self.client.get("/api/records").json()["records"]}
        self.assertNotIn(pid, ids)

    def test_records_sort(self) -> None:
        """排序参数生效（价格升序）。"""
        self.client.post("/api/monitor/run_once")
        data = self.client.get("/api/records?sort=price&order=asc").json()
        prices = [r["price"] for r in data["records"]]
        self.assertEqual(prices, sorted(prices))

    # ------------------------------------------------------------------ #
    def test_sse_stream(self) -> None:
        """SSE 日志流：连接即回放 + 实时广播（直接驱动生成器）。

        注：starlette 1.5 TestClient 对无限 StreamingResponse 的流式读取
        存在兼容问题，SSE 实时行为由本测试 + Docker 真机冒烟双重验证。
        """
        import asyncio

        class _MockRequest:
            """最小 Request 替身：is_disconnected 恒 False。"""

            async def is_disconnected(self):
                return False

        # 先产生若干日志（run_once 触发 monitor/notifier 日志）
        self.client.post("/api/monitor/run_once")

        async def consume():
            agen = web_api.sse_events(_MockRequest(), self.service)
            try:
                first = await agen.__anext__()
                self.assertIn(": connected", first)
                # 发布实时事件（广播先入队列；回放 backlog 耗尽后才会读到）
                self.service.append_log("INFO", "live-sse-event", "12:00:00")
                found = False
                for _ in range(50):  # 跳过全部回放行，直到收到实时事件
                    line = await asyncio.wait_for(agen.__anext__(), timeout=5)
                    if "live-sse-event" in line:
                        found = True
                        break
                self.assertTrue(found)
            finally:
                await agen.aclose()

        asyncio.run(consume())

    def test_sse_route_registered(self) -> None:
        """SSE 路由存在（TestClient 无法流式读取，仅验证注册）。"""
        paths = {route.path for route in web_api.app.routes}
        self.assertIn("/api/logs/stream", paths)


if __name__ == "__main__":
    unittest.main()
