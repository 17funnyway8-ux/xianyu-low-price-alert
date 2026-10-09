#!/usr/bin/env bash
# ===========================================================
# 闲鱼低价提醒工具 —— P0 容器 POC 语义验证脚本
# ===========================================================
# 本机无 Docker 时，用 XY_DATA_DIR=<临时目录> 模拟「容器内 XY_DATA_DIR=/app/data」
# 的语义，验证以下 P0 链路：
#   1) `cli once --config config.poc.yaml`  mock 抓取成功（离线确定性数据）
#   2) secret.key 自动生成于 $XY_DATA_DIR（POSIX 0600）
#   3) SQLite 落盘到 $XY_DATA_DIR/state/xianyu_alert.db
#   4) `cli cookie status` 可运行（只检测不写入）
#   5) Fernet 加解密往返 OK（encrypt_text -> decrypt_text）
#   6) dpapi1: 老密文优雅降级（返回空串提示重登）
#
# 有 Docker 的主机可直接在容器内跑等价命令（见脚本末尾输出 / 交付说明）。
#
# ⚠️ 容器内全量测试的边界（非代码缺陷，请勿据此报障）：
#   python:3.13-slim 镜像只装业务核心（不装 GUI/Web），因此容器内跑
#   `unittest discover` 全量测试会有**预期失败**：
#     - GUI 测试（test_gui* / test_qa_macos_extra 等）：slim 无 tkinter
#       （libtk8.6.so 缺失），必然失败；
#     - test_paths：容器预设 XY_DATA_DIR=/app/data，导致「默认路径」断言失败。
#   这两类失败是「容器环境 vs 测试预期」的差异，不是代码缺陷。
#   容器内验证业务核心的正确方式是【核心测试子集】（198 个，容器内实测全过），
#   模块范围：tests.test_models tests.test_storage tests.test_notifier
#            tests.test_cookie tests.test_secure tests.test_monitor tests.test_filter
#   - 宿主机直接跑：python -m unittest tests.test_models …（同上模块列表）
#   - 容器内跑：tests/ 未打进镜像，需挂载 + 覆盖 entrypoint，
#     完整命令见本脚本末尾输出的 docker compose 版。
#   全量 836 测试的权威基线以宿主机/CI 为准。
#
# 用法：
#   bash scripts/verify_docker_p0.sh            # 使用随机临时目录
#   XY_POC_DIR=/tmp/xy-poc bash scripts/verify_docker_p0.sh   # 指定目录
#
# 退出码：0=全部通过；1=任一项失败。
# ===========================================================

set -euo pipefail

# ---- 0. 路径与解释器 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# 优先使用项目虚拟环境；无则回退系统 python3
if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="python3"
fi

echo "==> 项目根目录：${PROJECT_ROOT}"
echo "==> Python 解释器：${PYTHON}（$(${PYTHON} --version 2>&1)）"

# ---- 1. 临时数据目录（模拟容器内 /app/data）----
POC_DIR="${XY_POC_DIR:-$(mktemp -d /tmp/xy-poc.XXXXXX)}"
mkdir -p "${POC_DIR}"
echo "==> XY_DATA_DIR（模拟卷挂载目录）：${POC_DIR}"
export XY_DATA_DIR="${POC_DIR}"

PASS=0
FAIL=0

pass() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }

check_exit() {
    # $1=期望退出码 $2=描述
    local expected="$1" desc="$2" actual="$3"
    if [[ "${actual}" -eq "${expected}" ]]; then
        pass "${desc}"
    else
        fail "${desc}（期望退出码 ${expected}，实际 ${actual}）"
    fi
}

# ---- 2. import 冒烟 ----
echo ""
echo "==> [1/6] 模块 import 冒烟"
if ${PYTHON} -c "import xianyu_alert.paths, xianyu_alert.secure, xianyu_alert.cli; print('imports OK')" >/dev/null 2>&1; then
    pass "xianyu_alert.paths / secure / cli 可导入"
else
    fail "xianyu_alert 模块导入失败"
fi

# ---- 3. once（mock 抓取成功 + SQLite 落盘）----
echo ""
echo "==> [2/6] cli once（mock 抓取）"
set +e
ONCE_OUT="$(${PYTHON} -m xianyu_alert.cli once --config config.poc.yaml 2>&1)"
ONCE_RC=$?
set -e
check_exit 0 "cli once 退出码为 0" "${ONCE_RC}"
if echo "${ONCE_OUT}" | grep -q "本轮共触发"; then
    pass "once 正常输出「本轮共触发」"
else
    fail "once 未输出预期汇总行"
fi
if echo "${ONCE_OUT}" | grep -qE "抓取 [1-9]"; then
    pass "mock 抓取到商品（非 0）"
else
    fail "mock 抓取商品数为 0（未达到「抓取成功」语义）"
fi
echo "${ONCE_OUT}" | tail -6 | sed 's/^/      /'

# ---- 4. Fernet 往返（首次加密会触发 secret.key 惰性生成）----
echo ""
echo "==> [3/6] Fernet 加解密往返"
set +e
FERNET_OUT="$(${PYTHON} - <<'PYEOF'
from xianyu_alert import secure
plain = "cookie2=abc123; _m_h5_tk=xyz_1700000000000"
cipher = secure.encrypt_text(plain)
back = secure.decrypt_text(cipher)
assert cipher.startswith("fernet1:"), "密文应带 fernet1: 前缀"
assert back == plain, "解密结果应与原文一致"
print(f"OK encrypt->decrypt 往返一致（密文前缀 fernet1:，长度 {len(cipher)}）")
PYEOF
)"
FERNET_RC=$?
set -e
check_exit 0 "Fernet 往返脚本退出码为 0" "${FERNET_RC}"
if echo "${FERNET_OUT}" | grep -q "^OK"; then
    pass "${FERNET_OUT}"
