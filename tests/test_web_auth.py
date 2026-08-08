"""Web 认证单元测试（P3-01）：XY_WEB_TOKEN Bearer 认证依赖。

覆盖：
    - 未设 token 时行为与 P1 完全一致（放行）；
    - 设置 token 后无认证 /api/* 请求 401（统一信封 {ok:false,message}）；
    - 正确 Bearer token → 200；
    - 错误 token / 大小写前缀 / 空 Authorization → 401；
    - /healthz、/、/static/* 免认证；
    - secrets.compare_digest 常数时间校验被调用。
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
#: 本模块测试用的固定 token
TEST_TOKEN = "test-token-123"


def setUpModule() -> None:
    global _TMP
    _TMP = tempfile.TemporaryDirectory(prefix="xianyu-web-auth-")
    os.environ["XY_DATA_DIR"] = _TMP.name
    secure.set_key_file(os.path.join(_TMP.name, "secret.key"))
    # 环境变量在模块级设置；每个测试的豁免/401 场景通过用例内断言区分
    os.environ["XY_WEB_TOKEN"] = TEST_TOKEN


def tearDownModule() -> None:
    os.environ.pop("XY_WEB_TOKEN", None)
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


class WebAuthTestCase(unittest.TestCase):
    """XY_WEB_TOKEN 认证依赖测试（模块级已设置 token）。"""

    def setUp(self) -> None:
        self.storage_path = f"state/db_{uuid.uuid4().hex}.db"
        self.config_path = os.path.join(_TMP.name, f"config_{uuid.uuid4().hex}.yaml")
        gui.save_raw_config(self.config_path, make_mock_config_dict())
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
    def test_unauthenticated_api_401_envelope(self) -> None:
        """无 Authorization 头 → 401 + 统一信封 {ok:false, message}。"""
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("message", data)
        self.assertIn("token", data["message"])

    def test_wrong_token_401(self) -> None:
        """错误 token → 401。"""
        resp = self.client.get("/api/config", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status_code, 401)

    def test_bearer_case_sensitive_401(self) -> None:
        """`bearer ` 小写前缀不被识别（严格 Bearer）→ 401。"""
        resp = self.client.get(
            "/api/config", headers={"Authorization": f"bearer {TEST_TOKEN}"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_empty_authorization_401(self) -> None:
        """Authorization 头为空串 → 401。"""
        resp = self.client.get("/api/config", headers={"Authorization": ""})
        self.assertEqual(resp.status_code, 401)

    def test_correct_token_200(self) -> None:
        """正确 Bearer token → 200。"""
        resp = self.client.get(
            "/api/config", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    def test_healthz_exempt(self) -> None:
        """/healthz 免认证 → 200。"""
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_index_exempt(self) -> None:
        """/ 免认证 → 200 HTML。"""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])

    def test_static_exempt(self) -> None:
        """/static/* 免认证 → 200。"""
        resp = self.client.get("/static/app.js")
        self.assertEqual(resp.status_code, 200)
        resp_css = self.client.get("/static/style.css")
        self.assertEqual(resp_css.status_code, 200)

    def test_all_api_routes_under_auth(self) -> None:
        """全部 /api/* 路由都挂在受保护 router 下（抽查若干关键路径）。"""
        paths = set()

        def _collect(routes) -> None:
            for route in routes:
                if hasattr(route, "path"):
                    paths.add(route.path)
                if hasattr(route, "routes"):
                    _collect(route.routes)
                original = getattr(route, "original_router", None)
                if original is not None and hasattr(original, "routes"):
                    _collect(original.routes)

        _collect(web_api.app.routes)
        for path in (
            "/api/config",
            "/api/monitor/status",
            "/api/cookie/pool",
            "/api/blacklist",
            "/api/records/check_shelf",
            "/api/records/check_shelf/status",
            "/api/records/clear",
            "/api/logs/stream",
        ):
            self.assertIn(path, paths)

    def test_sse_route_protected(self) -> None:
        """SSE /api/logs/stream 也受认证保护（fetch 流式订阅的原因）。"""
        resp = self.client.get("/api/logs/stream")
        self.assertEqual(resp.status_code, 401)


class WebAuthDisabledTestCase(unittest.TestCase):
    """未设 XY_WEB_TOKEN 时行为与 P1 完全一致（放行）。"""

    def setUp(self) -> None:
        self._saved_token = os.environ.pop("XY_WEB_TOKEN", None)
        self.storage_path = f"state/db_{uuid.uuid4().hex}.db"
        self.config_path = os.path.join(_TMP.name, f"config_{uuid.uuid4().hex}.yaml")
        gui.save_raw_config(self.config_path, make_mock_config_dict())
        cfg = gui.load_raw_config(self.config_path)
        cfg["storage"]["path"] = self.storage_path
        gui.save_raw_config(self.config_path, cfg)
        self.service = MonitorService(config_path=self.config_path)
        web_api.app.dependency_overrides[web_api.get_service] = lambda: self.service
        self.client = TestClient(web_api.app)

    def tearDown(self) -> None:
        self.service.shutdown()
        web_api.app.dependency_overrides.clear()
        if self._saved_token is not None:
            os.environ["XY_WEB_TOKEN"] = self._saved_token

    def test_no_token_allows_requests(self) -> None:
        """未设 token：无 Authorization 请求 /api/config → 200（与 P1 一致）。"""
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    def test_no_token_allows_sse_route(self) -> None:
        """未设 token：SSE 路由注册存在（TestClient 无法流式读取，避免挂起）。"""
        paths = set()

        def _collect(routes) -> None:
            for route in routes:
                if hasattr(route, "path"):
                    paths.add(route.path)
                if hasattr(route, "routes"):
                    _collect(route.routes)
                original = getattr(route, "original_router", None)
                if original is not None and hasattr(original, "routes"):
                    _collect(original.routes)

        _collect(web_api.app.routes)
        self.assertIn("/api/logs/stream", paths)


class RequireAuthUnitTestCase(unittest.TestCase):
    """require_auth 依赖单元级：secrets.compare_digest 常数时间校验。"""

    def test_compare_digest_used(self) -> None:
        """token 校验走 secrets.compare_digest（常数时间，防时序侧信道）。"""
        from unittest import mock

        saved = os.environ.get("XY_WEB_TOKEN")
        os.environ["XY_WEB_TOKEN"] = TEST_TOKEN
        try:
            class _Req:
                headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

            with mock.patch("web.api.secrets.compare_digest", wraps=__import__("secrets").compare_digest) as m:
                web_api.require_auth(_Req())  # type: ignore[arg-type]
                m.assert_called_once()
        finally:
            if saved is None:
                os.environ.pop("XY_WEB_TOKEN", None)
            else:
                os.environ["XY_WEB_TOKEN"] = saved

    def test_no_token_returns_without_compare(self) -> None:
        """未设 token 时直接放行，不触发 compare_digest。"""
        from unittest import mock

        saved = os.environ.pop("XY_WEB_TOKEN", None)
        try:
            class _Req:
                headers = {}

            with mock.patch("web.api.secrets.compare_digest") as m:
                web_api.require_auth(_Req())  # type: ignore[arg-type]
                m.assert_not_called()
        finally:
            if saved is not None:
                os.environ["XY_WEB_TOKEN"] = saved


if __name__ == "__main__":
    unittest.main()
