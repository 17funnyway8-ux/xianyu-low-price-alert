# ===========================================================
# 闲鱼低价提醒工具 —— Dockerfile（P0 容器 POC 最小版）
# ===========================================================
# P0 目标：验证「业务核心 + v1.8 新能力在容器内可跑」，**不装 Web**。
#   - 基础镜像 python:3.13-slim（与开发运行时一致；slim 比 alpine 省 musl 兼容坑）
#   - 系统包仅 tzdata（TZ=Asia/Shanghai 必需）+ ca-certificates（requests HTTPS）
#   - 依赖仅 requirements.txt（requests/beautifulsoup4/PyYAML/cryptography），
#     不引入 fastapi / uvicorn / playwright / PySide6（那是 P1+ 的事）
#   - XY_DATA_DIR=/app/data → paths.data_dir() 直通（v1.8 已支持，零改动），
#     config.yaml / secret.key / state/ 全部自动落卷
#
# P0 无 Web 服务 → 不设 HEALTHCHECK（P1 起用 HTTP /healthz 探活；
# 容器内健康巡检请用 `cli cookie status` / `cli list`，它们不参与单实例锁）。
#
# 使用（一次性命令，非常驻服务）：
#   docker compose run --rm xianyu-alert once --config config.poc.yaml
#   docker compose run --rm xianyu-alert cookie status --config config.example.yaml
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

# P0 只装业务核心依赖（无 Web 依赖，镜像最小化）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 业务核心包（全量原样复用，零改动）
COPY xianyu_alert/ ./xianyu_alert/

# P0 演示/示例配置：
#   config.poc.yaml      -> mock 离线抓取演示（无 Cookie / 无网络即可跑通 once）
#   config.example.yaml  -> mtop 真实抓取模板（需先配置 Cookie）
COPY config.poc.yaml config.example.yaml ./

# 数据卷挂载点（XY_DATA_DIR=/app/data -> config.yaml / secret.key / state/ 落卷）
RUN mkdir -p /app/data

# 占位 CMD：P0 容器语义是「一次性命令」，实际入口用 docker compose run --rm：
#   docker compose run --rm xianyu-alert once --config config.poc.yaml
#   docker compose run --rm xianyu-alert cookie status --config config.example.yaml
CMD ["python", "-c", "print('xianyu-alert P0 容器就绪。请用 docker compose run --rm xianyu-alert once --config config.poc.yaml 验证业务核心。')"]
