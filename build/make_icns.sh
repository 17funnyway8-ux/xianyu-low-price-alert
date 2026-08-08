#!/usr/bin/env bash
# ===========================================================
# iconset → icns（macOS 专用：iconutil 合成）
# 对齐 macOS 适配设计文档 §3.5 / 任务 T04
#
# 用法：
#     bash build/make_icns.sh build/iconset build/icon.icns
# 说明：
#     - 只支持 macOS（iconutil 是系统工具）；
#     - Windows 开发机上只需运行 make_icns.py 生成 iconset PNG，
#       最终 icns 合成在 M4 Mac 上执行（macos_build.sh 已自动串联）。
# ===========================================================
set -euo pipefail

ICONSET_DIR="${1:-build/iconset}"
OUTPUT_ICNS="${2:-build/icon.icns}"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "错误：iconutil 仅 macOS 可用。请在 M4 Mac 上执行本脚本。" >&2
    exit 1
fi

if [ ! -d "${ICONSET_DIR}" ] || [ -z "$(ls -A "${ICONSET_DIR}" 2>/dev/null)" ]; then
    echo "错误：iconset 目录为空或不存在：${ICONSET_DIR}" >&2
    echo "请先运行：python build/make_icns.py --input icon.ico --output-dir ${ICONSET_DIR}" >&2
    exit 1
fi

OUTPUT_DIR="$(cd "$(dirname "${OUTPUT_ICNS}")" && pwd)"
OUTPUT_NAME="$(basename "${OUTPUT_ICNS}")"

echo "==> iconutil -c icns（${ICONSET_DIR} → ${OUTPUT_ICNS}）"
iconutil -c icns "${ICONSET_DIR}" -o "${OUTPUT_DIR}/${OUTPUT_NAME}"

if [ -f "${OUTPUT_ICNS}" ]; then
    echo "完成：${OUTPUT_ICNS}"
else
    echo "错误：iconutil 未产出 ${OUTPUT_ICNS}" >&2
    exit 1
fi
