# ===========================================================
# 闲鱼低价提醒工具 —— Dockerfile（P1：Web 基础版常驻服务）
# ===========================================================
# P1 目标：FastAPI + 原生单页 + monitor 后台线程的常驻容器。
#   - 基础镜像 python:3.13-slim（与开发运行时一致；slim 比 alpine 省 musl 兼容坑）
#   - 系统包仅 tzdata（TZ=Asia/Shanghai 必需）+ ca-certificates（requests HTTPS）
#   - 依赖：requirements.txt（业务核心）+ requirements-web.txt（fastapi/uvicorn）；
#     不装 playwright / PySide6 / tkinter（零新增桌面依赖，省 ~1GB+150MB）
#   - XY_DATA_DIR=/app/data → paths.data_dir() 直通（v1.8 已支持，零改动），
#     config.yaml / secret.key / state/ 全部自动落卷
#   - 入口：entrypoint.sh 分发（默认 python -m web.entry；run 子命令走 CLI 调试）
#   - HEALTHCHECK：HTTP /healthz（**不得**用 cli once/run，会抢单实例锁返回 2）
#
# 使用：
#   docker compose -p xianyu-alert up -d --build     # 常驻 Web 服务
#   docker compose -p xianyu-alert down              # 优雅退出（SIGTERM）
#   docker compose -p xianyu-alert run --rm xianyu-alert cookie status --config config.example.yaml
# ===========================================================

FROM python:3.13-slim

# 运行期环境：不写 __pycache__ / 日志即时刷出 / 时区 Asia/Shanghai / 数据目录
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    XY_DATA_DIR=/app/data

# tzdata：TZ=Asia/Shanghai 必须安装（slim 镜像默认无时区库）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖：业务核心（requirements.txt）+ Web（requirements-web.txt）
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

# 业务核心包（全量原样复用，零改动）
COPY xianyu_alert/ ./xianyu_alert/

# Web 层（P1 新增）
COPY web/ ./web/

# 入口分发脚本（默认 web.entry 常驻；CLI 子命令调试）
COPY entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

# 示例/演示配置：mock 离线抓取演示 + mtop 真实抓取模板
COPY config.poc.yaml config.example.yaml ./

# 数据卷挂载点（XY_DATA_DIR=/app/data -> config.yaml / secret.key / state/ 落卷）
RUN mkdir -p /app/data

EXPOSE 8080

# 健康检查：HTTP /healthz（含 monitor 线程状态）；不得用 cli once/run（抢锁）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

# P1 常驻 Web：entrypoint.sh 默认转发到 python -m web.entry
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "web.entry"]
