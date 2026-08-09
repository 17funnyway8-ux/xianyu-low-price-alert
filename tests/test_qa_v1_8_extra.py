"""QA 独立验证 v1.8 增量回归文件（历史惯例：覆盖本版本全部新行为）。

v1.8 主题：Cookie 自动刷新 + 进程单实例锁。
覆盖（A1~A8 汇总 + 全量回归）：
  1. cookie：pool_usable_cookies 健康过滤（expired/no_token 剔除、保序、
     pool_enabled_cookies 行为不变）；save_cookies_validated 拒绝保存 /
     _m_h5_tk=t 无时间戳兼容；resolve_cookie_for_round 增强（健康过滤 →
     单值兜底 → 空串）；
  2. storage：get_meta_value / set_meta_value / delete_meta_value（跨重启）；
  3. notifier：notify_message / safe_notify_message / notify_plain_message
     （6 子类各自实现）；
  4. monitor：_check_cookie_health_and_alert（mtop+过期 → 提醒、去抖状态跃迁、
     meta 表跨重启、mock no-op、节流、池汇总、脱敏无 Cookie 明文）；
  5. singleton：acquire/release/is_running/同进程幂等/冲突文案；
  6. GUI（offscreen）：一键刷新入口与 Cookie 管理「刷新选中」控件存在性；
  7. cli：login 拒绝保存无效 Cookie（退出码非 0、config 不变）；cookie status
     只检测不写入。

全 mock：MockFetcher / :memory: SQLite / unittest.mock.patch，不访问外网。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from xianyu_alert.config import CookiePoolItem, config_from_dict  # noqa: E402
from xianyu_alert.storage import (  # noqa: E402
    Storage,
    _META_COOKIE_ALERT_PREFIX,
    _META_COOKIE_POOL_ALERT_KEY,
)


def _spawn_lock_holder(path: str) -> subprocess.Popen:
    """启动真实子进程并让它持有锁（跨平台模拟「另一进程」）。

    POSIX flock 按「打开文件描述」互斥；Windows msvcrt 字节锁按**进程**归属
    （同进程第二 fd 可再次加锁）——因此「另一进程」必须用真实子进程。
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child_code = (
        "import sys, time\n"
        f"sys.path.insert(0, {project_root!r})\n"
        "from xianyu_alert import singleton\n"
        f"lock = singleton.acquire_instance_lock({path!r})\n"
        "if lock is None:\n"
        "    raise SystemExit(3)\n"
        "time.sleep(10)\n"
    )
    return subprocess.Popen([sys.executable, "-c", child_code])

#: 未来时间戳（有效）
OK_COOKIE = "_m_h5_tk=abc_9999999999999; c=1"
#: 无时间戳 → ok（历史样本兼容）
LEGACY_OK_COOKIE = "_m_h5_tk=t; c=1"
#: 2001 年时间戳 → 必然过期
EXPIRED_COOKIE = "_m_h5_tk=abc_1000000000000; c=1"
#: 缺 _m_h5_tk
NO_TOKEN_COOKIE = "cookie2=only"


def _read_yaml(path: str) -> dict:
    """读取 YAML（自动关闭文件）。"""
    with open(path, "r", encoding="utf-8") as fp:
        return yaml.safe_load(fp)


def make_config(**overrides: object) -> dict:
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


