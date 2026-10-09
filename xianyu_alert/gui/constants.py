"""GUI 常量层：窗口尺寸、通道元数据、版本历史等不依赖 widget 的定义（v1.9.5 从 gui.py 拆出）。"""

from __future__ import annotations

import logging
import os
from typing import Any

from .. import __version__

# 防御性导入：无图形环境（如 CI / 无 tkinter 的打包机）也能 import 本模块
# 并运行全部纯函数测试；真正构造窗口时 tkinter 必须可用。
try:  # pragma: no cover - 分支由运行环境决定
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    TK_AVAILABLE = True
except ImportError:  # pragma: no cover - 无 tkinter 环境
    tk = None  # type: ignore[misc,assignment]
    messagebox = None  # type: ignore[misc,assignment]
    ttk = None  # type: ignore[misc,assignment]
    ScrolledText = None  # type: ignore[misc,assignment]
    TK_AVAILABLE = False
from ..config import (
    DEFAULT_PRESET_EXCLUDE_KEYWORDS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------- #
# 常量
# ---------------------------------------------------------------------- #
#: v3.2 起标题栏直接带版本号，用户一眼可知当前版本
WINDOW_TITLE = f"闲鱼低价提醒工具 v{__version__}"
#: v3.6 窗口加宽：让「监控配置」页整排按钮（➕ 添加 / ✏️ 更新选中 / 删除 / 编辑…
#: / 预置词…）在默认窗口内完整可见不溢出；同时适当抬高最小尺寸下限。
WINDOW_SIZE = "1020x720"
MIN_WINDOW_SIZE = (880, 600)
#: UI 队列轮询间隔（毫秒）：队列有消息时用这个频率快速消费
POLL_INTERVAL_MS = 100
#: UI 队列空闲轮询间隔（毫秒，v3.5）：队列为空（挂机）时降到 500ms，
#: 把空转唤醒次数从 10 次/秒降到 2 次/秒，降低挂机 CPU 消耗；
#: 队列一旦有消息立即回到 POLL_INTERVAL_MS。
POLL_IDLE_INTERVAL_MS = 500
#: 单次 `_poll_queue` 最多处理的消息条数（v3.6 UI 防卡）：
#: 关闭「仅展示符合的低价」或日志洪峰时，队列可能瞬间积压大量日志；
#: 若一次性全部在主线程渲染（每次 insert + see("end") 都是 O(n)），
#: 会把主线程拖住数秒。限制单次处理量，剩余消息留到下一轮 after 再消费，
#: 保证窗口始终可拖动、可点击。
MAX_QUEUE_MESSAGES_PER_POLL = 200
#: 提醒记录「加入黑名单」弹窗的默认原因（v3.6）
BLACKLIST_REASON_DEFAULT = "人工剔除"
#: 日志区最多保留的行数（超出后从头裁剪，避免长期运行内存膨胀）
MAX_LOG_LINES = 2000
#: 启动时从数据库加载的历史提醒条数
HISTORY_LIMIT = 200
DEFAULT_DB_PATH = os.path.join("state", "xianyu_alert.db")
#: 关闭窗口时等待监控线程收尾的最大秒数（v3.5 稳定性修复）。
#: 监控线程若正卡在网络请求（mtop 超时 20s + 重试退避）无法立刻响应停止信号，
#: 主线程只等这么久就继续销毁窗口，避免「关闭卡死 / 进程残留」。
CLOSE_JOIN_TIMEOUT = 5.0
#: 「校验在架」批量检查时相邻两次详情接口请求的最小间隔秒数（v3.7）。
#: 详情接口与搜索接口共用同一套 mtop 签名与风控策略，批量校验必须限速，
#: 避免短时间内高频请求触发风控。
SOLD_CHECK_INTERVAL = 1.5
#: 单次「校验在架」最多检查的提醒记录条数（防止一次点按钮请求过猛）。
SOLD_CHECK_MAX_ITEMS = 30
#: 「标记已售出 / 校验在架」的默认原因文案（写回 product.sold_reason）。
SOLD_REASON_MANUAL = "人工标记"
SOLD_REASON_DETAIL = "详情接口判定"

#: 预置排除词（「添加预置排除词」按钮一次性写入；对应回收商/置换商典型帖子）
#: v3.3 起追加「收」，且**添加新关键词时自动预置**（必含词保持留空由用户自填）。
#: v3.5 起可配置：该常量仅作默认值兜底，实际预置词从
#: `config.yaml` 顶层 `preset_exclude_keywords`（GUI「编辑预置排除词」弹窗可改）读取；
#: 缺省时回退到 `DEFAULT_PRESET_EXCLUDE_KEYWORDS`（向后兼容）。
PRESET_EXCLUDE_KEYWORDS: tuple[str, ...] = tuple(DEFAULT_PRESET_EXCLUDE_KEYWORDS)

#: Cookie 状态灯（v3 升级为六态：未配置 / 缺 token / 已过期 / 即将过期 / 无法解密 / 正常）
COOKIE_STATE_MISSING = "missing"
COOKIE_STATE_NO_TOKEN = "no_token"
COOKIE_STATE_EXPIRED = "expired"
COOKIE_STATE_EXPIRING = "expiring"
COOKIE_STATE_UNDECRYPTABLE = "undecryptable"
COOKIE_STATE_OK = "ok"

#: 抓取器下拉框：(内部值, 界面显示文案)
#: v3.2 起：只展示 mtop（默认，★推荐）+ mock（标注「开发演示用」）；
#:           web 不再展示（代码保留为 legacy，向后兼容旧配置）。
FETCHER_CHOICES: tuple[tuple[str, str], ...] = (
    ("mtop", "mtop（真实抓取闲鱼，需登录 Cookie）★推荐"),
    ("mock", "mock（开发演示用，本地假数据，无需登录）"),
)

#: 通知通道展示顺序（v3 新增 bark / webhook）
CHANNEL_ORDER: tuple[str, ...] = ("console", "serverchan", "email", "telegram", "bark", "webhook")
#: 通道中文名
CHANNEL_LABELS: dict[str, str] = {
    "console": "控制台（打印到日志区，永远可用）",
    "serverchan": "Server酱（微信推送）",
    "email": "邮件（SMTP）",
    "telegram": "Telegram Bot",
    "bark": "Bark（iOS 推送）",
    "webhook": "企业微信机器人（Webhook）",
}
#: 各通道必填字段
CHANNEL_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "console": (),
    "serverchan": ("sendkey",),
    "email": ("smtp_host", "smtp_port", "username", "password", "to"),
    "telegram": ("bot_token", "chat_id"),
    "bark": ("url",),
    "webhook": ("url",),
}
#: 各通道字段的界面定义：(字段名, 中文标签, 是否密码框, 默认值)
CHANNEL_FIELDS: dict[str, tuple[tuple[str, str, bool, str], ...]] = {
    "console": (),
    "serverchan": (("sendkey", "SendKey", True, ""),),
    "email": (
        ("smtp_host", "SMTP 服务器", False, "smtp.qq.com"),
        ("smtp_port", "端口（465=SSL / 587=TLS）", False, "465"),
        ("username", "账号（同时作为发件人）", False, ""),
        ("password", "密码 / 授权码", True, ""),
        ("to", "收件人（多个用英文逗号分隔）", False, ""),
    ),
    "telegram": (
        ("bot_token", "Bot Token", True, ""),
        ("chat_id", "Chat ID", False, ""),
    ),
    "bark": (("url", "Bark URL（形如 https://api.day.app/YourKey/）", False, "https://api.day.app/"),),
    "webhook": (("url", "Webhook URL（企业微信群机器人地址）", False, ""),),
}

