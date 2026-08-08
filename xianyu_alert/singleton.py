"""进程级单实例锁（OS 文件锁，零新增运行时依赖）。

解决的问题（v1.8）：GUI（Tk/Qt）+ `cli run` + `cli once` 都会打开同一 SQLite
写库，若两个进程同时运行会互相抢锁导致数据库锁死 / 数据损坏。本模块提供一把
**OS 级进程锁**：同一 `data_dir()` 下只允许一个实例运行。

实现：
    - POSIX（macOS / Linux）：`fcntl.flock(fd, LOCK_EX | LOCK_NB)`；
    - Windows：`msvcrt.locking(fd, LK_NBLCK, 1)`（先保证文件至少 1 字节）；
    - 进程退出（含崩溃 / kill -9 / 断电）由 OS 自动释放锁——**无残留锁问题**，
      锁状态在打开的文件描述符上而非文件存在性（L8）。

设计要点（对齐架构设计 §2.1 / 共享知识 1/9/10）：
    - 锁锚定 `paths.default_state_dir()/instance.lock`（=`data_dir()/state/instance.lock`）；
    - 同进程重复调用幂等（模块级缓存 `_held_lock`），不会自锁（L4）；
    - 锁获取 IO 失败默认放行（warning）并返回 None，不因锁模块故障阻断主流程（L10）；
    - `login` / `list` 不参与锁（写 config 不写 SQLite / 只读），GUI 与 login
      并发写 config 的冲突由 GUI 侧 config mtime 检测（C22）缓解。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

#: 锁文件名（位于 paths.default_state_dir()）
LOCK_FILE_NAME = "instance.lock"

# 平台相关模块：顶层分别 try import，供测试 mock `sys.platform` / 缺失场景（A12）。
try:  # pragma: no cover - 分支由运行环境决定
    import fcntl

    _FCNTL_AVAILABLE = True
except ImportError:  # pragma: no cover - Windows 无 fcntl
    fcntl = None  # type: ignore[assignment]
    _FCNTL_AVAILABLE = False

try:  # pragma: no cover - 分支由运行环境决定
    import msvcrt

    _MSVCRT_AVAILABLE = True
except ImportError:  # pragma: no cover - POSIX 无 msvcrt
    msvcrt = None  # type: ignore[assignment]
    _MSVCRT_AVAILABLE = False


def _is_windows() -> bool:
    """是否使用 Windows 锁实现（动态读取 sys.platform，便于测试 mock）。"""
    return sys.platform == "win32" or os.name == "nt"


def _resolve_lock_path(lock_path: Optional[str]) -> str:
    """解析锁文件路径；None → `paths.default_state_dir()/instance.lock`。

    Args:
        lock_path: 显式锁文件路径；None 时使用默认路径。

    Returns:
        锁文件绝对路径。
    """
    if lock_path:
        return os.path.abspath(str(lock_path))
    from . import paths

    return os.path.join(paths.default_state_dir(), LOCK_FILE_NAME)


class InstanceLock:
    """已持有的进程级独占锁（持有 fd 与 pid）。

    Attributes:
        lock_path: 锁文件绝对路径。
        fd: 打开的文件描述符（release 后为 -1）。
        pid: 持有该锁的进程 PID。
    """

    def __init__(self, lock_path: str, fd: int, pid: int) -> None:
        """初始化。

        Args:
            lock_path: 锁文件绝对路径。
            fd: 打开的文件描述符。
            pid: 当前进程 PID。
        """
        self.lock_path: str = lock_path
        self.fd: int = fd
        self.pid: int = pid

    def release(self) -> None:
        """释放锁（幂等：重复 / 已释放均安全）。

        显式释放是「优雅退出」路径；进程崩溃 / kill 时 OS 会自动释放，
        是否调用本方法都不影响锁的正确性（L8）。
        """
        if self.fd < 0:
            return
        try:
            if _is_windows():
                if msvcrt is not None:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                if fcntl is not None:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001 - 解锁失败不影响后续 close
            logger.debug("释放单实例锁失败（%s）：继续关闭 fd", self.lock_path)
        try:
            os.close(self.fd)
        except Exception:  # noqa: BLE001 - close 失败幂等
            pass
        self.fd = -1


#: 模块级缓存：同进程内已持有的锁（幂等，避免入口叠加自锁）
_held_lock: Optional[InstanceLock] = None


def _try_lock(fd: int) -> None:
    """对已打开的 fd 执行非阻塞加锁。

    Args:
        fd: 文件描述符。

    Raises:
        BlockingIOError / OSError: 锁已被其它进程占用（调用方转为 None）。
    """
    if _is_windows():
        # Windows 需要文件至少 1 字节；空文件先写 1 字节再锁
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        if msvcrt is None:
            raise OSError("msvcrt 不可用，无法加锁")
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    if fcntl is None:
        raise OSError("fcntl 不可用，无法加锁")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _write_pid(fd: int) -> None:
    """把当前进程 PID 写入锁文件（供冲突方展示「PID xxx」提示）。"""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.lseek(fd, 0, os.SEEK_SET)
    except Exception:  # noqa: BLE001 - 写 PID 失败不影响锁本身
        logger.debug("写入锁文件 PID 失败", exc_info=True)


def acquire_instance_lock(
    lock_path: Optional[str] = None, strict: bool = False
) -> Optional[InstanceLock]:
    """尝试获取进程级独占锁。

    - lock_path=None → `paths.default_state_dir()/instance.lock`（自动建目录）；
    - 成功 → 返回 InstanceLock；已被其它进程占用 → 返回 None（不抛异常）；
    - IO 异常：strict=True 时抛 OSError；False（默认）时 logger.warning 并返回
      None（放行，L10）；
    - 同进程重复调用 → 返回已持有的同一个对象（幂等，L4），不二次加锁。

    Args:
        lock_path: 显式锁文件路径；None 时使用默认路径。
        strict: True 时加锁 IO 异常直接抛出；False 时放行（默认）。

    Returns:
        已持有的 InstanceLock；占用或异常时返回 None。
    """
    global _held_lock
    # 幂等：仅当请求路径与已持有锁路径**一致**时返回缓存对象（L4）；
    # 若路径不同，视为另一把锁继续获取（本模块单缓存槽，新锁会覆盖缓存，
    # 旧 fd 由 OS 在进程退出时释放——生产只使用默认单锁路径，此分支仅防御）。
    if _held_lock is not None and _held_lock.lock_path == _resolve_lock_path(lock_path):
        return _held_lock

    path = _resolve_lock_path(lock_path)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        if strict:
            raise
        logger.warning("无法创建单实例锁文件 %s（%s），已放行", path, exc)
        return None

    try:
        _try_lock(fd)
    except (BlockingIOError, OSError):
        # 已被其它进程占用（flock LOCK_NB / msvcrt LK_NBLCK 的非阻塞失败路径）
        try:
            os.close(fd)
        except Exception:  # noqa: BLE001
            pass
        return None
    except Exception as exc:  # noqa: BLE001 - 其它 IO 异常
        try:
            os.close(fd)
        except Exception:  # noqa: BLE001
            pass
        if strict:
            raise OSError(f"获取单实例锁失败：{exc}") from exc
        logger.warning("获取单实例锁失败（%s），已放行", exc)
        return None

    _write_pid(fd)
    _held_lock = InstanceLock(lock_path=path, fd=fd, pid=os.getpid())
    logger.info("已获取单实例锁：%s（PID %s）", path, os.getpid())
    return _held_lock


def release_instance_lock(lock: Optional[InstanceLock]) -> None:
    """释放锁（幂等：None / 已释放均安全）。

    进程退出时 OS 自动释放，可不显式调用；显式调用用于 CLI 子命令
    （run / once）执行完毕后及时让出锁。

    Args:
        lock: 待释放的 InstanceLock；None 直接返回。
    """
    global _held_lock
    if lock is None:
        return
    if _held_lock is lock:
        _held_lock = None
    lock.release()


def lock_holder_pid(lock_path: Optional[str] = None) -> str:
    """读取当前（或最近）锁持有者 PID（尽力而为，用于冲突提示文案）。

    Args:
        lock_path: 显式锁文件路径；None 时使用默认路径。

    Returns:
        PID 字符串；无法读取时返回空串。
    """
    path = _resolve_lock_path(lock_path)
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return fp.read().strip()
    except OSError:
        return ""


def _probe_is_busy(path: str) -> bool:
    """对指定锁文件做**非阻塞临时探测**：能拿到锁则立即释放（不持有），
    返回是否已被占用。使用独立临时 fd，**绝不触碰模块级 `_held_lock`**。

    Args:
        path: 锁文件绝对路径。

    Returns:
        True 表示已被占用（或探测失败，保守视为运行中）；False 表示空闲。
    """
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return True
    try:
        _try_lock(fd)
    except (BlockingIOError, OSError):
        try:
            os.close(fd)
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001 - 探测异常保守视为运行中
        try:
            os.close(fd)
        except Exception:  # noqa: BLE001
            pass
        return True
    # 拿到临时锁 → 立即释放并关闭（只探测，不持有）
    InstanceLock(lock_path=path, fd=fd, pid=os.getpid()).release()
    return False


def is_running(lock_path: Optional[str] = None) -> bool:
    """只检测不持有：非阻塞尝试获取，能获取则立即释放并返回 False（无实例），
    否则返回 True（已有实例在运行）。

    边界（QA 观察项 1）：
        - 本进程已持有**同一路径**的锁（模块缓存命中）→ 直接返回 True 且
          **不释放**（`is_running` 用于「另一实例视角」探测，不能破坏当前锁）；
        - 请求路径与已持有锁**不同** → 基于新路径做临时 fd 非阻塞探测
          （`_probe_is_busy`），**不触碰 `_held_lock`**，避免误释放已持有的锁。

    Args:
        lock_path: 显式锁文件路径；None 时使用默认路径。

    Returns:
        True 表示已有实例持有锁（含本进程持有同路径锁）。
    """
    global _held_lock
    path = _resolve_lock_path(lock_path)
    if _held_lock is not None and _held_lock.lock_path == path:
        return True
    return _probe_is_busy(path)