# ---------------------------------------------------------------------- #
# 1. cookie 纯函数
# ---------------------------------------------------------------------- #
class TestV18Cookie(unittest.TestCase):
    """pool_usable_cookies / save_cookies_validated / resolve_cookie_for_round。"""

    def _pool(self, items: list) -> list:
        return [CookiePoolItem(**item) for item in items]

    def test_pool_usable_filters_and_preserves_order(self) -> None:
        from xianyu_alert.cookie import pool_enabled_cookies, pool_usable_cookies

        pool = self._pool(
            [
                {"name": "exp", "cookie": EXPIRED_COOKIE, "enabled": True},
                {"name": "ok1", "cookie": OK_COOKIE, "enabled": True},
                {"name": "no_tk", "cookie": NO_TOKEN_COOKIE, "enabled": True},
                {"name": "ok2", "cookie": LEGACY_OK_COOKIE, "enabled": True},
                {"name": "dis", "cookie": OK_COOKIE, "enabled": False},
                {"name": "empty", "cookie": "", "enabled": True},
            ]
        )
        self.assertEqual(pool_usable_cookies(pool), [OK_COOKIE, LEGACY_OK_COOKIE])
        # pool_enabled_cookies 行为不变：只过滤启用/非空
        self.assertEqual(
            pool_enabled_cookies(pool),
            [EXPIRED_COOKIE, OK_COOKIE, NO_TOKEN_COOKIE, LEGACY_OK_COOKIE],
        )

    def test_save_cookies_validated_rejects_and_preserves(self) -> None:
        from xianyu_alert.cookie import save_cookies_validated

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as fp:
                yaml.safe_dump(make_config(), fp, allow_unicode=True, sort_keys=False)
            before = _read_yaml(path)

            for bad in (EXPIRED_COOKIE, NO_TOKEN_COOKIE, "   "):
                with self.assertRaises(ValueError):
                    save_cookies_validated(path, bad)
                self.assertEqual(
                    _read_yaml(path), before,
                    f"{bad[:20]} 不应落盘",
                )

            # 无时间戳 _m_h5_tk=t 兼容保存
            save_cookies_validated(path, LEGACY_OK_COOKIE)
            self.assertEqual(
                _read_yaml(path)["monitor"]["cookies"],
                LEGACY_OK_COOKIE,
            )

    def test_resolve_health_filtering(self) -> None:
        from xianyu_alert.cookie import resolve_cookie_for_round

        # 池混入 expired → 只用健康条目
        mon = SimpleNamespace(
            cookie_pool=self._pool(
                [
                    {"name": "exp", "cookie": EXPIRED_COOKIE, "enabled": True},
                    {"name": "ok", "cookie": OK_COOKIE, "enabled": True},
                ]
            ),
            cookies="",
        )
        self.assertEqual(resolve_cookie_for_round(mon, 0), OK_COOKIE)
        self.assertEqual(resolve_cookie_for_round(mon, 5), OK_COOKIE)

        # 池全部失效 → 单值健康兜底
        mon2 = SimpleNamespace(
            cookie_pool=self._pool([{"name": "exp", "cookie": EXPIRED_COOKIE, "enabled": True}]),
            cookies=LEGACY_OK_COOKIE,
        )
        self.assertEqual(resolve_cookie_for_round(mon2, 0), LEGACY_OK_COOKIE)

        # 池全部失效 + 单值也失效 → 空串
        mon3 = SimpleNamespace(
            cookie_pool=self._pool([{"name": "exp", "cookie": EXPIRED_COOKIE, "enabled": True}]),
            cookies=NO_TOKEN_COOKIE,
        )
        self.assertEqual(resolve_cookie_for_round(mon3, 0), "")


# ---------------------------------------------------------------------- #
# 2. storage meta
# ---------------------------------------------------------------------- #
class TestV18StorageMeta(unittest.TestCase):
    """get_meta_value / set_meta_value / delete_meta_value。"""

    def test_meta_roundtrip_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "meta.db")
            key = _META_COOKIE_ALERT_PREFIX + "abc123"
            with Storage(db) as st:
                self.assertIsNone(st.get_meta_value(key))
                st.set_meta_value(key, "expired")
                self.assertEqual(st.get_meta_value(key), "expired")
                st.set_meta_value(key, "ok")  # upsert
                self.assertEqual(st.get_meta_value(key), "ok")
            with Storage(db) as st2:
                self.assertEqual(st2.get_meta_value(key), "ok")
                st2.delete_meta_value(key)
                self.assertIsNone(st2.get_meta_value(key))
                st2.delete_meta_value(key)  # 幂等


