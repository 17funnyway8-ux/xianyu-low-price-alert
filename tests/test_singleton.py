"""singleton 单实例锁单元测试（A9/A10/A11/A12）。

覆盖：
    - 首次 acquire 成功；二次 acquire（另一 fd / 模拟另一进程）失败且不抛未处理异常；
    - 同进程重复 acquire 幂等返回同一对象；release 后可重新 acquire；
    - 模拟崩溃（不 release 直接丢弃 fd）后新 acquire 立即成功（OS 自动释放）；
    - mock 已持有锁时：cli run/once 返回 2 且 stderr 含中文提示；
      gui.main / gui_qt.main 返回非 0 且产生中文提示（offscreen）；
    - mock sys.platform / patch fcntl、msvcrt 缺失场景；is_running 只检测不持有。

全部使用临时目录锁文件，不触碰项目真实 state/。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import singleton  # noqa: E402


def _spawn_lock_holder(lock_path: str) -> subprocess.Popen:
    """启动真实子进程并让它持有锁（跨平台模拟「另一进程」）。

    POSIX flock 按「打开文件描述」互斥（同进程第二 fd 会冲突），而 Windows
    msvcrt 字节锁按**进程**归属（同进程第二 fd 可再次加锁）——因此「另一进程」
    必须用真实子进程，才能跨平台验证冲突语义。

    Args:
        lock_path: 子进程要持有的锁文件路径。

    Returns:
        子进程对象；调用方负责 terminate/wait 清理。
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child_code = (
        "import sys, time\n"
        f"sys.path.insert(0, {project_root!r})\n"
        "from xianyu_alert import singleton\n"
        f"lock = singleton.acquire_instance_lock({lock_path!r})\n"
        "if lock is None:\n"
        "    raise SystemExit(3)\n"
        "time.sleep(10)\n"
    )
    return subprocess.Popen([sys.executable, "-c", child_code])


