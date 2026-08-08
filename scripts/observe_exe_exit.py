"""观察：exe 子进程优雅关闭后，bootloader 父进程是否最终退出。"""
import ctypes
import os
import re
import subprocess
import sys
import time

WM_CLOSE = 0x0010
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(PROJECT_ROOT, "dist", "闲鱼低价提醒工具.exe")
user32 = ctypes.windll.user32


def find_exe_pids() -> list:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {os.path.basename(EXE)}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
    ).stdout
    pids = []
    for line in out.splitlines():
        m = re.search(r"\"(\d+)\"", line)
        if m:
            pids.append(int(m.group(1)))
    return pids


def enum_windows(pid: int) -> list:
    windows = []
    Proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lp):
        win_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        if win_pid.value == pid:
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            windows.append((hwnd, buf.value, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(Proc(cb), 0)
    return windows


def main() -> int:
    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
    time.sleep(6)
    pids_before = find_exe_pids()
    print("启动后 exe PID:", pids_before, flush=True)

    # 找到 GUI 子进程主窗口并发 WM_CLOSE
    closed = 0
    for pid in pids_before:
        for hwnd, title, visible in enum_windows(pid):
            if visible and title:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                print(f"→ WM_CLOSE hwnd={hwnd} title={title!r}", flush=True)
                closed += 1
    if not closed:
        print("未找到可关闭窗口", flush=True)
        for pid in pids_before:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        return 1

    # 每 2s 观察一次，最长 60s
    for i in range(30):
        time.sleep(2)
        pids_now = find_exe_pids()
        print(f"t={2*(i+1):>3}s 剩余 PID: {pids_now}", flush=True)
        if not pids_now:
            print("全部进程已退出 ✅", flush=True)
            return 0
    print("仍有进程残留 ❌", flush=True)
    for pid in pids_now:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
    return 1


if __name__ == "__main__":
    sys.exit(main())
