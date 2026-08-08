"""打包 exe GUI 冒烟（v3.5）：启动 exe → 等 5s → 发 WM_CLOSE 优雅关闭 → 验证退出。

不依赖 PowerShell / psutil，仅用 ctypes 找主窗口并投递 WM_CLOSE，
从而触发 Tk 的 WM_DELETE_WINDOW → on_close（与用户点 X 完全等价）。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

WM_CLOSE = 0x0010
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(PROJECT_ROOT, "dist", "闲鱼低价提醒工具.exe")


def find_main_window_hwnd(pid: int) -> int:
    """按 PID 查找其主窗口句柄（Tk 主窗口）。"""
    result = {"hwnd": 0}

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    def callback(hwnd: int, _lparam: int) -> bool:
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        win_pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        if win_pid.value == pid:
            result["hwnd"] = hwnd
            return False  # 停止枚举
        return True

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)
    return result["hwnd"]


def find_exe_pids() -> list:
    """PyInstaller onefile 会有 bootloader 父进程 + 实际子进程，返回全部同名校验 PID。"""
    import re

    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {os.path.basename(EXE)}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    pids: list = []
    for line in result.stdout.splitlines():
        m = re.search(r"\"(\d+)\"", line)
        if m:
            pids.append(int(m.group(1)))
    return pids


def main() -> int:
    """执行冒烟流程。"""
    if not os.path.isfile(EXE):
        print(f"exe 不存在：{EXE}")
        return 1

    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
    print(f"1) 已启动 exe，PID={proc.pid}", flush=True)

    time.sleep(6)
    alive = proc.poll() is None
    print(f"2) 启动 6s 后启动进程存活：{alive}", flush=True)
    if not alive:
        print(f"   启动即崩溃，returncode={proc.returncode}")
        return 1

    # onefile：bootloader 父进程 + 实际子进程；对全部相关 PID 的可见主窗口投递 WM_CLOSE
    pids = find_exe_pids()
    windows: list = []
    for pid in pids:
        found = find_main_window_hwnd(pid)
        if found:
            windows.append((pid, found))
    print(f"3) exe 相关 PID={pids}，可见主窗口={windows}", flush=True)
    if not windows:
        print("   未找到主窗口")
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001
                pass
        return 1

    # 投递 WM_CLOSE → Tk WM_DELETE_WINDOW → on_close（优雅关闭路径）
    window_pids = [pid for pid, _hwnd in windows]
    for pid, hwnd in windows:
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    print("4) 已发送 WM_CLOSE，等待 GUI 子进程退出…", flush=True)

    # 关键断言：持有窗口的 GUI 子进程应在超时内优雅退出（应用代码可控的部分）。
    # PyInstaller onefile 的 bootloader 父进程负责清理临时目录，可能因
    # Defender/平台原因延迟数十秒后才退出（最小 hello-world onefile 同样如此），
    # 属平台行为而非应用泄漏 —— 由 observe_exe_exit.py 单独验证其最终退出。
    deadline = time.monotonic() + 15
    children_gone = False
    while time.monotonic() < deadline:
        time.sleep(0.5)
        remaining = find_exe_pids()
        if not any(pid in remaining for pid in window_pids):
            children_gone = True
            break
    if not children_gone:
        print("5) 15s 内 GUI 子进程未退出 —— 关闭卡死！", flush=True)
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001
                pass
        return 1
    print(f"5) GUI 子进程优雅退出 ✅（bootloader 父进程清理临时目录可能需数十秒，属平台行为）",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
