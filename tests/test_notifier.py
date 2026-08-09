"""通知模块单元测试（不访问外网，网络请求全部用 mock）。"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.config import config_from_dict  # noqa: E402
from xianyu_alert.models import Product  # noqa: E402
from xianyu_alert.notifier import (  # noqa: E402
    BarkNotifier,
    ConsoleNotifier,
    EmailNotifier,
    Notifier,
    ServerChanNotifier,
    TelegramNotifier,
    WebhookNotifier,
    build_notifiers,
    build_title,
    format_markdown,
    format_message,
    format_messages,
)

SAMPLE = Product(
    product_id="123456789",
    title="Switch 续航版 国行",
    price=899.0,
    url="https://www.goofish.com/item?id=123456789",
    publish_time="2024-05-01 10:00",
    keyword="Switch",
)
SAMPLE2 = Product(
    product_id="987654321",
    title="iPhone 12 128G",
    price=1288.0,
    url="https://www.goofish.com/item?id=987654321",
    publish_time="2024-05-02 09:30",
    keyword="iPhone",
)


class TestFormat(unittest.TestCase):
    """消息格式化测试：必须包含四要素。"""

    def test_format_message_contains_four_elements(self) -> None:
        text = format_message(SAMPLE)
        self.assertIn("Switch 续航版 国行", text)      # 商品名称
        self.assertIn("¥899.00", text)                 # 价格
        self.assertIn(SAMPLE.url, text)                # 商品链接
        self.assertIn("2024-05-01 10:00", text)        # 发布时间

    def test_format_message_missing_publish_time(self) -> None:
        product = Product("1", "无时间商品", 10.0, "https://x")
        self.assertIn("发布时间: 未知", format_message(product))

    def test_format_messages_multiple(self) -> None:
        text = format_messages([SAMPLE, SAMPLE2])
        self.assertIn("【1】", text)
        self.assertIn("【2】", text)
        self.assertIn("iPhone 12 128G", text)

    def test_format_messages_empty(self) -> None:
        self.assertEqual(format_messages([]), "")
        self.assertEqual(format_markdown([]), "")

    def test_format_markdown(self) -> None:
        text = format_markdown([SAMPLE])
        self.assertIn("**1. Switch 续航版 国行**", text)
        self.assertIn("- 价格: ¥899.00", text)
        self.assertIn("- 商品链接: " + SAMPLE.url, text)
        self.assertIn("- 发布时间: 2024-05-01 10:00", text)

    def test_build_title(self) -> None:
        self.assertIn("Switch", build_title([SAMPLE]))
        self.assertIn("2", build_title([SAMPLE, SAMPLE2]))
        self.assertEqual(build_title([]), "闲鱼低价提醒")


class TestConsoleNotifier(unittest.TestCase):
    """控制台通知输出测试（捕获 stdout）。"""

    def test_output_contains_four_elements(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ConsoleNotifier().notify([SAMPLE])
        output = buffer.getvalue()
        self.assertIn("商品名称: Switch 续航版 国行", output)
        self.assertIn("价格: ¥899.00", output)
        self.assertIn("商品链接: " + SAMPLE.url, output)
        self.assertIn("发布时间: 2024-05-01 10:00", output)

    def test_empty_products_no_output(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ConsoleNotifier().notify([])
        self.assertEqual(buffer.getvalue(), "")

    def test_name(self) -> None:
        self.assertEqual(ConsoleNotifier().name, "console")


class TestServerChanNotifier(unittest.TestCase):
    """Server酱通知测试：patch requests.post。"""

    def test_requires_sendkey(self) -> None:
        with self.assertRaises(ValueError):
            ServerChanNotifier("")

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_post_payload(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = mock.Mock(status_code=200, text="ok")
        ServerChanNotifier("SCTTESTKEY").notify([SAMPLE])

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://sctapi.ftqq.com/SCTTESTKEY.send")
        payload = kwargs["data"]
        self.assertIn("Switch", payload["title"])
        self.assertIn("¥899.00", payload["desp"])
        self.assertIn(SAMPLE.url, payload["desp"])
        self.assertIn("2024-05-01 10:00", payload["desp"])

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_non_200_raises(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = mock.Mock(status_code=500, text="err")
        with self.assertRaises(RuntimeError):
            ServerChanNotifier("KEY").notify([SAMPLE])

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_safe_notify_swallows_error(self, mock_post: mock.MagicMock) -> None:
        """网络异常应被 safe_notify 吞掉，返回 False 而不抛出。"""
        mock_post.side_effect = RuntimeError("network down")
        self.assertFalse(ServerChanNotifier("KEY").safe_notify([SAMPLE]))


class TestTelegramNotifier(unittest.TestCase):
    """Telegram 通知测试。"""

    def test_requires_params(self) -> None:
        with self.assertRaises(ValueError):
            TelegramNotifier("", "chat")
        with self.assertRaises(ValueError):
            TelegramNotifier("token", "")

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_post_payload(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = mock.Mock(status_code=200, text="ok")
        TelegramNotifier("123:ABC", "42").notify([SAMPLE])

        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.telegram.org/bot123:ABC/sendMessage")
        self.assertEqual(kwargs["data"]["chat_id"], "42")
        self.assertIn("商品链接", kwargs["data"]["text"])


class TestEmailNotifier(unittest.TestCase):
    """邮件通知测试：patch smtplib。"""

    def test_requires_params(self) -> None:
        with self.assertRaises(ValueError):
            EmailNotifier("", 587, "u", "p", "to@x.com")
        with self.assertRaises(ValueError):
            EmailNotifier("smtp.x.com", 587, "u", "p", "")

    def test_recipients_parsing(self) -> None:
        notifier = EmailNotifier("smtp.x.com", 587, "u@x.com", "p", "a@x.com, b@x.com")
        self.assertEqual(notifier.recipients, ["a@x.com", "b@x.com"])
        self.assertTrue(notifier.use_tls)
        self.assertFalse(notifier.use_ssl)

    def test_ssl_auto_enabled_on_465(self) -> None:
        notifier = EmailNotifier("smtp.x.com", 465, "u@x.com", "p", ["a@x.com"])
        self.assertTrue(notifier.use_ssl)
        self.assertFalse(notifier.use_tls)

    @mock.patch("xianyu_alert.notifier.smtplib.SMTP")
    def test_send_via_starttls(self, mock_smtp: mock.MagicMock) -> None:
        server = mock_smtp.return_value
        notifier = EmailNotifier("smtp.x.com", 587, "u@x.com", "pwd", "a@x.com")
        notifier.notify([SAMPLE])

        mock_smtp.assert_called_once()
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("u@x.com", "pwd")
        server.sendmail.assert_called_once()
        sent_body = server.sendmail.call_args[0][2]
        self.assertIn("u@x.com", server.sendmail.call_args[0][0])
        self.assertIsInstance(sent_body, str)


class TestNotifierMessage(unittest.TestCase):
    """v1.8：notify_message / safe_notify_message / notify_plain_message。"""

    def test_console_notify_message_prints(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ConsoleNotifier().notify_message("标题", "正文")
        output = buffer.getvalue()
        self.assertIn("标题", output)
        self.assertIn("正文", output)

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_serverchan_message_payload(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = mock.Mock(status_code=200)
        ServerChanNotifier("KEY").notify_message("标题", "正文")
        args, kwargs = mock_post.call_args
        self.assertEqual(kwargs["data"]["title"], "标题")
        self.assertEqual(kwargs["data"]["desp"], "正文")

    @mock.patch("xianyu_alert.notifier.smtplib.SMTP")
    def test_email_message_subject_and_body(self, mock_smtp: mock.MagicMock) -> None:
        import base64
        from email.header import decode_header

        server = mock_smtp.return_value
        notifier = EmailNotifier("smtp.x.com", 587, "u@x.com", "p", "a@x.com")
        notifier.notify_message("标题", "正文")
        sent = server.sendmail.call_args[0][2]
        # Subject 头被 MIME base64 编码，需解码断言
        subject_line = next(line for line in sent.splitlines() if line.startswith("Subject:"))
        subject_value = subject_line.split(":", 1)[1].strip()
        decoded_subject = "".join(
            part.decode(charset or "utf-8") if isinstance(part, bytes) else part
            for part, charset in decode_header(subject_value)
        )
        self.assertEqual(decoded_subject, "标题")
        # 正文也是 base64 编码
        self.assertIn(base64.b64encode("正文".encode("utf-8")).decode("ascii"), sent)

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_telegram_message_text(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = mock.Mock(status_code=200)
        TelegramNotifier("T", "1").notify_message("标题", "正文")
        self.assertEqual(mock_post.call_args.kwargs["data"]["text"], "标题\n\n正文")

    @mock.patch("xianyu_alert.notifier.requests.get")
    def test_bark_message_text(self, mock_get: mock.MagicMock) -> None:
        mock_get.return_value = mock.Mock(status_code=200)
        BarkNotifier("https://api.day.app/K/").notify_message("标题", "正文")
        url = mock_get.call_args[0][0]
        import urllib.parse

        self.assertIn("标题", urllib.parse.unquote(url))

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_webhook_message_content(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = mock.Mock(status_code=200)
        WebhookNotifier("https://x/hook").notify_message("标题", "正文")
        payload = json.loads(mock_post.call_args.kwargs["data"])
        self.assertEqual(payload["text"]["content"], "标题\n\n正文")

    def test_safe_notify_message_swallows(self) -> None:
        """safe_notify_message 失败只 warning 返回 False。"""
        notifier = ServerChanNotifier("KEY")
        with mock.patch.object(notifier, "notify_message", side_effect=RuntimeError("boom")):
            with self.assertLogs("xianyu_alert.notifier", level="WARNING"):
                self.assertFalse(notifier.safe_notify_message("标题", "正文"))

    def test_safe_notify_message_ok(self) -> None:
        notifier = ConsoleNotifier()
        buffer = io.StringIO()
        # redirect 到 StringIO：Windows CI 管道 cp1252 无法编码中文，
        # 直接打到真实 stdout 会抛 UnicodeEncodeError → safe 返回 False
        with redirect_stdout(buffer):
            self.assertTrue(notifier.safe_notify_message("标题", "正文"))
        self.assertIn("标题", buffer.getvalue())

    def test_notify_plain_message_sends_all(self) -> None:
        """notify_plain_message 向全部通知器发送；单个失败不影响其它。"""
        received: list = []

        class FakeNotifier(Notifier):
            name = "fake"

            def __init__(self, raise_error: bool = False) -> None:
                self.raise_error = raise_error

            def notify(self, products: list) -> None:  # type: ignore[override]
                pass

            def notify_message(self, title: str, text: str) -> None:
                if self.raise_error:
                    raise RuntimeError("boom")
                received.append((title, text))

        from xianyu_alert.notifier import notify_plain_message

        notify_plain_message(
            [FakeNotifier(raise_error=True), FakeNotifier()], "标题", "正文"
        )
        self.assertEqual(received, [("标题", "正文")])

    def test_notify_plain_message_empty(self) -> None:
        from xianyu_alert.notifier import notify_plain_message

        notify_plain_message([], "标题", "正文")  # 不抛


class TestBuildNotifiers(unittest.TestCase):
    """build_notifiers 工厂测试。"""

    @staticmethod
    def _config(channels: list) -> object:
        return config_from_dict(
            {
                "keywords": [{"keyword": "Switch", "max_price": 1000}],
                "notify": {"channels": channels},
            }
        )

    def test_build_console(self) -> None:
        notifiers = build_notifiers(self._config([{"type": "console"}]))
        self.assertEqual([n.name for n in notifiers], ["console"])

    def test_build_multiple(self) -> None:
        notifiers = build_notifiers(
            self._config(
                [
                    {"type": "console"},
                    {"type": "serverchan", "sendkey": "KEY"},
                    {"type": "telegram", "bot_token": "T", "chat_id": "1"},
                ]
            )
        )
        self.assertEqual([n.name for n in notifiers], ["console", "serverchan", "telegram"])

    def test_skip_incomplete_channel(self) -> None:
        """参数不全的通道应被跳过（只保留 console）。"""
        with self.assertLogs("xianyu_alert.notifier", level="WARNING"):
            notifiers = build_notifiers(
                self._config([{"type": "console"}, {"type": "serverchan"}])
            )
        self.assertEqual([n.name for n in notifiers], ["console"])

    def test_fallback_to_console(self) -> None:
        """所有通道都不可用时应兜底为 console。"""
        with self.assertLogs("xianyu_alert.notifier", level="WARNING"):
            notifiers = build_notifiers(self._config([{"type": "telegram", "bot_token": "t"}]))
        self.assertEqual([n.name for n in notifiers], ["console"])

    def test_notifier_is_abstract(self) -> None:
        with self.assertRaises(TypeError):
            Notifier()  # type: ignore[abstract]


class TestBarkNotifier(unittest.TestCase):
    """Bark 推送测试：GET URL 构造（quote / 尾斜杠归一）+ safe_notify。"""

    def test_requires_url(self) -> None:
        with self.assertRaises(ValueError):
            BarkNotifier("")

    @mock.patch("xianyu_alert.notifier.requests.get")
    def test_get_url_quotes_message(self, mock_get: mock.MagicMock) -> None:
        mock_get.return_value = mock.Mock(status_code=200, text="ok")
        BarkNotifier("https://api.day.app/MyKey/").notify([SAMPLE])

        url = mock_get.call_args[0][0]
        expected_prefix = "https://api.day.app/MyKey/"
        self.assertTrue(url.startswith(expected_prefix))
        # 消息部分被 quote 编码（原始消息含空格与换行，编码后不应含空格）
        quoted = url[len(expected_prefix):]
        self.assertNotIn(" ", quoted)
        self.assertIn("Switch", __import__("urllib.parse", fromlist=["unquote"]).unquote(quoted))

    @mock.patch("xianyu_alert.notifier.requests.get")
    def test_trailing_slash_normalized(self, mock_get: mock.MagicMock) -> None:
        mock_get.return_value = mock.Mock(status_code=200)
        BarkNotifier("https://api.day.app/MyKey").notify([SAMPLE])
        url = mock_get.call_args[0][0]
        self.assertTrue(url.startswith("https://api.day.app/MyKey/"))

    @mock.patch("xianyu_alert.notifier.requests.get")
    def test_non_200_raises(self, mock_get: mock.MagicMock) -> None:
        mock_get.return_value = mock.Mock(status_code=500)
        with self.assertRaises(RuntimeError):
            BarkNotifier("https://api.day.app/K/").notify([SAMPLE])

    @mock.patch("xianyu_alert.notifier.requests.get")
    def test_safe_notify_swallows(self, mock_get: mock.MagicMock) -> None:
        mock_get.side_effect = RuntimeError("net down")
        self.assertFalse(BarkNotifier("https://api.day.app/K/").safe_notify([SAMPLE]))

    @mock.patch("xianyu_alert.notifier.requests.get")
    def test_timeout_passed(self, mock_get: mock.MagicMock) -> None:
        mock_get.return_value = mock.Mock(status_code=200)
        BarkNotifier("https://api.day.app/K/", timeout=3.5).notify([SAMPLE])
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 3.5)


class TestWebhookNotifier(unittest.TestCase):
    """Webhook 推送测试：JSON payload + Content-Type 头 + safe_notify。"""

    def test_requires_url(self) -> None:
        with self.assertRaises(ValueError):
            WebhookNotifier("")

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_post_json_payload_and_header(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = mock.Mock(status_code=200)
        WebhookNotifier("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x").notify([SAMPLE])

        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x")
        # 显式 Content-Type: application/json（修正 exe 裸 data 未设头的隐患）
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        payload = json.loads(kwargs["data"])
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("Switch", payload["text"]["content"])
        self.assertIn("¥899.00", payload["text"]["content"])

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_non_200_raises(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = mock.Mock(status_code=403)
        with self.assertRaises(RuntimeError):
            WebhookNotifier("https://x/webhook").notify([SAMPLE])

    @mock.patch("xianyu_alert.notifier.requests.post")
    def test_safe_notify_swallows(self, mock_post: mock.MagicMock) -> None:
        mock_post.side_effect = RuntimeError("down")
        self.assertFalse(WebhookNotifier("https://x/webhook").safe_notify([SAMPLE]))


class TestBuildNotifiersV3(unittest.TestCase):
    """v3 新增通道工厂分支测试。"""

    @staticmethod
    def _config(channels: list) -> object:
        return config_from_dict(
            {
                "keywords": [{"keyword": "Switch", "max_price": 1000}],
                "notify": {"channels": channels},
            }
        )

    def test_build_bark_and_webhook(self) -> None:
        notifiers = build_notifiers(
            self._config(
                [
                    {"type": "bark", "url": "https://api.day.app/K/"},
                    {"type": "webhook", "url": "https://x/webhook"},
                ]
            )
        )
        self.assertEqual([n.name for n in notifiers], ["bark", "webhook"])
        self.assertIsInstance(notifiers[0], BarkNotifier)
        self.assertIsInstance(notifiers[1], WebhookNotifier)

    def test_skip_incomplete_new_channels(self) -> None:
        """缺 url 的 bark/webhook 应被跳过并 warning。"""
        with self.assertLogs("xianyu_alert.notifier", level="WARNING"):
            notifiers = build_notifiers(
                self._config(
                    [
                        {"type": "console"},
                        {"type": "bark"},
                        {"type": "webhook", "url": ""},
                    ]
                )
            )
        self.assertEqual([n.name for n in notifiers], ["console"])

    def test_valid_channel_types_include_new(self) -> None:
        from xianyu_alert.config import VALID_CHANNEL_TYPES

        self.assertIn("bark", VALID_CHANNEL_TYPES)
        self.assertIn("webhook", VALID_CHANNEL_TYPES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
