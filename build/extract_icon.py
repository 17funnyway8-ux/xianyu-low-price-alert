# -*- coding: utf-8 -*-
"""从原 exe 提取 RT_ICON 生成多尺寸 icon.ico（可复现脚本）。

用法：python build/extract_icon.py <原exe路径> [输出ico路径]
默认输出：项目根 icon.ico

说明：原 exe 内嵌 7 个 32bpp 图标（16/24/32/48/64/128/256），
按 Windows ICO 格式（ICONDIR + ICONDIRENTRY + 图像数据）重新封装，
PyInstaller 的 icon= 参数可直接使用。
"""

from __future__ import annotations

import os
import struct
import sys

import pefile

#: 资源类型
RT_ICON = 3
RT_GROUP_ICON = 14


def extract_icons(exe_path: str) -> list[tuple[int, int, int, int, bytes]]:
    """从 exe 提取首个图标组内全部图标。

    Returns:
        [(width, height, bpp, size, data), ...]
    """
    pe = pefile.PE(exe_path, fast_load=True)
    pe.parse_data_directories()
    mm = pe.get_memory_mapped_image()

    group_raw: bytes | None = None
    icon_map: dict[int, bytes] = {}
    for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if entry.id == RT_GROUP_ICON and group_raw is None:
            for sub in entry.directory.entries:
                rde = sub.directory.entries[0]
                offset = rde.data.struct.OffsetToData
                size = rde.data.struct.Size
                group_raw = mm[offset:offset + size]
        if entry.id == RT_ICON:
            for sub in entry.directory.entries:
                rde = sub.directory.entries[0]
                offset = rde.data.struct.OffsetToData
                size = rde.data.struct.Size
                icon_map[sub.id] = mm[offset:offset + size]

    if group_raw is None:
        raise RuntimeError("未找到 RT_GROUP_ICON 资源")

    _reserved, _typ, count = struct.unpack_from("<HHH", group_raw, 0)
    result: list[tuple[int, int, int, int, bytes]] = []
    for index in range(count):
        w, h, _colors, _res, _planes, bpp, size, gid = struct.unpack_from(
            "<BBBBHHIH", group_raw, 6 + 14 * index
        )
        data = icon_map.get(gid)
        if data is None or len(data) != size:
            continue
        result.append((w or 256, h or 256, bpp, size, data))
    return result


def write_ico(icons: list[tuple[int, int, int, int, bytes]], out_path: str) -> int:
    """把图标列表封装为 .ico 文件。返回写入字节数。"""
    entries: list[bytes] = []
    images: list[bytes] = []
    offset = 6 + 16 * len(icons)
    for w, h, bpp, size, data in icons:
        wb = 0 if w >= 256 else w
        hb = 0 if h >= 256 else h
        entries.append(struct.pack("<BBBBHHII", wb, hb, 0, 0, 1, bpp, size, offset))
        images.append(data)
        offset += size

    with open(out_path, "wb") as fp:
        fp.write(struct.pack("<HHH", 0, 1, len(icons)))
        for entry in entries:
            fp.write(entry)
        for image in images:
            fp.write(image)
    return offset


def main() -> int:
    """命令行入口。"""
    if len(sys.argv) < 2:
        print("用法：python build/extract_icon.py <原exe路径> [输出ico路径]")
        return 1
    exe_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) >= 3 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.ico"
    )

    icons = extract_icons(exe_path)
    if not icons:
        print("未提取到任何图标。")
        return 1
    total = write_ico(icons, out_path)
    print(f"已生成 {out_path}（{total} 字节，{len(icons)} 张图标）")
    for w, h, bpp, size, _data in icons:
        print(f"  - {w}x{h} bpp={bpp} size={size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
