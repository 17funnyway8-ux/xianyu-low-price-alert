"""单实例锁的异常与权限路径测试（v1.10.8，M11 补强）。

重评显示 `xianyu_alert/singleton.py` 覆盖率 80%，缺口集中在：
`acquire_instance_lock`(12) / `_probe_is_busy`(6) / `release`(5) / `_write_pid`(2) /
`lock_holder_pid`(2) / `_try_lock`(1) / `lock_diagnosis`(1)。

这些正是**NAS 上真实踩过的那类路径**：
    - 旧 root 版留下的锁文件 -> 非 root 进程 `PermissionError` -> 必须**放行**（不能因为锁文件让服务起不来）；
    - 真的被占用 -> 返回 None 并给出可操作诊断；
    - `strict=True` -> IO 异常直接抛，供测试/运维显式使用。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import singleton  # noqa: E402


class SingletonCase(unittest.TestCase):
    """公共夹具：临时锁路径 + 清理模块缓存。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "state", "instance.lock")
        self._saved = singleton._held_lock
        singleton._held_lock = None

    def tearDown(self) -> None:
        singleton.release_instance_lock(singleton._held_lock)
        singleton._held_lock = self._saved
        self._tmp.cleanup()


class TestAcquirePermissionError(SingletonCase):
    """旧 root 文件残留 -> PermissionError（NAS 上的真实场景）。"""

    def test_non_strict_lets_service_start(self) -> None:
        with mock.patch("xianyu_alert.singleton.os.open", side_effect=PermissionError("denied")):
            with self.assertLogs("xianyu_alert.singleton", level="WARNING") as logs:
                lock = singleton.acquire_instance_lock(self.path)
        self.assertIsNone(lock, "权限失败必须放行（返回 None 且不抛）")
        joined = " ".join(logs.output)
        self.assertIn("无法打开单实例锁文件", joined)
        self.assertIn("锁文件", joined, "日志必须带诊断，而不是只说失败")

    def test_strict_raises(self) -> None:
        with mock.patch("xianyu_alert.singleton.os.open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                singleton.acquire_instance_lock(self.path, strict=True)


class TestAcquireOtherOserror(SingletonCase):
    """其它 IO 异常。"""

    def test_non_strict_lets_service_start(self) -> None:
        with mock.patch("xianyu_alert.singleton.os.open", side_effect=OSError("disk full")):
            with self.assertLogs("xianyu_alert.singleton", level="WARNING") as logs:
                lock = singleton.acquire_instance_lock(self.path)
        self.assertIsNone(lock)
        self.assertIn("无法创建单实例锁文件", " ".join(logs.output))

    def test_strict_raises(self) -> None:
        with mock.patch("xianyu_alert.singleton.os.open", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                singleton.acquire_instance_lock(self.path, strict=True)


class TestAcquireOccupied(SingletonCase):
    """已被占用：返回 None + 可操作诊断。"""

    def test_blocking_lock_returns_none(self) -> None:
        with mock.patch("xianyu_alert.singleton._try_lock", side_effect=BlockingIOError("busy")):
            with self.assertLogs("xianyu_alert.singleton", level="INFO") as logs:
                lock = singleton.acquire_instance_lock(self.path)
        self.assertIsNone(lock)
        self.assertIn("被占用", " ".join(logs.output))

    def test_unexpected_lock_error_non_strict_passes(self) -> None:
        with mock.patch("xianyu_alert.singleton._try_lock", side_effect=RuntimeError("weird")):
            with self.assertLogs("xianyu_alert.singleton", level="WARNING") as logs:
                lock = singleton.acquire_instance_lock(self.path)
        self.assertIsNone(lock)
        self.assertIn("已放行", " ".join(logs.output))

    def test_unexpected_lock_error_strict_raises(self) -> None:
        with mock.patch("xianyu_alert.singleton._try_lock", side_effect=RuntimeError("weird")):
            with self.assertRaises(OSError):
                singleton.acquire_instance_lock(self.path, strict=True)


class TestProbeIsBusy(SingletonCase):
    """_probe_is_busy：只探测不持有。"""

    def test_open_failure_counts_as_busy(self) -> None:
        with mock.patch("xianyu_alert.singleton.os.open", side_effect=OSError("nope")):
            self.assertTrue(singleton._probe_is_busy(self.path), "探测失败保守视为运行中")

    def test_free_lock_returns_false_without_holding(self) -> None:
        self.assertFalse(singleton._probe_is_busy(self.path))
        self.assertIsNone(singleton._held_lock, "探测不得污染模块级持有缓存")

    def test_blocked_probe_returns_true(self) -> None:
        with mock.patch("xianyu_alert.singleton._try_lock", side_effect=BlockingIOError("busy")):
            self.assertTrue(singleton._probe_is_busy(self.path))

    def test_weird_error_counts_as_busy(self) -> None:
        with mock.patch("xianyu_alert.singleton._try_lock", side_effect=RuntimeError("weird")):
            self.assertTrue(singleton._probe_is_busy(self.path))


class TestReleaseAndHolder(SingletonCase):
    """释放与持有者查询。"""

    def test_release_is_idempotent(self) -> None:
        lock = singleton.acquire_instance_lock(self.path)
        self.assertIsNotNone(lock)
        singleton.release_instance_lock(lock)
        singleton.release_instance_lock(lock)
        self.assertIsNone(singleton._held_lock)

    def test_release_none_is_safe(self) -> None:
        singleton.release_instance_lock(None)  # 不应抛异常

    def test_acquire_then_release_clears_cache(self) -> None:
        lock = singleton.acquire_instance_lock(self.path)
        self.assertIs(singleton.acquire_instance_lock(self.path), lock, "同路径应返回缓存对象（幂等）")
        singleton.release_instance_lock(lock)
        self.assertIsNone(singleton._held_lock)

    def test_lock_holder_pid_missing_file(self) -> None:
        self.assertEqual(singleton.lock_holder_pid(self.path), "")

    def test_lock_holder_pid_falls_back_when_file_unreadable(self) -> None:
        """Windows：msvcrt 锁住的文件新句柄读不了（共享冲突）-> 回退到内存 PID。

        这个平台差异由 Windows CI 发现：此前会静默返回空串，冲突提示里只剩「PID 未知」。
        这里用 mock 在任何平台上模拟同样的 OSError。
        """
        lock = singleton.acquire_instance_lock(self.path)
        self.assertIsNotNone(lock)
        try:
            with mock.patch("builtins.open", side_effect=OSError("sharing violation")):
                self.assertEqual(singleton.lock_holder_pid(self.path), str(os.getpid()))
        finally:
            singleton.release_instance_lock(lock)

    def test_lock_holder_pid_after_acquire(self) -> None:
        lock = singleton.acquire_instance_lock(self.path)
        self.assertIsNotNone(lock)
        try:
            self.assertEqual(singleton.lock_holder_pid(self.path), str(os.getpid()))
        finally:
            singleton.release_instance_lock(lock)


if __name__ == "__main__":
    unittest.main()
