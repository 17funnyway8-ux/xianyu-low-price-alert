"""商品抓取器。

提供三种实现：
    MtopFetcher : **推荐** —— 走闲鱼 h5api mtop 接口，是唯一能真正抓到数据的方式。
    WebFetcher  : 旧版 HTML 解析，对闲鱼**实测无效**（JS 渲染），仅作通用 HTML 站点示例保留。
    MockFetcher : 确定性伪造数据，用于测试与开箱即用的演示。

===================== 为什么必须用 MtopFetcher =====================
闲鱼（goofish.com）是**强反爬 + 前端 JS 渲染**的站点：
    1. 搜索结果由前端 JS 通过带签名（sign/token）的 mtop 接口异步拉取，
       `GET /search?q=xx` 返回的 HTML 里**根本没有商品数据**（实测 0 条）；
    2. 匿名请求 mtop 接口会被风控拦截，返回
       `{"ret":["RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!"]}` 且不下发 `_m_h5_tk`；
    3. 因此**必须**携带用户登录后的真实 Cookie（含 `_m_h5_tk`），
       可通过 `python -m xianyu_alert.cli login` 一键获取。
==================================================================
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from .config import Config, DEFAULT_USER_AGENT
from .models import Product

logger = logging.getLogger(__name__)

# 闲鱼 Web 站点
BASE_URL = "https://www.goofish.com"
SEARCH_URL_TEMPLATE = BASE_URL + "/search?q={keyword}"
ITEM_URL_TEMPLATE = BASE_URL + "/item?id={product_id}"

# ---------------------------------------------------------------------- #
# mtop 接口常量
# ---------------------------------------------------------------------- #
#: 闲鱼 PC 搜索 mtop 接口
MTOP_API_NAME = "mtop.taobao.idlemtopsearch.pc.search"
MTOP_URL = f"https://h5api.m.goofish.com/h5/{MTOP_API_NAME}/1.0/"
#: 商品详情 mtop 接口（v3.7 实机探测可用）：返回 itemDO.itemStatusStr / itemStatus
#: 等状态字段，用于「校验提醒记录中的商品是否已售出/下架」（需求 3，方案 B）。
#: 候选名 mtop.taobao.idle.item.detail 等 5 个已实测返回
#: `FAIL_SYS_API_NOT_FOUNDED`（不存在），正确名称为 pc.detail。
MTOP_DETAIL_API_NAME = "mtop.taobao.idle.pc.detail"
MTOP_DETAIL_URL = f"https://h5api.m.goofish.com/h5/{MTOP_DETAIL_API_NAME}/1.0/"
#: mtop 签名用的 appKey（闲鱼 PC 站固定值）
MTOP_APP_KEY = "34839810"
#: 关键 Cookie 名，token 取其下划线前半段
MTOP_TOKEN_COOKIE = "_m_h5_tk"

#: 风控拦截特征（触发后需要放慢频率）
_RET_RISK_MARKERS = ("RGV587_ERROR", "被挤爆", "FAIL_SYS_ILLEGAL_ACCESS", "SM::")
#: 令牌过期特征（mtop 标准行为：用新下发的 _m_h5_tk 重算 sign 重试一次即可）
_RET_TOKEN_MARKERS = (
    "FAIL_SYS_TOKEN_EXOIRED",  # 阿里官方拼写错误，保留原样
    "FAIL_SYS_TOKEN_EXPIRED",
    "FAIL_SYS_TOKEN_EMPTY",
    "TOKEN_EMPTY",
    "令牌过期",
    "令牌为空",
)
#: 登录态失效特征
_RET_SESSION_MARKERS = (
    "FAIL_SYS_SESSION_EXPIRED",
    "SESSION_EXPIRED",
    "NEED_LOGIN",
    "RET_LOGIN",
    "未登录",
    "会话失效",
)

#: 未配置 Cookie 时的统一引导语
COOKIE_GUIDE = (
    "未配置登录 Cookie，请先运行 `python -m xianyu_alert.cli login` 获取"
    "（或在图形界面「监控配置」页点击「获取 Cookie」）。"
)

# 从链接中提取商品 ID：/item/123456、/items/123456、?id=123456
_ID_PATTERNS = (
    re.compile(r"/items?/(\d{6,})"),
    re.compile(r"[?&]id=(\d{6,})"),
)
# 价格文本：¥1,299.00 / 1299 / 1299.5
_PRICE_PATTERN = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)")
#: 无法定价的关键词（闲鱼常见「面议 / 电议 / 私聊 / 咨询」文案，直接放弃解析）
_PRICE_BLOCK_WORDS = ("面议", "电议", "私聊", "咨询")
# 发布时间文案：3分钟前 / 2小时前 / 昨天 / 2024-05-01
_TIME_PATTERN = re.compile(
    r"(\d+\s*(?:秒|分钟|小时|天|周|个月|月|年)前|刚刚|今天\s*\d{1,2}:\d{2}|昨天\s*\d{1,2}:\d{2}"
    r"|昨天|前天|\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?|\d{1,2}-\d{1,2}\s*(?:发布)?)"
)

#: 翻页之间的固定限速（秒）：降低多页抓取触发风控的概率
PAGE_SLEEP = 2.0


class FetchError(RuntimeError):
    """抓取失败（网络异常 / 状态码异常 / 重试耗尽）时抛出。"""


# ---------------------------------------------------------------------- #
# 抽象基类
# ---------------------------------------------------------------------- #
class Fetcher(ABC):
    """抓取器抽象基类。"""

    #: 抓取器名称，用于日志
    name: str = "fetcher"

    @abstractmethod
    def fetch(self, keyword: str) -> List[Product]:
        """按关键词抓取最新商品列表。

        Args:
            keyword: 搜索关键词。

        Returns:
            Product 列表（可能为空列表）。

        Raises:
            FetchError: 抓取过程发生不可恢复的错误。
        """
        raise NotImplementedError

    def close(self) -> None:
        """释放资源（默认无操作，子类可覆盖）。"""
        return None

    def set_cookies(self, cookie_str: str) -> None:
        """切换抓取器使用的 Cookie（v3.2 多 Cookie 池轮换）。

        默认实现为无操作；需要跟随轮换更新请求态的抓取器
        （MtopFetcher / WebFetcher）应覆盖此方法。
        fetcher 契约保持「单 Cookie」不变，轮换由 monitor 层
        每轮挑选后调用本方法注入，避免改动 fetch() 签名。

        Args:
            cookie_str: 新的 Cookie 请求头字符串（可为空串）。
        """
        return None

    def set_max_price(self, max_price: Optional[float]) -> None:
        """设置抓取时的价格上限（v3.4 服务端价格筛选）。

        默认实现为无操作；MtopFetcher 覆盖此方法把阈值写入请求体，
        使接口服务端直接按 `priceRange:0,{max_price};` 筛选，与
        闲鱼网页「最新发布 + 价格<360」的行为一致。
        fetcher 契约保持 `fetch(keyword)` 不变，阈值由 monitor 层
        每轮调用本方法注入，避免改动 fetch() 签名。

        Args:
            max_price: 价格上限（元）；None 表示不过滤价格。
        """
        return None


# ---------------------------------------------------------------------- #
# 工具函数
# ---------------------------------------------------------------------- #
def extract_product_id(url: str) -> str:
    """从商品链接中提取数字商品 ID。

    Args:
        url: 商品链接（可能是相对路径）。

    Returns:
        商品 ID 字符串；提取不到时返回空串。
    """
    if not url:
        return ""
    for pattern in _ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return ""


def parse_price(text: str) -> Optional[float]:
    """从任意文本中解析出第一个价格数字（v3 增强版）。

    支持：
        - 旧格式（保持既有行为）：`¥1,299.00` / `1299` / `1299.5`；
        - 万换算（移植 exe 资产）：`1.2万` -> 12000.0、`3.5万` -> 35000.0；
        - 关键词过滤：「面议 / 电议 / 私聊 / 咨询」等非定价文案返回 None。

    Args:
        text: 含价格的文本。

    Returns:
        解析出的价格；解析失败或命中过滤关键词时返回 None。
    """
    if not text:
        return None
    s = str(text).strip()
    # 去掉货币符号、千分位逗号与空白
    s = s.replace("￥", "").replace("¥", "").replace(",", "").replace(" ", "")
    if not s:
        return None
    # 「面议 / 电议 / 私聊 / 咨询」等无法定价的文案直接放弃
    if any(word in s for word in _PRICE_BLOCK_WORDS):
        return None
    multiplier = 1
    if "万" in s:
        multiplier = 10000
        s = s.replace("万", "")
    match = _PRICE_PATTERN.search(s)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "")) * multiplier
    except ValueError:  # pragma: no cover - 正则已保证可转换
        return None


def parse_publish_time(text: str) -> str:
    """尽力从文本中提取发布时间文案。

    Args:
        text: 商品卡片纯文本。

    Returns:
        发布时间文案；提取不到时返回空串。
    """
    if not text:
        return ""
    match = _TIME_PATTERN.search(text)
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------------------- #
# mtop 工具函数（纯函数，便于单测）
# ---------------------------------------------------------------------- #
def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """把 `k1=v1; k2=v2` 形式的 Cookie 请求头解析成字典。

    Args:
        cookie_str: Cookie 请求头字符串，可为空。

    Returns:
        {cookie 名: cookie 值} 字典；空输入返回空字典。
    """
    result: Dict[str, str] = {}
    for part in str(cookie_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            result[name] = value.strip()
    return result


def mtop_sign(token: str, timestamp: str, app_key: str, data: str) -> str:
    """计算 mtop 接口签名。

    算法：`md5(f"{token}&{t}&{appKey}&{data}")`，其中
    token 为 Cookie `_m_h5_tk` 的下划线前半段，data 为紧凑序列化的 JSON 请求体。

    Args:
        token: `_m_h5_tk` 下划线前半段。
        timestamp: 13 位毫秒时间戳字符串。
        app_key: mtop appKey。
        data: 紧凑 JSON 字符串（`separators=(",", ":")`）。

    Returns:
        32 位小写 md5 十六进制字符串。
    """
    raw = f"{token}&{timestamp}&{app_key}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def extract_token(cookie_value: str) -> str:
    """从 `_m_h5_tk` 的值中取出签名 token（下划线前半段）。

    Args:
        cookie_value: 形如 `abc123def_1700000000000` 的 Cookie 值。

    Returns:
        下划线前的 token；输入为空时返回空串。
    """
    value = str(cookie_value or "").strip()
    if not value:
        return ""
    return value.split("_")[0]


def coerce_text(value: Any) -> str:
    """把 mtop 返回的「可能是 str / dict / list 富文本」的字段压成纯字符串。

    闲鱼的 exContent 里 title、price 等字段有时是富文本片段数组，
    例如 `[{"text": "¥"}, {"text": "1299"}]`，需要拼接后再解析。

    Args:
        value: 任意结构的字段值。

    Returns:
        压平后的字符串；无法提取时返回空串。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        # 优先取常见的文本键
        for key in ("text", "content", "title", "value", "label", "desc"):
            if key in value:
                text = coerce_text(value[key])
                if text:
                    return text
        parts = [coerce_text(item) for item in value.values()]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, (list, tuple)):
        parts = [coerce_text(item) for item in value]
        return "".join(part for part in parts if part).strip()
    return str(value).strip()


