"""mtop 纯函数层：签名、令牌提取、请求体构造、响应解析（无 IO，最易测）。"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ..models import Product
from ..parsing import (  # noqa: F401 - 再导出：既有调用点与测试无需改动
    extract_product_id,
    parse_price,
    parse_publish_time,
)
from .constants import (
    ITEM_URL_TEMPLATE,
)

logger = logging.getLogger(__name__)

# 闲鱼 Web 站点
def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    """把 `k1=v1; k2=v2` 形式的 Cookie 请求头解析成字典。

    Args:
        cookie_str: Cookie 请求头字符串，可为空。

    Returns:
        {cookie 名: cookie 值} 字典；空输入返回空字典。
    """
    result: dict[str, str] = {}
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
    max_price: float | None = None,
) -> dict[str, Any]:
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


def build_detail_payload(product_id: str) -> dict[str, Any]:
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


def parse_detail_sold_status(data: Any) -> bool | None:
    """从商品详情接口响应的 data 节点解析「是否在线」（纯函数，v3.7）。

    实机探测结论（mtop.taobao.idle.pc.detail，2026-08 真实 Cookie）：
        - 在架：`itemDO.itemStatusStr = "在线"`、`itemDO.itemStatus = 0`；
        - 已售出/下架：itemStatusStr 会变为「已售出 / 已下架」等文案，
          itemStatus 为非 0 值；
        - 字段缺失 / 结构异常 → 返回 None（调用方按「无法判定」处理）。

    判定口径（v1.8.1 收紧，防误标）：
        「已售出/下架」只采信 `itemStatusStr` 的**文案白名单**；
        `itemStatus` 数值分支仅在 `== 0` 时判在架 —— 非 0 但无文案佐证时
        返回 None（无法判定），而不是 False。原因：`itemStatus` 是服务端枚举，
        可能新增「交易中 / 审核中」等值，一律判 False 会让调用方
        （校验在架 → mark_sold_out_by_id）把**仍在架的低价商品**标记为已售出
        并从提醒列表隐藏，属数据损坏。

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
        # 非 0 且上面 itemStatusStr 未命中「已售/下架」文案 → 证据不足。
        # 保守返回 None（无法判定），避免未知枚举值把在架商品误标为已售出。
    return None


def _ret_text(payload: Any) -> str:
    """把响应中的 ret 字段拼成一整段文本，便于关键字匹配。"""
    ret = payload.get("ret") if isinstance(payload, dict) else payload
    if isinstance(ret, (list, tuple)):
        return " ".join(str(x) for x in ret)
    return str(ret or "")


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    """判断文本中是否含有任一特征串（大小写不敏感）。"""
    upper = text.upper()
    return any(marker.upper() in upper for marker in markers)


def parse_mtop_item(item: Any, keyword: str) -> Product | None:
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
    # 主图：站点返回协议相对地址（//img.alicdn.com/...），归一化由 Product 统一处理
    image_url = coerce_text(ex_content.get("picUrl") or ex_content.get("pic_url") or "")
    # v1.10.3：卖家 / 地区 / 原价（mtop 的 exContent 里通常都有，此前直接丢掉了）
    seller = coerce_text(
        ex_content.get("userNickName") or ex_content.get("sellerNick") or args.get("userNickName") or ""
    )
    location = coerce_text(ex_content.get("area") or ex_content.get("city") or args.get("area") or "")
    original_price = parse_price(
        coerce_text(ex_content.get("originalPrice") or ex_content.get("oriPrice") or args.get("originalPrice"))
    )

    try:
        return Product(
            product_id=product_id,
            title=title[:200],
            price=price,
            url=url,
            publish_time=publish_time,
            keyword=keyword,
            image_url=image_url,
            seller=seller,
            location=location,
            original_price=original_price,
        )
    except ValueError as exc:
        logger.debug("[mtop] 跳过非法商品 %s：%s", product_id, exc)
        return None


def parse_mtop_result_list(result_list: Any, keyword: str) -> list[Product]:
    """批量解析 mtop 的 `data.resultList`。

    Args:
        result_list: 响应中的 resultList（期望是 list，其它类型按空处理）。
        keyword: 当前搜索关键词。

    Returns:
        解析成功的 Product 列表（按原顺序，已按 product_id 去重）。
    """
    if not isinstance(result_list, (list, tuple)):
        return []

    products: list[Product] = []
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

__all__ = [
    "parse_cookie_string",
    "mtop_sign",
    "extract_token",
    "coerce_text",
    "format_publish_time",
    "build_search_payload",
    "format_price_bound",
    "build_detail_payload",
    "parse_detail_sold_status",
    "parse_mtop_item",
    "parse_mtop_result_list",
]
