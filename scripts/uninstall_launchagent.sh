#!/usr/bin/env bash
# ===========================================================
# 卸载 LaunchAgent（可选开机自启）
# 对齐 macOS 适配设计文档 §3.6 / 任务 T04
#
# 用法（在 M4 Mac 上执行）：
#     bash scripts/uninstall_launchagent.sh
# ===========================================================
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
    echo "错误：LaunchAgent 仅 macOS 可用。" >&2
    exit 1
fi

PLIST_DST="${HOME}/Library/LaunchAgents/com.xianyu-alert.gui.plist"
LABEL="com.xianyu-alert.gui"

echo "==> launchctl bootout（卸载）"
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true

if [ -f "${PLIST_DST}" ]; then
    rm -f "${PLIST_DST}"
    echo "已删除：${PLIST_DST}"
else
    echo "plist 不存在（可能已卸载）：${PLIST_DST}"
fi
echo "LaunchAgent 已卸载。"
