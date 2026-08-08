"""容器 / 本地 Web 入口：python -m web.entry。

启动时序（对齐 docs/v1.8_Docker化增量研判与执行方案.md §2.3）：
    1. paths.ensure_data_dir()      —— XY_DATA_DIR=/app/data 全链路落卷；
    2. secure._load_or_create_key() —— 无则生成 secret.key → data_dir()（POSIX 0600）；
    3. singleton.acquire_instance_lock() —— 获取失败 → stderr 提示 + 退出码非 0；
    4. MonitorService 装载（load_config；缺失则生成默认配置）；
    5. 日志：stdout + state/xianyu_alert.log 滚动文件（复用 cli.install_file_logging）；
    6. uvicorn.run(api.app, host=0.0.0.0, port=8080)（uvicorn 跑在子线程，
       主线程等待 SIGTERM/SIGINT）；
    7. 优雅退出：monitor.stop() + thread.join(timeout=5) → server.should_exit →
       release_instance_lock() → exit 0。

健康检查约定：容器 HEALTHCHECK 走 HTTP /healthz，**不得**调用 `cli once/run`
（会与运行中的 Web 实例抢单实例锁返回 2，设计 §5.4 / 风险 #9）。

运行：
    本地冒烟：XY_DATA_DIR=/tmp/xy-web .venv/bin/python -m web.entry
    容器常驻：docker compose up -d（Dockerfile CMD 指向本模块）
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import List, Optional

import uvicorn

from xianyu_alert import paths, secure, singleton
from xianyu_alert.cli import setup_logging

from . import api
from .monitor_service import MonitorService

logger = logging.getLogger("xianyu_alert")

#: 默认监听地址 / 端口（对齐设计 §5.3：容器内 0.0.0.0:8080）
HOST = "0.0.0.0"
PORT = 8080
#: 等待 uvicorn 就绪的最长秒数（端口占用 / 配置错误时提前失败退出）
UVICORN_READY_TIMEOUT = 10.0
#: 优雅退出时等待 uvicorn 收尾的最长秒数
UVICORN_JOIN_TIMEOUT = 10.0


def _acquire_lock():
    """获取进程单实例锁；失败返回 None（调用方据此退出非 0）。"""
    lock = singleton.acquire_instance_lock()
    if lock is None:
        holder = singleton.lock_holder_pid()
        print(
            f"已有实例运行中（PID {holder or '未知'}），Web 服务无法启动，请先退出其它实例。",
            file=sys.stderr,
        )
    return lock


def main(argv: Optional[List[str]] = None) -> int:
    """Web 入口主函数（返回进程退出码）。"""
    del argv  # 无 CLI 参数；预留签名便于测试调用

    # 1. 数据目录
    paths.ensure_data_dir()

    # 2. 密钥（无则生成；卷挂载后随卷持久化，必须与 config/db 一起备份）
    key = secure._load_or_create_key()  # noqa: SLF001 - 内部函数，入口处一次性调用
    if key is None:
        logger.warning("Fernet 密钥不可用，Cookie 将无法加密保存（请检查 cryptography 安装）")

    # 3. 单实例锁（Web 进程即唯一实例；失败退出 2）
    lock = _acquire_lock()
    if lock is None:
        return 2

    # 4. 日志（stdout + state/xianyu_alert.log 滚动文件，setup_logging 内已装）
    setup_logging(verbose=False)

    # 5. MonitorService（装载配置；缺失则生成默认配置）
    service = MonitorService()
    logger.info("Web 服务启动：数据目录 %s，配置 %s", paths.data_dir(), service.config_path)

    # 6. 信号处理：SIGTERM / SIGINT → 优雅退出
    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame) -> None:  # noqa: ANN001
        logger.info("收到信号 %s，正在优雅退出……", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # 7. uvicorn 跑在子线程（主线程阻塞等待停止信号）
    server = uvicorn.Server(uvicorn.Config(api.app, host=HOST, port=PORT, log_level="info"))
    server_thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    server_thread.start()

    # 等待就绪：端口占用 / 配置错误时 uvicorn 线程提前退出
    deadline = time.monotonic() + UVICORN_READY_TIMEOUT
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            break
        if not server_thread.is_alive():
            print("uvicorn 启动失败（端口被占用或配置错误），退出。", file=sys.stderr)
            singleton.release_instance_lock(lock)
            return 1
        time.sleep(0.1)
    else:
        print("uvicorn 启动超时，退出。", file=sys.stderr)
        singleton.release_instance_lock(lock)
        return 1

    logger.info("Web 服务已就绪：http://127.0.0.1:%d/（容器内 0.0.0.0:%d）", PORT, PORT)

    # 8. 阻塞等待停止信号
    stop_event.wait()

    # 9. 优雅退出：monitor.stop() + join → uvicorn 收尾 → 释放锁
    logger.info("正在停止监测线程……")
    service.stop()  # monitor.stop() + thread.join(timeout=5)
    logger.info("正在关闭 HTTP 服务……")
    server.should_exit = True
    server_thread.join(timeout=UVICORN_JOIN_TIMEOUT)
    service.shutdown()
    singleton.release_instance_lock(lock)
    logger.info("Web 服务已优雅退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
