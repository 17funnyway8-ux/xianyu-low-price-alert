#!/usr/bin/env bash
# ===========================================================
# 安装 LaunchAgent（可选开机自启 + 崩溃拉起）
# 对齐 macOS 适配设计文档 §3.6 / 任务 T04
#
# 用法（在 M4 Mac 上，项目根目录执行）：
#     bash scripts/install_launchagent.sh
# 或指定 .app 路径：
#     bash scripts/install_launchagent.sh /path/to/闲鱼低价提醒工具.app
# ===========================================================
set -euo pipefail

# ---------- 定位项目根（与 macos_build.sh 一致：脚本位于 scripts/ 时上级即项目根） ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 默认 APP_PATH 指向 <项目根>/dist/闲鱼低价提醒工具.app；显式传参则用传入路径
if [ -n "${1:-}" ]; then
    APP_PATH="${1}"
else
    APP_PATH="${PROJECT_ROOT}/dist/闲鱼低价提醒工具.app"
fi

if [ "$(uname -s)" != "Darwin" ]; then
    echo "错误：LaunchAgent 仅 macOS 可用。" >&2
    exit 1
fi
if [ ! -d "${APP_PATH}" ]; then
    echo "错误：找不到 .app：${APP_PATH}" >&2
    echo "请先运行 bash build/macos_build.sh 构建 .app。" >&2
    exit 1
fi

PLIST_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/com.xianyu-alert.gui.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.xianyu-alert.gui.plist"
HOME_DIR="${HOME}"
LOG_DIR="${HOME}/Library/Logs/xianyu-alert"

mkdir -p "${LOG_DIR}" "${HOME}/Library/LaunchAgents"

echo "==> 生成 plist：${PLIST_DST}"
sed -e "s|__APP_PATH__|${APP_PATH}|g" -e "s|__HOME__|${HOME_DIR}|g" "${PLIST_SRC}" > "${PLIST_DST}"

echo "==> launchctl bootstrap（加载）"
launchctl bootout "gui/$(id -u)/com.xianyu-alert.gui" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_DST}"

echo "==> launchctl kickstart（立即启动一次，验证可用）"
launchctl kickstart -k "gui/$(id -u)/com.xianyu-alert.gui" 2>/dev/null || true

echo ""
echo "LaunchAgent 已安装："
echo "    ${PLIST_DST}"
echo "下次登录将自动启动 .app；崩溃时自动拉起。"
echo "卸载：bash scripts/uninstall_launchagent.sh"
