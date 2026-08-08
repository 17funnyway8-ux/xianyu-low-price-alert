"""通知通道。

内置六种通知方式：
    ConsoleNotifier    : 打印到标准输出（默认，永远可用）
    ServerChanNotifier : Server酱（微信推送）
    EmailNotifier      : SMTP 邮件
    TelegramNotifier   : Telegram Bot
    BarkNotifier       : iOS Bark 推送（GET {url}/{quote(msg)}）
    WebhookNotifier    : 企业微信 / 钉钉风格机器人（POST JSON）

所有网络类通知都会捕获异常并降级为 warning 日志，**不会中断主循环**。
"""

from __future__ import annotations

import json
import logging
import smtplib
from abc import ABC, abstractmethod
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, List, Optional

import requests

from .config import Config, NotifyChannel
from .models import Product

logger = logging.getLogger(__name__)

# 通知标题模板
DEFAULT_TITLE = "闲鱼低价提醒"
SERVERCHAN_URL_TEMPLATE = "https://sctapi.ftqq.com/{sendkey}.send"
TELEGRAM_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
HTTP_TIMEOUT = 10.0


# ---------------------------------------------------------------------- #
# 消息格式化
# ---------------------------------------------------------------------- #
def format_message(product: Product) -> str:
    """格式化单个商品的提醒文案（四要素齐全）。

    Args:
        product: 商品对象。

    Returns:
        含「商品名称 / 价格 / 商品链接 / 发布时间」的多行文本。
    """
    publish_time = product.publish_time or "未知"
    keyword_line = f"命中关键词: {product.keyword}\n" if product.keyword else ""
    return (
        f"商品名称: {product.title}\n"
        f"价格: {product.price_text}\n"
        f"商品链接: {product.url or '无'}\n"
        f"发布时间: {publish_time}\n"
        f"{keyword_line}"
    ).rstrip("\n")


def format_messages(products: List[Product]) -> str:
    """把多个商品格式化为一段文本，逐条列出。

    Args:
        products: 商品列表。

    Returns:
        拼接后的文本；空列表返回空串。
    """
    if not products:
        return ""
    blocks: List[str] = []
    for index, product in enumerate(products, start=1):
        blocks.append(f"【{index}】\n{format_message(product)}")
    return "\n\n".join(blocks)


def format_markdown(products: List[Product]) -> str:
    """把多个商品格式化为 Markdown（Server酱 / Telegram 用）。"""
    if not products:
        return ""
    lines: List[str] = []
    for index, product in enumerate(products, start=1):
        lines.append(f"**{index}. {product.title}**")
        lines.append(f"- 价格: {product.price_text}")
        lines.append(f"- 商品链接: {product.url or '无'}")
        lines.append(f"- 发布时间: {product.publish_time or '未知'}")
        if product.keyword:
            lines.append(f"- 命中关键词: {product.keyword}")
        lines.append("")
    return "\n".join(lines).strip()


def build_title(products: List[Product]) -> str:
    """生成通知标题。"""
    if not products:
        return DEFAULT_TITLE
    first = products[0]
    if len(products) == 1:
        return f"{DEFAULT_TITLE}｜{first.title[:30]} {first.price_text}"
    return f"{DEFAULT_TITLE}｜发现 {len(products)} 个低价商品"


# ---------------------------------------------------------------------- #
# 抽象基类
# ---------------------------------------------------------------------- #
class Notifier(ABC):
    """通知器抽象基类。"""

    #: 通道名称，用于日志与测试断言
    name: str = "notifier"

    @abstractmethod
    def notify(self, products: List[Product]) -> None:
        """发送提醒。

        Args:
            products: 需要提醒的商品列表（调用方保证非空时才调用）。
        """
        raise NotImplementedError

    def safe_notify(self, products: List[Product]) -> bool:
        """带异常保护的发送，失败只记录 warning。

        Args:
            products: 商品列表。

        Returns:
            True 表示发送成功（或无需发送）。
        """
        if not products:
            return True
        try:
            self.notify(products)
            return True
        except Exception as exc:  # noqa: BLE001 - 通知失败不能中断主循环
            logger.warning("[%s] 通知发送失败：%s", self.name, exc)
            return False


# ---------------------------------------------------------------------- #
# 具体实现
# ---------------------------------------------------------------------- #
class ConsoleNotifier(Notifier):
    """控制台通知：直接打印到 stdout。"""

    name = "console"

    def notify(self, products: List[Product]) -> None:
        """打印提醒到标准输出。"""
        if not products:
            return
        print("=" * 60)
        print(build_title(products))
        print("=" * 60)
        print(format_messages(products))
        print("=" * 60, flush=True)


