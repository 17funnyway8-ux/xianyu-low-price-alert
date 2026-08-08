"""GUI 集成冒烟（v3.6）：真实 Tk 建窗 → 按钮拆分 → 窗口布局不溢出 →
慢 fetch 期间 UI 不卡（需求 3 复现验证）→ 黑名单 → 关闭。

沙箱说明：本环境（无交互桌面）在「未映射窗口上 update_idletasks 后再修改
Treeview」会触发 Tcl 硬崩溃，因此拆成两个阶段：
  Phase A：布局校验（建窗 → update_idletasks → 读请求宽度 → 销毁，不改树）
  Phase B：行为 + UI 不卡（全新建窗，全程隐藏，不 update_idletasks，
           用 root.update() 驱动事件循环；on_run_once() 等价点击「立即执行一轮」）
"""  # noqa: E501

from __future__ import annotations

import os
import sys
import tempfile
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402
from tkinter import ttk  # noqa: E402
from unittest import mock  # noqa: E402

import xianyu_alert.gui as g  # noqa: E402
from xianyu_alert.fetcher import MockFetcher  # noqa: E402
from xianyu_alert.storage import Storage  # noqa: E402

CFG = {
    "keywords": [{"keyword": "Switch", "max_price": 1000}],
    "monitor": {"interval_seconds": 600, "user_agent": "", "cookies": ""},
    "fetcher": {"type": "mock", "mock_products_per_round": 5, "mock_fail_rounds": []},
    "storage": {"path": ":memory:"},
    "notify": {"channels": [{"type": "console"}]},
    "preset_exclude_keywords": ["商家", "实体店"],
}


def write_cfg(d: str) -> str:
    cp = os.path.join(d, "config.yaml")
    with open(cp, "w", encoding="utf-8") as f:
        yaml.safe_dump(CFG, f, allow_unicode=True, sort_keys=False)
    return cp


def find_button(frame: "tk.Widget", text: str):
    for child in frame.winfo_children():
        if isinstance(child, ttk.Button) and str(child.cget("text")) == text:
            return child
    return None


def phase_a_layout(d: str) -> None:
    """Phase A：按钮存在 + 布局请求宽度不溢出（不改树）。"""
    root = tk.Tk()
    root.withdraw()
    gui = g.XianyuAlertGUI(root, config_path=write_cfg(d))
    root.update_idletasks()

    add_btn = find_button(gui.entry_row, "➕ 添加")
    update_btn = find_button(gui.entry_row, "✏️ 更新选中")
    assert add_btn is not None, "应存在「➕ 添加」按钮"
    assert update_btn is not None, "应存在「✏️ 更新选中」按钮"
    print("1) 按钮拆分: ➕ 添加 / ✏️ 更新选中 均存在")

    entry_req = gui.entry_row.winfo_reqwidth()
    window_req = root.winfo_reqwidth()
    # 默认窗口 1020 宽；输入行可用宽度约 968（notebook 16 + kw_frame 20 + pack 16）
    assert window_req <= 1020, f"窗口自然宽度 {window_req} 应 ≤1020"
    assert entry_req <= 968, f"关键词输入行请求宽度 {entry_req} 应 ≤968（默认窗口内不溢出）"
    print(f"2) 布局: 窗口请求宽 {window_req}, 输入行请求宽 {entry_req} ≤ 968 未溢出")
    print("PHASE_A PASS")

    root.destroy()


