"""进程级请求计量（v1.11.4）：让「它到底打了多少次」一眼可见。

背景：账号安全排查时发现，用户完全看不到程序的请求节奏 —— 系统页只有轮次与
最近轮次时间，出了风控只能靠翻容器日志数行数。本模块给每次真实出网请求记账，
并在 /healthz 与状态接口里暴露最近 10 分钟 / 1 小时的请求量。

只维护本地内存计数（上限 5000 条时间戳，重启清零），不发任何请求。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime

#: 时间戳环形缓冲上限（足够覆盖 1 小时窗口，内存开销可忽略）
DEFAULT_MAXLEN = 5000
#: 统计窗口（秒）
WINDOW_SHORT = 600.0    # 10 分钟
WINDOW_LONG = 3600.0    # 1 小时


class RequestMeter:
    """按时间窗口统计闲鱼出网请求（线程安全，纯本地）。

    Attributes:
        total: 进程启动以来累计请求数（含重试与保活）。
    """

    def __init__(self, clock: Callable[[], float] | None = None, maxlen: int = DEFAULT_MAXLEN) -> None:
        """初始化计量器。

        Args:
            clock: 时钟函数（测试可注入），默认 time.time。
            maxlen: 时间戳缓冲上限。
        """
        self._clock: Callable[[], float] = clock or time.time
        self._lock = threading.Lock()
        self._stamps: deque[float] = deque(maxlen=max(16, int(maxlen)))
        self._last_at: float = 0.0
        self.total: int = 0
        self.kinds: dict[str, int] = {}

    def note(self, kind: str = "search") -> None:
        """记一次请求（在真正发出请求之前调用）。

        Args:
            kind: 请求类型（search / detail / other），仅用于分类展示。
        """
        now = self._clock()
        with self._lock:
            self._stamps.append(now)
            self._last_at = now
            self.total += 1
            key = str(kind or "other")
            self.kinds[key] = self.kinds.get(key, 0) + 1

    def _count_since(self, now: float, window: float) -> int:
        cutoff = now - window
        return sum(1 for stamp in self._stamps if stamp >= cutoff)

    def snapshot(self) -> dict[str, object]:
        """状态快照（供 /healthz 与状态接口）。"""
        now = self._clock()
        with self._lock:
            return {
                "total": self.total,
                "last_10min": self._count_since(now, WINDOW_SHORT),
                "last_hour": self._count_since(now, WINDOW_LONG),
                "last_request_at": (
                    datetime.fromtimestamp(self._last_at).strftime("%Y-%m-%d %H:%M:%S")
                    if self._last_at > 0
                    else None
                ),
                "kinds": dict(self.kinds),
            }

    def reset(self) -> None:
        """清零（供测试与人工排障使用）。"""
        with self._lock:
            self._stamps.clear()
            self._last_at = 0.0
            self.total = 0
            self.kinds.clear()


#: 进程级单例：抓取器记账，Web 层展示
REQ_METER = RequestMeter()
