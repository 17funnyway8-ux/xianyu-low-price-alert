"""GUI 单元测试。

策略：
    1. **不真正显示窗口** —— 绝大多数测试只覆盖 gui 模块里抽出的纯函数
       （配置 <-> 表单转换、Cookie 状态判定、输入校验、通道完整性判定等）；
    2. 少量需要 Tk 的测试放在 TestTkAvailability 中，`setUpClass` 里探测
       图形环境，不可用则整个类 SkipTest，保证 CI / 无显示环境安全。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import tempfile
import unittest
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from xianyu_alert.config import ConfigError, config_from_dict
from xianyu_alert.gui import (
    CHANNEL_ORDER,
    COOKIE_STATE_MISSING,
    COOKIE_STATE_NO_TOKEN,
    COOKIE_STATE_OK,
    DEFAULT_CONFIG_DICT,
    QueueLogHandler,
    build_config_dict,
    channel_is_complete,
    config_to_form,
    cookie_status,
    default_channel_options,
    fetcher_label,
    fetcher_type_from_label,
    format_countdown,
    load_raw_config,
    make_sample_product,
    normalize_channel_options,
    save_raw_config,
    validate_interval,
    validate_keyword_entry,
)


# ---------------------------------------------------------------------- #
# 1. Cookie 状态判定
# ---------------------------------------------------------------------- #
class TestCookieStatus(unittest.TestCase):
    """Cookie 三态判定测试。"""

    def test_missing(self) -> None:
        """空 Cookie 判定为「未配置」。"""
        for value in ("", "   ", None):
            state, text = cookie_status(value)
            self.assertEqual(state, COOKIE_STATE_MISSING)
            self.assertIn("未配置", text)

    def test_no_token(self) -> None:
        """有 Cookie 但缺 _m_h5_tk 判定为「可能无效」。"""
        state, text = cookie_status("cookie2=abc; unb=123")
        self.assertEqual(state, COOKIE_STATE_NO_TOKEN)
        self.assertIn("_m_h5_tk", text)

    def test_ok(self) -> None:
        """含 _m_h5_tk 判定为「已配置」。"""
        state, text = cookie_status("cookie2=abc; _m_h5_tk=deadbeef_170000; x=1")
        self.assertEqual(state, COOKIE_STATE_OK)
        self.assertIn("✅", text)


# ---------------------------------------------------------------------- #
# 2. 输入校验
# ---------------------------------------------------------------------- #
class TestValidation(unittest.TestCase):
    """关键词 / 间隔输入校验测试。"""

    def test_keyword_ok(self) -> None:
        """正常输入返回规范化结果。"""
        self.assertEqual(validate_keyword_entry("  Switch ", " 800.5 "), ("Switch", 800.5))

    def test_keyword_empty(self) -> None:
        """关键词为空时报错。"""
        with self.assertRaises(ValueError) as ctx:
            validate_keyword_entry("   ", "800")
        self.assertIn("关键词", str(ctx.exception))

    def test_price_empty(self) -> None:
        """价格为空时报错。"""
        with self.assertRaises(ValueError):
            validate_keyword_entry("Switch", "")

    def test_price_not_number(self) -> None:
        """价格非数字时报错。"""
        with self.assertRaises(ValueError) as ctx:
            validate_keyword_entry("Switch", "八百")
        self.assertIn("数字", str(ctx.exception))

    def test_price_not_positive(self) -> None:
        """价格为 0 / 负数时报错。"""
        for bad in ("0", "-1", "-99.9"):
            with self.assertRaises(ValueError) as ctx:
                validate_keyword_entry("Switch", bad)
            self.assertIn("正数", str(ctx.exception))

    def test_interval_ok(self) -> None:
        """正常间隔返回整数秒。"""
        self.assertEqual(validate_interval("300"), 300)
        self.assertEqual(validate_interval(" 600 "), 600)

    def test_interval_invalid(self) -> None:
        """间隔为空 / 非数字 / 非正数时报错。"""
        for bad in ("", "abc", "0", "-5"):
            with self.assertRaises(ValueError):
                validate_interval(bad)


# ---------------------------------------------------------------------- #
# 3. 通道参数完整性
# ---------------------------------------------------------------------- #
class TestChannelOptions(unittest.TestCase):
    """通知通道参数处理测试。"""

    def test_console_always_complete(self) -> None:
        """控制台无必填参数，永远完整。"""
        self.assertTrue(channel_is_complete("console", {}))

    def test_serverchan(self) -> None:
        """Server酱必须有 sendkey。"""
        self.assertTrue(channel_is_complete("serverchan", {"sendkey": "SCT123"}))
        self.assertFalse(channel_is_complete("serverchan", {"sendkey": "  "}))
        self.assertFalse(channel_is_complete("serverchan", {}))

    def test_email_requires_all(self) -> None:
        """邮件必须五个字段齐全。"""
        full = {
            "smtp_host": "smtp.qq.com",
            "smtp_port": "465",
            "username": "a@qq.com",
            "password": "pwd",
            "to": "b@qq.com",
        }
        self.assertTrue(channel_is_complete("email", full))
        for missing in full:
            partial = dict(full)
            partial[missing] = ""
            self.assertFalse(channel_is_complete("email", partial), f"缺 {missing} 应判为不完整")

    def test_telegram(self) -> None:
        """Telegram 必须有 bot_token 与 chat_id。"""
        self.assertTrue(channel_is_complete("telegram", {"bot_token": "t", "chat_id": "1"}))
        self.assertFalse(channel_is_complete("telegram", {"bot_token": "t"}))

    def test_unknown_channel(self) -> None:
        """未知通道判为不完整。"""
        self.assertFalse(channel_is_complete("wechat", {"a": "b"}))

    def test_normalize_strips_and_casts_port(self) -> None:
        """规范化会去空白、丢空值、把端口转 int。"""
        result = normalize_channel_options(
            "email",
            {"smtp_host": "  smtp.qq.com ", "smtp_port": " 465 ", "username": "", "to": "a@b.c"},
        )
        self.assertEqual(result["smtp_host"], "smtp.qq.com")
        self.assertEqual(result["smtp_port"], 465)
        self.assertIsInstance(result["smtp_port"], int)
        self.assertNotIn("username", result)

    def test_default_channel_options(self) -> None:
        """每个通道都有默认参数字典。"""
        for ctype in CHANNEL_ORDER:
            self.assertIsInstance(default_channel_options(ctype), dict)
        self.assertEqual(default_channel_options("console"), {})
        self.assertIn("smtp_host", default_channel_options("email"))


# ---------------------------------------------------------------------- #
# 4. 抓取器下拉框
# ---------------------------------------------------------------------- #
class TestFetcherLabels(unittest.TestCase):
    """抓取器标签互转测试（v3.2：web 已从下拉框移除，代码保留为 legacy）。"""

    def test_round_trip(self) -> None:
        """内部值 -> 文案 -> 内部值 可逆。"""
        for ftype in ("mock", "mtop"):
            self.assertEqual(fetcher_type_from_label(fetcher_label(ftype)), ftype)

    def test_labels_are_chinese_and_descriptive(self) -> None:
        """下拉文案带中文说明，mtop 标注推荐、mock 标注开发演示用。"""
        self.assertIn("开发演示", fetcher_label("mock"))
        self.assertIn("推荐", fetcher_label("mtop"))

    def test_web_not_exposed_in_choices(self) -> None:
        """v3.2：web 不再出现在 GUI 下拉框（代码层仍保留向后兼容）。"""
        from xianyu_alert.gui import FETCHER_CHOICES

        choice_values = [value for value, _label in FETCHER_CHOICES]
        self.assertNotIn("web", choice_values)
        self.assertEqual(choice_values[0], "mtop")  # mtop 为默认项
        # 旧配置若仍为 web：标签回退到首个选项（mtop），保存后即迁移为 mtop
        self.assertEqual(fetcher_type_from_label(fetcher_label("web")), "mtop")

    def test_unknown_label_falls_back(self) -> None:
        """未知文案回退到第一个选项。"""
        self.assertEqual(fetcher_type_from_label("外星抓取器"), "mtop")
        self.assertEqual(fetcher_type_from_label("mtop"), "mtop")  # 兼容直接传内部值


# ---------------------------------------------------------------------- #
# 5. 配置 <-> 表单
# ---------------------------------------------------------------------- #
class TestConfigForm(unittest.TestCase):
    """配置字典与表单状态互转测试。"""

    def test_config_to_form_basic(self) -> None:
        """标准配置能正确读入表单。"""
        form = config_to_form(
            {
                "keywords": [{"keyword": "Switch", "max_price": 800}],
                "monitor": {"interval_seconds": 600, "cookies": "_m_h5_tk=a_1"},
                "fetcher": {"type": "mtop"},
                "storage": {"path": "state/x.db"},
                "notify": {"channels": [{"type": "serverchan", "sendkey": "SCT1"}]},
            }
        )
        self.assertEqual(form["keywords"], [("Switch", 800.0)])
        self.assertEqual(form["interval"], 600)
        self.assertEqual(form["fetcher_type"], "mtop")
        self.assertEqual(form["cookies"], "_m_h5_tk=a_1")
        self.assertEqual(form["storage_path"], "state/x.db")
        self.assertTrue(form["channels"]["serverchan"]["enabled"])
        self.assertEqual(form["channels"]["serverchan"]["options"]["sendkey"], "SCT1")
        self.assertFalse(form["channels"]["telegram"]["enabled"])

    def test_config_to_form_tolerates_garbage(self) -> None:
        """脏数据不抛异常，回退到默认值。"""
        form = config_to_form(
            {
                "keywords": ["不是字典", {"keyword": "", "max_price": 1}, {"keyword": "OK", "max_price": "abc"}],
                "monitor": {"interval_seconds": "不是数字"},
                "fetcher": {"type": "外星人"},
                "notify": {"channels": "不是列表"},
            }
        )
        self.assertEqual(form["keywords"], [])
        # v3.2：脏数据回退到默认值 600（原 300）
        self.assertEqual(form["interval"], 600)
        self.assertEqual(form["fetcher_type"], "mtop")
        # 无任何通道时兜底启用控制台
        self.assertTrue(form["channels"]["console"]["enabled"])

    def test_config_to_form_none(self) -> None:
        """None / 非字典输入也能安全处理。"""
        for bad in (None, "字符串", 123, []):
            form = config_to_form(bad)
            self.assertEqual(form["keywords"], [])
            self.assertTrue(form["channels"]["console"]["enabled"])

    def test_build_config_dict_shape(self) -> None:
        """组装出的配置字典结构正确，且能通过核心校验。"""
        channels: Dict[str, Dict[str, Any]] = {
            "console": {"enabled": True, "options": {}},
            "serverchan": {"enabled": True, "options": {"sendkey": "SCT9"}},
            "email": {"enabled": True, "options": {"smtp_host": "smtp.qq.com"}},  # 参数不全
            "telegram": {"enabled": False, "options": {"bot_token": "t", "chat_id": "1"}},
        }
        data = build_config_dict(
            keywords=[("Switch", 800.0), ("iPad", 1500.0)],
            interval_seconds=300,
            fetcher_type="mtop",
            cookies="_m_h5_tk=abc_1",
            storage_path="state/gui.db",
            channels=channels,
        )
        self.assertEqual(data["keywords"], [
            {"keyword": "Switch", "max_price": 800.0},
            {"keyword": "iPad", "max_price": 1500.0},
        ])
        self.assertEqual(data["monitor"]["interval_seconds"], 300)
        self.assertEqual(data["monitor"]["cookies"], "_m_h5_tk=abc_1")
        self.assertEqual(data["fetcher"]["type"], "mtop")
        self.assertEqual(data["storage"]["path"], "state/gui.db")

        types = [c["type"] for c in data["notify"]["channels"]]
        self.assertEqual(types, ["console", "serverchan"])  # email 参数不全、telegram 未勾选

        # 关键：组装结果必须能通过核心配置校验
        config = config_from_dict(data)
        self.assertEqual(config.fetcher.type, "mtop")
        self.assertEqual(len(config.keywords), 2)

    def test_build_config_dict_preserves_base_fields(self) -> None:
        """基于 base 增量覆盖，保留用户手工添加的其它字段。"""
        base = {
            "monitor": {"interval_seconds": 60, "user_agent": "MyUA", "extra": "keep-me"},
            "fetcher": {"type": "mock", "mock_products_per_round": 7},
            "storage": {"path": "old.db", "vacuum": True},
            "自定义顶层": 1,
        }
        data = build_config_dict(
            keywords=[("A", 1.0)],
            interval_seconds=300,
            fetcher_type="mtop",
            cookies="",
            storage_path="new.db",
            channels={"console": {"enabled": True, "options": {}}},
            base=base,
        )
        self.assertEqual(data["monitor"]["user_agent"], "MyUA")
        self.assertEqual(data["monitor"]["extra"], "keep-me")
        self.assertEqual(data["monitor"]["interval_seconds"], 300)
        self.assertEqual(data["fetcher"]["mock_products_per_round"], 7)
        self.assertEqual(data["fetcher"]["type"], "mtop")
        self.assertEqual(data["storage"]["path"], "new.db")
        self.assertTrue(data["storage"]["vacuum"])
        self.assertEqual(data["自定义顶层"], 1)
        # base 未被就地修改
        self.assertEqual(base["fetcher"]["type"], "mock")

    def test_build_config_dict_falls_back_to_console(self) -> None:
        """一个通道都没启用时兜底写入 console。"""
        data = build_config_dict(
            keywords=[("A", 1.0)],
            interval_seconds=300,
            fetcher_type="mock",
            cookies="",
            storage_path="a.db",
            channels={"console": {"enabled": False, "options": {}}},
        )
        self.assertEqual(data["notify"]["channels"], [{"type": "console"}])

    def test_round_trip_form_config(self) -> None:
        """表单 -> 配置 -> 表单 往返一致。"""
        data = build_config_dict(
            keywords=[("Switch", 800.0)],
            interval_seconds=450,
            fetcher_type="mtop",
            cookies="_m_h5_tk=zz_1",
            storage_path="state/rt.db",
            channels={
                "console": {"enabled": True, "options": {}},
                "telegram": {"enabled": True, "options": {"bot_token": "T", "chat_id": "9"}},
            },
        )
        form = config_to_form(data)
        self.assertEqual(form["keywords"], [("Switch", 800.0)])
        self.assertEqual(form["interval"], 450)
        self.assertEqual(form["fetcher_type"], "mtop")
        self.assertEqual(form["cookies"], "_m_h5_tk=zz_1")
        self.assertTrue(form["channels"]["telegram"]["enabled"])
        self.assertEqual(form["channels"]["telegram"]["options"]["chat_id"], "9")


# ---------------------------------------------------------------------- #
# 6. 配置文件读写
# ---------------------------------------------------------------------- #
class TestRawConfigIO(unittest.TestCase):
    """config.yaml 读写测试。"""

    def test_load_missing_returns_default(self) -> None:
        """文件不存在时返回内置默认配置（不报错退出）。"""
        data = load_raw_config(os.path.join(tempfile.gettempdir(), "绝对不存在的配置_xyz.yaml"))
        self.assertEqual(data["fetcher"]["type"], DEFAULT_CONFIG_DICT["fetcher"]["type"])
        self.assertIn("keywords", data)

    def test_default_config_is_valid(self) -> None:
        """内置默认配置本身必须能通过核心校验。"""
        config = config_from_dict(load_raw_config("绝对不存在.yaml"))
        self.assertGreaterEqual(len(config.keywords), 1)

    def test_save_and_load(self) -> None:
        """写入后能原样读回。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "config.yaml")
            payload = {"keywords": [{"keyword": "中文关键词", "max_price": 12.5}]}
            save_raw_config(path, payload)
            self.assertTrue(os.path.isfile(path))
            with open(path, "r", encoding="utf-8") as fp:
                loaded = yaml.safe_load(fp)
            self.assertEqual(loaded, payload)
            self.assertEqual(load_raw_config(path), payload)

    def test_load_broken_yaml_returns_default(self) -> None:
        """YAML 语法错误时回退默认配置而不是崩溃。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.yaml")
            with open(path, "w", encoding="utf-8") as fp:
                fp.write("keywords: [unclosed\n  : :\n")
            data = load_raw_config(path)
            self.assertIn("keywords", data)


# ---------------------------------------------------------------------- #
# 7. 日志 handler
# ---------------------------------------------------------------------- #
class TestQueueLogHandler(unittest.TestCase):
    """日志队列 handler 测试。"""

    def test_receives_records(self) -> None:
        """挂到 xianyu_alert logger 上能收到各模块日志。"""
        ui_queue: "queue.Queue" = queue.Queue()
        handler = QueueLogHandler(ui_queue, level=logging.INFO)
        target = logging.getLogger("xianyu_alert.test_gui_dummy")
        root_pkg = logging.getLogger("xianyu_alert")
        old_level = root_pkg.level
        root_pkg.setLevel(logging.INFO)
        root_pkg.addHandler(handler)
        try:
            target.info("命中商品 %d 个", 3)
            target.warning("触发风控")
            target.debug("这条不应出现")
        finally:
            root_pkg.removeHandler(handler)
            root_pkg.setLevel(old_level)

        items = []
        while not ui_queue.empty():
            items.append(ui_queue.get_nowait())

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][0], "log")
        self.assertEqual(items[0][1][0], "INFO")
        self.assertIn("命中商品 3 个", items[0][1][1])
        self.assertEqual(items[1][1][0], "WARNING")
        self.assertIn("触发风控", items[1][1][1])

    def test_emit_never_raises(self) -> None:
        """队列异常时 emit 也不抛异常。"""

        class BrokenQueue:
            """put_nowait 总是失败的队列。"""

            def put_nowait(self, _item: Any) -> None:
                raise RuntimeError("queue broken")

        handler = QueueLogHandler(BrokenQueue())  # type: ignore[arg-type]
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
        handler.emit(record)  # 不应抛异常


# ---------------------------------------------------------------------- #
# 8. 其它辅助
# ---------------------------------------------------------------------- #
class TestMiscHelpers(unittest.TestCase):
    """杂项辅助函数测试。"""

    def test_sample_product(self) -> None:
        """测试用假商品字段齐全。"""
        product = make_sample_product("Switch")
        self.assertEqual(product.keyword, "Switch")
        self.assertTrue(product.title)
        self.assertGreater(product.price, 0)
        self.assertTrue(product.url)

    def test_format_countdown(self) -> None:
        """倒计时格式化为 mm:ss。"""
        self.assertEqual(format_countdown(0), "00:00")
        self.assertEqual(format_countdown(-5), "00:00")
        self.assertEqual(format_countdown(59.9), "00:59")
        self.assertEqual(format_countdown(300), "05:00")
        self.assertEqual(format_countdown(3599), "59:59")


# ---------------------------------------------------------------------- #
# 9. 需要真实 Tk 的测试（无显示环境自动跳过）
# ---------------------------------------------------------------------- #
class TestTkAvailability(unittest.TestCase):
    """依赖 Tk 的最小化测试；无图形环境时整类跳过。"""

    root: Any = None

    @classmethod
    def setUpClass(cls) -> None:
        """尝试创建隐藏的 Tk 根窗口，失败则跳过整个类。"""
        try:
            import tkinter

            cls.root = tkinter.Tk()
            cls.root.withdraw()
        except Exception as exc:  # noqa: BLE001 - 无显示环境
            raise unittest.SkipTest(f"当前环境无 GUI 显示，跳过 Tk 相关测试：{exc}")

    @classmethod
    def tearDownClass(cls) -> None:
        """销毁根窗口。"""
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:  # noqa: BLE001
                pass

    def test_gui_class_constructs(self) -> None:
        """XianyuAlertGUI 能在临时配置上正常构造并关闭。"""
        from xianyu_alert.gui import XianyuAlertGUI

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.yaml")
            save_raw_config(
                config_path,
                {
                    "keywords": [{"keyword": "Switch", "max_price": 800}],
                    "monitor": {"interval_seconds": 300, "cookies": ""},
                    "fetcher": {"type": "mock"},
                    "storage": {"path": os.path.join(tmp, "t.db")},
                    "notify": {"channels": [{"type": "console"}]},
                },
            )
            import tkinter

            root = tkinter.Toplevel(self.root)
            root.withdraw()
            try:
                app = XianyuAlertGUI(root, config_path=config_path)
                self.assertEqual(app._collect_keywords(), [("Switch", 800.0)])
                data = app._collect_config_dict()
                self.assertEqual(data["fetcher"]["type"], "mock")
                config_from_dict(data)  # 必须可用
                app._remove_log_handler()
            finally:
                try:
                    root.destroy()
                except Exception:  # noqa: BLE001
                    pass

    def test_gui_allows_mtop_without_cookie_save(self) -> None:
        """v3.2：选了 mtop 但没 Cookie 时**允许保存**（不拦截首用），保存后由 on_save_config 弹 warning。"""
        from xianyu_alert.gui import XianyuAlertGUI, fetcher_label

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.yaml")
            save_raw_config(
                config_path,
                {
                    "keywords": [{"keyword": "Switch", "max_price": 800}],
                    "monitor": {"interval_seconds": 300, "cookies": ""},
                    "fetcher": {"type": "mock"},
                    "storage": {"path": os.path.join(tmp, "t.db")},
                    "notify": {"channels": [{"type": "console"}]},
                },
            )
            import tkinter

            root = tkinter.Toplevel(self.root)
            root.withdraw()
            try:
                app = XianyuAlertGUI(root, config_path=config_path)
                app.var_fetcher.set(fetcher_label("mtop"))
                # 不应再抛 ValueError（v3.2 允许保存）
                data = app._collect_config_dict()
                self.assertEqual(data["fetcher"]["type"], "mtop")
                config_from_dict(data)  # 组装结果仍必须可校验
                app._remove_log_handler()
            finally:
                try:
                    root.destroy()
                except Exception:  # noqa: BLE001
                    pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
