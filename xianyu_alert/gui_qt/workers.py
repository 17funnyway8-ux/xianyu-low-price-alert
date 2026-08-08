"""Qt 后台线程与日志桥接（线程铁律：后台线程绝不触碰任何 Qt 控件）。

设计对齐（macOS 适配设计文档 §3.1.2）：
    - `MonitorWorker(QThread)`：`run()` 为 Tk 版 `_monitor_worker` 的移植，
      循环体 / 停止事件 / 单轮逻辑逐行保留，仅把 `self._push(...)` 换成
      `self.ui_message.emit(kind, payload)`；
    - `ui_message = Signal(str, object)`：跨线程自动 queued 投递到主线程槽函数，
      **不再需要 queue + after 轮询**；
    - `QtLogHandler(logging.Handler)` 持有 `LogBridge(QObject)`：logging 来自任意
      线程，`emit()` → `bridge.message.emit(level, text)`（Qt 信号跨线程安全）；
    - 优雅关闭：`request_stop()`（stop_event.set）→ `wait(CLOSE_JOIN_TIMEOUT)`。

**线程铁律**：后台线程（MonitorWorker / logging 线程）**绝不访问任何 Qt 控件**；
所有控件状态在主线程一次性读取为普通值传给后台线程；控件更新只发生在主线程槽函数。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from ..config import Config
from ..fetcher import build_fetcher
from ..models import Product
from ..monitor import Monitor
from ..notifier import build_notifiers
from ..storage import Storage

logger = logging.getLogger(__name__)

#: 关闭窗口时等待监控线程收尾的最大秒数（对齐 Tk 版 CLOSE_JOIN_TIMEOUT）
CLOSE_JOIN_TIMEOUT = 5.0


class MonitorWorker(QThread):
    """后台监控线程：运行 monitor 循环，通过信号把消息投递回主线程。

    Signals:
        ui_message(str, object): kind ∈ {"log", "alert", "status", "state", "message"}，
            payload 与 Tk 版 `_push` 语义一致（便于测试与主线程分发复用）。
    """

    ui_message = Signal(str, object)

    def __init__(
        self,
        config: Config,
        single_round: bool,
        detail_only: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        """初始化。

        Args:
            config: 主线程已校验的配置对象（纯本地值，不引用任何控件）。
            single_round: True 只跑一轮；False 按间隔循环。
            detail_only: True 时 monitor 只记录概况与命中明细。
            parent: QObject 父对象。
        """
        super().__init__(parent)
        self._config = config
        self._single_round = single_round
        self._detail_only = detail_only
        self._stop_event = threading.Event()
        self._round_no = 0
        self._alert_total = 0
        self._next_run_at = 0.0

    def request_stop(self) -> None:
        """请求停止：置位停止事件（主线程调用，跨线程安全）。"""
        self._stop_event.set()

    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: C901 - 循环体与 Tk 版逐行对齐，保持可读性
        """后台线程主体（monitor 循环，绝不触碰任何 Qt 控件）。"""
        fetcher = None
        storage = None
        try:
            fetcher = build_fetcher(self._config)
            storage = Storage(self._config.storage.path)
            notifiers = build_notifiers(self._config)
            monitor = Monitor(self._config, fetcher, storage, notifiers)
            interval = self._config.monitor.interval_seconds

            # 启动预检：Cookie 过期 → warning 日志（不阻断运行）
            monitor.preflight_cookie()

            self._emit_log(
                "INFO",
                f"[{datetime.now():%H:%M:%S}] 监控启动：抓取方式 {self._config.fetcher.type}，"
                f"关键词 {[r.keyword for r in self._config.keywords]}，"
                f"间隔 {interval} 秒，通知通道 {[n.name for n in notifiers]}",
            )

            while True:
                # 每轮开始前先检查停止信号，缩短停止响应时间
                if self._stop_event.is_set():
                    self._emit_log("INFO", f"[{datetime.now():%H:%M:%S}] 已收到停止信号，监控退出。")
                    break
                self._next_run_at = 0.0
                self._emit_log("INFO", f"[{datetime.now():%H:%M:%S}] ===== 第 {self._round_no + 1} 轮监测开始 =====")
                hits: List[Product] = []
                try:
                    monitor.run_once(log_item_details=not self._detail_only)
                    hits = list(monitor.last_result.notified_products)
                except Exception as exc:  # noqa: BLE001 - 单轮异常不终止循环
                    self._emit_log("ERROR", f"[{datetime.now():%H:%M:%S}] 本轮监测异常：{exc}")
                    logger.debug("监测轮次异常", exc_info=True)

                self._round_no += 1
                self._alert_total += len(hits)
                now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for product in hits:
                    self.ui_message.emit(
                        "alert",
                        {
                            "time": now_text,
                            "keyword": product.keyword,
                            "title": product.title,
                            "price": product.price_text,
                            "publish": product.publish_time or "未知",
                            "url": product.url,
                            "product_id": product.product_id,
                        },
                    )
                    self._emit_log(
                        "ALERT",
                        f"[{datetime.now():%H:%M:%S}] 🔔 低价命中！[{product.keyword}] "
                        f"{product.title} —— {product.price_text}",
                    )
                self.ui_message.emit("status", {"rounds": self._round_no, "alerts": self._alert_total})

                if self._single_round or self._stop_event.is_set():
                    break

                self._next_run_at = time.monotonic() + interval
                # 用 Event.wait 代替 sleep，才能立刻响应「停止监控」
                if self._stop_event.wait(interval):
                    self._emit_log("INFO", f"[{datetime.now():%H:%M:%S}] 已收到停止信号，监控退出。")
                    break
        except Exception as exc:  # noqa: BLE001 - 后台异常绝不允许崩窗
            logger.debug("监控线程异常", exc_info=True)
            self._emit_log("ERROR", f"[{datetime.now():%H:%M:%S}] 监控线程异常退出：{exc}")
            self.ui_message.emit(
                "message",
                {"level": "error", "title": "监控异常", "text": f"监控线程异常退出：\n{exc}"},
            )
        finally:
            for closable in (fetcher, storage):
                if closable is None:
                    continue
                try:
                    closable.close()
                except Exception:  # noqa: BLE001
                    pass
            self._next_run_at = 0.0
            self._emit_log("INFO", f"[{datetime.now():%H:%M:%S}] 监控已停止。")
            self.ui_message.emit("state", {"running": False})

    def _emit_log(self, level: str, text: str) -> None:
        """向主线程投递一条日志（内部辅助，保证调用点简洁）。"""
        self.ui_message.emit("log", (level, text))


class LogBridge(QObject):
    """日志桥：把 logging 记录通过 Qt 信号跨线程投递到主线程。

    Signals:
        message(str, str): (级别名, 文本)，由 QtLogHandler.emit 触发。
    """

    message = Signal(str, str)


class SoldCheckWorker(QThread):
    """「校验在架」后台线程（v3.7 方案 B：详情接口批量校验）。

    移植自 Tk 版 `on_check_on_shelf` 的后台 worker（gui.py:3085-3143）：
        - 主线程只把 (product_id, keyword, title) 普通值 + 已校验 Config 传入；
        - 后台线程逐条调用 `fetcher.check_item_status`，两次请求间固定限速
          `SOLD_CHECK_INTERVAL` 秒，避免触发风控；
        - 判定已售出/下架 → `storage.mark_sold_out_by_id`；
        - 全程不触碰任何 Qt 控件，只通过 `ui_message` 信号投递日志；
        - 完成后发 `finished_reload` 信号（主线程重载提醒记录表）。

    Signals:
        ui_message(str, object): 与 MonitorWorker 同构（"log"/"message"）。
        finished_reload(): 校验完成（主线程刷新表格）。
    """

    ui_message = Signal(str, object)
    finished_reload = Signal()

    def __init__(
        self,
        config: Config,
        items: List[Dict[str, str]],
        interval: float = 1.5,
        max_items: int = 30,
        parent: Optional[QObject] = None,
    ) -> None:
        """初始化。

        Args:
            config: 已校验的配置对象（fetcher.type 必须为 mtop）。
            items: 待校验条目 [{product_id, keyword, title}]。
            interval: 两次请求最小间隔秒数（限速，防风控）。
            max_items: 单次最多校验条数。
            parent: QObject 父对象。
        """
        super().__init__(parent)
        self._config = config
        self._items = list(items or [])[: max_items if max_items > 0 else 30]
        self._interval = interval
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """请求提前停止（主线程调用）。"""
        self._stop_event.set()

    def run(self) -> None:  # noqa: C901 - 与 Tk 版逐行对齐
        """后台线程主体。"""
        from ..fetcher import build_fetcher

        sold_ids: List[str] = []
        unknown = 0
        online = 0
        fetcher = None
        storage = None
        try:
            fetcher = build_fetcher(self._config)
            storage = Storage(self._config.storage.path)
            items = self._items
            for index, entry in enumerate(items):
                if self._stop_event.is_set():
                    self._emit_log("INFO", f"[{datetime.now():%H:%M:%S}] 已收到停止信号，校验提前结束。")
                    break
                pid = str(entry.get("product_id", "") or "")
                if not pid:
                    continue
                try:
                    online_flag = fetcher.check_item_status(pid, timeout=12.0)
                except Exception as exc:  # noqa: BLE001 - 单条失败不中断批量
                    self._emit_log("WARNING", f"[{datetime.now():%H:%M:%S}] 校验 {pid} 异常：{exc}")
                    online_flag = None
                if online_flag is False:
                    storage.mark_sold_out_by_id(pid, reason=SOLD_REASON_DETAIL)
                    sold_ids.append(pid)
                    self._emit_log(
                        "INFO",
                        f"[{datetime.now():%H:%M:%S}] 🚫 商品「{str(entry.get('title', ''))[:24]}」（{pid}）已下架/售出，已标记",
                    )
                elif online_flag is True:
                    online += 1
                else:
                    unknown += 1
                    self._emit_log(
                        "WARNING",
                        f"[{datetime.now():%H:%M:%S}] ⚠️ 商品 {pid} 在架状态无法判定（跳过，未标记）",
                    )
                # 限速：除最后一条外都在两次请求之间等待
                if index < len(items) - 1:
                    self._stop_event.wait(self._interval)
        except Exception as exc:  # noqa: BLE001 - 后台异常绝不崩窗
            self._emit_log("ERROR", f"[{datetime.now():%H:%M:%S}] 校验在架线程异常：{exc}")
        finally:
            for closable in (fetcher, storage):
                if closable is None:
                    continue
                try:
                    closable.close()
                except Exception:  # noqa: BLE001
                    pass
        self._emit_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] ✅ 校验完成：在架 {online}，已下架/售出 {len(sold_ids)}，无法判定 {unknown}",
        )
        self.finished_reload.emit()

    def _emit_log(self, level: str, text: str) -> None:
        self.ui_message.emit("log", (level, text))


class TestChannelWorker(QThread):
    """「测试发送」后台线程：调用 notifier.notify 后把结果信号回主线程。

    Signals:
        done(bool, str): (是否成功, 结果文案)，由主线程弹框 + 写日志。
    """

    done = Signal(bool, str)

    def __init__(
        self,
        notifier: Any,
        product: Product,
        ctype: str,
        parent: Optional[QObject] = None,
    ) -> None:
        """初始化。

        Args:
            notifier: 已构造的通知器实例。
            product: 测试用假商品（gui.make_sample_product 产物）。
            ctype: 通道类型（用于文案）。
            parent: QObject 父对象。
        """
        super().__init__(parent)
        self._notifier = notifier
        self._product = product
        self._ctype = str(ctype or "")

    def run(self) -> None:
        from ..gui import CHANNEL_LABELS

        label = CHANNEL_LABELS.get(self._ctype, self._ctype)
        try:
            self._notifier.notify([self._product])
        except Exception as exc:  # noqa: BLE001 - 网络类异常一律弹框告知
            self.done.emit(False, f"通道「{label}」发送失败：\n{exc}")
        else:
            self.done.emit(True, f"通道「{label}」已发送测试消息。")


#: 「校验在架」限速间隔（秒，防风控）与单次上限（对齐 gui.py 常量）
SOLD_CHECK_INTERVAL = 1.5
SOLD_CHECK_MAX_ITEMS = 30
#: 「标记已售出 / 校验在架」写回 product.sold_reason 的原因文案
SOLD_REASON_MANUAL = "人工标记"
SOLD_REASON_DETAIL = "详情接口判定"


class QtLogHandler(logging.Handler):
    """把 logging 记录转发到 LogBridge 信号（主线程渲染）。

    与 Tk 版 `QueueLogHandler` 语义一致：monitor / fetcher / notifier 等模块的
    既有日志无需任何改动就能显示在图形界面里；`emit()` 自身异常绝不向外抛。
    """

    def __init__(self, bridge: LogBridge, level: int = logging.INFO) -> None:
        """初始化。

        Args:
            bridge: 目标 LogBridge 实例。
            level: 处理的最低日志级别。
        """
        super().__init__(level=level)
        self.bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        """把一条日志通过信号投递（自身异常绝不向外抛）。"""
        try:
            message = record.getMessage()
            timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            text = f"[{timestamp}] {message}"
            if record.exc_info:
                text = f"{text}\n{self.format(record)}"
            self.bridge.message.emit(record.levelname, text)
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响业务
            pass