# ---------------------------------------------------------------------- #
# 3. notifier 纯文本提醒
# ---------------------------------------------------------------------- #
class TestV18NotifierMessage(unittest.TestCase):
    """notify_message / safe_notify_message / notify_plain_message。"""

    def test_console_message_prints(self) -> None:
        from xianyu_alert.notifier import ConsoleNotifier

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ConsoleNotifier().notify_message("标题", "正文")
        self.assertIn("标题", buffer.getvalue())
        self.assertIn("正文", buffer.getvalue())

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_serverchan_message(self, mock_post: mock.MagicMock) -> None:
        from xianyu_alert.notifier import ServerChanNotifier

        mock_post.return_value = mock.Mock(status_code=200)
        ServerChanNotifier("K").notify_message("标题", "正文")
        self.assertEqual(mock_post.call_args.kwargs["data"]["title"], "标题")
        self.assertEqual(mock_post.call_args.kwargs["data"]["desp"], "正文")

    @mock.patch("xianyu_alert.notifier.smtplib.SMTP")
    def test_email_message(self, mock_smtp: mock.MagicMock) -> None:
        import base64
        from email.header import decode_header

        from xianyu_alert.notifier import EmailNotifier

        server = mock_smtp.return_value
        EmailNotifier("smtp.x.com", 587, "u@x.com", "p", "a@x.com").notify_message("标题", "正文")
        sent = server.sendmail.call_args[0][2]
        subject_line = next(line for line in sent.splitlines() if line.startswith("Subject:"))
        decoded = "".join(
            part.decode(charset or "utf-8") if isinstance(part, bytes) else part
            for part, charset in decode_header(subject_line.split(":", 1)[1].strip())
        )
        self.assertEqual(decoded, "标题")
        self.assertIn(base64.b64encode("正文".encode("utf-8")).decode("ascii"), sent)

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_telegram_and_webhook_message(self, mock_post: mock.MagicMock) -> None:
        from xianyu_alert.notifier import TelegramNotifier, WebhookNotifier

        mock_post.return_value = mock.Mock(status_code=200)
        TelegramNotifier("T", "1").notify_message("标题", "正文")
        self.assertEqual(mock_post.call_args.kwargs["data"]["text"], "标题\n\n正文")
        WebhookNotifier("https://x/h").notify_message("标题", "正文")
        payload = json.loads(mock_post.call_args.kwargs["data"])
        self.assertEqual(payload["text"]["content"], "标题\n\n正文")

    @mock.patch("xianyu_alert.notifier.requests.get")
    def test_bark_message(self, mock_get: mock.MagicMock) -> None:
        import urllib.parse

        from xianyu_alert.notifier import BarkNotifier

        mock_get.return_value = mock.Mock(status_code=200)
        BarkNotifier("https://api.day.app/K/").notify_message("标题", "正文")
        url = mock_get.call_args[0][0]
        self.assertIn("标题", urllib.parse.unquote(url))

    def test_safe_and_plain(self) -> None:
        from xianyu_alert.notifier import ConsoleNotifier, Notifier, notify_plain_message

        buffer = io.StringIO()
        # redirect 到 StringIO：Windows CI 管道 cp1252 无法编码中文，
        # 直接打到真实 stdout 会抛 UnicodeEncodeError → safe 返回 False
        with redirect_stdout(buffer):
            self.assertTrue(ConsoleNotifier().safe_notify_message("标题", "正文"))
        self.assertIn("标题", buffer.getvalue())

        received: list = []

        class Boom(Notifier):
            name = "boom"

            def notify(self, products: list) -> None:  # type: ignore[override]
                pass

            def notify_message(self, title: str, text: str) -> None:
                raise RuntimeError("boom")

        class Good(Notifier):
            name = "good"

            def notify(self, products: list) -> None:  # type: ignore[override]
                pass

            def notify_message(self, title: str, text: str) -> None:
                received.append((title, text))

        notify_plain_message([Boom(), Good()], "标题", "正文")  # 单个失败不影响其它
        self.assertEqual(received, [("标题", "正文")])
        notify_plain_message([], "标题", "正文")  # 空列表不抛


# ---------------------------------------------------------------------- #
# 4. monitor 健康检测 / 去抖
# ---------------------------------------------------------------------- #
class _MsgRecorder:
    """记录 notify_message 的简单替身。"""

    def __init__(self) -> None:
        self.messages: list = []

    def safe_notify_message(self, title: str, text: str) -> bool:
        self.messages.append((title, text))
        return True


