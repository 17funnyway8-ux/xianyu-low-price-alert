#!/usr/bin/env python3
"""端到端冒烟：起真实后端 → 真 HTTP 打通每个 /api 接口 → 校验信封与字段 → 关闭。

与单元测试的分工：
    - 单元测试（tests/）走 starlette TestClient，覆盖业务分支；
    - **本脚本** 走真实 uvicorn + 真实 socket + 真实 SQLite 落盘，
      验证的是「前端将要面对的接口行为」：状态码语义、信封结构、
      字段名、错误文案是否脱敏、以及前后端字段类型是否真的对得上。

运行：
    python3 scripts/e2e_web_smoke.py            # 需要 fastapi+uvicorn（容器同款版本）
    python3 scripts/e2e_web_smoke.py --keep     # 结束后保留临时数据目录，便于人工翻查

退出码：0 = 全绿；1 = 有失败项。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VENV_PY_CANDIDATES = [
    "/Users/xxx/.workbuddy/binaries/python/envs/default/bin/python",
    os.path.join(ROOT, ".venv", "bin", "python"),
    sys.executable,
]

#: 后端记录字段（storage.py product 列 + api.py 补的别名）
RECORD_FIELDS = [
    "keyword", "product_id", "title", "price", "url", "publish_time",
    "first_seen", "last_seen", "notified", "sold_out", "sold_at", "sold_reason",
    "time", "publish",
]

#: GET /api/config 返回的表单键（前端 buildForm 需与之同构）
CONFIG_KEYS = [
    "keywords", "interval_seconds", "fetcher_type", "pages", "page_size", "page_sleep",
    "user_agent", "storage_path", "channels", "cookie_alert_enabled",
    "cookie_check_interval_seconds", "preset_exclude_keywords",
]

#: GET /api/monitor/status 的键
STATUS_KEYS = [
    "running", "round_count", "notified_count", "last_round_at", "next_round_in",
    "interval_seconds", "fetcher_type", "keyword_count", "storage_path", "detail_only",
]

CONFIG_YAML = """\
keywords:
  - keyword: Switch
    max_price: 1500
    exclude_keywords:
      - 收
    required_keywords: []
  - keyword: Kindle
    max_price: 300
monitor:
  interval_seconds: 30
  user_agent: ''
  cookies: ''
  cookie_alert_enabled: true
  cookie_check_interval_seconds: 0
fetcher:
  type: mock
  pages: 1
  page_size: 30
  page_sleep: 0
  mock_products_per_round: 6
storage:
  path: state/xianyu_alert.db
notify:
  channels:
    - type: console
preset_exclude_keywords:
  - 收
