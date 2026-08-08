"""图形界面双击启动入口。

Windows 下 `.pyw` 后缀由 pythonw.exe 关联，双击**不会弹出黑色控制台窗口**。

用法：
    直接双击本文件，或在命令行执行 `pythonw run_gui.pyw`。
等价于：
    python -m xianyu_alert.cli gui

frozen 适配（对齐 v3 设计文档 D3）：
    - 打包为 exe 后**不 chdir**、**不把源码根塞进 sys.path**；
    - 配置路径统一走 `paths.default_config_path()`（exe 同目录 config.yaml）。
"""

from __future__ import annotations

import os
import sys

from xianyu_alert import paths


def _run() -> int:
    """导入并启动图形界面。

    Returns:
        进程退出码。
    """
    if not paths.is_frozen():
        # 源码模式：保证从任意工作目录双击都能 import 到 xianyu_alert 包
        _PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
        if _PROJECT_ROOT not in sys.path:
            sys.path.insert(0, _PROJECT_ROOT)

    try:
        from xianyu_alert.gui import main as gui_main
    except Exception as exc:  # noqa: BLE001 - 导入失败也要给用户看得懂的提示
        try:
            import tkinter
            from tkinter import messagebox

            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror(
                "启动失败",
                f"无法加载图形界面模块：\n{exc}\n\n"
                "请确认已安装依赖：pip install -r requirements.txt",
            )
            root.destroy()
        except Exception:  # noqa: BLE001 - 连 Tk 都用不了时退回打印
            print(f"无法加载图形界面模块：{exc}")
        return 1

    # v1.8 单实例锁：GUI 启动前先获取锁（幂等，重复获取安全）；
    # 冲突提示与退出码由 gui_main 统一处理（messagebox + 非 0）。
    from xianyu_alert.singleton import acquire_instance_lock, release_instance_lock

    lock = acquire_instance_lock()
    try:
        return gui_main(config_path=paths.default_config_path())
    finally:
        release_instance_lock(lock)


if __name__ == "__main__":
    sys.exit(_run())
