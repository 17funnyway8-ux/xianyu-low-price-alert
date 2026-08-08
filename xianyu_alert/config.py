"""配置加载与校验（config.yaml）。

配置结构示例见项目根目录的 `config.example.yaml`。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from . import paths, secure
from .filters import extract_required_keywords

logger = logging.getLogger(__name__)

# 默认浏览器 UA（尽量贴近真实浏览器，降低被风控概率）
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 允许的抓取器类型 / 通知通道类型
#   mtop : 真实抓取（走闲鱼 h5api mtop 接口，需登录 Cookie）—— 推荐（v3.2 起为默认）
#   web  : 旧版 HTML 解析（对闲鱼实测无效，仅作通用 HTML 站点示例保留；
#          v3.2 起 GUI 不再展示、config.example.yaml 不再推荐，代码保留供向后兼容）
#   mock : 离线演示 / 测试用确定性假数据（开发演示用）
VALID_FETCHER_TYPES = ("mtop", "web", "mock")
#   bark / webhook 为 v3 新增通道（iOS Bark 推送 / 企微、钉钉风格机器人）
VALID_CHANNEL_TYPES = ("console", "serverchan", "email", "telegram", "bark", "webhook")

#: 预置排除词默认值（v3.5 起可配置、可持久化）。
#: 这是「新关键词自动预置的模板」：GUI 添加新关键词时会把这份列表写入
#: 该关键词的 exclude_keywords；用户在 GUI「编辑预置排除词」弹窗中可增删，
#: 保存后写回 config.yaml 顶层 `preset_exclude_keywords` 字段。
#: 与 `keywords[].exclude_keywords`（单条关键词的过滤规则）是不同层级，
#: 前者是模板、后者是结果。
DEFAULT_PRESET_EXCLUDE_KEYWORDS: List[str] = ["回收", "置换", "收购", "高价回收", "收"]


class ConfigError(ValueError):
    """配置文件缺失、格式错误或校验不通过时抛出。"""


@dataclass
class KeywordRule:
    """单个关键词的监测规则。

    Attributes:
        keyword: 搜索关键词。
        max_price: 价格阈值，仅当 `price < max_price` 时才触发提醒。
        exclude_keywords: 排除词列表（v3.1）。商品文本命中任一排除词即跳过。
        required_keywords: 必含词列表（v3.1）。商品文本必须包含全部；
            为空表示不强制要求（等同关闭该过滤）。未显式配置时
            由 `filters.extract_required_keywords` 从主关键词自动提取默认值。
        enabled: 是否启用（v3.7）。停用的关键词在 GUI 中仍可见可编辑，
            保存后写回 config，但 `monitor.run_once` 会跳过它（不抓取、
            不计数、不打命中日志），用于「临时不想监控的商品，停用而不删除」。
            缺省 True，向后兼容旧 config 不写该字段的配置。
    """

    keyword: str
    max_price: float
    exclude_keywords: List[str] = field(default_factory=list)
    required_keywords: List[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class CookiePoolItem:
    """Cookie 池中的单个账号条目（v3.2 多 Cookie 管理）。

    Attributes:
        name: 用户自定义标识（如「主账号」「小号1」），仅用于界面展示。
        cookie: Cookie 请求头字符串。**内存中为明文**；YAML 落盘时
            由保存路径（GUI / 配置文件）负责以 DPAPI 密文（dpapi1:）存储，
            加载时自动解密——与 `monitor.cookies` 的既有语义保持一致。
        enabled: 是否参与轮换（停用的条目不参与抓取请求）。
    """

    name: str
    cookie: str
    enabled: bool = True


@dataclass
class MonitorConfig:
    """监测循环相关配置。

    Attributes:
        interval_seconds: 监测间隔（秒）。v3.2 起默认 600（10 分钟），
            过短容易触发闲鱼风控。
        user_agent: 浏览器 UA。
        cookies: 单个 Cookie（明文，加载时若为密文自动解密）。
        cookies_encrypted: cookies 是否以 DPAPI 密文（dpapi1: 前缀）存储。
        cookie_pool: 多账号 Cookie 池（v3.2）。**池优先、单值兜底**：
            池中启用条目非空时按轮次轮换取用；池为空时回退 `cookies` 字段。
    """

    interval_seconds: int = 600
    user_agent: str = DEFAULT_USER_AGENT
    cookies: str = ""
    #: cookies 是否以 DPAPI 密文（dpapi1: 前缀）存储；加载时自动解密
    cookies_encrypted: bool = False
    #: 多账号 Cookie 池（v3.2）；元素为 CookiePoolItem，cookie 为明文
    cookie_pool: List["CookiePoolItem"] = field(default_factory=list)


@dataclass
class FetcherConfig:
    """抓取器配置。"""

    #: 抓取器类型。v3.2 起默认 mtop（真实抓取）；mock 仅开发演示；
    #: web 为旧版 HTML 解析，已废弃（保留代码，GUI 不再暴露）
    type: str = "mtop"
    # 以下参数仅 mock 抓取器使用
    mock_products_per_round: int = 5
    mock_fail_rounds: List[int] = field(default_factory=list)
    # 以下参数仅 mtop 抓取器使用：每页拉取的商品数量
    page_size: int = 30
    #: mtop 多页抓取：共抓取多少页（默认 1 不改变现状；翻页增加风控风险）
    pages: int = 1
    #: 翻页之间的限速间隔（秒）
    page_sleep: float = 2.0


@dataclass
class StorageConfig:
    """持久化存储配置。"""

    path: str = os.path.join("state", "xianyu_alert.db")


@dataclass
class NotifyChannel:
    """单个通知通道配置。

    Attributes:
        type: 通道类型（console / serverchan / email / telegram / bark / webhook）。
        options: 该通道所需的参数字典（不含 type 本身）。
    """

    type: str
    options: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """读取通道参数。"""
        return self.options.get(key, default)


@dataclass
class NotifyConfig:
    """通知配置。"""

    channels: List[NotifyChannel] = field(default_factory=list)


@dataclass
class Config:
    """完整配置对象。

    Attributes:
        keywords: 关键词监测规则列表。
        monitor: 监测循环配置。
        fetcher: 抓取器配置。
        storage: 持久化存储配置。
        notify: 通知配置。
        preset_exclude_keywords: 预置排除词模板（v3.5）。
            添加新关键词时自动写入该关键词的 exclude_keywords；
            缺省时回退 `DEFAULT_PRESET_EXCLUDE_KEYWORDS`（向后兼容）。
    """

    keywords: List[KeywordRule] = field(default_factory=list)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    fetcher: FetcherConfig = field(default_factory=FetcherConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    preset_exclude_keywords: List[str] = field(
        default_factory=lambda: list(DEFAULT_PRESET_EXCLUDE_KEYWORDS)
    )

    # ------------------------------------------------------------------ #
    def keyword_map(self) -> Dict[str, float]:
        """返回 {关键词: 价格阈值} 的映射，便于快速查阈值。"""
        return {rule.keyword: rule.max_price for rule in self.keywords}


# ---------------------------------------------------------------------- #
# 解析逻辑
# ---------------------------------------------------------------------- #
def _as_dict(value: Any, name: str) -> Dict[str, Any]:
    """把配置节点转换为 dict；None 视为空 dict。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置节点 `{name}` 必须是一个映射（dict），当前为 {type(value).__name__}")
    return value