class ServerChanNotifier(Notifier):
    """Server酱（sctapi.ftqq.com）微信推送。"""

    name = "serverchan"

    def __init__(self, sendkey: str, timeout: float = HTTP_TIMEOUT) -> None:
        """初始化。

        Args:
            sendkey: Server酱 SendKey。
            timeout: 请求超时秒数。

        Raises:
            ValueError: sendkey 为空。
        """
        sendkey = str(sendkey or "").strip()
        if not sendkey:
            raise ValueError("ServerChanNotifier 需要 sendkey")
        self.sendkey: str = sendkey
        self.timeout: float = float(timeout)

    def notify(self, products: List[Product]) -> None:
        """POST 到 Server酱接口。"""
        if not products:
            return
        url = SERVERCHAN_URL_TEMPLATE.format(sendkey=self.sendkey)
        payload = {"title": build_title(products), "desp": format_markdown(products)}
        response = requests.post(url, data=payload, timeout=self.timeout)
        if response.status_code != 200:
            raise RuntimeError(f"Server酱返回 HTTP {response.status_code}")
        logger.info("[serverchan] 已推送 %d 个商品", len(products))


class TelegramNotifier(Notifier):
    """Telegram Bot 推送。"""

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = HTTP_TIMEOUT) -> None:
        """初始化。

        Args:
            bot_token: Bot Token。
            chat_id: 目标会话 ID。
            timeout: 请求超时秒数。

        Raises:
            ValueError: 参数缺失。
        """
        bot_token = str(bot_token or "").strip()
        chat_id = str(chat_id or "").strip()
        if not bot_token or not chat_id:
            raise ValueError("TelegramNotifier 需要 bot_token 与 chat_id")
        self.bot_token: str = bot_token
        self.chat_id: str = chat_id
        self.timeout: float = float(timeout)

    def notify(self, products: List[Product]) -> None:
        """POST 到 Telegram sendMessage 接口。"""
        if not products:
            return
        url = TELEGRAM_URL_TEMPLATE.format(token=self.bot_token)
        text = f"{build_title(products)}\n\n{format_messages(products)}"
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": False}
        response = requests.post(url, data=payload, timeout=self.timeout)
        if response.status_code != 200:
            raise RuntimeError(f"Telegram 返回 HTTP {response.status_code}")
        logger.info("[telegram] 已推送 %d 个商品", len(products))


class EmailNotifier(Notifier):
    """SMTP 邮件通知（支持 STARTTLS / SSL）。"""

    name = "email"

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        to: Any,
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        """初始化。

        Args:
            smtp_host: SMTP 服务器地址。
            smtp_port: SMTP 端口（587 走 STARTTLS，465 走 SSL）。
            username: 登录账号（同时作为发件人）。
            password: 登录密码 / 授权码。
            to: 收件人，字符串或字符串列表。
            use_tls: 是否使用 STARTTLS。
            use_ssl: 是否直接使用 SSL 连接（与 use_tls 互斥，465 端口用）。
            timeout: 连接超时秒数。

        Raises:
            ValueError: 必要参数缺失。
        """
        smtp_host = str(smtp_host or "").strip()
        username = str(username or "").strip()
        password = str(password or "")
        if isinstance(to, str):
            recipients = [x.strip() for x in to.split(",") if x.strip()]
        elif isinstance(to, (list, tuple)):
            recipients = [str(x).strip() for x in to if str(x).strip()]
        else:
            recipients = []

        if not smtp_host or not username or not password or not recipients:
            raise ValueError("EmailNotifier 需要 smtp_host / username / password / to")

        self.smtp_host: str = smtp_host
        self.smtp_port: int = int(smtp_port or 587)
        self.username: str = username
        self.password: str = password
        self.recipients: List[str] = recipients
        self.use_ssl: bool = bool(use_ssl) or self.smtp_port == 465
        self.use_tls: bool = bool(use_tls) and not self.use_ssl
        self.timeout: float = float(timeout)

    def _build_message(self, products: List[Product]) -> MIMEText:
        """构造邮件对象。"""
        body = format_messages(products)
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = Header(build_title(products), "utf-8")
        message["From"] = formataddr((str(Header("闲鱼低价提醒", "utf-8")), self.username))
        message["To"] = ", ".join(self.recipients)
        return message

    def notify(self, products: List[Product]) -> None:
        """通过 SMTP 发送邮件。"""
        if not products:
            return
        message = self._build_message(products)
        if self.use_ssl:
            server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=self.timeout)
        else:
            server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)
        try:
            if self.use_tls:
                server.starttls()
            server.login(self.username, self.password)
            server.sendmail(self.username, self.recipients, message.as_string())
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - 关闭失败无需影响主流程
                pass
        logger.info("[email] 已发送 %d 个商品到 %s", len(products), ", ".join(self.recipients))


