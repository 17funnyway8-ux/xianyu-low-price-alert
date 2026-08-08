# -*- coding: utf-8 -*-
"""v3.4 探测：dump 响应的 filterBar / resultInfo / tabList / appBar，找网页筛选线索。"""
from __future__ import annotations

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from xianyu_alert.config import load_config  # noqa: E402
from xianyu_alert.cookie import resolve_cookie_for_round  # noqa: E402
from xianyu_alert.fetcher import MtopFetcher, build_search_payload  # noqa: E402


def main() -> int:
    cfg = load_config(os.path.join(PROJECT_ROOT, "dist", "config.yaml"))
    cookie = resolve_cookie_for_round(cfg.monitor, 0)
    fetcher = MtopFetcher(cookies=cookie, page_size=30)
    keyword = "DDR4 3200 16G"
    try:
        payload = build_search_payload(keyword, 1, 30)
        resp = fetcher._post_once(payload)
        ret = resp.get("ret") or []
        ret_text = " ".join(str(x) for x in ret) if isinstance(ret, (list, tuple)) else str(ret or "")
        if "TOKEN" in ret_text.upper() or "令牌" in ret_text:
            resp = fetcher._post_once(payload)
        print(f"[ret] {resp.get('ret')}")
        data = resp.get("data")
        if not isinstance(data, dict):
            print("data 非 dict")
            return 1
        for section in ("filterBar", "resultInfo", "tabList", "appBar", "resultPrefixBar", "topList"):
            value = data.get(section)
            print(f"\n########## {section} ##########")
            print(json.dumps(value, ensure_ascii=False, indent=1)[:6000])
    finally:
        fetcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
