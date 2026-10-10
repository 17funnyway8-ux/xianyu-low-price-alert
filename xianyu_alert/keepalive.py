"""Cookie 空闲保活（v1.9 新增）。

问题：_m_h5_tk 的有效期只有数小时（实测约 2.5-4 小时），而且**只有发出请求
才会滑动续期**。监控暂停、夜间空闲、或者"抓一次就停"之后，令牌自然过期 ——
用户感受到的就是"刚拿的 Cookie 几小时后就不能用了"。

本模块把保活与监控解耦：只要服务进程在运行（例如 NAS 上的容器 24x7），就按
间隔发一次**极轻量**的已鉴权请求，把令牌的滑动窗口接续下去。保活不写库、
不通知，失败只记日志，绝不影响正常抓取轮次。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: 保活间隔下限（秒）：低于该值视为关闭，避免无意义地频繁请求
MIN_KEEPALIVE_INTERVAL = 300
#: 默认保活间隔（秒）= 30 分钟
DEFAULT_KEEPALIVE_INTERVAL = 1800
#: 保活线程的巡检间隔（秒）
DEFAULT_TICK = 30.0
#: 保活失败后的冷却倍数：失败（多半是风控 / 网络）后至少等
#: max(interval, MIN_KEEPALIVE_INTERVAL) 再试，避免 30s 巡检变成重试风暴。
FAILURE_COOLDOWN_FACTOR = 1.0


def keepalive_due(
    *,
    now: float,
    last_auth_at: float,
    interval: int,
    enabled: bool = True,
) -> bool:
    """判断此刻是否该执行一次保活（纯函数，便于单测）。

    Args:
        now: 当前时间戳（秒）。
        last_auth_at: 最近一次已鉴权请求的时间戳（秒）；0 表示"从未有过"。
        interval: 保活间隔（秒）。
        enabled: 保活总开关。

    Returns:
        True 表示应当保活。
    """
    if not enabled:
        return False
    if interval < MIN_KEEPALIVE_INTERVAL:
        return False
    if last_auth_at <= 0:
        return True
    return (now - last_auth_at) >= interval


class CookieKeeper:
    """后台保活线程：按配置间隔调用 probe 维持登录态。

    与 Monitor 解耦：probe 与 settings 都是回调，便于在没有完整 Monitor 的场景
    （例如 Web 服务）复用，也便于单测注入。

    Attributes:
        running: 保活线程是否在运行。
    """

    def __init__(
        self,
        probe: Callable[[], bool],
        settings: Callable[[], dict],
        tick: float = DEFAULT_TICK,
    ) -> None:
        """构造保活线程。

        Args:
            probe: 执行一次保活的回调；返回 True 表示成功。内部应自行吞掉异常。
            settings: 返回当前配置的回调，需含 enabled / interval / last_auth_at。
            tick: 巡检间隔（秒）。
        """
        self._probe = probe
        self._settings = settings
        self._tick = max(5.0, float(tick))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.success_count = 0
        self.failure_count = 0
        self.last_attempt_at: float = 0.0
        #: v1.11.2：失败冷却截止时间戳。保活失败通常意味着闲鱼风控（RGV587）或断网，
        #: 此时按 30s 巡检节奏立刻重试只会把风控越撞越紧 —— 线上实测过：
        #: 被打进风控后每 30s 重试一次、每次抓 3 页，等于自己制造请求风暴。
        self._cooldown_until: float = 0.0

    @property
    def running(self) -> bool:
        """保活线程是否存活。"""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动保活线程（幂等）。"""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cookie-keeper", daemon=True)
        self._thread.start()
        logger.info("Cookie 保活线程已启动（巡检间隔 %.0fs）", self._tick)

    def stop(self, timeout: float = 2.0) -> None:
        """停止保活线程（幂等）。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def tick_once(self) -> bool:
        """巡检一次（供线程与测试调用），返回是否真的执行了保活。"""
        try:
            cfg = self._settings() or {}
        except Exception as exc:  # noqa: BLE001 - 配置读取异常不应打断线程
            logger.warning("保活读取配置失败：%s", exc)
            return False
        now = time.time()
        interval = int(cfg.get("interval", 0) or 0)
        if now < self._cooldown_until:
            # v1.11.2：失败后的冷却期内**不再尝试**（避免风控期重试风暴）
            return False
        if not keepalive_due(
            now=now,
            last_auth_at=float(cfg.get("last_auth_at", 0.0) or 0.0),
            interval=interval,
            enabled=bool(cfg.get("enabled", True)),
        ):
            return False
        self.last_attempt_at = now
        try:
            ok = bool(self._probe())
        except Exception as exc:  # noqa: BLE001 - 保活失败不影响主流程
            logger.warning("保活执行异常：%s", exc)
            ok = False
        if ok:
            self.success_count += 1
            self._cooldown_until = 0.0
        else:
            self.failure_count += 1
            cooldown = max(interval, MIN_KEEPALIVE_INTERVAL) * FAILURE_COOLDOWN_FACTOR
            self._cooldown_until = now + cooldown
            logger.warning("保活失败，%.0f 秒内不再重试（避免在风控期制造重试风暴）", cooldown)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick_once()
            self._stop.wait(self._tick)

    def snapshot(self) -> dict:
        """保活状态快照（供 API / 界面展示）。"""
        return {
            "running": self.running,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_attempt_at": self.last_attempt_at,
        }
