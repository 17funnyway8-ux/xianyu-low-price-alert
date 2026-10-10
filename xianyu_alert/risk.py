"""风控熔断（v1.11.3）：观测到闲鱼风控后，让**所有**出网入口安静下来。

背景（线上实测）：2026-10-10 11 点那一小时里，容器日志出现 **129 条 RGV587 风控**，
成因是「保活失败无退避 + 每次抓 3 页」与短时间内的连续试抓叠加。更早的行为是：

    1. 同一轮里某一页被风控 → 循环**继续抓剩余页**（再打 2 次）；
    2. 下一轮仍按 monitor.interval_seconds 照常开始；
    3. 保活探测、校验在架照常打点。

也就是说，程序在"被限流"时反而保持原节奏继续试探 —— 越撞越紧。

本模块给出**进程级**熔断器：一旦观测到风控，冷却期内所有出网入口一律静默跳过，
冷却时长按「连续命中次数」指数增长（首次 interval x 3，之后翻倍，上限 6 小时）。
它只维护本地状态，**不发任何请求**；纯内存，重启即失效（重启后先观察一轮即可）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

#: 冷却下限（秒）：即使 monitor 间隔配得很短，风控后也至少安静这么久
MIN_COOLDOWN_SECONDS = 300.0
#: 冷却上限（秒）：6 小时
MAX_COOLDOWN_SECONDS = 6 * 3600.0
#: 首次命中的冷却倍数：interval x 3
COOLDOWN_MULTIPLIER = 3.0


class RiskGuard:
    """进程级风控熔断状态（线程安全，纯本地）。

    Attributes:
        hits: 连续风控命中次数（成功一轮后清零）。
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        """初始化熔断器。

        Args:
            clock: 时钟函数（测试可注入），默认 time.time。
        """
        self._clock: Callable[[], float] = clock or time.time
        self._lock = threading.Lock()
        self._until: float = 0.0
        self.hits: int = 0
        self.last_detail: str = ""

    # ------------------------------------------------------------------ #
    def note_risk(self, interval_seconds: float = 0.0, detail: str = "") -> float:
        """记录一次风控命中并延长冷却，返回冷却截止时间戳。

        Args:
            interval_seconds: 当前监控间隔（用于计算首次冷却时长）。
            detail: 风控原文（截断后存快照，便于排查）。

        Returns:
            冷却截止时间戳（秒）。
        """
        with self._lock:
            self.hits += 1
            base = max(float(interval_seconds or 0.0), MIN_COOLDOWN_SECONDS)
            cooldown = base * COOLDOWN_MULTIPLIER * (2 ** (self.hits - 1))
            cooldown = min(cooldown, MAX_COOLDOWN_SECONDS)
            self._until = max(self._until, self._clock() + cooldown)
            self.last_detail = str(detail or "")[:200]
            return self._until

    def note_success(self) -> None:
        """一轮抓取成功 → 连续命中计数清零（不提前解除已有冷却）。"""
        with self._lock:
            self.hits = 0

    def active(self) -> bool:
        """当前是否处于冷却期（冷却期内不应发起任何闲鱼请求）。"""
        with self._lock:
            return self._clock() < self._until

    def remaining(self) -> float:
        """剩余冷却秒数（未冷却时为 0）。"""
        with self._lock:
            return max(0.0, self._until - self._clock())

    def snapshot(self) -> dict[str, object]:
        """状态快照（供 /healthz、状态接口与日志使用）。"""
        with self._lock:
            remaining = max(0.0, self._until - self._clock())
            return {
                "active": remaining > 0,
                "remaining_seconds": int(remaining),
                "hits": self.hits,
                "last_detail": self.last_detail,
            }

    def reset(self) -> None:
        """清空熔断状态（供测试与人工排障使用）。"""
        with self._lock:
            self._until = 0.0
            self.hits = 0
            self.last_detail = ""


#: 进程级单例：监控轮次 / 保活 / 校验在架共享同一份熔断状态
RISK_GUARD = RiskGuard()
