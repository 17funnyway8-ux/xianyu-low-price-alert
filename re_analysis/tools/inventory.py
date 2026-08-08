# -*- coding: utf-8 -*-
"""解包产物清单生成 + 业务模块识别 + 行为线索静态提取。

用法:
    python inventory.py <extracted_root> <evidence_dir>
"""
from __future__ import annotations

import marshal
import os
import re
import sys
import types
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pyc_analyze import collect_strings, load_code  # noqa: E402

# PyInstaller 运行时自带的引导模块（非业务、非第三方）
RUNTIME_PYC: Set[str] = {
    "pyiboot01_bootstrap.pyc", "pyimod01_archive.pyc", "pyimod02_importers.pyc",
    "pyimod03_ctypes.pyc", "pyimod04_pywin32.pyc", "pyi_rth_inspect.pyc",
    "pyi_rth_pkgutil.pyc", "pyi_rth_multiprocessing.pyc", "pyi_rth__tkinter.pyc",
    "pyi_rth_setuptools.pyc", "struct.pyc",
}

# 已知第三方库顶层名（用于分类）
THIRD_PARTY: Set[str] = {
    "requests", "urllib3", "certifi", "charset_normalizer", "idna",
    "playwright", "pyee", "greenlet", "yaml", "bs4", "soupsieve",
    "setuptools", "packaging", "_distutils_hack",
}

URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+")
PATH_RE = re.compile(r"[A-Za-z0-9_\-.]+\.(?:json|jsonl|yaml|yml|ico|db|sqlite3?|log|lnk|txt|ini|cfg)\b")


