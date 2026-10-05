#!/usr/bin/env python3
# ===========================================================
# scripts/verify_web_deploy.py —— 已部署实例的交付验收（对真实容器跑）
#
# 与 e2e_web_smoke.py 的区别：
#   e2e_web_smoke   ：临时起一个 uvicorn 进程，验证「接口契约」正确；
#   verify_web_deploy：打真实的部署地址（默认 http://127.0.0.1:8080），
#                     验证「交付物」是否真的到位 —— 重点是用**字节哈希**
#                     逐个比对容器返回的静态资源与本地磁盘文件。
#
#   为什么必须要哈希比对：静态资源不进构建产物、没有版本号，光看 200
#   是发现不了「容器里还是上一版前端」的。哈希一致才算端到端落地。
#
# 用法：
#   python scripts/verify_web_deploy.py                       # 默认 :8080
#   python scripts/verify_web_deploy.py --base http://10.0.0.26:8080
#   python scripts/verify_web_deploy.py --token <XY_WEB_TOKEN>  # 开了认证时
# 退出码：0 = 全绿；1 = 有失败；2 = 连不上
# ===========================================================
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "web" / "static"

PASS, FAIL = [], []


def ck(ok: bool, name: str, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(("  \u2705 " if ok else "  \u274c ") + name + (("\t\t" + detail) if detail else ""))
    return ok


class Headers:
    """大小写不敏感的响应头访问器。

    坑：HTTP/1.1 下 uvicorn 发的是小写头名（content-type），而
    `dict(r.headers)` 会把 HTTPMessage 自带的大小写不敏感性丢掉，
    于是 hd.get("Content-Type") 恒为 None —— 会被误判成「没有 Content-Type」。
    """

    def __init__(self, h):
        self._h = {str(k).lower(): v for k, v in h.items()}

    def get(self, name: str, default: str = ""):
        return self._h.get(name.lower(), default)

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._h


def fetch(base: str, path: str, token: str = "", method: str = "GET", timeout: int = 10):
    """返回 (status, Headers, body_bytes)。HTTP 错误不抛，交给断言判断。"""
    req = urllib.request.Request(base.rstrip("/") + path, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, Headers(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, Headers(e.headers), e.read()
    except Exception as e:  # 连接层失败
        print(f"\n\u274c 无法连接 {base}{path}：{e}")
        sys.exit(2)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sse_probe(base: str, path: str, nbytes: int = 900, timeout: int = 6):
    """SSE 是无尽流，不能用 urlopen 整读（会挂到超时）。
    这里直接开 socket，拿到响应头 + 前 nbytes 字节就主动断开。"""
    import http.client
    from urllib.parse import urlparse

    u = urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Accept": "text/event-stream"})
        r = conn.getresponse()
        ct = r.getheader("Content-Type", "")
        # read() 在流式响应上会阻塞；用 read1 拿已经到达的字节
        chunk = r.read1(nbytes) if hasattr(r, "read1") else r.read(nbytes)
        return r.status, ct, chunk
    finally:
        conn.close()


# 新前端的结构特征：老版单页 tab（tab-config/tab-notify/tab-run）没有这些
NEW_MARKERS = [
    ('data-view="overview"', "四视图架构"),
    ('data-view="targets"', "监控配置视图"),
    ('data-view="hits"', "命中战果视图"),
    ('data-view="system"', "通知与系统视图"),
    ('id="cmdkBtn"', "命令面板入口"),
    ('id="dirtySaveBtn"', "未保存改动提示条"),
    ('id="hitsBadge"', "命中角标"),
]
# 老前端残留特征：出现即说明镜像里装的是旧前端
OLD_MARKERS = ['"tab-config"', "tab-notify", "tab-run", 'id="tabNav"']


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--token", default="")
    a = ap.parse_args()
    base = a.base

    print("=" * 72)
    print(f"闲鱼低价提醒工具 · 部署验收  →  {base}")
    print("=" * 72)

    # ---------- 1. 进程健康 ----------
    print("\n[1] 服务健康")
    st, _, body = fetch(base, "/healthz", a.token)
    ck(st == 200, "GET /healthz → 200", f"status={st}")
    try:
        hz = json.loads(body)
    except Exception:
        hz = {}
    ck(isinstance(hz, dict) and hz.get("status") == "ok", "健康体 status=ok", str(hz.get("status")))
    ver = hz.get("version", "")
    ck(bool(ver), "健康体带版本号", f"version={ver}")
    ck("data_dir" in hz, "健康体带 data_dir（数据卷已挂）", str(hz.get("data_dir")))
    ck("monitor_running" in hz, "健康体带 monitor_running", f"running={hz.get('monitor_running')}")

    # ---------- 2. 单页入口 ----------
    print("\n[2] 单页入口 GET /")
    st, hd, html = fetch(base, "/", a.token)
    ck(st == 200, "GET / → 200", f"status={st}")
    ck("text/html" in hd.get("Content-Type", ""), "Content-Type 是 text/html",
       hd.get("Content-Type", ""))
    page = html.decode("utf-8", "replace")

    for needle, label in NEW_MARKERS:
        ck(needle in page, f"新前端特征：{label}", needle)

    leaked = [m for m in OLD_MARKERS if m in page]
    ck(not leaked, "页面不含旧前端残留标记", ("发现 " + ", ".join(leaked)) if leaked else "无")

    # 引用的资源清单（顺序即加载顺序，与 <script> 出现次序一致）
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
    ck(bool(refs), "页面引用静态资源", f"{len(refs)} 个")

    # ---------- 3. 静态资源逐字节哈希比对 ----------
    print("\n[3] 静态资源完整性（容器返回 vs 本地磁盘）")
    bad_ct, bad_hash, missing = [], [], []
    for ref in refs:
        disk = STATIC / ref[len("/static/"):]
        if not disk.is_file():
            missing.append(ref)
            continue
        st, hd, body = fetch(base, ref, a.token)
        if st != 200:
            bad_ct.append(f"{ref}(HTTP {st})")
            continue
        ct = hd.get("Content-Type", "")
        want_js = ref.endswith(".js")
        want_css = ref.endswith(".css")
        if want_js and "javascript" not in ct:
            bad_ct.append(f"{ref}({ct})")
        if want_css and "css" not in ct:
            bad_ct.append(f"{ref}({ct})")
        if sha(body) != sha(disk.read_bytes()):
            bad_hash.append(f"{ref}(容器 {len(body)}B / 磁盘 {disk.stat().st_size}B)")

    ck(not missing, "引用的资源磁盘上均存在", ("缺 " + ", ".join(missing)) if missing else "全部命中")
    ck(not bad_ct, "全部资源 HTTP 200 且 Content-Type 正确",
       ("异常 " + "; ".join(bad_ct)) if bad_ct else f"{len(refs)} 个全通过")
    ck(not bad_hash, "★ 全部资源与本地磁盘字节一致（证明镜像装的是新前端）",
       ("不一致 " + "; ".join(bad_hash)) if bad_hash else f"{len(refs)} 个 sha256 全等")

    # 静态资源必须走协商缓存：否则升级镜像后浏览器可能拿到
    # 「新 index.html + 旧 JS」的混搭组合（本次开发中就出现过这种假象）
    st, hd, _ = fetch(base, "/static/js/app.js", a.token)
    cc = hd.get("Cache-Control", "")
    ck(cc == "no-cache", "静态资源带 Cache-Control: no-cache（防升级后新旧资源混搭）", cc or "(无)")

    # ---------- 4. 关键模块确实带上了业务逻辑 ----------
    print("\n[4] 关键模块内容抽查")
    probes = [
        ("/static/js/state.js", "COOKIE_STATES", "Cookie 状态映射"),
        ("/static/js/state.js", "progressOf", "狙击进度派生逻辑"),
        ("/static/js/ui.js", "setView", "视图切换"),
        ("/static/js/view-targets.js", "cookieLight", "Cookie 状态灯"),
        ("/static/js/cmdk.js", "UI.navigate", "命令面板跳转"),
        ("/static/app.js", "boot", "启动引导"),
        ("/static/style.css", "--primary", "设计系统令牌"),
    ]
    for ref, needle, label in probes:
        st, _, body = fetch(base, ref, a.token)
        txt = body.decode("utf-8", "replace")
        ck(st == 200 and needle in txt, f"{label}", f"{ref} 含 {needle}")

    # ---------- 5. API 统一信封 ----------
    print("\n[5] API 统一信封 {ok, message}")
    for path, want_ok in [("/api/config", True), ("/api/records", True),
                          ("/api/monitor/status", True), ("/api/cookie/pool", True)]:
        st, _, body = fetch(base, path, a.token)
        try:
            j = json.loads(body)
        except Exception:
            j = None
        good = st == 200 and isinstance(j, dict) and j.get("ok") is want_ok
        ck(good, f"GET {path} → ok:{want_ok}", f"status={st}")

    # 404 / 405 也必须走信封（api.py 已把 handler 挂到 StarletteHTTPException）
    print("\n[6] 未匹配路由也返回统一信封")
    for path, method, want in [("/api/definitely-not-here", "GET", 404),
                               ("/api/config", "DELETE", 405)]:
        st, hd, body = fetch(base, path, a.token, method=method)
        try:
            j = json.loads(body)
        except Exception:
            j = None
        good = (st == want and isinstance(j, dict)
                and j.get("ok") is False and isinstance(j.get("message"), str) and j.get("message"))
        ck(good, f"{method} {path} → {want} + 信封",
           f"status={st} msg={(j or {}).get('message')!r}")

    # ---------- 7. SSE ----------
    print("\n[7] SSE 日志流")
    try:
        st, ct, chunk = sse_probe(base, "/api/logs/stream")
    except Exception as e:
        st, ct, chunk = 0, "", b""
        ck(False, "GET /api/logs/stream 可连接", str(e))
    else:
        ck(st == 200, "GET /api/logs/stream → 200", f"status={st}")
    ck("text/event-stream" in ct, "Content-Type 是 text/event-stream", ct)
    ck(len(chunk) > 0, "首包有内容（心跳或日志）", f"{len(chunk)} 字节")

    # ---------- 8. 静态目录穿越 ----------
    print("\n[8] 路径穿越防护")
    for evil in ["/static/../web/api.py", "/static/%2e%2e/web/api.py",
                 "/static/../../etc/passwd"]:
        st, _, body = fetch(base, evil, a.token)
        leaked = b"FASTAPI" in body or b"root:" in body or b"APIKey" in body
        ck(not leaked, f"不泄露源码：{evil}", f"status={st}")

    # ---------- 汇总 ----------
    print("\n" + "-" * 72)
    print(f"共 {len(PASS) + len(FAIL)} 项：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
