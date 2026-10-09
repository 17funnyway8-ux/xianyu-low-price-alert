#!/usr/bin/env python3
"""文档一致性校验（CI 用）。

做四件事（全部机械比对，不靠人眼）：

  1. 相对链接：Markdown 的 [文本](路径) 目标必须存在（跳过 http(s)/mailto/纯锚点）
  2. 仓库路径提及：正文行内代码里出现的仓库路径必须真实存在（以 git 跟踪文件为准）
  3. 版本一致性：README / README_EN / docs 里的镜像标签必须与 xianyu_alert.__version__ 一致
  4. 文档索引：docs/README.md 必须登记 docs/ 下的全部文档

为什么需要它：文档不会"报错"，只会悄悄过期 —— 例如脚本被移动、镜像标签停在旧版本、
新增文档忘了进索引。这类问题靠人眼审阅必然漏，且会让使用者照做过时的步骤。

用法：
    python3 scripts/check_docs.py          # 人类可读报告
    python3 scripts/check_docs.py --json   # 机器可读
退出码：0 = 无问题；1 = 存在问题
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import subprocess
import sys

BT = chr(96)  # 行内代码的反引号（写成常量，避免源码里出现裸反引号）


def _force_utf8_output() -> None:
    """把 stdout / stderr 切到 UTF-8。

    v1.10.11：Windows 控制台默认 cp1252（中文系统为 gbk），本脚本会打印中文报告，
    直接运行会在 print 阶段抛 UnicodeEncodeError 而**整个检查器崩掉** ——
    CI 的文档门禁跑在 ubuntu 上（UTF-8），所以这个坑一直没暴露，
    直到本轮为脚本补自测、在 Windows runner 上真的跑了一遍。
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):  # 老版本 / 非文本流：忽略即可
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 只检查这些顶层目录下的路径提及，避免把示例文本误判为路径
PATH_ROOTS = ("xianyu_alert/", "web/", "scripts/", "tests/", "docs/", ".github/")
#: 允许的"故意不存在"占位写法
PATH_SKIP_TOKENS = ("<", ">", "*", "...", "$", "xxx", "XXX", "path/to")


def tracked_files() -> set[str]:
    """返回 git 跟踪的文件路径集合（含文档与代码）。"""
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def tracked_dirs(files: set[str]) -> set[str]:
    """从跟踪文件推导出的目录集合。"""
    dirs: set[str] = set()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return dirs


def md_files(files: set[str]) -> list[str]:
    """仓库内受检查的 Markdown 文件（排除第三方与归档示例）。"""
    return sorted(f for f in files if f.endswith(".md"))


def check_links(files: set[str]) -> list[dict]:
    """检查 Markdown 相对链接目标是否存在。"""
    problems = []
    link_re = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
    for path in md_files(files):
        text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        for lineno, line in enumerate(text.splitlines(), 1):
            for _label, target in link_re.findall(line):
                t = target.strip().split("#", 1)[0].strip()
                if not t or t.startswith(("http://", "https://", "mailto:", "tel:")):
                    continue
                resolved = os.path.normpath(os.path.join(os.path.dirname(path), t))
                if resolved.startswith(".."):
                    problems.append({"file": path, "line": lineno, "kind": "链接越界", "detail": target})
                elif resolved not in files and not os.path.exists(os.path.join(ROOT, resolved)):
                    problems.append({"file": path, "line": lineno, "kind": "链接失效", "detail": target})
    return problems


def check_paths(files: set[str], dirs: set[str]) -> list[dict]:
    """检查正文行内代码里的仓库路径是否真实存在。"""
    problems = []
    span_re = re.compile(BT + "([^" + BT + "]+)" + BT)
    for path in md_files(files):
        text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        for lineno, line in enumerate(text.splitlines(), 1):
            for span in span_re.findall(line):
                for candidate in span.split():
                    cand = candidate.strip("，。、（）()[];:,")
                    if not cand.startswith(PATH_ROOTS):
                        continue
                    if any(tok in cand for tok in PATH_SKIP_TOKENS):
                        continue
                    cand = cand.rstrip("/")
                    if cand in files or cand in dirs:
                        continue
                    problems.append({"file": path, "line": lineno, "kind": "路径不存在", "detail": cand})
    return problems


def check_versions(files: set[str]) -> list[dict]:
    """检查文档中的镜像标签与代码版本是否一致。"""
    problems = []
    init = open(os.path.join(ROOT, "xianyu_alert/__init__.py"), encoding="utf-8").read()
    m = re.search(r'__version__\s*=\s*"([0-9][0-9.]*)"', init)
    if not m:
        return [{"file": "xianyu_alert/__init__.py", "line": 0, "kind": "版本缺失", "detail": "__version__ 未找到"}]
    version = m.group(1)
    tag_re = re.compile(r"xianyu-alert:([0-9][0-9.]*)")
    docs = [f for f in md_files(files) if f in ("README.md", "README_EN.md") or f.startswith("docs/")]
    for path in docs:
        text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        for lineno, line in enumerate(text.splitlines(), 1):
            for found in tag_re.findall(line):
                if found != version:
                    problems.append({
                        "file": path, "line": lineno, "kind": "镜像标签过期",
                        "detail": f"文档写 {found}，代码是 {version}",
                    })
    return problems


def check_index(files: set[str]) -> list[dict]:
    """检查 docs/README.md 是否登记了 docs/ 下全部文档。"""
    problems = []
    index_path = "docs/README.md"
    if index_path not in files:
        return [{"file": index_path, "line": 0, "kind": "索引缺失", "detail": "docs/README.md 不存在"}]
    index = open(os.path.join(ROOT, index_path), encoding="utf-8").read()
    # 索引允许用通配表达"这一类文档"（例如 *.mermaid 代表全部架构图），
    # 因此显式路径与通配模式都要认，否则会把刻意的归类写法误报成缺项。
    globs = re.findall(r"[A-Za-z0-9_./*-]*\*[A-Za-z0-9_./*-]*", index)
    for f in sorted(files):
        if not f.startswith("docs/") or f == index_path:
            continue
        if not f.endswith((".md", ".mermaid")):
            continue
        if f in index:
            continue
        rel = f[len("docs/"):]
        base = os.path.basename(f)
        if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(base, g) for g in globs):
            continue
        problems.append({"file": index_path, "line": 0, "kind": "索引缺项", "detail": f})
    return problems


def main() -> int:
    """入口：跑四项检查并输出报告。"""
    _force_utf8_output()
    files = tracked_files()
    dirs = tracked_dirs(files)
    problems: list[dict] = []
    groups = {
        "相对链接": check_links(files),
        "仓库路径": check_paths(files, dirs),
        "版本一致": check_versions(files),
        "文档索引": check_index(files),
    }
    for items in groups.values():
        problems.extend(items)

    if "--json" in sys.argv:
        print(json.dumps({"total": len(problems), "groups": groups}, ensure_ascii=False, indent=2))
        return 1 if problems else 0

    print("=" * 66)
    print(" 文档一致性校验")
    print("=" * 66)
    for name, items in groups.items():
        mark = "✅" if not items else "❌"
        print(f"{mark} {name}: {len(items)} 项问题")
        for it in items[:12]:
            loc = f"{it['file']}:{it['line']}" if it["line"] else it["file"]
            print(f"     - [{it['kind']}] {loc} -> {it['detail']}")
        if len(items) > 12:
            print(f"     ... 另有 {len(items) - 12} 项")
    print("-" * 66)
    print(f"合计 {len(problems)} 项问题；退出码 {'1' if problems else '0'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
