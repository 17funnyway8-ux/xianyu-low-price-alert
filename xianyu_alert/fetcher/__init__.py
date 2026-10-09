"""xianyu_alert.fetcher -- 抓取层（v1.9.9 起为包）。

对外保持与原单文件相同的命名空间；内部按职责分层：

    constants  站点地址与接口常量
    base       Fetcher ABC / FetchError
    mtop_api   mtop 纯函数（签名/令牌/请求体/解析，无 IO）
    mtop       MtopFetcher（主路径）
    web        WebFetcher（兜底，解析在 web_parse.py）
    mock       MockFetcher（测试/演示）
    factory    build_fetcher
"""

from __future__ import annotations

from .base import (
    ABC,
    Fetcher,
    FetchError,
    Product,
    abstractmethod,
)  # noqa: F401

# Compat layer: the original single-module imports exposed these names at module
# level; external code and tests may reference them, so they stay re-exported.
from .constants import (
    _RET_RISK_MARKERS,
    _RET_SESSION_MARKERS,
    _RET_TOKEN_MARKERS,
    BASE_URL,
    COOKIE_GUIDE,
    ITEM_URL_TEMPLATE,
    MTOP_API_NAME,
    MTOP_APP_KEY,
    MTOP_DETAIL_API_NAME,
    MTOP_DETAIL_URL,
    MTOP_TOKEN_COOKIE,
    MTOP_TOKEN_ENC_COOKIE,
    MTOP_URL,
    PAGE_SLEEP,
    SEARCH_URL_TEMPLATE,
    annotations,
    extract_product_id,
    logger,
    logging,
    parse_price,
    parse_publish_time,
)  # noqa: F401  # noqa: F401 - compat re-export
from .factory import (
    Config,
    build_fetcher,
)  # noqa: F401
from .mock import (
    MockFetcher,
    random,
    timedelta,
)  # noqa: F401
from .mtop import (
    DEFAULT_USER_AGENT,
    Callable,
    MtopFetcher,
    contextlib,
    json,
    re,
    requests,
    time,
)  # noqa: F401
from .mtop_api import (
    Any,
    Sequence,
    _contains_any,
    _ret_text,
    build_detail_payload,
    build_search_payload,
    coerce_text,
    datetime,
    extract_token,
    format_price_bound,
    format_publish_time,
    hashlib,
    mtop_sign,
    parse_cookie_string,
    parse_detail_sold_status,
    parse_mtop_item,
    parse_mtop_result_list,
)  # noqa: F401  # noqa: F401 - compat re-export
from .web import (
    WebFetcher,
    quote_plus,
    web_parse,
)  # noqa: F401

__all__ = [
    "ABC",
    "Any",
    "BASE_URL",
    "COOKIE_GUIDE",
    "Callable",
    "Config",
    "DEFAULT_USER_AGENT",
    "FetchError",
    "Fetcher",
    "ITEM_URL_TEMPLATE",
    "MTOP_API_NAME",
    "MTOP_APP_KEY",
    "MTOP_DETAIL_API_NAME",
    "MTOP_DETAIL_URL",
    "MTOP_TOKEN_COOKIE",
    "MTOP_TOKEN_ENC_COOKIE",
    "MTOP_URL",
    "MockFetcher",
    "MtopFetcher",
    "PAGE_SLEEP",
    "Product",
    "SEARCH_URL_TEMPLATE",
    "Sequence",
    "WebFetcher",
    "_RET_RISK_MARKERS",
    "_RET_SESSION_MARKERS",
    "_RET_TOKEN_MARKERS",
    "_contains_any",
    "_ret_text",
    "abstractmethod",
    "annotations",
    "build_detail_payload",
    "build_fetcher",
    "build_search_payload",
    "coerce_text",
    "contextlib",
    "datetime",
    "extract_product_id",
    "extract_token",
    "format_price_bound",
    "format_publish_time",
    "hashlib",
    "json",
    "logger",
    "logging",
    "mtop_sign",
    "parse_cookie_string",
    "parse_detail_sold_status",
    "parse_mtop_item",
    "parse_mtop_result_list",
    "parse_price",
    "parse_publish_time",
    "quote_plus",
    "random",
    "re",
    "requests",
    "time",
    "timedelta",
    "web_parse",
]
