"""Cookie 池纯函数测试（v1.10.10，M15 + M16 共用逻辑）。

背景：Tk 的「Cookie 管理」对话框（`on_manage_cookies`，367 行）与 Qt 的 `CookieDialog`
各自实现了一遍「切换启用 / 停用过期 / 删除 / 增改」，**逻辑重复且都埋在控件回调里**：
既难测（要 Tk / Qt 环境），也容易两边跑偏 —— 事实上本轮就发现两版的「自动停用过期项」
判定标准不一样（Tk 用 detect_cookie_health，Qt 用 cookie_prefers_rotation）。

抽成纯函数后：不碰控件、**不改入参**、返回新列表，因此可以完全离线断言，
两个 GUI 也共享同一套语义。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.gui import (  # noqa: E402
    pool_delete_entry,
    pool_disable_indexes,
    pool_expired_indexes,
    pool_summary,
    pool_toggle_entry,
    pool_upsert_entry,
)

VALID = "cookie2=abc; unb=1; _m_h5_tk=tok_1791559226130"


def make_pool() -> list[dict]:
    return [
        {"name": "主号", "cookie": VALID, "enabled": True},
        {"name": "小号", "cookie": "cookie2=def", "enabled": False},
    ]


class TestPoolToggle(unittest.TestCase):
    def test_switches_enabled_state(self) -> None:
        pool = make_pool()
        before_second = pool[1]["enabled"]
        after = pool_toggle_entry(pool, 0)
        self.assertFalse(after[0]["enabled"])
        self.assertEqual(after[1]["enabled"], before_second, "只切换目标条目")

    def test_twice_restores(self) -> None:
        pool = make_pool()
        self.assertTrue(pool_toggle_entry(pool_toggle_entry(pool, 0), 0)[0]["enabled"])

    def test_does_not_mutate_input(self) -> None:
        pool = make_pool()
        pool_toggle_entry(pool, 0)
        self.assertTrue(pool[0]["enabled"], "纯函数不得改动入参")

    def test_out_of_range_is_noop(self) -> None:
        pool = make_pool()
        for bad in (-1, 99):
            self.assertEqual(pool_toggle_entry(pool, bad), pool, bad)

    def test_missing_enabled_defaults_to_true_then_false(self) -> None:
        after = pool_toggle_entry([{"name": "x", "cookie": VALID}], 0)
        self.assertFalse(after[0]["enabled"], "缺省视为启用，切换后为停用")

    def test_preserves_other_fields(self) -> None:
        after = pool_toggle_entry([{"name": "n", "cookie": "c", "extra": 1}], 0)
        self.assertEqual(after[0]["extra"], 1)


class TestPoolExpired(unittest.TestCase):
    def test_detects_invalid_entries(self) -> None:
        """判定口径（v1.9 四层模型）：

        - 缺 _m_h5_tk（令牌层）→ **可自愈**，不算需要停用；
        - 连登录态都没有（空串）→ 服务端大概率会拒，才算需要停用。
        """
        pool = [
            {"cookie": VALID},
            {"cookie": "cookie2=def"},
            {"cookie": ""},
        ]
        self.assertEqual(pool_expired_indexes(pool), [2])

    def test_valid_token_is_not_expired(self) -> None:
        """令牌过期可自愈，不算「需要停用」（v1.9 四层模型）。"""
        self.assertNotIn(0, pool_expired_indexes([{"cookie": VALID}]))

    def test_empty_pool(self) -> None:
        self.assertEqual(pool_expired_indexes([]), [])


class TestPoolDisable(unittest.TestCase):
    def test_disables_selected_and_keeps_entries(self) -> None:
        after = pool_disable_indexes(make_pool(), [0, 1])
        self.assertEqual(len(after), 2, "停用不删除条目")
        self.assertEqual([e["enabled"] for e in after], [False, False])

    def test_ignores_out_of_range(self) -> None:
        after = pool_disable_indexes(make_pool(), [99])
        self.assertTrue(after[0]["enabled"])

    def test_does_not_mutate_input(self) -> None:
        pool = make_pool()
        pool_disable_indexes(pool, [0])
        self.assertTrue(pool[0]["enabled"])


class TestPoolDelete(unittest.TestCase):
    def test_deletes_entry(self) -> None:
        after = pool_delete_entry(make_pool(), 0)
        self.assertEqual([e["name"] for e in after], ["小号"])

    def test_out_of_range_is_noop(self) -> None:
        pool = make_pool()
        self.assertEqual(pool_delete_entry(pool, 99), pool)
        self.assertEqual(pool_delete_entry(pool, -1), pool)

    def test_does_not_mutate_input(self) -> None:
        pool = make_pool()
        pool_delete_entry(pool, 0)
        self.assertEqual(len(pool), 2)


class TestPoolUpsert(unittest.TestCase):
    def test_appends_when_index_none(self) -> None:
        after = pool_upsert_entry(make_pool(), {"name": "新", "cookie": VALID})
        self.assertEqual(len(after), 3)
        self.assertEqual(after[-1]["name"], "新")

    def test_replaces_at_index(self) -> None:
        after = pool_upsert_entry(make_pool(), {"name": "改", "cookie": VALID}, 1)
        self.assertEqual(after[1]["name"], "改")
        self.assertEqual(len(after), 2, "替换不应改变长度")

    def test_out_of_range_appends(self) -> None:
        after = pool_upsert_entry(make_pool(), {"name": "新", "cookie": VALID}, 99)
        self.assertEqual(len(after), 3)

    def test_copies_entry(self) -> None:
        entry = {"name": "新", "cookie": VALID}
        after = pool_upsert_entry([], entry)
        entry["name"] = "被外部改了"
        self.assertEqual(after[0]["name"], "新", "外部后续修改不应穿透进池")

    def test_does_not_mutate_input(self) -> None:
        pool = make_pool()
        pool_upsert_entry(pool, {"name": "新", "cookie": VALID})
        self.assertEqual(len(pool), 2)


class TestPoolSummary(unittest.TestCase):
    def test_counts(self) -> None:
        summary = pool_summary(make_pool())
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["enabled"], 1)
        self.assertEqual(summary["disabled"], 1)
        self.assertEqual(summary["expired"], 0, "缺令牌属可自愈，不算过期")

    def test_empty_pool(self) -> None:
        self.assertEqual(
            pool_summary([]), {"total": 0, "enabled": 0, "disabled": 0, "expired": 0}
        )


if __name__ == "__main__":
    unittest.main()
