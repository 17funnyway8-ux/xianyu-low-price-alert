# -*- coding: utf-8 -*-
"""v3.4 探测：propValueStr.searchFilter="priceRange:0,360;" + fromFilter=true 组合。"""
from __future__ import annotations

import json
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from xianyu_alert.config import load_config  # noqa: E402
from xianyu_alert.cookie import resolve_cookie_for_round  # noqa: E402
from xianyu_alert.fetcher import MtopFetcher, build_search_payload, parse_mtop_result_list  # noqa: E402

COMBO_LIST = [
    {
        "name": "P1 priceRange 0,360 + fromFilter=true (sort=create desc)",
        "overrides": {
            "propValueStr": {"searchFilter": "priceRange:0,360;"},
            "fromFilter": True,
        },
    },
    {
        "name": "P2 priceRange 0,360 + fromFilter=true + searchReqFromPage=pcSearch",
        "overrides": {
            "propValueStr": {"searchFilter": "priceRange:0,360;"},
            "fromFilter": True,
        },
    },
    {
        "name": "P3 priceRange 0,99999999 + fromFilter=true (all prices)",
        "overrides": {
            "propValueStr": {"searchFilter": "priceRange:0,99999999;"},
            "fromFilter": True,
        },
    },
]


def probe_once(fetcher: Any, payload: dict) -> dict:
    result = fetcher._post_once(payload)
    ret = result.get("ret") or []
    ret_text = " ".join(str(x) for x in ret) if isinstance(ret, (list, tuple)) else str(ret or "")
    if "TOKEN" in ret_text.upper() or "令牌" in ret_text:
        result = fetcher._post_once(payload)
    return result


def main() -> int:
    cfg = load_config(os.path.join(PROJECT_ROOT, "dist", "config.yaml"))
    cookie = resolve_cookie_for_round(cfg.monitor, 0)
    fetcher = MtopFetcher(cookies=cookie, page_size=30)
    keyword = "DDR4 3200 16G"
    try:
        for i, combo in enumerate(COMBO_LIST):
            if i > 0:
                print("[probe] 等待 8s…")
                time.sleep(8)
            payload = build_search_payload(keyword, 1, 30)
            for k, v in combo["overrides"].items():
                payload[k] = v
            print(f"\n===== {combo['name']} =====")
            print(f"[payload] {json.dumps(payload, ensure_ascii=False)}")
            try:
                resp = probe_once(fetcher, payload)
            except Exception as exc:  # noqa: BLE001
                print(f"!! 失败: {exc}")
                continue
            print(f"[ret] {resp.get('ret')}")
            data = resp.get("data")
            if not isinstance(data, dict):
                print(f"[data] 非 dict: {str(data)[:200]}")
                continue
            rl = data.get("resultList") or []
            products = parse_mtop_result_list(rl, keyword)
            cheap = sum(1 for p in products if p.price < 360)
            total = data.get("resultInfo", {}).get("searchResControlFields", {}).get("numFound")
            print(f"[resultList] {len(rl)} | 解析 {len(products)} | <360: {cheap} | numFound={total}")
            for p in products[:5]:
                print(f"  ¥{p.price:<8} {p.publish_time}  {p.title[:44]}")
    finally:
        fetcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