def human(n: int) -> str:
    """字节数转可读字符串。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def scan_tree(root: str) -> Dict[str, List[Tuple[str, int]]]:
    """扫描解包目录，按类别归类文件。"""
    buckets: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            ext = os.path.splitext(fn)[1].lower()
            if ext in (".dll", ".pyd", ".so", ".exe"):
                buckets["binary"].append((rel, size))
            elif ext == ".pyc":
                buckets["pyc"].append((rel, size))
            else:
                buckets["data"].append((rel, size))
    return buckets


def classify_pyc(rel: str) -> str:
    """判定 pyc 属于 业务/第三方/标准库/运行时。"""
    base = os.path.basename(rel)
    if base in RUNTIME_PYC or base.startswith("pyi"):
        return "runtime"
    if rel.startswith("PYZ.pyz_extracted/"):
        sub = rel[len("PYZ.pyz_extracted/"):]
        top = sub.split("/")[0].replace(".pyc", "")
        return "thirdparty" if top in THIRD_PARTY else "stdlib"
    if "/" not in rel:
        return "business"  # CArchive 顶层非运行时 pyc → 候选业务脚本
    top = rel.split("/")[0]
    return "thirdparty" if top in THIRD_PARTY else "stdlib"


def main() -> int:
    root: str = sys.argv[1]
    ev: str = sys.argv[2]
    os.makedirs(ev, exist_ok=True)

    buckets = scan_tree(root)
    pycs = sorted(buckets["pyc"])
    bins = sorted(buckets["binary"])
    data = sorted(buckets["data"])

    groups: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for rel, size in pycs:
        groups[classify_pyc(rel)].append((rel, size))

    # -------- 02 归档清单 --------
    lines: List[str] = []
    add = lines.append
    add("# 02 - PyInstaller 归档解包清单")
    add("")
    add("> 工具：`pyinstxtractor-ng 2026.7.3`（只读打开目标 exe，输出至 `re_analysis/extracted/`）")
    add("")
    add("## 2.1 解包总览")
    add("")
    add("| 项 | 值 |")
    add("| --- | --- |")
    add("| CArchive 条目数 | 1196（工具报告） |")
    add("| PYZ 归档条目数 | 555（工具报告） |")
    add("| PyInstaller 版本特征 | 2.1+ （CArchive cookie 88 字节格式） |")
    add("| Python 运行时版本 | 3.13（工具自动识别） |")
    add("| 归档载荷长度 | 53,585,903 字节 |")
    add(f"| 落盘文件总数 | {len(pycs) + len(bins) + len(data)} |")
    add(f"| ├─ .pyc | {len(pycs)} |")
    add(f"| ├─ .dll/.pyd | {len(bins)} |")
    add(f"| └─ 其它数据文件 | {len(data)} |")
    add("")
    add("## 2.2 pyc 分类统计")
    add("")
    add("| 类别 | 数量 | 说明 |")
    add("| --- | --- | --- |")
    label = {
        "business": "业务代码（CArchive 顶层、非 PyInstaller 运行时）",
        "runtime": "PyInstaller 引导/运行时钩子",
        "thirdparty": "第三方依赖",
        "stdlib": "CPython 标准库",
    }
    for k in ("business", "runtime", "thirdparty", "stdlib"):
        add(f"| {k} | {len(groups[k])} | {label[k]} |")
    add("")
    add("## 2.3 CArchive 顶层 pyc（全部列出）")
    add("")
    add("| 文件 | 大小 | 分类 |")
    add("| --- | --- | --- |")
    for rel, size in pycs:
        if "/" not in rel:
            add(f"| `{rel}` | {size:,} B | {classify_pyc(rel)} |")
    add("")
    add("## 2.4 体积 Top 30 二进制")
    add("")
    add("| 文件 | 大小 |")
    add("| --- | --- |")
    for rel, size in sorted(bins, key=lambda x: -x[1])[:30]:
        add(f"| `{rel}` | {human(size)} |")
    add("")
    with open(os.path.join(ev, "02_archive_toc.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # -------- 03 模块清单 --------
    lines = []
    add = lines.append
    add("# 03 - 模块清单：业务代码 / 第三方库 / 运行时")
    add("")
    add("## 3.1 业务模块（核心发现）")
    add("")
    add("| 文件 | 大小 | co_filename | code object 数 |")
    add("| --- | --- | --- | --- |")
    biz: List[str] = []
    for rel, size in groups["business"]:
        full = os.path.join(root, rel)
        try:
            c = load_code(full)
            n = len(list(_count(c)))
            add(f"| `{rel}` | {size:,} B | `{c.co_filename}` | {n} |")
            biz.append(rel)
        except Exception as exc:  # noqa: BLE001
            add(f"| `{rel}` | {size:,} B | (解析失败: {exc}) | - |")
    add("")
    add("> **关键结论**：CArchive 顶层只有 **1 个** 业务脚本 `xianyu_price_alert.pyc`，")
    add("> 其 `co_filename` 为 `xianyu_price_alert.py`。**归档中不存在 `xianyu_alert` 包**（已用")
    add("> `find . -iname '*xianyu*'` 全量搜索确认，仅命中该单文件）。")
    add("> 即：本 exe 是一个**单文件脚本**的打包产物，不是本地模块化项目的打包产物。")
    add("")

    add("## 3.2 第三方库（按顶层包聚合）")
    add("")
    tp: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "bytes": 0})
    for rel, size in groups["thirdparty"]:
        sub = rel[len("PYZ.pyz_extracted/"):] if rel.startswith("PYZ.pyz_extracted/") else rel
        top = sub.split("/")[0].replace(".pyc", "")
        tp[top]["count"] += 1
        tp[top]["bytes"] += size
    add("| 顶层包 | pyc 数 | 合计字节 | 版本证据 |")
    add("| --- | --- | --- | --- |")
    ver_evidence: Dict[str, str] = {
        "playwright": "`playwright-1.61.0.dist-info/` 目录 → **1.61.0**（确证）",
        "certifi": "随 requests 附带，无 dist-info（未知版本）",
        "charset_normalizer": "含 `ada92cb5d92a588d1b93__mypyc.cp313-win_amd64.pyd`（mypyc 编译版，≥3.x）",
        "greenlet": "`greenlet/` 顶层目录 + `_greenlet.cp313-win_amd64.pyd`",
        "requests": "无 dist-info（未知版本）",
        "urllib3": "无 dist-info（未知版本）",
        "pyee": "playwright 依赖",
        "idna": "requests 依赖",
        "setuptools": "打包环境残留",
        "packaging": "setuptools 依赖",
        "_distutils_hack": "setuptools 残留",
    }
    for top in sorted(tp):
        add(f"| `{top}` | {tp[top]['count']} | {tp[top]['bytes']:,} | {ver_evidence.get(top, '—')} |")
    add("")
    add("> **注意**：`yaml`(PyYAML) 与 `bs4`(beautifulsoup4) **均未出现**在归档中。")
    add("> 而本地项目 `requirements.txt` 明确依赖这两者 → 强差分证据（见 04）。")
    add("")

    add("## 3.3 运行时 / 二进制")
    add("")
    add("| 文件 | 大小 | 作用 |")
    add("| --- | --- | --- |")
    notes = {
        "python313.dll": "CPython 3.13 解释器主体（**确证 Python 版本**）",
        "_tkinter.pyd": "tkinter C 扩展 → GUI 使用 tkinter",
        "_ssl.pyd": "TLS 支持（https 请求）",
        "_hashlib.pyd": "hashlib（MD5 签名依赖）",
        "libcrypto-3-x64.dll": "OpenSSL 3.x",
        "libssl-3-x64.dll": "OpenSSL 3.x",
        "_asyncio.pyd": "asyncio（playwright 依赖）",
        "_socket.pyd": "socket",
        "VCRUNTIME140.dll": "MSVC 运行时",
    }
    for rel, size in bins:
        base = os.path.basename(rel)
        if base in notes or size > 1_000_000:
            add(f"| `{rel}` | {human(size)} | {notes.get(base, '—')} |")
    add("")
    add("### Tcl/Tk 数据目录（tkinter GUI 确证）")
    add("")
    tcl = [d for d in ("_tcl_data", "_tk_data", "tcl8") if os.path.isdir(os.path.join(root, d))]
    for d in tcl:
        cnt = sum(len(fs) for _, _, fs in os.walk(os.path.join(root, d)))
        add(f"- `{d}/` — {cnt} 个文件")
    add("")
    with open(os.path.join(ev, "03_module_inventory.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # -------- 行为线索：业务模块字符串 --------
    all_urls: Set[str] = set()
    all_paths: Set[str] = set()
    all_strs: Set[str] = set()
    for rel in biz:
        code = load_code(os.path.join(root, rel))
        s = collect_strings(code)
        all_strs |= s
        for item in s:
            all_urls |= set(URL_RE.findall(item))
            all_paths |= set(PATH_RE.findall(item))

    with open(os.path.join(ev, "_business_strings.txt"), "w", encoding="utf-8") as f:
        f.write("### URLs ###\n")
        for u in sorted(all_urls):
            f.write(u + "\n")
        f.write("\n### FILE PATHS / NAMES ###\n")
        for p in sorted(all_paths):
            f.write(p + "\n")
        f.write("\n### ALL STRING CONSTANTS ###\n")
        for s in sorted(all_strs):
            f.write(repr(s) + "\n")

    print(f"[ok] business={len(biz)} thirdparty_top={len(tp)} urls={len(all_urls)} strings={len(all_strs)}")
    print("URLs:", sorted(all_urls))
    print("PATHS:", sorted(all_paths))
    return 0


def _count(code: types.CodeType):
    """递归计数 code object。"""
    yield code
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            yield from _count(c)


if __name__ == "__main__":
    raise SystemExit(main())
