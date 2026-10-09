"""开机自启：三平台统一接口。

为什么需要这个模块
------------------
改造前，"开机自启"是**三套互不相干的东西**：

    Windows  桌面快捷方式（shortcut.py，指向桌面而非启动文件夹，其实并不自启）
    macOS    模板 plist + 手动脚本 scripts/install_launchagent.sh（要用户自己看文档敲命令）
    Linux    什么都没有

于是同一个诉求（"开机自动跑起来"）在三平台体验完全不同，模板与代码还会各自漂移。
本模块把三平台收敛到一套接口（status / enable / disable），GUI、CLI、脚本共用同一份逻辑：

    macOS    LaunchAgent（~/Library/LaunchAgents/com.xianyu-alert.gui.plist + launchctl）
    Linux    systemd --user（~/.config/systemd/user/xianyu-alert.service + systemctl --user）
    Windows  启动文件夹快捷方式（%APPDATA%.../Startup/*.lnk）

所有外部命令都经过可注入的 runner，文件路径都基于可注入的 home，
因此**三个平台的行为都能在任意一台机器上离线测到**（见 tests/test_autostart.py）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PLATFORM_MACOS = "macos"
PLATFORM_LINUX = "linux"
PLATFORM_WINDOWS = "windows"
PLATFORM_UNSUPPORTED = "unsupported"

#: macOS LaunchAgent 标签（与既有模板保持一致，便于平滑升级）
LAUNCH_AGENT_LABEL = "com.xianyu-alert.gui"
#: Linux systemd --user 单元名
SYSTEMD_UNIT_NAME = "xianyu-alert.service"
#: Windows 启动文件夹里的快捷方式名
WINDOWS_LNK_NAME = "闲鱼低价提醒工具.lnk"

#: 默认 runner（可注入替身以便单测）
Runner = Callable[..., Any]


@dataclass(frozen=True)
class AutostartStatus:
    """开机自启的状态快照（三平台统一形状）。

    Attributes:
        platform: macos / linux / windows / unsupported。
        mechanism: 机制的短名（LaunchAgent / systemd --user / 启动文件夹）。
        supported: 当前平台是否支持。
        enabled: 是否已启用。
        path: 落地文件路径（plist / unit / lnk）。
        detail: 人类可读说明。
        hint: 未启用或不支持时的下一步建议。
    """

    platform: str
    mechanism: str
    supported: bool
    enabled: bool
    path: str = ""
    detail: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（CLI --json / Web API 复用）。"""
        return {
            "platform": self.platform,
            "mechanism": self.mechanism,
            "supported": self.supported,
            "enabled": self.enabled,
            "path": self.path,
            "detail": self.detail,
            "hint": self.hint,
        }


def platform_key(platform: str | None = None) -> str:
    """把 sys.platform 归一化成 macos / linux / windows / unsupported。"""
    raw = (platform or sys.platform).lower()
    if raw.startswith("darwin"):
        return PLATFORM_MACOS
    if raw.startswith("linux"):
        return PLATFORM_LINUX
    if raw.startswith("win"):
        return PLATFORM_WINDOWS
    return PLATFORM_UNSUPPORTED


def home_dir(home: str | None = None) -> str:
    """返回用户主目录（测试可注入）。"""
    return os.path.expanduser(home or "~")


def launch_agent_path(home: str | None = None) -> str:
    """macOS LaunchAgent plist 的落地路径。"""
    return os.path.join(home_dir(home), "Library", "LaunchAgents", LAUNCH_AGENT_LABEL + ".plist")


def systemd_unit_path(home: str | None = None) -> str:
    """Linux user 单元文件的落地路径。"""
    return os.path.join(home_dir(home), ".config", "systemd", "user", SYSTEMD_UNIT_NAME)