def format_publish_time(raw: Any) -> str:
    """把 mtop 的 publishTime（13 位毫秒时间戳）转换为 `YYYY-MM-DD HH:MM:SS`。

    Args:
        raw: 原始值，可能是字符串 / 整数 / None / 已经格式化好的文案。

    Returns:
        格式化后的时间字符串；无法解析时返回空串或原文案（绝不抛异常）。
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if not text.isdigit():
        # 已经是可读文案（如「3分钟前」），原样保留
        return text
    try:
        number = int(text)
    except ValueError:  # pragma: no cover - isdigit 已保证可转换
        return ""
    if number <= 0:
        return ""
    # 13 位为毫秒，10 位为秒
    seconds = number / 1000.0 if number >= 10 ** 12 else float(number)
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def build_search_payload(
    keyword: str,
    page_number: int = 1,
    rows_per_page: int = 30,
    max_price: Optional[float] = None,
) -> Dict[str, Any]:
    """构造闲鱼 PC 搜索的 mtop 请求体。

    v3.3 实证结论（针对「用户实测仍是综合页旧商品」）：
        - `sortField="create"` 是「最新发布」的**字段值**（对照开源协议分析
          goofish-client 的 SortField 枚举：`CREATE = 'create'`）；
        - **但排序字段必须配合 `sortValue="desc"` 才真正按发布时间倒序**。
          仅设 sortField、sortValue 留空时，服务端会回退到默认「综合」排序，
          这正是用户看到旧商品的原因（对照 goofish-client 的
          SortValue 枚举：`DESC = 'desc'`，其文档示例即
          `sortField: CREATE + sortValue: DESC = 最新优先`）。

    v3.4 实机验证（真机探测，关键词「DDR4 3200 16G」）：
        - **网页「最新发布 + 价格<360」的价格筛选是服务端筛选**：
          在请求体 `propValueStr.searchFilter` 传 `priceRange:0,360;`
          且 `fromFilter=true` 时，接口直接返回「最新发布且价格<360」的
          商品（实测 21/29 条 <360，如 ¥360/¥328/¥350/¥280），
          与网页第 1 页看到的低价新品一致；不传价格筛选时返回的
          最新 30 条被高价多根套条主导（仅 1/30 条 <360），
          这正是旧版「客户端过滤后几乎无命中」的根因。
        - 因此本函数新增 `max_price` 参数：调用方（monitor）把关键词
          阈值传入，接口服务端直接筛价，避免「抓全量最新再本地过滤」
          与网页结果不一致的问题。

    Args:
        keyword: 搜索关键词。
        page_number: 页码（从 1 开始）。
        rows_per_page: 每页条数。
        max_price: 可选。价格上限（元）。传入时在服务端按
            `priceRange:0,{max_price};` 筛选并置 `fromFilter=true`；
            不传时保持旧行为（`propValueStr={}` / `fromFilter=false`）。

    Returns:
        待紧凑序列化的请求体字典。
    """
    payload = {
        "pageNumber": int(page_number),
        "keyword": str(keyword),
        "fromFilter": False,
        "rowsPerPage": int(rows_per_page),
        "sortValue": "desc",
        "sortField": "create",
        "customDistance": "",
        "gps": "",
        "propValueStr": {},
        "customGps": "",
        "searchReqFromPage": "pcSearch",
        "extraFilterValue": "{}",
        "userPositionJson": "{}",
    }
    if max_price is not None:
        payload["fromFilter"] = True
        payload["propValueStr"] = {
            "searchFilter": f"priceRange:0,{format_price_bound(max_price)};"
        }
    return payload


def format_price_bound(value: float) -> str:
    """把价格阈值格式化为 priceRange 使用的整数字符串。

    网页筛选价格单位是「元」，服务端 priceRange 接受整数（如 360）。
    整数阈值输出无小数点（360），避免 `360.0` 这种多余小数。

    Args:
        value: 价格（元），如 360.0。

    Returns:
        格式化的边界字符串，如 `360`；非有限值时返回 `99999999` 兜底。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):  # pragma: no cover - 防御脏数据
        return "99999999"
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return "99999999"
    if number.is_integer():
        return str(int(number))
    return str(number)


