"""运维韧性测试：单实例锁诊断（M11）+ 通知静默/聚合/重试（M13）。

    M11  此前用户只看到一句"已有实例运行中/无法创建锁文件"，分不清是
         "真有实例在跑"还是"旧版本留下的残file" —— 现在给出可操作处置。
    M13  命中密集时会刷屏、夜间照发 —— 现在支持静默时段 + 聚合窗口 + 渠道级重试。

全部离线（临时目录 + 注入单调钟/墙钟），不碰网络、不依赖真实时间。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import singleton  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.notifier import Notifier  # noqa: E402
from xianyu_alert.notify_policy import (  # noqa: E402
    NotificationBuffer,
    NotificationPolicy,
    in_quiet_hours,
    parse_quiet_hours,
)


class TestLockDiagnosis(unittest.TestCase):
    """M11：锁状态的可操作提示。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "instance.lock")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_lock_is_reported_without_side_effect(self) -> None:
        """诊断函数不应创建文件（曾经因为探测用 O_CREAT 而误建）。"""
        text = singleton.lock_diagnosis(self.path)
        self.assertIn("不存在", text)
        self.assertFalse(os.path.exists(self.path), "诊断不应有写副作用")

    def test_stale_lock_is_recognised(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write("999999")
        self.assertTrue(singleton.is_lock_stale(self.path))
        self.assertIn("陈旧锁", singleton.lock_diagnosis(self.path))

    def test_live_lock_reports_holder_and_remedy(self) -> None:
        lock = singleton.acquire_instance_lock(self.path)
        self.assertIsNotNone(lock)
        try:
            self.assertFalse(singleton.is_lock_stale(self.path), "正被持有不算陈旧")
            other = singleton.acquire_instance_lock(self.path)
            # 同进程内 flock 语义因平台而异：这里只断言诊断文本可用
            text = singleton.lock_diagnosis(self.path)
            self.assertTrue(text)
            self.assertIn("锁文件", text)
            if other is not None:
                singleton.release_instance_lock(other)
        finally:
            singleton.release_instance_lock(lock)


class TestQuietHours(unittest.TestCase):
    """M13：静默时段解析与判定。"""

    def test_parse_valid(self) -> None:
        window = parse_quiet_hours("23:00-07:00")
        self.assertIsNotNone(window)

    def test_parse_invalid_and_empty(self) -> None:
        for bad in ("", "  ", "23:00", "25:00-07:00", "abc-def"):
            self.assertIsNone(parse_quiet_hours(bad), bad)

    def test_cross_midnight(self) -> None:
        window = parse_quiet_hours("23:00-07:00")
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 10, 23, 30), window))
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 10, 2, 0), window))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 10, 12, 0), window))

    def test_same_day_window(self) -> None:
        window = parse_quiet_hours("01:00-06:00")
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 10, 3, 0), window))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 10, 7, 0), window))

    def test_boundary_is_exclusive_at_end(self) -> None:
        window = parse_quiet_hours("23:00-07:00")
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 10, 23, 0), window), "起点含")
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 10, 7, 0), window), "终点不含")

    def test_none_window_never_quiet(self) -> None:
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 10, 3, 0), None))