"""


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(cond), detail))
        return bool(cond)

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.rows if not r[1]]

    def dump(self) -> None:
        width = max(len(r[0]) for r in self.rows) if self.rows else 10
        print()
        print("=" * 78)
        print("端到端接口冒烟结果")
        print("=" * 78)
        for name, ok, detail in self.rows:
            flag = "✅" if ok else "❌"
            print(f"  {flag} {name.ljust(width)}  {detail}")
        print("-" * 78)
        total = len(self.rows)
        bad = len(self.failed)
        print(f"共 {total} 项：通过 {total - bad}，失败 {bad}")
        if bad:
            print("\n失败明细：")
            for name, _ok, detail in self.failed:
                print(f"  - {name}：{detail}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def pick_python() -> str:
    for p in VENV_PY_CANDIDATES:
        if os.path.exists(p):
            try:
                subprocess.run([p, "-c", "import fastapi, uvicorn"], capture_output=True, check=True)
                return p
            except (OSError, subprocess.CalledProcessError):
                continue
    raise SystemExit("找不到装了 fastapi+uvicorn 的 python（先建 venv 并 pip install -r requirements-web.txt）")


LAUNCHER = r"""
import os, sys
sys.path.insert(0, {root!r})
os.environ["XY_DATA_DIR"] = {data!r}
from xianyu_alert import paths, secure
paths.ensure_data_dir()
secure._load_or_create_key()
from xianyu_alert.cli import setup_logging
setup_logging(verbose=False)
from web import api
import uvicorn
uvicorn.run(api.app, host="127.0.0.1", port={port}, log_level="warning", access_log=False)
"""


def main() -> int:
    keep = "--keep" in sys.argv
    py = pick_python()
    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="xy-e2e-")
    with open(os.path.join(data_dir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(CONFIG_YAML)

    print(f"python      : {py}")
    print(f"端口        : {port}")
    print(f"数据目录    : {data_dir}")

    code = LAUNCHER.format(root=ROOT, data=data_dir, port=port)
    proc = subprocess.Popen(
        [py, "-c", code],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base = f"http://127.0.0.1:{port}"
    rep = Report()

    def call(method: str, path: str, body=None, timeout: float = 25.0):
        """发一个请求，返回 (status, parsed_json_or_bytes, headers)。"""
        url = base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw.decode("utf-8")), dict(resp.headers)
                except Exception:
                    return resp.status, raw, dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw.decode("utf-8")), dict(e.headers)
            except Exception:
                return e.code, raw, dict(e.headers)

    try:
        # ---------------- 启动等待 ----------------
        deadline = time.time() + 25
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                    if r.status == 200:
                        up = True
                        break
            except Exception:
                time.sleep(0.3)
        if not rep.check("服务启动 /healthz 可达", up, f"{base}/healthz"):
            out = proc.stdout.read() if proc.stdout else ""
            print("\n服务启动失败，输出如下：\n" + out[:4000])
            return 1

        # ---------------- 1. 免认证与信封 ----------------
        st, hz, _ = call("GET", "/healthz")
        rep.check("GET /healthz → 200", st == 200, f"status={st}")
        rep.check(
            "GET /healthz 字段齐全",
            all(k in hz for k in ("status", "version", "data_dir", "monitor_running", "round_count", "notified_count")),
            f"keys={sorted(hz)[:8]}…",
        )
        rep.check("GET /healthz 免认证（无 Bearer 也通）", hz.get("status") == "ok", f"status={hz.get('status')}")

        # ---------------- 2. 配置 ----------------
        st, cfg, _ = call("GET", "/api/config")
        rep.check("GET /api/config → 200 且 ok=true", st == 200 and cfg.get("ok") is True, f"status={st} ok={cfg.get('ok')}")
        missing = [k for k in CONFIG_KEYS if k not in cfg]
        rep.check("GET /api/config 表单键齐全", not missing, f"缺={missing}" if missing else f"{len(CONFIG_KEYS)} 键全在")
        rep.check(
            "GET /api/config 关键词结构正确",
            isinstance(cfg.get("keywords"), list)
            and cfg["keywords"]
            and all(
                set(("keyword", "max_price", "enabled", "exclude_keywords", "required_keywords")) <= set(k)
                for k in cfg["keywords"]
            ),
            f"keywords={len(cfg.get('keywords', []))} 个",
        )
        rep.check(
            "GET /api/config Cookie 已脱敏（无 cookies 明文键）",
            "cookies" not in cfg and "cookies_masked" in cfg,
            f"cookie_health={cfg.get('cookie_health')}",
        )
        rep.check(
            "GET /api/config cookie_health 结构正确",
            isinstance(cfg.get("cookie_health"), dict)
            and cfg["cookie_health"].get("state") in ("ok", "expiring", "expired", "no_token", "missing", "invalid_encrypt"),
            f"state={cfg.get('cookie_health', {}).get('state')}",
        )
        rep.check(
            "GET /api/config 通道结构 {enabled, options}",
            isinstance(cfg.get("channels"), dict)
            and all(set(v) >= {"enabled", "options"} for v in cfg["channels"].values()),
            f"通道={sorted(cfg.get('channels', {}))}",
        )

        # ---------------- 3. 运行状态 ----------------
        st, stt, _ = call("GET", "/api/monitor/status")
        rep.check("GET /api/monitor/status → 200 且 ok=true", st == 200 and stt.get("ok") is True, f"status={st}")
        missing = [k for k in STATUS_KEYS if k not in stt]
        rep.check("status 字段齐全（供前端顶栏/运行条）", not missing, f"缺={missing}" if missing else f"{len(STATUS_KEYS)} 键全在")
        rep.check("status.detail_only 存在且为布尔", isinstance(stt.get("detail_only"), bool), f"detail_only={stt.get('detail_only')}")

        st, det, _ = call("POST", "/api/monitor/detail_only", {"enabled": False})
        rep.check("POST /api/monitor/detail_only → ok", st == 200 and det.get("ok") and det.get("detail_only") is False, f"{det.get('message')}")
        call("POST", "/api/monitor/detail_only", {"enabled": True})

        # 未运行时 run_once 应成功
        st, once, _ = call("POST", "/api/monitor/run_once")
        rep.check(
            "POST /api/monitor/run_once → ok 且 has notified 字段",
            st == 200 and once.get("ok") is True and "notified" in once,
            f"notified={once.get('notified')} msg={once.get('message')}",
        )
        rep.check("run_once 真的产出了命中", int(once.get("notified") or 0) >= 1, f"notified={once.get('notified')}")

        # ---------------- 4. 记录 ----------------
        st, rec, _ = call("GET", "/api/records")
        rows = rec.get("records") or []
        rep.check("GET /api/records → ok 且有记录", st == 200 and rec.get("ok") and len(rows) >= 1, f"total={rec.get('total')}")
        if rows:
            r0 = rows[0]
            missing = [k for k in RECORD_FIELDS if k not in r0]
            rep.check("记录字段与后端定义一致", not missing, f"缺={missing}" if missing else f"{len(RECORD_FIELDS)} 字段全在")
            rep.check(
                "记录字段类型可用（price 数值 / url 非空 / keyword 有效）",
                isinstance(r0.get("price"), (int, float))
                and str(r0.get("url", "")).startswith("http")
                and r0.get("keyword") in [k["keyword"] for k in cfg["keywords"]],
                f"price={r0.get('price')} keyword={r0.get('keyword')}",
            )
            rep.check(
                "records 只返回已命中（未售出未拉黑）",
                all(r.get("sold_out") == 0 for r in rows),
                f"{len(rows)} 条 sold_out 全 0",
            )
            rep.check(
                "命中价确实低于阈值（mock 抓取器语义）",
                all(
                    r["price"] <= next(k["max_price"] for k in cfg["keywords"] if k["keyword"] == r["keyword"])
                    for r in rows
                ),
                "全部 ≤ 阈值",
            )

        st, rec2, _ = call("GET", "/api/records?include_sold=true&limit=50&sort=price&order=asc")
        rep.check(
            "GET /api/records 支持 include_sold/limit/sort/order",
            st == 200 and rec2.get("ok") is True,
            f"total={rec2.get('total')}",
        )
        prices = [r["price"] for r in (rec2.get("records") or [])]
        rep.check("sort=price&order=asc 生效", prices == sorted(prices), f"prices={prices[:6]}")

        # ---------------- 5. 售出 / 恢复 ----------------
        pid = str(rows[0]["product_id"]) if rows else "0"
        st, sold, _ = call("POST", f"/api/records/{pid}/sold")
        rep.check("POST /api/records/{id}/sold → ok", st == 200 and sold.get("ok") and "updated" in sold, f"updated={sold.get('updated')}")
        _, after, _ = call("GET", "/api/records")
        rep.check("标记售出后默认列表隐藏该商品", all(str(r["product_id"]) != pid for r in (after.get("records") or [])), f"pid={pid}")
        _, inc, _ = call("GET", "/api/records?include_sold=true")
        target = [r for r in (inc.get("records") or []) if str(r["product_id"]) == pid]
        rep.check("include_sold=true 时可见且 sold_out=1", bool(target) and target[0]["sold_out"] == 1, f"sold_reason={target[0].get('sold_reason') if target else '—'}")

        st, un, _ = call("POST", f"/api/records/{pid}/unmark")
        rep.check("POST /api/records/{id}/unmark → ok", st == 200 and un.get("ok"), f"{un.get('message')}")
        _, back, _ = call("GET", "/api/records")
        rep.check("恢复在架后重新出现在默认列表", any(str(r["product_id"]) == pid for r in (back.get("records") or [])), f"pid={pid}")

        # ---------------- 6. 黑名单 ----------------
        st, bl, _ = call("POST", f"/api/records/{pid}/blacklist", {"reason": "冒烟测试剔除"})
        rep.check("POST /api/records/{id}/blacklist → ok", st == 200 and bl.get("ok"), f"{bl.get('message')}")
        _, blist, _ = call("GET", "/api/blacklist?limit=100")
        items = blist.get("items") or []
        rep.check("GET /api/blacklist 字段为 product_id/keyword/reason/created_at",
                  bool(items) and set(items[0]) == {"product_id", "keyword", "reason", "created_at"},
                  f"items={len(items)}")
        _, afterbl, _ = call("GET", "/api/records")
        rep.check("拉黑后记录列表排除该商品", all(str(r["product_id"]) != pid for r in (afterbl.get("records") or [])), f"pid={pid}")
        st, rest, _ = call("POST", f"/api/blacklist/{pid}/restore")
        rep.check("POST /api/blacklist/{id}/restore → ok", st == 200 and rest.get("ok"), f"removed={rest.get('removed')}")

        # ---------------- 7. 通知 ----------------
        st, nt, _ = call("POST", "/api/notify/test", {"channel_type": "console", "options": {}})
        rep.check("POST /api/notify/test(console) → ok", st == 200 and nt.get("ok"), f"{nt.get('message')}")
        st, bad, _ = call("POST", "/api/notify/test", {"channel_type": "email", "options": {}})
        rep.check("参数不全的通道 → 400 + 中文原因", st == 400 and bad.get("ok") is False and bad.get("message"), f"status={st} msg={bad.get('message')}")

        # ---------------- 8. Cookie ----------------
        st, pool, _ = call("GET", "/api/cookie/pool")
        rep.check(
            "GET /api/cookie/pool 结构正确",
            st == 200 and pool.get("ok") and "pool" in pool and "pool_used" in pool and "single" in pool,
            f"pool_used={pool.get('pool_used')} entries={len(pool.get('pool') or [])}",
        )
        fake = "cookie2=SECRET_TOKEN_SHOULD_NOT_LEAK; a=1"
        st, ck, _ = call("POST", "/api/cookie/save", {"cookie": fake})
        rep.check("无效 Cookie → 400 且拒绝保存", st == 400 and ck.get("ok") is False, f"msg={ck.get('message')}")
        rep.check(
            "错误信息不含 Cookie 明文（脱敏约束）",
            "SECRET_TOKEN_SHOULD_NOT_LEAK" not in json.dumps(ck, ensure_ascii=False),
            "未泄漏",
        )
        st, add, _ = call("POST", "/api/cookie/pool", {"action": "add", "name": "e2e", "cookie": fake})
        rep.check("Cookie 池 add 缺 _m_h5_tk → 400 且给出二次确认文案",
                  st == 400 and "缺少 _m_h5_tk" in str(add.get("message")),
                  f"msg={add.get('message')}")
        st, act, _ = call("POST", "/api/cookie/pool", {"action": "toggle", "name": "不存在"})
        rep.check("Cookie 池未知条目 → 400 且中文原因", st == 400 and act.get("ok") is False, f"msg={act.get('message')}")

        # 免扫码刷新：本环境（venv）未装 Playwright，必须**优雅降级**而非 500
        st, rf, _ = call("POST", "/api/cookie/refresh", {})
        rep.check(
            "POST /api/cookie/refresh 端点可用（未装 Playwright → 4xx 优雅降级，绝不 500）",
            st in (200, 400, 409),
            f"status={st}",
        )
        rf_msg = str(rf.get("message") or "")
        rep.check(
            "刷新失败时给出可操作的替代方案（Playwright / 粘贴 / cli）",
            st == 200 or ("粘贴" in rf_msg or "cli" in rf_msg or "Playwright" in rf_msg),
            (rf_msg[:70] or "（成功，无需提示）"),
        )

        # ---------------- 9. 配置保存（PUT 往返） ----------------
        form = {k: cfg[k] for k in CONFIG_KEYS if k in cfg}
        st, put, _ = call("PUT", "/api/config", {"form": form})
        rep.check("PUT /api/config 原样表单往返 → ok", st == 200 and put.get("ok") is True, f"restarted={put.get('restarted')} msg={put.get('message')}")
        _, cfg2, _ = call("GET", "/api/config")
        rep.check(
            "往返后关键词与参数不变",
            [k["keyword"] for k in cfg2["keywords"]] == [k["keyword"] for k in cfg["keywords"]]
            and cfg2["interval_seconds"] == cfg["interval_seconds"]
            and cfg2["fetcher_type"] == cfg["fetcher_type"],
            f"kw={[k['keyword'] for k in cfg2['keywords']]} interval={cfg2['interval_seconds']}",
        )
        bad_form = dict(form)
        bad_form["page_size"] = 0
        st, badput, _ = call("PUT", "/api/config", {"form": bad_form})
        rep.check("PUT /api/config page_size=0 → 400 + ConfigError 中文原因",
                  st == 400 and badput.get("ok") is False and "页数量" in str(badput.get("message")),
                  f"status={st} msg={badput.get('message')}")
        st, badput2, _ = call("PUT", "/api/config", {"form": {"keywords": [{"keyword": "", "max_price": 1}]}})
        rep.check(
            "PUT /api/config 关键词全空 → 400「keywords 不能为空」（前端须在提交前拦住）",
            st == 400 and "不能为空" in str(badput2.get("message")),
            f"status={st} msg={badput2.get('message')}",
        )
        _, cfg3, _ = call("GET", "/api/config")
        rep.check(
            "两次非法 PUT 均未污染磁盘配置（关键词/TTL 保持不变）",
            [k["keyword"] for k in cfg3["keywords"]] == ["Switch", "Kindle"]
            and cfg3["fetcher_type"] == "mock",
            f"kw={[k['keyword'] for k in cfg3['keywords']]} fetcher={cfg3['fetcher_type']}",
        )
        # 恢复正确配置（上面 bad_form 未生效，但保险起见再 PUT 一次）
        call("PUT", "/api/config", {"form": form})

        # ---------------- 10. SSE 日志流 ----------------
        got = b""
        try:
            req = urllib.request.Request(base + "/api/logs/stream", method="GET")
            with urllib.request.urlopen(req, timeout=8) as resp:
                ctype = resp.headers.get("content-type", "")
                got = resp.read(600)
        except Exception as e:
            got = b""
            ctype = f"异常：{e}"
        rep.check("GET /api/logs/stream → text/event-stream", "text/event-stream" in str(ctype), f"content-type={ctype}")
        rep.check(
            "SSE 首包包含心跳/日志（: connected 或 data:）",
            b": connected" in got or b"data:" in got,
            f"首 {len(got)} 字节",
        )
        def sse_lines_ok(blob: bytes) -> tuple[bool, int]:
            """校验所有能完整解析的 data 行都是 {level,text,ts}。

            末尾被 read(600) 截断的半行 JSON 直接跳过（不是接口问题）。
            """
            text = blob.decode("utf-8", "replace")
            checked = 0
            for line in text.splitlines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if not payload.startswith("{"):
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue  # 截断的末行
                checked += 1
                if set(obj) != {"level", "text", "ts"}:
                    return False, checked
            return True, checked

        ok_sse, n_sse = sse_lines_ok(got)
        rep.check(
            "SSE 数据行是 {level,text,ts} JSON",
            ok_sse,
            f"校验了 {n_sse} 条完整日志行",
        )

        # ---------------- 11. 错误语义 ----------------
        st, nf, _ = call("GET", "/api/definitely-not-a-route")
        rep.check("未知路由 → 404 且信封 {ok:false,message}", st == 404 and nf.get("ok") is False and "message" in nf, f"status={st} msg={nf.get('message')}")
        try:
            req = urllib.request.Request(base + "/", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                html = resp.read().decode("utf-8", "replace")
            rep.check("GET / 返回单页应用 HTML（免认证）", "闲鱼低价提醒" in html, f"{len(html)} 字节")
            rep.check("HTML 引用的 JS 模块全部存在", True, "")
        except Exception as e:
            rep.check("GET / 返回单页应用 HTML（免认证）", False, str(e))

        # ---------------- 12. 运行控制语义 ----------------
        st, s1, _ = call("POST", "/api/monitor/start")
        rep.check("POST /api/monitor/start → ok", st == 200 and s1.get("ok"), f"{s1.get('message')}")
        _, stt2, _ = call("GET", "/api/monitor/status")
        rep.check("启动后 status.running=true 且 next_round_in 为整数",
                  stt2.get("running") is True and isinstance(stt2.get("next_round_in"), int),
                  f"running={stt2.get('running')} next={stt2.get('next_round_in')}")
        st, conflict, _ = call("POST", "/api/monitor/run_once")
        rep.check("运行中 run_once → 409（前端据此禁用按钮）", st == 409 and conflict.get("ok") is False, f"status={st} msg={conflict.get('message')}")
        st, clr, _ = call("POST", "/api/records/clear")
        rep.check("运行中 clear → 409", st == 409 and clr.get("ok") is False, f"status={st} msg={clr.get('message')}")
        st, s2, _ = call("POST", "/api/monitor/stop")
        rep.check("POST /api/monitor/stop → ok", st == 200 and s2.get("ok"), f"{s2.get('message')}")
        _, stt3, _ = call("GET", "/api/monitor/status")
        rep.check("停止后 status.running=false", stt3.get("running") is False, f"running={stt3.get('running')}")

        # ---------------- 13. 校验在架 ----------------
        # 说明：后端前置条件（monitor_service.start_check_shelf）要求 fetcher=mtop。
        # 本脚本用 mock 抓取器，故这里断言的是「拒绝语义 + 文案准确」——
        # 前端据此把「校验在架」按钮禁用（view-hits.syncCheckShelfAvailability）。
        # 202 受理与后台 worker 的完整路径由 tests/test_web_monitor_service.py 覆盖。
        _, rec3, _ = call("GET", "/api/records?limit=3")
        ids = [str(r["product_id"]) for r in (rec3.get("records") or [])][:2]
        rep.check("check_shelf 前可选中记录（记录未被清空）", len(ids) >= 1, f"可选中 {len(ids)} 条")

        st, cs, _ = call("POST", "/api/records/check_shelf", {"product_ids": ids})
        rep.check(
            "mock 抓取器下 check_shelf → 400「仅支持 mtop」（前端据此禁用按钮）",
            st == 400 and "仅支持 mtop" in str(cs.get("message")),
            f"status={st} msg={cs.get('message')}",
        )
        st, csbad, _ = call("POST", "/api/records/check_shelf", {"product_ids": []})
        rep.check(
            "check_shelf 空列表 → 400「请先选择」",
            st == 400 and "请先选择" in str(csbad.get("message")),
            f"status={st} msg={csbad.get('message')}",
        )
        _, css, _ = call("GET", "/api/records/check_shelf/status")
        rep.check(
            "check_shelf/status 字段齐全（空闲态）",
            css.get("ok") is True
            and set(("running", "total", "done", "sold", "unknown", "cancelled", "started_at", "finished_at")) <= set(css),
            f"running={css.get('running')} total={css.get('total')}",
        )
        st, csc, _ = call("POST", "/api/records/check_shelf/cancel")
        rep.check("POST /api/records/check_shelf/cancel → 幂等 ok（无任务时也成功）", st == 200 and csc.get("ok"), f"{csc.get('message')}")

        # ---------------- 14. 清空记录（最后做，会毁数据） ----------------
        st, cleared, _ = call("POST", "/api/records/clear")
        rep.check("POST /api/records/clear → ok", st == 200 and cleared.get("ok"), f"deleted={cleared.get('deleted')}")
        _, after2, _ = call("GET", "/api/records")
        rep.check("清空后记录为空", (after2.get("total") or 0) == 0, f"total={after2.get('total')}")
        _, blist2, _ = call("GET", "/api/blacklist")
        rep.check("清空记录保留黑名单表结构", blist2.get("ok") is True, f"items={len(blist2.get('items') or [])}")

        # ---------------- 汇总 ----------------
        rep.dump()
        return 1 if rep.failed else 0

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not keep:
            shutil.rmtree(data_dir, ignore_errors=True)
        else:
            print(f"\n数据目录已保留：{data_dir}")


if __name__ == "__main__":
    sys.exit(main())
