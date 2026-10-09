"""通知投递策略：静默时段 + 命中聚合（v1.10.2）。

为什么需要：改造前每轮命中都会立刻把消息推出去。低价商品密集出现时（比如整点上新），
用户会被十几条通知连续轰炸 —— 这是同类工具最常见的抱怨；夜间更是直接把人吵醒。

这里把"什么时候该发"和"攒多少一起发"抽成**纯逻辑**（不碰网络、不依赖渠道实现），
因此可以完整离线测试：静默时段跨午夜、聚合窗口、窗口到期的边界都覆盖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from typing import Any


def parse_quiet_hours(text: str) -> tuple[dtime, dtime] | None:
    """解析静默时段配置（如 "23:00-07:00"）。

    Args:
        text: "HH:MM-HH:MM"；空串或非法格式返回 None（表示不静默）。

    Returns:
        (开始, 结束)；跨午夜（开始 > 结束）由调用方按跨天处理。
    """
    raw = str(text or "").strip()
    if not raw or "-" not in raw:
        return None
    left, _, right = raw.partition("-")
    try:
        start = _parse_hhmm(left)
        end = _parse_hhmm(right)
    except ValueError:
        return None
    return start, end


def _parse_hhmm(text: str) -> dtime:
    """把 HH:MM 解析成 time；非法时抛 ValueError。"""
    hour, _, minute = str(text).strip().partition(":")
    return dtime(int(hour), int(minute))


def in_quiet_hours(now: datetime, window: tuple[dtime, dtime] | None) -> bool:
    """判断某个时刻是否落在静默时段内（支持跨午夜，如 23:00-07:00）。

    Args:
        now: 当前时间。
        window: parse_quiet_hours 的结果；None 表示不静默。

    Returns:
        True 表示此刻应静默（攒着不发）。
    """
    if window is None:
        return False
    start, end = window
    current = now.time()
    if start == end:
        return False
    if start < end:
        return start <= current < end
    # 跨午夜： [start, 24:00) 或 [00:00, end)
    return current >= start or current < end


@dataclass
class NotificationPolicy:
    """通知投递策略（静默时段 + 聚合窗口 + 重试）。

    Attributes:
        quiet_hours: "HH:MM-HH:MM"；空表示不静默。
        aggregate_seconds: 聚合窗口秒数；0 表示不聚合（每条立刻发）。
        retry_attempts: 单渠道失败后的重试次数（0 表示不重试）。
    """

    quiet_hours: str = ""
    aggregate_seconds: int = 0
    retry_attempts: int = 0

    @property
    def enabled(self) -> bool:
        """是否需要走聚合/静默路径（都关闭时保持旧行为）。"""
        return self.aggregate_seconds > 0 or bool(parse_quiet_hours(self.quiet_hours))

    def quiet_window(self) -> tuple[dtime, dtime] | None:
        """解析后的静默窗口。"""
        return parse_quiet_hours(self.quiet_hours)

    def is_quiet(self, now: datetime) -> bool:
        """此刻是否静默。"""
        return in_quiet_hours(now, self.quiet_window())


@dataclass
class NotificationBuffer:
    """命中商品的聚合缓冲（纯状态机，可注入时钟）。

    行为：
        - 策略未启用 → 每次 add 都立刻"到期"，调用方按原语义立即投递；
        - 静默中 → 攒着不投，直到离开静默时段后的第一次检查；
        - 非静默 → 攒够 aggregate_seconds 才投，避免密集刷屏。
    """

    policy: NotificationPolicy = field(default_factory=NotificationPolicy)
    _pending: list[Any] = field(default_factory=list)
    _first_pending_at: float = 0.0

    def add(self, products: list[Any], mono: float | None = None) -> None:
        """把本轮命中加入缓冲。

        Args:
            products: 本轮命中商品。
            mono: **单调钟**时间戳（聚合窗口用它计时，避免墙钟被调整时窗口错乱）。
        """
        if not products:
            return
        stamp = time.monotonic() if mono is None else mono
        if not self._pending:
            self._first_pending_at = stamp
        self._pending.extend(products)

    def pending_count(self) -> int:
        """当前攒了多少条（供状态展示）。"""
        return len(self._pending)

    def due(self, now: datetime | None = None, mono: float | None = None) -> bool:
        """现在是否应该投递（纯判定，不改变状态）。

        Args:
            now: 墙钟时间（判断静默时段）。
            mono: 单调时钟（判断聚合窗口）；默认取 time.monotonic()。

        Returns:
            True 表示可以把缓冲发出去。
        """
        if not self._pending:
            return False
        if not self.policy.enabled:
            return True
        wall = now or datetime.now()
        if self.policy.is_quiet(wall):
            return False
        if self.policy.aggregate_seconds <= 0:
            return True
        stamp = time.monotonic() if mono is None else mono
        return (stamp - self._first_pending_at) >= self.policy.aggregate_seconds

    def take(self) -> list[Any]:
        """取出并清空缓冲。"""
        items = list(self._pending)
        self._pending.clear()
        self._first_pending_at = 0.0
        return items

    def flush_if_due(self, now: datetime | None = None, mono: float | None = None) -> list[Any]:
        """到期则返回待投递列表并清空；否则返回空列表。"""
        if not self.due(now, mono):
            return []
        return self.take()
