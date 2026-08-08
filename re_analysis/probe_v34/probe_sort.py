# -*- coding: utf-8 -*-
"""v3.4 实机探测：找出与闲鱼网页「最新发布」一致的 mtop 排序参数组合。

用法（项目根目录执行）：
    python re_analysis/probe_v34/probe_sort.py [--config dist/config.yaml] [--keyword "DDR4 3200 16G"]

安全策略：
    - 每次请求间隔 >= 8 秒（保守）；
    - 最多探测 N 种组合（默认 7 种）；
    - 触发风控(RGV587)后等待 10 秒再继续；
    - 全程打印每条结果的前 3 条（标题 / 价格 / 发布时间）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

# 保证能 import 项目包
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from xianyu_alert.config import load_config  # noqa: E402
from xianyu_alert.cookie import resolve_cookie_for_round  # noqa: E402
from xianyu_alert.fetcher import (  # noqa: E402
    MTOP_URL,
    build_search_payload,
    parse_mtop_result_list,
)

# 探测的候选参数组合（在默认 payload 基础上做增量修改）
PROBE_COMBOS: List[Dict[str, Any]] = [
    # C0：当前 v3.3 组合（基线）
    {"name": "C0 baseline sortField=create+sortValue=desc", "overrides": {}},
    # C1：只设 sortField=create，不设 sortValue
    {"name": "C1 sortField=create only", "overrides": {"sortValue": ""}},
    # C2：加 searchType / sortType
    {
        "name": "C2 +searchType=1 +sortType=1",
        "overrides": {"searchType": "1", "sortType": "1"},
    },
    # C3：fromFilter=true
    {"name": "C3 fromFilter=true", "overrides": {"fromFilter": True}},
    # C4：加 searchChannel=pcSearch + from=search
    {
        "name": "C4 +searchChannel +from=search",
        "overrides": {"searchChannel": "pcSearch", "from": "search"},
    },
    # C5：sortValue=desc + sortField=create + fromFilter=true + searchFrom
    {
        "name": "C5 sortField=create+sortValue=desc+fromFilter=true+searchFrom=search",
        "overrides": {"fromFilter": True, "searchFrom": "search"},
    },
    # C6：无 sortField/sortValue（默认综合排序，对照）
    {"name": "C6 default (no sort)", "overrides": {"sortField": "", "sortValue": ""}},
]


def resolve_cookie(config_path: str) -> str:
    """加载配置并返回第一个可用的明文 Cookie。"""
    cfg = load_config(config_path)
    cookie = resolve_cookie_for_round(cfg.monitor, 0)
    if not cookie:
        # 回退 monitor.cookies
        cookie = str(cfg.monitor.cookies or "")
    return cookie


def probe_once(fetcher: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """用 fetcher 内部逻辑发起一次请求，返回响应 JSON。

    复用 MtopFetcher._post_once 的真实请求路径（含 token 吸收），
    传入自定义 payload 以探测不同参数组合；令牌过期时用吸收后的
    新 token 重发一次（与 _search 同逻辑）。
    """
    result = fetcher._post_once(payload)
    ret = result.get("ret") or []
    ret_text = " ".join(str(x) for x in ret) if isinstance(ret, (list, tuple)) else str(ret or "")
    if "TOKEN" in ret_text.upper() or "令牌" in ret_text:
        result = fetcher._post_once(payload)
    return result


def summarize(result: Dict[str, Any], keyword: str) -> Dict[str, Any]:
    """解析响应并输出前 3 条信息 + 统计。"""
    ret = result.get("ret")
    data = result.get("data")
    if not isinstance(data, dict):
        return {"ret": ret, "items": [], "total": 0, "price_stats": {}}
    result_list = data.get("resultList")
    products = parse_mtop_result_list(result_list, keyword)
    prices = [p.price for p in products]
    cheap = [p for p in products if p.price < 360.0]
    total = data.get("totalCount") or data.get("total") or len(products)
    return {
        "ret": ret,
        "items": [
            {
                "title": p.title[:40],
                "price": p.price,
                "publish_time": p.publish_time,
            }
            for p in products[:3]
        ],
        "total": total,
        "count": len(products),
        "price_stats": {
            "min": min(prices) if prices else None,
            "max": max(prices) if prices else None,
            "lt_360": len(cheap),
            "all": prices,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.path.join(PROJECT_ROOT, "dist", "config.yaml"))
    parser.add_argument("--keyword", default="DDR4 3200 16G")
    parser.add_argument("--combo", default="all", help="all 或 C0..C6 逗号分隔")
    parser.add_argument("--max-requests", type=int, default=7)
    args = parser.parse_args()

    cookie = resolve_cookie(args.config)
    if not cookie:
        print("!! 未取到有效 Cookie，无法探测")
        return 2
    # 脱敏打印 cookie 前缀，确认加载成功
    from xianyu_alert.secure import mask_cookie
    print(f"[cookie] 已加载（脱敏 {mask_cookie(cookie)}，长度 {len(cookie)}）")
    if "_m_h5_tk=" not in cookie:
        print("!! Cookie 缺少 _m_h5_tk，探测结果不可信")
        return 2

    selected = args.combo.split(",") if args.combo != "all" else ["all"]
    combos = PROBE_COMBOS if selected == ["all"] else [c for c in PROBE_COMBOS if c["name"].split()[0] in selected]

    print(f"\n[probe] 关键词: {args.keyword} | 组合数: {len(combos)} | 间隔 >= 8s")
    from xianyu_alert.fetcher import MtopFetcher

    fetcher = MtopFetcher(cookies=cookie, page_size=30)
    results: List[Dict[str, Any]] = []
    try:
        for i, combo in enumerate(combos):
            if i > 0:
                wait = 8
                print(f"[probe] 等待 {wait}s 后发起下一组…")
                time.sleep(wait)
            payload = build_search_payload(args.keyword, page_number=1, rows_per_page=30)
            for key, value in combo["overrides"].items():
                if value == "":
                    payload.pop(key, None)
                else:
                    payload[key] = value
            print(f"\n===== {combo['name']} =====")
            print(f"[payload] {json.dumps(payload, ensure_ascii=False)}")
            try:
                resp = probe_once(fetcher, payload)
            except Exception as exc:  # noqa: BLE001
                print(f"!! 请求失败: {exc}")
                results.append({"name": combo["name"], "error": str(exc)})
                # 疑似风控：多等一会
                if "风控" in str(exc) or "RGV587" in str(exc):
                    print("[probe] 命中风控，等待 10s…")
                    time.sleep(10)
                continue
            summary = summarize(resp, args.keyword)
            summary["name"] = combo["name"]
            results.append(summary)
            print(f"[ret] {summary['ret']}")
            print(f"[total] {summary['total']} | 解析 {summary['count']} 条 | 价格<360: {summary['price_stats']['lt_360']}")
            print(f"[price] min={summary['price_stats']['min']} max={summary['price_stats']['max']}")
            for idx, item in enumerate(summary["items"], 1):
                print(f"  {idx}. ¥{item['price']:<8} {item['publish_time']}  {item['title']}")
    finally:
        fetcher.close()
    print("\n\n================= 汇总对比 =================")
    for r in results:
        if "error" in r:
            print(f"- {r['name']}: ERROR {r['error']}")
        else:
            lt = r["price_stats"]["lt_360"]
            times = [it["publish_time"] for it in r["items"]]
            print(
                f"- {r['name']}: total={r['total']} count={r['count']} "
                f"<360={lt}/{r['count']} 前3时间={times}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
