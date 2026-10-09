"""关闭压力测试 —— 父进程运行器（v3.5）。

反复「启动 GUI → 跑 1-2 轮监控（mock）→ 关闭」≥5 次，每次验证：
    1. 子进程正常退出（returncode == 0）；
    2. 退出后进程不残留（PID 已消失）；
    3. 无异常堆栈 / 无 LEFTOVER_THREADS 标记；
    4. 单次「启动→关闭」耗时在可接受范围内（不卡死）。

用法：
    python scripts/stress_close_test.py [iterations] [run_seconds]

返回码：0 = 全部通过；非 0 = 存在失败项。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import yaml

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DRIVER = os.path.join(_PROJECT_ROOT, "scripts", "stress_close_driver.py")
PYTHON = sys.executable

#: 每次启停最多允许的秒数（远超正常耗时，用于判定「卡死」）
ITERATION_TIMEOUT = 30.0
#: 默认迭代次数（≥5）
DEFAULT_ITERATIONS = 5
#: 每次运行（打开到关闭）的秒数
DEFAULT_RUN_SECONDS = 5.0

MOCK_CONFIG = {
    "keywords": [{"keyword": "Switch", "max_price": 1000}],
    "monitor": {"interval_seconds": 2, "user_agent": "", "cookies": ""},
    "fetcher": {"type": "mock", "mock_products_per_round": 5, "mock_fail_rounds": []},
    "storage": {"path": ":memory:"},
    "notify": {"channels": [{"type": "console"}]},
    "preset_exclude_keywords": ["回收", "置换", "收购", "高价回收", "收"],
}


def pid_exists(pid: int) -> bool:
    """Windows 下用 tasklist 判断进程是否还存在。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return str(pid) in result.stdout
    except Exception:  # noqa: BLE001 - 查询失败按不存在处理
        return False


def run_one_iteration(index: int, run_seconds: float, tmpdir: str) -> dict:
    """执行一次「启动 → 运行 → 关闭」迭代，返回结果统计。"""
    config_path = os.path.join(tmpdir, f"config_{index}.yaml")
    with open(config_path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(MOCK_CONFIG, fp, allow_unicode=True, sort_keys=False)

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [PYTHON, _DRIVER, config_path, str(run_seconds)],
            cwd=_PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        return {"index": index, "ok": False, "reason": f"spawn失败: {exc!r}", "elapsed": 0.0}

    try:
        out, err = proc.communicate(timeout=ITERATION_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate(timeout=10)
        return {
            "index": index,
            "ok": False,
            "reason": f"超时({ITERATION_TIMEOUT}s)未退出 —— 疑似关闭卡死",
            "elapsed": time.monotonic() - started,
            "out": out,
            "err": err,
        }

    elapsed = time.monotonic() - started
    pid = proc.pid
    # 进程已退出后再确认 PID 不再存活（无残留）
    time.sleep(0.5)
    leftover = pid_exists(pid)

    problems: list = []
    if proc.returncode != 0:
        problems.append(f"returncode={proc.returncode}")
    if "Traceback" in err or "Traceback" in out:
        problems.append("存在异常堆栈")
    if "[STRESS] CLOSED" not in out:
        problems.append("缺少 CLOSED 标记")
    if leftover:
        problems.append("进程残留（PID 仍存活）")
    if "[STRESS] LEFTOVER_THREADS" in out:
        problems.append("存在未退出子线程")

    return {
        "index": index,
        "ok": not problems,
        "reason": "; ".join(problems) if problems else "OK",
        "elapsed": elapsed,
        "out": out,
        "err": err,
    }


def main() -> int:
    """运行全部迭代并汇总结果。"""
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ITERATIONS
    run_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_RUN_SECONDS
    iterations = max(1, iterations)

    print(f"关闭压力测试：{iterations} 次迭代，每次运行 {run_seconds}s，超时 {ITERATION_TIMEOUT}s")
    print(f"Python: {PYTHON}")
    print(f"驱动:   {_DRIVER}\n")

    results = []
    with tempfile.TemporaryDirectory(prefix="xianyu_stress_") as tmpdir:
        for i in range(1, iterations + 1):
            result = run_one_iteration(i, run_seconds, tmpdir)
            results.append(result)
            status = "PASS" if result["ok"] else "FAIL"
            print(f"  [{i}/{iterations}] {status}  elapsed={result['elapsed']:.2f}s  {result['reason']}")
            if not result["ok"] and result.get("err"):
                print("      stderr 尾部:", result["err"][-500:].replace("\n", " | "))

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    elapsed_list = [r["elapsed"] for r in results]
    print(f"\n结果：{passed}/{total} 通过")
    if elapsed_list:
        print(f"单次耗时：min={min(elapsed_list):.2f}s  max={max(elapsed_list):.2f}s  "
              f"avg={sum(elapsed_list) / len(elapsed_list):.2f}s")
    return 0 if passed == total else 1


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    sys.exit(main())
