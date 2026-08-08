#!/usr/bin/env python3
"""生成 macOS AppIcon.iconset（ico → 各尺寸 PNG）。

对齐 macOS 适配设计文档 §3.5 / 任务 T04：
    - 在任意平台（含 Windows 开发机）用 Pillow 读取 icon.ico 的第 1 帧，
      缩放生成 iconset 所需的全部尺寸 PNG（16/32/128/256/512 + @2x）；
    - 输出目录结构符合 `iconutil -c icns` 要求：
        AppIcon_16x16.png / AppIcon_16x16@2x.png / AppIcon_32x32.png /
        AppIcon_32x32@2x.png / AppIcon_128x128.png / AppIcon_128x128@2x.png /
        AppIcon_256x256.png / AppIcon_256x256@2x.png / AppIcon_512x512.png /
        AppIcon_512x512@2x.png
    - macOS 上由 `build/make_icns.sh` 调用 `iconutil -c icns` 合成最终 icns；
    - 也可用 `--icns-output build/icon.icns` 让 Pillow 直接合成 icns
      （跨平台，Windows 开发机即可产出交付物，Bug #1）。

用法：
    python build/make_icns.py --input icon.ico --output-dir build/iconset
    python build/make_icns.py --input icon.ico --output-dir build/iconset --icns-output build/icon.icns
依赖：Pillow（`pip install pillow`；macos_build.sh 会在 venv 中安装）
"""

from __future__ import annotations

import argparse
import os
from typing import List, Tuple

#: iconset 各尺寸（尺寸, 是否 @2x）
ICONSET_SIZES: List[Tuple[int, bool]] = [
    (16, False),
    (16, True),
    (32, False),
    (32, True),
    (128, False),
    (128, True),
    (256, False),
    (256, True),
    (512, False),
    (512, True),
]


def _output_name(size: int, retina: bool) -> str:
    """按 iconutil 命名规范生成 PNG 文件名。"""
    suffix = "@2x" if retina else ""
    return f"AppIcon_{size}x{size}{suffix}.png"


def make_iconset(input_ico: str, output_dir: str) -> List[str]:
    """生成 iconset 全部 PNG。

    Args:
        input_ico: 源 icon.ico 路径。
        output_dir: 输出目录（自动创建）。

    Returns:
        生成的 PNG 文件路径列表。

    Raises:
        FileNotFoundError: 源图标不存在。
        ImportError: Pillow 未安装。
    """
    if not os.path.isfile(input_ico):
        raise FileNotFoundError(f"找不到源图标：{input_ico}")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 依赖提示
        raise ImportError(
            "需要 Pillow：`pip install pillow`（macos_build.sh 已自动安装）"
        ) from exc

    os.makedirs(output_dir, exist_ok=True)
    # 打开 ico 第 1 帧（通常是最大尺寸），转 RGBA
    source = Image.open(input_ico).convert("RGBA")

    written: List[str] = []
    for size, retina in ICONSET_SIZES:
        # @2x 的实际像素 = size * 2，逻辑尺寸仍标注为 size
        pixels = size * (2 if retina else 1)
        resized = source.resize((pixels, pixels), Image.LANCZOS)
        path = os.path.join(output_dir, _output_name(size, retina))
        resized.save(path, "PNG")
        written.append(path)
    return written


def make_icns(input_ico: str, output_path: str) -> str:
    """直接用 Pillow 合成多尺寸 .icns（跨平台，无需 macOS iconutil）。

    在 Windows 开发机上也能产出 `build/icon.icns` 随仓库交付，使
    `macos_build.sh` 的 `if [ ! -f "${ICNS}" ]` 守卫直接跳过生成步骤
    （Bug #1 修复：干净 M4 构建不再强依赖图标生成链路）。

    Args:
        input_ico: 源 icon.ico 路径。
        output_path: 输出 .icns 路径（父目录自动创建）。

    Returns:
        输出 .icns 路径。

    Raises:
        FileNotFoundError: 源图标不存在。
        ImportError: Pillow 未安装。
    """
    if not os.path.isfile(input_ico):
        raise FileNotFoundError(f"找不到源图标：{input_ico}")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 依赖提示
        raise ImportError(
            "需要 Pillow：`pip install pillow`（macos_build.sh 已自动安装）"
        ) from exc

    source = Image.open(input_ico).convert("RGBA")
    frames = []
    sizes = []
    for size, retina in ICONSET_SIZES:
        pixels = size * (2 if retina else 1)
        resized = source.resize((pixels, pixels), Image.LANCZOS)
        frames.append(resized)
        # @2x 的 icns 条目逻辑尺寸仍为 size（像素 2 倍），由 ICNS 插件按 retina 编码
        sizes.append((size, size))

    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    frames[0].save(
        output_path,
        format="ICNS",
        append_images=frames[1:],
        sizes=sizes,
    )
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="ico → AppIcon.iconset PNG / icns")
    parser.add_argument("--input", required=True, help="源 icon.ico 路径")
    parser.add_argument("--output-dir", default="build/iconset", help="iconset 输出目录")
    parser.add_argument(
        "--icns-output",
        default="",
        help="可选：直接用 Pillow 合成 .icns（跨平台，无需 iconutil），如 build/icon.icns",
    )
    args = parser.parse_args()
    files = make_iconset(args.input, args.output_dir)
    print(f"已生成 {len(files)} 个 PNG 到 {args.output_dir}")
    if args.icns_output:
        icns_path = make_icns(args.input, args.icns_output)
        print(f"已生成 icns：{icns_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