def _wait_lock_held(lock_path: str, timeout: float = 5.0) -> None:
    """轮询直到另一进程真正持有锁（或超时抛错）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if singleton.is_running(lock_path):
            return
        time.sleep(0.05)
    raise AssertionError(f"等待子进程持有锁超时：{lock_path}")


def _terminate_lock_holder(proc: subprocess.Popen) -> None:
    """终止锁持有子进程（OS 自动释放锁）。"""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 - 强制终止兜底
        proc.kill()


class TestInstanceLock(unittest.TestCase):
    """锁行为核心测试（临时锁文件）。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.lock_path = os.path.join(self.tmpdir.name, "instance.lock")
        # 每个用例前清空模块级缓存，避免跨用例串锁
        singleton._held_lock = None

    def tearDown(self) -> None:
        singleton.release_instance_lock(singleton._held_lock)
        singleton._held_lock = None
        self.tmpdir.cleanup()

    def test_first_acquire_succeeds(self) -> None:
        """首次 acquire 成功并写入锁文件。"""
        lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock)
        assert lock is not None
        self.assertEqual(lock.pid, os.getpid())
        self.assertGreater(lock.fd, 0)
        # 锁文件已写入当前 PID（Windows 上 msvcrt 锁区不允许第二句柄读取，
        # 因此用持有锁的同一 fd 读取，跨平台一致）
        os.lseek(lock.fd, 0, os.SEEK_SET)
        self.assertEqual(os.read(lock.fd, 64).decode("utf-8").strip(), str(os.getpid()))

    def test_second_acquire_conflict_returns_none(self) -> None:
        """真实另一进程持有锁 → 本进程 acquire 返回 None 且不抛异常。

        跨平台语义：POSIX flock 按「打开文件描述」互斥（同进程第二 fd 也会冲突）；
        Windows msvcrt 字节锁按**进程**归属（同进程第二 fd 可再次加锁，CI 实测）。
        因此「另一进程」必须用真实子进程，才能跨平台验证冲突。
        """
        proc = _spawn_lock_holder(self.lock_path)
        try:
            _wait_lock_held(self.lock_path)
            # 本进程 acquire → None（跨进程冲突，不抛异常）
            self.assertIsNone(singleton.acquire_instance_lock(self.lock_path))
        finally:
            _terminate_lock_holder(proc)

    def test_same_process_acquire_idempotent(self) -> None:
        """同进程重复 acquire 返回同一对象（L4，不自锁）。"""
        first = singleton.acquire_instance_lock(self.lock_path)
        second = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(first)
        self.assertIs(first, second)

    def test_release_then_reacquire_succeeds(self) -> None:
        """release 后可重新 acquire。"""
        first = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(first)
        singleton.release_instance_lock(first)
        second = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(second)
        singleton.release_instance_lock(second)

    def test_release_is_idempotent(self) -> None:
        """release 重复调用安全（None / 已释放均不抛）。"""
        singleton.release_instance_lock(None)
        lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock)
        singleton.release_instance_lock(lock)
        singleton.release_instance_lock(lock)

    def test_crash_drop_fd_then_reacquire(self) -> None:
        """模拟进程崩溃（不 release 直接丢弃 fd）→ 新 acquire 立即成功（A10）。"""
        lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock)
        assert lock is not None
        # 崩溃：直接关闭 fd，不调用 release（OS 在 fd 关闭时自动释放锁）
        os.close(lock.fd)
        lock.fd = -1
        singleton._held_lock = None  # 新进程无模块缓存

        new_lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(new_lock, "崩溃后应无需人工删锁即可重新获取")
        singleton.release_instance_lock(new_lock)

    def test_lock_holder_pid(self) -> None:
        """锁文件中的 PID 可读（冲突提示用）。"""
        lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock)
        # Windows 上 msvcrt 锁区不允许第二句柄读取（lock_holder_pid 返回 ""），
        # 因此持有期间用同一 fd 读取；release 后再用 lock_holder_pid 验证磁盘读取路径。
        os.lseek(lock.fd, 0, os.SEEK_SET)
        self.assertEqual(os.read(lock.fd, 64).decode("utf-8").strip(), str(os.getpid()))
        singleton.release_instance_lock(lock)
        # release 后文件内容保留最近持有者 PID（尽力而为）
        self.assertEqual(singleton.lock_holder_pid(self.lock_path), str(os.getpid()))

    def test_is_running_detects_only(self) -> None:
        """is_running 只检测不持有：无实例返回 False，有实例返回 True。"""
        # 无实例 → False
        self.assertFalse(singleton.is_running(self.lock_path))

        # 本进程持有（模块缓存命中）→ True（跨平台：缓存命中直接返回，不探测）
        lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock)
        self.assertTrue(singleton.is_running(self.lock_path))
        singleton.release_instance_lock(lock)

        # 真实另一进程持有 → True（跨平台；Windows 字节锁按进程归属，
        # 不能用同进程第二 fd 探测模拟另一进程）
        proc = _spawn_lock_holder(self.lock_path)
        try:
            _wait_lock_held(self.lock_path)
            self.assertTrue(singleton.is_running(self.lock_path))
        finally:
            _terminate_lock_holder(proc)

        # 子进程退出后锁由 OS 释放 → False（短暂轮询防释放时序竞态）
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and singleton.is_running(self.lock_path):
            time.sleep(0.05)
        self.assertFalse(singleton.is_running(self.lock_path))

    def test_is_running_other_path_does_not_touch_held_lock(self) -> None:
        """QA 观察项 1：持有 A 锁时 is_running(不同路径 B) 不误释放 A。"""
        other_path = os.path.join(self.tmpdir.name, "other.lock")
        lock_a = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock_a)

        # B 空闲 → is_running(B) 返回 False，且 A 仍被持有
        self.assertFalse(singleton.is_running(other_path))
        self.assertIs(singleton._held_lock, lock_a)

        # B 被「真实另一进程」占用 → is_running(B) 返回 True，且 A 仍被持有
        # （Windows 字节锁按进程归属，同进程第二 fd 会自锁成功，故用子进程模拟）
        proc = _spawn_lock_holder(other_path)
        try:
            _wait_lock_held(other_path)
            self.assertTrue(singleton.is_running(other_path))
            self.assertIs(singleton._held_lock, lock_a)  # A 未被误释放
        finally:
            _terminate_lock_holder(proc)
        singleton.release_instance_lock(lock_a)

    def test_acquire_other_path_returns_new_lock(self) -> None:
        """QA 观察项 1：持有 A 时 acquire(不同路径 B) 返回新锁，而非 A。"""
        other_path = os.path.join(self.tmpdir.name, "other2.lock")
        lock_a = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock_a)
        lock_b = singleton.acquire_instance_lock(other_path)
        self.assertIsNotNone(lock_b)
        self.assertIsNot(lock_b, lock_a)          # 新锁
        self.assertIs(singleton._held_lock, lock_b)  # 缓存指向 B（单缓存槽）
        # 分别释放：A 的 fd 关闭、B 释放后缓存清空
        singleton.release_instance_lock(lock_a)
        self.assertIs(singleton._held_lock, lock_b)
        singleton.release_instance_lock(lock_b)
        self.assertIsNone(singleton._held_lock)

    def test_default_lock_path_under_state(self) -> None:
        """默认锁路径 = data_dir()/state/instance.lock（共享知识 1）。"""
        lock = singleton.acquire_instance_lock()
        self.assertIsNotNone(lock)
        assert lock is not None
        from xianyu_alert import paths

        expected = os.path.join(paths.default_state_dir(), singleton.LOCK_FILE_NAME)
        self.assertEqual(lock.lock_path, expected)
        singleton.release_instance_lock(lock)


