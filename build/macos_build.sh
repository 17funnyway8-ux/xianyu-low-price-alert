#!/usr/bin/env bash
# ===========================================================
# macOS M4 构建脚本：venv + 依赖 + PyInstaller .app + ad-hoc 签名 + 自检
# 对齐 macOS 适配设计文档 §3.5 / 任务 T04
#
# 用法（在 M4 Mac 上，项目根目录执行）：
#     bash build/macos_build.sh
# 或：
#     bash build/macos_build.sh /path/to/python3        # 指定 python3
#
# 产物：
#     dist/闲鱼低价提醒工具.app
# 数据目录（双击运行后自动创建）：
#     ~/Library/Application Support/闲鱼低价提醒工具/
# ===========================================================
set -euo pipefail

# ---------- 定位项目根 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

echo "==> 项目根：${PROJECT_ROOT}"

# ---------- Python 解释器 ----------
PYTHON="${1:-python3}"
if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "错误：找不到 python3，请安装 Python 3.13（或传入解释器路径作为第一个参数）。" >&2
    exit 1
fi
PY_MAJOR=$("${PYTHON}" -c 'import sys; print(sys.version_info[0])')
PY_MINOR=$("${PYTHON}" -c 'import sys; print(sys.version_info[1])')
echo "==> 使用 Python ${PY_MAJOR}.${PY_MINOR}：$("${PYTHON}" -c 'import sys; print(sys.executable)')"

# ---------- 创建 venv（build/venv-macos，可复用） ----------
VENV_DIR="${PROJECT_ROOT}/build/venv-macos"
if [ ! -x "${VENV_DIR}/bin/python" ]; then
    echo "==> 创建虚拟环境：${VENV_DIR}"
    "${PYTHON}" -m venv "${VENV_DIR}"
fi
VENV_PY="${VENV_DIR}/bin/python"
"${VENV_PY}" -m pip install --upgrade pip

# ---------- 安装依赖 ----------
echo "==> 安装运行依赖（cryptography + PySide6 + 既有依赖）"
"${VENV_PY}" -m pip install -r requirements.txt
"${VENV_PY}" -m pip install -r requirements-macos.txt
echo "==> 安装构建依赖（pyinstaller）"
"${VENV_PY}" -m pip install -r requirements-build.txt

# ---------- 生成图标 icns（若缺失） ----------
# Bug #1：make_icns.py 依赖 Pillow，必须先安装（requirements-build.txt 已含
# pillow>=10.0；这里显式再装一次，保证干净 M4 上即使跳过 requirements 也能生成）。
echo "==> 安装图标生成依赖（Pillow）"
"${VENV_PY}" -m pip install "pillow>=10.0"

ICNS="${PROJECT_ROOT}/build/icon.icns"
if [ ! -f "${ICNS}" ]; then
    echo "==> 生成 icon.icns（icon.ico → iconset → iconutil）"
    "${VENV_PY}" "${PROJECT_ROOT}/build/make_icns.py" --input "${PROJECT_ROOT}/icon.ico" \
        --output-dir "${PROJECT_ROOT}/build/iconset"
    bash "${PROJECT_ROOT}/build/make_icns.sh" "${PROJECT_ROOT}/build/iconset" "${ICNS}"
fi

# ---------- PyInstaller 构建 .app ----------
echo "==> PyInstaller 构建 .app（arm64 / windowed）"
"${VENV_PY}" -m PyInstaller "build/macos_闲鱼低价提醒工具.spec" --noconfirm --clean

APP_PATH="${PROJECT_ROOT}/dist/闲鱼低价提醒工具.app"
if [ ! -d "${APP_PATH}" ]; then
    echo "错误：未生成 ${APP_PATH}" >&2
    exit 1
fi

# ---------- ad-hoc 签名（自用无需开发者证书） ----------
echo "==> ad-hoc 签名（codesign --force --deep --sign -）"
codesign --force --deep --sign - "${APP_PATH}"

# ---------- 自检 ----------
echo "==> 自检"
codesign --verify --deep --verbose=2 "${APP_PATH}" || {
    echo "警告：codesign --verify 未通过，请检查签名。" >&2
}
echo "==> 产物架构"
file "${APP_PATH}/Contents/MacOS/闲鱼低价提醒工具" || true
echo "==> Info.plist 关键项"
/usr/libexec/PlistBuddy -c "Print :NSAppSleepDisabled" "${APP_PATH}/Contents/Info.plist" 2>/dev/null \
    || echo "（NSAppSleepDisabled 未找到，请检查 spec 的 info_plist）"

echo ""
echo "构建完成：${APP_PATH}"
echo "首次双击运行后，数据将写入："
echo "    ~/Library/Application Support/闲鱼低价提醒工具/"
echo "（config.yaml / state/ / secret.key）"
echo "如需开机自启：bash scripts/install_launchagent.sh"