def windows_startup_dir(home: str | None = None, env: dict[str, str] | None = None) -> str:
    """Windows 启动文件夹路径（优先 %APPDATA%，回退到 ~/AppData/Roaming）。"""
    environment = env if env is not None else os.environ
    appdata = environment.get("APPDATA") or os.path.join(home_dir(home), "AppData", "Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def gui_command(exe_path: str | None = None) -> list[str]:
    """自启时要执行的命令（打包形态指向可执行文件，源码形态走 python -m）。"""
    if exe_path:
        return [str(exe_path)]
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        return [sys.executable]
    return [sys.executable, "-m", "xianyu_alert.gui"]


def build_launch_agent_plist(command: Sequence[str], home: str | None = None) -> str:
    """生成 LaunchAgent plist（登录后启动；异常退出自动拉起）。

    KeepAlive.SuccessfulExit=false 的语义：**崩溃会被拉起，用户主动退出不会**，
    避免"关不掉的守护进程"这种糟糕体验。
    """
    args_xml = "".join(chr(10) + "        <string>" + _xml_escape(a) + "</string>" for a in command)
    data_dir = os.path.join(home_dir(home), "Library", "Application Support", "闲鱼低价提醒工具")
    log_dir = os.path.join(home_dir(home), "Library", "Logs", "xianyu-alert")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>' + chr(10)
        + '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' + chr(10)
        + '<plist version="1.0">' + chr(10)
        + '<dict>' + chr(10)
        + '    <key>Label</key>' + chr(10) + '    <string>' + LAUNCH_AGENT_LABEL + '</string>' + chr(10)
        + '    <key>ProgramArguments</key>' + chr(10) + '    <array>' + args_xml + chr(10) + '    </array>' + chr(10)
        + '    <key>RunAtLoad</key>' + chr(10) + '    <true/>' + chr(10)
        + '    <key>KeepAlive</key>' + chr(10) + '    <dict>' + chr(10)
        + '        <key>SuccessfulExit</key>' + chr(10) + '        <false/>' + chr(10)
        + '    </dict>' + chr(10)
        + '    <key>ProcessType</key>' + chr(10) + '    <string>Interactive</string>' + chr(10)
        + '    <key>EnvironmentVariables</key>' + chr(10) + '    <dict>' + chr(10)
        + '        <key>XY_DATA_DIR</key>' + chr(10) + '        <string>' + _xml_escape(data_dir) + '</string>' + chr(10)
        + '    </dict>' + chr(10)
        + '    <key>StandardOutPath</key>' + chr(10) + '    <string>' + _xml_escape(os.path.join(log_dir, "launchagent.out.log")) + '</string>' + chr(10)
        + '    <key>StandardErrorPath</key>' + chr(10) + '    <string>' + _xml_escape(os.path.join(log_dir, "launchagent.err.log")) + '</string>' + chr(10)
        + '</dict>' + chr(10) + '</plist>' + chr(10)
    )


def build_systemd_unit(command: Sequence[str], home: str | None = None) -> str:
    """生成 systemd --user 单元（登录后启动；崩溃拉起但正常退出不拉起）。

    用 systemd --user（而不是系统级）是关键：**不需要 root**，
    与 macOS 的 LaunchAgent、Windows 的启动文件夹语义对齐（都随用户会话）。
    """
    exec_start = " ".join(_systemd_quote(a) for a in command)
    return (
        "[Unit]" + chr(10)
        + "Description=闲鱼低价提醒工具（低价监控）" + chr(10)
        + "After=network-online.target" + chr(10) + chr(10)
        + "[Service]" + chr(10)
        + "Type=simple" + chr(10)
        + "ExecStart=" + exec_start + chr(10)
        + "Restart=on-failure" + chr(10)
        + "RestartSec=5" + chr(10)
        + "WorkingDirectory=" + _systemd_quote(home_dir(home)) + chr(10) + chr(10)
        + "[Install]" + chr(10)
        + "WantedBy=default.target" + chr(10)
    )


