"""MonitorService 单例：monitor 线程管理 + 日志环形缓冲 + SSE 广播 + 配置热重启。

职责（对齐 docs/v1.8_Docker化增量研判与执行方案.md §2.3 / §6 P1）：
    - 持有全局唯一的 Monitor 实例（monitor 线程 daemon，跑在独立后台线程）；
    - 日志环形缓冲（固定 2000 行，参考 gui.py QueueLogHandler / gui_qt workers.py
      LogBridge 思路）：挂一个 logging.Handler 到 `xianyu_alert` logger，
      monitor / fetcher / notifier 等模块的既有日志自动进环形缓冲并被 SSE 广播；
    - SSE 广播器：asyncio.Queue 订阅者集合，跨线程发布用 loop.call_soon_threadsafe；
    - 配置热重启：保存配置 → 停止旧 monitor → 用新 Config 重建 → 重启（若原在运行）；
    - config.yaml mtime 检测：外部修改（如宿主机 `cli login` 刷新 Cookie）→
      自动重载 Config + 打日志（运行中 monitor 的 config 引用同步替换，下一轮生效）。

线程模型（硬性约束）：
    - monitor 线程绝不触碰任何 Web / ASGI 状态，只写日志缓冲 + 共享只读状态
      （round_count / notified_count / last_round_at，均由本类锁保护）；
    - Web handler 读查询走同一 Storage 实例（check_same_thread=False + 短事务 +
      busy timeout），与 GUI 主线程读 monitor 线程写的语义完全一致；
    - SQLite 写只在 monitor 线程（单写者语义由 v1.8 单实例锁保证）。

本模块只依赖业务核心 + 标准库，不 import FastAPI（api.py 负责 HTTP 层）。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import threading
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from xianyu_alert import gui, secure  # noqa: F401  # gui 防御性导入 tkinter，容器可 import
from xianyu_alert.config import Config, ConfigError, config_from_dict, load_config
from xianyu_alert.fetcher import build_fetcher
from xianyu_alert.monitor import Monitor
from xianyu_alert.notifier import build_notifiers
from xianyu_alert.storage import Storage

logger = logging.getLogger(__name__)

#: 日志环形缓冲最大行数（固定容量，超出后丢弃最旧行，避免内存膨胀）
LOG_BUFFER_MAXLEN = 2000
#: 状态条展示时返回的最近日志条数
LOG_STATUS_LIMIT = 200
#: 关闭 monitor 线程时等待收尾的最大秒数（对齐 GUI CLOSE_JOIN_TIMEOUT=5.0）
MONITOR_JOIN_TIMEOUT = 5.0
#: SQLite busy timeout（毫秒）：Web 读与 monitor 写并发时的等待上限
SQLITE_BUSY_TIMEOUT_MS = 5000

#: 日志来源 logger 名（monitor/fetcher/notifier 等子 logger 会自动向上传播）
_LOG_LOGGER_NAME = "xianyu_alert"


# ---------------------------------------------------------------------- #
# SSE 广播器（跨线程：monitor 线程 -> asyncio 订阅者）
# ---------------------------------------------------------------------- #
class SseBroadcaster:
    """线程安全的 SSE 广播器：monitor 线程发布，ASGI 端点订阅。

    每个订阅者持有一个 `asyncio.Queue`（maxsize 500，溢出丢最旧）；
    发布来自任意线程（monitor / API handler），通过
    `loop.call_soon_threadsafe(queue.put_nowait, data)` 投递到订阅者所在事件循环，
    避免直接在别的线程操作 asyncio.Queue（线程不安全）。
    """

    def __init__(self) -> None:
        self._subscribers: Dict[int, "tuple[asyncio.Queue, asyncio.AbstractEventLoop]"] = {}
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> "tuple[int, asyncio.Queue]":
        """注册一个订阅者，返回 (订阅号, 队列)。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            sub_id = next(self._counter)
            self._subscribers[sub_id] = (queue, loop)
        return sub_id, queue

    def unsubscribe(self, sub_id: int) -> None:
        """注销订阅者（幂等）。"""
        with self._lock:
            self._subscribers.pop(sub_id, None)

    def publish(self, data: Dict[str, Any]) -> None:
        """把一条消息广播给全部订阅者（跨线程安全，自身异常绝不外抛）。"""
        with self._lock:
            subs = list(self._subscribers.items())
        for sub_id, (queue, loop) in subs:
            try:
                if loop.is_closed():
                    self.unsubscribe(sub_id)
                    continue
                loop.call_soon_threadsafe(self._safe_put, queue, data)
            except Exception:  # noqa: BLE001 - 单个订阅者失败不影响其它订阅者
                self.unsubscribe(sub_id)

    @staticmethod
    def _safe_put(queue: asyncio.Queue, data: Dict[str, Any]) -> None:
        """在事件循环线程内执行的投递（队列满时丢最旧，绝不阻塞）。"""
        try:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(data)
        except Exception:  # noqa: BLE001 - 投递失败静默（订阅端可能已断开）
            pass


