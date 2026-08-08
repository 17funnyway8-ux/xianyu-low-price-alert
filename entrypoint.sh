#!/bin/sh
# ===========================================================
# 闲鱼低价提醒工具 —— 容器入口分发脚本（P1）
# ===========================================================
# 目标：一条镜像同时支持「常驻 Web 服务」与「CLI 调试」。
#
#   docker compose up -d                          → python -m web.entry（常驻 Web）
#   docker compose run --rm xianyu-alert once ... → python -m xianyu_alert.cli once ...
#   docker compose run --rm xianyu-alert cookie status ...
#
# 说明：
#   - Dockerfile CMD 为 ["python", "-m", "web.entry"]，up 时作为参数传入，
#     本脚本识别后转发到 web.entry；
#   - 第一个参数是已知 CLI 子命令（once/run/list/login/cookie/shortcut/gui）
#     时直接走 CLI（容器内调试用；Web 运行中不可 once/run，会抢单实例锁返回 2，
#     调试请用 cookie status / list / login）；
#   - 其余参数形态原样执行（如显式 python -m xianyu_alert.cli cookie status）。
# ===========================================================
set -e

case "$1" in
  once|run|list|login|cookie|shortcut|gui|-v|--version|-h|--help)
    exec python -m xianyu_alert.cli "$@"
    ;;
esac

case "$*" in
  "python -m web.entry"|"python -m web.entry "*)
    # 去掉 CMD 前缀后转发（up -d 默认路径）
    shift 3 2>/dev/null || true
    exec python -m web.entry "$@"
    ;;
esac

# 其它命令原样执行
exec "$@"
