"""配置 / 表单状态（纯逻辑，不依赖任何 Qt 显示）。

职责（对齐 macOS 适配设计文档 §3.1.1 与 Tk 版纯函数复用原则）：
    - `load_form(config_path)`：读取 config.yaml → 返回 (raw_config, form)，
      复用 `gui.load_raw_config` + `gui.config_to_form`（含 Cookie 解密）；
    - `form_to_config_dict(form, base, ...)`：把 Qt 三页签收集的表单状态组装为
      可写盘配置字典，复用 `gui.build_config_dict`（Cookie 一律 Fernet 加密）；
    - `form_to_config_object(...)`：组装并校验为 `Config` 对象（启动监控用），
      复用 `config.config_from_dict`。

本模块**不 import PySide6**，可在纯 Python 单测中直接验证。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import Config, ConfigError, config_from_dict
from ..gui import DEFAULT_DB_PATH, build_config_dict, config_to_form, load_raw_config

#: 表单状态的默认结构（与 gui.config_to_form 返回结构一致）
DEFAULT_FORM: Dict[str, Any] = {
    "keywords": [],
    "keyword_enabled": {},
    "keyword_filters": {},
    "interval": 600,
    "fetcher_type": "mtop",
    "cookies": "",
    "cookies_was_encrypted": False,
    "cookies_undecryptable": False,
    "user_agent": "",
    "storage_path": DEFAULT_DB_PATH,
    "pages": 1,
    "channels": {},
    "cookie_pool": [],
    "preset_exclude_keywords": [],
}


def load_form(config_path: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """读取配置文件并转换为界面表单状态。

    Args:
        config_path: 配置文件路径。

    Returns:
        (raw_config, form) 二元组：
        - raw_config: 原始配置字典（写盘时作为 base 保留未覆盖字段）；
        - form: config_to_form 产物（三个页签据此初始化控件）。
    """
    raw = load_raw_config(config_path)
    form = config_to_form(raw)
    return raw, form


def form_to_config_dict(
    form: Dict[str, Any],
    base: Optional[Dict[str, Any]] = None,
    channels: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """把表单状态组装为可写盘配置字典。

    Args:
        form: 表单状态（config_to_form 结构，字段可由 Qt 页签更新）。
        base: 原始配置字典（保留用户手工添加的其它字段）。
        channels: 通知通道状态 {ctype: {"enabled": bool, "options": {...}}}；
            None 时使用 form 中的 channels。

    Returns:
        可直接 yaml.safe_dump 的配置字典（Cookie 以 fernet1: 密文落盘）。
    """
    f = dict(form or {})
    data = build_config_dict(
        keywords=list(f.get("keywords") or []),
        interval_seconds=int(f.get("interval", 600)),
        fetcher_type=str(f.get("fetcher_type", "mtop")),
        cookies=str(f.get("cookies", "") or ""),
        storage_path=str(f.get("storage_path") or DEFAULT_DB_PATH),
        channels=channels if channels is not None else (f.get("channels") or {}),
        base=base,
        pages=int(f.get("pages", 1)),
        encrypt_cookies=True,  # Fernet 跨平台可用，一律加密落盘
        keyword_filters=f.get("keyword_filters"),
        cookie_pool=f.get("cookie_pool"),
        preset_exclude_keywords=f.get("preset_exclude_keywords"),
        keyword_enabled=f.get("keyword_enabled"),
    )
    return data


def form_to_config_object(
    form: Dict[str, Any],
    base: Optional[Dict[str, Any]] = None,
    channels: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Config:
    """把表单状态组装为校验后的 Config 对象（启动监控用）。

    Args:
        form: 表单状态。
        base: 原始配置字典。
        channels: 通知通道状态；None 时使用 form 中的 channels。

    Returns:
        校验通过的 Config 实例。

    Raises:
        ConfigError / ValueError: 配置不合法（由 config_from_dict 抛出）。
    """
    data = form_to_config_dict(form, base=base, channels=channels)
    return config_from_dict(data)
