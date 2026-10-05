#!/usr/bin/env bash
# ===========================================================
# scripts/verify_all.sh —— 前端交付验证「一键四层」
#
# 四层各管一段，缺一层就有盲区：
#   L1 契约静态比对  前端调用的 URL/字段/通道 ↔ 后端源码 AST
#                    → 抓「对不上的名字」，不需要起服务，最快
#   L2 HTTP 冒烟     临时 uvicorn + mock 抓取器，打真实 HTTP
#                    → 抓「接口语义」，可以随便写数据
#   L3 DOM 端到端    真实前端 × 临时后端（jsdom 跑 <script>）
#                    → 抓「渲染期才暴露的问题」，可以断言具体数据
#   L4 部署验收      真实前端 × 已部署实例，且强制只读
#                    → 抓「镜像里是不是新前端」（静态资源逐字节哈希）
#
# 用法：
#   scripts/verify_all.sh                    # 默认验证 http://127.0.0.1:8080
#   scripts/verify_all.sh --base http://10.0.0.26:8080
#   scripts/verify_all.sh --skip-live        # 跳过 L4（实例没起时）
# 退出码：0 = 四层全绿；1 = 有失败
# ===========================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${XY_PY:-/Users/xxx/.workbuddy/binaries/python/envs/default/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

NODE_BIN="${XY_NODE:-/Users/xxx/.workbuddy/binaries/node/versions/22.22.2-3/bin/node}"
[ -x "$NODE_BIN" ] || NODE_BIN="$(command -v node)"

JSDOM_DIR="${XY_JSDOM:-/Users/xxx/.workbuddy/binaries/node/workspace/node_modules}"

BASE="http://127.0.0.1:8080"
SKIP_LIVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    --skip-live) SKIP_LIVE=1; shift ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

declare -a NAMES=() RESULTS=()
run_layer() {
  local name="$1"; shift
  echo ""
  echo "############################################################"
  echo "# $name"
  echo "############################################################"
  if "$@"; then
    NAMES+=("$name"); RESULTS+=("PASS")
  else
    NAMES+=("$name"); RESULTS+=("FAIL")
  fi
}

# ---------- L1 契约静态比对 ----------
l1() {
  "$NODE_BIN" scripts/web_contract_probe.js > /tmp/xy_probe.json 2>/tmp/xy_probe.err || {
    echo "探针失败："; cat /tmp/xy_probe.err; return 1;
  }
  "$PY" scripts/check_web_contract.py
}

# ---------- L2 HTTP 冒烟 ----------
l2() { "$PY" scripts/e2e_web_smoke.py; }

# ---------- L3 DOM 端到端（临时后端 + mock）----------
l3() {
  NODE_PATH="$JSDOM_DIR" "$NODE_BIN" scripts/e2e_web_dom.js
}

# ---------- L4 部署验收（只读）----------
l4() { "$PY" scripts/verify_web_deploy.py --base "$BASE"; }
l4b() { NODE_PATH="$JSDOM_DIR" "$NODE_BIN" scripts/e2e_web_dom_live.js --base "$BASE"; }

run_layer "L1 契约静态比对（前端调用 ↔ 后端源码）" l1
run_layer "L2 HTTP 冒烟（临时后端 + mock 抓取器）" l2
run_layer "L3 DOM 端到端（真实前端 × 临时后端）" l3
if [ "$SKIP_LIVE" -eq 0 ]; then
  run_layer "L4 部署验收（HTTP 层，含静态资源哈希比对）" l4
  run_layer "L5 部署验收（DOM 层，只读，对生产零写入）" l4b
fi

echo ""
echo "============================================================"
echo " 汇总"
echo "============================================================"
FAILED=0
for i in "${!NAMES[@]}"; do
  if [ "${RESULTS[$i]}" = "PASS" ]; then
    printf '  ✅ %s\n' "${NAMES[$i]}"
  else
    printf '  ❌ %s\n' "${NAMES[$i]}"
    FAILED=1
  fi
done
echo "============================================================"
if [ "$FAILED" -eq 0 ]; then
  echo " 全部通过"
else
  echo " 存在失败层，请查看上方输出"
fi
exit $FAILED