def _parse_string_list(value: Any, name: str) -> List[str]:
    """解析字符串列表配置；None / 缺失视为空列表。

    Args:
        value: 原始值（应为 list[str] 或 None）。
        name: 字段名，用于报错信息。

    Returns:
        去空白、去空串、去重、保序后的字符串列表。

    Raises:
        ConfigError: 值存在但类型不是列表。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"`{name}` 必须是字符串列表，当前为 {type(value).__name__}")
    result: List[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        if text not in result:
            result.append(text)
    return result


def _parse_keywords(raw: Any) -> List[KeywordRule]:
    """解析并校验 keywords 列表。"""
    if not raw:
        raise ConfigError("`keywords` 不能为空，至少需要配置一个关键词")
    if not isinstance(raw, list):
        raise ConfigError("`keywords` 必须是列表")

    rules: List[KeywordRule] = []
    seen: set = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"`keywords[{index}]` 必须是映射，例如 {{keyword: Switch, max_price: 800}}")

        keyword = str(item.get("keyword", "")).strip()
        if not keyword:
            raise ConfigError(f"`keywords[{index}].keyword` 不能为空")
        if keyword in seen:
            raise ConfigError(f"`keywords` 中存在重复关键词：{keyword}")
        seen.add(keyword)

        if "max_price" not in item:
            raise ConfigError(f"`keywords[{index}]`（{keyword}）缺少 max_price")
        try:
            max_price = float(item["max_price"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"`keywords[{index}].max_price` 必须是数字：{item['max_price']!r}") from exc
        if max_price <= 0:
            raise ConfigError(f"`keywords[{index}].max_price` 必须为正数，当前 {max_price}")

        exclude_keywords = _parse_string_list(
            item.get("exclude_keywords"), f"keywords[{index}].exclude_keywords"
        )
        if "required_keywords" in item:
            # 显式配置（含空列表 = 关闭强制必含）→ 原样使用
            required_keywords = _parse_string_list(
                item.get("required_keywords"), f"keywords[{index}].required_keywords"
            )
        else:
            # 未显式配置 → 从主关键词自动提取「数字 + 可选单位」片段作为默认必含词
            required_keywords = extract_required_keywords(keyword)

        # v3.7：启用/停用标记。缺省 True（旧 config 不写该字段 → 默认启用）；
        # 对脏数据（非布尔 / 字符串 "false" 等）做容错，绝不抛异常。
        raw_enabled = item.get("enabled", True)
        if isinstance(raw_enabled, str):
            # YAML 解析出的 `enabled: "false"` 是字符串，直接 bool() 会变 True
            enabled = raw_enabled.strip().lower() not in ("0", "false", "no", "off", "")
        elif isinstance(raw_enabled, bool):
            enabled = raw_enabled
        elif raw_enabled is None:
            enabled = True
        else:
            try:
                enabled = bool(raw_enabled)
            except Exception:  # noqa: BLE001 - 脏数据容错
                enabled = True

        rules.append(
            KeywordRule(
                keyword=keyword,
                max_price=max_price,
                exclude_keywords=exclude_keywords,
                required_keywords=required_keywords,
                enabled=enabled,
            )
        )
    return rules


def _parse_cookie_pool(raw: Any) -> List[CookiePoolItem]:
    """解析并校验 `monitor.cookie_pool`（v3.2 多 Cookie 管理）。

    支持两种 Cookie 存储形态（与 `monitor.cookies` 一致）：
        - 明文（向后兼容）；
        - `dpapi1:` 密文（GUI 保存时加密，加载时自动解密）。
    解密失败 / 字段非法的条目会被**跳过**（打 warning），不阻断整体加载。

    Args:
        raw: YAML 中的 cookie_pool 节点（应为 list[dict] 或 None）。

    Returns:
        解析后的 CookiePoolItem 列表（cookie 字段为明文）。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("`monitor.cookie_pool` 必须是列表")
    items: List[CookiePoolItem] = []
    seen_names: set = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning("`monitor.cookie_pool[%d]` 不是映射，已跳过", index)
            continue
        name = str(entry.get("name") or "").strip()
        cookie_raw = str(entry.get("cookie") or "").strip()
        if not name or not cookie_raw:
            logger.warning("`monitor.cookie_pool[%d]` 缺少 name 或 cookie，已跳过", index)
            continue
        if secure.is_encrypted(cookie_raw):
            cookie = secure.decrypt_text(cookie_raw)
            if not cookie:
                logger.warning(
                    "`monitor.cookie_pool[%d]`（%s）密文无法解密，已跳过", index, name
                )
                continue
        else:
            cookie = cookie_raw
        if name in seen_names:
            logger.warning("`monitor.cookie_pool` 中存在重复名称：%s（保留首个）", name)
            continue
        seen_names.add(name)
        try:
            enabled = bool(entry.get("enabled", True))
        except Exception:  # noqa: BLE001 - 脏数据容错
            enabled = True
        items.append(CookiePoolItem(name=name, cookie=cookie, enabled=enabled))
    return items