def phase_b_behavior(d: str) -> None:
    """Phase B：行为 + UI 不卡 + 黑名单 + 关闭（全程隐藏，不 update_idletasks）。"""
    root = tk.Tk()
    root.withdraw()
    gui = g.XianyuAlertGUI(root, config_path=write_cfg(d))

    # ---- 3) 添加 / 重复拦截 / 更新改名 ----
    gui.var_keyword.set("Switch")
    gui.var_price.set("1000")
    gui.on_add_keyword()
    assert len(gui.tree_keywords.get_children()) == 1
    gui.var_keyword.set("Switch")
    gui.var_price.set("500")
    shown: list = []
    with mock.patch("xianyu_alert.gui.messagebox.showinfo", side_effect=lambda *a: shown.append(a)):
        gui.on_add_keyword()
    assert len(gui.tree_keywords.get_children()) == 1, "同名重复添加应被拦截"
    assert any("已存在" in str(x) for x in shown), "应提示「已存在」"
    iid = gui.tree_keywords.get_children()[0]
    gui.tree_keywords.selection_set(iid)
    gui.var_keyword.set("Swtich")  # 修正错别字
    gui.var_price.set("999")
    gui.on_update_keyword()
    assert len(gui.tree_keywords.get_children()) == 1, "改名后更新选中仍是更新原行"
    assert gui.tree_keywords.item(iid, "values")[0] == "Swtich"
    print("3) 添加/拦截/更新改名 行为正确")

    # ---- 4) 慢 fetch 期间 UI 不卡（需求 3 复现）----
    class SlowFetcher:
        name = "slow"

        def __init__(self, _config: object) -> None:
            self._inner = MockFetcher(products_per_round=5)

        def fetch(self, keyword: str) -> list:
            time.sleep(1.2)  # 模拟慢网络
            return self._inner.fetch(keyword)

        def set_cookies(self, cookie: str) -> None:
            self._inner.set_cookies(cookie)

        def set_max_price(self, max_price: object) -> None:
            self._inner.set_max_price(max_price)

        def close(self) -> None:
            self._inner.close()

    heartbeat = {"count": 0}

    def beat() -> None:
        heartbeat["count"] += 1
        if heartbeat["count"] < 50:
            root.after(50, beat)

    root.after(50, beat)
    with mock.patch("xianyu_alert.gui.build_fetcher", side_effect=lambda cfg: SlowFetcher(cfg)):
        gui.on_run_once()  # 等价点击「⚡ 立即执行一轮」
        assert gui._worker is not None and gui._worker.is_alive()
        deadline = time.monotonic() + 3.0
        while gui._worker.is_alive() and time.monotonic() < deadline:
            root.update()  # 主线程正常驱动事件循环（对应可拖动/可点击）
            time.sleep(0.01)
        root.update()
    gui._worker.join(timeout=3.0)
    assert heartbeat["count"] >= 5, (
        f"慢 fetch 期间主线程 after 心跳应持续触发，实际 {heartbeat['count']} 次"
    )
    print(f"4) 慢 fetch 期间 UI 不卡: 心跳 {heartbeat['count']} 次（主线程未被阻塞）")

    # ---- 5) 黑名单：存储 + 查询排除 ----
    st = Storage(":memory:")
    st.mark_notified(make_product("777777", "噪音商品"))
    assert len(st.list_notified()) == 1
    st.add_blacklist("777777", keyword="Switch", reason="假货")
    assert st.is_blacklisted("777777")
    assert len(st.list_notified()) == 0, "黑名单商品不应出现在提醒记录"
    st.remove_blacklist("777777")
    assert not st.is_blacklisted("777777")
    assert len(st.list_notified()) == 1, "恢复后重新可见"
    st.close()
    print("5) 黑名单 CRUD + 提醒记录排除 正确")

    # ---- 6) 关闭 ----
    gui.on_close()
    print("6) 关闭完成")
    print("PHASE_B PASS")


def make_product(product_id: str, title: str):
    from xianyu_alert.models import Product

    return Product(
        product_id=product_id,
        title=title,
        price=50.0,
        url=f"https://www.goofish.com/item?id={product_id}",
        publish_time="2026-01-01 12:00:00",
        keyword="Switch",
    )


def main(argv: list) -> int:
    # 本沙箱环境的 Tcl 在「建窗-销毁-再建窗」同进程后会进入脆弱状态，
    # 因此 Phase A / Phase B 由驱动脚本分别以独立进程运行。
    with tempfile.TemporaryDirectory(prefix="xianyu_gui_v36_smoke_") as d:
        if "a" in argv:
            phase_a_layout(d)
        if "b" in argv:
            phase_b_behavior(d)
        if not argv:
            phase_a_layout(d)
            phase_b_behavior(d)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