#: config.yaml 不存在时使用的内置默认配置（v3.2：间隔 600s、抓取器 mtop）
DEFAULT_CONFIG_DICT: dict[str, Any] = {
    "keywords": [{"keyword": "Switch", "max_price": 1000}],
    "monitor": {"interval_seconds": 600, "user_agent": "", "cookies": ""},
    "fetcher": {"type": "mtop", "mock_products_per_round": 5, "mock_fail_rounds": []},
    "storage": {"path": DEFAULT_DB_PATH},
    "notify": {"channels": [{"type": "console"}]},
    "preset_exclude_keywords": list(DEFAULT_PRESET_EXCLUDE_KEYWORDS),
}

#: 功能更新日志（v3.2：「关于」对话框展示版本历史）
UPDATE_LOG = (
    "## 版本历史\n"
    "- **v1.10.1** 存储层类型化记录（NotifiedRecord/SoldOutRecord/BlacklistEntry，保留字典式访问）；Web 层裸 SQL 收敛回存储层；配置带版本号并可迁移\n"
    "- **v1.10.0** 监控调度与过滤补强：配置按轮次热更（改完不必重启）；过滤判定回传原因；保活与轮次统一到单一时间源；新增轮次级指标（耗时/抓取/过滤/命中）\n"
    "- **v1.9.9** 抓取层 fetcher.py（1451 行）拆为包：constants / base / mtop_api（纯函数）/ mtop / web / mock / factory；对外命名空间零缺失\n"
    "- **v1.9.8** 开机自启三平台统一：新增 autostart 模块（macOS LaunchAgent / Linux systemd --user / Windows 启动文件夹）+ CLI 子命令 + GUI 一键按钮（Tk/Qt）\n"
    "- **v1.9.7** Web 后端（monitor_service.py 1561 行）拆为包：constants / logging_bridge / forms / cookie_pool / shelf_check / keepalive / service；主服务类按功能拆 mixin，service.py 降到 489 行（对外命名空间零缺失）\n"
    "- **v1.9.6** 网页兜底采集（全项目最低分模块）改造：解析层独立为 web_parse.py（三级策略 + 声明式选择器表 + 诊断报告）；共享解析原语抽到 parsing.py；新增 19 条 HTML 夹具测试；文档补齐（0 条时日志直接说明原因与排查方向）\n"
    "- **v1.9.5** gui.py（3932 行单文件）拆为 gui/ 包：constants（常量）/ helpers（纯函数） / app（主窗口类）；对外命名空间 118 个名字与 72 个类成员**零缺失**（机械校验）\n"
    "- **v1.9.4** 修复 CLI 机器可读输出：日志改走 stderr（此前日志写 stdout，会让 xianyu-alert cookie status --json 的管道消费方解析失败）\n"
    "- **v1.9.3** CLI 增强：once/list/cookie status 支持 --json 机器可读输出；新增 config validate（改完配置先验一遍）与 cookie keepalive（查看/开关空闲保活）；顺带把「局部导入晚于使用」的运行时陷阱拆掉\n"
    "- **v1.9.2** 保活探测改为把服务端刷新的 _m_h5_tk 节流落盘（此前一次性 fetcher 会把新令牌丢掉，界面一直显示令牌已过期，而实际会话已被续期）\n"
    "- **v1.9.1** 修复保活线程接线（Web 入口与 API 此前用了两个 MonitorService 实例，导致保活状态误报未运行）；GUI（Qt/Tk）Cookie 校验规则与核心保存路径统一\n"
    "- **v1.9.0** Cookie 管理大改造：分层凭据模型（登录态 / 会话凭据 / 签名令牌 / 风控指纹）；修正 mtop 令牌内嵌时间戳语义（是过期时刻，不是签发时刻）；令牌过期不再被判定为不可用（服务端会下发新令牌自愈）；新增空闲保活，默认每 30 分钟维持一次登录态\n"
    "- **v1.8.5** 界面适配宽屏：内容区宽度上限由 1080px 改为 min(1720px, 96vw)，命中战果在宽屏自动多列排布（不再把单行拉长）；顶栏新增明确的开始/停止监控按钮（此前只有可点击的状态胶囊，看不出来能点）\n"
    "- **v1.8.4** 命中战果支持查看商品大图：缩略图悬停浮出中图预览（桌面端），点击打开全屏大图（原图，最大细节）并可在同一列表内左右连续翻看、Esc 关闭；列表缩略图改用 CDN 小图变体（约 7KB）；触屏设备点击同样可看大图，大图自适应窗口尺寸\n"
    "- **v1.8.3** 命中战果展示商品主图（解析 mtop picUrl 与网页卡片图，协议相对 / http 地址统一升级 https；库表自动迁移新增 image_url 列，存量记录显示「无图」占位）；容器改为非 root（uid 1000）运行；新增 CI 质量门禁（ruff + mypy + 三平台测试 + 覆盖率门槛）与 Docker Hub 多架构镜像发布；全仓库静默吞异常改为 contextlib.suppress\n"
    "- **v1.8.2** 登录令牌自动续期链路：TTL 按实测校准为 90 分钟（此前误按 24 小时，"
    "会把已失效令牌报成「正常」）、`_m_h5_tk_enc` 与 `_m_h5_tk` 成对吸收（修正轮换时错配）、"
    "刷新后的令牌节流落盘（进程重启不再退回旧令牌）、抓取失败按「令牌 / 会话 / 风控 / 配置」"
    "分层给出正确处置建议（风控时不再误导去重新登录）；新增免扫码刷新入口"
    "（`cli cookie refresh` + Web「静默刷新」按钮），配合落在数据目录的浏览器持久化 profile，"
    "做到「人工登录一次，之后自动续期」\n"
    "- **v1.8.1** 源码级排查修复：Cookie 池数据保真（密钥变更/丢失时，无法解密的条目"
    "不再被静默删除，改为保留原密文并在界面标记「无法解密」）、"
    "「校验在架」不再把未知 itemStatus 误判为已售出（避免在架低价商品被标记并隐藏）、"
    "多账号 Cookie 轮换前清空会话 jar（修正旧 _m_h5_tk_enc 与新 _m_h5_tk 串号）、"
    "Cookie 健康检测改以服务端续期后的 token 为准（消除「已过期」假告警）；"
    "Web 前端全量重写（态势总览/监控配置/命中战果/通知与系统四视图 + ⌘K 命令面板 + SSE 实时日志），"
    "并修复认证后日志流不重连、切换「显示已售出」误报新命中等问题\n"
    "- **v1.8.0** Cookie 过期自动检测与提醒（过期/即将过期推送全部通知通道，状态跃迁去抖）、"
    "「🔄 一键刷新 Cookie」入口（GUI 校验 + Fernet 加密回写 + 状态灯即时变绿）、"
    "多 Cookie 池过期条目自动跳过（仅用 ok/expiring 条目轮换）、"
    "进程单实例锁（GUI / cli run / once 共用一把锁，双开第二实例提示退出）、"
    "cli login 保存前自动校验（无效 Cookie 拒绝保存）、cli cookie status 只检测不写入\n"
    "- **v1.7.0** 关键词可启用/停用（停用不删除，监控跳过停用词）、"
    "运行日志高亮（新商品/低价命中蓝色加粗、完成绿色、轮次分隔、已下架灰色）、"
    "提醒记录不再显示已售出/下架商品（手动标记 + 详情接口「校验在架」，默认隐藏可切换显示）\n"
    "- **v1.6.0** 「添加」与「更新选中」按钮拆分（修正错别字/改名不再是新增而是更新原行）、"
    "窗口加宽自适应（filters 列拉伸）、修复「立即执行一轮 / 开始监控」后窗口无响应"
    "（后台监控线程不再触碰任何 tkinter 控件 + 日志洪峰分批渲染）、"
    "新增临时黑名单（提醒记录中人工剔除噪音商品 → 不再提醒、不再进提醒记录，支持恢复）\n"
    "- **v1.0.0** 初始 CLI：多关键词+阈值、循环监测、新商品筛选、SQLite 去重、4 通道通知\n"
    "- **v1.1.0** GUI + mtop 真实抓取 + Cookie 自动获取 + DPAPI 加密 + 六态检测\n"
    "- **v1.2.0** 排除关键词 + 必含词过滤 + 打包独立 exe\n"
    "- **v1.3.0** 默认间隔 600s、mtop 默认、多 Cookie 管理、日志清空/排序/可读性、版本号+更新日志\n"
    "- **v1.4.0** 新关键词默认预置排除词（含「收」）、必含词留空自填、移除 GUI 自动获取 Cookie"
    "（改「Cookie 管理」内手动步骤）、企业微信机器人通道更名、日志「仅展示符合的低价」开关、"
    "搜索排序修正（sortField=create + sortValue=desc = 最新发布）\n"
    "- **v1.4.1** 服务端价格筛选（抓取结果与网页「最新发布+价格<阈值」一致，实机验证）、"
    "修复 Cookie 管理「添加」按钮无反应\n"
    "- **v1.5.0** 预置排除词可配置可持久化（「编辑预置排除词」弹窗，新关键词自动带上定制预置词）、"
    "关闭流程稳定性修复（停止信号 → join 超时 → 取消 after → 移除日志 handler → 销毁窗口，"
    "避免运行几轮后关闭 GUI 卡死 / 进程残留）\n"
)

