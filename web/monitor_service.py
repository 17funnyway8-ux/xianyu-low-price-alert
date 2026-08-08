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
import tempfile
import threading
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

import yaml

from xianyu_alert import gui, secure  # noqa: F401  # gui 防御性导入 tkinter，容器可 import
from xianyu_alert.config import (
    Config,
    ConfigError,
    config_from_dict,
    load_config,
    serialize_cookie_pool,
)
from xianyu_alert.cookie import (
    HEALTH_EXPIRING,
    HEALTH_OK,
    TOKEN_TTL_MS,
    cookie_has_token,
    cookie_token_timestamp,
    detect_cookie_health,
)
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

#: 「校验在架」限速间隔 / 单次上限 / 单条详情接口超时（秒）
#: 直接复用 gui.SOLD_CHECK_*（gui.py:113-115），Web 与桌面行为完全一致（设计 R2）
SOLD_CHECK_INTERVAL: float = gui.SOLD_CHECK_INTERVAL          # 1.5
SOLD_CHECK_MAX_ITEMS: int = gui.SOLD_CHECK_MAX_ITEMS          # 30
SOLD_REASON_DETAIL: str = gui.SOLD_REASON_DETAIL              # "详情接口判定"
CHECK_SHELF_ITEM_TIMEOUT: float = 12.0

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

    # ---- P2-09：监测参数透传（page_size / page_sleep 读 fetcher 节点） ----
    fetcher_raw = data.get("fetcher") if isinstance(data, dict) else None
    fetcher_raw = fetcher_raw if isinstance(fetcher_raw, dict) else {}
    try:
        page_size = int(fetcher_raw.get("page_size", 30))
    except (TypeError, ValueError):
        page_size = 30
    if page_size < 1:
        page_size = 30
    try:
        page_sleep = float(fetcher_raw.get("page_sleep", 2.0))
    except (TypeError, ValueError):
        page_sleep = 2.0
    if page_sleep < 0:
        page_sleep = 2.0

    return {
        "keywords": keywords,
        "interval_seconds": int(form.get("interval") or 600),
        "fetcher_type": str(form.get("fetcher_type") or "mtop"),
        "pages": int(form.get("pages") or 1),
        "page_size": page_size,
        "page_sleep": page_sleep,
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

    # ---- P2-09：user_agent 写回（gui.build_config_dict 只 setdefault，不覆盖表单）；
    # 表单未提供该字段时保留 base（避免旧客户端/缺省表单把 UA 清空） ----
    if form.get("user_agent") is not None:
        monitor["user_agent"] = str(form.get("user_agent") or "").strip()

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

    # ---- P2-09：page_size / page_sleep 透传写回 fetcher 节点（base deepcopy 保留其它字段） ----
    fetcher = data.setdefault("fetcher", {})
    page_size_raw = form.get("page_size")
    if page_size_raw is not None and str(page_size_raw).strip() != "":
        try:
            page_size = int(page_size_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"抓取每页数量必须是整数，当前输入：{page_size_raw}") from exc
        if page_size < 1 or page_size > 100:
            raise ConfigError(f"抓取每页数量必须在 1~100 之间，当前输入：{page_size_raw}")
        fetcher["page_size"] = page_size
    page_sleep_raw = form.get("page_sleep")
    if page_sleep_raw is not None and str(page_sleep_raw).strip() != "":
        try:
            page_sleep = float(page_sleep_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"翻页间隔必须是数字（秒），当前输入：{page_sleep_raw}") from exc
        if page_sleep < 0:
            raise ConfigError(f"翻页间隔不能为负数，当前输入：{page_sleep_raw}")
        fetcher["page_sleep"] = page_sleep

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

        # ---- P2 新增状态：校验在架批处理（与 monitor 线程互斥，R7） ----
        self._check_shelf_lock = threading.Lock()
        self._check_shelf_thread: Optional[threading.Thread] = None
        self._check_shelf_cancel: Optional[threading.Event] = None
        self._check_shelf_state: Dict[str, Any] = {
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
                    notified = monitor.run_once(log_item_details=not self._detail_only)
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
            notified = monitor.run_once(log_item_details=not self._detail_only)
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
                "detail_only": bool(self._detail_only),
            }

    def shutdown(self) -> None:
        """优雅关闭：停 monitor → 中止校验在架 → 关 storage → 摘除日志接收。"""
        self.stop()
        with self._check_shelf_lock:
            cancel_event = self._check_shelf_cancel
            shelf_thread = self._check_shelf_thread
            if cancel_event is not None:
                cancel_event.set()
        if shelf_thread is not None and shelf_thread.is_alive():
            shelf_thread.join(timeout=MONITOR_JOIN_TIMEOUT)
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

    # ------------------------------------------------------------------ #
    # P2-11：明细日志开关
    # ------------------------------------------------------------------ #
    def set_detail_only(self, enabled: bool) -> None:
        """设置明细日志开关（true=仅展示命中；run_once 传 log_item_details=False）。"""
        self._detail_only = bool(enabled)

    # ------------------------------------------------------------------ #
    # P2-03：校验在架（异步批处理，与 monitor 线程互斥 R7）
    # ------------------------------------------------------------------ #
    def start_check_shelf(self, product_ids: List[str]) -> Dict[str, Any]:
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
        ids: List[str] = []
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

    def cancel_check_shelf(self) -> Dict[str, Any]:
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

    def check_shelf_status(self) -> Dict[str, Any]:
        """返回校验批处理进度（前端轮询 2s 用）。"""
        with self._check_shelf_lock:
            return dict(self._check_shelf_state)

    def _check_shelf_worker(self, ids: List[str], cancel_event: threading.Event) -> None:
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
                    status = fetcher.check_item_status(pid, timeout=CHECK_SHELF_ITEM_TIMEOUT)
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
                try:
                    fetcher.close()
                except Exception:  # noqa: BLE001
                    pass
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
    def cookie_pool_list(self) -> Dict[str, Any]:
        """读磁盘 config 的 monitor.cookie_pool（解密为明文）→ 逐条 detect_cookie_health
        + mask_cookie 脱敏 + expire 时间 → 返回展示列表（**绝不出明文**，R4）。

        Returns:
            {
                "pool_used": bool,           # 池中是否有「启用+健康」条目（轮换语义）
                "pool": [{"name", "enabled", "health_state", "health_reason",
                          "expire_at", "masked"}, ...],
                "single": {"health_state", "health_reason", "masked"},
                "message": 可选提示（池空/无健康条目时回退单值）。
            }
        """
        items = self._read_pool_plaintext()
        pool: List[Dict[str, Any]] = []
        for item in items:
            cookie = str(item.get("cookie") or "")
            state, reason = detect_cookie_health(cookie)
            pool.append(
                {
                    "name": str(item.get("name") or ""),
                    "enabled": bool(item.get("enabled", True)),
                    "health_state": state,
                    "health_reason": reason,
                    "expire_at": self._cookie_expire_text(
                        cookie, enabled=bool(item.get("enabled", True))
                    ),
                    "masked": secure.mask_cookie(cookie) or "",
                }
            )
        with self._lock:
            single_raw = str(self._config.monitor.cookies or "") if self._config else ""
        single_state, single_reason = detect_cookie_health(single_raw)
        single = {
            "health_state": single_state,
            "health_reason": single_reason,
            "masked": secure.mask_cookie(single_raw) or "",
        }
        # pool_used：池中是否有「启用 + 非空 + 健康」条目（resolve_cookie_for_round 语义）。
        # 注意 items 是 dict（非 CookiePoolItem），不能用 pool_enabled_cookies（属性访问），
        # 这里直接按 dict 键过滤，与 cookie.pool_usable_cookies 语义对齐。
        usable: List[str] = []
        for item in items:
            if not bool(item.get("enabled", True)):
                continue
            ck = str(item.get("cookie") or "")
            if not ck:
                continue
            try:
                state, _reason = detect_cookie_health(ck)
            except Exception:  # noqa: BLE001 - 检测异常按不可用处理
                continue
            if state in (HEALTH_OK, HEALTH_EXPIRING):
                usable.append(ck)
        pool_used = bool(usable)
        result: Dict[str, Any] = {
            "pool_used": pool_used,
            "pool": pool,
            "single": single,
        }
        if not pool_used:
            result["message"] = "池为空或无健康条目，将回退单值 Cookie"
        return result

    def cookie_pool_action(
        self,
        action: str,
        name: Optional[str] = None,
        new_name: Optional[str] = None,
        cookie: Optional[str] = None,
        force_missing_token: bool = False,
    ) -> Dict[str, Any]:
        """action 分发（add/update/delete/toggle/set_default/refresh_selected/auto_disable_expired）。

        所有写路径：内存明文操作 → `serialize_cookie_pool(items, encrypt=True)`
        （fernet1: 密文）→ 原子写盘 → `reload_if_external_changed()`（R1/R4）。

        Returns:
            成功：{"ok": True, "message": str, "pool": 展示列表?}；
            失败：{"ok": False, "message": str, "code": int}。
        """
        action = str(action or "").strip().lower()
        items = self._read_pool_plaintext()

        def _find_idx(target: str) -> int:
            for i, item in enumerate(items):
                if item.get("name") == target:
                    return i
            return -1

        def _dup_name(target: str, exclude_idx: int = -1) -> bool:
            for i, item in enumerate(items):
                if i == exclude_idx:
                    continue
                if item.get("name") == target:
                    return True
            return False

        def _persist() -> List[Dict[str, Any]]:
            """加密写盘 + mtime 重载 + 返回刷新后的池展示列表（只取 pool 数组）。"""
            self._write_pool_encrypted(items)
            self.reload_if_external_changed()
            return self.cookie_pool_list()["pool"]

        if action == "add":
            nm = str(name or "").strip()
            ck = str(cookie or "").strip()
            if not nm:
                return {"ok": False, "message": "条目名称不能为空", "code": 400}
            if not ck:
                return {"ok": False, "message": "Cookie 内容不能为空", "code": 400}
            if _dup_name(nm):
                return {"ok": False, "message": f"已存在同名条目「{nm}」", "code": 400}
            if not cookie_has_token(ck) and not force_missing_token:
                return {
                    "ok": False,
                    "message": "缺少 _m_h5_tk，mtop 抓取很可能失败，仍要添加吗？",
                    "code": 400,
                }
            items.append({"name": nm, "cookie": ck, "enabled": True})
            pool = _persist()
            return {"ok": True, "message": f"已添加 Cookie 条目「{nm}」", "pool": pool}

        if action == "update":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            new_nm = str(new_name or "").strip()
            if new_nm:
                if _dup_name(new_nm, exclude_idx=idx):
                    return {"ok": False, "message": f"已存在同名条目「{new_nm}」", "code": 400}
                items[idx]["name"] = new_nm
            if cookie is not None:
                ck = str(cookie or "").strip()
                if not ck:
                    return {"ok": False, "message": "Cookie 内容不能为空", "code": 400}
                state, reason = detect_cookie_health(ck)
                if state != HEALTH_OK:
                    return {
                        "ok": False,
                        "message": f"Cookie 无效（{state}）：{reason}，未保存任何改动",
                        "code": 400,
                    }
                items[idx]["cookie"] = ck
            pool = _persist()
            return {"ok": True, "message": f"已更新条目「{new_nm or nm}」", "pool": pool}

        if action == "delete":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            items.pop(idx)
            pool = _persist()
            return {"ok": True, "message": f"已删除条目「{nm}」", "pool": pool}

        if action == "toggle":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            items[idx]["enabled"] = not bool(items[idx].get("enabled", True))
            pool = _persist()
            now_state = "启用" if items[idx]["enabled"] else "停用"
            return {"ok": True, "message": f"已{now_state}条目「{nm}」", "pool": pool}

        if action == "set_default":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            ck = str(items[idx].get("cookie") or "")
            state, reason = detect_cookie_health(ck)
            if state not in (HEALTH_OK, HEALTH_EXPIRING):
                return {
                    "ok": False,
                    "message": f"条目「{nm}」当前不可用（{state}）：{reason}",
                    "code": 400,
                }
            self._write_single_cookie_encrypted(ck)
            self.reload_if_external_changed()
            return {"ok": True, "message": f"已把「{nm}」设为默认 Cookie，下一轮生效"}

        if action == "refresh_selected":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            ck = str(cookie or "").strip()
            if not ck:
                return {"ok": False, "message": "请粘贴新的 Cookie 内容", "code": 400}
            state, reason = detect_cookie_health(ck)
            if state != HEALTH_OK:
                return {
                    "ok": False,
                    "message": f"Cookie 无效（{state}）：{reason}，未保存任何改动",
                    "code": 400,
                }
            items[idx]["cookie"] = ck
            pool = _persist()
            return {"ok": True, "message": f"已刷新条目「{nm}」", "pool": pool}

        if action == "auto_disable_expired":
            disabled = 0
            for item in items:
                if not bool(item.get("enabled", True)):
                    continue
                state, _reason = detect_cookie_health(str(item.get("cookie") or ""))
                if state in ("expired", "no_token", "missing", "invalid_encrypt"):
                    item["enabled"] = False
                    disabled += 1
            if disabled:
                pool = _persist()
            else:
                pool = self.cookie_pool_list()["pool"]
            return {
                "ok": True,
                "message": f"已自动停用 {disabled} 个过期/无效条目（保留条目）",
                "pool": pool,
            }

        return {"ok": False, "message": f"未知操作：{action}", "code": 400}

    def _read_pool_plaintext(self) -> List[Dict[str, Any]]:
        """读取磁盘 config 的 monitor.cookie_pool 并逐条解密为明文。

        Returns:
            [{"name": str, "cookie": str(明文；解密失败为空), "enabled": bool}, ...]。
            解密失败条目保留 name/enabled，cookie 置空（前端显示 invalid_encrypt）。
        """
        data = gui.load_raw_config(self.config_path)
        monitor = data.get("monitor") if isinstance(data, dict) else None
        monitor = monitor if isinstance(monitor, dict) else {}
        items: List[Dict[str, Any]] = []
        for entry in monitor.get("cookie_pool") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            raw = str(entry.get("cookie") or "").strip()
            if not name:
                continue
            cookie = raw
            if secure.is_encrypted(raw):
                decrypted = secure.decrypt_text(raw)
                if not decrypted:
                    logger.warning("Cookie 池条目 %s 密文无法解密（跳过内容）", name)
                    cookie = ""
                else:
                    cookie = decrypted
            try:
                enabled = bool(entry.get("enabled", True))
            except Exception:  # noqa: BLE001 - 脏数据容错
                enabled = True
            items.append({"name": name, "cookie": cookie, "enabled": enabled})
        return items

    def _write_pool_encrypted(self, items: List[Dict[str, Any]]) -> None:
        """把明文池序列化为 fernet1: 密文并**原子写盘**（同目录临时文件 + os.replace）。

        磁盘上不存在明文持久化窗口（R4）；写盘后由调用方触发 reload。
        """
        data = gui.load_raw_config(self.config_path)
        if not isinstance(data, dict):
            data = {}
        monitor = data.get("monitor")
        if not isinstance(monitor, dict):
            monitor = {}
        serialized = serialize_cookie_pool(items, encrypt=True)
        monitor["cookie_pool"] = serialized
        data["monitor"] = monitor
        parent = os.path.dirname(os.path.abspath(self.config_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                yaml.safe_dump(
                    data, fp, allow_unicode=True, sort_keys=False, default_flow_style=False
                )
            os.replace(tmp_path, self.config_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info("Cookie 池已加密写盘（%d 条，fernet1: 密文）", len(serialized))

    def _write_single_cookie_encrypted(self, cookie: str) -> None:
        """把明文 Cookie 加密写入 monitor.cookies（fernet1:），原子写盘。

        Raises:
            ValueError: 加密不可用（Fernet 密钥缺失 / encrypt_text 降级返回明文）。
        """
        cipher = secure.encrypt_text(cookie)
        if not secure.is_encrypted(cipher):
            raise ValueError("Cookie 加密不可用（Fernet 密钥缺失或不可用），未保存任何改动")
        data = gui.load_raw_config(self.config_path)
        if not isinstance(data, dict):
            data = {}
        monitor = data.get("monitor")
        if not isinstance(monitor, dict):
            monitor = {}
        monitor["cookies"] = cipher
        monitor["cookies_encrypted"] = True
        data["monitor"] = monitor
        parent = os.path.dirname(os.path.abspath(self.config_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                yaml.safe_dump(
                    data, fp, allow_unicode=True, sort_keys=False, default_flow_style=False
                )
            os.replace(tmp_path, self.config_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info("已把默认 Cookie 加密写入 monitor.cookies（fernet1: 密文）")

    def _cookie_expire_text(self, cookie: str, enabled: bool = True) -> str:
        """计算 Cookie 过期时间展示文本（未知 → 「未知」；停用/空 → 「—」）。"""
        raw = str(cookie or "").strip()
        if not enabled or not raw:
            return "—"
        ts = cookie_token_timestamp(raw)
        if ts is None:
            return "未知"
        expire_ms = int(ts) + TOKEN_TTL_MS
        return datetime.fromtimestamp(expire_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------ #
    # P2-02 / P2-04 / P2-05：黑名单 / 售出撤销 / 清空记录
    # ------------------------------------------------------------------ #
    def clear_records(self) -> Dict[str, Any]:
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

    def unmark_record(self, product_id: str) -> Dict[str, Any]:
        """把商品恢复为在架（撤销售出标记，幂等）。"""
        pid = str(product_id or "").strip()
        if not pid:
            return {"ok": False, "message": "product_id 不能为空", "code": 400}
        updated = self.storage.unmark_sold_out(pid)
        logger.info("已把商品 %s 恢复为在架", pid)
        return {"ok": True, "updated": int(updated), "message": "已恢复为在架"}


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
