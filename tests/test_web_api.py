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

    def test_healthz_has_version_and_data_dir(self) -> None:
        """P2-16 关于弹窗依赖：/healthz 返回 version 与 data_dir（QA FINDING-3）。"""
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("version", data)
        self.assertIn("data_dir", data)
        self.assertTrue(str(data["version"]))
        self.assertTrue(str(data["data_dir"]))

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
        """SSE 路由存在（TestClient 无法流式读取，仅验证注册）。

        注：starlette 1.5 的 `include_router` 会在 app.routes 中放一个
        `_IncludedRouter` 容器（无 path 属性），需递归收集其子路由路径。
        """
        paths = set()

        def _collect(routes) -> None:
            for route in routes:
                if hasattr(route, "path"):
                    paths.add(route.path)
                if hasattr(route, "routes"):
                    _collect(route.routes)
                # starlette 1.5 _IncludedRouter：子路由藏在 original_router.routes
                original = getattr(route, "original_router", None)
                if original is not None and hasattr(original, "routes"):
                    _collect(original.routes)

        _collect(web_api.app.routes)
        self.assertIn("/api/logs/stream", paths)

    # ------------------------------------------------------------------ #
    # P2-01：Cookie 池 API
    # ------------------------------------------------------------------ #
    def test_cookie_pool_list_empty_no_plaintext(self) -> None:
        """池为空时 GET /api/cookie/pool 返回空列表 + 单值状态，无明文。"""
        resp = self.client.get("/api/cookie/pool")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["pool"], [])
        self.assertIn("single", data)
        self.assertIn("health_state", data["single"])
        # P3：未配置任何 Cookie → 默认账号为空串
        self.assertEqual(data["default_name"], "")
        self.assertFalse(data["default_is_pool"])
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertNotIn("cookie_pool", raw)  # 未写任何池条目

    def test_cookie_pool_add_requires_token_confirmation(self) -> None:
        """add 缺 _m_h5_tk → 400 提示二次确认，不落盘。"""
        resp = self.client.post(
            "/api/cookie/pool",
            json={"action": "add", "name": "小号", "cookie": "cookie2=abc"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("_m_h5_tk", resp.json()["message"])
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertNotIn("cookie2=abc", raw)

    def test_cookie_pool_add_force_and_encrypted(self) -> None:
        """force_missing_token=true 添加成功：池落盘为 fernet1: 密文，绝无明文。"""
        resp = self.client.post(
            "/api/cookie/pool",
            json={
                "action": "add",
                "name": "主账号",
                "cookie": "cookie2=abc; _m_h5_tk=t_1700000000000",
                "force_missing_token": False,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("pool", data)
        # 落盘为密文
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertIn("fernet1:", raw)
        self.assertNotIn("_m_h5_tk=t_1700000000000", raw.replace("fernet1:", ""))
        # 列表脱敏回显
        pool = data["pool"]
        self.assertEqual(pool[0]["name"], "主账号")
        self.assertNotIn("_m_h5_tk=t_1700000000000", str(pool))
        self.assertTrue(pool[0]["masked"])

    def test_cookie_pool_toggle_and_auto_disable(self) -> None:
        """toggle 停用/启用 + auto_disable_expired 批量停用过期条目（保留条目）。"""
        # _m_h5_tk 时间戳 1700000000000（2023-11）→ 必然已过期
        self.client.post(
            "/api/cookie/pool",
            json={"action": "add", "name": "过期号", "cookie": "cookie2=abc; _m_h5_tk=t_1700000000000"},
        )
        # toggle 停用
        resp = self.client.post(
            "/api/cookie/pool", json={"action": "toggle", "name": "过期号"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("已停用", resp.json()["message"])
        # 再启用，然后 auto_disable_expired
        self.client.post("/api/cookie/pool", json={"action": "toggle", "name": "过期号"})
        resp = self.client.post(
            "/api/cookie/pool", json={"action": "auto_disable_expired"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("已自动停用 1 个", resp.json()["message"])
        pool = resp.json()["pool"]
        self.assertFalse(pool[0]["enabled"])  # 保留条目但停用

    def test_cookie_pool_set_default_writes_single(self) -> None:
        """set_default：健康条目写入单值 monitor.cookies（加密落盘）。"""
        cookie = valid_cookie()
        self.client.post(
            "/api/cookie/pool",
            json={"action": "add", "name": "默认号", "cookie": cookie},
        )
        resp = self.client.post(
            "/api/cookie/pool", json={"action": "set_default", "name": "默认号"}
        )
        self.assertEqual(resp.status_code, 200)
        saved = gui.load_raw_config(self.config_path)
        self.assertIn("fernet1:", str(saved["monitor"].get("cookies", "")))
        self.assertNotIn("_m_h5_tk=abc_", str(saved["monitor"].get("cookies", "")))
        # P3：GET 返回当前默认账号名（单值命中池条目 → 条目名）
        resp2 = self.client.get("/api/cookie/pool")
        self.assertEqual(resp2.status_code, 200)
        data = resp2.json()
        self.assertEqual(data["default_name"], "默认号")
        self.assertTrue(data["default_is_pool"])

    def test_cookie_pool_refresh_rejects_invalid(self) -> None:
        """refresh_selected 校验非 ok → 400 且不落盘。"""
        self.client.post(
            "/api/cookie/pool",
            json={"action": "add", "name": "小号", "cookie": valid_cookie()},
        )
        resp = self.client.post(
            "/api/cookie/pool",
            json={"action": "refresh_selected", "name": "小号", "cookie": "cookie2=bad"},
        )
        self.assertEqual(resp.status_code, 400)
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertNotIn("cookie2=bad", raw)

    # ------------------------------------------------------------------ #
    # P2-02：黑名单 API
    # ------------------------------------------------------------------ #
    def test_blacklist_list_and_restore(self) -> None:
        """GET /api/blacklist 4 字段 + restore 幂等移出。"""
        self.client.post("/api/monitor/run_once")
        records = self.client.get("/api/records").json()["records"]
        self.assertGreater(len(records), 0)
        pid = records[0]["product_id"]
        self.client.post(f"/api/records/{pid}/blacklist", json={"reason": "测试剔除"})
        resp = self.client.get("/api/blacklist")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["total"], 1)
        item = data["items"][0]
        for key in ("product_id", "keyword", "reason", "created_at"):
            self.assertIn(key, item)
        # restore 幂等
        resp = self.client.post(f"/api/blacklist/{pid}/restore")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()["removed"], 1)
        resp = self.client.post(f"/api/blacklist/{pid}/restore")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["removed"], 0)

    # ------------------------------------------------------------------ #
    # P2-03：校验在架 API
    # ------------------------------------------------------------------ #
    def test_check_shelf_rejects_non_mtop(self) -> None:
        """fetcher 非 mtop → 400「校验在架仅支持 mtop」。"""
        resp = self.client.post(
            "/api/records/check_shelf", json={"product_ids": ["123"]}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("mtop", resp.json()["message"])

    def test_check_shelf_rejects_empty_ids(self) -> None:
        """product_ids 空 → 400。"""
        resp = self.client.post("/api/records/check_shelf", json={"product_ids": []})
        self.assertEqual(resp.status_code, 400)

    def test_check_shelf_missing_body_422_envelope(self) -> None:
        """body 缺 product_ids → 422 且走统一信封 {ok:false,message}（QA FINDING-2）。"""
        resp = self.client.post("/api/records/check_shelf", json={})
        self.assertEqual(resp.status_code, 422)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("message", data)
        self.assertIn("参数校验失败", data["message"])
        self.assertNotIn("detail", data)  # 不再返回 FastAPI 默认 detail

    def test_check_shelf_accepted_202_with_mock_fetcher(self) -> None:
        """mtop 配置 + mock fetcher → 202 接受 + status 轮询完成。"""
        from unittest import mock

        from web import monitor_service as ms

        # 改配置为 mtop（否则 400）
        cfg = gui.load_raw_config(self.config_path)
        cfg["fetcher"]["type"] = "mtop"
        gui.save_raw_config(self.config_path, cfg)
        service = MonitorService(config_path=self.config_path)
        web_api.app.dependency_overrides[web_api.get_service] = lambda: service
        try:
            class _FakeFetcher:
                def __init__(self):
                    self.calls = []

                def check_item_status(self, product_id, timeout=None):
                    self.calls.append(product_id)
                    return True

                def close(self):
                    pass

            fake = _FakeFetcher()
            with mock.patch.object(ms, "build_fetcher", return_value=fake):
                resp = self.client.post(
                    "/api/records/check_shelf", json={"product_ids": ["111", "222"]}
                )
            self.assertEqual(resp.status_code, 202)
            self.assertTrue(resp.json()["ok"])
            self.assertEqual(resp.json()["count"], 2)
            # 轮询直到完成（1.5s × 2 条 + 余量）
            deadline = time.time() + 10
            done = False
            while time.time() < deadline:
                st = self.client.get("/api/records/check_shelf/status").json()
                if not st["running"]:
                    done = True
                    break
                time.sleep(0.2)
            self.assertTrue(done)
            st = self.client.get("/api/records/check_shelf/status").json()
            self.assertEqual(st["total"], 2)
            self.assertEqual(st["done"], 2)
        finally:
            service.shutdown()
            web_api.app.dependency_overrides[web_api.get_service] = lambda: self.service

    # ------------------------------------------------------------------ #
    # P2-04：售出撤销 API
    # ------------------------------------------------------------------ #
    def test_unmark_round_trip(self) -> None:
        """mark sold → 默认列表消失 → unmark → 恢复在架（幂等）。"""
        self.client.post("/api/monitor/run_once")
        records = self.client.get("/api/records").json()["records"]
        pid = records[0]["product_id"]
        self.client.post(f"/api/records/{pid}/sold")
        default_ids = {r["product_id"] for r in self.client.get("/api/records").json()["records"]}
        self.assertNotIn(pid, default_ids)
        resp = self.client.post(f"/api/records/{pid}/unmark")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()["updated"], 1)
        default_ids = {r["product_id"] for r in self.client.get("/api/records").json()["records"]}
        self.assertIn(pid, default_ids)
        # 幂等
        resp = self.client.post(f"/api/records/{pid}/unmark")
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------------ #
    # P2-05：清空记录 API
    # ------------------------------------------------------------------ #
    def test_clear_records_preserves_blacklist(self) -> None:
        """clear 后 product 清空、blacklist 保留、返回删除数。"""
        self.client.post("/api/monitor/run_once")
        records = self.client.get("/api/records").json()["records"]
        pid = records[0]["product_id"]
        self.client.post(f"/api/records/{pid}/blacklist", json={"reason": "保留测试"})
        resp = self.client.post("/api/records/clear")
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(resp.json()["deleted"], 0)
        # product 清空
        self.assertEqual(self.client.get("/api/records").json()["records"], [])
        # blacklist 保留
        bl = self.client.get("/api/blacklist").json()["items"]
        self.assertTrue(any(item["product_id"] == pid for item in bl))

    def test_clear_records_conflict_while_running(self) -> None:
        """monitor 运行时 clear → 409。"""
        self.client.post("/api/monitor/start")
        time.sleep(0.2)
        resp = self.client.post("/api/records/clear")
        self.assertEqual(resp.status_code, 409)
        self.client.post("/api/monitor/stop")

    # ------------------------------------------------------------------ #
    # P2-11：明细日志开关 API
    # ------------------------------------------------------------------ #
    def test_detail_only_toggle(self) -> None:
        """POST /api/monitor/detail_only 切换 + status 回显。"""
        resp = self.client.post("/api/monitor/detail_only", json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        st = self.client.get("/api/monitor/status").json()
        self.assertFalse(st["detail_only"])
        resp = self.client.post("/api/monitor/detail_only", json={"enabled": True})
        self.assertEqual(resp.status_code, 200)
        st = self.client.get("/api/monitor/status").json()
        self.assertTrue(st["detail_only"])


if __name__ == "__main__":
    unittest.main()
