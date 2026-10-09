"""运维脚本自测（v1.10.11，M24 补强）。

脚本此前**完全没有自测**：加了新脚本没人登记、文档检查器坏了也没人知道。
本文件做三件具体的事：
    1. 文档检查器真的能跑通（subprocess 级 smoke，退出码 0）；
    2. **scripts/ 下的每个脚本都必须在 scripts/README.md 里登记** ——
       这条会真正咬人：以后新增脚本忘了写文档，测试直接红；
    3. 统一入口 run.sh 的契约（可执行、未知子命令给提示并非 0）；
    4. e2e 脚本的计数逻辑（Report.check 的通过与失败统计）单测。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
PY = sys.executable


def script_files() -> list[str]:
    """scripts/ 下需要登记的文件（排除归档目录与私有文件）。"""
    names = []
    for name in sorted(os.listdir(SCRIPTS)):
        if name.startswith("_") or name.startswith("."):
            continue
        if name.endswith((".sh", ".py")):
            names.append(name)
    return names


class TestCheckDocs(unittest.TestCase):
    """文档检查器必须可运行且当前仓库是干净的。"""

    def test_runs_clean(self) -> None:
        proc = subprocess.run(
            [PY, os.path.join(SCRIPTS, "check_docs.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, f"文档检查器应通过：\n{proc.stdout[-400:]}{proc.stderr[-400:]}")
        self.assertIn("0 项问题", proc.stdout + proc.stderr)


class TestScriptRegistry(unittest.TestCase):
    """每个脚本都要在 scripts/README.md 里登记（否则新增脚本会静默失联）。"""

    def test_readme_exists(self) -> None:
        self.assertTrue(os.path.isfile(os.path.join(SCRIPTS, "README.md")))

    def test_every_script_is_documented(self) -> None:
        readme = open(os.path.join(SCRIPTS, "README.md"), encoding="utf-8").read()
        undocumented = [name for name in script_files() if name not in readme]
        self.assertEqual(undocumented, [], f"以下脚本未在 scripts/README.md 登记：{undocumented}")

    def test_registry_covers_shell_and_python(self) -> None:
        names = script_files()
        self.assertTrue(any(n.endswith(".sh") for n in names), "至少应有一个 shell 脚本")
        self.assertTrue(any(n.endswith(".py") for n in names), "至少应有一个 Python 脚本")


class TestRunEntrypoint(unittest.TestCase):
    """统一入口 run.sh 的契约。"""

    def setUp(self) -> None:
        self.run_sh = os.path.join(SCRIPTS, "run.sh")
        if not os.path.isfile(self.run_sh):  # pragma: no cover - 仓库必然有
            self.skipTest("run.sh 不存在")

    def test_is_executable(self) -> None:
        self.assertTrue(os.access(self.run_sh, os.X_OK), "run.sh 应可执行")

    def test_unknown_subcommand_is_rejected(self) -> None:
        proc = subprocess.run(
            ["bash", self.run_sh, "绝不存在子命令"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotEqual(proc.returncode, 0, "未知子命令必须是非 0")
        self.assertTrue((proc.stdout + proc.stderr).strip(), "未知子命令应给出可读提示")


class TestE2eReport(unittest.TestCase):
    """e2e 计数逻辑（避免"失败了却报成功"）。"""

    def _report(self):
        sys.path.insert(0, SCRIPTS)
        import e2e_web_smoke

        return e2e_web_smoke.Report()

    def test_success_and_failure_counting(self) -> None:
        report = self._report()
        report.check("ok1", True)
        report.check("ok2", True)
        report.check("bad", False, "detail")
        self.assertEqual(len(report.rows), 3)
        self.assertEqual(len(report.failed), 1, "failed 是属性，返回失败行列表")

    def test_failure_records_detail(self) -> None:
        report = self._report()
        report.check("bad", False, "细节")
        self.assertEqual(len(report.failed), 1)
        self.assertIn("细节", str(report.failed))

    def test_launcher_template_has_placeholders(self) -> None:
        sys.path.insert(0, SCRIPTS)
        import e2e_web_smoke

        # 模板里 root / data 用 !r 形式注入（repr，自动加引号）
        for key in ("{root!r}", "{data!r}", "{port}"):
            self.assertIn(key, e2e_web_smoke.LAUNCHER, f"启动模板缺少 {key}")


if __name__ == "__main__":
    unittest.main()
