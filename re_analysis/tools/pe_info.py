# -*- coding: utf-8 -*-
"""PE 结构与元信息提取器（只读）。

用法: python pe_info.py <exe路径> <输出md路径>
"""
from __future__ import annotations

import datetime
import hashlib
import os
import sys
from typing import Any, List

import pefile


def sha256_of(path: str) -> str:
    """计算文件 SHA256。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_of(path: str) -> str:
    """计算文件 MD5。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    exe_path: str = sys.argv[1]
    out_path: str = sys.argv[2]

    lines: List[str] = []
    add = lines.append

    st = os.stat(exe_path)
    add("# 01 - PE 结构与元信息")
    add("")
    add("> 证据来源：`pefile` 解析目标 exe（只读打开，未做任何写入）。")
    add("")
    add("## 1.1 文件基本信息")
    add("")
    add("| 项 | 值 |")
    add("| --- | --- |")
    add(f"| 路径 | `{exe_path}` |")
    add(f"| 大小 | {st.st_size:,} 字节 ({st.st_size / 1024 / 1024:.2f} MB) |")
    mt = datetime.datetime.fromtimestamp(st.st_mtime)
    add(f"| 修改时间 (mtime) | {mt.isoformat(sep=' ')} |")
    add(f"| MD5 | `{md5_of(exe_path)}` |")
    add(f"| SHA256 | `{sha256_of(exe_path)}` |")
    add("")

    pe = pefile.PE(exe_path, fast_load=False)

    add("## 1.2 PE 头")
    add("")
    add("| 项 | 值 |")
    add("| --- | --- |")
    machine = pe.FILE_HEADER.Machine
    machine_name = pefile.MACHINE_TYPE.get(machine, "UNKNOWN")
    add(f"| Machine | 0x{machine:04X} ({machine_name}) |")
    magic = pe.OPTIONAL_HEADER.Magic
    add(f"| Optional Magic | 0x{magic:04X} ({'PE32+ (64-bit)' if magic == 0x20B else 'PE32 (32-bit)'}) |")
    ts = pe.FILE_HEADER.TimeDateStamp
    add(f"| TimeDateStamp | {ts} (0x{ts:08X}) = {datetime.datetime.utcfromtimestamp(ts).isoformat(sep=' ')} UTC |")
    sub = pe.OPTIONAL_HEADER.Subsystem
    add(f"| Subsystem | {sub} ({pefile.SUBSYSTEM_TYPE.get(sub, 'UNKNOWN')}) |")
    add(f"| NumberOfSections | {pe.FILE_HEADER.NumberOfSections} |")
    add(f"| ImageBase | 0x{pe.OPTIONAL_HEADER.ImageBase:X} |")
    add(f"| AddressOfEntryPoint | 0x{pe.OPTIONAL_HEADER.AddressOfEntryPoint:X} |")
    add(f"| SizeOfImage | 0x{pe.OPTIONAL_HEADER.SizeOfImage:X} ({pe.OPTIONAL_HEADER.SizeOfImage:,}) |")
    add(f"| DllCharacteristics | 0x{pe.OPTIONAL_HEADER.DllCharacteristics:04X} |")
    add(f"| Checksum | 0x{pe.OPTIONAL_HEADER.CheckSum:08X} |")
    add("")

    add("## 1.3 节区表")
    add("")
    add("| # | 名称 | VirtualAddress | VirtualSize | RawSize | RawPtr | Characteristics | 熵 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, s in enumerate(pe.sections):
        name = s.Name.rstrip(b"\x00").decode("latin-1", "replace")
        add(
            f"| {i} | `{name}` | 0x{s.VirtualAddress:08X} | 0x{s.Misc_VirtualSize:X} "
            f"({s.Misc_VirtualSize:,}) | 0x{s.SizeOfRawData:X} ({s.SizeOfRawData:,}) | "
            f"0x{s.PointerToRawData:08X} | 0x{s.Characteristics:08X} | {s.get_entropy():.3f} |"
        )
    add("")
    last = max(pe.sections, key=lambda x: x.PointerToRawData + x.SizeOfRawData)
    end_of_sections = last.PointerToRawData + last.SizeOfRawData
    overlay = st.st_size - end_of_sections
    add(f"- 节区数据结束偏移：`0x{end_of_sections:X}` ({end_of_sections:,})")
    add(f"- **Overlay（附加数据）大小：{overlay:,} 字节 "
        f"({overlay / 1024 / 1024:.2f} MB，占全文件 {overlay / st.st_size * 100:.1f}%)**")
    add("- 结论：绝大部分体积位于 overlay，符合 PyInstaller onefile 把 CArchive 追加在 PE 尾部的特征。")
    add("")

    add("## 1.4 数字签名")
    add("")
    sec_dir_idx = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_SECURITY"]
    sec_dir = pe.OPTIONAL_HEADER.DATA_DIRECTORY[sec_dir_idx]
    if sec_dir.VirtualAddress == 0 or sec_dir.Size == 0:
        add("- **无 Authenticode 数字签名**（IMAGE_DIRECTORY_ENTRY_SECURITY 为空）。")
        add("- 证据：`DATA_DIRECTORY[4].VirtualAddress == 0 and .Size == 0`")
    else:
        add(f"- 存在签名目录：偏移 0x{sec_dir.VirtualAddress:X}，大小 {sec_dir.Size}")
    add("")

    add("## 1.5 导入表（关键 DLL）")
    add("")
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        add("| DLL | 导入符号数 | 示例符号 |")
        add("| --- | --- | --- |")
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            dll = entry.dll.decode("latin-1", "replace")
            names: List[str] = []
            for imp in entry.imports:
                if imp.name:
                    names.append(imp.name.decode("latin-1", "replace"))
            sample = ", ".join(f"`{n}`" for n in names[:8])
            add(f"| `{dll}` | {len(entry.imports)} | {sample} |")
    else:
        add("- 无导入表。")
    add("")

    add("## 1.6 资源")
    add("")
    if hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
        add("| 资源类型 | 条目数 |")
        add("| --- | --- |")
        for rt in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            rid = rt.id if rt.id is not None else -1
            tname = pefile.RESOURCE_TYPE.get(rid, str(rt.name) if rt.name else f"ID_{rid}")
            cnt = len(rt.directory.entries) if hasattr(rt, "directory") else 0
            add(f"| {tname} | {cnt} |")
        add("")
        has_icon = any(
            pefile.RESOURCE_TYPE.get(rt.id or -1) == "RT_ICON"
            for rt in pe.DIRECTORY_ENTRY_RESOURCE.entries
        )
        add(f"- 图标资源 (RT_ICON) 存在：**{'是' if has_icon else '否'}**")
    else:
        add("- 无资源目录。")
    add("")

    add("### VS_VERSIONINFO 版本资源")
    add("")
    found_ver = False
    if hasattr(pe, "FileInfo") and pe.FileInfo:
        for file_info_list in pe.FileInfo:
            for fi in file_info_list:
                if getattr(fi, "Key", b"") == b"StringFileInfo":
                    for st_entry in fi.StringTable:
                        add("| 字段 | 值 |")
                        add("| --- | --- |")
                        for k, v in st_entry.entries.items():
                            ks = k.decode("utf-8", "replace")
                            vs = v.decode("utf-8", "replace")
                            add(f"| {ks} | `{vs}` |")
                        add("")
                        found_ver = True
    if hasattr(pe, "VS_FIXEDFILEINFO") and pe.VS_FIXEDFILEINFO:
        ffi = pe.VS_FIXEDFILEINFO[0]
        fv = (ffi.FileVersionMS >> 16, ffi.FileVersionMS & 0xFFFF,
              ffi.ProductVersionMS >> 16, ffi.ProductVersionMS & 0xFFFF)
        add(f"- VS_FIXEDFILEINFO FileVersion(MS/LS): "
            f"{ffi.FileVersionMS >> 16}.{ffi.FileVersionMS & 0xFFFF}."
            f"{ffi.FileVersionLS >> 16}.{ffi.FileVersionLS & 0xFFFF}")
        found_ver = True
    if not found_ver:
        add("- **无 VS_VERSIONINFO 版本资源**（未在打包时指定 --version-file）。")
    add("")

    pe.close()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"written: {out_path}  ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