else
    fail "Fernet 往返失败：${FERNET_OUT}"
fi

# ---- 5. secret.key 生成 + 0600 ----
# 说明：secret.key 是**惰性生成**的（首次 encrypt_text 时落盘，secure.py
# `_load_or_create_key`），上方 Fernet 往返已触发生成；容器内真实路径
# （cli login 保存 Cookie / Web 粘贴）同样在首次加密时生成。
echo ""
echo "==> [4/6] secret.key 权限"
KEY_FILE="${POC_DIR}/secret.key"
if [[ -f "${KEY_FILE}" ]]; then
    pass "secret.key 已生成于 \${XY_DATA_DIR}"
else
    fail "secret.key 未生成于 \${XY_DATA_DIR}"
fi
if [[ -n "${KEY_FILE}" && -f "${KEY_FILE}" ]]; then
    MODE="$(stat -c '%a' "${KEY_FILE}" 2>/dev/null || stat -f '%Lp' "${KEY_FILE}" 2>/dev/null || echo '?')"
    if [[ "${MODE}" == "600" ]]; then
        pass "secret.key 权限为 0600（实际 ${MODE}）"
    else
        fail "secret.key 权限不是 0600（实际 ${MODE}）"
    fi
fi

# ---- 6. SQLite 落盘 ----
echo ""
echo "==> [5/6] SQLite 落盘"
DB_FILE="${POC_DIR}/state/xianyu_alert.db"
if [[ -f "${DB_FILE}" ]]; then
    pass "SQLite 已落盘 ${DB_FILE}"
    DB_SIZE="$(stat -c '%s' "${DB_FILE}" 2>/dev/null || stat -f '%z' "${DB_FILE}" 2>/dev/null || echo '?')"
    echo "      SQLite 文件大小：${DB_SIZE} bytes"
else
    fail "SQLite 未落盘（期望 ${DB_FILE}）"
fi
LOCK_FILE="${POC_DIR}/state/instance.lock"
[[ -f "${LOCK_FILE}" ]] && pass "单实例锁文件已生成于 \${XY_DATA_DIR}/state/instance.lock" \
    || fail "单实例锁文件未生成"

# ---- 7. cookie status（只检测不写入）----
echo ""
echo "==> [6/6] cli cookie status"
set +e
STATUS_OUT="$(${PYTHON} -m xianyu_alert.cli cookie status --config config.poc.yaml 2>&1)"
STATUS_RC=$?
set -e
check_exit 0 "cli cookie status 退出码为 0" "${STATUS_RC}"
if echo "${STATUS_OUT}" | grep -q "单值 Cookie"; then
    pass "cookie status 输出「单值 Cookie」（脱敏回显）"
else
    fail "cookie status 未输出单值 Cookie 行"
fi
echo "${STATUS_OUT}" | tail -4 | sed 's/^/      /'

# ---- 8. dpapi1: 老密文优雅降级（P0 验收项）----
echo ""
echo "==> [7] dpapi1: 老密文优雅降级"
set +e
DPAPI_OUT="$(${PYTHON} - <<'PYEOF'
from xianyu_alert import secure
result = secure.decrypt_text("dpapi1:AQAAANCMnd8BFdERjHoAwE/Cl+sBAAAA...")
assert result == "", "dpapi1: 老密文应返回空串（无法跨平台解密）"
print("OK dpapi1: 老密文返回空串，提示重登（优雅降级）")
PYEOF
)"
DPAPI_RC=$?
set -e
check_exit 0 "dpapi1 降级脚本退出码为 0" "${DPAPI_RC}"
if echo "${DPAPI_OUT}" | grep -q "^OK"; then
    pass "${DPAPI_OUT}"
else
    fail "dpapi1 降级失败：${DPAPI_OUT}"
fi

# ---- 汇总 ----
echo ""
echo "============================================================"
echo "P0 容器语义验证汇总：通过 ${PASS} 项 / 失败 ${FAIL} 项"
echo "临时数据目录（模拟卷）：${POC_DIR}"
echo "============================================================"
if [[ "${FAIL}" -gt 0 ]]; then
    echo "❌ 存在失败项，请检查上方输出。"
    exit 1
fi
echo "✅ 全部通过。"
echo ""
echo "在【有 Docker】的主机上，容器内等价命令（目录名含中文/空格时必须带 -p xianyu-alert）："
echo "  docker compose -p xianyu-alert run --rm xianyu-alert once --config config.poc.yaml"
echo "  docker compose -p xianyu-alert run --rm xianyu-alert cookie status --config config.poc.yaml"
echo "（service 已设 entrypoint=python -m xianyu_alert.cli，run 后直接跟子命令即可）"
echo "容器内业务核心测试子集（198 个；tests/ 未打进镜像，需挂载并覆盖 entrypoint）："
echo "  docker compose -p xianyu-alert run --rm --no-deps -v \"./tests:/app/tests\" \\"
echo "    --entrypoint python xianyu-alert -m unittest tests.test_models tests.test_storage tests.test_notifier tests.test_cookie tests.test_secure tests.test_monitor tests.test_filter"
exit 0
