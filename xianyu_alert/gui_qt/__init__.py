"""PySide6（Qt for macOS）图形界面包。

macOS 端入口：`main(config_path)` 装配 QApplication 并启动主窗口。
防御性导入：本包顶层**不强制** import PySide6，`import xianyu_alert.gui_qt`
在无 PySide6 环境（如 Windows 开发机）也能成功；仅调用 `main()` 时才要求
PySide6 可用（入口分发会先检查 `is_available()` 再回退 Tkinter）。

设计对齐（macOS 适配设计文档 §3.1）：
    - 保持 Qt 默认 macOS 风格（QMacStyle），不强制 Fusion 样式；
    - 深色模式跟随系统（不硬编码 stylesheet）；
    - 默认系统字体，个别控件回退异常时可用 PingFang SC 兜底。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from ..singleton import acquire_instance_lock, lock_holder_pid, release_instance_lock

logger = logging.getLogger(__name__)

__all__ = ["is_available", "main"]


def is_available() -> bool:
    """PySide6 是否可导入（供 cli/entry 平台分发回退判断）。

    Returns:
        True 表示 PySide6 可用。
    """
    try:
        import PySide6  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - 导入失败视为不可用
        return False


def main(config_path: str = "") -> int:
    """启动 Qt 图形界面（macOS 主入口）。

    Args:
        config_path: 配置文件路径；空串时使用 paths.default_config_path()。

    Returns:
        进程退出码（Qt 窗口正常退出为 0；已有实例冲突返回 1）。
    """
    from PySide6.QtWidgets import QApplication, QMessageBox

    if config_path == "":
        from .. import paths

        config_path = paths.default_config_path()

    # 无显示环境（CI / 单测）自动走 offscreen 平台插件；macOS 真机走原生窗口。
    if os.environ.get("QT_QPA_PLATFORM") is None and sys.platform not in ("darwin", "win32"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    # v1.8 单实例锁（L5）：检测到已有实例 → 弹中文提示 + 返回非 0，不抢锁。
    lock = acquire_instance_lock()
    if lock is None:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.warning(
            None,
            "已有实例正在运行",
            f"已有实例正在运行（PID {lock_holder_pid() or '未知'}），请先关闭再启动。",
        )
        return 1

    try:
        app = QApplication.instance() or QApplication(sys.argv)
        app.setApplicationName("闲鱼低价提醒工具")
        app.setOrganizationName("xianyu-alert")

        from .app import XianyuAlertQtApp

        window = XianyuAlertQtApp(config_path=config_path)
        window.show()
        return app.exec()
    finally:
        release_instance_lock(lock)
