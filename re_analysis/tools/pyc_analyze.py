# -*- coding: utf-8 -*-
"""pyc 反汇编与结构化提取器（Python 3.13 原生 marshal）。

用法:
    python pyc_analyze.py <pyc路径> <反汇编输出txt> <结构化输出md>

依赖当前解释器版本与目标 pyc 版本一致（均为 CPython 3.13），
因此可直接 marshal.loads 出 code object。
"""
from __future__ import annotations

import dis
import importlib.util
import io
import marshal
import os
import sys
import types
from typing import Any, Dict, List, Optional, Set, Tuple

MAGIC: bytes = importlib.util.MAGIC_NUMBER
HEADER_LEN: int = 16


def load_code(path: str) -> types.CodeType:
    """从 pyc 文件读出顶层 code object。

    自动处理 PyInstaller 剥离/保留头部两种情况。

    Args:
        path: pyc 文件路径。

    Returns:
        顶层 code object。

    Raises:
        ValueError: 无法解析为 code object。
    """
    with open(path, "rb") as f:
        raw: bytes = f.read()

    # 情况 A: 标准 16 字节头
    if raw[:4] == MAGIC:
        try:
            obj = marshal.loads(raw[HEADER_LEN:])
            if isinstance(obj, types.CodeType):
                return obj
        except Exception:  # noqa: BLE001
            pass

    # 情况 B: 头被剥离，直接就是 marshal 数据
    try:
        obj = marshal.loads(raw)
        if isinstance(obj, types.CodeType):
            return obj
    except Exception:  # noqa: BLE001
        pass

    # 情况 C: 尝试跳过 8 / 12 / 16 字节
    for skip in (8, 12, 16):
        try:
            obj = marshal.loads(raw[skip:])
            if isinstance(obj, types.CodeType):
                return obj
        except Exception:  # noqa: BLE001
            continue

    raise ValueError(f"无法从 {path} 解析出 code object")


def walk_codes(code: types.CodeType, prefix: str = "") -> List[Tuple[str, types.CodeType]]:
    """递归遍历所有嵌套 code object。

    Args:
        code: 根 code object。
        prefix: 名称前缀。

    Returns:
        [(限定名, code object)] 列表，深度优先。
    """
    qual: str = f"{prefix}.{code.co_name}" if prefix else code.co_name
    out: List[Tuple[str, types.CodeType]] = [(qual, code)]
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            out.extend(walk_codes(const, qual))
    return out


def fmt_const(value: Any, maxlen: int = 400) -> str:
    """格式化常量为单行可读文本。"""
    if isinstance(value, types.CodeType):
        return f"<code {value.co_name}>"
    try:
        s = repr(value)
    except Exception:  # noqa: BLE001
        s = f"<unrepr {type(value).__name__}>"
    s = s.replace("\n", "\\n").replace("\r", "\\r")
    if len(s) > maxlen:
        s = s[:maxlen] + f"... (截断, 共{len(s)}字符)"
    return s


def signature_of(code: types.CodeType) -> str:
    """根据 code object 还原函数签名（不含默认值）。"""
    n_pos_only: int = code.co_posonlyargcount
    n_args: int = code.co_argcount
    n_kwonly: int = code.co_kwonlyargcount
    names: List[str] = list(code.co_varnames)

    parts: List[str] = []
    idx: int = 0
    for i in range(n_args):
        parts.append(names[i])
        if i + 1 == n_pos_only:
            parts.append("/")
    idx = n_args

    flags = code.co_flags
    has_varargs = bool(flags & 0x04)
    has_varkw = bool(flags & 0x08)

    if has_varargs:
        parts.append("*" + names[idx + n_kwonly])
    elif n_kwonly:
        parts.append("*")

    for i in range(n_kwonly):
        parts.append(names[n_args + i])

    if has_varkw:
        kw_idx = n_args + n_kwonly + (1 if has_varargs else 0)
        parts.append("**" + names[kw_idx])

    return f"{code.co_name}({', '.join(parts)})"


