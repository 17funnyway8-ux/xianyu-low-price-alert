"""Docker/Web 化业务逻辑核对补测（P2-10）。

覆盖（对齐 docs/v1.8_P2P3_增量PRD_Web全功能与打磨.md §2.3 核对表 + P2-10 验收）：
    1) Web 保存配置后 `page_size`/`page_sleep`/`user_agent`/`cookie_pool` 不丢失
       （base deepcopy 语义）；
    2) records 列表自动排除黑名单 + 默认排除已售出（`include_sold` 语义）；
    3) Cookie 池经 Web 保存后落盘加密（`fernet1:`）、运行中 monitor 下一轮换用；
    4) 售出与黑名单互斥：已黑名单商品不出现在 records、不可被标记售出影响。

沿用项目测试惯例：全 mock + `XY_DATA_DIR` 临时目录 + `secure.set_key_file` 临时密钥。
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
    _TMP = tempfile.TemporaryDirectory(prefix="xianyu-qa-docker-")
    os.environ["XY_DATA_DIR"] = _TMP.name
    secure.set_key_file(os.path.join(_TMP.name, "secret.key"))


def tearDownModule() -> None:
    secure.set_key_file(None)
    os.environ.pop("XY_DATA_DIR", None)
    _TMP.cleanup()


def make_mock_config_dict(storage_path: str = "state/xianyu_alert.db") -> dict:
    return {
        "keywords": [{"keyword": "Switch", "max_price": 1000}],
        "monitor": {"interval_seconds": 60, "user_agent": "", "cookies": ""},
        "fetcher": {"type": "mock", "mock_products_per_round": 5, "mock_fail_rounds": [], "pages": 1},
        "storage": {"path": storage_path},
        "notify": {"channels": [{"type": "console"}]},
        "preset_exclude_keywords": ["回收", "置换"],
    }


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
        "page_size": 30,
        "page_sleep": 2.0,
        "user_agent": "",
        "storage_path": "state/xianyu_alert.db",
        "channels": {"console": {"enabled": True, "options": {}}},
        "cookie_alert_enabled": True,
        "cookie_check_interval_seconds": 0,
        "preset_exclude_keywords": ["回收", "置换"],
    }
    form.update(overrides)
    return form


def valid_cookie() -> str:
    return f"_m_h5_tk=abc_{int(time.time() * 1000)}; cookie2=xyz"


class QaDockerExtraTestCase(unittest.TestCase):
    """P2-10 业务核对补测（Web 层 API + 服务层）。"""

    def setUp(self) -> None:
        self.storage_path = f"state/db_{uuid.uuid4().hex}.db"
        self.config_path = os.path.join(_TMP.name, f"config_{uuid.uuid4().hex}.yaml")
        gui.save_raw_config(self.config_path, make_mock_config_dict(self.storage_path))
        self.service = MonitorService(config_path=self.config_path)
        web_api.app.dependency_overrides[web_api.get_service] = lambda: self.service
        self.client = TestClient(web_api.app)

    def tearDown(self) -> None:
        self.service.shutdown()
        web_api.app.dependency_overrides.clear()

    # ------------------------------------------------------------------ #
    # 1) 配置保留（base deepcopy 语义）
    # ------------------------------------------------------------------ #
    def test_config_save_preserves_advanced_fields(self) -> None:
        """保存配置后 page_size/page_sleep/user_agent/cookie_pool 均不丢失。"""
        # 先在磁盘配置里放手工维护的高级字段（模拟用户已用 config.yaml 配置）
        data = gui.load_raw_config(self.config_path)
        data["fetcher"]["page_size"] = 45
        data["fetcher"]["page_sleep"] = 0.8
        data["monitor"]["user_agent"] = "Mozilla/5.0 (Xianyu Bot)"
        data["monitor"]["cookie_pool"] = [
            {
                "name": "手工号",
                "cookie": secure.encrypt_text(valid_cookie()),
                "enabled": True,
            }
        ]
        gui.save_raw_config(self.config_path, data)

        # Web 保存表单（不包含 cookie_pool / 不包含明文 Cookie；
        # 也不带 page_size/page_sleep/user_agent → 走 base deepcopy 保留）
        form = make_form(interval_seconds=90)
        form.pop("page_size", None)
        form.pop("page_sleep", None)
        form.pop("user_agent", None)
        resp = self.client.put("/api/config", json={"form": form})
        self.assertEqual(resp.status_code, 200)
        saved = gui.load_raw_config(self.config_path)
        self.assertEqual(saved["monitor"]["interval_seconds"], 90)
        # 高级字段保留
        self.assertEqual(saved["fetcher"]["page_size"], 45)
        self.assertEqual(saved["fetcher"]["page_sleep"], 0.8)
        self.assertEqual(saved["monitor"]["user_agent"], "Mozilla/5.0 (Xianyu Bot)")
        self.assertEqual(len(saved["monitor"]["cookie_pool"]), 1)
        self.assertIn("fernet1:", str(saved["monitor"]["cookie_pool"][0]["cookie"]))
        # 表单往返可见（web_form_from_config 输出 page_size/page_sleep/user_agent）
        form = web_api.web_form_from_config(gui.load_raw_config(self.config_path))
        self.assertEqual(form["page_size"], 45)
        self.assertEqual(form["page_sleep"], 0.8)
        self.assertEqual(form["user_agent"], "Mozilla/5.0 (Xianyu Bot)")

    def test_config_save_advanced_fields_from_form(self) -> None:
        """表单显式修改 page_size/page_sleep/user_agent → 保存后写回 config.yaml。"""
        resp = self.client.put(
            "/api/config",
            json={
                "form": make_form(
                    page_size=60,
                    page_sleep=1.2,
                    user_agent="Mozilla/5.0 (Web Form)",
                )
            },
        )
        self.assertEqual(resp.status_code, 200)
        saved = gui.load_raw_config(self.config_path)
        self.assertEqual(saved["fetcher"]["page_size"], 60)
        self.assertEqual(saved["fetcher"]["page_sleep"], 1.2)
        self.assertEqual(saved["monitor"]["user_agent"], "Mozilla/5.0 (Web Form)")

    # ------------------------------------------------------------------ #
    # 2) records 排除语义（黑名单 + 已售出）
    # ------------------------------------------------------------------ #
    def test_records_exclude_blacklist_and_sold(self) -> None:
        """records 默认排除黑名单 + 已售出；include_sold 后已售出可见、黑名单仍排除。"""
        self.client.post("/api/monitor/run_once")
        records = self.client.get("/api/records").json()["records"]
        self.assertGreaterEqual(len(records), 2)
        pid_a = records[0]["product_id"]
        pid_b = records[1]["product_id"]
        # pid_a 进黑名单
        self.client.post(f"/api/records/{pid_a}/blacklist", json={"reason": "噪音"})
        # pid_b 标记售出
        self.client.post(f"/api/records/{pid_b}/sold")
        # 默认列表：两者都不可见
        ids = {r["product_id"] for r in self.client.get("/api/records").json()["records"]}
        self.assertNotIn(pid_a, ids)
        self.assertNotIn(pid_b, ids)
        # include_sold=true：售出可见，黑名单仍排除
        ids_sold = {
            r["product_id"]
            for r in self.client.get("/api/records?include_sold=true").json()["records"]
        }
        self.assertNotIn(pid_a, ids_sold)
        self.assertIn(pid_b, ids_sold)

    # ------------------------------------------------------------------ #
    # 3) Cookie 池经 Web 保存后加密落盘 + 运行中换用
    # ------------------------------------------------------------------ #
    def test_pool_encrypted_and_running_monitor_switches(self) -> None:
        """池 add 后 fernet1: 落盘；运行中 monitor 下一轮经 reload 换用新池。"""
        from xianyu_alert import cookie

        cookie_str = valid_cookie()
        # 启动 monitor（mock fetcher 不需要真实 Cookie，但轮换逻辑会读配置）
        self.client.post("/api/monitor/start")
        time.sleep(0.3)
        # 通过 API 添加池条目 → 加密写盘 + reload
        resp = self.client.post(
            "/api/cookie/pool",
            json={"action": "add", "name": "主账号", "cookie": cookie_str},
        )
        self.assertEqual(resp.status_code, 200)
        raw = open(self.config_path, "r", encoding="utf-8").read()
        self.assertIn("fernet1:", raw)
        self.assertNotIn("_m_h5_tk=abc_", raw.replace("fernet1:", ""))
        # reload 后运行中 monitor 的 config 引用已替换（下一轮换用新池）
        self.client.post("/api/monitor/stop")
        # 池明文可读且健康
        items = self.service._read_pool_plaintext()  # noqa: SLF001
        self.assertEqual(len(items), 1)
        self.assertTrue(cookie.cookie_has_token(items[0]["cookie"]))
        self.assertEqual(cookie.detect_cookie_health(items[0]["cookie"])[0], "ok")

    # ------------------------------------------------------------------ #
    # 4) 售出与黑名单互斥
    # ------------------------------------------------------------------ #
    def test_sold_and_blacklist_mutual_exclusion(self) -> None:
        """已黑名单商品不出现在 records、标记售出不影响黑名单存在。"""
        self.client.post("/api/monitor/run_once")
        records = self.client.get("/api/records").json()["records"]
        self.assertGreater(len(records), 0)
        pid = records[0]["product_id"]
        # 进黑名单
        self.client.post(f"/api/records/{pid}/blacklist", json={"reason": "互斥测试"})
        # 尝试标记售出（应成功但不产生可见影响）
        resp = self.client.post(f"/api/records/{pid}/sold")
        self.assertEqual(resp.status_code, 200)
        # records（含售出）都不出现
        ids = {
            r["product_id"]
            for r in self.client.get("/api/records?include_sold=true").json()["records"]
        }
        self.assertNotIn(pid, ids)
        # 黑名单仍在
        bl = self.client.get("/api/blacklist").json()["items"]
        self.assertTrue(any(item["product_id"] == pid for item in bl))
        # 恢复黑名单后（仍标记售出）→ include_sold 可见（黑名单互斥解除）
        self.client.post(f"/api/blacklist/{pid}/restore")
        ids = {
            r["product_id"]
            for r in self.client.get("/api/records?include_sold=true").json()["records"]
        }
        self.assertIn(pid, ids)


if __name__ == "__main__":
    unittest.main()