class TestNotificationBuffer(unittest.TestCase):
    """M13：聚合窗口与静默的组合行为。"""

    def test_disabled_policy_is_immediately_due(self) -> None:
        buffer = NotificationBuffer(NotificationPolicy())
        buffer.add([1], mono=0.0)
        self.assertTrue(buffer.due(now=datetime(2026, 10, 10, 23, 0), mono=0.0))

    def test_aggregation_window_gates_delivery(self) -> None:
        policy = NotificationPolicy(aggregate_seconds=60)
        buffer = NotificationBuffer(policy)
        buffer.add(["a"], mono=100.0)
        self.assertFalse(buffer.due(mono=159.0), "窗口未到不发")
        self.assertTrue(buffer.due(mono=160.0), "满 60 秒到期")

    def test_quiet_hours_hold_even_after_window(self) -> None:
        policy = NotificationPolicy(quiet_hours="23:00-07:00", aggregate_seconds=1)
        buffer = NotificationBuffer(policy)
        buffer.add(["a"], mono=0.0)
        self.assertFalse(buffer.due(now=datetime(2026, 10, 10, 23, 30), mono=999.0))
        self.assertTrue(buffer.due(now=datetime(2026, 10, 10, 12, 0), mono=999.0), "离开静默期后放行")

    def test_flush_clears_buffer(self) -> None:
        policy = NotificationPolicy(aggregate_seconds=1)
        buffer = NotificationBuffer(policy)
        buffer.add(["a", "b"], mono=0.0)
        self.assertEqual(buffer.pending_count(), 2)
        self.assertEqual(buffer.flush_if_due(mono=5.0), ["a", "b"])
        self.assertEqual(buffer.pending_count(), 0)
        self.assertEqual(buffer.flush_if_due(mono=5.0), [])

    def test_empty_add_is_noop(self) -> None:
        buffer = NotificationBuffer(NotificationPolicy(aggregate_seconds=10))
        buffer.add([], mono=0.0)
        self.assertEqual(buffer.pending_count(), 0)

    def test_policy_enabled_flag(self) -> None:
        self.assertFalse(NotificationPolicy().enabled)
        self.assertTrue(NotificationPolicy(aggregate_seconds=10).enabled)
        self.assertTrue(NotificationPolicy(quiet_hours="23:00-07:00").enabled)
        self.assertFalse(NotificationPolicy(quiet_hours="非法").enabled)


class FlakyNotifier(Notifier):
    """前 N-1 次抛错、第 N 次成功，用于验证渠道级重试。"""

    name = "flaky"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def notify(self, products: list[Product]) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("模拟渠道故障")

    def notify_message(self, title: str, text: str) -> None:
        pass


class TestChannelRetry(unittest.TestCase):
    """M13：渠道级重试（次数由配置注入到实例，不改调用签名）。"""

    def _product(self) -> Product:
        return Product(product_id="123456789", title="t", price=1.0, url="u", keyword="k")

    def test_retry_then_success(self) -> None:
        notifier = FlakyNotifier(fail_times=2)
        notifier.retry_attempts = 3
        notifier.retry_backoff = 0.0
        self.assertTrue(notifier.safe_notify([self._product()]))
        self.assertEqual(notifier.calls, 3)

    def test_no_retry_when_attempts_is_one(self) -> None:
        notifier = FlakyNotifier(fail_times=5)
        notifier.retry_attempts = 1
        self.assertFalse(notifier.safe_notify([self._product()]))
        self.assertEqual(notifier.calls, 1)

    def test_empty_products_short_circuits(self) -> None:
        notifier = FlakyNotifier(fail_times=5)
        self.assertTrue(notifier.safe_notify([]))
        self.assertEqual(notifier.calls, 0)


class TestConfigFields(unittest.TestCase):
    """M13：配置字段解析（含脏数据容错）。"""

    def _config(self, notify: dict) -> object:
        from xianyu_alert.config import config_from_dict

        return config_from_dict(
            {
                "keywords": [{"keyword": "A", "max_price": 1}],
                "monitor": {"interval_seconds": 60},
                "notify": notify,
            }
        )

    def test_defaults(self) -> None:
        cfg = self._config({})
        self.assertEqual(cfg.notify.quiet_hours, "")
        self.assertEqual(cfg.notify.aggregate_seconds, 0)
        self.assertEqual(cfg.notify.retry_attempts, 1)

    def test_values_parsed(self) -> None:
        cfg = self._config({"quiet_hours": "23:00-07:00", "aggregate_seconds": 120, "retry_attempts": 3})
        self.assertEqual(cfg.notify.quiet_hours, "23:00-07:00")
        self.assertEqual(cfg.notify.aggregate_seconds, 120)
        self.assertEqual(cfg.notify.retry_attempts, 3)

    def test_dirty_values_fall_back(self) -> None:
        cfg = self._config({"quiet_hours": "非法", "aggregate_seconds": "x", "retry_attempts": 0})
        self.assertEqual(cfg.notify.quiet_hours, "", "非法静默时段应被忽略而不是阻断启动")
        self.assertEqual(cfg.notify.aggregate_seconds, 0)
        self.assertEqual(cfg.notify.retry_attempts, 1)


if __name__ == "__main__":
    unittest.main()