class TestV18MonitorHealth(unittest.TestCase):
    """_check_cookie_health_and_alert：提醒 / 去抖 / 跨重启 / 节流 / mock no-op / 脱敏。"""

    def _monitor(self, cookie: str = "", pool: list = None, ftype: str = "mtop",
                 alert_enabled: bool = True, check_interval: int = 0, notifiers=None):
        cfg = config_from_dict(
            make_config(
                monitor={
                    "interval_seconds": 1,
                    "cookies": cookie,
                    "cookie_pool": pool or [],
                    "cookie_alert_enabled": alert_enabled,
                    "cookie_check_interval_seconds": check_interval,
                },
                fetcher={"type": ftype},
            )
        )
        from xianyu_alert.monitor import Monitor

        storage = Storage(":memory:")
        fetcher = mock.MagicMock()
        fetcher.fetch.return_value = []
        recorder = notifiers or [_MsgRecorder()]
        monitor = Monitor(cfg, fetcher, storage, recorder)
        return monitor, storage

    def test_mock_fetcher_noop(self) -> None:
        rec = _MsgRecorder()
        monitor, storage = self._monitor(cookie=EXPIRED_COOKIE, ftype="mock", notifiers=[rec])
        try:
            monitor.run_once()
            self.assertEqual(rec.messages, [])
        finally:
            storage.close()

    def test_expired_single_alerts_without_plaintext(self) -> None:
        rec = _MsgRecorder()
        monitor, storage = self._monitor(cookie=EXPIRED_COOKIE, notifiers=[rec])
        try:
            monitor.run_once()
            self.assertEqual(len(rec.messages), 1)
            title, text = rec.messages[0]
            self.assertEqual(title, "闲鱼 Cookie 已过期/无效")
            self.assertIn("刷新", text)
            self.assertNotIn(EXPIRED_COOKIE, text)  # C19：不含 Cookie 明文
        finally:
            storage.close()

    def test_debounce_transition_and_cross_restart(self) -> None:
        rec = _MsgRecorder()
        monitor, storage = self._monitor(cookie=EXPIRED_COOKIE, notifiers=[rec])
        key = _META_COOKIE_ALERT_PREFIX + hashlib.sha1(EXPIRED_COOKIE.encode()).hexdigest()[:12]
        try:
            monitor.run_once()
            self.assertEqual(len(rec.messages), 1)
            self.assertEqual(storage.get_meta_value(key), "expired")
            monitor.run_once()  # 同状态去抖
            self.assertEqual(len(rec.messages), 1)
            # 恢复 ok 后再失效 → 重新提醒
            storage.set_meta_value(key, "ok")
            monitor.run_once()
            self.assertEqual(len(rec.messages), 2)
        finally:
            storage.close()

        # 跨重启：新 Storage + 新 Monitor，状态仍在 → 去抖
        rec2 = _MsgRecorder()
        monitor2, storage2 = self._monitor(cookie=EXPIRED_COOKIE, notifiers=[rec2])
        try:
            storage2.set_meta_value(key, "expired")
            monitor2.run_once()
            self.assertEqual(rec2.messages, [])
        finally:
            storage2.close()

    def test_throttle_interval(self) -> None:
        rec = _MsgRecorder()
        monitor, storage = self._monitor(cookie=EXPIRED_COOKIE, check_interval=3600, notifiers=[rec])
        try:
            monitor.run_once()
            self.assertEqual(len(rec.messages), 1)
            monitor.config.monitor.cookies = "_m_h5_tk=def_1000000000000; c=2"
            monitor.run_once()  # 节流命中 → 不重复检测
            self.assertEqual(len(rec.messages), 1)
        finally:
            storage.close()

    def test_alert_disabled_switch(self) -> None:
        rec = _MsgRecorder()
        monitor, storage = self._monitor(cookie=EXPIRED_COOKIE, alert_enabled=False, notifiers=[rec])
        try:
            monitor.run_once()
            self.assertEqual(rec.messages, [])
        finally:
            storage.close()

    def test_pool_summary_alert(self) -> None:
        rec = _MsgRecorder()
        pool = [
            {"name": "bad", "cookie": EXPIRED_COOKIE, "enabled": True},
            {"name": "ok", "cookie": OK_COOKIE, "enabled": True},
        ]
        monitor, storage = self._monitor(cookie=OK_COOKIE, pool=pool, notifiers=[rec])
        try:
            monitor.run_once()
            combined = "\n".join(f"{t}\n{tx}" for t, tx in rec.messages)
            self.assertIn("池中有 1 条 Cookie 已过期", combined)
            self.assertTrue(storage.get_meta_value(_META_COOKIE_POOL_ALERT_KEY))
        finally:
            storage.close()


# ---------------------------------------------------------------------- #
# 5. singleton 单实例锁
# ---------------------------------------------------------------------- #
class TestV18Singleton(unittest.TestCase):
    """acquire / release / is_running / 幂等 / 冲突文案。"""

    def setUp(self) -> None:
        import xianyu_alert.singleton as s

        self.s = s
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "instance.lock")
        s._held_lock = None

    def tearDown(self) -> None:
        self.s.release_instance_lock(self.s._held_lock)
        self.s._held_lock = None
        self.tmp.cleanup()

    def test_acquire_release_is_running_idempotent(self) -> None:
        lock = self.s.acquire_instance_lock(self.path)
        self.assertIsNotNone(lock)
        self.assertIs(lock, self.s.acquire_instance_lock(self.path))  # 同进程幂等
        # 本进程持有锁（模块缓存命中）→ is_running 返回 True 且不破坏已持有的锁
        self.assertTrue(self.s.is_running(self.path))
        self.assertIs(lock, self.s._held_lock)  # 未被 is_running 释放
        self.s.release_instance_lock(lock)

        # 真实另一进程持有 → is_running 返回 True（跨平台；Windows 字节锁按
        # 进程归属，同进程第二 fd 探测会自锁成功 → 误判空闲，故用子进程模拟）
        proc = _spawn_lock_holder(self.path)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self.s.is_running(self.path):
                    break
                time.sleep(0.05)
            self.assertTrue(self.s.is_running(self.path), "子进程应已持有锁")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()

        # 子进程退出后锁由 OS 释放 → False（短暂轮询防释放时序竞态）
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.s.is_running(self.path):
            time.sleep(0.05)
        self.assertFalse(self.s.is_running(self.path))

    def test_cli_conflict_returns_2(self) -> None:
        from xianyu_alert import cli

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as fp:
                yaml.safe_dump(make_config(), fp, allow_unicode=True, sort_keys=False)
            stderr = io.StringIO()
            with mock.patch("xianyu_alert.cli.acquire_instance_lock", return_value=None), \
                 mock.patch("xianyu_alert.cli.lock_holder_pid", return_value="42"):
                with redirect_stderr(stderr):
                    code = cli.main(["once", "--config", path])
            self.assertEqual(code, 2)
            self.assertIn("已有实例运行中", stderr.getvalue())
            self.assertIn("42", stderr.getvalue())


