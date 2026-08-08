# ===========================================================
# 闲鱼低价提醒工具 —— Dockerfile（QA FINDING-1 修复版：alpine 基座 + 无 venv 重复）
# ===========================================================
# 修复背景（QA FINDING-1：docker images 272MB 未达 ≤180MB 验收）：
#   - P2 初版用「多阶段 + /opt/venv」：runtime 层复制完整 venv，造成 Python
#     stdlib 重复（UNIQUE 层 115.5MB vs P1 的 78.6MB），且 `python:3.13-slim`
#     基础镜像在本机 Docker 的 SIZE 列会把多架构 manifest list 共享层计入
#     （实测仅 FROM python:3.13-slim + tzdata 即显示 200MB）→ 无论如何都无法 ≤180MB；
#   - 本版修复：
#       ① runtime 层直接在系统 python `pip install --no-cache-dir` 装依赖，
#         去掉 venv 重复（QA 推荐方案①）；
#       ② 基座改用 `python:3.13-alpine`（musl，单架构存储，SIZE 列真实）：
#         本仓库全部依赖（requests / beautifulsoup4 / PyYAML / cryptography /
#         fastapi / uvicorn）均有 musllinux wheel 或纯 Python 实现，已实测容器内
#         fernet 加密/解密、web.entry、/healthz、monitor run_once(mock)、graceful
#         exit 全部正常；`docker images` 实测 ≈ 129MB ≤ 180MB。
#   - 与设计「slim 比 alpine 省 musl 兼容坑」的偏差说明：该考量在「依赖集固定且
#     全部有 musllinux wheel」的前提下不构成实际风险，此处以 PRD 验收
#     「docker images ≤~180MB」优先（偏差已在交付说明中记录）。
#
# 功能基线（与 P1/P2 完全一致）：
#   - XY_DATA_DIR=/app/data → config.yaml / secret.key / state/ 全部落卷；
#   - 入口：entrypoint.sh 分发（默认 python -m web.entry；CLI 子命令调试）；
#   - HEALTHCHECK：HTTP /healthz（不得用 cli once/run，会抢单实例锁返回 2）。
#
# 使用：
#   docker compose -p xianyu-alert up -d --build     # 常驻 Web 服务
#   docker compose -p xianyu-alert down              # 优雅退出（SIGTERM）
# ===========================================================

FROM python:3.13-alpine

# 运行期环境：不写 __pycache__ / 日志即时刷出 / 时区 Asia/Shanghai / 数据目录
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    XY_DATA_DIR=/app/data

# tzdata：TZ=Asia/Shanghai 必须安装（alpine 默认无时区库）；ca-certificates：requests HTTPS
RUN apk add --no-cache tzdata ca-certificates

WORKDIR /app

# 依赖（runtime 直接装系统 python，无 venv 重复；--no-cache-dir 不留缓存；
# 先 COPY 清单利用构建缓存，源码改动不触发 pip install 重跑）
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

# 业务核心包（全量原样复用，零改动）
COPY xianyu_alert/ ./xianyu_alert/

# Web 层（P2 全功能）
COPY web/ ./web/

# 入口分发脚本（默认 web.entry 常驻；CLI 子命令调试）
COPY entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

# 示例/演示配置：mock 离线抓取演示 + mtop 真实抓取模板
COPY config.poc.yaml config.example.yaml ./

# 数据卷挂载点（XY_DATA_DIR=/app/data -> config.yaml / secret.key / state/ 落卷）
RUN mkdir -p /app/data

# 可选非 root 加固（P3-04 should，默认不启用：需卷属主对齐，见 compose user: 注释）
# RUN adduser -D -u 1000 appuser && chown -R appuser:appuser /app
# USER appuser

EXPOSE 8080

# 健康检查：HTTP /healthz（含 monitor 线程状态）；不得用 cli once/run（抢锁）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

# P1 常驻 Web：entrypoint.sh 默认转发到 python -m web.entry
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "web.entry"]
