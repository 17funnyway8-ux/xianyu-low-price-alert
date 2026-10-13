"""公开仓库「真实凭据字面量」守卫（v1.11.10）。

本仓库是**公开**的：任何跟踪进仓库的字面量都会被 GitHub 代码搜索、爬虫、镜像站收走。
v1.11.10 的教训是一个测试夹具把真实 Cookie 里的用户 ID 字段当样本写死了
（v1.10.2 引入，直到本轮隐私排查才发现）。它不是登录凭据（拿它登不进去任何账号），
但会把公开仓库与真实闲鱼账号关联起来 —— 对一个要「安静地盯着平台」的账号来说，
这种关联本身就是风险，而且事后很难彻底收回（标签、PR 引用、缓存都会留着）。

因此这里做**机械扫描**，不靠人眼：

    扫描范围：git 跟踪的全部文件（== 会被推上 GitHub 的全部内容）；
    判定方式：只认「真实凭据的形状」，不认具体值 —— 守卫自己绝不能变成新的泄漏点；
    失败输出：只给 文件:行号 + 字段名 + 脱敏前缀 + 长度，绝不把命中的值再写进公开日志。

夹具（tests/ 下的一切假 Cookie）请一律使用 abc / xyz / 1 / 2 / 9 / 123 这类明显占位值。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: (标签, 正则)。正则里只有「形状」，没有任何真实值。
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("真实用户 ID（unb）", re.compile(r"\bunb=(\d{6,})")),
    ("真实会话凭据（cookie2）", re.compile(r"\bcookie2=([0-9a-fA-F]{16,})")),
    ("真实签名令牌（_m_h5_tk）", re.compile(r"\b_m_h5_tk=([0-9a-fA-F]{16,})_\d{10,}")),
    ("真实 Havana 凭据", re.compile(r"\bhavana_lgc2_77=([A-Za-z0-9%+/=_-]{10,})")),
    ("真实登录态（sgcookie）", re.compile(r"\bsgcookie=([A-Za-z0-9%+/=_-]{10,})")),
    ("真实登录态（tfstk）", re.compile(r"\btfstk=([A-Za-z0-9%+/=_-]{10,})")),
    ("凭据密文", re.compile(r"\b(?:fernet1|dpapi1):([A-Za-z0-9_+/=-]{60,})")),
    ("GitHub 令牌", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def tracked_files() -> list[str]:
    """git 跟踪的文件列表（即所有会被推送到公开仓库的路径）。"""
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def mask(value: str) -> str:
    """把命中值脱敏成「前 2 位 + 掩码 + 末位（长度）」，避免二次泄漏。"""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 3)}{value[-1]}（{len(value)} 位）"


def hit_value(match: re.Match[str]) -> str:
    """取命中值：有捕获组就用最后一个，没有则退化为整段。

    私钥与 GitHub 令牌两条规则没有捕获组 —— 早期版本直接取 group(1)，
    结果扫描自己人时抛 IndexError（守卫先崩，等于没守卫）。
    """
    return match.groups()[-1] if match.groups() else match.group(0)


def scan_tracked_files() -> list[str]:
    """返回形如 path:line 的命中列表（只含脱敏信息）。"""
    findings: list[str] = []
    for rel in tracked_files():
        path = os.path.join(ROOT, rel)
        try:
            raw = open(path, "rb").read()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:  # 二进制（图片 / 图标）不参与文本扫描
            continue
        text = raw.decode("utf-8", "replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    findings.append(f"{rel}:{lineno} {label} -> {mask(hit_value(match))}")
    return findings


class TestPublicPrivacyGuard(unittest.TestCase):
    """公开仓库不得出现真实凭据字面量。"""

    def test_tracked_files_contain_no_real_credentials(self) -> None:
        findings = scan_tracked_files()
        self.assertEqual(
            findings,
            [],
            "git 跟踪的文件里出现了真实凭据形状（公开仓库 = 等于公开）：\n"
            + "\n".join(findings),
        )

    def test_scan_is_not_vacuous(self) -> None:
        """守卫必须真的扫到了文件，否则「零命中」毫无意义。"""
        self.assertGreater(len(tracked_files()), 200)

    def test_patterns_actually_match_real_shapes(self) -> None:
        """反向验证：每条规则都能抓住对应的真实形状。

        样本在运行期拼接（而不是写成完整字面量），否则守卫自己就会命中自己 ——
        这正是本文件开头说的「守卫不能变成新的泄漏点」。
        """
        samples = {
            "真实用户 ID（unb）": "unb=" + "9" * 9,
            "真实会话凭据（cookie2）": "cookie2=" + "a1b2c3d4" * 4,
            "真实签名令牌（_m_h5_tk）": "_m_h5_tk=" + "f" * 32 + "_" + "1" * 13,
            "真实 Havana 凭据": "havana_lgc2_77=" + "A" * 24,
            "真实登录态（sgcookie）": "sgcookie=" + "B" * 24,
            "真实登录态（tfstk）": "tfstk=" + "C" * 24,
            "凭据密文": "fernet1:" + "D" * 80,
            "GitHub 令牌": "ghp_" + "E" * 30,
            # 必须运行期拼接：整条字面量写进本文件会当场被自己的规则命中
            # （第一次跑就是被这条抓出来的 —— 守卫写成字面量 = 守卫自己违规）
            "私钥": "-----BEGIN " + "RSA " + "PRIVATE KEY-----",
        }
        for label, pattern in PATTERNS:
            with self.subTest(label=label):
                self.assertIn(label, samples)
                found = pattern.search(samples[label])
                self.assertIsNotNone(found, f"规则「{label}」漏掉了真实形状的样本")
                assert found is not None
                # 命中值必须取得到 —— 否则扫描到真实凭据时会先抛异常（守卫失效）
                self.assertTrue(hit_value(found), f"规则「{label}」取不出命中值")

    def test_mask_never_reveals_full_value(self) -> None:
        self.assertEqual(mask("ab"), "**")
        self.assertEqual(mask("1234567890"), "12*******0（10 位）")


if __name__ == "__main__":
    unittest.main()