def build_detail_payload(product_id: str) -> Dict[str, Any]:
    """构造闲鱼商品详情接口（mtop.taobao.idle.pc.detail）的请求体。

    v3.7 实机探测：payload 同时传 `itemId` 与 `id` 两个键均可被服务端接受，
    返回 `data.itemDO.itemStatusStr`（"在线"）等状态字段。

    Args:
        product_id: 商品 ID（数字串）。

    Returns:
        待紧凑序列化的请求体字典。
    """
    pid = str(product_id or "").strip()
    return {"itemId": pid, "id": pid}


def parse_detail_sold_status(data: Any) -> Optional[bool]:
    """从商品详情接口响应的 data 节点解析「是否在线」（纯函数，v3.7）。

    实机探测结论（mtop.taobao.idle.pc.detail，2026-08 真实 Cookie）：
        - 在架：`itemDO.itemStatusStr = "在线"`、`itemDO.itemStatus = 0`；
        - 已售出/下架：itemStatusStr 会变为「已售出 / 已下架」等文案，
          itemStatus 为非 0 值；
        - 字段缺失 / 结构异常 → 返回 None（调用方按「无法判定」处理）。

    Args:
        data: 详情接口响应的 data 节点（期望 dict）。

    Returns:
        True = 在架；False = 已售出/已下架；None = 无法判定。
    """
    if not isinstance(data, dict):
        return None
    item_do = data.get("itemDO")
    item_do = item_do if isinstance(item_do, dict) else {}

    status_str = coerce_text(item_do.get("itemStatusStr"))
    if status_str:
        if "在线" in status_str:
            return True
        if any(
            word in status_str
            for word in ("已售", "售出", "下架", "失效", "删除", "违规", "不存在")
        ):
            return False

    status = item_do.get("itemStatus")
    if status is not None:
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = None
        if code == 0:
            return True
        if code is not None and code != 0:
            return False
    return None


def _ret_text(payload: Any) -> str:
    """把响应中的 ret 字段拼成一整段文本，便于关键字匹配。"""
    if isinstance(payload, dict):
        ret = payload.get("ret")
    else:
        ret = payload
    if isinstance(ret, (list, tuple)):
        return " ".join(str(x) for x in ret)
    return str(ret or "")


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    """判断文本中是否含有任一特征串（大小写不敏感）。"""
    upper = text.upper()
    return any(marker.upper() in upper for marker in markers)


def parse_mtop_item(item: Any, keyword: str) -> Optional[Product]:
    """把 mtop resultList 中的单个元素解析为 Product。

    容错策略：任何一层结构缺失 / 类型不符都不抛异常，直接返回 None
    （调用方跳过该条并打 debug 日志），保证个别脏数据不影响整批结果。

    结构（实测）：
        item["data"]["item"]["main"]["exContent"]          -> title / area / picUrl ...
        item["data"]["item"]["main"]["clickParam"]["args"] -> price / item_id / publishTime

    Args:
        item: resultList 中的单个元素。
        keyword: 当前搜索关键词。

    Returns:
        解析成功返回 Product；字段不足以构成有效商品时返回 None。
    """
    if not isinstance(item, dict):
        return None

    data = item.get("data")
    data = data if isinstance(data, dict) else {}
    inner = data.get("item")
    inner = inner if isinstance(inner, dict) else {}
    main = inner.get("main")
    main = main if isinstance(main, dict) else {}

    ex_content = main.get("exContent")
    ex_content = ex_content if isinstance(ex_content, dict) else {}
    click_param = main.get("clickParam")
    click_param = click_param if isinstance(click_param, dict) else {}
    args = click_param.get("args")
    args = args if isinstance(args, dict) else {}

    # ---- product_id ----
    product_id = coerce_text(
        args.get("item_id")
        or args.get("itemId")
        or ex_content.get("itemId")
        or ex_content.get("item_id")
        or ex_content.get("id")
    )
    if not product_id:
        # 最后兜底：从 targetUrl 里抠数字 ID
        product_id = extract_product_id(coerce_text(main.get("targetUrl") or inner.get("targetUrl")))
    if not product_id:
        logger.debug("[mtop] 跳过缺少 item_id 的条目：%s", str(item)[:160])
        return None

    # ---- title ----
    title = coerce_text(ex_content.get("title") or ex_content.get("titleSummary") or args.get("title"))
    if not title:
        logger.debug("[mtop] 跳过缺少 title 的条目：%s", product_id)
        return None

    # ---- price ----
    price = parse_price(coerce_text(args.get("price")))
    if price is None:
        price = parse_price(coerce_text(ex_content.get("price") or ex_content.get("priceInfo")))
    if price is None:
        logger.debug("[mtop] 跳过价格无法解析的条目：%s（%s）", product_id, title[:30])
        return None

    # ---- 其余字段 ----
    publish_time = format_publish_time(args.get("publishTime") or ex_content.get("publishTime"))
    url = ITEM_URL_TEMPLATE.format(product_id=product_id)

    try:
        return Product(
            product_id=product_id,
            title=title[:200],
            price=price,
            url=url,
            publish_time=publish_time,
            keyword=keyword,
        )
    except ValueError as exc:
        logger.debug("[mtop] 跳过非法商品 %s：%s", product_id, exc)
        return None


