"""命令行入口。

用法：
    python -m xianyu_alert.cli run  --config config.yaml   # 持续监测
    python -m xianyu_alert.cli once --config config.yaml   # 只跑一轮（适合 cron）
    python -m xianyu_alert.cli list --config config.yaml   # 查看已提醒记录
    python -m xianyu_alert.cli login --config config.yaml  # 获取闲鱼 Cookie 并写入配置
    python -m xianyu_alert.cli shortcut                    # 在桌面创建快捷方式
    python -m xianyu_alert.cli gui  --config config.yaml   # 启动图形界面（推荐新手）
    python -m xianyu_alert.cli --version
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from typing import List, Optional

from . import __version__, paths
from .config import Config, ConfigError, load_config
from .cookie import (
    LoginTimeout,
    PlaywrightUnavailable,
    acquire_via_playwright,
    acquire_via_prompt,
    ensure_cookie_encrypted,
    save_cookies_validated,
)
from .fetcher import Fetcher, build_fetcher
from .monitor import Monitor
from .notifier import Notifier, build_notifiers
from .singleton import acquire_instance_lock, lock_holder_pid, release_instance_lock
from .storage import Storage

logger = logging.getLogger("xianyu_alert")

DEFAULT_CONFIG_PATH = paths.default_config_path()
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
#: 滚动日志文件大小上限（1MB）
LOG_MAX_BYTES = 1_000_000
#: 滚动日志保留份数
LOG_BACKUP_COUNT = 3


def install_file_logging() -> Optional[str]:
    """安装滚动文件日志到 `state/xianyu_alert.log`（frozen 后为 exe 同目录）。

    windowed 打包的 exe 没有控制台，文件日志是唯一查错通道。
    重复调用是幂等的（同一进程只挂一个 FileHandler）。

    Returns:
        日志文件路径；失败返回 None。
    """
    try:
        log_dir = paths.default_state_dir()
        log_path = os.path.join(log_dir, "xianyu_alert.log")
        target = logging.getLogger("xianyu_alert")
        for handler in target.handlers:
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                return log_path
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        target.addHandler(handler)
        logger.info("日志已写入文件：%s", log_path)
        return log_path
    except Exception as exc:  # noqa: BLE001 - 文件日志失败不能阻断运行
        logging.getLogger("xianyu_alert").warning("无法创建文件日志：%s", exc)
        return None


def setup_logging(verbose: bool = False) -> None:
    """配置全局日志（控制台 + 滚动文件）。

    Args:
        verbose: True 时输出 DEBUG 级别日志。
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
    )
    install_file_logging()


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器。"""
    parser = argparse.ArgumentParser(
        prog="xianyu-alert",
        description="闲鱼低价提醒工具：周期性监测关键词商品，低于价格阈值时推送通知。",
    )
    parser.add_argument("--version", action="version", version=f"xianyu-alert {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    for name, help_text in (
        ("run", "持续按间隔循环监测（Ctrl+C 退出）"),
        ("once", "只执行一轮监测后退出（适合 cron / 计划任务）"),
        ("list", "列出已提醒过的商品记录"),
        ("login", "获取闲鱼 Cookie 并写入 monitor.cookies（半自动/粘贴/脚本三种模式）"),
        ("shortcut", "在桌面创建快捷方式（指向本程序 / 打包后的 exe）"),
        ("gui", "启动图形界面（推荐不熟悉命令行的用户）"),
        ("cookie", "Cookie 相关子命令（当前支持 status：只检测不写入）"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "-c", "--config", default=DEFAULT_CONFIG_PATH,
            help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
        )
        sub.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
        if name == "run":
            sub.add_argument(
                "--max-rounds", type=int, default=None,
                help="最多运行多少轮后退出（默认无限）",
            )
        if name == "list":
            sub.add_argument("--limit", type=int, default=50, help="最多显示多少条（默认 50）")
        if name == "login":
            sub.add_argument(
                "--cookie-string", default=None,
                help="直接传入 Cookie 请求头字符串并存盘（脚本模式，跳过浏览器与交互）",
            )
        if name == "shortcut":
            sub.add_argument("--name", default="闲鱼低价提醒工具", help="快捷方式名称（不含 .lnk）")
        if name == "cookie":
            # v1.8：`cli cookie status` —— 只检测健康状态，不写入任何配置
            cookie_subs = sub.add_subparsers(dest="cookie_command")
            sub_status = cookie_subs.add_parser(
                "status", help="只检测并打印单值 + Cookie 池健康状态（不写入，适合脚本/ssh 巡检）"
            )
            sub_status.add_argument(
                "-c", "--config", default=DEFAULT_CONFIG_PATH,
                help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
            )
            sub_status.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    return parser


def _validate_ranges(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """CLI 参数范围校验：负数 / 零直接拒绝并给出提示（R1）。"""
    if args.command == "run":
        max_rounds = getattr(args, "max_rounds", None)
        if max_rounds is not None and max_rounds <= 0:
            parser.error("--max-rounds 必须为正整数（当前 %s）" % max_rounds)
    if args.command == "list":
        limit = getattr(args, "limit", 50)
        if limit <= 0:
            parser.error("--limit 必须为正整数（当前 %s）" % limit)


def _prepare(config: Config) -> tuple[Fetcher, Storage, List[Notifier]]:
    """根据配置构建运行所需的三大组件。

    Args:
        config: 全局配置。

    Returns:
        (fetcher, storage, notifiers) 三元组。
    """
    fetcher = build_fetcher(config)
    storage = Storage(config.storage.path)
    notifiers = build_notifiers(config)
    return fetcher, storage, notifiers


def cmd_run(args: argparse.Namespace) -> int:
    """执行 run 子命令。"""
    config = load_config(args.config)
    fetcher, storage, notifiers = _prepare(config)
    monitor = Monitor(config, fetcher, storage, notifiers)
    try:
        monitor.run_forever(max_rounds=getattr(args, "max_rounds", None))
    finally:
        fetcher.close()
        storage.close()
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    """执行 once 子命令。"""
    config = load_config(args.config)
    fetcher, storage, notifiers = _prepare(config)
    monitor = Monitor(config, fetcher, storage, notifiers)
    try:
        count = monitor.run_once()
        print(f"\n本轮共触发 {count} 条低价提醒。")
    finally:
        fetcher.close()
        storage.close()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """执行 list 子命令：展示已提醒记录。"""
    config = load_config(args.config)
    storage = Storage(config.storage.path)
    try:
        rows = storage.list_notified(limit=args.limit)
        if not rows:
            print("暂无已提醒记录。")
            return 0
        print(f"共 {len(rows)} 条已提醒记录：\n")
        for row in rows:
            print(f"[{row['keyword']}] {row['title']}")
            print(f"    价格: ¥{float(row['price']):.2f}")
            print(f"    链接: {row['url']}")
            print(f"    发布时间: {row['publish_time'] or '未知'}")
            print(f"    提醒时间: {row['last_seen']}")
            print("")
    finally:
        storage.close()
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """执行 login 子命令：获取 Cookie 并写入 config 的 monitor.cookies。

    三种模式（按优先级）：
        1. --cookie-string 直接传入（脚本/CI 用）；
        2. Playwright 半自动：打开浏览器让用户登录后自动提取；
        3. Playwright 不可用时降级为终端粘贴模式。

    v1.8（C15/C20）：任何模式保存前都经 `save_cookies_validated` 校验——
    非 `ok` 状态（缺 token / 已过期 / 无法解密）**拒绝保存**并给出可操作原因，
    配置内容保持不变、退出码非 0。

    login = 「首次登录 + 刷新」双重语义（Q3）：GUI 挂机时也可用本命令刷新 Cookie，
    login 不参与单实例锁（写 config.yaml 不写 SQLite）。
    """
    config_path: str = args.config

    # 模式 1：脚本直传
    cookie_string = getattr(args, "cookie_string", None)
    if cookie_string is not None:
        try:
            save_cookies_validated(config_path, cookie_string)
        except ValueError as exc:
            logger.error("保存失败：%s", exc)
            return 2
        if paths.is_frozen():
            ensure_cookie_encrypted(config_path)
        print(f"Cookie 已写入 {config_path} 的 monitor.cookies（已校验并加密保存），可运行 once 验证。")
        return 0

    # 模式 2：Playwright 半自动
    try:
        cookie_str = acquire_via_playwright()
        save_cookies_validated(config_path, cookie_str)
        if paths.is_frozen():
            ensure_cookie_encrypted(config_path)
        print(
            f"已自动写入 {config_path} 的 monitor.cookies（含 _m_h5_tk，已校验并加密保存），"
            "可运行 `python -m xianyu_alert.cli once` 验证。"
        )
        return 0
    except PlaywrightUnavailable as exc:
        print(f"[提示] {exc}\n已切换到手动粘贴模式。")
    except LoginTimeout as exc:
        logger.error("%s", exc)
        return 3
    except KeyboardInterrupt:
        logger.info("已被用户取消。")
        return 130

    # 模式 3：降级为终端粘贴
    try:
        cookie_str = acquire_via_prompt()
        save_cookies_validated(config_path, cookie_str)
        if paths.is_frozen():
            ensure_cookie_encrypted(config_path)
        print(f"Cookie 已写入 {config_path} 的 monitor.cookies（已校验并加密保存）。")
        return 0
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    except (KeyboardInterrupt, EOFError):
        logger.info("已被用户取消。")
        return 130


def cmd_cookie_status(args: argparse.Namespace) -> int:
    """执行 `cookie status` 子命令：只检测健康状态，**不写入**任何配置（C10）。

    打印单值 `monitor.cookies` + Cookie 池各条目的健康状态（`detect_cookie_health`），
    Cookie 一律 `secure.mask_cookie` 脱敏回显（C19），便于脚本 / ssh 远程巡检。

    Args:
        args: 已解析参数（含 cookie_command / config / verbose）。

    Returns:
        0 表示检测完成；用法错误返回 1。
    """
    if getattr(args, "cookie_command", None) != "status":
        print("用法：xianyu-alert cookie status [--config 配置文件路径]")
        return 1

    from . import secure
    from .cookie import detect_cookie_health

    config = load_config(args.config)
    print(f"配置文件：{os.path.abspath(args.config)}")
    print(f"抓取方式：{config.fetcher.type}")

    single = str(config.monitor.cookies or "")
    state, reason = detect_cookie_health(single)
    masked = secure.mask_cookie(single) or "（空）"
    print(f"单值 Cookie：{masked} → {state}（{reason}）")

    pool = config.monitor.cookie_pool or []
    if pool:
        print(f"Cookie 池（共 {len(pool)} 条）：")
        for item in pool:
            enabled = bool(getattr(item, "enabled", True))
            cookie = str(getattr(item, "cookie", "") or "")
            st, rs = detect_cookie_health(cookie)
            status_text = "启用" if enabled else "停用"
            print(
                f"  - {item.name}（{status_text}）：{secure.mask_cookie(cookie) or '（空）'} "
                f"→ {st}（{rs}）"
            )
    else:
        print("Cookie 池：未配置")
    return 0


def cmd_shortcut(args: argparse.Namespace) -> int:
    """执行 shortcut 子命令：在桌面创建快捷方式（仅 Windows 支持）。"""
    from .shortcut import create_shortcut, supported

    if not supported():
        # macOS：无 .lnk 语义，.app 拖入「应用程序」/ Dock 即用
        print("当前平台不支持创建桌面快捷方式（仅 Windows）。")
        print("macOS 上请直接把 .app 拖入「应用程序」文件夹或 Dock。")
        return 1

    result = create_shortcut(name=getattr(args, "name", "闲鱼低价提醒工具"))
    if result:
        print(f"已在桌面创建快捷方式：{result}")
        return 0
    print("创建桌面快捷方式失败，请查看日志。")
    return 1


def cmd_gui(args: argparse.Namespace) -> int:
    """执行 gui 子命令：启动图形界面（按平台分发）。

    - macOS（sys.platform == "darwin"）→ PySide6（Qt）原生观感 GUI；
    - 其余平台（Windows / Linux）→ Tkinter（原样保留，零回归）；
    - macOS 上 PySide6 缺失时回退 Tkinter 并打 warning。

    图形界面模块延迟导入，避免无 GUI 环境下 import 本模块即失败。
    """
    if sys.platform == "darwin":
        try:
            # gui_qt 顶层不 import PySide6（防御性设计），因此不能靠
            # `from .gui_qt import main` 失败来探测缺失；必须先显式探测
            # is_available()，再调用 main()（main 内部的 QApplication 导入
            # 失败也会被同一 except ImportError 捕获 → 回退 Tk，Bug #2）。
            from .gui_qt import is_available, main as qt_main

            if not is_available():
                raise ImportError("PySide6 模块不可用")
            return qt_main(config_path=args.config)
        except ImportError as exc:
            logger.warning("PySide6 不可用，回退 Tkinter 图形界面：%s", exc)

    try:
        from .gui import main as gui_main
    except ImportError as exc:
        logger.error(
            "无法加载图形界面模块（可能缺少 tkinter）：%s\n"
            "Windows / macOS 官方 Python 自带 tkinter；"
            "Linux 请安装系统包，例如 `sudo apt install python3-tk`。",
            exc,
        )
        return 2
    return gui_main(config_path=args.config)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口。

    Args:
        argv: 命令行参数（默认取 sys.argv[1:]）。

    Returns:
        进程退出码，0 表示成功。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 1

    # R1：参数范围校验（负数 / 零拒绝）
    _validate_ranges(args, parser)

    setup_logging(getattr(args, "verbose", False))

    handlers = {
        "run": cmd_run,
        "once": cmd_once,
        "list": cmd_list,
        "login": cmd_login,
        "shortcut": cmd_shortcut,
        "gui": cmd_gui,
        "cookie": cmd_cookie_status,
    }
    handler = handlers[args.command]

    # v1.8 单实例锁（L1/L6）：GUI / run / once 都会打开 SQLite 写库，共用一把锁；
    # 冲突 → stderr 中文提示 + 退出码 2（不阻塞、不抢锁）。
    # login / list / shortcut / cookie 不参与锁（login 只写 config.yaml、list/cookie 只读）。
    lock = None
    if args.command in ("run", "once", "gui"):
        lock = acquire_instance_lock()
        if lock is None:
            holder = lock_holder_pid()
            print(f"已有实例运行中（PID {holder or '未知'}），请先退出再运行。", file=sys.stderr)
            return 2

    try:
        return handler(args)
    except ConfigError as exc:
        logger.error("配置错误：%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("已被用户中断。")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底
        logger.exception("运行失败：%s", exc)
        return 1
    finally:
        release_instance_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
