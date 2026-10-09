"""在架/已售校验（ShelfCheckMixin，v1.9.7 从 MonitorService 拆出）。"""

from __future__ import annotations

import contextlib
import logging
import threading
from datetime import datetime
from typing import Any

from xianyu_alert import gui, secure  # noqa: F401  # gui 防御性导入 tkinter，容器可 import
from xianyu_alert.fetcher import build_fetcher

from .constants import (
    CHECK_SHELF_ITEM_TIMEOUT,
    SOLD_CHECK_INTERVAL,
    SOLD_CHECK_MAX_ITEMS,
    SOLD_REASON_DETAIL,
)

logger = logging.getLogger(__name__)


class ShelfCheckMixin:
    # 下列属性/方法由宿主 MonitorService（__init__ 或其它 mixin）提供：
    # mixin 与宿主共享同一实例状态，这里声明仅为类型可见性，不做初始化。
    _check_shelf_lock: Any
    _check_shelf_thread: Any
    _check_shelf_cancel: Any
    _check_shelf_state: Any
    _config: Any
    _lock: Any
    _storage: Any
    _thread: Any
    def start_check_shelf(self, product_ids: list[str]) -> dict[str, Any]:
        """启动校验在架批处理（异步线程，202 语义）。

        校验顺序与 GUI `on_check_on_shelf`（gui.py:3325-3368）一致：
            1. ids 空 → 400；
            2. fetcher 非 mtop → 400「校验在架仅支持 mtop 抓取方式」；
            3. monitor 线程运行中 → 409「监测正在运行中，请先停止后再校验在架状态」；
            4. 已有批处理运行 → 409「已有校验任务正在执行」；
            5. 超 `SOLD_CHECK_MAX_ITEMS` → 截断到 30 并打日志。

        Returns:
            成功：{"ok": True, "accepted": True, "count": N}；
            失败：{"ok": False, "message": str, "code": int}。
        """
        ids: list[str] = []
        for pid in product_ids or []:
            p = str(pid or "").strip()
            if p:
                ids.append(p)
        if not ids:
            return {"ok": False, "message": "请先选择要校验的商品", "code": 400}

        with self._lock:
            monitor_running = self._thread is not None and self._thread.is_alive()
            fetcher_type = self._config.fetcher.type if self._config else ""
        if monitor_running:
            return {"ok": False, "message": "监测正在运行中，请先停止后再校验在架状态", "code": 409}
        if fetcher_type != "mtop":
            return {"ok": False, "message": "校验在架仅支持 mtop 抓取方式", "code": 400}

        if len(ids) > SOLD_CHECK_MAX_ITEMS:
            logger.info(
                "校验在架商品数 %d 超过上限 %d，已截断",
                len(ids),
                SOLD_CHECK_MAX_ITEMS,
            )
            ids = ids[:SOLD_CHECK_MAX_ITEMS]

        with self._check_shelf_lock:
            if self._check_shelf_thread is not None and self._check_shelf_thread.is_alive():
                return {"ok": False, "message": "已有校验任务正在执行", "code": 409}
            cancel_event = threading.Event()
            self._check_shelf_cancel = cancel_event
            self._check_shelf_state = {
                "running": True,
                "total": len(ids),
                "done": 0,
                "sold": 0,
                "unknown": 0,
                "cancelled": False,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": None,
            }
            thread = threading.Thread(
                target=self._check_shelf_worker,
                args=(list(ids), cancel_event),
                name="xianyu-web-check-shelf",
                daemon=True,
            )
            self._check_shelf_thread = thread
            thread.start()
        logger.info("校验在架任务已启动：%d 个商品", len(ids))
        return {"ok": True, "accepted": True, "count": len(ids)}

    def cancel_check_shelf(self) -> dict[str, Any]:
        """请求中止校验批处理（worker 在限速等待处唤醒退出）。"""
        with self._check_shelf_lock:
            cancel_event = self._check_shelf_cancel
            running = (
                self._check_shelf_thread is not None and self._check_shelf_thread.is_alive()
            )
        if not running:
            return {"ok": True, "cancelled": False, "message": "当前没有正在执行的校验任务"}
        if cancel_event is not None:
            cancel_event.set()
        logger.info("已请求中止校验在架任务")
        return {"ok": True, "cancelled": True, "message": "已请求中止校验任务"}

    def check_shelf_status(self) -> dict[str, Any]:
        """返回校验批处理进度（前端轮询 2s 用）。"""
        with self._check_shelf_lock:
            return dict(self._check_shelf_state)

    def _check_shelf_worker(self, ids: list[str], cancel_event: threading.Event) -> None:
        """后台线程：build_fetcher(config) → 逐条 check_item_status(pid, timeout=12.0)。

        - 判 False → `mark_sold_out_by_id(pid, reason=SOLD_REASON_DETAIL)` + sold+1；
        - 判 True → done+1（日志「✅ 在架」）；
        - None/异常 → unknown+1（WARNING 继续，不中断批量）；
        - 两条请求间 `cancel_event.wait(SOLD_CHECK_INTERVAL)`，被取消则 break；
        - 全程 logger.info 写进度（环形缓冲 + SSE 可见）；finally fetcher.close() + 收尾状态。
        """
        fetcher = None
        try:
            with self._lock:
                config = self._config
            if config is None:
                logger.error("校验在架失败：配置尚未加载")
                return
            fetcher = build_fetcher(config)
            logger.info(
                "开始校验 %d 个商品的在架状态（每次间隔 %gs 限速）…",
                len(ids),
                SOLD_CHECK_INTERVAL,
            )
            done = sold = unknown = 0
            for pid in ids:
                if cancel_event.is_set():
                    logger.info("校验在架已收到中止请求，正在退出…")
                    break
                try:
                    _check_status = getattr(fetcher, "check_item_status", None)
                    status = (
                        _check_status(pid, timeout=CHECK_SHELF_ITEM_TIMEOUT)
                        if callable(_check_status)
                        else None
                    )
                except Exception as exc:  # noqa: BLE001 - 单条异常不中断批量
                    status = None
                    logger.warning(
                        "⚠️ 商品 %s 在架状态无法判定（异常：%s，跳过，未标记）", pid, exc
                    )
                if status is False:
                    try:
                        with self._lock:
                            storage = self._storage
                        if storage is not None:
                            storage.mark_sold_out_by_id(pid, reason=SOLD_REASON_DETAIL)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("商品 %s 标记售出失败：%s", pid, exc)
                    sold += 1
                    logger.info("🚫 商品（%s）已下架/售出，已标记", pid)
                elif status is True:
                    done += 1
                    logger.info("✅ 商品（%s）在架", pid)
                else:
                    unknown += 1
                    logger.warning("⚠️ 商品 %s 在架状态无法判定（跳过，未标记）", pid)
                with self._check_shelf_lock:
                    st = self._check_shelf_state
                    st["done"] = done
                    st["sold"] = sold
                    st["unknown"] = unknown
                if cancel_event.wait(SOLD_CHECK_INTERVAL):
                    break
            cancelled = bool(cancel_event.is_set())
            logger.info(
                "校验在架完成：共 %d 条，标记售出 %d，无法判定 %d（被取消：%s）",
                len(ids),
                sold,
                unknown,
                "是" if cancelled else "否",
            )
        finally:
            if fetcher is not None:
                with contextlib.suppress(Exception):
                    fetcher.close()
            with self._check_shelf_lock:
                st = self._check_shelf_state
                st["running"] = False
                st["cancelled"] = bool(cancel_event.is_set())
                st["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._check_shelf_thread = None
                self._check_shelf_cancel = None

    # ------------------------------------------------------------------ #
    # P2-01：Cookie 池管理（读明文/写密文 + reload R1/R4）
    # ------------------------------------------------------------------ #