def parse_mtop_result_list(result_list: Any, keyword: str) -> List[Product]:
    """批量解析 mtop 的 `data.resultList`。

    Args:
        result_list: 响应中的 resultList（期望是 list，其它类型按空处理）。
        keyword: 当前搜索关键词。

    Returns:
        解析成功的 Product 列表（按原顺序，已按 product_id 去重）。
    """
    if not isinstance(result_list, (list, tuple)):
        return []

    products: List[Product] = []
    seen: set = set()
    for item in result_list:
        product = parse_mtop_item(item, keyword)
        if product is None or product.product_id in seen:
            continue
        seen.add(product.product_id)
        products.append(product)
    return products


# ---------------------------------------------------------------------- #
# 真实抓取器（推荐）：mtop 接口
# ---------------------------------------------------------------------- #
class MtopFetcher(Fetcher):
    """通过闲鱼 h5api mtop 接口抓取「最新发布」商品。

    这是**唯一能真正拿到闲鱼数据**的实现。必须携带用户登录后的 Cookie
    （含 `_m_h5_tk`），可用 `python -m xianyu_alert.cli login` 一键获取。

    实现要点：
        1. 签名 `md5(token&t&appKey&data)`，token 取 `_m_h5_tk` 下划线前半段；
        2. 内部维护 requests.Session，服务端刷新的 `_m_h5_tk` 会被自动吸收；
        3. 命中「令牌过期」时用新 token 重算 sign **自动重试一次**（mtop 标准行为）；
        4. 命中风控 / 登录态失效时抛出 FetchError 并给出可操作的中文指引。

    Attributes:
        cookies: 原始 Cookie 请求头字符串。
        user_agent: 浏览器 UA。
        timeout: 单次请求超时（秒）。
        retries: 网络异常时的总尝试次数。
        page_size: 每次搜索拉取的商品条数。
        pages: 多页抓取的总页数（默认 1 不改变现状）。
        page_sleep: 翻页之间的限速秒数。
    """

    name = "mtop"

    def __init__(
        self,
        cookies: str = "",
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        retries: int = 3,
        backoff_base: float = 1.5,
        page_size: int = 30,
        pages: int = 1,
        page_sleep: float = PAGE_SLEEP,
        session: Optional[requests.Session] = None,
        sleep_func: Optional[Callable[[float], None]] = None,
    ) -> None:
        """初始化 mtop 抓取器。

        Args:
            cookies: 登录后的 Cookie 请求头字符串（必须含 `_m_h5_tk`）。
            user_agent: 浏览器 UA。
            timeout: 请求超时秒数。
            retries: 网络异常时的总尝试次数（>=1）。
            backoff_base: 指数退避基数。
            page_size: 每页商品数（1~100）。
            pages: 多页抓取总页数（>=1；翻页会增加请求频率，有风控风险）。
            page_sleep: 翻页间隔秒数（限速）。
            session: 可注入的 requests.Session（便于测试）。
            sleep_func: 可注入的 sleep 实现（便于测试中免等待）。
        """
        self.cookies: str = str(cookies or "").strip()
        self.user_agent: str = user_agent or DEFAULT_USER_AGENT
        self.timeout: float = float(timeout)
        self.retries: int = max(1, int(retries))
        self.backoff_base: float = float(backoff_base)
        self.page_size: int = max(1, min(100, int(page_size)))
        self.pages: int = max(1, int(pages))
        self.page_sleep: float = float(page_sleep) if page_sleep is not None else PAGE_SLEEP
        self.session: requests.Session = session if session is not None else requests.Session()
        self._sleep: Callable[[float], None] = sleep_func or time.sleep
        #: v3.4 服务端价格筛选：价格上限（元），None 表示不过滤
        self._max_price: Optional[float] = None

        #: Cookie 字典，作为 token 的权威来源（服务端刷新后就地更新）
        self._cookie_dict: Dict[str, str] = parse_cookie_string(self.cookies)
        self._sync_session_cookies()

    # ------------------------------------------------------------------ #
    # Cookie / token 管理
    # ------------------------------------------------------------------ #
    def _sync_session_cookies(self) -> None:
        """把 Cookie 字典写入 session.cookies（失败不影响主流程）。"""
        for name, value in self._cookie_dict.items():
            try:
                self.session.cookies.set(name, value, domain=".goofish.com")
            except Exception:  # noqa: BLE001 - 注入的假 session 可能不支持
                logger.debug("[mtop] 写入 session cookie 失败：%s", name)

    def set_cookies(self, cookie_str: str) -> None:
        """轮换时替换 Cookie（v3.2 多 Cookie 池）。

        更新请求头、token 权威字典与会话 Cookie，保证下一次请求
        使用新账号的登录态；服务端后续刷新的 `_m_h5_tk` 仍会被吸收。

        Args:
            cookie_str: 新的 Cookie 请求头字符串（可为空串）。
        """
        self.cookies = str(cookie_str or "").strip()
        self._cookie_dict = parse_cookie_string(self.cookies)
        self._sync_session_cookies()
        logger.debug("[mtop] 已切换 Cookie（长度 %d）", len(self.cookies))

    def current_token(self) -> str:
        """返回当前用于签名的 token（`_m_h5_tk` 下划线前半段）。"""
        return extract_token(self._cookie_dict.get(MTOP_TOKEN_COOKIE, ""))

    def set_max_price(self, max_price: Optional[float]) -> None:
        """设置服务端价格筛选上限（v3.4）。

        下次 `_search` 构造请求体时会把阈值写入
        `propValueStr.searchFilter="priceRange:0,{max_price};"` 并置
        `fromFilter=true`，使接口返回「最新发布且价格<max_price」的
        商品，与闲鱼网页行为一致。

        Args:
            max_price: 价格上限（元）；None 表示不过滤价格。
        """
        try:
            self._max_price = float(max_price) if max_price is not None else None
        except (TypeError, ValueError):  # pragma: no cover - 防御脏数据
            self._max_price = None
        logger.debug("[mtop] 服务端价格上限已更新：%s", self._max_price)

    def _absorb_token(self, response: Any) -> str:
        """从响应的 Set-Cookie 中吸收新的 `_m_h5_tk`。

        Args:
            response: requests 响应对象（或结构兼容的测试替身）。

        Returns:
            吸收到的新 Cookie 值；没有则返回空串。
        """
        new_value = ""
        # 1) 优先走 requests 的 cookie jar
        try:
            jar = getattr(response, "cookies", None)
            if jar is not None:
                new_value = str(jar.get(MTOP_TOKEN_COOKIE) or "")
        except Exception:  # noqa: BLE001 - jar 实现各异，失败即降级
            new_value = ""
        # 2) 降级：手工解析 Set-Cookie 响应头
        if not new_value:
            try:
                headers = getattr(response, "headers", None) or {}
                raw = str(headers.get("Set-Cookie") or headers.get("set-cookie") or "")
                match = re.search(r"_m_h5_tk=([^;,\s]+)", raw)
                if match:
                    new_value = match.group(1)
            except Exception:  # noqa: BLE001
                new_value = ""

        if new_value and new_value != self._cookie_dict.get(MTOP_TOKEN_COOKIE):
            self._cookie_dict[MTOP_TOKEN_COOKIE] = new_value
            try:
                self.session.cookies.set(MTOP_TOKEN_COOKIE, new_value, domain=".goofish.com")
            except Exception:  # noqa: BLE001
                pass
            logger.debug("[mtop] 已吸收服务端刷新的 %s", MTOP_TOKEN_COOKIE)
        return new_value

    def _check_cookies(self) -> None:
        """校验 Cookie 是否具备发起 mtop 请求的条件。

        Raises:
            FetchError: Cookie 为空，或不含 `_m_h5_tk`。
        """
        if not self.cookies and not self._cookie_dict:
            raise FetchError(COOKIE_GUIDE)
        if not self._cookie_dict.get(MTOP_TOKEN_COOKIE):
            raise FetchError(
                f"Cookie 中缺少 {MTOP_TOKEN_COOKIE}，无法计算 mtop 签名，说明 Cookie 已失效或不完整。"
                "请重新运行 `python -m xianyu_alert.cli login` 获取。"
            )

    def check_cookie_health(self) -> tuple[bool, str]:
        """检查 Cookie 是否过期 / 临期（不阻断请求）。

        解析 `_m_h5_tk` 内嵌的 13 位毫秒时间戳（24 小时有效）：
        过期或临期时返回 (False, 提示文案)，但**不拦截请求**——
        具体请求是否成功由服务端决定，这里只负责给出可操作的提示。

        Returns:
            (是否健康, 原因文案)。
        """
        from .cookie import cookie_expiry_status

        status = cookie_expiry_status(self.cookies)
        if status == "expired":
            return False, "Cookie 已过期（_m_h5_tk 时间戳超过 24 小时），请重新登录获取新 Cookie"
        if status == "expiring":
            return False, "Cookie 即将过期（剩余不足 1 小时），建议尽快重新登录"
        if status == "missing":
            return False, "未配置登录 Cookie，无法发起 mtop 请求"
        if status == "no_token":
            return False, f"Cookie 中缺少 {MTOP_TOKEN_COOKIE}，无法计算 mtop 签名"
        if status == "unknown":
            return True, "Cookie 未包含可解析的 _m_h5_tk 时间戳，无法判断是否过期"
        return True, "Cookie 状态正常"

    def check_item_status(self, product_id: str, timeout: Optional[float] = None) -> Optional[bool]:
        """校验单个商品是否仍在架（v3.7 需求 3，方案 B 详情接口判定）。

        走闲鱼商品详情接口 `mtop.taobao.idle.pc.detail`（**实测可用**）：
        - 返回 True 表示在架（itemDO.itemStatusStr="在线" / itemStatus=0）；
        - 返回 False 表示已售出 / 已下架（itemStatusStr 变为其它文案）；
        - 返回 None 表示无法判定（请求失败 / 风控 / 响应结构缺失），
          调用方应跳过该商品而不是误判为售出。

        注意：这是**单次请求**。批量校验请由调用方控制节奏
        （GUI「校验在架」按固定间隔限速调用），避免触发风控。

        Args:
            product_id: 商品 ID（数字串）。
            timeout: 覆盖默认超时秒数；None 使用构造时的 self.timeout。

        Returns:
            True = 在架；False = 已售出/下架；None = 无法判定。
        """
        pid = str(product_id or "").strip()
        if not pid:
            return None
        try:
            self._check_cookies()
        except FetchError:
            return None
        try:
            result = self._post_once(
                build_detail_payload(pid),
                api_name=MTOP_DETAIL_API_NAME,
                api_url=MTOP_DETAIL_URL,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 详情校验是尽力而为，任何失败都按「无法判定」处理
            logger.warning("[mtop] 校验商品 %s 详情失败：%s", pid, exc)
            return None
        ret_text = _ret_text(result)
        if "SUCCESS" not in ret_text.upper():
            logger.warning("[mtop] 校验商品 %s 详情接口返回异常：%s", pid, ret_text)
            return None
        return parse_detail_sold_status(result.get("data"))

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        """构造 mtop 请求头。"""
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": BASE_URL + "/",
            "Origin": BASE_URL,
            "Connection": "keep-alive",
        }

    def _build_params(self, timestamp: str, sign: str, api_name: str = MTOP_API_NAME) -> Dict[str, str]:
        """构造 mtop query 参数。

        Args:
            timestamp: 13 位毫秒时间戳。
            sign: mtop 签名。
            api_name: 目标接口名（默认搜索接口；v3.7 详情校验传详情接口名）。
        """
        return {
            "jsv": "2.7.2",
            "appKey": MTOP_APP_KEY,
            "t": timestamp,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api_name,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.search.0.0",
        }

    def _post_once(
        self,
        payload: Dict[str, Any],
        api_name: str = MTOP_API_NAME,
        api_url: str = MTOP_URL,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """用当前 token 签名并发起一次请求（含网络层重试）。

        Args:
            payload: 请求体（搜索为搜索体；v3.7 详情校验为详情体）。
            api_name: 目标接口名（默认搜索接口）。
            api_url: 目标接口 URL（默认搜索接口）。
            timeout: 覆盖默认超时秒数；None 使用构造时的 self.timeout。

        Returns:
            解析后的响应 JSON 字典。

        Raises:
            FetchError: 网络重试耗尽，或响应不是合法 JSON。
        """
        token = self.current_token()
        timestamp = str(int(time.time() * 1000))
        data = json.dumps(payload, separators=(",", ":"))
        sign = mtop_sign(token, timestamp, MTOP_APP_KEY, data)

        params = self._build_params(timestamp, sign, api_name=api_name)
        body = {"data": data}
        request_timeout = self.timeout if timeout is None else float(timeout)

        last_error: Optional[BaseException] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.post(
                    api_url,
                    params=params,
                    data=body,
                    headers=self._headers(),
                    timeout=request_timeout,
                )
                status = getattr(response, "status_code", 200)
                if status != 200:
                    raise FetchError(f"mtop 接口返回 HTTP {status}")
                # 无论成功失败都吸收服务端下发的新 token
                self._absorb_token(response)
                try:
                    result = response.json()
                except Exception as exc:  # noqa: BLE001 - 非 JSON 响应
                    raise FetchError(f"mtop 响应不是合法 JSON：{exc}") from exc
                if not isinstance(result, dict):
                    raise FetchError(f"mtop 响应结构异常（期望 dict，实际 {type(result).__name__}）")
                return result
            except Exception as exc:  # noqa: BLE001 - 统一转换为 FetchError
                last_error = exc
                if attempt < self.retries:
                    delay = self.backoff_base ** attempt
                    logger.warning(
                        "[mtop] 请求失败（第 %d/%d 次）：%s，%.1fs 后重试",
                        attempt, self.retries, exc, delay,
                    )
                    self._sleep(delay)
        raise FetchError(f"请求闲鱼 mtop 接口失败，已重试 {self.retries} 次：{last_error}")

    def _search(self, keyword: str, page_number: int = 1) -> Dict[str, Any]:
        """执行一次搜索，并处理 mtop 的业务返回码。

        令牌过期时会用服务端新下发的 `_m_h5_tk` 重算签名**自动重试一次**。

        Args:
            keyword: 搜索关键词。
            page_number: 页码（从 1 开始）。

        Returns:
            成功的响应 JSON。

        Raises:
            FetchError: 风控 / 登录态失效 / 重试后仍失败。
        """
        payload = build_search_payload(
            keyword,
            page_number=page_number,
            rows_per_page=self.page_size,
            max_price=self._max_price,
        )

        for round_index in range(2):  # 最多 2 次：首发 + 令牌过期后重试 1 次
            result = self._post_once(payload)
            ret_text = _ret_text(result)

            if "SUCCESS" in ret_text.upper():
                return result

            if _contains_any(ret_text, _RET_TOKEN_MARKERS):
                if round_index == 0:
                    logger.warning("[mtop] 令牌过期（%s），已用新 token 重算签名重试", ret_text)
                    continue
                raise FetchError(f"mtop 令牌反复过期，请重新登录获取 Cookie：{ret_text}")

            if _contains_any(ret_text, _RET_RISK_MARKERS):
                raise FetchError(
                    f"触发闲鱼风控（{ret_text}）。请调大 monitor.interval_seconds"
                    "（建议 300 秒以上）或稍后重试。"
                )

            if _contains_any(ret_text, _RET_SESSION_MARKERS):
                raise FetchError(
                    f"闲鱼登录态失效（{ret_text}）。"
                    "请重新运行 `python -m xianyu_alert.cli login` 获取 Cookie。"
                )

            raise FetchError(f"mtop 接口返回异常：{ret_text or '(空 ret)'}")

        # 理论上不可达（循环内必然 return 或 raise）
        raise FetchError("mtop 搜索失败：未获得有效响应")  # pragma: no cover

    # ------------------------------------------------------------------ #
    def fetch(self, keyword: str) -> List[Product]:
        """按关键词抓取最新发布的商品（支持多页 + 页级容错）。

        - 默认 `pages=1` 与旧行为完全一致（单页请求）；
        - `pages>1` 时循环抓取第 1..N 页，页间 `sleep(page_sleep)` 限速；
        - 单页失败只 warning，**不丢整轮**；全部页失败才抛 FetchError；
        - 跨页按 product_id 去重合并。

        Args:
            keyword: 搜索关键词。

        Returns:
            Product 列表；接口正常但无结果时返回空列表。

        Raises:
            FetchError: Cookie 缺失 / 风控 / 登录失效 / 全部页失败。
        """
        self._check_cookies()
        ok, reason = self.check_cookie_health()
        if not ok:
            logger.warning("[mtop] %s", reason)

        logger.info(
            "[mtop] 抓取关键词「%s」（最新发布，每页 %d 条，共 %d 页，页间隔 %.1fs）",
            keyword, self.page_size, self.pages, self.page_sleep,
        )

        all_products: List[Product] = []
        seen: set = set()
        failed_pages = 0
        last_error: Optional[BaseException] = None

        for page in range(1, self.pages + 1):
            try:
                result = self._search(keyword, page_number=page)
                data = result.get("data")
                data = data if isinstance(data, dict) else {}
                result_list = data.get("resultList")
                page_products = parse_mtop_result_list(result_list, keyword)
                new_items = [p for p in page_products if p.product_id not in seen]
                for product in new_items:
                    seen.add(product.product_id)
                    all_products.append(product)
                logger.info(
                    "[mtop] 第 %d/%d 页解析到 %d 个商品（去重后累计 %d）",
                    page, self.pages, len(page_products), len(all_products),
                )
            except FetchError as exc:
                # 页级容错：单页失败只记录 warning，继续抓取其余页
                failed_pages += 1
                last_error = exc
                logger.warning(
                    "[mtop] 第 %d/%d 页抓取失败：%s（已跳过该页，继续抓取其余页）",
                    page, self.pages, exc,
                )
            if page < self.pages:
                self._sleep(self.page_sleep)

        if failed_pages == self.pages:
            detail = f"（最后错误：{last_error}）" if last_error is not None else ""
            raise FetchError(f"[mtop] 全部 {self.pages} 页均抓取失败，放弃本轮抓取{detail}")

        if not all_products:
            logger.warning(
                "[mtop] 关键词「%s」未解析到任何商品（%d 页）。"
                "可能是该关键词确实无新品，或返回结构已变化。",
                keyword,
                self.pages,
            )
        else:
            logger.info("[mtop] 关键词「%s」解析到 %d 个商品（共 %d 页）", keyword, len(all_products), self.pages)
        return all_products

    def close(self) -> None:
        """关闭内部 session。"""
        try:
            self.session.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
            pass


# ---------------------------------------------------------------------- #
# 旧版 HTML 抓取器（对闲鱼实测无效，保留作通用示例）
# ⚠️ v3.2 起已标记「废弃（legacy）」：GUI 不再展示、config.example.yaml
#    不再推荐、默认值改为 mtop。**代码保留**仅用于向后兼容既有 config.yaml
#    中显式配置了 `fetcher.type: web` 的场景；新配置请改用 mtop / mock。
# ---------------------------------------------------------------------- #
class WebFetcher(Fetcher):
    """抓取闲鱼（goofish.com）网页搜索结果。

    Attributes:
        user_agent: 请求 UA。
        cookies: 原始 Cookie 字符串（形如 `k1=v1; k2=v2`），用于携带登录态。
        timeout: 单次请求超时（秒）。
        retries: 失败重试次数（指数退避）。
    """

    name = "web"

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        cookies: str = "",
        timeout: float = 10.0,
        retries: int = 3,
        backoff_base: float = 1.5,
        session: Optional[requests.Session] = None,
    ) -> None:
        """初始化 Web 抓取器。

        Args:
            user_agent: 浏览器 UA。
            cookies: Cookie 字符串，可为空。
            timeout: 请求超时秒数。
            retries: 总尝试次数（>=1）。
            backoff_base: 指数退避基数，第 n 次失败后 sleep backoff_base**n 秒。
            session: 可注入的 requests.Session（便于测试）。
        """
        self.user_agent: str = user_agent or DEFAULT_USER_AGENT
        self.cookies: str = cookies or ""
        self.timeout: float = float(timeout)
        self.retries: int = max(1, int(retries))
        self.backoff_base: float = float(backoff_base)
        self.session: requests.Session = session or requests.Session()

    def set_cookies(self, cookie_str: str) -> None:
        """轮换时替换 Cookie（v3.2 多 Cookie 池；WebFetcher 已废弃仍保持兼容）。"""
        self.cookies = str(cookie_str or "")

    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        """构造请求头。"""
        headers: Dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": BASE_URL + "/",
            "Connection": "keep-alive",
        }
        if self.cookies:
            headers["Cookie"] = self.cookies
        return headers

    def _request(self, url: str) -> str:
        """带重试的 GET 请求。

        Args:
            url: 目标 URL。

        Returns:
            响应文本。

        Raises:
            FetchError: 重试耗尽仍失败。
        """
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, headers=self._headers(), timeout=self.timeout)
                if response.status_code != 200:
                    raise FetchError(f"HTTP {response.status_code}")
                return response.text
            except Exception as exc:  # noqa: BLE001 - 统一转换为 FetchError
                last_error = exc
                if attempt < self.retries:
                    delay = self.backoff_base ** attempt
                    logger.warning(
                        "[web] 请求失败（第 %d/%d 次）：%s，%.1fs 后重试",
                        attempt, self.retries, exc, delay,
                    )
                    time.sleep(delay)
        raise FetchError(f"请求 {url} 失败，已重试 {self.retries} 次：{last_error}")

    # ------------------------------------------------------------------ #
    def fetch(self, keyword: str) -> List[Product]:
        """抓取指定关键词的商品列表。

        Args:
            keyword: 搜索关键词。

        Returns:
            Product 列表；页面能取到但解析不出商品时返回空列表。

        Raises:
            FetchError: 网络请求失败。
        """
        url = SEARCH_URL_TEMPLATE.format(keyword=quote_plus(keyword))
        logger.info("[web] 抓取关键词 %s -> %s", keyword, url)
        html = self._request(url)

        products = self.parse(html, keyword)
        if not products:
            logger.warning(
                "[web] 关键词「%s」未解析到任何商品。"
                "闲鱼为 JS 渲染 + 强反爬站点，纯 HTTP 抓取常常拿不到数据，"
                "请检查 monitor.cookies 是否配置了有效登录态，或改用 fetcher.type=mock 演示。",
                keyword,
            )
        return products

    # ------------------------------------------------------------------ #
    def parse(self, html: str, keyword: str) -> List[Product]:
        """解析搜索结果页 HTML。

        解析策略（两级兜底）：
            1. 优先从内联脚本里的 JSON（`window.__INIT_DATA__` 之类）提取商品；
            2. 回退到 BeautifulSoup 遍历带商品链接的 <a> 卡片。

        Args:
            html: 页面 HTML。
            keyword: 当前关键词（写入 Product.keyword）。

        Returns:
            去重后的 Product 列表。
        """
        if not html:
            return []

        products: List[Product] = self._parse_from_inline_json(html, keyword)
        if products:
            return products
        return self._parse_from_dom(html, keyword)

    def _parse_from_inline_json(self, html: str, keyword: str) -> List[Product]:
        """尝试从内联 JSON 中提取商品（闲鱼常把首屏数据塞进 script）。"""
        results: List[Product] = []
        seen: set = set()

        for match in re.finditer(
            r"window\.__(?:INIT_DATA|NEXT_DATA|PAGE_DATA)__\s*=\s*(\{.*?\})\s*[;<]",
            html,
            re.DOTALL,
        ):
            raw = match.group(1)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for item in self._walk_json_items(data):
                product = self._product_from_json_item(item, keyword)
                if product is not None and product.product_id not in seen:
                    seen.add(product.product_id)
                    results.append(product)
        return results

    @staticmethod
    def _walk_json_items(node: Any) -> Iterable[Dict[str, Any]]:
        """深度遍历 JSON，产出「看起来像商品」的 dict 节点。"""
        stack: List[Any] = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                keys = set(current.keys())
                if keys & {"itemId", "id"} and keys & {"title", "name", "content"}:
                    yield current
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)

    def _product_from_json_item(self, item: Dict[str, Any], keyword: str) -> Optional[Product]:
        """把 JSON 节点转换为 Product，字段缺失时返回 None。"""
        product_id = str(item.get("itemId") or item.get("id") or "").strip()
        if not product_id.isdigit():
            return None
        title = str(item.get("title") or item.get("name") or item.get("content") or "").strip()
        if not title:
            return None
        price = parse_price(str(item.get("price") or item.get("soldPrice") or ""))
        if price is None:
            return None
        publish_time = str(
            item.get("publishTime") or item.get("pubTime") or item.get("time") or ""
        ).strip()
        url = str(item.get("url") or "").strip() or ITEM_URL_TEMPLATE.format(product_id=product_id)
        try:
            return Product(
                product_id=product_id,
                title=title,
                price=price,
                url=url,
                publish_time=publish_time,
                keyword=keyword,
            )
        except ValueError:
            return None

    def _parse_from_dom(self, html: str, keyword: str) -> List[Product]:
        """回退方案：用 BeautifulSoup 遍历商品链接卡片。"""
        soup = BeautifulSoup(html, "html.parser")
        results: List[Product] = []
        seen: set = set()

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            product_id = extract_product_id(href)
            if not product_id or product_id in seen:
                continue

            # 卡片文本：优先取 <a> 自身，再向上找一层容器补充信息
            card_text = anchor.get_text(" ", strip=True)
            container = anchor.parent
            container_text = container.get_text(" ", strip=True) if container is not None else ""
            full_text = card_text or container_text

            title = self._extract_title(anchor) or full_text
            price = parse_price(self._extract_price_text(anchor, container) or full_text)
            if not title or price is None:
                continue

            seen.add(product_id)
            url = href if href.startswith("http") else urljoin(BASE_URL, href)
            try:
                results.append(
                    Product(
                        product_id=product_id,
                        title=title[:200],
                        price=price,
                        url=url,
                        publish_time=parse_publish_time(container_text or full_text),
                        keyword=keyword,
                    )
                )
            except ValueError as exc:
                logger.debug("[web] 跳过非法商品卡片 %s：%s", product_id, exc)
        return results

    @staticmethod
    def _extract_title(anchor: Any) -> str:
        """从卡片中提取标题（优先带 title 语义的节点）。"""
        for attr in ("title", "aria-label"):
            value = anchor.get(attr)
            if value:
                return str(value).strip()
        node = anchor.find(attrs={"class": re.compile(r"title|name|desc", re.I)})
        if node is not None:
            text = node.get_text(" ", strip=True)
            if text:
                return text
        return ""

    @staticmethod
    def _extract_price_text(anchor: Any, container: Any) -> str:
        """从卡片中提取价格文本。"""
        for scope in (anchor, container):
            if scope is None:
                continue
            node = scope.find(attrs={"class": re.compile(r"price", re.I)})
            if node is not None:
                text = node.get_text(" ", strip=True)
                if text:
                    return text
        return ""