def collect_strings(code: types.CodeType, acc: Optional[Set[str]] = None) -> Set[str]:
    """递归收集所有字符串常量。"""
    if acc is None:
        acc = set()
    for c in code.co_consts:
        if isinstance(c, str):
            acc.add(c)
        elif isinstance(c, types.CodeType):
            collect_strings(c, acc)
        elif isinstance(c, (tuple, frozenset)):
            for item in c:
                if isinstance(item, str):
                    acc.add(item)
    return acc


def analyze(pyc_path: str, disasm_out: str, struct_out: str) -> None:
    """执行完整分析并落盘。"""
    root: types.CodeType = load_code(pyc_path)
    all_codes: List[Tuple[str, types.CodeType]] = walk_codes(root)

    # ---- 1. 完整反汇编 ----
    buf = io.StringIO()
    buf.write(f"# 反汇编: {os.path.basename(pyc_path)}\n")
    buf.write(f"# 顶层 code: {root.co_name}  filename={root.co_filename!r}\n")
    buf.write(f"# 嵌套 code object 总数: {len(all_codes)}\n")
    buf.write("=" * 100 + "\n\n")
    for qual, c in all_codes:
        buf.write("\n" + "=" * 100 + "\n")
        buf.write(f"CODE OBJECT: {qual}\n")
        buf.write(f"  签名      : {signature_of(c)}\n")
        buf.write(f"  firstlineno: {c.co_firstlineno}   flags: 0x{c.co_flags:X}\n")
        buf.write(f"  co_varnames: {c.co_varnames}\n")
        buf.write(f"  co_names   : {c.co_names}\n")
        buf.write(f"  co_freevars: {c.co_freevars}  co_cellvars: {c.co_cellvars}\n")
        buf.write("-" * 100 + "\n")
        try:
            dis.dis(c, file=buf, depth=0)
        except Exception as exc:  # noqa: BLE001
            buf.write(f"[反汇编失败] {exc}\n")
    os.makedirs(os.path.dirname(disasm_out) or ".", exist_ok=True)
    with open(disasm_out, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())

    # ---- 2. 结构化摘要 ----
    lines: List[str] = []
    add = lines.append
    base: str = os.path.basename(pyc_path)
    add(f"# 结构化提取: {base}")
    add("")
    add(f"- 源文件名 (co_filename)：`{root.co_filename}`")
    add(f"- 嵌套 code object 总数：{len(all_codes)}")
    add(f"- 模块级 co_names 数量：{len(root.co_names)}")
    add("")

    add("## 模块级 co_names（全局引用名）")
    add("")
    add("```")
    for i in range(0, len(root.co_names), 6):
        add("  " + ", ".join(root.co_names[i:i + 6]))
    add("```")
    add("")

    add("## 全部 code object 清单（签名 / 行号）")
    add("")
    add("| 限定名 | 签名 | 首行号 | 局部变量数 |")
    add("| --- | --- | --- | --- |")
    for qual, c in all_codes:
        add(f"| `{qual}` | `{signature_of(c)}` | {c.co_firstlineno} | {len(c.co_varnames)} |")
    add("")

    add("## 各 code object 的常量表（co_consts）")
    add("")
    for qual, c in all_codes:
        interesting = [x for x in c.co_consts if not isinstance(x, types.CodeType)]
        if not interesting or (len(interesting) == 1 and interesting[0] is None):
            continue
        add(f"### `{qual}`")
        add("")
        add("```python")
        for const in interesting:
            add(f"{fmt_const(const)}")
        add("```")
        add("")

    os.makedirs(os.path.dirname(struct_out) or ".", exist_ok=True)
    with open(struct_out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[ok] {base}: {len(all_codes)} code objects -> {disasm_out}, {struct_out}")


def main() -> int:
    analyze(sys.argv[1], sys.argv[2], sys.argv[3])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
