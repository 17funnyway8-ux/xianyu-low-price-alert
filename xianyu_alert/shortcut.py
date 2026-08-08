"""桌面快捷方式创建（安全转义版）。

对齐 v3 设计文档 C11：
    - 通过 PowerShell + WScript.Shell 创建 .lnk；
    - **修复 exe 逆向中的 f-string 转义隐患**：路径含 `$` / `"` / 反引号时
      会被 PowerShell 误解析（`$` 是变量前缀，`"` 提前结束字符串），
      统一用 `_ps_escape` 转义后再拼进脚本；
    - subprocess 一律**参数化列表**调用，杜绝 shell 拼接注入；
    - 提供 `create_shortcut(exe_path, name)` 纯函数，返回成功/失败信息。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any, Callable, Optional

from . import paths

logger = logging.getLogger(__name__)

#: 默认快捷方式描述
DEFAULT_DESCRIPTION = "闲鱼低价提醒工具"


def supported() -> bool:
    """当前平台是否支持创建桌面快捷方式（仅 Windows）。

    macOS 无 .lnk 语义：`.app` 拖入「应用程序」/ Dock 即用，
    开机自启由 LaunchAgent 承担（见 docs/LaunchAgent模板.plist）。

    Returns:
        True 表示当前平台支持（sys.platform == "win32"）。
    """
    return sys.platform == "win32"


def _ps_escape(value: str) -> str:
    """把字符串转义为 PowerShell 双引号字符串字面量。

    规则（按 PowerShell 语法）：
        - 反引号 `` ` `` -> `` `` ``（反引号是转义前缀，先转义）；
        - `$` -> `` `$ ``（否则被当作变量）；
        - `"` -> `` `" ``（否则提前结束字符串）。

    Args:
        value: 原始路径/参数。

    Returns:
        转义后的文本。
    """
    text = str(value or "")
    return text.replace("`", "``").replace("$", "`$").replace('"', '`"')


def desktop_dir() -> str:
    """返回桌面目录。

    优先读取注册表 User Shell Folders（兼容 OneDrive 重定向），
    失败时回退到 `~/Desktop`。

    Returns:
        桌面目录绝对路径。
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            desktop, _ = winreg.QueryValueEx(key, "Desktop")
        if desktop:
            return os.path.expandvars(desktop)
    except Exception:  # noqa: BLE001 - 注册表不可用时回退
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def build_powershell_script(
    target: str,
    args: str,
    workdir: str,
    lnk: str,
    icon: str = "",
    description: str = DEFAULT_DESCRIPTION,
) -> str:
    """构造创建快捷方式的 PowerShell 脚本（所有路径均已转义）。

    Args:
        target: 目标程序路径。
        args: 启动参数（可空）。
        workdir: 工作目录。
        lnk: 快捷方式 .lnk 完整路径。
        icon: 图标文件路径（可空，空则不设置图标）。
        description: 快捷方式描述。

    Returns:
        可直接交给 `powershell -Command` 执行的脚本文本。
    """
    lines = [
        "$ws = New-Object -ComObject WScript.Shell",
        f'$lnk = $ws.CreateShortcut("{_ps_escape(lnk)}")',
        f'$lnk.TargetPath = "{_ps_escape(target)}"',
        f'$lnk.Arguments = "{_ps_escape(args)}"',
        f'$lnk.WorkingDirectory = "{_ps_escape(workdir)}"',
    ]
    if icon:
        lines.append(f'$lnk.IconLocation = "{_ps_escape(icon)},0"')
    lines.append(f'$lnk.Description = "{_ps_escape(description)}"')
    lines.append("$lnk.Save()")
    return "\n".join(lines)


def _default_target_and_args(exe_path: Optional[str]) -> tuple[str, str]:
    """根据运行形态决定快捷方式的目标与参数。

    - 显式传入 exe_path：直接用（常用于打包后指定 exe）；
    - frozen：指向当前 exe（sys.executable），无参数；
    - 源码模式：指向 pythonw.exe（无控制台窗口），参数为 run_gui.pyw 绝对路径。

    Args:
        exe_path: 调用方显式指定的目标路径。

    Returns:
        (target, args) 二元组。
    """
    if exe_path:
        return str(exe_path), ""
    if paths.is_frozen():
        return sys.executable, ""
    target = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    args = os.path.join(paths.app_base_dir(), "run_gui.pyw")
    return target, args


def create_shortcut(
    exe_path: Optional[str] = None,
    name: str = DEFAULT_DESCRIPTION,
    runner: Optional[Callable[..., Any]] = None,
) -> Optional[str]:
    """在桌面创建 .lnk 快捷方式。

    Args:
        exe_path: 目标程序路径；None 时自动选择（frozen→exe，源码→pythonw）。
        name: 快捷方式名称（不含 .lnk 后缀）。
        runner: 可注入的 subprocess.run 替身（便于单测）。

    Returns:
        成功返回 .lnk 完整路径；失败返回 None。
    """
    if not supported():
        # macOS / Linux：无 .lnk 语义，显式提示（GUI 侧已隐藏该按钮）
        logger.warning("当前平台不支持创建桌面快捷方式（仅 Windows）：%s", sys.platform)
        return None

    desktop = desktop_dir()
    try:
        os.makedirs(desktop, exist_ok=True)
    except OSError as exc:
        logger.warning("无法访问桌面目录 %s：%s", desktop, exc)
        return None

    lnk = os.path.join(desktop, f"{name}.lnk")
    target, args = _default_target_and_args(exe_path)
    workdir = os.path.dirname(target) if target else paths.app_base_dir()
    icon = paths.resource_path("icon.ico")
    if not os.path.isfile(icon):
        icon = ""  # 无图标文件时不设置 IconLocation

    script = build_powershell_script(
        target=target,
        args=args,
        workdir=workdir,
        lnk=lnk,
        icon=icon,
        description=DEFAULT_DESCRIPTION,
    )

    run = runner if runner is not None else subprocess.run
    try:
        run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:  # noqa: BLE001 - 创建失败返回 None
        logger.warning("创建桌面快捷方式失败：%s", exc)
        return None

    if not os.path.isfile(lnk):
        logger.warning("快捷方式未生成：%s", lnk)
        return None

    logger.info("已创建桌面快捷方式：%s", lnk)
    return lnk