# ---------------------------------------------------------------------- #
# 日志 Handler（环形缓冲 + 广播）
# ---------------------------------------------------------------------- #
class ServiceLogHandler(logging.Handler):
    """把 `xianyu_alert` logger 的日志转发到「当前活跃」的 MonitorService。

    模块级单例 handler（避免多次 import / 多个服务实例叠加重复日志）；
    通过 `set_service` 指向当前活跃服务，服务 shutdown 时置空。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._service: "Optional[MonitorService]" = None

    def set_service(self, service: "Optional[MonitorService]") -> None:
        """切换当前活跃服务（None 表示无服务，日志只进标准输出）。"""
        self._service = service

    def emit(self, record: logging.LogRecord) -> None:
        """把一条日志写入服务环形缓冲并广播（自身异常绝不向外抛）。"""
        try:
            service = self._service
            if service is None:
                return
            message = record.getMessage()
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            text = f"[{ts}] {message}"
            if record.exc_info:
                text = f"{text}\n{self.format(record)}"
            service.append_log(record.levelname, text, ts)
        except Exception:  # noqa: BLE001 - 日志处理失败绝不影响业务线程
            pass


#: 模块级日志 handler（进程内只挂一次）
_log_handler: Optional[ServiceLogHandler] = None


def _ensure_log_handler() -> ServiceLogHandler:
    """确保 `xianyu_alert` logger 挂了唯一的 ServiceLogHandler（幂等）。

    同时把该 logger 级别设为 INFO：Web 服务明确需要捕获 INFO 级日志进环形
    缓冲（不依赖 root logger 的 basicConfig 配置；entry.py 也会 setup_logging）。
    """
    global _log_handler
    if _log_handler is None:
        _log_handler = ServiceLogHandler()
        target = logging.getLogger(_LOG_LOGGER_NAME)
        target.setLevel(logging.INFO)
        target.addHandler(_log_handler)
    return _log_handler


# ---------------------------------------------------------------------- #
# 表单转换（复用 gui 纯函数；Cookie 一律脱敏，不回传明文）
# ---------------------------------------------------------------------- #
def web_form_from_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """把原始配置字典转换为 Web 表单（复用 gui.config_to_form + 脱敏）。

    Returns:
        形如 {
            "keywords": [{"keyword", "max_price", "enabled",
                          "exclude_keywords", "required_keywords"}, ...],
            "interval_seconds", "fetcher_type", "pages", "user_agent",
            "storage_path", "channels", "cookie_alert_enabled",
            "cookie_check_interval_seconds", "preset_exclude_keywords",
            "cookies_masked", "cookies_was_encrypted", "cookies_undecryptable",
            "cookie_health": {"state", "text"},
        }。
        Cookie 字段一律 mask_cookie 脱敏，**绝不回传明文**（共享知识 5/7）。
    """
    form = gui.config_to_form(data)

    keywords: List[Dict[str, Any]] = []
    for kw, price in form.get("keywords", []):
        filters = form.get("keyword_filters", {}).get(kw, {}) or {}
        keywords.append(
            {
                "keyword": kw,
                "max_price": price,
                "enabled": gui.parse_enabled_flag(
                    form.get("keyword_enabled", {}).get(kw), default=True
                ),
                "exclude_keywords": list(filters.get("exclude_keywords") or []),
                "required_keywords": list(filters.get("required_keywords") or []),
            }
        )

    raw_cookie = str(form.get("cookies") or "")
    state, text = gui.cookie_status(raw_cookie)
    monitor_raw = data.get("monitor") if isinstance(data, dict) else None
    monitor_raw = monitor_raw if isinstance(monitor_raw, dict) else {}
    try:
        check_interval = int(monitor_raw.get("cookie_check_interval_seconds", 0) or 0)
    except (TypeError, ValueError):
        check_interval = 0
    if check_interval < 0:
        check_interval = 0

    return {
        "keywords": keywords,
        "interval_seconds": int(form.get("interval") or 600),
        "fetcher_type": str(form.get("fetcher_type") or "mtop"),
        "pages": int(form.get("pages") or 1),
        "user_agent": str(form.get("user_agent") or ""),
        "storage_path": str(form.get("storage_path") or "state/xianyu_alert.db"),
        "channels": form.get("channels", {}),
        "cookie_alert_enabled": gui.parse_enabled_flag(
            monitor_raw.get("cookie_alert_enabled"), default=True
        ),
        "cookie_check_interval_seconds": check_interval,
        "preset_exclude_keywords": list(form.get("preset_exclude_keywords") or []),
        "cookies_masked": secure.mask_cookie(raw_cookie) or "",
        "cookies_was_encrypted": bool(form.get("cookies_was_encrypted", False)),
        "cookies_undecryptable": bool(form.get("cookies_undecryptable", False)),
        "cookie_health": {"state": state, "text": text},
    }


def config_from_web_form(form: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """把 Web 表单转换为可写盘的配置字典（复用 gui.build_config_dict）。

    - 关键词/通道/间隔等字段由表单覆盖；
    - **Cookie 字段不在表单中回传**（GET 已脱敏），这里从 base 原样保留——
      Cookie 只能通过 `POST /api/cookie/save`（校验 + Fernet 加密）更新，
      杜绝表单保存路径误写明文（共享知识 7）。
    - v1.8 新增字段 cookie_alert_enabled / cookie_check_interval_seconds
      （gui.build_config_dict 不覆盖，这里显式写回）。
    """
    keywords_raw = form.get("keywords") or []
    keywords: List["tuple[str, float]"] = []
    keyword_filters: Dict[str, Dict[str, List[str]]] = {}
    keyword_enabled: Dict[str, bool] = {}
    for item in keywords_raw:
        if not isinstance(item, dict):
            continue
        kw = str(item.get("keyword") or "").strip()
        if not kw:
            continue
        try:
            price = float(item.get("max_price", 0))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        keywords.append((kw, price))
        keyword_filters[kw] = {
            "exclude_keywords": [str(x) for x in (item.get("exclude_keywords") or [])],
            "required_keywords": [str(x) for x in (item.get("required_keywords") or [])],
        }
        keyword_enabled[kw] = gui.parse_enabled_flag(item.get("enabled"), default=True)

    channels = form.get("channels") or {}
    preset = form.get("preset_exclude_keywords")
    data = gui.build_config_dict(
        keywords=keywords,
        interval_seconds=int(form.get("interval_seconds") or 600),
        fetcher_type=str(form.get("fetcher_type") or "mock"),
        cookies="",
        storage_path=str(form.get("storage_path") or "state/xianyu_alert.db"),
        channels=channels,
        base=base if isinstance(base, dict) else None,
        pages=int(form.get("pages") or 1),
        keyword_filters=keyword_filters if keywords_raw else None,
        keyword_enabled=keyword_enabled if keywords_raw else None,
        preset_exclude_keywords=preset,
    )

    # ---- 保留 Cookie 字段（表单不回传明文，一律以磁盘/服务内存中的为准） ----
    base_monitor = base.get("monitor") if isinstance(base, dict) else None
    base_monitor = base_monitor if isinstance(base_monitor, dict) else {}
    monitor = data.setdefault("monitor", {})
    monitor["cookies"] = str(base_monitor.get("cookies") or "")
    monitor["cookies_encrypted"] = bool(base_monitor.get("cookies_encrypted", False))
    if "cookie_pool" in base_monitor:
        monitor["cookie_pool"] = base_monitor["cookie_pool"]

    # ---- v1.8 新增字段（gui.build_config_dict 不覆盖，这里显式写回） ----
    monitor["cookie_alert_enabled"] = gui.parse_enabled_flag(
        form.get("cookie_alert_enabled"), default=True
    )
    try:
        check_interval = int(form.get("cookie_check_interval_seconds") or 0)
    except (TypeError, ValueError):
        check_interval = 0
    if check_interval < 0:
        check_interval = 0
    monitor["cookie_check_interval_seconds"] = check_interval

    return data


# ---------------------------------------------------------------------- #
# MonitorService
# ---------------------------------------------------------------------- #
class MonitorService:
    """Web 侧 monitor 生命周期管理单例。

    Attributes:
        config_path: 配置文件路径（默认 paths.default_config_path()）。
        config: 当前生效的 Config 对象。
        storage: 当前生效的 Storage 实例（Web 读 / monitor 写共用，
            check_same_thread=False，写只在 monitor 线程）。
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        from xianyu_alert import paths  # 延迟导入，避免模块顶层循环依赖

        self.config_path: str = config_path or paths.default_config_path()
        self._lock = threading.RLock()
        self._config: Optional[Config] = None
        self._storage: Optional[Storage] = None
        self._monitor: Optional[Monitor] = None
        self._fetcher: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._running: bool = False
        self._round_count: int = 0
        self._notified_count: int = 0
        self._last_round_at: Optional[datetime] = None
        self._config_mtime: Optional[float] = None
        self._logs: Deque[Dict[str, str]] = deque(maxlen=LOG_BUFFER_MAXLEN)
        self._log_lock = threading.Lock()
        self._broadcaster = SseBroadcaster()

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
            try:
                self._storage.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响重载
                pass
            self._storage = None
        data = gui.load_raw_config(self.config_path)
        self._config = config_from_dict(data)
        self._storage = Storage(self._config.storage.path)
        # Web 读 + monitor 写并发：短事务 + busy timeout（设计 §2.4）
        try:
            self._storage.conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        except Exception:  # noqa: BLE001 - busy timeout 设置失败不阻断
            pass
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
                try:
                    notified = monitor.run_once()
                    with self._lock:
                        self._round_count += 1
                        self._notified_count += notified
                        self._last_round_at = datetime.now()
                except Exception as exc:  # noqa: BLE001 - 单轮异常不打断循环
                    logger.exception("监测轮次异常，已跳过：%s", exc)
                if stop_event.wait(monitor.config.monitor.interval_seconds):
                    break
        finally:
            # 仅当本线程仍是「当前活跃 worker」时才复位运行状态：
            # stop() 已把 self._thread 置 None（_running 由 stop() 复位）；
            # 若 stop 超时后立刻 start() 了新线程，旧线程退出不得覆盖新状态。
            with self._lock:
                if self._thread is threading.current_thread():
                    self._running = False
            logger.info("监测线程已退出")

    def start(self) -> Dict[str, Any]:
        """启动 monitor 后台线程（幂等：已在运行时直接返回）。

        Returns:
            {"ok": bool, "message": str}。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": True, "message": "监测已在运行中"}
            config = self._config
            if config is None:
                return {"ok": False, "message": "配置尚未加载"}
            fetcher = build_fetcher(config)
            notifiers = build_notifiers(config)
            monitor = Monitor(config, fetcher, self._storage, notifiers)
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
            logger.info(
                "监测已启动：关键词 %s，间隔 %d 秒，抓取器 %s",
                [r.keyword for r in config.keywords],
                config.monitor.interval_seconds,
                getattr(fetcher, "name", type(fetcher).__name__),
            )
            return {"ok": True, "message": "监测已启动"}

    def stop(self) -> Dict[str, Any]:
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
                try:
                    fetcher.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响停止
                    pass
            self._running = False
            logger.info("监测已停止")
        return {"ok": True, "message": "监测已停止"}

    def run_once(self) -> Dict[str, Any]:
        """立即执行一轮监测（仅在 monitor 未运行时可用）。

        Returns:
            {"ok": bool, "message": str, "notified": int}。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": False, "message": "监测正在运行中，无法手动执行单轮", "notified": 0}
            config = self._config
            if config is None:
                return {"ok": False, "message": "配置尚未加载", "notified": 0}
            fetcher = build_fetcher(config)
            monitor = Monitor(config, fetcher, self._storage, build_notifiers(config))
        try:
            try:
                self._check_config_mtime()
            except Exception:  # noqa: BLE001
                pass
            notified = monitor.run_once()
            with self._lock:
                self._round_count += 1
                self._notified_count += notified
                self._last_round_at = datetime.now()
            return {
                "ok": True,
                "message": f"单轮执行完成，触发 {notified} 条提醒",
                "notified": int(notified),
            }
        finally:
            try:
                fetcher.close()
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> Dict[str, Any]:
        """返回运行状态（供 /healthz 与前端状态条轮询）。"""
        with self._lock:
            running = self._running and self._thread is not None and self._thread.is_alive()
            interval = self._config.monitor.interval_seconds if self._config else 0
            last = self._last_round_at
            next_round_in: Optional[int] = None
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
            }

    def shutdown(self) -> None:
        """优雅关闭：停 monitor → 关 storage → 摘除日志接收。"""
        self.stop()
        with self._lock:
            if self._storage is not None:
                try:
                    self._storage.close()
                except Exception:  # noqa: BLE001
                    pass
                self._storage = None
        handler = _ensure_log_handler()
        if handler._service is self:  # noqa: SLF001 - 同包内部访问
            handler.set_service(None)

    # ------------------------------------------------------------------ #
    # 配置热重启
    # ------------------------------------------------------------------ #
    def apply_config(self, form: Dict[str, Any]) -> Dict[str, Any]:
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

    def recent_logs(self, limit: int = LOG_STATUS_LIMIT) -> List[Dict[str, str]]:
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


# ---------------------------------------------------------------------- #
# 模块级单例访问器
# ---------------------------------------------------------------------- #
#: 进程内唯一 MonitorService 实例（首次访问时懒创建；测试可自行构造并替换）
_service_instance: Optional[MonitorService] = None


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