# ---------------------------------------------------------------------- #
# Mock 抓取器
# ---------------------------------------------------------------------- #
class MockFetcher(Fetcher):
    """确定性伪造抓取器，用于单元测试与离线演示。

    生成规则（保证可复现）：
        - 每个关键词维护独立的轮次计数 round_no（从 1 开始）；
        - 第 r 轮返回商品索引窗口 [r-1, r-1+products_per_round)，
          因此相邻两轮会有 products_per_round-1 个重叠商品，
          可用于验证「上一轮已出现的商品不算新」；
        - 商品索引 i 为 3 的倍数时生成「低价商品」（10~199 元），
          否则生成「高价商品」（2000~5000 元），
          从而稳定产出可触发阈值与不可触发阈值两类样本；
        - 所有随机数用 `关键词#索引` 作种子，结果完全确定。

    Attributes:
        products_per_round: 每轮返回的商品数量。
        fail_rounds: 需要抛出 FetchError 的轮次编号集合（从 1 开始）。
    """

    name = "mock"

    #: 低价商品价格区间
    CHEAP_RANGE = (10.0, 199.0)
    #: 高价商品价格区间
    EXPENSIVE_RANGE = (2000.0, 5000.0)

    def __init__(
        self,
        products_per_round: int = 5,
        fail_rounds: Optional[Sequence[int]] = None,
        round_provider: Optional[Any] = None,
    ) -> None:
        """初始化 Mock 抓取器。

        Args:
            products_per_round: 每轮生成的商品数量（>=1）。
            fail_rounds: 抛出 FetchError 的轮次列表，例如 [2] 表示第 2 轮失败。
            round_provider: 可选 callable，签名 `(keyword: str) -> int`，
                用于外部注入轮次；不传则内部自增计数。
        """
        self.products_per_round: int = max(1, int(products_per_round))
        self.fail_rounds: set = {int(x) for x in (fail_rounds or [])}
        self.round_provider = round_provider
        self._rounds: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    def current_round(self, keyword: str) -> int:
        """返回某关键词当前已进行的轮次（尚未 fetch 时为 0）。"""
        return self._rounds.get(keyword, 0)

    def reset(self) -> None:
        """重置所有关键词的轮次计数。"""
        self._rounds.clear()

    def _next_round(self, keyword: str) -> int:
        """推进并返回下一轮次编号。"""
        if self.round_provider is not None:
            return int(self.round_provider(keyword))
        self._rounds[keyword] = self._rounds.get(keyword, 0) + 1
        return self._rounds[keyword]

    # ------------------------------------------------------------------ #
    def make_product(self, keyword: str, index: int) -> Product:
        """根据 (关键词, 索引) 确定性地生成一个商品。

        Args:
            keyword: 关键词。
            index: 全局商品索引（>=0）。

        Returns:
            确定性生成的 Product。
        """
        rnd = random.Random(f"{keyword}#{index}")
        is_cheap = index % 3 == 0
        low, high = self.CHEAP_RANGE if is_cheap else self.EXPENSIVE_RANGE
        price = round(rnd.uniform(low, high), 2)

        product_id = str(1000000 + rnd.randrange(0, 899999) + index)
        suffix = "捡漏特价" if is_cheap else "个人闲置"
        title = f"{keyword} {suffix} 第{index}号 九成新"
        publish_time = (datetime(2024, 1, 1, 12, 0, 0) + timedelta(minutes=index * 7)).strftime(
            "%Y-%m-%d %H:%M"
        )
        return Product(
            product_id=product_id,
            title=title,
            price=price,
            url=ITEM_URL_TEMPLATE.format(product_id=product_id),
            publish_time=publish_time,
            keyword=keyword,
        )

    def fetch(self, keyword: str) -> List[Product]:
        """生成本轮的伪造商品列表。

        Args:
            keyword: 搜索关键词。

        Returns:
            本轮商品列表（按索引升序）。

        Raises:
            FetchError: 当前轮次在 fail_rounds 中，用于模拟抓取失败。
        """
        round_no = self._next_round(keyword)
        if round_no in self.fail_rounds:
            raise FetchError(f"[mock] 模拟第 {round_no} 轮抓取失败（关键词：{keyword}）")

        start = round_no - 1
        products = [
            self.make_product(keyword, index)
            for index in range(start, start + self.products_per_round)
        ]
        logger.info("[mock] 关键词「%s」第 %d 轮生成 %d 个商品", keyword, round_no, len(products))
        return products


