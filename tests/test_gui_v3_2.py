"""GUI v3.2 纯函数测试：默认值（600s / mtop）、web 隐藏、多 Cookie 池、
轮换逻辑、提醒表排序、更新日志、Cookie 有效性检测。

沿用 test_gui.py / test_gui_v3.py 的「抽纯函数测试」模式，**不真正显示窗口**。
gui.py 顶部对 tkinter 采用防御性导入：无图形环境也能 import 并跑纯函数测试。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import secure  # noqa: E402


def setUpModule() -> None:
    """密钥文件隔离到临时目录（Fernet 真实加解密所需，避免污染项目根）。"""
    _tmp = tempfile.TemporaryDirectory(prefix="xianyu-gui32-")
    secure.set_key_file(os.path.join(_tmp.name, "secret.key"))
    globals()["_SECURE_TMP"] = _tmp


def tearDownModule() -> None:
    secure.set_key_file(None)
    _tmp = globals().get("_SECURE_TMP")
    if _tmp is not None:
        _tmp.cleanup()

from xianyu_alert.config import (  # noqa: E402
    CookiePoolItem,
    MonitorConfig,
    config_from_dict,
    serialize_cookie_pool,
)
from xianyu_alert.cookie import (  # noqa: E402
    HEALTH_EXPIRED,
    HEALTH_EXPIRING,
    HEALTH_INVALID_ENCRYPT,
    HEALTH_MISSING,
    HEALTH_NO_TOKEN,
    HEALTH_OK,
    detect_cookie_health,
    pool_enabled_cookies,
    resolve_cookie_for_round,
)
from xianyu_alert.fetcher import MockFetcher  # noqa: E402
from xianyu_alert.gui import (  # noqa: E402
    ALERT_COLUMNS,
    DEFAULT_CONFIG_DICT,
    FETCHER_CHOICES,
    UPDATE_LOG,
    about_full_text,
    about_text,
    build_config_dict,
    config_to_form,
    fetcher_label,
    fetcher_type_from_label,
    sort_alert_rows,
)
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.notifier import Notifier  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402

#: 未来时间戳（有效）
FUTURE_COOKIE = "_m_h5_tk=abc_9999999999999; cookie2=1"
#: 2023 年签发（已过期）
EXPIRED_COOKIE = "_m_h5_tk=abc_1700000000000; cookie2=1"


def make_monitor_config(
    interval: int = 600,
    cookies: str = "",
    pool: list = None,
) -> MonitorConfig:
    """构造带可选 Cookie 池的 MonitorConfig。"""
    cfg = MonitorConfig(interval_seconds=interval, cookies=cookies)
    if pool:
        cfg.cookie_pool = [CookiePoolItem(**item) for item in pool]
    return cfg


def make_app_config(pool: list = None, cookies: str = "", ftype: str = "mtop") -> dict:
    """构造能通过 config_from_dict 校验的配置字典。"""
    data = {
        "keywords": [{"keyword": "Switch", "max_price": 800}],
        "monitor": {"interval_seconds": 600, "cookies": cookies},
        "fetcher": {"type": ftype},
        "storage": {"path": ":memory:"},
        "notify": {"channels": [{"type": "console"}]},
    }
    if pool:
        data["monitor"]["cookie_pool"] = pool
    return data


# ---------------------------------------------------------------------- #
# 1. v3.2 默认值：间隔 600s、抓取器 mtop
# ---------------------------------------------------------------------- #
class TestDefaultsV32(unittest.TestCase):
    """默认配置与表单默认值。"""

    def test_default_config_interval_600(self) -> None:
        self.assertEqual(DEFAULT_CONFIG_DICT["monitor"]["interval_seconds"], 600)

    def test_default_config_fetcher_mtop(self) -> None:
        self.assertEqual(DEFAULT_CONFIG_DICT["fetcher"]["type"], "mtop")

    def test_monitor_config_dataclass_default_600(self) -> None:
        self.assertEqual(MonitorConfig().interval_seconds, 600)

    def test_config_from_dict_defaults(self) -> None:
        """缺省字段按新默认：600s + mtop。"""
        cfg = config_from_dict(
            {
                "keywords": [{"keyword": "Switch", "max_price": 100}],
                "monitor": {},
                "fetcher": {},
                "storage": {"path": ":memory:"},
                "notify": {"channels": [{"type": "console"}]},
            }
        )
        self.assertEqual(cfg.monitor.interval_seconds, 600)
        self.assertEqual(cfg.fetcher.type, "mtop")

    def test_config_to_form_defaults(self) -> None:
        form = config_to_form({})
        self.assertEqual(form["interval"], 600)
        self.assertEqual(form["fetcher_type"], "mtop")

    def test_gui_default_config_dict_still_valid(self) -> None:
        cfg = config_from_dict(DEFAULT_CONFIG_DICT)
        self.assertEqual(cfg.monitor.interval_seconds, 600)
        self.assertEqual(cfg.fetcher.type, "mtop")


# ---------------------------------------------------------------------- #
# 2. FETCHER_CHOICES：web 隐藏、mtop 默认选中
# ---------------------------------------------------------------------- #
class TestFetcherChoicesV32(unittest.TestCase):
    """下拉框不再展示 web；mtop 为首项（默认选中）。"""

    def test_web_removed_from_choices(self) -> None:
        values = [value for value, _label in FETCHER_CHOICES]
        self.assertNotIn("web", values)

    def test_mtop_is_first_choice(self) -> None:
        self.assertEqual(FETCHER_CHOICES[0][0], "mtop")

    def test_mock_still_present_with_dev_note(self) -> None:
        labels = [label for _value, label in FETCHER_CHOICES]
        self.assertTrue(any("开发演示" in label for label in labels))

    def test_label_fallback_for_legacy_web(self) -> None:
        """旧配置为 web 时：界面回退为 mtop（保存后自动迁移）。"""
        self.assertEqual(fetcher_type_from_label(fetcher_label("web")), "mtop")

    def test_web_still_in_valid_fetcher_types(self) -> None:
        """代码层 web 仍保留（向后兼容），仅 GUI 不展示。"""
        from xianyu_alert.config import VALID_FETCHER_TYPES

        self.assertIn("web", VALID_FETCHER_TYPES)


# ---------------------------------------------------------------------- #
# 3. 多 Cookie 池：config 组装 / 解析往返（含密文）
# ---------------------------------------------------------------------- #
class TestCookiePoolConfig(unittest.TestCase):
    """cookie_pool 字段的序列化 / 反序列化往返。"""

    def test_build_config_dict_writes_pool(self) -> None:
        """GUI 保存：pool 写入 monitor.cookie_pool，且 cookie 为密文。"""
        data = build_config_dict(
            keywords=[("A", 1.0)],
            interval_seconds=600,
            fetcher_type="mtop",
            cookies="",
            storage_path="a.db",
            channels={"console": {"enabled": True, "options": {}}},
            cookie_pool=[
                {"name": "主账号", "cookie": FUTURE_COOKIE, "enabled": True},
                {"name": "小号1", "cookie": "cookie2=1", "enabled": False},
            ],
        )
        pool = data["monitor"]["cookie_pool"]
        self.assertEqual(len(pool), 2)
        self.assertEqual(pool[0]["name"], "主账号")
        # Fernet 跨平台可用 → 密文前缀必须是 fernet1:
        if secure.is_encrypted(secure.encrypt_text(FUTURE_COOKIE)):
            self.assertTrue(pool[0]["cookie"].startswith(secure.FERNET_PREFIX))
        self.assertFalse(pool[1]["enabled"])

    def test_config_to_form_reads_pool(self) -> None:
        """GUI 加载：pool 中的密文 cookie 自动解密为明文。"""
        cipher = secure.encrypt_text(FUTURE_COOKIE)
        with mock.patch.object(secure, "decrypt_text", return_value=FUTURE_COOKIE):
            form = config_to_form(
                {
                    "monitor": {
                        "cookie_pool": [
                            {"name": "主账号", "cookie": cipher, "enabled": True},
                        ]
                    }
                }
            )
        self.assertEqual(form["cookie_pool"], [{"name": "主账号", "cookie": FUTURE_COOKIE, "enabled": True}])

    def test_config_from_dict_parses_pool(self) -> None:
        """核心配置解析：monitor.cookie_pool → MonitorConfig.cookie_pool。"""
        cfg = config_from_dict(make_app_config(pool=[{"name": "主", "cookie": FUTURE_COOKIE}]))
        self.assertEqual(len(cfg.monitor.cookie_pool), 1)
        self.assertEqual(cfg.monitor.cookie_pool[0].name, "主")
        self.assertEqual(cfg.monitor.cookie_pool[0].cookie, FUTURE_COOKIE)
        self.assertTrue(cfg.monitor.cookie_pool[0].enabled)

    def test_pool_parse_skips_invalid_entries(self) -> None:
        """非法条目（缺字段 / 无法解密）被跳过，不阻断加载。"""
        raw = [
            {"name": "", "cookie": "x"},               # 缺名称
            {"name": "ok", "cookie": ""},              # 缺 cookie
            {"name": "bad", "cookie": secure.PREFIX + "AAAA"},  # 无法解密
            {"name": "good", "cookie": "cookie2=1; _m_h5_tk=t_1"},  # 合法
        ]
        with mock.patch.object(secure, "decrypt_text", return_value=""):
            cfg = config_from_dict(make_app_config(pool=raw))
        self.assertEqual([item.name for item in cfg.monitor.cookie_pool], ["good"])

    def test_roundtrip_serialize_parse(self) -> None:
        """serialize_cookie_pool -> config_from_dict -> 明文一致（加密往返）。"""
        pool = [{"name": "主账号", "cookie": FUTURE_COOKIE, "enabled": True}]
        serialized = serialize_cookie_pool(pool, encrypt=True)
        cfg = config_from_dict(make_app_config(pool=serialized))
        self.assertEqual(cfg.monitor.cookie_pool[0].cookie, FUTURE_COOKIE)
        self.assertEqual(cfg.monitor.cookie_pool[0].name, "主账号")

    def test_legacy_single_cookie_still_works(self) -> None:
        """无 cookie_pool 的旧配置：cookies 单值仍正常解析。"""
        cfg = config_from_dict(make_app_config(cookies=FUTURE_COOKIE))
        self.assertEqual(cfg.monitor.cookies, FUTURE_COOKIE)
        self.assertEqual(cfg.monitor.cookie_pool, [])


# ---------------------------------------------------------------------- #
# 4. 轮换逻辑：池优先、单值兜底
# ---------------------------------------------------------------------- #
class TestCookieRotation(unittest.TestCase):
    """多 Cookie 轮换与回退。"""

    def setUp(self) -> None:
        self.pool_cfg = make_monitor_config(
            cookies="_m_h5_tk=fb_9999999999999; c=1",
            pool=[
                {"name": "a", "cookie": "_m_h5_tk=ca_9999999999999; c=1", "enabled": True},
                {"name": "b", "cookie": "_m_h5_tk=cb_9999999999999; c=1", "enabled": True},
            ],
        )
        self.empty_pool_cfg = make_monitor_config(cookies="_m_h5_tk=fb_9999999999999; c=1", pool=[])

    def test_rotates_through_enabled_cookies(self) -> None:
        """2 个启用条目：round 0->A, 1->B, 2->A, 3->B。"""
        picks = [resolve_cookie_for_round(self.pool_cfg, i) for i in range(4)]
        self.assertEqual(picks, ["_m_h5_tk=ca_9999999999999; c=1", "_m_h5_tk=cb_9999999999999; c=1", "_m_h5_tk=ca_9999999999999; c=1", "_m_h5_tk=cb_9999999999999; c=1"])
    def test_pool_empty_falls_back_to_single(self) -> None:
        """池为空 → 始终回退单值 cookies。"""
        picks = [resolve_cookie_for_round(self.empty_pool_cfg, i) for i in range(3)]
        self.assertEqual(picks, ["_m_h5_tk=fb_9999999999999; c=1", "_m_h5_tk=fb_9999999999999; c=1", "_m_h5_tk=fb_9999999999999; c=1"])

    def test_disabled_cookies_excluded(self) -> None:
        """停用条目不参与轮换。"""
        cfg = make_monitor_config(
            cookies="_m_h5_tk=fb_9999999999999; c=1",
            pool=[
                {"name": "a", "cookie": "_m_h5_tk=ca_9999999999999; c=1", "enabled": True},
                {"name": "b", "cookie": "_m_h5_tk=cb_9999999999999; c=1", "enabled": False},
            ],
        )
        self.assertEqual(pool_enabled_cookies(cfg.cookie_pool), ["_m_h5_tk=ca_9999999999999; c=1"])
        self.assertEqual(resolve_cookie_for_round(cfg, 5), "_m_h5_tk=ca_9999999999999; c=1")

    def test_pool_all_disabled_falls_back(self) -> None:
        """池存在但全部停用 → 回退单值。"""
        cfg = make_monitor_config(
            cookies="_m_h5_tk=fb_9999999999999; c=1",
            pool=[{"name": "a", "cookie": "_m_h5_tk=ca_9999999999999; c=1", "enabled": False}],
        )
        self.assertEqual(resolve_cookie_for_round(cfg, 0), "_m_h5_tk=fb_9999999999999; c=1")


# ---------------------------------------------------------------------- #
# 5. Monitor 集成：轮换注入 fetcher
# ---------------------------------------------------------------------- #
class TestMonitorRotationIntegration(unittest.TestCase):
    """Monitor.run_once 每轮挑选 Cookie 并注入 fetcher.set_cookies。"""

    def test_run_once_rotates_cookie_each_round(self) -> None:
        cfg = config_from_dict(
            make_app_config(
                pool=[
                    {"name": "a", "cookie": "_m_h5_tk=ca_9999999999999; c=1", "enabled": True},
                    {"name": "b", "cookie": "_m_h5_tk=cb_9999999999999; c=1", "enabled": True},
                ]
            )
        )
        used: list = []

        class RecordingFetcher(MockFetcher):
            """记录 set_cookies 收到的 Cookie。"""

            def set_cookies(self, cookie_str: str) -> None:
                used.append(cookie_str)

        storage = Storage(":memory:")
        try:
            monitor = Monitor(cfg, RecordingFetcher(), storage, [])
            monitor.run_once()
            monitor.run_once()
            monitor.run_once()
        finally:
            storage.close()
        self.assertEqual(used, ["_m_h5_tk=ca_9999999999999; c=1", "_m_h5_tk=cb_9999999999999; c=1", "_m_h5_tk=ca_9999999999999; c=1"])

    def test_preflight_uses_pool_cookie(self) -> None:
        """preflight 检查池中第 0 条 Cookie（而不是单值空字段）。"""
        cfg = config_from_dict(
            make_app_config(pool=[{"name": "a", "cookie": EXPIRED_COOKIE}])
        )
        storage = Storage(":memory:")
        try:
            monitor = Monitor(cfg, MockFetcher(), storage, [])
            msg = monitor.preflight_cookie()
        finally:
            storage.close()
        self.assertIn("过期", msg)


# ---------------------------------------------------------------------- #
# 6. 提醒记录表排序
# ---------------------------------------------------------------------- #
class TestAlertSorting(unittest.TestCase):
    """价格数值排序 / 发布时间排序 / 反序切换 / 非法价格兜底。"""

    def _rows(self) -> list:
        return [
            {"iid": "a", "time": "2024-02-01 09:00:00", "keyword": "Switch", "title": "A", "price": "¥1000.00", "publish": "2024-01-03 10:00:00"},
            {"iid": "b", "time": "2024-02-01 09:00:00", "keyword": "Switch", "title": "B", "price": "¥99.50", "publish": "2024-01-01 10:00:00"},
            {"iid": "c", "time": "2024-02-01 09:00:00", "keyword": "Switch", "title": "C", "price": "面议", "publish": "2024-01-02 10:00:00"},
        ]

    def test_price_ascending(self) -> None:
        result = sort_alert_rows(self._rows(), "price", ascending=True)
        self.assertEqual([r["iid"] for r in result], ["b", "a", "c"])

    def test_price_descending_invalid_last(self) -> None:
        """降序：合法价格从高到低，非法价格（面议）仍排最后。"""
        result = sort_alert_rows(self._rows(), "price", ascending=False)
        self.assertEqual([r["iid"] for r in result], ["a", "b", "c"])

    def test_publish_string_sort(self) -> None:
        result = sort_alert_rows(self._rows(), "publish", ascending=True)
        self.assertEqual([r["iid"] for r in result], ["b", "c", "a"])

    def test_publish_descending(self) -> None:
        result = sort_alert_rows(self._rows(), "publish", ascending=False)
        self.assertEqual([r["iid"] for r in result], ["a", "c", "b"])

    def test_time_string_sort(self) -> None:
        result = sort_alert_rows(self._rows(), "time", ascending=False)
        # time 全部相同 → 稳定排序保持原序
        self.assertEqual([r["iid"] for r in result], ["a", "b", "c"])

    def test_reverse_toggle_flips_order(self) -> None:
        rows = self._rows()
        asc = sort_alert_rows(rows, "price", ascending=True)
        desc = sort_alert_rows(rows, "price", ascending=False)
        self.assertNotEqual([r["iid"] for r in asc], [r["iid"] for r in desc])

    def test_does_not_mutate_input(self) -> None:
        rows = self._rows()
        original = list(rows)
        sort_alert_rows(rows, "price")
        self.assertEqual(rows, original)

    def test_invalid_column_returns_copy(self) -> None:
        rows = self._rows()
        self.assertEqual(sort_alert_rows(rows, "不存在"), rows)
        self.assertIsNot(sort_alert_rows(rows, "不存在"), rows)


# ---------------------------------------------------------------------- #
# 7. 更新日志
# ---------------------------------------------------------------------- #
class TestUpdateLog(unittest.TestCase):
    """UPDATE_LOG 常量与「关于」完整文案。"""

    def test_update_log_contains_v130(self) -> None:
        self.assertIn("v1.3.0", UPDATE_LOG)
        self.assertIn("v1.0.0", UPDATE_LOG)
        self.assertIn("多 Cookie 管理", UPDATE_LOG)

    def test_about_full_text_contains_history(self) -> None:
        text = about_full_text()
        self.assertIn("v1.3.0", text)
        self.assertIn("版本历史", text)
        self.assertIn("免责声明", text)

    def test_about_text_version(self) -> None:
        from xianyu_alert import __version__

        self.assertIn(f"v{__version__}", about_text())


# ---------------------------------------------------------------------- #
# 8. detect_cookie_health 各状态分支
# ---------------------------------------------------------------------- #
class TestDetectCookieHealth(unittest.TestCase):
    """有效性检测六种状态。"""

    def test_ok(self) -> None:
        state, reason = detect_cookie_health(FUTURE_COOKIE)
        self.assertEqual(state, HEALTH_OK)
        self.assertIn("有效", reason)

    def test_expired(self) -> None:
        state, reason = detect_cookie_health(EXPIRED_COOKIE)
        self.assertEqual(state, HEALTH_EXPIRED)
        self.assertIn("过期", reason)

    def test_expiring(self) -> None:
        now = int(time.time() * 1000)
        expiring = f"_m_h5_tk=abc_{now - 23 * 3600 * 1000 - 30 * 60 * 1000}"
        state, reason = detect_cookie_health(expiring)
        self.assertEqual(state, HEALTH_EXPIRING)
        self.assertIn("即将过期", reason)

    def test_no_token(self) -> None:
        state, reason = detect_cookie_health("cookie2=1; unb=2")
        self.assertEqual(state, HEALTH_NO_TOKEN)
        self.assertIn("_m_h5_tk", reason)

    def test_missing(self) -> None:
        for value in ("", "  ", None):
            state, reason = detect_cookie_health(value)
            self.assertEqual(state, HEALTH_MISSING)
            self.assertIn("未配置", reason)

    def test_invalid_encrypt(self) -> None:
        with mock.patch.object(secure, "decrypt_text", return_value=""):
            state, reason = detect_cookie_health(secure.PREFIX + "AAAA")
        self.assertEqual(state, HEALTH_INVALID_ENCRYPT)
        self.assertIn("解密", reason)

    def test_encrypted_ok_after_decrypt(self) -> None:
        with mock.patch.object(secure, "decrypt_text", return_value=FUTURE_COOKIE):
            state, _reason = detect_cookie_health(secure.PREFIX + "Zm9v")
        self.assertEqual(state, HEALTH_OK)

    def test_legacy_short_timestamp_is_ok(self) -> None:
        """历史样本 `_m_h5_tk=deadbeef_170000`（6 位后缀）按有效处理。"""
        state, _reason = detect_cookie_health("cookie2=abc; _m_h5_tk=deadbeef_170000; x=1")
        self.assertEqual(state, HEALTH_OK)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
