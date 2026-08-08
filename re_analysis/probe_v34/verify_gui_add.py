# -*- coding: utf-8 -*-
"""v3.4 任务B验证：Cookie 管理「添加」按钮点击后应弹出子对话框并可保存。"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tkinter as tk

orig_report = tk.Tk.report_callback_exception


def report(exc, val, tb):  # pragma: no cover - 仅在异常时触发
    print("!!! Tk 回调异常被吞:")
    traceback.print_exception(exc, val, tb)


tk.Tk.report_callback_exception = report

root = tk.Tk()
root.withdraw()
from xianyu_alert.gui import XianyuAlertGUI  # noqa: E402

gui = XianyuAlertGUI(root, config_path="config.yaml")
gui.on_manage_cookies()

tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
dlg = tops[0]
print("对话框标题:", dlg.title())


def find_button(w, text_part):
    for child in w.winfo_children():
        try:
            if child.winfo_class() == "TButton" and text_part in str(child.cget("text")):
                return child
        except Exception:
            pass
        found = find_button(child, text_part)
        if found:
            return found
    return None


add_btn = find_button(dlg, "添加")
print("找到添加按钮:", add_btn is not None)
add_btn.invoke()


def find_toplevel_children():
    out = []

    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, tk.Toplevel):
                out.append(c)
            walk(c)

    walk(root)
    return out


subs = find_toplevel_children()
for s in subs:
    print("子 Toplevel:", s.title())
add_sub = [s for s in subs if "添加" in s.title()]
print("添加子对话框弹出:", len(add_sub) > 0)

# 填写名称 + Cookie 后保存
if add_sub:
    d = add_sub[0]
    entry = [None]
    text = [None]

    def walk2(w):
        for c in w.winfo_children():
            if entry[0] is None and isinstance(c, tk.Entry):
                entry[0] = c
            if text[0] is None and isinstance(c, tk.Text):
                text[0] = c
            walk2(c)

    walk2(d)
    if entry[0] is not None:
        entry[0].insert(0, "测试账号")
    if text[0] is not None:
        text[0].insert("1.0", "cookie2=abc; _m_h5_tk=test_1785488087003")
    save_btn = [None]

    def walk3(w):
        for c in w.winfo_children():
            if save_btn[0] is None and c.winfo_class() == "TButton" and "保存" in str(c.cget("text")):
                save_btn[0] = c
            walk3(c)

    walk3(d)
    print("找到保存按钮:", save_btn[0] is not None)
    save_btn[0].invoke()
    print("_cookie_pool 长度:", len(gui._cookie_pool))
    print("_cookie_pool[0]:", {k: (v[:30] if isinstance(v, str) else v) for k, v in gui._cookie_pool[0].items()})
    subs2 = [s for s in find_toplevel_children() if "添加" in s.title()]
    print("保存后添加子对话框数:", len(subs2))

for s in find_toplevel_children():
    s.destroy()
root.destroy()
print("OK - 无异常")