#: 手动粘贴 Cookie 的操作说明
COOKIE_MANUAL_HELP = (
    "手动获取 Cookie 步骤：\n"
    "  1. 用 Chrome / Edge 打开 https://www.goofish.com 并登录你的闲鱼账号；\n"
    "  2. 按 F12 打开开发者工具，切到「网络 / Network」标签；\n"
    "  3. 在闲鱼页面随便搜索一个词（例如 Switch）；\n"
    "  4. 在请求列表中找到发往 h5api.m.goofish.com 的请求并点击；\n"
    "  5. 在「标头 / Headers」→「请求标头 / Request Headers」中找到 Cookie；\n"
    "  6. 复制整行 Cookie 的值（必须包含 _m_h5_tk=）粘贴到下方输入框。"
)


# ====================================================================== #
# 纯函数区（不依赖任何 widget，便于单元测试）
# ====================================================================== #

__all__ = [
    "WINDOW_TITLE",
    "WINDOW_SIZE",
    "MIN_WINDOW_SIZE",
    "POLL_INTERVAL_MS",
    "POLL_IDLE_INTERVAL_MS",
    "MAX_QUEUE_MESSAGES_PER_POLL",
    "BLACKLIST_REASON_DEFAULT",
    "MAX_LOG_LINES",
    "HISTORY_LIMIT",
    "DEFAULT_DB_PATH",
    "CLOSE_JOIN_TIMEOUT",
    "SOLD_CHECK_INTERVAL",
    "SOLD_CHECK_MAX_ITEMS",
    "SOLD_REASON_MANUAL",
    "SOLD_REASON_DETAIL",
    "PRESET_EXCLUDE_KEYWORDS",
    "COOKIE_STATE_MISSING",
    "COOKIE_STATE_NO_TOKEN",
    "COOKIE_STATE_EXPIRED",
    "COOKIE_STATE_EXPIRING",
    "COOKIE_STATE_UNDECRYPTABLE",
    "COOKIE_STATE_OK",
    "FETCHER_CHOICES",
    "CHANNEL_ORDER",
    "CHANNEL_LABELS",
    "CHANNEL_REQUIRED_FIELDS",
    "CHANNEL_FIELDS",
    "DEFAULT_CONFIG_DICT",
    "UPDATE_LOG",
    "COOKIE_MANUAL_HELP",
]