# ---------------------------------------------------------------------- #
# 工厂
# ---------------------------------------------------------------------- #
def build_fetcher(config: Config) -> Fetcher:
    """根据配置构建抓取器。

    多 Cookie 池（v3.2）：mtop 分支初始 Cookie 按「池优先、单值兜底」
    策略解析——池中启用条目非空取第 0 条，否则回退 `monitor.cookies`。
    轮换由 monitor 每轮调用 `fetcher.set_cookies()` 完成，这里只负责首轮。

    Args:
        config: 全局配置。

    Returns:
        Fetcher 实例：
            fetcher.type == "mtop" -> MtopFetcher（真实抓取，推荐）
            fetcher.type == "mock" -> MockFetcher（离线演示）
            其它（"web"）          -> WebFetcher（旧版 HTML 解析，已废弃）
    """
    ftype = config.fetcher.type
    if ftype == "mock":
        return MockFetcher(
            products_per_round=config.fetcher.mock_products_per_round,
            fail_rounds=config.fetcher.mock_fail_rounds,
        )
    if ftype == "mtop":
        from .cookie import resolve_cookie_for_round

        return MtopFetcher(
            cookies=resolve_cookie_for_round(config.monitor, 0),
            user_agent=config.monitor.user_agent,
            page_size=config.fetcher.page_size,
            pages=config.fetcher.pages,
            page_sleep=config.fetcher.page_sleep,
        )
    return WebFetcher(
        user_agent=config.monitor.user_agent,
        cookies=config.monitor.cookies,
    )
