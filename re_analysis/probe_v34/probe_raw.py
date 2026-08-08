# -*- coding: utf-8 -*-
"""v3.4 探测：检查 mtop 搜索响应原始结构 + 尝试价格筛选参数。

重点：
    1. dump C0 响应的 data 顶层键（找 totalCount / filter 相关字段）；
    2. 探测 extraFilterValue / propValueStr 传价格区间是否生效；
    3. 探测 searchType / fromFilter / page 等网页「最新+价格」组合。
"""

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
from xianyu_alert.fetcher import MtopFetcher, build_search_payload  # noqa: E402

COMBO_LIST = [
    {
        "name": "R0 raw response structure",
        "overrides": {},
        "dump": True,
    },
    {
        "name": "R1 extraFilterValue price<360",
        "overrides": {"extraFilterValue": json.dumps({"price": {"max": 360}}, ensure_ascii=False)},
    },
    {
        "name": "R2 propValueStr price<360",
        "overrides": {"propValueStr": json.dumps({"priceRange": {"min": 0, "max": 360}}, ensure_ascii=False)},
    },
    {
        "name": "R3 extraFilterValue priceEnd=360",
        "overrides": {"extraFilterValue": json.dumps({"priceEnd": 360, "priceStart": 0}, ensure_ascii=False)},
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
            try:
                resp = probe_once(fetcher, payload)
            except Exception as exc:  # noqa: BLE001
                print(f"!! 失败: {exc}")
                continue
            print(f"[ret] {resp.get('ret')}")
            data = resp.get("data")
            if not isinstance(data, dict):
                print(f"[data] 非 dict: {type(data).__name__} = {str(data)[:300]}")
                continue
            if combo.get("dump"):
                print(f"[data top-level keys] {sorted(data.keys())}")
                # 打印可能是筛选信息 / 总数 / 分页的字段
                for key in ("totalCount", "total", "filter", "filterList", "searchType", "pageSize", "hasNext", "scrollId"):
                    if key in data:
                        print(f"  {key} = {json.dumps(data[key], ensure_ascii=False)[:400]}")
                rl = data.get("resultList")
                print(f"[resultList] 类型 {type(rl).__name__} 长度 {len(rl) if isinstance(rl, (list, tuple)) else 'N/A'}")
                if isinstance(rl, (list, tuple)) and rl:
                    first = rl[0]
                    print(f"[item0 top keys] {sorted(first.keys()) if isinstance(first, dict) else type(first).__name__}")
                    if isinstance(first, dict):
                        d = first.get("data")
                        if isinstance(d, dict):
                            print(f"[item0.data keys] {sorted(d.keys())}")
                            it = d.get("item")
                            if isinstance(it, dict):
                                print(f"[item0.data.item keys] {sorted(it.keys())}")
                                m = it.get("main")
                                if isinstance(m, dict):
                                    print(f"[item0.data.item.main keys] {sorted(m.keys())}")
                                    cp = m.get("clickParam")
                                    if isinstance(cp, dict):
                                        print(f"[clickParam keys] {sorted(cp.keys())}")
            else:
                # 普通输出：前 3 条 价格/时间
                rl = data.get("resultList") or []
                count = len(rl)
                cheap = 0
                from xianyu_alert.fetcher import parse_mtop_result_list
                products = parse_mtop_result_list(rl, keyword)
                cheap = sum(1 for p in products if p.price < 360)
                print(f"[count] {count} | 解析 {len(products)} | <360: {cheap}")
                for p in products[:3]:
                    print(f"  ¥{p.price:<8} {p.publish_time}  {p.title[:40]}")
    finally:
        fetcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
