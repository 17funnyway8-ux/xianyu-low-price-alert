"""SSE 广播与日志桥：把后台线程的日志转发到前端（v1.9.7 拆出）。"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

from xianyu_alert import gui, secure  # noqa: F401  # gui 防御性导入 tkinter，容器可 import

from .constants import _LOG_LOGGER_NAME

if TYPE_CHECKING:
    from .service import MonitorService

logger = logging.getLogger(__name__)

class SseBroadcaster:
    """线程安全的 SSE 广播器：monitor 线程发布，ASGI 端点订阅。

    每个订阅者持有一个 `asyncio.Queue`（maxsize 500，溢出丢最旧）；
    发布来自任意线程（monitor / API handler），通过
    `loop.call_soon_threadsafe(queue.put_nowait, data)` 投递到订阅者所在事件循环，
    避免直接在别的线程操作 asyncio.Queue（线程不安全）。
    """

    def __init__(self) -> None:
        self._subscribers: dict[int, tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = {}
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> tuple[int, asyncio.Queue]:
        """注册一个订阅者，返回 (订阅号, 队列)。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            sub_id = next(self._counter)
            self._subscribers[sub_id] = (queue, loop)
        return sub_id, queue

    def unsubscribe(self, sub_id: int) -> None:
        """注销订阅者（幂等）。"""
        with self._lock:
            self._subscribers.pop(sub_id, None)

    def publish(self, data: dict[str, Any]) -> None:
        """把一条消息广播给全部订阅者（跨线程安全，自身异常绝不外抛）。"""
        with self._lock:
            subs = list(self._subscribers.items())
        for sub_id, (queue, loop) in subs:
            try:
                if loop.is_closed():
                    self.unsubscribe(sub_id)
                    continue
                loop.call_soon_threadsafe(self._safe_put, queue, data)
            except Exception:  # noqa: BLE001 - 单个订阅者失败不影响其它订阅者
                self.unsubscribe(sub_id)

    @staticmethod
    def _safe_put(queue: asyncio.Queue, data: dict[str, Any]) -> None:
        """在事件循环线程内执行的投递（队列满时丢最旧，绝不阻塞）。"""
        with contextlib.suppress(Exception):   # 投递失败静默（订阅端可能已断开）
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(data)



# ---------------------------------------------------------------------- #
# 日志 Handler（环形缓冲 + 广播）
# ---------------------------------------------------------------------- #
class ServiceLogHandler(logging.Handler):
    """把 `xianyu_alert` logger 的日志转发到「当前活跃」的 MonitorService。

    模块级单例 handler（避免多次 import / 多个服务实例叠加重复日志）；
    通过 `set_service` 指向当前活跃服务，服务 shutdown 时置空。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._service: MonitorService | None = None

    def set_service(self, service: MonitorService | None) -> None:
        """切换当前活跃服务（None 表示无服务，日志只进标准输出）。"""
        self._service = service

    def emit(self, record: logging.LogRecord) -> None:
        """把一条日志写入服务环形缓冲并广播（自身异常绝不向外抛）。"""
        with contextlib.suppress(Exception):   # 日志处理失败绝不影响业务线程
            service = self._service
            if service is None:
                return
            message = record.getMessage()
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            text = f"[{ts}] {message}"
            if record.exc_info:
                text = f"{text}\n{self.format(record)}"
            service.append_log(record.levelname, text, ts)


#: 模块级日志 handler（进程内只挂一次）
_log_handler: ServiceLogHandler | None = None


def _ensure_log_handler() -> ServiceLogHandler:
    """确保 `xianyu_alert` logger 挂了唯一的 ServiceLogHandler（幂等）。

    同时把该 logger 级别设为 INFO：Web 服务明确需要捕获 INFO 级日志进环形
    缓冲（不依赖 root logger 的 basicConfig 配置；entry.py 也会 setup_logging）。
    """
    global _log_handler
    if _log_handler is None:
        _log_handler = ServiceLogHandler()
        target = logging.getLogger(_LOG_LOGGER_NAME)
        target.setLevel(logging.INFO)
        target.addHandler(_log_handler)
    return _log_handler


# ---------------------------------------------------------------------- #
# 表单转换（复用 gui 纯函数；Cookie 一律脱敏，不回传明文）
# ---------------------------------------------------------------------- #

__all__ = [
    "SseBroadcaster",
    "ServiceLogHandler",
]
