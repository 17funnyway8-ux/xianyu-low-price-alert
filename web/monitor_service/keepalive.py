"""空闲保活（KeepaliveMixin，v1.9.7 从 MonitorService 拆出）。"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import time
from typing import Any

from xianyu_alert import gui, secure  # noqa: F401  # gui 防御性导入 tkinter，容器可 import
from xianyu_alert.fetcher import build_fetcher
from xianyu_alert.risk import RISK_GUARD

logger = logging.getLogger(__name__)


class KeepaliveMixin:
    # 下列属性/方法由宿主 MonitorService（__init__ 或其它 mixin）提供：
    # mixin 与宿主共享同一实例状态，这里声明仅为类型可见性，不做初始化。
    _keeper: Any
    _persist_refreshed_token: Any
    config: Any
    def keepalive_probe(self) -> bool:
        """发一次轻量抓取以维持令牌滑动续期（不写库、不通知）。

        v1.9：与是否在跑监控无关 —— 只要服务在运行就保活，避免"抓一次就停、
        几小时后再用要重新拿 Cookie"。
        v1.11.2：只挑**启用中**的关键词、且**只抓 1 页**（保活只需要一次鉴权请求）。

        Returns:
            True 表示请求成功（令牌已续期）。
        """
        from xianyu_alert.cookie import resolve_cookie_for_round

        if RISK_GUARD.active():
            # v1.11.3：风控冷却期内保活静默。保活本质也是一次真实抓取，
            # 被限流时继续打点只会延长处罚（线上实测过 129 条 RGV587）。
            logger.warning(
                "保活跳过：风控冷却中（剩余 %d 秒），冷却结束后自动恢复",
                int(RISK_GUARD.remaining()),
            )
            return False
        config = self.config
        cookie = resolve_cookie_for_round(config.monitor, 0)
        if not cookie:
            return False
        rules = [r for r in config.keywords if str(getattr(r, "keyword", "") or "")]
        if not rules:
            return False
        # v1.11.2：优先用**启用中**的关键词探测。此前固定用 keywords[0]，
        # 若它恰好被用户停用，保活就等于对着"明确不想抓"的商品猛抓（线上实测到）。
        enabled = [r for r in rules if bool(getattr(r, "enabled", True))]
        keyword = str(getattr((enabled or rules)[0], "keyword", "") or "")

        # v1.11.2：保活只为续期令牌，**只抓 1 页**。此前沿用 fetcher.pages（默认 3 页），
        # 一次探测就是 3 个请求；叠加 30s 巡检重试，足以把账号打进 RGV587 风控。
        probe_config = config
        with contextlib.suppress(Exception):
            probe_config = dataclasses.replace(
                config, fetcher=dataclasses.replace(config.fetcher, pages=1)
            )
        fetcher = build_fetcher(probe_config)
        setter = getattr(fetcher, "set_cookies", None)
        if callable(setter):
            setter(cookie)
        fetcher.fetch(keyword)
        # v1.9.2：保活用的是一次性 fetcher，服务端刷新的 _m_h5_tk 只留在它的内存里；
        # 必须复用与正常轮次相同的节流落盘，否则界面会一直显示"令牌已过期"
        # （实际会话已被服务端续期，但本地存的是旧时间戳）。
        with contextlib.suppress(Exception):
            self._persist_refreshed_token(fetcher)
        self._last_auth_at = time.time()
        return True

    def keepalive_settings(self) -> dict[str, Any]:
        """保活线程读取的配置快照。"""
        monitor_cfg = self.config.monitor
        return {
            "enabled": bool(getattr(monitor_cfg, "keepalive_enabled", True)),
            "interval": int(getattr(monitor_cfg, "keepalive_interval_seconds", 1800) or 0),
            "last_auth_at": self._last_auth_at,
        }

    def start_keepalive(self) -> None:
        """启动 Cookie 空闲保活线程（幂等）。"""
        from xianyu_alert.keepalive import CookieKeeper

        if self._keeper is None:
            self._keeper = CookieKeeper(
                probe=self.keepalive_probe, settings=self.keepalive_settings
            )
        self._keeper.start()

    def stop_keepalive(self) -> None:
        """停止 Cookie 空闲保活线程（幂等）。"""
        if self._keeper is not None:
            self._keeper.stop()