# ---------------------------------------------------------------------- #
# 6. cli login 拒绝保存 + cookie status
# ---------------------------------------------------------------------- #
class TestV18Cli(unittest.TestCase):
    """login 校验保存 / cookie status 只检测不写入。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.yaml")
        with open(self.path, "w", encoding="utf-8") as fp:
            yaml.safe_dump(make_config(), fp, allow_unicode=True, sort_keys=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_login_rejects_invalid_cookie(self) -> None:
        from xianyu_alert import cli

        before = _read_yaml(self.path)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(["login", "--config", self.path, "--cookie-string", EXPIRED_COOKIE])
        self.assertNotEqual(code, 0)
        self.assertEqual(_read_yaml(self.path), before)

    def test_login_accepts_valid_cookie(self) -> None:
        from xianyu_alert import cli

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(["login", "--config", self.path, "--cookie-string", LEGACY_OK_COOKIE])
        self.assertEqual(code, 0)
        self.assertEqual(
            _read_yaml(self.path)["monitor"]["cookies"],
            LEGACY_OK_COOKIE,
        )
        self.assertIn("已写入", stdout.getvalue())

    def test_cookie_status_reads_only(self) -> None:
        from xianyu_alert import cli

        before = _read_yaml(self.path)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(["cookie", "status", "--config", self.path])
        self.assertEqual(code, 0)
        self.assertIn("单值 Cookie", stdout.getvalue())
        self.assertEqual(_read_yaml(self.path), before)


# ---------------------------------------------------------------------- #
# 7. GUI 控件存在性（offscreen / Tk 可用时）
# ---------------------------------------------------------------------- #
class TestV18GuiControls(unittest.TestCase):
    """一键刷新入口与 Cookie 管理「刷新选中」控件存在性（A6）。"""

    root = None

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import tkinter

            cls.root = tkinter.Tk()
            cls.root.withdraw()
        except Exception as exc:  # noqa: BLE001 - 无显示环境跳过
            raise unittest.SkipTest(f"无 GUI 显示环境：{exc}")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:  # noqa: BLE001
                pass

    def _make_app(self):
        from xianyu_alert.gui import XianyuAlertGUI, save_raw_config

        import tkinter

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            save_raw_config(
                path,
                make_config(
                    monitor={
                        "interval_seconds": 60,
                        "cookies": "",
                        "cookie_pool": [
                            {"name": "a", "cookie": OK_COOKIE, "enabled": True}
                        ],
                    }
                ),
            )
            root = tkinter.Toplevel(self.root)
            root.withdraw()
            return XianyuAlertGUI(root, config_path=path), root

    def test_refresh_button_and_manage_buttons(self) -> None:
        app, root = self._make_app()
        try:
            self.assertEqual(app.btn_refresh_cookie["text"], "🔄 一键刷新 Cookie")
            app.on_manage_cookies()
            from tkinter import ttk

            texts = set()

            def _walk(widget):
                try:
                    children = widget.winfo_children()
                except Exception:  # noqa: BLE001
                    return
                for child in children:
                    try:
                        if isinstance(child, ttk.Button):
                            texts.add(str(child["text"]))
                    except Exception:  # noqa: BLE001
                        pass
                    _walk(child)

            _walk(app.root)
            self.assertIn("🔄 刷新选中", texts)
            self.assertIn("⏹ 自动停用过期项", texts)
        finally:
            app._remove_log_handler()
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