class TestKeyRotation(unittest.TestCase):
    """M08：密钥轮换与恢复指引。"""

    def setUp(self) -> None:
        from xianyu_alert import secure

        self.secure = secure
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.key_path = os.path.join(self.tmp, "secret.key")
        self.config_path = os.path.join(self.tmp, "config.yaml")
        self.original_key_file = secure._KEY_FILE
        secure.set_key_file(self.key_path)
        self.plain = "cookie2=abc; unb=918749838; _m_h5_tk=tok_1791559226130"

    def tearDown(self) -> None:
        self.secure.set_key_file(self.original_key_file)
        self._tmp.cleanup()

    def _write_config(self, cipher: str) -> None:
        import yaml

        data = {
            "keywords": [{"keyword": "Switch", "max_price": 1000}],
            "monitor": {
                "interval_seconds": 60,
                "cookies": cipher,
                "cookie_pool": [{"name": "小号", "cookie": cipher, "enabled": True}],
            },
        }
        with open(self.config_path, "w", encoding="utf-8") as fp:
            yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)

    def test_rotate_reencrypts_all_ciphertexts(self) -> None:
        import yaml

        cipher = self.secure.encrypt_text(self.plain)
        self._write_config(cipher)
        result = self.secure.rotate_key(self.config_path, key_path=self.key_path)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["rotated"], 2, "单值 Cookie + 池内 Cookie 都要重加密")

        self.secure.set_key_file(self.key_path)
        after = yaml.safe_load(open(self.config_path, encoding="utf-8"))
        new_cipher = after["monitor"]["cookies"]
        self.assertNotEqual(new_cipher, cipher, "密文必须变化（换钥）")
        self.secure.set_key_file(self.key_path)
        self.assertEqual(self.secure.decrypt_text(new_cipher), self.plain, "新密文应解出原文")
        self.assertEqual(
            self.secure.decrypt_text(after["monitor"]["cookie_pool"][0]["cookie"]), self.plain
        )

    def test_rotate_creates_backups(self) -> None:
        self.secure.encrypt_text(self.plain)  # 触发生成密钥文件
        self._write_config(self.secure.encrypt_text(self.plain))
        result = self.secure.rotate_key(self.config_path, key_path=self.key_path)
        self.assertTrue(os.path.isfile(result["key_backup"]), "旧密钥必须备份")
        self.assertTrue(os.path.isfile(result["config_backup"]), "旧配置必须备份")

    def test_rotate_without_ciphertext_is_noop(self) -> None:
        self._write_config("")
        result = self.secure.rotate_key(self.config_path, key_path=self.key_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["rotated"], 0)

    def test_rotate_aborts_on_undecryptable(self) -> None:
        """解不开的密文必须整体中止：绝不留下"一半新一半旧"的配置。"""

        self.secure.encrypt_text(self.plain)
        self._write_config(self.secure.FERNET_PREFIX + "不是合法密文")
        before = open(self.config_path, encoding="utf-8").read()
        result = self.secure.rotate_key(self.config_path, key_path=self.key_path)
        self.assertFalse(result["ok"])
        self.assertEqual(open(self.config_path, encoding="utf-8").read(), before, "配置不得被改动")
        self.assertIn("中止", result["message"])

    def test_rotate_missing_config(self) -> None:
        result = self.secure.rotate_key(os.path.join(self.tmp, "nope.yaml"), key_path=self.key_path)
        self.assertFalse(result["ok"])

    def test_key_status_and_recovery_hint(self) -> None:
        status = self.secure.key_status()
        self.assertIn("key_path", status)
        self.assertIn("hint", status)
        hint = self.secure.recovery_hint(key_exists=False)
        self.assertIn("密钥文件不存在", hint)
        self.assertIn("重新登录", hint, "必须给出可执行的下一步")

    def test_cli_secure_status_json(self) -> None:
        import io as _io
        import json as _json
        from contextlib import redirect_stdout

        from xianyu_alert import cli

        buf = _io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["secure", "status", "--json"])
        self.assertEqual(code, 0)
        payload = None
        for line in reversed(buf.getvalue().strip().splitlines()):
            if line.strip().startswith("{"):
                payload = _json.loads(line)
                break
        self.assertIsNotNone(payload)
        self.assertEqual(payload["action"], "status")
        self.assertIn("key_exists", payload)
