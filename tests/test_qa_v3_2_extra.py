"""QA 独立补充边界测试（v3.2 增量）——严过关（Yan）独立验证。

补充视角（不与既有 test_gui_v3_2.py 重复）：
    1. cookie_pool 轮换的取模越界 / 负数轮次 / 重复名称 / 真实损坏密文跳过；
    2. detect_cookie_health 的 expiring 精确边界（恰 1 小时 vs 1 小时 +1ms）、
       真实损坏密文的 invalid_encrypt 分支（不 mock）；
    3. 提醒表排序：空表、中文/空值价格兜底、连续两次点击同列反序、
       iid 与 url 映射在排序后保持（双击不破）；
    4. 默认值：config 模块 dataclass 默认 600s / mtop；GUI 标题带版本号；
    5. 保存行为：mtop 无 Cookie 保存成功且 warning（完整 on_save_config）；
       Cookie 池落盘为密文；有池启用条目时不再 warning。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from xianyu_alert import secure  # noqa: E402


def setUpModule() -> None:
    """密钥文件隔离到临时目录（Fernet 真实加解密所需，避免污染项目根）。"""
    _tmp = tempfile.TemporaryDirectory(prefix="xianyu-qa32-")
    secure.set_key_file(os.path.join(_tmp.name, "secret.key"))
    globals()["_SECURE_TMP"] = _tmp


def tearDownModule() -> None:
    secure.set_key_file(None)
    _tmp = globals().get("_SECURE_TMP")
    if _tmp is not None:
        _tmp.cleanup()

from xianyu_alert.config import (  # noqa: E402
    CookiePoolItem,
    FetcherConfig,
    MonitorConfig,
    config_from_dict,
    serialize_cookie_pool,
)
from xianyu_alert.cookie import (  # noqa: E402
    HEALTH_EXPIRING,
    HEALTH_INVALID_ENCRYPT,
    HEALTH_NO_TOKEN,
    HEALTH_OK,
    TOKEN_EXPIRING_SOON_MS,
    TOKEN_TTL_MS,
    cookie_expiry_status,
    detect_cookie_health,
    resolve_cookie_for_round,
)
from xianyu_alert.fetcher import MockFetcher  # noqa: E402
from xianyu_alert.gui import (  # noqa: E402
    WINDOW_TITLE,
    build_config_dict,
    save_raw_config,
    sort_alert_rows,
)
from xianyu_alert.monitor import Monitor  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402

FUTURE_COOKIE = "_m_h5_tk=abc_9999999999999; cookie2=1"


def make_app_config(pool=None, cookies="", ftype="mtop") -> dict:
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
# 1. cookie_pool 轮换边界
# ---------------------------------------------------------------------- #
class TestCookiePoolRotationExtra(unittest.TestCase):
    """取模越界 / 负数轮次 / 重复名称 / 损坏密文跳过。"""

    def _cfg(self, pool_items, cookies="FALLBACK") -> MonitorConfig:
        cfg = MonitorConfig(interval_seconds=600, cookies=cookies)
        cfg.cookie_pool = [CookiePoolItem(**item) for item in pool_items]
        return cfg

    def test_modulo_overflow_round_index(self) -> None:
        """池 2 条：极大轮次号取模仍正确（1000001 % 2 = 1）。"""
        cfg = self._cfg(
            [
                {"name": "a", "cookie": "C_A", "enabled": True},
                {"name": "b", "cookie": "C_B", "enabled": True},
            ]
        )
        self.assertEqual(resolve_cookie_for_round(cfg, 1000001), "C_B")
        self.assertEqual(resolve_cookie_for_round(cfg, 1000000), "C_A")

    def test_negative_round_index_uses_python_modulo(self) -> None:
        """负数轮次：Python 取模 -1 % 2 = 1 → 取最后一条（不崩即可）。"""
        cfg = self._cfg(
            [
                {"name": "a", "cookie": "C_A", "enabled": True},
                {"name": "b", "cookie": "C_B", "enabled": True},
            ]
        )
        self.assertEqual(resolve_cookie_for_round(cfg, -1), "C_B")

    def test_single_enabled_pool_always_that_cookie(self) -> None:
        """池只有 1 条启用：任何轮次都取它，不回退单值。"""
        cfg = self._cfg(
            [{"name": "a", "cookie": "C_A", "enabled": True}],
            cookies="FALLBACK",
        )
        for i in range(5):
            self.assertEqual(resolve_cookie_for_round(cfg, i), "C_A")

    def test_broken_cipher_entry_skipped_parsing(self) -> None:
        """真实损坏密文（dpapi1:!!!!）→ 解密失败返回空 → 解析时跳过，不崩。"""
        raw = [
            {"name": "ok", "cookie": FUTURE_COOKIE, "enabled": True},
            {"name": "bad", "cookie": secure.PREFIX + "!!!!", "enabled": True},
        ]
        cfg = config_from_dict(make_app_config(pool=raw))
        names = [item.name for item in cfg.monitor.cookie_pool]
        self.assertEqual(names, ["ok"])

    def test_duplicate_name_keeps_first(self) -> None:
        """重复名称条目：保留首个，后续跳过。"""
        raw = [
            {"name": "同", "cookie": "C_A", "enabled": True},
            {"name": "同", "cookie": "C_B", "enabled": True},
        ]
        cfg = config_from_dict(make_app_config(pool=raw))
        self.assertEqual(len(cfg.monitor.cookie_pool), 1)
        self.assertEqual(cfg.monitor.cookie_pool[0].cookie, "C_A")

    def test_pool_all_disabled_monitor_injection(self) -> None:
        """Monitor 集成：池全 disabled → 每轮注入单值回退 Cookie。"""
        cfg = config_from_dict(
            make_app_config(
                pool=[{"name": "a", "cookie": "C_A", "enabled": False}],
                cookies="FALLBACK",
            )
        )
        used: list = []

        class RecordingFetcher(MockFetcher):
            def set_cookies(self, cookie_str: str) -> None:
                used.append(cookie_str)

        storage = Storage(":memory:")
        try:
            monitor = Monitor(cfg, RecordingFetcher(), storage, [])
            monitor.run_once()
            monitor.run_once()
        finally:
            storage.close()
        self.assertEqual(used, ["FALLBACK", "FALLBACK"])


# ---------------------------------------------------------------------- #
# 2. detect_cookie_health 边界
# ---------------------------------------------------------------------- #
class TestDetectHealthExtra(unittest.TestCase):
    """真实损坏密文 / expiring 精确边界 / 解密后 no_token。"""

    def test_invalid_encrypt_real_broken_cipher(self) -> None:
        """真实不可解密密文（不 mock）：invalid_encrypt。"""
        state, reason = detect_cookie_health(secure.PREFIX + "!!!!")
        self.assertEqual(state, HEALTH_INVALID_ENCRYPT)
        self.assertIn("解密", reason)

    def test_expiring_exact_one_hour_boundary(self) -> None:
        """剩余恰 1 小时（remain == 3600000）→ expiring（<= 边界，纯函数固定 now）。"""
        now_ms = 10 ** 13
        ts = now_ms - TOKEN_TTL_MS + TOKEN_EXPIRING_SOON_MS
        cookie = f"_m_h5_tk=abc_{ts}"
        status = cookie_expiry_status(cookie, now_ms=now_ms)
        self.assertEqual(status, "expiring")

    def test_expiring_just_over_one_hour_is_ok(self) -> None:
        """剩余 1 小时 +1ms（remain == 3600001）→ ok（严格大于才 ok，纯函数固定 now）。"""
        now_ms = 10 ** 13
        ts = now_ms - TOKEN_TTL_MS + TOKEN_EXPIRING_SOON_MS + 1
        cookie = f"_m_h5_tk=abc_{ts}"
        status = cookie_expiry_status(cookie, now_ms=now_ms)
        self.assertEqual(status, "ok")

    def test_detect_health_expiring_real_now(self) -> None:
        """detect_cookie_health 用系统当前时间：真实临期 Cookie → HEALTH_EXPIRING。"""
        now_ms = int(time.time() * 1000)
        ts = now_ms - TOKEN_TTL_MS + TOKEN_EXPIRING_SOON_MS // 2  # 剩半小时
        cookie = f"_m_h5_tk=abc_{ts}"
        state, reason = detect_cookie_health(cookie)
        self.assertEqual(state, HEALTH_EXPIRING)
        self.assertIn("即将过期", reason)

    def test_decrypted_no_token(self) -> None:
        """密文解密后仍缺 _m_h5_tk → no_token。"""
        cookie = f"_m_h5_tk_enc=xxx; cookie2=1"
        state, reason = detect_cookie_health(cookie)
        self.assertEqual(state, HEALTH_NO_TOKEN)
        self.assertIn("_m_h5_tk", reason)


# ---------------------------------------------------------------------- #
# 3. 提醒表排序边界
# ---------------------------------------------------------------------- #
class TestAlertSortExtra(unittest.TestCase):
    """空表 / 中文与空值兜底 / 连续反序 / iid 映射保持。"""

    def _rows(self) -> list:
        return [
            {"iid": "i1", "price": "¥1,299.00", "title": "A"},
            {"iid": "i2", "price": "面议", "title": "B"},
            {"iid": "i3", "price": "", "title": "C"},
            {"iid": "i4", "price": "电议", "title": "D"},
            {"iid": "i5", "price": "32", "title": "E"},
        ]

    def test_empty_table_no_crash(self) -> None:
        self.assertEqual(sort_alert_rows([], "price", True), [])
        self.assertEqual(sort_alert_rows([], "time", False), [])
        self.assertEqual(sort_alert_rows([], "keyword", True), [])

    def test_chinese_and_empty_price_fallback_last(self) -> None:
        """价格列含「面议/电议/空」：合法值按数值排，非法值恒排最后。"""
        asc = sort_alert_rows(self._rows(), "price", True)
        iids = [r["iid"] for r in asc]
        self.assertEqual(iids[0], "i5")        # 32 最小
        self.assertEqual(iids[1], "i1")        # 1299 次之
        self.assertEqual(set(iids[2:]), {"i2", "i3", "i4"})  # 非法恒最后

    def test_descending_keeps_invalid_last(self) -> None:
        desc = sort_alert_rows(self._rows(), "price", False)
        iids = [r["iid"] for r in desc]
        self.assertEqual(iids[0], "i1")
        self.assertEqual(iids[1], "i5")
        self.assertEqual(set(iids[2:]), {"i2", "i3", "i4"})

    def test_double_click_same_column_toggles(self) -> None:
        """模拟 GUI 状态机：第一次点击升序、再点同列反序。"""
        state_col, state_asc = "", True
        rows = self._rows()

        def click(col):
            nonlocal state_col, state_asc
            if state_col != col:
                state_col, state_asc = col, True
            else:
                state_asc = not state_asc
            return sort_alert_rows(rows, col, state_asc)

        first = [r["iid"] for r in click("price")]
        second = [r["iid"] for r in click("price")]
        self.assertEqual(first[0], "i5")
        self.assertEqual(second[0], "i1")
        self.assertNotEqual(first, second)

    def test_sort_preserves_iid_mapping_for_double_click(self) -> None:
        """排序只重排 iid 顺序、不丢 iid——URL 映射（双击打开）保持有效。"""
        url_map = {"i1": "u1", "i2": "u2", "i3": "u3", "i4": "u4", "i5": "u5"}
        sorted_rows = sort_alert_rows(self._rows(), "price", True)
        # 每个 iid 在排序后仍能映射到原 URL
        for row in sorted_rows:
            self.assertIn(row["iid"], url_map)
        self.assertEqual(set(r["iid"] for r in sorted_rows), set(url_map.keys()))


# ---------------------------------------------------------------------- #
# 4. 默认值
# ---------------------------------------------------------------------- #
class TestDefaultsExtra(unittest.TestCase):
    """config 模块级默认：interval 600 / fetcher mtop / GUI 标题。"""

    def test_monitor_config_default_interval_600(self) -> None:
        self.assertEqual(MonitorConfig().interval_seconds, 600)

    def test_fetcher_config_default_mtop(self) -> None:
        self.assertEqual(FetcherConfig().type, "mtop")

    def test_config_from_dict_empty_monitor_fetcher(self) -> None:
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

    def test_gui_window_title_contains_version(self) -> None:
        from xianyu_alert import __version__

        self.assertIn(f"v{__version__}", WINDOW_TITLE)


# ---------------------------------------------------------------------- #
# 5. 保存行为：mtop 无 Cookie 不拦截 + warning
# ---------------------------------------------------------------------- #
class TestSaveBehaviorExtra(unittest.TestCase):
    """完整 on_save_config：mtop 无 Cookie 保存成功且弹 warning（不拦截）。"""

    @classmethod
    def setUpClass(cls) -> None:
        import tkinter

        cls.root = tkinter.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _make_app(self, tmp: str, ftype: str = "mock", pool=None) -> tuple:
        from xianyu_alert.gui import XianyuAlertGUI, fetcher_label

        config_path = os.path.join(tmp, "config.yaml")
        save_raw_config(
            config_path,
            {
                "keywords": [{"keyword": "Switch", "max_price": 800}],
                "monitor": {"interval_seconds": 600, "cookies": ""},
                "fetcher": {"type": ftype},
                "storage": {"path": os.path.join(tmp, "t.db")},
                "notify": {"channels": [{"type": "console"}]},
            },
        )
        import tkinter

        root = tkinter.Toplevel(self.root)
        root.withdraw()
        app = XianyuAlertGUI(root, config_path=config_path)
        app.var_fetcher.set(fetcher_label("mtop"))
        if pool is not None:
            app._cookie_pool = pool
        return app, root, config_path

    def test_mtop_no_cookie_save_ok_and_warns(self) -> None:
        """保存成功（showinfo）+ mtop 无 Cookie → 弹 warning（不拦截）。"""
        from xianyu_alert.gui import XianyuAlertGUI

        with tempfile.TemporaryDirectory() as tmp:
            app, root, config_path = self._make_app(tmp, ftype="mock")
            try:
                with mock.patch.object(
                    XianyuAlertGUI, "_append_log"
                ) as m_log, mock.patch(
                    "xianyu_alert.gui.messagebox.showinfo"
                ) as m_info, mock.patch(
                    "xianyu_alert.gui.messagebox.showwarning"
                ) as m_warn:
                    app.on_save_config()
                self.assertEqual(m_info.call_count, 1, "保存成功提示应出现")
                self.assertGreaterEqual(m_warn.call_count, 1, "mtop 无 Cookie 应弹 warning")
                # 落盘文件确实存在且可解析
                with open(config_path, encoding="utf-8") as fp:
                    saved = yaml.safe_load(fp)
                self.assertEqual(saved["fetcher"]["type"], "mtop")
                self.assertEqual(saved["monitor"]["cookies"], "")
                config_from_dict(saved)  # 保存结果必须可校验
            finally:
                app._remove_log_handler()
                root.destroy()

    def test_mtop_with_pool_enabled_no_warning(self) -> None:
        """mtop + 池有启用条目 → 保存成功且不弹 Cookie warning。"""
        from xianyu_alert.gui import XianyuAlertGUI

        with tempfile.TemporaryDirectory() as tmp:
            app, root, config_path = self._make_app(
                tmp, ftype="mock", pool=[{"name": "主", "cookie": FUTURE_COOKIE, "enabled": True}]
            )
            try:
                with mock.patch.object(
                    XianyuAlertGUI, "_append_log"
                ), mock.patch(
                    "xianyu_alert.gui.messagebox.showinfo"
                ), mock.patch(
                    "xianyu_alert.gui.messagebox.showwarning"
                ) as m_warn:
                    app.on_save_config()
                self.assertEqual(m_warn.call_count, 0, "有池启用条目时不应弹 Cookie warning")
            finally:
                app._remove_log_handler()
                root.destroy()

    def test_cookie_pool_saved_as_cipher_on_disk(self) -> None:
        """Cookie 池落盘：YAML 中是 dpapi1: 密文，不是明文。"""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.yaml")
            data = build_config_dict(
                keywords=[("A", 1.0)],
                interval_seconds=600,
                fetcher_type="mtop",
                cookies="",
                storage_path="a.db",
                channels={"console": {"enabled": True, "options": {}}},
                cookie_pool=[{"name": "主", "cookie": FUTURE_COOKIE, "enabled": True}],
            )
            save_raw_config(config_path, data)
            with open(config_path, encoding="utf-8") as fp:
                raw_text = fp.read()
            saved = yaml.safe_load(raw_text)
            stored = saved["monitor"]["cookie_pool"][0]["cookie"]
            if secure.is_encrypted(secure.encrypt_text(FUTURE_COOKIE)):
                # Fernet 跨平台可用：必须是 fernet1: 密文，明文绝不落盘
                self.assertTrue(stored.startswith(secure.FERNET_PREFIX), "Cookie 池应以密文落盘")
                self.assertNotIn(FUTURE_COOKIE, raw_text, "明文 Cookie 不应出现在配置文件中")
            else:
                # 加密不可用降级明文（环境限制，记录行为）
                self.assertEqual(stored, FUTURE_COOKIE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
