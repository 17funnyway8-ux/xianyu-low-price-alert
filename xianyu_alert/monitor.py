"""核心监测循环：抓取 -> 筛选新商品 -> 黑名单 -> 价格阈值 -> 去重 -> 通知。

「新商品」判定：本轮 fetch 到、且不在**上一轮出现的 product_id 集合**中。
「去重」：storage.notified 标志保证同一商品永不重复提醒（跨重启有效）。
「临时黑名单」（v3.6）：用户人工剔除的商品（噪音/假货/非目标）在
    新商品判定后、通知前被过滤——不通知、不进 notified，但进入 prev_ids
    避免重复抓取；在 GUI「黑名单管理」中可恢复。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Config, KeywordRule
from .fetcher import MTOP_TOKEN_COOKIE, Fetcher, FetchError
from .filters import (
    filter_decision,
    hits_exclude_keywords,
    matches_required_keywords,
    product_search_text,
)
from .models import Product
from .notifier import Notifier
from .notify_policy import NotificationBuffer, NotificationPolicy
from .risk import RISK_GUARD
from .spec_match import build_spec, match_spec
from .storage import Storage

logger = logging.getLogger(__name__)

#: v1.8：Cookie 过期提醒触发状态集（共享知识 4）。
#: expiring 单独用「即将过期」文案，其余统一用「已过期/无效，请刷新」文案。
_COOKIE_ALERT_STATES = {
    "expired", "expiring", "missing", "no_token", "invalid_encrypt",
    # v1.9 新增：登录态缺失 / 会话凭据过期 —— 这两类才真的可能需要重新登录
    "session_missing", "havana_expired",
}

#: v1.8：过期/即将过期提醒的刷新指引（不含 Cookie 明文，C19）。
_COOKIE_ALERT_GUIDE = (
    "请打开 https://www.goofish.com 登录后重新获取 Cookie，"
    "并在 GUI 点击「🔄 一键刷新 Cookie」或运行 "
    "`python -m xianyu_alert.cli login` 更新。"
)

#: 抓取失败层次 → 日志中的处置指引（配合 `FetchError.kind`）。
#:
#: 三种"失效"的修复手段完全不同，而真实场景中它们会**同时出现**：
#: 2026-09-24 实测同一份 Cookie，搜索接口报 `RGV587_ERROR`（风控）、
#: 详情接口报 `FAIL_SYS_TOKEN_EXOIRED`（令牌）。此时笼统地提示
#: 「请重新登录」会把排查方向带偏 —— 风控时重登根本没用。
def _stopped(stop_event: Any | None) -> bool:
    """外部停止信号是否已置位（None 视为未置位）。

    兼容任意可注入的替身对象（测试常用 SimpleNamespace），因此这里做防御性取值。
    """
    if stop_event is None:
        return False
    try:
        return bool(stop_event.is_set())
    except Exception:  # noqa: BLE001 - 替身对象可能没有 is_set
        return False


_FETCH_FAILURE_HINTS = {
    "token": "（令牌层：服务端会自动下发新令牌并重试，连续失败才需重新登录）",
    "session": "（会话层：登录态已失效，需重新登录获取 Cookie）",
    "risk": "（风控层：**重新登录无效**，请降速 / 启用多账号轮换 / 更换出口网络）",
    "config": "（配置层：Cookie 缺失，请先配置登录 Cookie）",
}


#: 风控冷却期"保活跳过"日志的最小间隔（秒，v1.11.6）。
#: 保活判断挂在 1 秒分片睡眠上，冷却期内会每秒调用一次；不降频就会刷屏。
RISK_SKIP_LOG_INTERVAL = 300.0

#: 过滤原因的中文标签（v1.10：让日志与指标可读）
FILTER_REASON_LABELS = {
    "missing_required": "缺必含词",
    "excluded": "命中排除词",
    # v1.11 规格语义过滤（把搜索词当规格：品牌锚定 / 代际 / 频率 / 容量）
    "spec_brand": "品牌不符（或竞品先出现）",
    "spec_generation": "代际不符",
    "spec_frequency": "频率/型号不符",
    "spec_capacity": "容量不足",
    # v1.11.1：模块规格（单条容量 / 条数）——「64G 其实想买两根 32G」
    "spec_module": "单条容量不足",
    "spec_module_count": "条数不足",
}


@dataclass


class RoundResult:
    """单轮监测的统计结果（便于日志与测试断言）。"""

    fetched: int = 0
    #: 被关键词过滤规则（排除词 / 必含词）跳过的商品数（v3.1）
    filtered: int = 0
    new_products: int = 0
    notified: int = 0
    failed_keywords: list[str] = field(default_factory=list)
    notified_products: list[Product] = field(default_factory=list)
    #: 轮次序号（v1.10：轮次指标用）
    round_no: int = 0
    #: 本轮耗时（毫秒，v1.10）
    duration_ms: float = 0.0
    #: 被过滤商品的原因分布（v1.10）：missing_required / excluded -> 条数
    filtered_reasons: dict[str, int] = field(default_factory=dict)
    #: 本轮是否因**风控冷却**被整体跳过（v1.11.3）
    risk_skipped: bool = False
    #: 跳过的剩余冷却秒数（v1.11.3，仅 risk_skipped=True 时有意义）
    risk_remaining_seconds: int = 0


class Monitor:
    """闲鱼低价监测器。

    Attributes:
        config: 全局配置。
        fetcher: 抓取器。
        storage: 状态存储。
        notifiers: 通知器列表。
    """

    def __init__(
        self,
        config: Config,
        fetcher: Fetcher,
        storage: Storage,
        notifiers: list[Notifier],
        config_path: str | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        """初始化监测器。

        Args:
            config: 全局配置。
            fetcher: 抓取器实例。
            storage: 存储实例。
            notifiers: 通知器列表（可为空列表，此时只记录不通知）。
            config_path: 配置文件路径（热更用）。
            sleep_func: 关键词间限速用的 sleep 实现（测试注入 no-op，v1.11.3）。
        """
        self.config: Config = config
        self.fetcher: Fetcher = fetcher
        self._sleep: Callable[[float], None] = sleep_func or time.sleep
        self.storage: Storage = storage
        self.notifiers: list[Notifier] = list(notifiers or [])
        self._stop: bool = False
        #: 最近一轮的详细结果，便于外部读取
        self.last_result: RoundResult = RoundResult()
        #: 已执行轮数（多 Cookie 池轮换的轮次序号来源，从 0 开始递增）
        self._round_no: int = 0
        #: v1.8：上次 Cookie 健康检测的 monotonic 时间戳（节流用，0=从未检测）
        self._last_cookie_check_at: float = 0.0
        #: v1.9：最近一次已鉴权请求时间（保活与轮次都会刷新）
        self._last_auth_at: float = 0.0
        #: v1.10：配置文件路径（用于轮次边界热更；None 表示关闭热更）
        self.config_path: str | None = config_path
        #: v1.10：上次加载配置时的 mtime（热更检测）
        self._config_mtime: float = 0.0
        #: v1.10：轮次级指标环形缓冲（最近 200 轮）
        self._round_metrics: deque[dict] = deque(maxlen=200)
        if config_path:
            self._config_mtime = self._read_config_mtime()
        #: v1.10.2：通知策略（静默时段 / 聚合窗口 / 重试）与聚合缓冲
        notify_cfg = config.notify
        self.notify_policy = NotificationPolicy(
            quiet_hours=str(getattr(notify_cfg, "quiet_hours", "") or ""),
            aggregate_seconds=int(getattr(notify_cfg, "aggregate_seconds", 0) or 0),
            retry_attempts=int(getattr(notify_cfg, "retry_attempts", 1) or 1),
        )
        self._notify_buffer = NotificationBuffer(self.notify_policy)

    # ------------------------------------------------------------------ #
    def _resolve_cookie(self, round_index: int = 0) -> str:
        """按「池优先、单值兜底」策略解析本轮应使用的 Cookie（v3.2）。

        - `monitor.cookie_pool` 启用条目非空 → 轮换取用第 `round_index` 条；
        - 池为空 → 回退 `monitor.cookies` 单值字段（向后兼容）。

        Args:
            round_index: 从 0 开始的轮次序号。

        Returns:
            本轮 Cookie 字符串（可能为空串）。
        """
        from .cookie import resolve_cookie_for_round

        return resolve_cookie_for_round(self.config.monitor, round_index)

    def _apply_cookie(self, cookie_str: str) -> None:
        """把本轮 Cookie 注入 fetcher（fetcher 契约不变，仍是单 Cookie）。"""
        setter = getattr(self.fetcher, "set_cookies", None)
        if setter is None:
            return
        try:
            setter(cookie_str)
        except Exception as exc:  # noqa: BLE001 - 轮换失败不阻断监测
            logger.debug("切换 Cookie 失败（继续使用旧 Cookie）：%s", exc)

    # ------------------------------------------------------------------ #
    def _check_cookie_health_and_alert(self, round_index: int = 0) -> None:
        """周期检测「本轮将使用的 Cookie」健康并推送提醒（v1.8，去抖）。

        流程（共享知识 4/5/8，检测零网络）：
          1. fetcher 非 mtop 或 `cookie_alert_enabled=false` → no-op（零成本）；
          2. 节流：`cookie_check_interval_seconds > 0` 且距上次检测不足 → no-op；
          3. 解析本轮 Cookie → `detect_cookie_health`；
          4. 单条去抖：仅状态跃迁（含首次检测，prev=None）时
             `safe_notify_message` 推送全部通道，meta 表持久化；
             `expiring` → 「即将过期」文案；其余 → 「已过期/无效」文案；
             恢复 ok 时写回 ok（再次失效将重新提醒）；
          5. 池汇总去抖：池健康条目数 < 启用条目数 → 跃迁推送
             「池中有 N 条 Cookie 已过期/无效」；
          6. 任何异常 catch 后只 warning，不打断监测循环（C2③）。

        Args:
            round_index: 本轮使用的轮次序号（已 resolve 的 Cookie 对应序号）。
        """
        try:
            if self.config.fetcher.type != "mtop":
                return
            if not getattr(self.config.monitor, "cookie_alert_enabled", True):
                return
            throttle = int(getattr(self.config.monitor, "cookie_check_interval_seconds", 0) or 0)
            now = time.monotonic()
            if throttle > 0 and self._last_cookie_check_at > 0 and (
                now - self._last_cookie_check_at
            ) < throttle:
                return
            self._last_cookie_check_at = now

            from .cookie import (
                cookie_prefers_rotation,
                detect_cookie_health,
                pool_enabled_cookies,
            )
            from .notifier import notify_plain_message
            from .storage import _META_COOKIE_ALERT_PREFIX, _META_COOKIE_POOL_ALERT_KEY

            cookie = self._resolve_cookie(round_index)
            # 健康检测与去抖指纹以「实际账号身份」为准：当健康过滤导致 resolve
            # 返回空串时（单值/池条目已过期），回退到配置中的源 Cookie（池条目
            # 或单值）计算指纹——保证「过期 → 刷新 → 再过期」能按账号身份重新
            # 提醒（C4），而不是把所有失效都归并到空串指纹上（那会漏报第二轮）。
            #
            # 边界（QA 观察项 2）：`invalid_encrypt` 单值场景下指纹退化为空串——
            # config.py 解析阶段已把无法解密的密文丢弃为 `monitor.cookies=""`，
            # 本方法看不到密文原文（v1.7 既有行为）。用户可见结果正确：仍收到
            # missing 状态提醒、文案含刷新指引、不含明文；仅设计文档 §9 第 3 条
            # 「invalid_encrypt 用密文原文计算指纹」在单值路径无法生效。
            identity = cookie
            if not identity:
                pool_cookies = pool_enabled_cookies(self.config.monitor.cookie_pool or [])
                if pool_cookies:
                    identity = pool_cookies[int(round_index) % len(pool_cookies)]
                else:
                    identity = str(getattr(self.config.monitor, "cookies", "") or "")
            state, _reason = detect_cookie_health(identity)

            # 单条去抖 key：cookie_alert_state:<sha1(身份明文)[:12]>（共享知识 3）
            fingerprint = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
            meta_key = _META_COOKIE_ALERT_PREFIX + fingerprint

            if state in _COOKIE_ALERT_STATES:
                prev = self.storage.get_meta_value(meta_key)
                if state != prev:
                    title = "闲鱼 Cookie 即将过期" if state == "expiring" else "闲鱼 Cookie 已过期/无效"
                    notify_plain_message(self.notifiers, title, _COOKIE_ALERT_GUIDE)
                    self.storage.set_meta_value(meta_key, state)
            else:
                # 恢复 ok：写回 ok，再次失效将重新提醒
                self.storage.set_meta_value(meta_key, "ok")

            # 池汇总（C12）：池健康条目数 < 启用条目数 → degraded 跃迁提醒
            pool = self.config.monitor.cookie_pool or []
            enabled_cookies = pool_enabled_cookies(pool)
            # v1.9：降级只统计"真正需要注意"的条目 —— 令牌过期/缺失可自愈，
            # 不算降级；登录态缺失 / 会话凭据过期 / 密文无法解密才算。
            healthy_cookies = [c for c in enabled_cookies if cookie_prefers_rotation(c)]
            enabled_count = len(enabled_cookies)
            degraded = enabled_count > 0 and len(healthy_cookies) < enabled_count
            degraded_count = enabled_count - len(healthy_cookies)

            prev_pool_raw = self.storage.get_meta_value(_META_COOKIE_POOL_ALERT_KEY)
            prev_degraded = False
            if prev_pool_raw:
                try:
                    prev_pool = json.loads(prev_pool_raw)
                except json.JSONDecodeError:
                    prev_pool = {}
                prev_degraded = bool(prev_pool.get("degraded", False)) if isinstance(prev_pool, dict) else False

            if degraded != prev_degraded:
                if degraded:
                    notify_plain_message(
                        self.notifiers,
                        "闲鱼 Cookie 池部分失效",
                        f"池中有 {degraded_count} 条 Cookie 需要重新登录或刷新。\n{_COOKIE_ALERT_GUIDE}",
                    )
                self.storage.set_meta_value(
                    _META_COOKIE_POOL_ALERT_KEY,
                    json.dumps(
                        {
                            "degraded": degraded,
                            "count": degraded_count,
                            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        },
                        ensure_ascii=False,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - 检测/提醒失败不打断监测循环（C2③）
            logger.warning("Cookie 健康检测/提醒失败：%s", exc)

    # ------------------------------------------------------------------ #
    def preflight_cookie(self) -> str:
        """启动预检：检查 Cookie 是否缺失 / 过期（只警告，不阻断运行）。

        仅在 `fetcher.type == mtop` 时检查；其余抓取器直接返回空串。
        多 Cookie 池（v3.2）存在时检查「第 0 轮」将使用的池条目，
        否则检查单值 `monitor.cookies`。过期 / 缺失时输出 warning
        日志并返回提示文案。

        Returns:
            提示文案；无需提示时返回空串。
        """
        if self.config.fetcher.type != "mtop":
            return ""
        from .cookie import cookie_expiry_status, pool_enabled_cookies

        cookie = self._resolve_cookie(0)
        if not cookie:
            # v1.8：健康过滤导致 resolve 返回空串（如单值/池条目已过期）时，
            # 回退到源 Cookie 身份检测，保证预检仍提示「过期」而非「未配置」。
            pool_cookies = pool_enabled_cookies(self.config.monitor.cookie_pool or [])
            if pool_cookies:
                cookie = pool_cookies[0 % len(pool_cookies)]
            else:
                cookie = str(getattr(self.config.monitor, "cookies", "") or "")
        status = cookie_expiry_status(cookie)
        messages = {
            "missing": "未配置登录 Cookie，mtop 真实抓取将失败，请先获取 Cookie。",
            "no_token": f"Cookie 中缺少 {MTOP_TOKEN_COOKIE}，无法计算 mtop 签名，请重新登录。",
            "expired": (
                "登录令牌已过期（距上次成功请求超过有效期）—— mtop 令牌会随请求自动续期，"
                "下一次抓取通常即可恢复；若持续失败说明登录态已失效，需重新登录。"
            ),
            "expiring": "登录令牌即将过期，会在下次抓取时自动续期。",
            "unknown": "Cookie 未包含可解析的 _m_h5_tk 时间戳，无法判断是否过期。",
        }
        msg = messages.get(status, "")
        if msg:
            logger.warning("[preflight] %s", msg)
        return msg

    # ------------------------------------------------------------------ #
    def run_once(self, round_ts: datetime | None = None, log_item_details: bool = False) -> int:
        """执行一轮监测。

        多 Cookie 池（v3.2）：每轮开始时按轮次序号从池中轮换取用
        一个 enabled Cookie 注入 fetcher（池为空时回退单值 cookies），
        从而分摊多账号间的请求频率、降低单账号风控概率。

        v3.3 新增 `log_item_details`：为 True 时把**每个关键词抓取到的
        商品明细**（标题 / 价格 / 是否命中原因）逐条写入 info 日志，
        包括被过滤的（排除词命中 / 必含词缺失 / 超阈值）与已提醒过的，
        每条注明原因；默认 False 保持既有「只打概况」行为不变。

        Args:
            round_ts: 本轮时间戳，默认取当前时间。
            log_item_details: True 时逐条记录商品明细与命中/过滤原因。

        Returns:
            本轮实际触发通知的商品总数。
        """
        ts = round_ts or datetime.now()
        started_at = time.monotonic()
        # v1.11.3：风控熔断 —— 冷却期内本轮**一个请求都不发**，直接跳过。
        # 这是"风控时它自己知道安静"的最后一道闸门（保活 / 校验在架也看同一状态）。
        if RISK_GUARD.active():
            remaining = int(RISK_GUARD.remaining())
            logger.warning(
                "⛔ 风控冷却中（剩余 %d 秒），本轮不发任何请求，直接跳过；"
                "冷却结束后会自动恢复",
                remaining,
            )
            result = RoundResult(round_no=self._round_no + 1)
            result.risk_skipped = True
            result.risk_remaining_seconds = remaining
            result.duration_ms = 0.0
            self.last_result = result
            return 0
        # 轮换：本轮使用池中的第 self._round_no 条 Cookie（池为空则用单值）
        cookie = self._resolve_cookie(self._round_no)
        self._apply_cookie(cookie)
        self._last_auth_at = time.time()
        self._round_no += 1
        # v1.8：resolve 后、关键词循环前，对「本轮将使用的 Cookie」做健康检测
        # 与过期提醒（fetcher!=mtop / 开关关 → no-op；去抖见方法内部）。
        self._check_cookie_health_and_alert(self._round_no - 1)

        result = RoundResult()

        processed_any = False
        for rule in self.config.keywords:
            if not rule.enabled:
                # v3.7：停用的关键词不抓取、不计数、不打命中日志，
                # 只打一行「已停用跳过」便于用户核对当前生效范围。
                logger.info("⏸ 关键词「%s」已停用，本轮跳过（如需恢复请在配置中启用）", rule.keyword)
                continue
            if processed_any:
                # v1.11.3：关键词之间也限速。此前只有"页间 sleep"，多个关键词
                # 是背靠背请求 —— 关键词一多就是把请求堆在一起。
                self._sleep_between_keywords()
            self._process_keyword(rule, ts, result, log_item_details=log_item_details)
            processed_any = True
            if RISK_GUARD.active():
                # 命中风控后**不再碰下一个关键词**（冷却期由 run_once 开头统一拦截）
                logger.warning("⛔ 关键词「%s」命中风控，本轮不再抓取剩余关键词", rule.keyword)
                break

        # 轮末刷新：静默时段结束或聚合窗口到期后，把攒下的命中发出去（v1.10.2）
        flushed = self.flush_notifications(ts)
        if flushed:
            result.notified += len(flushed)
            result.notified_products.extend(flushed)
        self.last_result = result
        result.round_no = self._round_no
        result.duration_ms = (time.monotonic() - started_at) * 1000.0
        self._round_metrics.append({
            "round": result.round_no,
            "duration_ms": round(result.duration_ms, 1),
            "fetched": result.fetched,
            "filtered": result.filtered,
            "filtered_reasons": dict(result.filtered_reasons),
            "new_products": result.new_products,
            "notified": result.notified,
            "failed_keywords": list(result.failed_keywords),
        })
        logger.info(
            "✅ 本轮完成（耗时 %.1fs）：抓取 %d 个，新商品 %d 个，通知 %d 个，失败关键词 %s",
            result.duration_ms / 1000.0,
            result.fetched,
            result.new_products,
            result.notified,
            result.failed_keywords or "无",
        )
        return result.notified

    # ------------------------------------------------------------------ #
    def deliver_hits(self, hits: list[Product], ts: datetime) -> list[Product]:
        """投递命中商品（v1.10.2：按策略静默 / 聚合 / 重试）。

        策略未启用时与改造前完全一致：立刻逐渠道发送并标记已提醒。
        启用后先入缓冲，由 flush_notifications 在「窗口到期且不在静默时段」时统一投递；
        因此**通知条数**的归属轮次可能后移（缓冲跨轮），指标以实际投递轮次为准。
        """
        if not hits:
            return []
        if not self.notify_policy.enabled:
            return self._send_and_mark(hits, ts)
        self._notify_buffer.add(hits)
        return self.flush_notifications(ts)

    def flush_notifications(self, ts: datetime | None = None) -> list[Product]:
        """把到期的聚合缓冲发出去（静默时段内不发）。"""
        pending = self._notify_buffer.flush_if_due()
        if not pending:
            return []
        return self._send_and_mark(pending, ts or datetime.now())

    def _send_and_mark(self, products: list[Product], ts: datetime) -> list[Product]:
        """逐渠道发送（带重试）并标记已提醒。"""
        for notifier in self.notifiers:
            notifier.safe_notify(products)
        for product in products:
            self.storage.mark_notified(product, ts)
        return products

    def _process_keyword(
        self,
        rule: KeywordRule,
        ts: datetime,
        result: RoundResult,
        log_item_details: bool = False,
    ) -> None:
        """处理单个关键词的完整流程。

        Args:
            rule: 关键词规则（关键词 + 价格阈值）。
            ts: 本轮时间戳。
            result: 累计统计结果（原地更新）。
            log_item_details: True 时逐条记录商品明细与命中/过滤原因。
        """
        keyword = rule.keyword
        # v3.4：把价格阈值注入 fetcher，让 mtop 接口在服务端按
        # `priceRange:0,{max_price};` 筛选，与网页「最新发布+价格<阈值」
        # 的结果一致（避免抓全量最新再本地过滤导致的低价新品丢失）。
        setter = getattr(self.fetcher, "set_max_price", None)
        if setter is not None:
            try:
                setter(rule.max_price)
            except Exception as exc:  # noqa: BLE001 - 阈值注入失败不阻断抓取
                logger.warning("关键词「%s」注入价格上限失败：%s", keyword, exc)
        try:
            products: list[Product] = self.fetcher.fetch(keyword) or []
        except FetchError as exc:
            # 按失败层次给出**正确的**处置指引，而不是一律"请重新登录"
            hint = _FETCH_FAILURE_HINTS.get(getattr(exc, "kind", ""), "")
            logger.warning("关键词「%s」抓取失败%s：%s", keyword, hint, exc)
            result.failed_keywords.append(keyword)
            if getattr(exc, "kind", "") == "risk":
                until = RISK_GUARD.note_risk(self.config.monitor.interval_seconds, str(exc))
                logger.warning(
                    "⛔ 观测到闲鱼风控：熔断已开启，冷却 %d 秒（到 %s）；"
                    "冷却期内监控 / 保活 / 校验在架一律静默",
                    int(RISK_GUARD.remaining()),
                    datetime.fromtimestamp(until).strftime("%H:%M:%S"),
                )
            return
        except Exception as exc:  # noqa: BLE001 - 任何抓取异常都不应中断其它关键词
            logger.warning("关键词「%s」抓取时发生未预期错误：%s", keyword, exc)
            result.failed_keywords.append(keyword)
            return

        # 补齐 keyword 字段（Mock/Web 抓取器一般已填，这里做兜底）
        for product in products:
            if not product.keyword:
                product.keyword = keyword

        result.fetched += len(products)
        # 抓取成功 → 连续风控计数清零（v1.11.3）
        RISK_GUARD.note_success()

        # 1.5) 关键词过滤（v3.1）：必含词缺失 / 排除词命中 → 跳过。
        #      过滤是业务规则，发生在 fetcher 返回后、阈值检查前；
        #      被过滤的商品不进入「新商品」判定与已见记录。
        # v1.11：规格语义过滤（把搜索词本身当规格）。关闭时不构造 spec，行为与旧版一致。
        spec = build_spec(keyword) if rule.spec_filter else None
        filtered_products: list[Product] = []
        for product in products:
            decision = filter_decision(product, rule.required_keywords, rule.exclude_keywords, spec)
            if decision.passed:
                filtered_products.append(product)
            else:
                result.filtered_reasons[decision.reason] = (
                    result.filtered_reasons.get(decision.reason, 0) + 1
                )
        skipped = len(products) - len(filtered_products)
        result.filtered += skipped
        if skipped:
            # v1.10：不只说"跳过了几个"，还说清"因为什么被跳过"
            breakdown = "、".join(
                FILTER_REASON_LABELS.get(k, k) + " " + str(v)
                for k, v in sorted(result.filtered_reasons.items())
            )
            logger.info(
                "关键词「%s」：按过滤规则跳过 %d 个商品（%s）", keyword, skipped, breakdown
            )

        # 1) 与上一轮对比，筛出「新出现」的商品
        previous_ids: set[str] = self.storage.get_previous_round_ids(keyword)
        new_products: list[Product] = [
            p for p in filtered_products if p.product_id not in previous_ids
        ]

        # 1.6) 临时黑名单（v3.6）：用户人工剔除的商品（噪音/假货/非目标）
        #       在新商品判定后、通知前过滤——黑名单商品不通知、不进 notified；
        #       但仍留在 filtered_products 中，从而进入本轮 prev_ids，
        #       避免之后每一轮都把它当「新商品」重复抓取/重复判定。
        blacklisted_skipped = sum(
            1 for p in new_products if self.storage.is_blacklisted(p.product_id)
        )
        if blacklisted_skipped:
            logger.info(
                "🚫 关键词「%s」：黑名单商品跳过 %d 个（不提醒、不进提醒记录）",
                keyword, blacklisted_skipped,
            )
        new_products = [
            p for p in new_products if not self.storage.is_blacklisted(p.product_id)
        ]
        result.new_products += len(new_products)

        # 2) 价格阈值 + 去重（notified 标志）
        hits: list[Product] = [
            p
            for p in new_products
            if p.price < rule.max_price and not self.storage.is_notified(keyword, p.product_id)
        ]

        # v3.7：命中低价时给概况行加 🔔 前缀，GUI 日志区自动高亮为醒目蓝色
        hit_prefix = "🔔 " if hits else ""
        logger.info(
            "%s关键词「%s」：抓取 %d，过滤 %d，新出现 %d，命中阈值(<%.2f)且未提醒 %d",
            hit_prefix, keyword, len(products), skipped, len(new_products), rule.max_price, len(hits),
        )

        # v3.3：明细日志开关（仅展示符合的低价 = 取消勾选时逐条列出）。
        # 按业务优先级给每条商品标注原因：
        #   必含词缺失 → 排除词命中 → 超阈值 → 上一轮已出现 → 已提醒过 → 命中低价。
        if log_item_details:
            for product in products:
                reason = self._item_reason(product, rule, previous_ids)
                logger.info(
                    "  [明细] %s %s —— %s", reason, product.price_text, product.title,
                )

        # 3) 发送通知（任一通道失败都不影响其它通道与后续流程）
        if hits:
            delivered = self.deliver_hits(hits, ts)
            result.notified += len(delivered)
            result.notified_products.extend(delivered)

        # 4) 记录本轮全部商品 + 更新上一轮 ID 集合（只记录通过过滤的商品）
        for product in filtered_products:
            if not self.storage.is_notified(keyword, product.product_id):
                self.storage.save_seen(product, ts)
        self.storage.set_previous_round_ids(keyword, {p.product_id for p in filtered_products})

    # ------------------------------------------------------------------ #
    def _item_reason(
        self,
        product: Product,
        rule: KeywordRule,
        previous_ids: set[str],
    ) -> str:
        """给单条商品标注「是否命中 / 被过滤原因」（v3.3 明细日志用）。

        Args:
            product: 待标注的商品。
            rule: 当前关键词规则。
            previous_ids: 上一轮出现的 product_id 集合（用于「不重复」标注）。

        Returns:
            原因文案，如「✅ 命中低价」「⛔ 排除词命中」。
        """
        if self.storage.is_blacklisted(product.product_id):
            return "🚫 已加入黑名单（人工剔除）"
        text = product_search_text(product)
        if rule.required_keywords and not matches_required_keywords(text, rule.required_keywords):
            return "⛔ 必含词缺失"
        if rule.exclude_keywords and hits_exclude_keywords(text, rule.exclude_keywords):
            return "⛔ 排除词命中"
        # v1.11：规格语义过滤（与 _process_keyword 同一套判定，保证日志口径一致）
        if rule.spec_filter:
            spec = build_spec(rule.keyword)
            if not spec.empty:
                spec_decision = match_spec(text, spec)
                if not spec_decision.passed:
                    return "⛔ 规格不符：" + spec_decision.detail
        if product.price >= rule.max_price:
            return "⏭ 超阈值"
        if product.product_id in previous_ids:
            return "🔁 上一轮已出现（不重复）"
        if self.storage.is_notified(rule.keyword, product.product_id):
            return "🔁 已提醒过"
        return "✅ 命中低价"

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        """请求停止 run_forever 循环。"""
        self._stop = True

    def keepalive_once(self) -> bool:
        """发一次轻量抓取以维持令牌滑动续期（不写库、不通知）。

        v1.9 新增。_m_h5_tk 只在"有请求"时滑动续期，空闲超过有效期就会过期；
        本方法由 CookieKeeper 线程按间隔调用，把滑动窗口接续下去。

        Returns:
            True 表示本次请求成功（令牌已续期）。
        """
        rules = [r for r in self.config.keywords if str(getattr(r, "keyword", "") or "")]
        if not rules:
            return False
        # v1.11.5：优先挑**启用中**的关键词（此前固定 keywords[0]，若它被用户停用，
        # 保活就等于对着"明确不想抓"的商品猛抓 —— 线上日志实测到 4080S 32G 被反复探测）。
        enabled = [r for r in rules if bool(getattr(r, "enabled", True))]
        keyword = str(getattr((enabled or rules)[0], "keyword", "") or "")
        if RISK_GUARD.active():
            # v1.11.3：风控冷却期内保活也静默（保活本质也是一次真实抓取，
            # 在被限流时继续打点只会延长处罚）。
            # v1.11.6：这条日志必须**降频**。保活判断挂在 1 秒分片睡眠上
            # （_interruptible_sleep 每片都调 _maybe_keepalive），冷却期内会每秒
            # 调到这里 —— 线上实测直接刷屏（86400 行/天），把真正的信号淹没。
            self._log_risk_skip()
            return False
        cookie = self._resolve_cookie(0)
        if not cookie:
            logger.warning("Cookie 保活跳过：当前没有可用的 Cookie")
            return False
        self._apply_cookie(cookie)
        # v1.11.5：保活只为续期令牌，**只抓 1 页**（此前沿用 fetcher.pages，默认 3 页，
        # 等于每次保活都多发 2 个请求）。单线程调用，临时覆盖后 finally 还原。
        pages_backup = getattr(self.fetcher, "pages", None)
        try:
            if pages_backup is not None:
                # 只有多页抓取器（mtop / web）才有 pages 属性
                self.fetcher.pages = 1  # type: ignore[attr-defined]
            self.fetcher.fetch(keyword)
        except Exception as exc:  # noqa: BLE001 - 保活失败不影响主流程
            logger.warning("Cookie 保活请求失败：%s", exc)
            return False
        finally:
            if pages_backup is not None:
                self.fetcher.pages = pages_backup  # type: ignore[attr-defined]
        self._last_auth_at = time.time()
        logger.info("Cookie 保活成功（%s），令牌已滑动续期", keyword)
        return True

    def auth_snapshot(self) -> dict[str, float | int | bool]:
        """保活线程所需的配置快照（开关 / 间隔 / 最近鉴权时间）。"""
        monitor_cfg = self.config.monitor
        return {
            "enabled": bool(getattr(monitor_cfg, "keepalive_enabled", True)),
            "interval": int(getattr(monitor_cfg, "keepalive_interval_seconds", 1800) or 0),
            "last_auth_at": self._last_auth_at,
        }

    def _read_config_mtime(self) -> float:
        """读取配置文件 mtime（读不到返回 0）。"""
        if not self.config_path:
            return 0.0
        try:
            return os.path.getmtime(self.config_path)
        except OSError:
            return 0.0

    def reload_if_config_changed(self) -> bool:
        """轮次边界热更：配置文件被外部改动时重新加载。

        v1.10（M04）：此前过滤规则（排除词 / 必含词）改动后，必须重启或整体重载才生效；
        现在每个轮次边界检测一次 mtime，变了就就地重载——**下一轮立即按新规则过滤**。

        Returns:
            True 表示本次确实重载了。
        """
        if not self.config_path:
            return False
        mtime = self._read_config_mtime()
        if not mtime or mtime == self._config_mtime:
            return False
        try:
            from .config import load_config

            fresh = load_config(self.config_path)
        except Exception as exc:  # noqa: BLE001 - 热更失败不能影响正在跑的循环
            logger.warning("配置热更失败（沿用旧配置）：%s", exc)
            return False
        self.config = fresh
        self._config_mtime = mtime
        logger.info(
            "🔄 配置已热更：关键词 %d 个（排除词 / 必含词 / 阈值下一轮生效）",
            len(fresh.keywords),
        )
        return True

    def _maybe_keepalive(self) -> bool:
        """按需保活（v1.10：**与轮次同一个时间源**）。

        此前保活由 Service 层独立线程按自己的节拍驱动，主循环又按 interval 睡眠，
        两套节奏并存、看代码时很难判断"到底什么时候会发请求"。现在保活判断
        直接挂在主循环上（轮次之间与分片睡眠期间都会检查），只有一个时间源。
        """
        from .keepalive import keepalive_due

        snap = self.auth_snapshot()
        if not keepalive_due(
            now=time.time(),
            last_auth_at=self._last_auth_at,
            interval=int(snap["interval"] or 0),
            enabled=bool(snap["enabled"]),
        ):
            return False
        return self.keepalive_once()

    def _log_risk_skip(self) -> None:
        """风控冷却期的保活跳过日志降频（v1.11.6，每 5 分钟最多一条）。"""
        now = time.time()
        last = float(getattr(self, "_risk_skip_log_at", 0.0) or 0.0)
        if now - last < RISK_SKIP_LOG_INTERVAL:
            return
        self._risk_skip_log_at = now
        logger.warning("Cookie 保活跳过：风控冷却中（剩余 %d 秒）", int(RISK_GUARD.remaining()))

    def _sleep_between_keywords(self) -> None:
        """关键词之间的限速（v1.11.3）。

        页间限速（fetcher.page_sleep）只覆盖「同一个关键词的多页」；多个关键词之间
        原本是背靠背请求 —— 关键词一多，请求就被堆在同一瞬间。这里补上同一档限速，
        并把单次等待限幅到 5 秒，避免拖长 monitor 线程的停止响应时间。

        注意：只有真正打闲鱼的抓取器（mtop / web）才需要限速 —— mock / stub 是
        离线假数据，限速只是白白拖慢测试与演示。
        """
        if str(getattr(self.fetcher, "name", "")) not in ("mtop", "web"):
            return
        pause = float(getattr(self.config.fetcher, "page_sleep", 0.0) or 0.0)
        pause = min(max(pause, 0.0), 5.0)
        if pause <= 0:
            return
        logger.debug("关键词间限速：%.1fs", pause)
        self._sleep(pause)

    def _interruptible_sleep(self, seconds: float, stop_event: Any | None = None) -> bool:
        """分片睡眠：期间响应停止信号（含外部 stop_event）并做保活检查。

        Args:
            seconds: 目标睡眠时长。
            stop_event: 外部停止信号；提供时用 Event.wait 实现**即时**唤醒。

        Returns:
            True 表示睡满了（可继续下一轮）；False 表示期间收到停止信号。
        """
        remaining = max(0.0, float(seconds))
        while remaining > 0 and not self._stop and not _stopped(stop_event):
            slice_seconds = min(1.0, remaining)
            if stop_event is not None:
                if stop_event.wait(slice_seconds):
                    break
            else:
                # v1.11.3：走可注入的 sleep 实现，便于测试免等待（与关键词间限速同一入口）
                self._sleep(slice_seconds)
            remaining -= slice_seconds
            self._maybe_keepalive()
        return not self._stop and not _stopped(stop_event)

    def metrics(self) -> dict:
        """轮次级指标快照（v1.10：耗时 / 抓取 / 过滤原因 / 命中）。

        Returns:
            形如 {"rounds": [...最近 200 轮...], "last": {...}, "totals": {...}}。
        """
        rounds = list(self._round_metrics)
        return {
            "rounds": rounds,
            "last": rounds[-1] if rounds else None,
            "totals": {
                "rounds": len(rounds),
                "fetched": sum(r["fetched"] for r in rounds),
                "filtered": sum(r["filtered"] for r in rounds),
                "new_products": sum(r["new_products"] for r in rounds),
                "notified": sum(r["notified"] for r in rounds),
                "failed_keywords": sum(len(r["failed_keywords"]) for r in rounds),
                "avg_duration_ms": round(
                    sum(r["duration_ms"] for r in rounds) / len(rounds), 1
                ) if rounds else 0.0,
            },
        }

    def run_forever(
        self,
        max_rounds: int | None = None,
        stop_event: Any | None = None,
        on_round: Callable[[int], None] | None = None,
    ) -> int:
        """按配置间隔持续运行监测循环。

        单轮内部的异常会被捕获并记录，循环不会因此退出；
        收到 KeyboardInterrupt 时优雅退出。

        v1.10（M05 单一时间源）：本循环是唯一的时间节奏 —— 轮次间隔、保活节拍、
        配置热更、停止信号都在同一个循环里处理。Web 服务原先自己写
        stop_event.wait(interval) 循环，现改为传入 stop_event 复用本方法，
        于是 CLI / GUI / Web 三条路径的时间行为完全一致。

        Args:
            max_rounds: 最多运行多少轮，None 表示无限（测试可传有限值）。
            stop_event: 外部停止信号（threading.Event）；置位后立即唤醒并退出。
            on_round: 每轮结束后的回调，参数为本轮通知数（服务层累计指标用）。

        Returns:
            累计通知的商品总数。
        """
        interval = self.config.monitor.interval_seconds
        total_notified = 0
        round_no = 0
        self._stop = False

        logger.info(
            "监测已启动：关键词 %s，间隔 %d 秒，抓取器 %s，通知通道 %s",
            [r.keyword for r in self.config.keywords],
            interval,
            getattr(self.fetcher, "name", type(self.fetcher).__name__),
            [n.name for n in self.notifiers] or ["无"],
        )

        # 启动预检：Cookie 过期 / 缺失时输出 warning（不阻断运行）
        self.preflight_cookie()

        try:
            while not self._stop and not _stopped(stop_event):
                # v1.10：轮次边界先热更配置（过滤规则/阈值改动立即生效）与按需保活
                if self.reload_if_config_changed():
                    interval = self.config.monitor.interval_seconds
                self._maybe_keepalive()
                round_no += 1
                logger.info("===== 第 %d 轮监测开始 =====", round_no)
                try:
                    notified = self.run_once()
                    total_notified += notified
                    if on_round is not None:
                        on_round(notified)
                except Exception as exc:  # noqa: BLE001 - 保证长期运行不被单轮异常打断
                    logger.exception("第 %d 轮监测异常，已跳过：%s", round_no, exc)

                if max_rounds is not None and round_no >= max_rounds:
                    break
                if self._stop:
                    break
                # 分片睡眠：等待期间同样能停、能保活（v1.10 单一时间源）
                if not self._interruptible_sleep(interval, stop_event=stop_event):
                    break
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，正在退出……")

        logger.info("监测结束，共运行 %d 轮，累计通知 %d 个商品", round_no, total_notified)
        return total_notified

    # ------------------------------------------------------------------ #
    def summary(self) -> dict[str, int]:
        """返回累计统计信息（已提醒商品总数等）。"""
        return {"total_notified": self.storage.count_notified()}