def _xml_escape(value: str) -> str:
    """XML 文本转义（路径里可能有 & < >）。"""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _systemd_quote(value: str) -> str:
    """systemd 参数引用：含空格时用双引号包裹并转义内部引号。"""
    text = str(value)
    if not text or " " in text or chr(9) in text:
        return '"' + text.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"') + '"'
    return text


def _run(runner: Runner, cmd: list[str]) -> tuple[int, str]:
    """执行外部命令，永不抛异常（自启配置失败不应该让主流程崩）。"""
    try:
        completed = runner(cmd, capture_output=True, text=True, check=False)
        code = int(getattr(completed, "returncode", 0) or 0)
        output = (getattr(completed, "stdout", "") or "") + (getattr(completed, "stderr", "") or "")
        return code, output.strip()
    except FileNotFoundError as exc:
        return 127, "命令不存在：" + str(exc)
    except Exception as exc:  # noqa: BLE001 - 兜底，配置自启是"尽力而为"
        return 1, str(exc)


def _write(path: str, content: str) -> None:
    """写入文件（自动建父目录）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(content)


def status(
    *,
    platform: str | None = None,
    home: str | None = None,
    runner: Runner | None = None,
) -> AutostartStatus:
    """查询当前平台的开机自启状态。

    runner 参数保留是为了与 enable/disable 保持一致的调用形状（本函数不做任何写操作）。
    """
    del runner
    kind = platform_key(platform)

    if kind == PLATFORM_MACOS:
        path = launch_agent_path(home)
        enabled = os.path.isfile(path)
        detail = "已安装 LaunchAgent（登录后自动启动，崩溃自动拉起）" if enabled else "未安装 LaunchAgent"
        return AutostartStatus(kind, "LaunchAgent", True, enabled, path, detail,
                               "" if enabled else "运行：xianyu-alert autostart enable")

    if kind == PLATFORM_LINUX:
        path = systemd_unit_path(home)
        enabled = os.path.isfile(path)
        detail = "已安装 systemd --user 单元（登录后自动启动）" if enabled else "未安装 systemd --user 单元"
        return AutostartStatus(kind, "systemd --user", True, enabled, path, detail,
                               "" if enabled else "运行：xianyu-alert autostart enable")

    if kind == PLATFORM_WINDOWS:
        path = os.path.join(windows_startup_dir(home), WINDOWS_LNK_NAME)
        enabled = os.path.isfile(path)
        detail = "已在启动文件夹创建快捷方式" if enabled else "启动文件夹中没有快捷方式"
        return AutostartStatus(kind, "启动文件夹", True, enabled, path, detail,
                               "" if enabled else "运行：xianyu-alert autostart enable")

    return AutostartStatus(kind, "-", False, False, "", "当前平台不支持开机自启", "可手动把启动命令写入系统的自启动项")


def enable(
    *,
    platform: str | None = None,
    home: str | None = None,
    runner: Runner | None = None,
    exe_path: str | None = None,
) -> AutostartStatus:
    """启用开机自启（三平台各自的机制）。

    Args:
        platform: 覆盖平台判定（测试用）。
        home: 覆盖主目录（测试用）。
        runner: 外部命令替身（测试用）。
        exe_path: 打包形态下的可执行文件路径。

    Returns:
        启用后的状态快照；失败时 enabled 为 False 且 detail 说明原因。
    """
    kind = platform_key(platform)
    run: Runner = runner or subprocess.run
    command = gui_command(exe_path)

    if kind == PLATFORM_MACOS:
        path = launch_agent_path(home)
        try:
            _write(path, build_launch_agent_plist(command, home))
        except OSError as exc:
            return AutostartStatus(kind, "LaunchAgent", True, False, path, "写入 plist 失败：" + str(exc))
        uid = str(os.getuid()) if hasattr(os, "getuid") else "501"
        _run(run, ["launchctl", "bootout", "gui/" + uid + "/" + LAUNCH_AGENT_LABEL])
        code, output = _run(run, ["launchctl", "bootstrap", "gui/" + uid, path])
        if code != 0:
            return AutostartStatus(kind, "LaunchAgent", True, True, path,
                                   "plist 已写入，但 launchctl 加载失败：" + output,
                                   "文件仍然保留，下次登录会生效；也可手动执行 launchctl bootstrap")
        return AutostartStatus(kind, "LaunchAgent", True, True, path, "已启用：登录后自动启动，崩溃自动拉起")

    if kind == PLATFORM_LINUX:
        path = systemd_unit_path(home)
        try:
            _write(path, build_systemd_unit(command, home))
        except OSError as exc:
            return AutostartStatus(kind, "systemd --user", True, False, path, "写入单元文件失败：" + str(exc))
        _run(run, ["systemctl", "--user", "daemon-reload"])
        code, output = _run(run, ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME])
        if code != 0:
            return AutostartStatus(kind, "systemd --user", True, True, path,
                                   "单元文件已写入，但 systemctl 启用失败：" + output,
                                   "在无 systemd 的环境（如 WSL1 / 容器）需改用其它方式")
        return AutostartStatus(kind, "systemd --user", True, True, path, "已启用：登录后自动启动")

    if kind == PLATFORM_WINDOWS:
        from .shortcut import _default_target_and_args, build_powershell_script

        startup = windows_startup_dir(home)
        lnk = os.path.join(startup, WINDOWS_LNK_NAME)
        target, args = _default_target_and_args(exe_path)
        script = build_powershell_script(target=target, args=args,
                                         workdir=os.path.dirname(target) or home_dir(home),
                                         lnk=lnk, description="闲鱼低价提醒工具（开机自启）")
        code, output = _run(run, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        if code != 0:
            return AutostartStatus(kind, "启动文件夹", True, False, lnk, "创建快捷方式失败：" + output)
        return AutostartStatus(kind, "启动文件夹", True, True, lnk, "已启用：登录后在启动文件夹中自动运行")

    return AutostartStatus(kind, "-", False, False, "", "当前平台不支持开机自启", "可手动把启动命令写入系统的自启动项")


def disable(
    *,
    platform: str | None = None,
    home: str | None = None,
    runner: Runner | None = None,
) -> AutostartStatus:
    """关闭开机自启（移除文件并注销注册）。"""
    kind = platform_key(platform)
    run: Runner = runner or subprocess.run
    del runner  # 仅通过 run 调用（保持签名一致，便于调用方统一传参）

    if kind == PLATFORM_MACOS:
        path = launch_agent_path(home)
        uid = str(os.getuid()) if hasattr(os, "getuid") else "501"
        _run(run, ["launchctl", "bootout", "gui/" + uid + "/" + LAUNCH_AGENT_LABEL])
        removed = _remove(path)
        return AutostartStatus(kind, "LaunchAgent", True, False, path,
                               "已关闭（已注销并删除 plist）" if removed else "本就未启用")

    if kind == PLATFORM_LINUX:
        path = systemd_unit_path(home)
        _run(run, ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
        removed = _remove(path)
        _run(run, ["systemctl", "--user", "daemon-reload"])
        return AutostartStatus(kind, "systemd --user", True, False, path,
                               "已关闭（已 disable 并删除单元文件）" if removed else "本就未启用")

    if kind == PLATFORM_WINDOWS:
        path = os.path.join(windows_startup_dir(home), WINDOWS_LNK_NAME)
        removed = _remove(path)
        return AutostartStatus(kind, "启动文件夹", True, False, path,
                               "已关闭（已删除启动文件夹快捷方式）" if removed else "本就未启用")

    return AutostartStatus(kind, "-", False, False, "", "当前平台不支持开机自启", "")


def _remove(path: str) -> bool:
    """删除文件；返回是否真的删除了。"""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("删除 %s 失败：%s", path, exc)
        return False
