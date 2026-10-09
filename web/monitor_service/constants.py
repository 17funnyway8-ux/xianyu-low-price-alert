"""Web 服务层常量（v1.9.7 从 monitor_service.py 拆出）。"""

from __future__ import annotations

import logging

from xianyu_alert import gui, secure  # noqa: F401  # gui 防御性导入 tkinter，容器可 import

logger = logging.getLogger(__name__)

#: 日志环形缓冲最大行数（固定容量，超出后丢弃最旧行，避免内存膨胀）
LOG_BUFFER_MAXLEN = 2000
#: 状态条展示时返回的最近日志条数
LOG_STATUS_LIMIT = 200
#: 关闭 monitor 线程时等待收尾的最大秒数（对齐 GUI CLOSE_JOIN_TIMEOUT=5.0）
MONITOR_JOIN_TIMEOUT = 5.0
#: SQLite busy timeout（毫秒）：Web 读与 monitor 写并发时的等待上限
SQLITE_BUSY_TIMEOUT_MS = 5000

#: 服务端刷新后的令牌落盘**最小间隔**（秒）。
#: mtop 令牌每次请求都会滑动续期，但没必要每次都写盘 —— 写盘会触发 mtime
#: 热重载，过于频繁等于每轮都在重建 Config。5 分钟一次足以保证「重启后令牌
#: 仍然新鲜」（令牌本身有效期 90 分钟，见 `cookie.TOKEN_TTL_MS`）。
TOKEN_PERSIST_MIN_INTERVAL = 300.0

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

__all__ = [
    "LOG_BUFFER_MAXLEN",
    "LOG_STATUS_LIMIT",
    "MONITOR_JOIN_TIMEOUT",
    "SQLITE_BUSY_TIMEOUT_MS",
    "TOKEN_PERSIST_MIN_INTERVAL",
    "SOLD_CHECK_INTERVAL",
    "SOLD_CHECK_MAX_ITEMS",
    "SOLD_REASON_DETAIL",
    "CHECK_SHELF_ITEM_TIMEOUT",
]
