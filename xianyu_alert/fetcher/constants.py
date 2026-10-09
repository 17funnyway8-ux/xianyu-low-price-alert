"""抓取层常量：站点地址、mtop 接口名与风控/令牌标记（v1.9.9 从 fetcher.py 拆出）。"""

from __future__ import annotations

import logging

from ..parsing import (  # noqa: F401 - 再导出：既有调用点与测试无需改动
    extract_product_id,
    parse_price,
    parse_publish_time,
)

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
#: 与 `_m_h5_tk` **成对下发**的配对值（服务端回验用）。
#: 2026-09-24 实测：令牌续期时服务端同时下发 `_m_h5_tk` 与 `_m_h5_tk_enc`
#: （均 `Max-Age=5400`）。两者必须同步更新，只换 `_tk` 会让下一次请求
#: 因配对值不匹配而签名校验失败。
MTOP_TOKEN_ENC_COOKIE = "_m_h5_tk_enc"

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
#: 翻页之间的固定限速（秒）：降低多页抓取触发风控的概率
PAGE_SLEEP = 2.0



__all__ = [
    "BASE_URL",
    "SEARCH_URL_TEMPLATE",
    "ITEM_URL_TEMPLATE",
    "MTOP_API_NAME",
    "MTOP_URL",
    "MTOP_DETAIL_API_NAME",
    "MTOP_DETAIL_URL",
    "MTOP_APP_KEY",
    "MTOP_TOKEN_COOKIE",
    "MTOP_TOKEN_ENC_COOKIE",
    "COOKIE_GUIDE",
    "PAGE_SLEEP",
]
