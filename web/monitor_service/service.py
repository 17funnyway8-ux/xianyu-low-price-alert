"""MonitorService：Web 后端与监控核心之间的服务层（v1.9.7 拆出）。

仍较大（主服务类），后续按 cookie 池 / 在架校验继续拆 mixin。
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections import deque
from datetime import datetime
from typing import Any

from xianyu_alert import gui, secure  # noqa: F401  # gui 防御性导入 tkinter，容器可 import
from xianyu_alert.config import (
    Config,
    ConfigError,
    config_from_dict,
    load_config,
)
from xianyu_alert.fetcher import build_fetcher
from xianyu_alert.monitor import Monitor
from xianyu_alert.notifier import build_notifiers
from xianyu_alert.storage import Storage

from .constants import (
    LOG_BUFFER_MAXLEN,
    LOG_STATUS_LIMIT,
    MONITOR_JOIN_TIMEOUT,
    SQLITE_BUSY_TIMEOUT_MS,
)
from .cookie_pool import CookiePoolMixin
from .forms import (
    config_from_web_form,
)
from .keepalive import KeepaliveMixin
from .logging_bridge import (
    SseBroadcaster,
    _ensure_log_handler,
)
from .shelf_check import ShelfCheckMixin

logger = logging.getLogger(__name__)



class MonitorService(CookiePoolMixin, ShelfCheckMixin, KeepaliveMixin):
    """Web 侧 monitor 生命周期管理单例。

    Attributes:
        config_path: 配置文件路径（默认 paths.default_config_path()）。
        config: 当前生效的 Config 对象。
        storage: 当前生效的 Storage 实例（Web 读 / monitor 写共用，
            check_same_thread=False，写只在 monitor 线程）。
    """

    def __init__(self, config_path: str | None = None) -> None:
        from xianyu_alert import paths  # 延迟导入，避免模块顶层循环依赖

        self.config_path: str = config_path or paths.default_config_path()
        self._lock = threading.RLock()
        self._config: Config | None = None
        self._storage: Storage | None = None
        self._monitor: Monitor | None = None
        self._fetcher: Any = None
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._running: bool = False
        self._round_count: int = 0
        self._notified_count: int = 0
        self._last_round_at: datetime | None = None
        self._config_mtime: float | None = None
        self._logs: deque[dict[str, str]] = deque(maxlen=LOG_BUFFER_MAXLEN)
        self._log_lock = threading.Lock()
        self._broadcaster = SseBroadcaster()

        # ---- P2 新增状态：校验在架批处理（与 monitor 线程互斥，R7） ----
        self._check_shelf_lock = threading.Lock()
        self._check_shelf_thread: threading.Thread | None = None
        #: v1.9：Cookie 空闲保活线程与最近鉴权时间
        self._keeper: Any = None
        self._last_auth_at: float = 0.0
        self._check_shelf_cancel: threading.Event | None = None
        self._check_shelf_state: dict[str, Any] = {
            "running": False,
            "total": 0,
            "done": 0,
            "sold": 0,
            "unknown": 0,
            "cancelled": False,
            "started_at": None,
            "finished_at": None,
        }
        #: P2-11 明细日志开关（默认仅展示命中；run_once 传 log_item_details=not 本值）
        self._detail_only: bool = True

        #: 上次把「服务端刷新的令牌」写盘的时间（monotonic 秒，节流用；0=从未）
        self._last_token_persist_at: float = 0.0

        # 首次启动：配置文件不存在时生成内置默认配置（gui.load_raw_config 兜底）
        data = gui.load_raw_config(self.config_path)
        if not os.path.isfile(self.config_path):
            gui.save_raw_config(self.config_path, data)
            logger.info("配置文件不存在，已生成默认配置：%s", self.config_path)
        self._reload_from_disk()

        # 日志接入：把本服务设为「当前活跃」日志接收者
        _ensure_log_handler().set_service(self)

    # ------------------------------------------------------------------ #
    # 内部：配置 / 存储装载
    # ------------------------------------------------------------------ #
    def _reload_from_disk(self) -> None:
        """从磁盘重新加载配置并重建 Storage（幂等；旧 storage 先关闭）。"""
        if self._storage is not None:
            with contextlib.suppress(Exception): # 关闭失败不影响重载
                self._storage.close()
            self._storage = None
        data = gui.load_raw_config(self.config_path)
        self._config = config_from_dict(data)
        self._storage = Storage(self._config.storage.path)
        # Web 读 + monitor 写并发：短事务 + busy timeout（设计 §2.4）
        with contextlib.suppress(Exception): # busy timeout 设置失败不阻断
            self._storage.conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        self._config_mtime = gui.config_file_mtime(self.config_path)

    def _check_config_mtime(self) -> None:
        """worker 每轮前比对 config.yaml mtime；外部修改 → 重载 + 打日志。"""
        with self._lock:
            current = gui.config_file_mtime(self.config_path)
            if current is None or self._config_mtime is None or current == self._config_mtime:
                return
            try:
                new_config = load_config(self.config_path)
            except ConfigError as exc:
                logger.warning("检测到配置文件外部变更，但重载失败（保持旧配置，下轮重试）：%s", exc)
                return
            self._config = new_config
            self._config_mtime = current
            # 运行中 monitor 的 config 引用同步替换（下一轮 run_once 生效，
            # 例如外部 `cli login` 刷新 Cookie 后轮换取用新 Cookie）
            if self._monitor is not None:
                self._monitor.config = new_config
            logger.info("检测到配置文件外部变更，已重载（下一轮生效）")

    # ------------------------------------------------------------------ #
    # 生命周期：start / stop / run_once / status / shutdown
    # ------------------------------------------------------------------ #
    def _worker(self, monitor: Monitor, stop_event: threading.Event) -> None:
        """monitor 后台线程循环（daemon）。

        与 GUI `_monitor_worker` 同构：每轮 run_once + `stop_event.wait(interval)`
        实现「停止信号即时唤醒」（Monitor.run_forever 用 time.sleep 无法即时停止，
        故此处不复用 run_forever，而按 GUI 既有线程模型实现）。
        """
        try:
            monitor.preflight_cookie()
            while not stop_event.is_set():
                try:
                    self._check_config_mtime()
                except Exception as exc:  # noqa: BLE001 - mtime 检测失败不打断循环
                    logger.warning("配置文件变更检测失败：%s", exc)
                if stop_event.is_set():
                    break
                # v1.10（M05 单一时间源）：保活判断并入本循环，
                # 与轮次共用同一节拍（保活线程在监控运行期间会被停掉）。
                monitor._maybe_keepalive()  # noqa: SLF001 - 服务层与 Monitor 是同一模块族
                try:
                    notified = monitor.run_once(log_item_details=not self._detail_only)
                    with self._lock:
                        self._round_count += 1
                        self._notified_count += notified
                        self._last_round_at = datetime.now()
                    # 本轮可能被服务端刷新了令牌：节流落盘，避免重启后令牌倒退
                    self._persist_refreshed_token(monitor.fetcher)
                except Exception as exc:  # noqa: BLE001 - 单轮异常不打断循环
                    logger.exception("监测轮次异常，已跳过：%s", exc)
                # 复用 Monitor 的分片睡眠：等待期间同样做保活检查，且 stop_event 即时唤醒
                if not monitor._interruptible_sleep(  # noqa: SLF001
                    monitor.config.monitor.interval_seconds, stop_event=stop_event
                ):
                    break
        finally:
            # 仅当本线程仍是「当前活跃 worker」时才复位运行状态：
            # stop() 已把 self._thread 置 None（_running 由 stop() 复位）；
            # 若 stop 超时后立刻 start() 了新线程，旧线程退出不得覆盖新状态。
            with self._lock:
                if self._thread is threading.current_thread():
                    self._running = False
            logger.info("监测线程已退出")

    def start(self) -> dict[str, Any]:
        """启动 monitor 后台线程（幂等：已在运行时直接返回）。

        P2 互斥（R7）：校验在架批处理执行中不可启动监测（409 语义）。

        Returns:
            {"ok": bool, "message": str}。
        """
        with self._check_shelf_lock:
            shelf_running = (
                self._check_shelf_thread is not None and self._check_shelf_thread.is_alive()
            )
        with self._lock:
            if shelf_running:
                return {"ok": False, "message": "校验在架任务正在执行中，无法启动监测"}
            if self._thread is not None and self._thread.is_alive():
                return {"ok": True, "message": "监测已在运行中"}
            config = self._config
            if config is None or self._storage is None:
                return {"ok": False, "message": "配置尚未加载"}
            fetcher = build_fetcher(config)
            notifiers = build_notifiers(config)
            monitor = Monitor(
                config, fetcher, self._storage, notifiers, config_path=self.config_path
            )
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._worker,
                args=(monitor, stop_event),
                name="xianyu-web-monitor",
                daemon=True,
            )
            self._monitor = monitor
            self._fetcher = fetcher
            self._stop_event = stop_event
            self._thread = thread
            self._running = True
            thread.start()
            # v1.10（M05 单一时间源）：监控循环自己会按同一节拍保活，
            # 因此停掉独立保活线程；stop() 时再恢复（空闲期仍能保活）。
            self.stop_keepalive()
            logger.info(
                "监测已启动：关键词 %s，间隔 %d 秒，抓取器 %s",
                [r.keyword for r in config.keywords],
                config.monitor.interval_seconds,
                getattr(fetcher, "name", type(fetcher).__name__),
            )
            return {"ok": True, "message": "监测已启动"}

    def stop(self) -> dict[str, Any]:
        """停止 monitor 后台线程并关闭本轮 fetcher（幂等）。

        Storage 由本服务持有（Web 读需要），**不随 stop 关闭**，
        仅在配置重载 / shutdown 时关闭。

        Returns:
            {"ok": bool, "message": str}。
        """
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._running = False
                return {"ok": True, "message": "监测未在运行"}
            thread = self._thread
            stop_event = self._stop_event
            monitor = self._monitor
            self._thread = None
            self._stop_event = None
            self._monitor = None
            if stop_event is not None:
                stop_event.set()
            if monitor is not None:
                monitor.stop()

        thread.join(timeout=MONITOR_JOIN_TIMEOUT)

        with self._lock:
            fetcher = self._fetcher
            self._fetcher = None
            if fetcher is not None:
                with contextlib.suppress(Exception): # 关闭失败不影响停止
                    fetcher.close()
            self._running = False
            logger.info("监测已停止")
        # v1.10（M05）：监控已停，恢复独立保活线程（空闲期维持登录态）
        self.start_keepalive()
        return {"ok": True, "message": "监测已停止"}

    def run_once(self) -> dict[str, Any]:
        """立即执行一轮监测（仅在 monitor 未运行时可用）。

        Returns:
            {"ok": bool, "message": str, "notified": int}。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": False, "message": "监测正在运行中，无法手动执行单轮", "notified": 0}
            config = self._config
            if config is None or self._storage is None:
                return {"ok": False, "message": "配置尚未加载", "notified": 0}
            fetcher = build_fetcher(config)
            monitor = Monitor(config, fetcher, self._storage, build_notifiers(config))
        try:
            with contextlib.suppress(Exception):
                self._check_config_mtime()
            notified = monitor.run_once(log_item_details=not self._detail_only)
            with self._lock:
                self._round_count += 1
                self._notified_count += notified
                self._last_round_at = datetime.now()
            # 手动单轮同样落盘（此处用的是临时 fetcher，需显式传入）
            self._persist_refreshed_token(fetcher)
            return {
                "ok": True,
                "message": f"单轮执行完成，触发 {notified} 条提醒",
                "notified": int(notified),
            }
        finally:
            with contextlib.suppress(Exception):
                fetcher.close()

    def status(self) -> dict[str, Any]:
        """返回运行状态（供 /healthz 与前端状态条轮询）。"""
        with self._lock:
            running = self._running and self._thread is not None and self._thread.is_alive()
            interval = self._config.monitor.interval_seconds if self._config else 0
            last = self._last_round_at
            next_round_in: int | None = None
            if running and last is not None:
                elapsed = (datetime.now() - last).total_seconds()
                next_round_in = max(0, int(interval - elapsed))
            return {
                "running": running,
                "round_count": self._round_count,
                "notified_count": self._notified_count,
                "last_round_at": last.strftime("%Y-%m-%d %H:%M:%S") if last else None,
                "next_round_in": next_round_in,
                "interval_seconds": int(interval),
                "fetcher_type": self._config.fetcher.type if self._config else "",
                "keyword_count": len(self._config.keywords) if self._config else 0,
                "storage_path": self._config.storage.path if self._config else "",
                "detail_only": bool(self._detail_only),
            }

    def shutdown(self) -> None:
        """优雅关闭：停 monitor → 停保活 → 中止校验在架 → 关 storage → 摘除日志接收。"""
        self.stop()
        self.stop_keepalive()
        with self._check_shelf_lock:
            cancel_event = self._check_shelf_cancel
            shelf_thread = self._check_shelf_thread
            if cancel_event is not None:
                cancel_event.set()
        if shelf_thread is not None and shelf_thread.is_alive():
            shelf_thread.join(timeout=MONITOR_JOIN_TIMEOUT)
        with self._lock:
            if self._storage is not None:
                with contextlib.suppress(Exception):
                    self._storage.close()
                self._storage = None
        handler = _ensure_log_handler()
        if handler._service is self:  # noqa: SLF001 - 同包内部访问
            handler.set_service(None)

    # ------------------------------------------------------------------ #
    # 配置热重启
    # ------------------------------------------------------------------ #
    def apply_config(self, form: dict[str, Any]) -> dict[str, Any]:
        """Web 保存配置：校验 → 停止 → 写盘 → 重载 → 重启（若原在运行）。

        Args:
            form: 前端表单（web_form_from_config 的逆结构）。

        Returns:
            {"ok": bool, "message": str, "restarted": bool}。

        Raises:
            ConfigError: 表单校验失败（api.py 转 400 + 中文原因）。
        """
        with self._lock:
            base = gui.load_raw_config(self.config_path)
            config_dict = config_from_web_form(form, base)
            # 完整校验（关键词/间隔/页数/通道/新字段），失败抛 ConfigError
            new_config = config_from_dict(config_dict)

            was_running = self._thread is not None and self._thread.is_alive()

        if was_running:
            self.stop()

        # 写盘 → 重载（重建 storage，应用新路径）
        gui.save_raw_config(self.config_path, config_dict)
        with self._lock:
            self._reload_from_disk()
            self._config = new_config

        if was_running:
            self.start()

        logger.info("配置已保存并生效（热重启：%s）", "是" if was_running else "否")
        return {"ok": True, "message": "配置已保存并生效", "restarted": was_running}

    def reload_if_external_changed(self) -> bool:
        """公开的 mtime 检测入口（API 在 Cookie 保存后调用，立即生效）。"""
        with self._lock:
            before = self._config_mtime
            self._check_config_mtime()
            return self._config_mtime != before

    # ------------------------------------------------------------------ #
    # 日志 / SSE
    # ------------------------------------------------------------------ #
    def append_log(self, level: str, text: str, ts: str) -> None:
        """写入环形缓冲并广播（由 ServiceLogHandler 调用，线程安全）。"""
        entry = {"level": level, "text": text, "ts": ts}
        with self._log_lock:
            self._logs.append(entry)
        self._broadcaster.publish(entry)

    def recent_logs(self, limit: int = LOG_STATUS_LIMIT) -> list[dict[str, str]]:
        """返回最近日志（新→旧方向由调用方决定；这里返回最旧→最新便于前端追加）。"""
        with self._log_lock:
            items = list(self._logs)
        if limit <= 0:
            return items
        return items[-limit:]

    @property
    def broadcaster(self) -> SseBroadcaster:
        """SSE 广播器。"""
        return self._broadcaster

    @property
    def storage(self) -> Storage:
        """当前 Storage 实例（Web 读查询用；调用方需保证服务未 shutdown）。"""
        if self._storage is None:
            raise RuntimeError("Storage 尚未初始化（服务已关闭）")
        return self._storage

    @property
    def config(self) -> Config:
        """当前生效的 Config 对象。"""
        if self._config is None:
            raise RuntimeError("Config 尚未加载")
        return self._config

    # ------------------------------------------------------------------ #
    # P2-11：明细日志开关
    # ------------------------------------------------------------------ #
    def set_detail_only(self, enabled: bool) -> None:
        """设置明细日志开关（true=仅展示命中；run_once 传 log_item_details=False）。"""
        self._detail_only = bool(enabled)

    # ------------------------------------------------------------------ #
    # P2-03：校验在架（异步批处理，与 monitor 线程互斥 R7）
    # ------------------------------------------------------------------ #
    def clear_records(self) -> dict[str, Any]:
        """清空去重记录（product + meta，**保留 blacklist**）。

        monitor 线程运行中 → 409「请先停止监控再清空记录」（对齐 GUI gui.py:3800-3802）。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": False, "message": "请先停止监控再清空记录", "code": 409}
            storage = self._storage
        if storage is None:
            return {"ok": False, "message": "存储尚未初始化", "code": 500}
        deleted = storage.clear_all()
        logger.info("已清空去重记录，共删除 %d 条", int(deleted))
        return {"ok": True, "deleted": int(deleted), "message": f"已清空去重记录，共删除 {deleted} 条"}

    def unmark_record(self, product_id: str) -> dict[str, Any]:
        """把商品恢复为在架（撤销售出标记，幂等）。"""
        pid = str(product_id or "").strip()
        if not pid:
            return {"ok": False, "message": "product_id 不能为空", "code": 400}
        updated = self.storage.unmark_sold_out(pid)
        logger.info("已把商品 %s 恢复为在架", pid)
        return {"ok": True, "updated": int(updated), "message": "已恢复为在架"}

    # ------------------------------------------------------------------ #
    # Cookie 免扫码刷新（日常续期的「一键入口」）
    # ------------------------------------------------------------------ #
# ---------------------------------------------------------------------- #
# 模块级单例访问器
# ---------------------------------------------------------------------- #
#: 进程内唯一 MonitorService 实例（首次访问时懒创建；测试可自行构造并替换）
_service_instance: MonitorService | None = None


def get_service() -> MonitorService:
    """返回进程内 MonitorService 单例（懒创建）。"""
    global _service_instance
    if _service_instance is None:
        _service_instance = MonitorService()
    return _service_instance


def reset_service() -> None:
    """重置单例（测试隔离用；先 shutdown 旧实例）。"""
    global _service_instance
    if _service_instance is not None:
        _service_instance.shutdown()
        _service_instance = None

__all__ = [
    "MonitorService",
    "get_service",
    "reset_service",
]