class BarkNotifier(Notifier):
    """iOS Bark 推送：`GET {url}/{quote(msg)}`。

    对齐 exe 语义：url 末尾斜杠归一后拼接 quote 编码的消息文本。
    """

    name = "bark"

    def __init__(self, url: str, timeout: float = HTTP_TIMEOUT) -> None:
        """初始化。

        Args:
            url: Bark 服务地址，形如 `https://api.day.app/YourKey/`。
            timeout: 请求超时秒数。

        Raises:
            ValueError: url 为空。
        """
        url = str(url or "").strip()
        if not url:
            raise ValueError("BarkNotifier 需要 url")
        self.url: str = url
        self.timeout: float = float(timeout)

    def notify(self, products: List[Product]) -> None:
        """GET 推送到 Bark 服务。"""
        if not products:
            return
        msg = f"{build_title(products)}\n\n{format_messages(products)}"
        # 尾斜杠归一 + quote 消息（避免消息中的空格/换行破坏 URL）
        url = self.url.rstrip("/") + "/" + requests.utils.quote(msg)
        response = requests.get(url, timeout=self.timeout)
        if response.status_code != 200:
            raise RuntimeError(f"Bark 返回 HTTP {response.status_code}")
        logger.info("[bark] 已推送 %d 个商品", len(products))


class WebhookNotifier(Notifier):
    """企业微信 / 钉钉风格机器人：`POST url` + JSON payload。

    修正 exe 裸 data 未设头的隐患：**显式携带
    `Content-Type: application/json`**，否则部分网关会拒收。
    """

    name = "webhook"

    def __init__(self, url: str, timeout: float = HTTP_TIMEOUT) -> None:
        """初始化。

        Args:
            url: Webhook 地址。
            timeout: 请求超时秒数。

        Raises:
            ValueError: url 为空。
        """
        url = str(url or "").strip()
        if not url:
            raise ValueError("WebhookNotifier 需要 url")
        self.url: str = url
        self.timeout: float = float(timeout)

    def notify(self, products: List[Product]) -> None:
        """POST 企业微信风格 JSON payload。"""
        if not products:
            return
        msg = f"{build_title(products)}\n\n{format_messages(products)}"
        payload = {"msgtype": "text", "text": {"content": msg}}
        headers = {"Content-Type": "application/json"}
        response = requests.post(
            self.url,
            data=json.dumps(payload, ensure_ascii=False),
            headers=headers,
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Webhook 返回 HTTP {response.status_code}")
        logger.info("[webhook] 已推送 %d 个商品", len(products))


# ---------------------------------------------------------------------- #
# 工厂
# ---------------------------------------------------------------------- #
def _build_one(channel: NotifyChannel) -> Optional[Notifier]:
    """根据单个通道配置构造 Notifier，参数不全时返回 None 并 warning。"""
    ctype = channel.type
    try:
        if ctype == "console":
            return ConsoleNotifier()
        if ctype == "serverchan":
            return ServerChanNotifier(sendkey=channel.get("sendkey", ""))
        if ctype == "telegram":
            return TelegramNotifier(
                bot_token=channel.get("bot_token", ""),
                chat_id=channel.get("chat_id", ""),
            )
        if ctype == "email":
            return EmailNotifier(
                smtp_host=channel.get("smtp_host", ""),
                smtp_port=channel.get("smtp_port", 587),
                username=channel.get("username", ""),
                password=channel.get("password", ""),
                to=channel.get("to", ""),
                use_tls=bool(channel.get("use_tls", True)),
                use_ssl=bool(channel.get("use_ssl", False)),
            )
        if ctype == "bark":
            return BarkNotifier(url=channel.get("url", ""))
        if ctype == "webhook":
            return WebhookNotifier(url=channel.get("url", ""))
    except ValueError as exc:
        logger.warning("通知通道 %s 配置不完整，已跳过：%s", ctype, exc)
        return None

    logger.warning("未知通知通道类型，已跳过：%s", ctype)
    return None


def build_notifier(channel: NotifyChannel) -> Optional[Notifier]:
    """根据**单个**通道配置构造 Notifier（公开封装）。

    图形界面的「测试发送」需要单独构造某一个通道，故对外暴露此函数。

    Args:
        channel: 单个通道配置。

    Returns:
        构造成功返回 Notifier；类型未知或参数不全时返回 None。
    """
    return _build_one(channel)


def build_notifiers(config: Config) -> List[Notifier]:
    """根据配置构建通知器列表。

    参数不全的通道会被跳过并打 warning；若最终一个都没有，
    则兜底返回 [ConsoleNotifier()]，保证提醒不会「静默丢失」。

    Args:
        config: 全局配置。

    Returns:
        Notifier 列表（至少含一个元素）。
    """
    notifiers: List[Notifier] = []
    for channel in config.notify.channels:
        notifier = _build_one(channel)
        if notifier is not None:
            notifiers.append(notifier)

    if not notifiers:
        logger.warning("没有任何可用的通知通道，已回退到控制台输出")
        notifiers.append(ConsoleNotifier())
    return notifiers
