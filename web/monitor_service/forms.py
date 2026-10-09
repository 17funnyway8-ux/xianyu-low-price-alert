"""Web 配置表单 <-> 配置对象 的纯转换（v1.9.7 拆出）。

这是 web 层里最容易被单测覆盖的部分：无 IO、无状态。
"""

from __future__ import annotations

import logging
from typing import Any

from xianyu_alert import gui, secure  # noqa: F401  # gui 防御性导入 tkinter，容器可 import
from xianyu_alert.config import (
    ConfigError,
)

logger = logging.getLogger(__name__)

def web_form_from_config(data: dict[str, Any]) -> dict[str, Any]:
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

    keywords: list[dict[str, Any]] = []
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


def config_from_web_form(form: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """把 Web 表单转换为可写盘的配置字典（复用 gui.build_config_dict）。

    - 关键词/通道/间隔等字段由表单覆盖；
    - **Cookie 字段不在表单中回传**（GET 已脱敏），这里从 base 原样保留——
      Cookie 只能通过 `POST /api/cookie/save`（校验 + Fernet 加密）更新，
      杜绝表单保存路径误写明文（共享知识 7）。
    - v1.8 新增字段 cookie_alert_enabled / cookie_check_interval_seconds
      （gui.build_config_dict 不覆盖，这里显式写回）。
    """
    keywords_raw = form.get("keywords") or []
    keywords: list[tuple[str, float]] = []
    keyword_filters: dict[str, dict[str, list[str]]] = {}
    keyword_enabled: dict[str, bool] = {}
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

__all__ = [
    "web_form_from_config",
    "config_from_web_form",
]
