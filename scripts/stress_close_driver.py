"""关闭压力测试 —— 子进程驱动（v3.5）。

启动一个真实 Tk 窗口 → 跑若干轮 mock 监控 → 定时调用 `on_close()` 关闭。
父进程（stress_close_test.py）负责反复 spawn 本脚本并验证进程干净退出。

用法：
    python stress_close_driver.py <config.yaml> [run_seconds] [--no-worker]

`--no-worker`：不启动监控线程，仅 GUI 空闲（用于单独测量 _poll_queue / _tick
轮询的挂机 CPU 消耗）。

输出标记（供父进程解析）：
    [STRESS] START
    [STRESS] CLOSED
    [STRESS] EXIT <code>
"""

from __future__ import annotations

import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def main() -> int:
    """驱动主体：建窗 → （可选）开监控 → 定时关闭 → 验证退出。"""
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    run_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    no_worker = "--no-worker" in sys.argv[3:]

    import tkinter as tk

    import xianyu_alert.gui as gui_mod

    # 关闭确认框自动同意（测试环境不阻塞）
    gui_mod.messagebox.askyesno = lambda *a, **k: True  # type: ignore[assignment]

    print("[STRESS] START", flush=True)
    start = time.monotonic()
    root = tk.Tk()
    try:
        gui = gui_mod.XianyuAlertGUI(root, config_path=config_path)
    except Exception as exc:  # noqa: BLE001 - 构造失败直接退出并报错
        print(f"[STRESS] GUI_INIT_ERROR {exc!r}", flush=True)
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        return 2

    if not no_worker:
        # 启动循环监控（mock 抓取器，间隔由 config 控制）
        try:
            gui._launch_worker(single_round=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[STRESS] WORKER_START_ERROR {exc!r}", flush=True)
            gui.on_close()
            return 3

    # 延迟后触发关闭流程（真实走 on_close：stop_event → join → 取消 after → destroy）
    root.after(int(run_seconds * 1000), gui.on_close)
    root.mainloop()

    elapsed = time.monotonic() - start
    print(f"[STRESS] CLOSED elapsed={elapsed:.2f}s", flush=True)

    # 关闭后确认所有后台线程均退出（监控线程已被 join）
    alive = [t.name for t in __import__("threading").enumerate()
             if t is not __import__("threading").current_thread() and t.is_alive()]
    if alive:
        print(f"[STRESS] LEFTOVER_THREADS {alive}", flush=True)
    # 报告进程累计 CPU 时间（用户+系统，秒），用于挂机 CPU 稳定性判断
    cpu = sum(os.times()[:4])
    print(f"[STRESS] CPU_SECONDS {cpu:.3f}", flush=True)
    print("[STRESS] EXIT 0", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    sys.exit(main())
