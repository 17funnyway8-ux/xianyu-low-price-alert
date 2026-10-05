#!/usr/bin/env bash
# ===========================================================
# scripts/quality_audit.sh —— 项目质量复评（与审查报告同口径）
# ===========================================================
# 一命令得出 4 个硬指标，用于：
#   1) 本地改动前的自检
#   2) CI 门禁（退出码非 0 即失败）
#   3) 里程碑复评分（M0/M1/M2/M3 前后对比）
#
# 用法：
#   scripts/quality_audit.sh              # 全量检查（含测试）
#   scripts/quality_audit.sh --no-tests   # 只查 lint/type
#   XY_PY=/path/to/python scripts/quality_audit.sh
#
# 退出码：0 = 全部达标；1 = 有未达标项
# ===========================================================
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$XY_PY"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then
    PY=".venv/bin/python"
  else
    PY="$(command -v python3)"
  fi
fi

# ---- 门槛（与审查报告一致；逐步收紧） ----
MIN_TOTAL_COVERAGE=56     # M0/M1 阶段防倒退基线（目标 M2 提到 70）
MIN_CORE_COVERAGE=77      # 核心包（不含 gui/gui_qt）；当前 77.9%，目标 80
MAX_RUFF=0
MAX_MYPY=0

RUN_TESTS=1
[ "$1" = "--no-tests" ] && RUN_TESTS=0

echo "=============================================================="
echo " 质量复评 · 闲鱼低价提醒工具"
echo " python : $PY"
echo " 时间   : $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="

FAIL=0

# ---- 1) ruff ----
if "$PY" -m ruff --version >/dev/null 2>&1; then
  RUFF_OUT="$("$PY" -m ruff check --output-format=concise xianyu_alert web tests scripts 2>&1)"
  RUFF_N="$(printf '%s\n' "$RUFF_OUT" | grep -cE '^[^ ].*:[0-9]+:[0-9]+:')"
  echo "[1/4] ruff        : $RUFF_N 条问题（门槛 <= ${MAX_RUFF}）"
  if [ "$RUFF_N" -gt "$MAX_RUFF" ]; then FAIL=1; fi
else
  echo "[1/4] ruff        : 未安装，跳过"
fi

# ---- 2) mypy ----
if "$PY" -m mypy --version >/dev/null 2>&1; then
  MYPY_OUT="$("$PY" -m mypy --ignore-missing-imports xianyu_alert web 2>&1)"
  MYPY_N="$(printf '%s\n' "$MYPY_OUT" | grep -oE 'Found [0-9]+ error' | grep -oE '[0-9]+' | tail -1)"
  [ -z "$MYPY_N" ] && MYPY_N=0
  echo "[2/4] mypy        : $MYPY_N 个错误（门槛 <= ${MAX_MYPY}）"
  if [ "$MYPY_N" -gt "$MAX_MYPY" ]; then FAIL=1; fi
else
  echo "[2/4] mypy        : 未安装，跳过"
fi

# ---- 3) 测试 + 4) 覆盖率 ----
if [ "$RUN_TESTS" = "1" ]; then
  export COVERAGE_FILE="$ROOT/.coverage.audit"
  TEST_LOG="$(mktemp)"
  "$PY" -m coverage run --source=xianyu_alert,web -m unittest discover -s tests > "$TEST_LOG" 2>&1
  TEST_RC=$?
  SUMMARY="$(grep -E '^(Ran |OK|FAILED)' "$TEST_LOG" | tr '\n' ' ')"
  echo "[3/4] 单元测试    : $SUMMARY"
  if [ "$TEST_RC" != "0" ]; then
    FAIL=1
    echo "      ↑ 失败详情：$TEST_LOG"
  fi

  COV_OUT="$("$PY" -m coverage report --skip-empty 2>&1)"
  TOTAL="$(printf '%s\n' "$COV_OUT" | grep -E '^TOTAL' | awk '{print $NF}' | tr -d '%')"
  CORE="$(printf '%s\n' "$COV_OUT" | grep -E '^xianyu_alert/|^web/' | grep -v 'gui' | awk '{n+=$2; m+=$3} END {if (n>0) printf "%.1f", (n-m)*100/n; else print 0}')"
  echo "[4/4] 覆盖率      : 整体 $TOTAL%（门槛 >= ${MIN_TOTAL_COVERAGE}） · 核心 $CORE%（门槛 >= ${MIN_CORE_COVERAGE}）"
  if [ -n "$TOTAL" ] && [ "$TOTAL" -lt "$MIN_TOTAL_COVERAGE" ]; then FAIL=1; fi
  CORE_INT="$(printf '%.0f' "$CORE")"
  if [ "$CORE_INT" -lt "$MIN_CORE_COVERAGE" ]; then FAIL=1; fi
else
  echo "[3/4] 单元测试    : 已跳过（--no-tests）"
  echo "[4/4] 覆盖率      : 已跳过（--no-tests）"
fi

echo "--------------------------------------------------------------"
if [ "$FAIL" = "0" ]; then
  echo " 结论：全部达标 ✅"
else
  echo " 结论：存在未达标项 ❌"
fi
echo "=============================================================="
exit $FAIL
