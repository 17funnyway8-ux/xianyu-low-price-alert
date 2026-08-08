"""GUI 集成冒烟（v3.5）：真实 Tk 建窗 → 定制预置词 → 保存落盘 → 关闭。"""
import os
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

import xianyu_alert.gui as g  # noqa: E402

CFG = {
    "keywords": [{"keyword": "Switch", "max_price": 1000}],
    "monitor": {"interval_seconds": 600, "user_agent": "", "cookies": ""},
    "fetcher": {"type": "mock", "mock_products_per_round": 5, "mock_fail_rounds": []},
    "storage": {"path": ":memory:"},
    "notify": {"channels": [{"type": "console"}]},
    "preset_exclude_keywords": ["商家", "实体店"],
}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="xianyu_gui_smoke_") as d:
        cp = os.path.join(d, "config.yaml")
        with open(cp, "w", encoding="utf-8") as f:
            yaml.safe_dump(CFG, f, allow_unicode=True, sort_keys=False)
        root = tk.Tk()
        root.withdraw()
        gui = g.XianyuAlertGUI(root, config_path=cp)
        print("1) 启动加载定制预置词:", gui._preset_exclude_keywords)
        assert gui._preset_exclude_keywords == ["商家", "实体店"]
        f = gui._default_filters("iPhone")
        print("2) 新关键词默认排除词:", f["exclude_keywords"], "必含:", f["required_keywords"])
        assert f["exclude_keywords"] == ["商家", "实体店"] and f["required_keywords"] == []
        gui._apply_preset_edit("回收\n高价回收")
        print("3) 编辑后预置词:", gui._preset_exclude_keywords)
        assert gui._preset_exclude_keywords == ["回收", "高价回收"]
        assert gui._raw_config["preset_exclude_keywords"] == ["回收", "高价回收"]
        gui.on_save_config()
        with open(cp, encoding="utf-8") as fp:
            saved = yaml.safe_load(fp)
        print("4) config 落盘 preset_exclude_keywords:", saved.get("preset_exclude_keywords"))
        assert saved.get("preset_exclude_keywords") == ["回收", "高价回收"]
        gui.on_close()
        print("5) 关闭完成")
        print("GUI 集成冒烟 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
