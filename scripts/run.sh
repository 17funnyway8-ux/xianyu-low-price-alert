#!/usr/bin/env bash
# 统一入口：把常用检查收敛成一个命令，避免"脚本散落、不知道先跑哪个"。
#
# 用法：
#   ./scripts/run.sh quality    # ruff + mypy + 单测 + 覆盖率（提交前自检）
#   ./scripts/run.sh contract   # 前端 ↔ 后端契约交叉校验
#   ./scripts/run.sh e2e        # 契约 + API 冒烟 + DOM 交互（需 npm ci，装过 jsdom）
#   ./scripts/run.sh all        # quality + e2e
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"

quality() { PYTHON="$PY" bash scripts/quality_audit.sh; }
contract() { "$PY" scripts/check_web_contract.py; }
e2e() {
  # 让 node 脚本（e2e_web_dom.js）也能找到同一个解释器：把它所在目录前置进 PATH
  export PATH="$(dirname "$PY"):$PATH"
  export PYTHON="$PY"
  contract
  "$PY" scripts/e2e_web_smoke.py
  if [ -d node_modules/jsdom ]; then
    node scripts/e2e_web_dom.js
  else
    echo "跳过 DOM e2e：未安装 jsdom（先跑 npm ci）"
  fi
}

case "${1:-all}" in
  quality) quality ;;
  contract) contract ;;
  e2e) e2e ;;
  all) quality; e2e ;;
  *) echo "用法: $0 {quality|contract|e2e|all}"; exit 2 ;;
esac