def serialize_cookie_pool(
    pool: Any, encrypt: bool = True
) -> List[Dict[str, Any]]:
    """把 Cookie 池序列化为可写盘 YAML 的字典列表（v3.2）。

    Args:
        pool: 形如 [{"name": str, "cookie": str(明文), "enabled": bool}] 的列表。
        encrypt: True 时把每个 cookie 用 DPAPI 加密（不可用时降级明文）。

    Returns:
        [{"name": ..., "cookie": (密文|明文), "enabled": bool}] 列表。
    """
    result: List[Dict[str, Any]] = []
    for item in pool or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        cookie = str(item.get("cookie") or "").strip()
        if not name or not cookie:
            continue
        stored = secure.encrypt_text(cookie) if encrypt else cookie
        result.append(
            {
                "name": name,
                "cookie": stored,
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return result


def _parse_monitor(raw: Any) -> MonitorConfig:
    """解析并校验 monitor 节点。

    支持 Cookie 两种存储形态：
        - 明文（向后兼容存量 config.yaml）；
        - `dpapi1:` 密文（v3 默认，保存时加密）。
    密文在加载时自动解密；解密失败置空并打 warning（提示重新登录）。

    v3.2 起支持 `monitor.cookie_pool` 多账号 Cookie 池；
    解析时逐条解密，非法条目跳过。
    """
    data = _as_dict(raw, "monitor")
    interval_raw = data.get("interval_seconds", 600)
    try:
        interval = int(interval_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"`monitor.interval_seconds` 必须是整数：{interval_raw!r}") from exc
    if interval <= 0:
        raise ConfigError(f"`monitor.interval_seconds` 必须大于 0，当前 {interval}")

    user_agent = str(data.get("user_agent") or DEFAULT_USER_AGENT).strip()
    cookies_raw = str(data.get("cookies") or "").strip()
    cookies_encrypted = bool(data.get("cookies_encrypted", False))

    if secure.is_encrypted(cookies_raw):
        # 前缀本身即是加密标记：无论 cookies_encrypted 字段如何，都按密文处理
        cookies_encrypted = True
        cookies = secure.decrypt_text(cookies_raw)
    else:
        cookies = cookies_raw

    return MonitorConfig(
        interval_seconds=interval,
        user_agent=user_agent,
        cookies=cookies,
        cookies_encrypted=cookies_encrypted,
        cookie_pool=_parse_cookie_pool(data.get("cookie_pool")),
    )


def _parse_fetcher(raw: Any) -> FetcherConfig:
    """解析并校验 fetcher 节点。"""
    data = _as_dict(raw, "fetcher")
    ftype = str(data.get("type") or "mtop").strip().lower()
    if ftype not in VALID_FETCHER_TYPES:
        raise ConfigError(f"`fetcher.type` 只能是 {VALID_FETCHER_TYPES} 之一，当前 {ftype!r}")

    try:
        per_round = int(data.get("mock_products_per_round", 5))
    except (TypeError, ValueError) as exc:
        raise ConfigError("`fetcher.mock_products_per_round` 必须是整数") from exc
    if per_round <= 0:
        raise ConfigError("`fetcher.mock_products_per_round` 必须大于 0")

    fail_rounds_raw = data.get("mock_fail_rounds") or []
    if not isinstance(fail_rounds_raw, list):
        raise ConfigError("`fetcher.mock_fail_rounds` 必须是整数列表")
    try:
        fail_rounds = [int(x) for x in fail_rounds_raw]
    except (TypeError, ValueError) as exc:
        raise ConfigError("`fetcher.mock_fail_rounds` 必须是整数列表") from exc

    try:
        page_size = int(data.get("page_size", 30))
    except (TypeError, ValueError) as exc:
        raise ConfigError("`fetcher.page_size` 必须是整数") from exc
    if page_size <= 0:
        raise ConfigError("`fetcher.page_size` 必须大于 0")

    try:
        pages = int(data.get("pages", 1))
    except (TypeError, ValueError) as exc:
        raise ConfigError("`fetcher.pages` 必须是整数") from exc
    if pages <= 0:
        raise ConfigError("`fetcher.pages` 必须大于等于 1")

    try:
        page_sleep = float(data.get("page_sleep", 2.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("`fetcher.page_sleep` 必须是数字") from exc
    if page_sleep < 0:
        raise ConfigError("`fetcher.page_sleep` 不能为负数")

    return FetcherConfig(
        type=ftype,
        mock_products_per_round=per_round,
        mock_fail_rounds=fail_rounds,
        page_size=page_size,
        pages=pages,
        page_sleep=page_sleep,
    )


def _parse_storage(raw: Any) -> StorageConfig:
    """解析 storage 节点。"""
    data = _as_dict(raw, "storage")
    path = str(data.get("path") or os.path.join("state", "xianyu_alert.db")).strip()
    if not path:
        raise ConfigError("`storage.path` 不能为空")
    return StorageConfig(path=path)


def _parse_notify(raw: Any) -> NotifyConfig:
    """解析 notify 节点。未配置任何通道时回退到 console。"""
    data = _as_dict(raw, "notify")
    channels_raw = data.get("channels") or []
    if not isinstance(channels_raw, list):
        raise ConfigError("`notify.channels` 必须是列表")

    channels: List[NotifyChannel] = []
    for index, item in enumerate(channels_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"`notify.channels[{index}]` 必须是映射")
        ctype = str(item.get("type", "")).strip().lower()
        if not ctype:
            raise ConfigError(f"`notify.channels[{index}]` 缺少 type")
        if ctype not in VALID_CHANNEL_TYPES:
            raise ConfigError(
                f"`notify.channels[{index}].type` 不支持：{ctype!r}，可选 {VALID_CHANNEL_TYPES}"
            )
        options = {k: v for k, v in item.items() if k != "type"}
        channels.append(NotifyChannel(type=ctype, options=options))

    if not channels:
        # 没配通道时至少保证有控制台输出，避免「监测到了但用户看不到」
        channels.append(NotifyChannel(type="console", options={}))
    return NotifyConfig(channels=channels)


def _parse_preset_exclude_keywords(data: Dict[str, Any]) -> List[str]:
    """解析顶层 `preset_exclude_keywords`（v3.5 预置排除词模板）。

    - 显式配置（含空列表 = 关闭自动预置）→ 按字符串列表解析；
    - 缺省 → 回退 `DEFAULT_PRESET_EXCLUDE_KEYWORDS`（向后兼容旧配置）。

    Args:
        data: 配置根字典。

    Returns:
        解析后的预置排除词列表。
    """
    if "preset_exclude_keywords" in data:
        return _parse_string_list(data.get("preset_exclude_keywords"), "preset_exclude_keywords")
    return list(DEFAULT_PRESET_EXCLUDE_KEYWORDS)


def config_from_dict(data: Dict[str, Any]) -> Config:
    """从已解析的字典构造 Config（便于测试直接注入配置）。

    Args:
        data: 与 config.yaml 顶层结构一致的字典。

    Returns:
        校验通过的 Config 对象。

    Raises:
        ConfigError: 任意字段校验失败。
    """
    data = _as_dict(data, "<root>")
    return Config(
        keywords=_parse_keywords(data.get("keywords")),
        monitor=_parse_monitor(data.get("monitor")),
        fetcher=_parse_fetcher(data.get("fetcher")),
        storage=_parse_storage(data.get("storage")),
        notify=_parse_notify(data.get("notify")),
        preset_exclude_keywords=_parse_preset_exclude_keywords(data),
    )


def load_config(path: Optional[str] = None) -> Config:
    """加载并校验 YAML 配置文件。

    Args:
        path: 配置文件路径；None 时使用默认路径
            （源码模式为项目根 config.yaml，frozen 后为 exe 同目录 config.yaml）。

    Returns:
        校验通过的 Config 对象。

    Raises:
        ConfigError: 文件不存在、YAML 语法错误或字段校验失败。
    """
    if path is None:
        path = paths.default_config_path()

    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在：{os.path.abspath(path)}")

    try:
        with open(path, "r", encoding="utf-8") as fp:
            raw = yaml.safe_load(fp)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 解析失败：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"配置文件读取失败：{exc}") from exc

    if raw is None:
        raise ConfigError(f"配置文件为空：{path}")

    return config_from_dict(raw)
