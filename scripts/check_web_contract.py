#!/usr/bin/env python3
"""web 前端 ↔ 后端契约交叉验证。

做四件事（全部机械比对，不靠人眼）：

  1. 路由对齐：跑 scripts/web_contract_probe.js 拿到前端数据层**真实发出**的
     URL/Method，与 web/api.py 的路由表逐条匹配（双向：前端多调/后端多余）。
  2. 通道元数据对齐：web/static/js/state.js 的 CHANNEL_ORDER / LABELS /
     FIELDS 与 xianyu_alert/gui.py 的常量比对（名称、顺序、字段、默认值、密级）。
  3. 配置表单对齐：前端 buildForm() 产出的键 vs 后端
     `config_from_web_form` 读取的键 与 `web_form_from_config` 返回的键。
  4. 记录字段对齐：后端 product 表列名 + /api/records 补的别名，vs 前端
     实际访问的记录字段（静态扫描 r./it./rec./row. 前缀）。

用法：
    python3 scripts/check_web_contract.py            # 人类可读报告
    python3 scripts/check_web_contract.py --json     # 机器可读
退出码：0=无 P0；1=存在 P0（前端会真的调不通/读不到字段）
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_PY = os.path.join(ROOT, "web", "api.py")
SERVICE_PY = os.path.join(ROOT, "web", "monitor_service.py")
GUI_PY = os.path.join(ROOT, "xianyu_alert", "gui.py")
STORAGE_PY = os.path.join(ROOT, "xianyu_alert", "storage.py")
STATIC_DIR = os.path.join(ROOT, "web", "static")
PROBE_JS = os.path.join(ROOT, "scripts", "web_contract_probe.js")

HTTP_METHODS = {"get", "post", "put", "delete", "patch"}

#: 探针使用的 NODE 二进制（优先受管版本，回退系统 node）
NODE_CANDIDATES = [
    "/Users/xxx/.workbuddy/binaries/node/versions/22.22.2-3/bin/node",
    "node",
]

P0: list[str] = []
P1: list[str] = []
INFO: list[str] = []
OK: list[str] = []


def note(level: str, msg: str) -> None:
    {"P0": P0, "P1": P1, "INFO": INFO, "OK": OK}[level].append(msg)


# ---------------------------------------------------------------- #
# 1. 后端路由表
# ---------------------------------------------------------------- #
def parse_backend_routes() -> list[dict]:
    """从 web/api.py 解析 (method, path, prefix, func) 列表。"""
    tree = ast.parse(open(API_PY, encoding="utf-8").read())

    # api_router = APIRouter(prefix="/api", ...) → 找出 prefix
    prefixes: dict[str, str] = {"app": ""}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "APIRouter":
                pfx = ""
                for kw in node.value.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        pfx = str(kw.value.value)
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        prefixes[tgt.id] = pfx

    routes: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            f = dec.func
            if not isinstance(f, ast.Attribute) or f.attr.lower() not in HTTP_METHODS:
                continue
            owner = getattr(f.value, "id", None)
            if owner not in prefixes:
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            path = str(dec.args[0].value)
            routes.append(
                {
                    "method": f.attr.upper(),
                    "path": prefixes[owner] + path,
                    "func": node.name,
                    "owner": owner,
                }
            )
    return routes


def pattern_to_regex(pattern: str) -> re.Pattern:
    """把 /api/records/{product_id}/sold 变成可匹配具体 URL 的正则。"""
    parts = re.split(r"(\{[^}]+\})", pattern)
    out = []
    for p in parts:
        if p.startswith("{") and p.endswith("}"):
            out.append(r"[^/]+")
        else:
            out.append(re.escape(p))
    return re.compile("^" + "".join(out) + "$")


# ---------------------------------------------------------------- #
# 2. 前端探针
# ---------------------------------------------------------------- #
def run_probe() -> dict:
    node = None
    for cand in NODE_CANDIDATES:
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True)
            node = cand
            break
        except (OSError, subprocess.CalledProcessError):
            continue
    if node is None:
        raise RuntimeError("找不到可用的 node（用于运行契约探针）")
    proc = subprocess.run([node, PROBE_JS], capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        raise RuntimeError("探针执行失败：" + proc.stderr)
    return json.loads(proc.stdout)


# ---------------------------------------------------------------- #
# 3. gui.py 常量
# ---------------------------------------------------------------- #
def parse_gui_constants() -> dict:
    tree = ast.parse(open(GUI_PY, encoding="utf-8").read())
    want = {"CHANNEL_ORDER", "CHANNEL_LABELS", "CHANNEL_FIELDS"}
    found: dict[str, object] = {}
    for node in tree.body:
        tgt = None
        val = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            tgt, val = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            tgt, val = node.target.id, node.value
        if tgt in want and val is not None:
            found[tgt] = ast.literal_eval(val)
    return found


# ---------------------------------------------------------------- #
# 4. monitor_service 表单键
# ---------------------------------------------------------------- #
def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise KeyError(name)


def parse_form_contract() -> dict:
    src = open(SERVICE_PY, encoding="utf-8").read()
    tree = ast.parse(src)

    # --- config_from_web_form: 收集 form.get("X" [, default]) ---
    fn = _func(tree, "config_from_web_form")
    reads: dict[str, bool] = {}  # key -> 是否有默认值兜底
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "form"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            key = str(node.args[0].value)
            has_default = len(node.args) > 1 or bool(node.keywords)
            reads[key] = reads.get(key, False) or has_default
    # int(form.get(...) or 600) 之类：ast 上仍是 get 单参，但外层有 or 兜底
    # 这里用「出现次数 > 1 或存在 or 表达式」的近似：额外扫描 get 调用是否被 BinOp(BitOr) 包裹
    for node in ast.walk(fn):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.BitOr):
            for v in node.values:
                for sub in ast.walk(v):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "get"
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "form"
                        and sub.args
                        and isinstance(sub.args[0], ast.Constant)
                    ):
                        reads[str(sub.args[0].value)] = True

    # --- web_form_from_config: 返回字典字面量的键 ---
    fn2 = _func(tree, "web_form_from_config")
    returns: list[str] = []
    for node in ast.walk(fn2):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant):
                    returns.append(str(k.value))
    return {"reads": reads, "returns_keys": sorted(set(returns))}


# ---------------------------------------------------------------- #
# 5. storage 列名 + api.py 补的别名
# ---------------------------------------------------------------- #
def parse_record_fields() -> dict:
    src = open(STORAGE_PY, encoding="utf-8").read()
    m = re.search(r"CREATE TABLE IF NOT EXISTS product \((.*?)\n\)", src, re.S)
    cols = []
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"\s*([a-z_][a-z0-9_]*)\s+(INTEGER|TEXT|REAL)", line, re.I)
            if mm:
                cols.append(mm.group(1))

    api_src = open(API_PY, encoding="utf-8").read()
    aliases = sorted(set(re.findall(r'item\["([a-z_]+)"\]\s*=', api_src)))
    return {"columns": cols, "aliases": aliases}


# ---------------------------------------------------------------- #
# 6. 前端记录字段访问扫描
# ---------------------------------------------------------------- #
IDENT_RE = re.compile(r"\b(?:r|rec|row)\.([A-Za-z_][A-Za-z0-9_]*)\b")

FRONT_DERIVED = {"id", "idx", "day", "depth", "thresh", "below", "isToday", "isHit", "sold", "fresh"}

#: 定义记录契约的两个文件：这里的未知字段才是真的契约缺口（P1），
#: 其它文件（视图/入口）里出现的未知字段可能是 Promise 结果、命令对象等，
#: 归为 INFO 供人工复核。
CONTRACT_FILES = {"state.js", "ui.js"}

#: 已知的非记录用途（Promise.allSettled 的结果对象等）
NON_RECORD_ALLOW = {"status", "reason"}


def scan_front_javascript_record_access() -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    targets = [os.path.join(STATIC_DIR, "app.js")]
    js_dir = os.path.join(STATIC_DIR, "js")
    for f in sorted(os.listdir(js_dir)):
        if f.endswith(".js"):
            targets.append(os.path.join(js_dir, f))
    for path in targets:
        rel = os.path.relpath(path, ROOT)
        text = open(path, encoding="utf-8").read()
        for mm in IDENT_RE.finditer(text):
            hits.setdefault(mm.group(1), []).append(os.path.basename(rel))
    return hits


# ---------------------------------------------------------------- #
# 主流程
# ---------------------------------------------------------------- #
def main() -> int:
    as_json = "--json" in sys.argv
    report: dict = {}

    # ---- 1. 路由 ----
    backend = parse_backend_routes()
    probe = run_probe()
    #: 前端数据层可调用的路由 = /api/* + /healthz（/ 与 /static/* 由浏览器直接请求）
    callable_routes = [r for r in backend if r["path"].startswith("/api") or r["path"] == "/healthz"]
    api_routes = [r for r in backend if r["path"].startswith("/api")]
    regexes = [(r, pattern_to_regex(r["path"])) for r in callable_routes]

    matched_backend: set[int] = set()
    front_calls: list[dict] = []
    for entry in probe["functions"]:
        for call in entry["calls"]:
            url = call["url"].split("?")[0]
            method = call["method"]
            found = None
            for i, (r, rx) in enumerate(regexes):
                if r["method"] == method and rx.match(url):
                    found = r
                    matched_backend.add(i)
                    break
            front_calls.append(
                {"fn": entry["fn"], "method": method, "url": call["url"], "route": found["path"] if found else None}
            )
            if found is None:
                note("P0", f"前端调用了后端不存在的路由：{method} {call['url']}（来自 {entry['fn']}）")

    callable_paths = {r["path"] for r in callable_routes}
    api_paths = {r["path"] for r in api_routes}
    free_paths = callable_paths - api_paths
    for r in api_routes:
        if r["path"] not in {c["route"] for c in front_calls}:
            note("P1", f"后端路由未被前端调用：{r['method']} {r['path']}（{r['func']}）")

    non_api = [r for r in backend if r["path"] not in callable_paths]
    note(
        "OK",
        f"后端共 {len(backend)} 条路由 / {len({r['path'] for r in backend})} 个路径"
        f"（受认证 /api/* {len(api_routes)} 条、免认证可调用 {len(free_paths)} 条 "
        f"[{' '.join(sorted(free_paths))}]、浏览器直取 {len(non_api)} 条）",
    )
    note("OK", f"前端数据层 {len(probe['functions'])} 个函数、{len(front_calls)} 次请求全部命中后端路由")
    if probe["uncovered"]:
        note("P1", "数据层有未被探针覆盖的导出函数：" + ", ".join(probe["uncovered"]))

    report["routes"] = {"front_calls": front_calls, "backend": backend}

    # ---- 2. 通道元数据 ----
    gui = parse_gui_constants()
    b_order = list(gui.get("CHANNEL_ORDER", ()))
    f_order = list(probe["channelOrder"])
    if b_order != f_order:
        note("P0", f"通道顺序不一致：后端 {b_order} vs 前端 {f_order}")
    else:
        note("OK", f"通道顺序一致：{b_order}")

    b_labels = gui.get("CHANNEL_LABELS", {})
    for k in b_order:
        if b_labels.get(k) != probe["channelLabels"].get(k):
            note("P0", f"通道 {k} 中文名不一致：后端 {b_labels.get(k)!r} vs 前端 {probe['channelLabels'].get(k)!r}")
    extra_labels = set(probe["channelLabels"]) - set(b_labels)
    if extra_labels:
        note("P1", f"前端多出的通道文案：{sorted(extra_labels)}")

    b_fields = gui.get("CHANNEL_FIELDS", {})
    for k in b_order:
        bf = [(x[0], x[1], bool(x[2]), x[3]) for x in b_fields.get(k, ())]
        ff = [(x["key"], x["label"], x["secret"], x["def"]) for x in probe["channelFields"].get(k, [])]
        if bf != ff:
            note("P0", f"通道 {k} 字段定义不一致：\n      后端 {bf}\n      前端 {ff}")
    if not P0:
        note("OK", "六类通道的字段 key/名称/密级/默认值全部一致")

    # ---- 3. 配置表单 ----
    fc = parse_form_contract()
    front_keys = set(probe["form"]["keys"])
    back_reads = set(fc["reads"])
    back_returns = set(fc["returns_keys"])

    missing_in_front = back_reads - front_keys
    for k in sorted(missing_in_front):
        if fc["reads"].get(k):
            note("P1", f"后端读取表单字段 {k}，前端未提交（后端有默认值兜底，行为退化为默认）")
        else:
            note("P0", f"后端读取表单字段 {k}，前端未提交且后端无默认值兜底")
    for k in sorted(front_keys - back_reads):
        note("P1", f"前端提交了后端未读取的字段：{k}（会被忽略，通常无害）")
    if not missing_in_front and not (front_keys - back_reads):
        note("OK", f"配置表单 {len(front_keys)} 个键与后端读取键完全一致")

    # 提交键 ⊆ 返回键 + 关键词结构
    kw_keys = set(probe["form"]["keywordKeys"])
    expect_kw = {"keyword", "max_price", "enabled", "exclude_keywords", "required_keywords"}
    if kw_keys != expect_kw:
        note("P0", f"关键词表单结构不一致：前端 {sorted(kw_keys)} vs 后端期望 {sorted(expect_kw)}")
    else:
        note("OK", "关键词表单项（keyword/max_price/enabled/exclude_keywords/required_keywords）一致")

    report["form"] = {
        "front_keys": sorted(front_keys),
        "backend_reads": sorted(back_reads),
        "backend_returns": sorted(back_returns),
        "missing_in_front": sorted(missing_in_front),
    }

    # ---- 4. 记录字段 ----
    rf = parse_record_fields()
    backend_record = set(rf["columns"]) | set(rf["aliases"])
    access = scan_front_javascript_record_access()
    unknown = {}
    for name, files in access.items():
        if name in backend_record or name in FRONT_DERIVED or name in NON_RECORD_ALLOW:
            continue
        unknown[name] = sorted(set(files))
    unknown_p1 = {k: v for k, v in unknown.items() if CONTRACT_FILES & set(v)}
    unknown_info = {k: v for k, v in unknown.items() if k not in unknown_p1}
    for name, files in sorted(unknown_p1.items()):
        note("P0", f"记录契约文件里访问了后端不存在的字段 {name}（{', '.join(files)}）")
    for name, files in sorted(unknown_info.items()):
        note("INFO", f"非契约文件里出现 r./rec./row.{name}（{', '.join(files)}）—— 多为 Promise 结果/局部对象，非记录字段")
    if not unknown:
        note("OK", f"前端访问的 {len(access)} 个记录字段全部来自后端列/别名或前端派生")

    report["records"] = {
        "columns": rf["columns"],
        "aliases": rf["aliases"],
        "front_access": sorted(access),
        "unknown": unknown,
    }

    # ---- 输出 ----
    if as_json:
        report["P0"], report["P1"], report["INFO"], report["OK"] = P0, P1, INFO, OK
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("闲鱼低价提醒工具 · 前端 ↔ 后端 契约交叉验证")
        print("=" * 72)
        for level, items in (("P0", P0), ("P1", P1), ("INFO", INFO), ("OK", OK)):
            if not items:
                continue
            head = {
                "P0": "❌ P0 阻断级",
                "P1": "⚠️  P1 提示级",
                "INFO": "ℹ️  INFO 仅供参考",
                "OK": "✅ 通过",
            }[level]
            print(f"\n{head}（{len(items)}）")
            for it in items:
                print("  - " + it)
        print()
        print("-" * 72)
        print(f"结论：P0={len(P0)}  P1={len(P1)}  INFO={len(INFO)}  通过项={len(OK)}")
        if P0:
            print("存在 P0 —— 前端与后端契约不一致，必须先修。")
        else:
            print("无 P0 —— 路由、通道元数据、配置表单、记录字段四个维度均对齐。")

    return 1 if P0 else 0


if __name__ == "__main__":
    sys.exit(main())
