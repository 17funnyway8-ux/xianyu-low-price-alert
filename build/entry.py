"""打包后的统一入口（由 PyInstaller spec 引用）。

行为：
    - **带命令行参数**（`--version` / `--help` / `once` / `run` / `login` /
      `shortcut` 等）→ 走 `xianyu_alert.cli.main`；
    - **无参数**（双击 exe）→ 启动图形界面。

windowed 打包（console=False）时 Windows 不会为 exe 分配控制台，
`sys.stdout` 为 None；此时把 CLI 输出重定向到 exe 同目录
`state/xianyu_alert_cli.log`，保证 `--version` / `--help` / `once`
等命令的输出可被查看与自动化验证。
"""

from __future__ import annotations

import os
import sys


def _ensure_cli_stdio() -> None:
    """windowed exe 无控制台时，把 CLI 输出落到文件（不崩溃、可查）。

    两个职责：
        1. 尽可能把 stdout/stderr 重配置为 UTF-8（errors=replace），
           避免中文 Windows（GBK 代码页）在打印「¥」等字符时抛
           `UnicodeEncodeError`（ConsoleNotifier / cli print 均受影响）；
        2. 若确无可用 stdout（windowed 双击场景），重定向到
           exe 同目录 `state/xianyu_alert_cli.log`。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 重配置失败则保留原样
            pass
    if sys.stdout is not None and hasattr(sys.stdout, "write"):
        return  # 已有可用 stdout（例如 shell 重定向场景）
    try:
        state_dir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "state")
        os.makedirs(state_dir, exist_ok=True)
        log_path = os.path.join(state_dir, "xianyu_alert_cli.log")
        stream = open(log_path, "a", encoding="utf-8", errors="replace")
        sys.stdout = stream
        sys.stderr = stream
    except Exception:  # noqa: BLE001 - 重定向失败也不影响 GUI 启动
        pass


def main() -> int:
    """入口主函数。

    Returns:
        进程退出码。
    """
    args = sys.argv[1:]
    if args:
        # 任何命令行参数 → CLI 模式
        _ensure_cli_stdio()
        from xianyu_alert.cli import main as cli_main

        return cli_main(args)

    # 无参数 → GUI 模式（双击场景；macOS 分发到 Qt/PySide6，其余 Tkinter）
    from xianyu_alert import paths

    if sys.platform == "darwin":
        try:
            # gui_qt 顶层不 import PySide6，须先显式探测 is_available() 再
            # 调用 main()；main() 内部 QApplication 导入失败同样回退 Tk（Bug #2）。
            from xianyu_alert.gui_qt import is_available, main as gui_main

            if not is_available():
                raise ImportError("PySide6 模块不可用")
        except ImportError:
            # macOS 上 PySide6 缺失 → 回退 Tkinter（并打 warning 由 GUI 自身处理）
            from xianyu_alert.gui import main as gui_main
    else:
        from xianyu_alert.gui import main as gui_main

    return gui_main(config_path=paths.default_config_path())


if __name__ == "__main__":
    sys.exit(main())
