"""核心监测循环：抓取 -> 筛选新商品 -> 黑名单 -> 价格阈值 -> 去重 -> 通知。

「新商品」判定：本轮 fetch 到、且不在**上一轮出现的 product_id 集合**中。
「去重」：storage.notified 标志保证同一商品永不重复提醒（跨重启有效）。
「临时黑名单」（v3.6）：用户人工剔除的商品（噪音/假货/非目标）在
    新商品判定后、通知前被过滤——不通知、不进 notified，但进入 prev_ids
    避免重复抓取；在 GUI「黑名单管理」中可恢复。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

from .config import Config, KeywordRule
from .fetcher import FetchError, Fetcher, MTOP_TOKEN_COOKIE
from .filters import (
    hits_exclude_keywords,
    matches_required_keywords,
    product_passes_filter,
    product_search_text,
)
from .models import Product
from .notifier import Notifier
from .storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class RoundResult:
    """单轮监测的统计结果（便于日志与测试断言）。"""

    fetched: int = 0
    #: 被关键词过滤规则（排除词 / 必含词）跳过的商品数（v3.1）
    filtered: int = 0
    new_products: int = 0
    notified: int = 0
    failed_keywords: List[str] = field(default_factory=list)
    notified_products: List[Product] = field(default_factory=list)


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
        notifiers: List[Notifier],
    ) -> None:
        """初始化监测器。

        Args:
            config: 全局配置。
            fetcher: 抓取器实例。
            storage: 存储实例。
            notifiers: 通知器列表（可为空列表，此时只记录不通知）。
        """
        self.config: Config = config
        self.fetcher: Fetcher = fetcher
        self.storage: Storage = storage
        self.notifiers: List[Notifier] = list(notifiers or [])
        self._stop: bool = False
        #: 最近一轮的详细结果，便于外部读取
        self.last_result: RoundResult = RoundResult()
        #: 已执行轮数（多 Cookie 池轮换的轮次序号来源，从 0 开始递增）
        self._round_no: int = 0

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
        from .cookie import cookie_expiry_status

        cookie = self._resolve_cookie(0)
        status = cookie_expiry_status(cookie)
        messages = {
            "missing": "未配置登录 Cookie，mtop 真实抓取将失败，请先获取 Cookie。",
            "no_token": f"Cookie 中缺少 {MTOP_TOKEN_COOKIE}，无法计算 mtop 签名，请重新登录。",
            "expired": "Cookie 已过期（_m_h5_tk 时间戳超过 24 小时），请重新登录获取新 Cookie。",
            "expiring": "Cookie 即将过期（剩余不足 1 小时），建议尽快重新登录。",
            "unknown": "Cookie 未包含可解析的 _m_h5_tk 时间戳，无法判断是否过期。",
        }
        msg = messages.get(status, "")
        if msg:
            logger.warning("[preflight] %s", msg)
        return msg

    # ------------------------------------------------------------------ #
    def run_once(self, round_ts: Optional[datetime] = None, log_item_details: bool = False) -> int:
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
        # 轮换：本轮使用池中的第 self._round_no 条 Cookie（池为空则用单值）
        cookie = self._resolve_cookie(self._round_no)
        self._apply_cookie(cookie)
        self._round_no += 1

        result = RoundResult()

        for rule in self.config.keywords:
            if not rule.enabled:
                # v3.7：停用的关键词不抓取、不计数、不打命中日志，
                # 只打一行「已停用跳过」便于用户核对当前生效范围。
                logger.info("⏸ 关键词「%s」已停用，本轮跳过（如需恢复请在配置中启用）", rule.keyword)
                continue
            self._process_keyword(rule, ts, result, log_item_details=log_item_details)

        self.last_result = result
        logger.info(
            "✅ 本轮完成：抓取 %d 个，新商品 %d 个，通知 %d 个，失败关键词 %s",
            result.fetched,
            result.new_products,
            result.notified,
            result.failed_keywords or "无",
        )
        return result.notified

    # ------------------------------------------------------------------ #
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
            products: List[Product] = self.fetcher.fetch(keyword) or []
        except FetchError as exc:
            logger.warning("关键词「%s」抓取失败：%s", keyword, exc)
            result.failed_keywords.append(keyword)
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

        # 1.5) 关键词过滤（v3.1）：必含词缺失 / 排除词命中 → 跳过。
        #      过滤是业务规则，发生在 fetcher 返回后、阈值检查前；
        #      被过滤的商品不进入「新商品」判定与已见记录。
        filtered_products: List[Product] = [
            p
            for p in products
            if product_passes_filter(p, rule.required_keywords, rule.exclude_keywords)
        ]
        skipped = len(products) - len(filtered_products)
        result.filtered += skipped
        if skipped:
            logger.info(
                "关键词「%s」：按过滤规则（排除词 / 必含词）跳过 %d 个商品",
                keyword, skipped,
            )

        # 1) 与上一轮对比，筛出「新出现」的商品
        previous_ids: Set[str] = self.storage.get_previous_round_ids(keyword)
        new_products: List[Product] = [
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
        hits: List[Product] = [
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
            for notifier in self.notifiers:
                notifier.safe_notify(hits)
            for product in hits:
                self.storage.mark_notified(product, ts)
            result.notified += len(hits)
            result.notified_products.extend(hits)

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
        previous_ids: Set[str],
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

    def run_forever(self, max_rounds: Optional[int] = None) -> int:
        """按配置间隔持续运行监测循环。

        单轮内部的异常会被捕获并记录，循环不会因此退出；
        收到 KeyboardInterrupt 时优雅退出。

        Args:
            max_rounds: 最多运行多少轮，None 表示无限（测试可传有限值）。

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
            while not self._stop:
                round_no += 1
                logger.info("===== 第 %d 轮监测开始 =====", round_no)
                try:
                    total_notified += self.run_once()
                except Exception as exc:  # noqa: BLE001 - 保证长期运行不被单轮异常打断
                    logger.exception("第 %d 轮监测异常，已跳过：%s", round_no, exc)

                if max_rounds is not None and round_no >= max_rounds:
                    break
                if self._stop:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，正在退出……")

        logger.info("监测结束，共运行 %d 轮，累计通知 %d 个商品", round_no, total_notified)
        return total_notified

    # ------------------------------------------------------------------ #
    def summary(self) -> Dict[str, int]:
        """返回累计统计信息（已提醒商品总数等）。"""
        return {"total_notified": self.storage.count_notified()}
