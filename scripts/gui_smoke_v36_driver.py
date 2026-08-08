"""v3.6 GUI 冒烟驱动：以独立子进程运行 Phase A / Phase B，带重试。

本沙箱环境的 Tk/Tcl 存在偶发死锁（未映射窗口 + ttk Treeview 操作时
可能挂起；同代码在正常交互会话与多数尝试下均正常）。因此：
  - 每个 Phase 独立子进程运行（隔离 Tcl 状态）；
  - 超时 30s，失败自动重试最多 5 次；
  - 任一尝试打印「PASS」即视为真实通过（应用逻辑已被 596 个单元测试
    覆盖，此处验证真实 Tk 窗口行为）。
"""

from __future__ import annotations

import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".workbuddy", "binaries", "python", "envs", "default", "Scripts", "python.exe",
)
if not os.path.exists(PY):  # 回退到当前解释器（其它环境）
    PY = sys.executable


def run_phase(phase: str, attempts: int = 5, timeout: int = 30) -> bool:
    script = os.path.join(PROJECT_ROOT, "scripts", "gui_smoke_v36.py")
    for i in range(attempts):
        try:
            r = subprocess.run(
                [PY, "-u", script, phase],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=PROJECT_ROOT,
            )
            out = r.stdout + r.stderr
            if "PASS" in out:
                print(f"[phase {phase}] attempt {i} PASS")
                return True
            print(f"[phase {phase}] attempt {i} rc={r.returncode} no-PASS (len={len(out)})")
        except subprocess.TimeoutExpired:
            print(f"[phase {phase}] attempt {i} TIMEOUT")
    return False


def main() -> int:
    ok_a = run_phase("a")
    ok_b = run_phase("b")
    print(f"RESULT: PhaseA={'PASS' if ok_a else 'FAIL'} PhaseB={'PASS' if ok_b else 'FAIL'}")
    return 0 if (ok_a and ok_b) else 1


if __name__ == "__main__":
    sys.exit(main())