class TestPlatformBranches(unittest.TestCase):
    """平台分支：mock sys.platform / fcntl / msvcrt 缺失场景（A12）。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.lock_path = os.path.join(self.tmpdir.name, "instance.lock")
        singleton._held_lock = None

    def tearDown(self) -> None:
        singleton.release_instance_lock(singleton._held_lock)
        singleton._held_lock = None
        self.tmpdir.cleanup()

    def test_windows_branch_uses_msvcrt(self) -> None:
        """sys.platform == win32 时走 msvcrt.locking（LK_NBLCK）。"""
        fake_msvcrt = mock.MagicMock()
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(singleton, "msvcrt", fake_msvcrt):
            lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock)
        self.assertTrue(fake_msvcrt.locking.called)
        singleton.release_instance_lock(lock)

    def test_windows_branch_oserror_conflict(self) -> None:
        """Windows 下 msvcrt.locking 抛 OSError → 返回 None（占用）。"""
        fake_msvcrt = mock.MagicMock()
        fake_msvcrt.locking.side_effect = OSError(13, "denied")
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(singleton, "msvcrt", fake_msvcrt):
            lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNone(lock)

    def test_posix_branch_uses_flock(self) -> None:
        """POSIX 走 fcntl.flock（LOCK_EX | LOCK_NB）。

        注意：`_is_windows()` 同时检查 `sys.platform` 与 `os.name`——真实 Windows 上
        `os.name == "nt"` 恒为真，仅 mock sys.platform 不足以切到 POSIX 分支，
        必须连 `os.name` 一起 mock（否则会走 msvcrt 分支、flock 未被调用）。
        """
        fake_fcntl = mock.MagicMock()
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch("os.name", "posix"), \
             mock.patch.object(singleton, "fcntl", fake_fcntl):
            lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock)
        self.assertTrue(fake_fcntl.flock.called)
        singleton.release_instance_lock(lock)

    def test_posix_branch_blocking_error_conflict(self) -> None:
        """POSIX 下 flock 抛 BlockingIOError → 返回 None（占用）。"""
        fake_fcntl = mock.MagicMock()
        fake_fcntl.flock.side_effect = BlockingIOError(11, "EAGAIN")
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch("os.name", "posix"), \
             mock.patch.object(singleton, "fcntl", fake_fcntl):
            lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNone(lock)

    def test_fcntl_missing_falls_back(self) -> None:
        """fcntl 缺失（Windows）时不抛异常：win32 分支不触碰 fcntl。"""
        fake_msvcrt = mock.MagicMock()
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(singleton, "fcntl", None), \
             mock.patch.object(singleton, "msvcrt", fake_msvcrt):
            lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNotNone(lock)  # msvcrt 可用 → Windows 分支正常加锁
        self.assertTrue(fake_msvcrt.locking.called)
        singleton.release_instance_lock(lock)

    def test_msvcrt_missing_returns_none(self) -> None:
        """msvcrt 缺失（win32 分支无实现）→ 返回 None 不抛异常。"""
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(singleton, "msvcrt", None):
            lock = singleton.acquire_instance_lock(self.lock_path)
        self.assertIsNone(lock)

    def test_io_error_strict_raises(self) -> None:
        """strict=True 时 IO 异常直接抛出；默认放行返回 None。"""
        with mock.patch("os.makedirs", side_effect=OSError(13, "denied")):
            self.assertIsNone(singleton.acquire_instance_lock(self.lock_path))
            with self.assertRaises(OSError):
                singleton.acquire_instance_lock(self.lock_path, strict=True)


class TestCliAndGuiConflict(unittest.TestCase):
    """A11：mock 已持有锁时 CLI / GUI 入口的冲突行为。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")
        with open(self.config_path, "w", encoding="utf-8") as fp:
            import yaml

            yaml.safe_dump(
                {
                    "keywords": [{"keyword": "Switch", "max_price": 1000}],
                    "monitor": {"interval_seconds": 60, "cookies": ""},
                    "fetcher": {"type": "mock"},
                    "storage": {"path": ":memory:"},
                    "notify": {"channels": [{"type": "console"}]},
                },
                fp,
                allow_unicode=True,
                sort_keys=False,
            )
        singleton._held_lock = None

    def tearDown(self) -> None:
        singleton.release_instance_lock(singleton._held_lock)
        singleton._held_lock = None
        self.tmpdir.cleanup()

    def _assert_cli_conflict(self, command: str) -> None:
        """断言 cli run/once 冲突 → 退出码 2 + stderr 中文提示。"""
        from xianyu_alert import cli

        stderr = io.StringIO()
        stdout = io.StringIO()
        with mock.patch("xianyu_alert.cli.acquire_instance_lock", return_value=None), \
             mock.patch("xianyu_alert.cli.lock_holder_pid", return_value="12345"):
            with redirect_stderr(stderr), redirect_stdout(stdout):
                code = cli.main([command, "--config", self.config_path])
        self.assertEqual(code, 2)
        self.assertIn("已有实例运行中", stderr.getvalue())
        self.assertIn("12345", stderr.getvalue())

    def test_cli_once_conflict_returns_2(self) -> None:
        self._assert_cli_conflict("once")

    def test_cli_run_conflict_returns_2(self) -> None:
        self._assert_cli_conflict("run")

    def test_gui_main_conflict_returns_nonzero(self) -> None:
        """gui.main 冲突 → 返回非 0 且产生中文提示（无显示环境降级打印）。"""
        from xianyu_alert import gui

        stdout = io.StringIO()
        with mock.patch("xianyu_alert.gui.acquire_instance_lock", return_value=None), \
             mock.patch("xianyu_alert.gui.lock_holder_pid", return_value="999"), \
             mock.patch("xianyu_alert.gui.tk.Tk", side_effect=Exception("no display")):
            with redirect_stdout(stdout):
                code = gui.main(config_path=self.config_path)
        self.assertNotEqual(code, 0)
        self.assertIn("已有实例正在运行", stdout.getvalue())

    def test_qt_main_conflict_returns_nonzero(self) -> None:
        """gui_qt.main 冲突 → 返回非 0 且产生中文提示（QMessageBox mock）。

        注意：mock `PySide6.QtWidgets.QApplication` 以避免创建真实 QNSApplication——
        macOS 上先建 Qt 后建 Tk 会崩溃（Tk 与 Qt 混用限制），此处只验证冲突分支。
        """
        try:
            from PySide6.QtWidgets import QMessageBox  # noqa: F401
        except Exception as exc:  # pragma: no cover - 无 PySide6 环境
            self.skipTest(f"PySide6 不可用：{exc}")

        from xianyu_alert import gui_qt

        fake_qapp = mock.MagicMock()
        fake_qapp.instance.return_value = fake_qapp
        with mock.patch("xianyu_alert.gui_qt.acquire_instance_lock", return_value=None), \
             mock.patch("xianyu_alert.gui_qt.lock_holder_pid", return_value="777"), \
             mock.patch("PySide6.QtWidgets.QApplication", fake_qapp), \
             mock.patch("PySide6.QtWidgets.QMessageBox") as mb:
            code = gui_qt.main(config_path=self.config_path)
        self.assertNotEqual(code, 0)
        self.assertTrue(mb.warning.called)
        args, _kwargs = mb.warning.call_args
        self.assertIn("已有实例正在运行", args[1] if len(args) > 1 else "")

    def test_login_and_list_not_locked(self) -> None:
        """login / list 不参与锁（L3/Q7）——即使锁被占用也正常执行。"""
        from xianyu_alert import cli

        called: list = []
        with mock.patch("xianyu_alert.cli.acquire_instance_lock", side_effect=AssertionError("不应获取锁")):
            with mock.patch.object(cli, "cmd_list", side_effect=lambda args: called.append("list") or 0):
                code = cli.main(["list", "--config", self.config_path])
        self.assertEqual(code, 0)
        self.assertEqual(called, ["list"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
