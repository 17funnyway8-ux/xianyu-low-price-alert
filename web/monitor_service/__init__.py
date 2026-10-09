"""web.monitor_service -- Web service layer (package since v1.9.7).

Same public namespace as the former single module; internally split into
constants / logging_bridge / forms / cookie_pool / shelf_check / keepalive / service

so each concern can be tested and changed on its own.
"""

from __future__ import annotations

# Compat layer: the former single-module imports exposed these at module level;
# external code/tests (e.g. resetting the singleton) may still reference them.
from .constants import (
    _LOG_LOGGER_NAME,
    CHECK_SHELF_ITEM_TIMEOUT,
    LOG_BUFFER_MAXLEN,
    LOG_STATUS_LIMIT,
    MONITOR_JOIN_TIMEOUT,
    SOLD_CHECK_INTERVAL,
    SOLD_CHECK_MAX_ITEMS,
    SOLD_REASON_DETAIL,
    SQLITE_BUSY_TIMEOUT_MS,
    TOKEN_PERSIST_MIN_INTERVAL,
    annotations,
    gui,
    logger,
    logging,
    secure,
)  # noqa: F401  # noqa: F401 - compat re-export
from .cookie_pool import (
    HEALTH_INVALID_ENCRYPT,
    HEALTH_MISSING,
    TOKEN_TTL_MS,
    CookiePoolMixin,
    cookie_has_token,
    cookie_is_usable,
    cookie_token_timestamp,
    detect_cookie_health,
    os,
    serialize_cookie_pool,
    tempfile,
    time,
    yaml,
)  # noqa: F401  # noqa: F401 - compat re-export
from .forms import (
    ConfigError,
    config_from_web_form,
    web_form_from_config,
)  # noqa: F401
from .keepalive import (
    KeepaliveMixin,
)  # noqa: F401
from .logging_bridge import (
    TYPE_CHECKING,
    Any,
    ServiceLogHandler,
    SseBroadcaster,
    _ensure_log_handler,
    _log_handler,
    asyncio,
    contextlib,
    datetime,
    itertools,
    threading,
)  # noqa: F401  # noqa: F401 - compat re-export
from .service import (
    Config,
    Monitor,
    MonitorService,
    Storage,
    _service_instance,
    build_notifiers,
    config_from_dict,
    deque,
    get_service,
    load_config,
    reset_service,
)  # noqa: F401
from .shelf_check import (
    ShelfCheckMixin,
    build_fetcher,
)  # noqa: F401

__all__ = [
    "Any",
    "CHECK_SHELF_ITEM_TIMEOUT",
    "Config",
    "ConfigError",
    "CookiePoolMixin",
    "HEALTH_INVALID_ENCRYPT",
    "HEALTH_MISSING",
    "KeepaliveMixin",
    "LOG_BUFFER_MAXLEN",
    "LOG_STATUS_LIMIT",
    "MONITOR_JOIN_TIMEOUT",
    "Monitor",
    "MonitorService",
    "SOLD_CHECK_INTERVAL",
    "SOLD_CHECK_MAX_ITEMS",
    "SOLD_REASON_DETAIL",
    "SQLITE_BUSY_TIMEOUT_MS",
    "ServiceLogHandler",
    "ShelfCheckMixin",
    "SseBroadcaster",
    "Storage",
    "TOKEN_PERSIST_MIN_INTERVAL",
    "TOKEN_TTL_MS",
    "TYPE_CHECKING",
    "_LOG_LOGGER_NAME",
    "_ensure_log_handler",
    "_log_handler",
    "_service_instance",
    "annotations",
    "asyncio",
    "build_fetcher",
    "build_notifiers",
    "config_from_dict",
    "config_from_web_form",
    "contextlib",
    "cookie_has_token",
    "cookie_is_usable",
    "cookie_token_timestamp",
    "datetime",
    "deque",
    "detect_cookie_health",
    "get_service",
    "gui",
    "itertools",
    "load_config",
    "logger",
    "logging",
    "os",
    "reset_service",
    "secure",
    "serialize_cookie_pool",
    "tempfile",
    "threading",
    "time",
    "web_form_from_config",
    "yaml",
]
