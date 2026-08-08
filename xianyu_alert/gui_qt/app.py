"""Qt 主窗口：XianyuAlertQtApp(QMainWindow) — 三页签 + 原生菜单栏 + 线程装配。

设计对齐（macOS 适配设计文档 §3.1.1 / §3.1.2 / 时序图）：
    - 三页签：监控配置（tab_config）/ 通知设置（tab_notify）/ 运行监控（tab_run）；
    - 原生菜单栏 QMenuBar：关于 / 保存配置 / 如何获取 Cookie / 查看日志目录 / 版本 / 退出；
    - 线程模型：`MonitorWorker(QThread)` + `ui_message` 信号跨线程投递；
      `QtLogHandler → LogBridge → LogView` 日志渲染通路；`QTimer(1000ms)` 状态刷新；
    - 优雅关闭：`closeEvent` → request_stop → wait(CLOSE_JOIN_TIMEOUT) → 移除日志
      handler → 保存配置（Fernet 加密）→ accept；
    - 提醒记录操作（标记售出 / 校验在架 / 加入黑名单）复用 Storage / fetcher 纯逻辑，
      网络操作一律后台线程（SoldCheckWorker），控件只在主线程被触碰。

**线程铁律**：后台线程（MonitorWorker / SoldCheckWorker / logging 线程）
**绝不访问任何 Qt 控件**；控件状态在主线程一次性读取为普通值传给后台线程；
控件更新只发生在主线程槽函数（_handle_ui_message 等）。
"""

from __future__ import annotations

import logging
import os
import sys
import time
import webbrowser
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QMenu,
    QLabel,
)

from .. import __version__, paths
from ..config import Config, ConfigError, config_from_dict
from ..gui import (
    CHANNEL_LABELS,
    DEFAULT_DB_PATH,
    HISTORY_LIMIT,
    about_full_text,
    blacklist_alert_row,
    channel_is_complete,
    config_file_mtime,
    make_sample_product,
    normalize_channel_options,
    save_raw_config,
)
from ..notifier import build_notifier
from ..storage import Storage
from .state import form_to_config_dict, load_form
from .tab_config import MonitorConfigTab
from .tab_notify import NotifyConfigTab
from .tab_run import RunMonitorTab
from .workers import (
    CLOSE_JOIN_TIMEOUT,
    MonitorWorker,
    QtLogHandler,
    LogBridge,
    SoldCheckWorker,
    TestChannelWorker,
)

logger = logging.getLogger(__name__)


class XianyuAlertQtApp(QMainWindow):
    """闲鱼低价提醒工具 Qt 主窗口。"""

    def __init__(self, config_path: str = "") -> None:
        """构造窗口与全部控件。

        Args:
            config_path: 配置文件路径；空串时使用 paths.default_config_path()。
        """
        super().__init__()
        self.config_path: str = str(config_path or "") or paths.default_config_path()
        #: 数据目录（macOS .app 落 ~/Library/Application Support/闲鱼低价提醒工具/）
        paths.ensure_data_dir()

        # ---- 运行时状态 ----
        self._raw_config: Dict[str, Any] = {}
        self._form: Dict[str, Any] = {}
        self._storage_path: str = DEFAULT_DB_PATH
        self._running: bool = False
        self._worker: Optional[MonitorWorker] = None
        self._sold_worker: Optional[SoldCheckWorker] = None
        self._test_workers: List[TestChannelWorker] = []
        self._closing: bool = False

        self.setWindowTitle(f"闲鱼低价提醒工具 v{__version__}")
        self.resize(1020, 720)
        self.setMinimumSize(880, 600)

        self._load_form()
        #: v1.8（C22）：config.yaml 的 mtime 快照，用于检测外部修改
        self._config_mtime: Optional[float] = config_file_mtime(self.config_path)
        self._build_tabs()
        self._build_menu_bar()
        self._build_status_bar()

        # ---- 日志桥 + 文件日志 ----
        self._bridge = LogBridge(self)
        self._bridge.message.connect(self._on_log_message)
        self._qt_log_handler = QtLogHandler(self._bridge, level=logging.INFO)
        logging.getLogger("xianyu_alert").addHandler(self._qt_log_handler)
        self._install_file_logging()

        # ---- 状态刷新定时器（1000ms，挂机时仅此一个定时器） ----
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        self._load_history()
        self.tab_run.append_log("INFO", f"[{datetime.now():%H:%M:%S}] 图形界面已启动（Qt / PySide6）。")
        self.tab_run.append_log("INFO", f"[{datetime.now():%H:%M:%S}] 配置文件：{os.path.abspath(self.config_path)}")

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _load_form(self) -> None:
        """读取配置 → 表单状态。"""
        self._raw_config, self._form = load_form(self.config_path)
        self._storage_path = str(self._form.get("storage_path") or DEFAULT_DB_PATH)

    def _build_tabs(self) -> None:
        self.tabs = QTabWidget(self)
        self.tab_config = MonitorConfigTab(self._form)
        self.tab_notify = NotifyConfigTab(self._form)
        self.tab_run = RunMonitorTab()
        self.tabs.addTab(self.tab_config, "监控配置")
        self.tabs.addTab(self.tab_notify, "通知设置")
        self.tabs.addTab(self.tab_run, "运行监控")
        self.setCentralWidget(self.tabs)

        # 页签 → 主窗口信号接线
        self.tab_config.save_requested.connect(self._save_config)
        self.tab_config.cookie_changed.connect(self._refresh_cookie_light)
        self.tab_config.refresh_cookie_requested.connect(self.on_refresh_cookie)
        self.tab_notify.test_requested.connect(self._test_channel)
        self.tab_run.start_requested.connect(self.on_start)
        self.tab_run.stop_requested.connect(self.on_stop)
        self.tab_run.run_once_requested.connect(self.on_run_once)
        self.tab_run.clear_records_requested.connect(self._on_clear_records)
        self.tab_run.check_shelf_requested.connect(self._on_check_shelf)
        self.tab_run.blacklist_requested.connect(self._on_blacklist_row)
        self.tab_run.sold_toggle_requested.connect(self._on_toggle_sold)
        self.tab_run.open_url_requested.connect(self._open_alert_url)

    def _build_menu_bar(self) -> None:
        """原生菜单栏（macOS 自动置顶显示）。"""
        bar = self.menuBar()
        menu_file = bar.addMenu("文件")
        act_save = menu_file.addAction("保存配置")
        act_save.triggered.connect(self._save_config)
        act_exit = menu_file.addAction("退出")
        act_exit.triggered.connect(self.close)

        menu_help = bar.addMenu("帮助")
        act_about = menu_help.addAction("关于")
        act_about.triggered.connect(self._show_about)
        act_cookie = menu_help.addAction("如何获取 Cookie")
        act_cookie.triggered.connect(self._show_cookie_help)
        act_logs = menu_help.addAction("查看日志目录")
        act_logs.triggered.connect(self._open_log_dir)
        act_version = menu_help.addAction("版本")
        act_version.triggered.connect(self._show_version)

    def _build_status_bar(self) -> None:
        self.statusBar().showMessage("就绪")

    def _install_file_logging(self) -> None:
        """滚动文件日志（state/xianyu_alert.log），复用 cli.install_file_logging。"""
        try:
            from ..cli import install_file_logging

            install_file_logging()
        except Exception as exc:  # noqa: BLE001 - 文件日志失败不阻断 GUI
            logger.debug("文件日志安装失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 配置收集 / 保存
    # ------------------------------------------------------------------ #
    def _collect_config_dict(self) -> Dict[str, Any]:
        """收集三页签表单 → 配置字典（Cookie 一律 Fernet 加密）。

        Raises:
            ValueError: 未添加任何关键词 / 间隔 / 页数非法（与 Tk 版一致）。
        """
        form = dict(self._form)
        form.update(self.tab_config.collect_config())
        channels = self.tab_notify.collect_channels()
        # 与 Tk 版 _collect_config_dict 同构的关键校验（QSpinBox 已保证整数范围，
        # 此处主要兜底「至少一个关键词」与异常表单值）。
        from ..gui import validate_interval, validate_pages

        if not (form.get("keywords") or []):
            raise ValueError("请至少添加一个关键词。")
        validate_interval(form.get("interval", 600))
        validate_pages(form.get("pages", 1))
        return form_to_config_dict(form, base=self._raw_config, channels=channels)

    def _build_config_object(self) -> Config:
        """从界面收集配置并构造校验通过的 Config 对象。

        Raises:
            ValueError / ConfigError: 配置非法。
        """
        return config_from_dict(self._collect_config_dict())

    def _save_config(self) -> None:
        """保存配置到 config.yaml（两页签状态一并落盘）。"""
        try:
            data = self._collect_config_dict()
            config_from_dict(data)  # 保存前先校验，避免写出跑不起来的配置
        except (ValueError, ConfigError) as exc:
            QMessageBox.warning(self, "配置有误", str(exc))
            return
        try:
            save_raw_config(self.config_path, data)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", f"写入 {self.config_path} 失败：{exc}")
            return

        self._raw_config = data
        self._storage_path = str(data.get("storage", {}).get("path") or DEFAULT_DB_PATH)
        # v1.8（C22）：本进程保存后更新 mtime 快照，避免触发「外部修改」重载提示
        self._touch_config_mtime()
        enabled = [c["type"] for c in data.get("notify", {}).get("channels", [])]
        self.tab_run.append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] 配置已保存到 {self.config_path}，启用通知通道：{', '.join(enabled)}",
        )
        QMessageBox.information(
            self,
            "保存成功",
            f"配置已写入：\n{os.path.abspath(self.config_path)}\n\n启用的通知通道：{', '.join(enabled)}",
        )

        # mtop 且无任何 Cookie → warning（不阻断）
        ftype = str(data.get("fetcher", {}).get("type", ""))
        pool_has_enabled = any(
            item.get("enabled") and str(item.get("cookie") or "").strip()
            for item in (data.get("monitor", {}).get("cookie_pool") or [])
        )
        if ftype == "mtop" and not str(data.get("monitor", {}).get("cookies") or "") and not pool_has_enabled:
            self.tab_run.append_log(
                "WARNING",
                f"[{datetime.now():%H:%M:%S}] 已保存，但 mtop 未配置任何 Cookie（单值或 Cookie 池均为空），"
                "真实抓取将失败。请点击「Cookie 管理」查看手动获取步骤并补充登录态。",
            )
            QMessageBox.warning(
                self,
                "Cookie 未配置",
                "配置已保存，但当前选择的是 mtop 真实抓取，\n"
                "尚未配置任何登录 Cookie（单值或 Cookie 池均为空），\n"
                "开始监控后真实抓取将失败。\n\n"
                "请点击「Cookie 管理」→「如何获取 Cookie？」按手动步骤补充登录态。",
            )

    # ------------------------------------------------------------------ #
    # v1.8（C7/C17/C22）：一键刷新 Cookie + config 外部修改检测
    # ------------------------------------------------------------------ #
    def on_refresh_cookie(self) -> None:
        """「🔄 一键刷新 Cookie」：引导式刷新 → 校验 → 加密回写 → 内存态同步。

        打开 RefreshCookieDialog（三步）；校验通过后更新 tab_config 单值 Cookie，
        再走 `_save_config()` 统一加密落盘（Fernet）+ 状态灯即时变绿（C17）。
        """
        from .dialogs import RefreshCookieDialog

        dlg = RefreshCookieDialog(parent=self)
        if dlg.exec() != RefreshCookieDialog.Accepted:
            return
        new_cookie = dlg.cookie()
        self.tab_config.set_single_cookie(new_cookie)
        try:
            self._save_config()
        except Exception as exc:  # noqa: BLE001 - 保存失败给出提示
            QMessageBox.critical(self, "保存失败", f"刷新 Cookie 保存失败：{exc}")
            return
        self.tab_run.append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] ✅ Cookie 已更新并加密保存，下一轮将生效（脱敏："
            f"{self._mask(new_cookie)}）",
        )
        QMessageBox.information(self, "刷新成功", "Cookie 已更新并加密保存，下一轮将生效。")

    @staticmethod
    def _mask(cookie: str) -> str:
        """Cookie 脱敏回显（C19）。"""
        from .. import secure

        return secure.mask_cookie(cookie) or "（空）"

    def _touch_config_mtime(self) -> None:
        """本进程保存后更新 mtime 快照，避免「自己保存」触发重载提示。"""
        self._config_mtime = config_file_mtime(self.config_path)

    def _check_config_mtime(self) -> None:
        """比对磁盘 mtime 与快照；外部修改 → 弹「是否重载？」询问（C22）。"""
        current = config_file_mtime(self.config_path)
        if current is None or self._config_mtime is None:
            return
        if abs(current - self._config_mtime) > 0.001:
            proceed = QMessageBox.question(
                self,
                "配置文件已被外部修改",
                f"检测到配置文件已被外部修改：\n{os.path.abspath(self.config_path)}\n\n是否立即重载？",
            )
            if proceed == QMessageBox.Yes:
                try:
                    self._reload_config_from_disk()
                except Exception as exc:  # noqa: BLE001 - 重载失败只记日志
                    self.tab_run.append_log("ERROR", f"[{datetime.now():%H:%M:%S}] 重载配置失败：{exc}")
            self._touch_config_mtime()

    def _reload_config_from_disk(self) -> None:
        """从磁盘重载配置到内存态并刷新页签（C22 用户确认后调用）。"""
        self._load_form()
        self._storage_path = str(self._form.get("storage_path") or DEFAULT_DB_PATH)
        self.tab_config.reload_from_form(self._form)
        self.tab_notify.reload_from_form(self._form)
        self.tab_run.append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] 检测到配置文件被外部修改，已重载：{os.path.abspath(self.config_path)}",
        )
        self._touch_config_mtime()

    # ------------------------------------------------------------------ #
    # 菜单动作
    # ------------------------------------------------------------------ #
    def _show_about(self) -> None:
        QMessageBox.information(self, "关于", about_full_text())

    def _show_version(self) -> None:
        QMessageBox.information(self, "版本", f"闲鱼低价提醒工具 v{__version__}")

    def _show_cookie_help(self) -> None:
        from ..gui import COOKIE_MANUAL_HELP

        QMessageBox.information(self, "如何获取 Cookie？", COOKIE_MANUAL_HELP)

    def _open_log_dir(self) -> None:
        """打开日志目录（Finder / 资源管理器）。"""
        log_dir = paths.default_state_dir()
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            pass
        try:
            if sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", log_dir])
            elif sys.platform == "win32":
                os.startfile(log_dir)  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", log_dir])
        except Exception as exc:  # noqa: BLE001 - 打开失败提示路径
            QMessageBox.information(self, "日志目录", f"日志目录：\n{log_dir}\n（打开失败：{exc}）")

    # ------------------------------------------------------------------ #
    # 监控启停
    # ------------------------------------------------------------------ #
    def _worker_alive(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def on_start(self) -> None:
        """开始循环监控。"""
        if self._worker_alive():
            QMessageBox.information(self, "已在运行", "监控已经在运行中。")
            return
        self._launch_worker(single_round=False)

    def on_run_once(self) -> None:
        """立即执行一轮监测。"""
        if self._worker_alive():
            QMessageBox.information(self, "正在运行", "监控正在运行中，请先停止后再手动执行。")
            return
        self._launch_worker(single_round=True)

    def _launch_worker(self, single_round: bool) -> None:
        """校验配置并启动后台监控线程（QThread）。

        配置在**主线程**收集与校验（纯本地，毫秒级），错误立即弹框；
        「仅展示符合的低价」勾选在主线程读取为普通 bool 传给后台线程。
        """
        try:
            config = self._build_config_object()
        except (ValueError, ConfigError) as exc:
            QMessageBox.warning(self, "配置有误", str(exc))
            return

        detail_only = self.tab_run.detail_only()
        self._running = True
        self.tab_run.set_running(True)
        self._worker = MonitorWorker(config, single_round=single_round, detail_only=detail_only)
        self._worker.ui_message.connect(self._handle_ui_message)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_worker_finished(self) -> None:
        """监控线程结束（主线程槽函数）。"""
        if self._closing:
            return
        self._running = False
        self.tab_run.set_running(False)

    def on_stop(self) -> None:
        """请求停止监控。"""
        if not self._worker_alive():
            self._running = False
            self.tab_run.set_running(False)
            return
        self._worker.request_stop()
        self.tab_run.append_log("INFO", f"[{datetime.now():%H:%M:%S}] 已发送停止信号，等待当前轮结束…")
        self.statusBar().showMessage("状态：正在停止…")

    # ------------------------------------------------------------------ #
    # 消息分发（主线程槽函数：所有 UI 更新只发生在这里）
    # ------------------------------------------------------------------ #
    def _handle_ui_message(self, kind: str, payload: Any) -> None:
        """处理后台线程投递的 ui_message（kind ∈ log/alert/status/state/message）。"""
        try:
            if kind == "log":
                level, text = payload
                self.tab_run.append_log(level, text)
            elif kind == "alert":
                self.tab_run.append_alert(dict(payload or {}))
            elif kind == "status":
                self.tab_run.update_stats(
                    rounds=int((payload or {}).get("rounds", 0)),
                    alerts=int((payload or {}).get("alerts", 0)),
                )
            elif kind == "state":
                running = bool((payload or {}).get("running", False))
                if not running:
                    self._running = False
                    self.tab_run.set_running(False)
                    self.statusBar().showMessage("监控已停止")
            elif kind == "message":
                msg = payload or {}
                level = str(msg.get("level", "info"))
                title = str(msg.get("title", "提示"))
                text = str(msg.get("text", ""))
                if level == "error":
                    QMessageBox.critical(self, title, text)
                else:
                    QMessageBox.information(self, title, text)
        except Exception as exc:  # noqa: BLE001 - 消息分发异常绝不让主线程崩
            logger.debug("处理 UI 消息异常（%s）：%s", kind, exc)

    def _on_log_message(self, level: str, text: str) -> None:
        """QtLogHandler → LogBridge → LogView 渲染（主线程槽函数）。"""
        if not self._closing:
            self.tab_run.append_log(level, text)

    def _tick(self) -> None:
        """每秒状态刷新：倒计时 / 状态栏 / config 外部修改检测（C22）。"""
        if self._worker is not None and self._worker.isRunning():
            next_run = getattr(self._worker, "_next_run_at", 0.0)
            self.tab_run.set_next_run_at(next_run)
        self.tab_run.refresh_status(time.monotonic())
        try:
            self._check_config_mtime()
        except Exception as exc:  # noqa: BLE001 - mtime 检测失败不影响主流程
            logger.debug("config mtime 检测异常：%s", exc)

    # ------------------------------------------------------------------ #
    # 通知通道测试
    # ------------------------------------------------------------------ #
    def _test_channel(self, ctype: str) -> None:
        """测试某个通知通道（后台线程发送，不卡 UI）。"""
        options = normalize_channel_options(ctype, self.tab_notify.channel_options(ctype))
        if not channel_is_complete(ctype, options):
            QMessageBox.warning(
                self,
                "参数不完整",
                f"通道「{CHANNEL_LABELS.get(ctype, ctype)}」缺少必填参数，请先填写完整。",
            )
            return
        from ..config import NotifyChannel

        notifier = build_notifier(NotifyChannel(type=ctype, options=options))
        if notifier is None:
            QMessageBox.critical(self, "构造失败", f"无法构造通道 {ctype}，请检查参数。")
            return

        self.tab_run.append_log("INFO", f"[{datetime.now():%H:%M:%S}] 正在测试通道 {ctype}…")
        worker = TestChannelWorker(notifier, make_sample_product(), ctype)
        self._test_workers.append(worker)  # 持有引用防 GC
        worker.done.connect(self._on_test_done)
        worker.finished.connect(lambda: self._test_workers.remove(worker))
        worker.start()

    def _on_test_done(self, success: bool, text: str) -> None:
        """测试发送结果（主线程槽函数）。"""
        if success:
            QMessageBox.information(self, "测试成功", text)
            self.tab_run.append_log("INFO", f"[{datetime.now():%H:%M:%S}] 测试发送成功")
        else:
            QMessageBox.critical(self, "测试失败", text)
            self.tab_run.append_log("ERROR", f"[{datetime.now():%H:%M:%S}] 测试发送失败：{text}")

    # ------------------------------------------------------------------ #
    # 提醒记录操作（复用 Storage / fetcher 纯逻辑，控件只在主线程）
    # ------------------------------------------------------------------ #
    def _open_alert_url(self, row: Dict[str, Any]) -> None:
        """双击 / 右键打开商品链接。"""
        url = str(row.get("url", "") or "")
        if not url:
            QMessageBox.information(self, "无链接", "该记录没有可打开的商品链接。")
            return
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "打开失败", f"无法打开链接：{exc}")

    def _toggle_sold(self, row: Dict[str, Any]) -> None:
        """标记售出 / 恢复在架（本地 SQLite，主线程执行）。"""
        product_id = str(row.get("product_id", "") or "")
        if not product_id:
            QMessageBox.warning(self, "缺少商品 ID", "该记录缺少商品 ID，无法标记售出。")
            return
        already_sold = bool(row.get("sold", False))
        try:
            storage = Storage(self._storage_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "操作失败", f"无法打开数据库：{exc}")
            return
        try:
            if already_sold:
                storage.unmark_sold_out(product_id)
                self.tab_run.append_log(
                    "INFO",
                    f"[{datetime.now():%H:%M:%S}] 已把商品「{row.get('title', '')}」（{product_id}）恢复为在架",
                )
            else:
                storage.mark_sold_out_by_id(product_id, reason="人工标记")
                self.tab_run.append_log(
                    "INFO",
                    f"[{datetime.now():%H:%M:%S}] 已把商品「{row.get('title', '')}」（{product_id}）标记为已售出/下架",
                )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "操作失败", f"标记售出失败：{exc}")
            return
        finally:
            storage.close()
        self._reload_alerts()

    def _on_toggle_sold(self, row: Dict[str, Any]) -> None:
        self._toggle_sold(row)

    def _on_blacklist_row(self, row: Dict[str, Any]) -> None:
        """把提醒记录加入黑名单（弹原因输入框）。"""
        from .dialogs import BlacklistDialog

        dlg = BlacklistDialog(parent=self)
        if dlg.exec() != BlacklistDialog.Accepted:
            return
        reason = dlg.reason()
        try:
            storage = Storage(self._storage_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "操作失败", f"无法打开数据库：{exc}")
            return
        try:
            ok = blacklist_alert_row(storage, row, reason=reason)
        except ValueError as exc:
            QMessageBox.warning(self, "加入失败", str(exc))
            return
        finally:
            storage.close()
        if not ok:
            QMessageBox.warning(self, "缺少商品 ID", "该记录缺少商品 ID，无法加入黑名单。")
            return
        self.tab_run.append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] 已把商品「{row.get('title', '')}」加入黑名单"
            + (f"（{reason}）" if reason else ""),
        )
        self._reload_alerts()

    def _on_check_shelf(self, rows: List[Dict[str, Any]]) -> None:
        """批量校验在架状态（SoldCheckWorker 后台执行）。"""
        if self._worker_alive():
            QMessageBox.information(self, "正在运行", "监控正在运行中，请先停止后再校验在架状态。")
            return
        items: List[Dict[str, str]] = []
        for row in rows or []:
            pid = str(row.get("product_id", "") or "")
            if not pid:
                continue
            items.append(
                {
                    "product_id": pid,
                    "keyword": str(row.get("keyword", "") or ""),
                    "title": str(row.get("title", "") or ""),
                }
            )
        if not items:
            QMessageBox.information(self, "没有可校验的商品", "提醒记录为空，没有可校验在架状态的商品。")
            return
        try:
            config = self._build_config_object()
        except (ValueError, ConfigError) as exc:
            QMessageBox.warning(self, "配置有误", str(exc))
            return
        if config.fetcher.type != "mtop":
            QMessageBox.information(
                self,
                "校验不可用",
                "「校验在架」需要 mtop 真实抓取（调用闲鱼商品详情接口）。\n"
                f"当前抓取方式是 {config.fetcher.type}，无法校验，请改用 mtop 并配置 Cookie。",
            )
            return
        self.tab_run.append_log(
            "INFO",
            f"[{datetime.now():%H:%M:%S}] 开始校验 {len(items)} 个商品的在架状态（每次间隔 1.5s 限速）…",
        )
        self._sold_worker = SoldCheckWorker(config, items)
        self._sold_worker.ui_message.connect(self._handle_ui_message)
        self._sold_worker.finished_reload.connect(self._reload_alerts)
        self._sold_worker.start()

    def _on_clear_records(self) -> None:
        """清空去重记录。"""
        if self._worker_alive():
            QMessageBox.information(self, "正在运行", "请先停止监控再清空记录。")
            return
        proceed = QMessageBox.question(
            self,
            "确认清空",
            "确定要清空全部去重记录吗？\n\n"
            "清空后，之前提醒过的商品会被重新视为「新商品」，\n"
            "下一轮监测可能会重复提醒。此操作不可撤销。",
        )
        if proceed != QMessageBox.Yes:
            return
        try:
            storage = Storage(self._storage_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "清空失败", f"无法打开数据库：{exc}")
            return
        try:
            deleted = storage.clear_all()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "清空失败", f"清空记录时出错：{exc}")
            return
        finally:
            storage.close()
        self.tab_run.table_alerts.clear_rows()
        self.tab_run.update_stats(0, 0)
        self.tab_run.append_log("INFO", f"[{datetime.now():%H:%M:%S}] 已清空去重记录，共删除 {deleted} 条。")
        QMessageBox.information(self, "已清空", f"已删除 {deleted} 条商品记录。")

    # ------------------------------------------------------------------ #
    def _load_history(self) -> None:
        """启动时加载历史提醒记录（HISTORY_LIMIT 条）。"""
        try:
            storage = Storage(self._storage_path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("打开数据库失败：%s", exc)
            return
        try:
            rows = storage.list_notified(limit=HISTORY_LIMIT)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取历史记录失败：%s", exc)
            return
        finally:
            storage.close()
        formatted = []
        for row in rows or []:
            formatted.append(
                {
                    "time": str(row.get("last_seen", "") or ""),
                    "keyword": str(row.get("keyword", "") or ""),
                    "title": str(row.get("title", "") or ""),
                    "price": str(row.get("price", "") or ""),
                    "publish": str(row.get("publish_time", "") or ""),
                    "url": str(row.get("url", "") or ""),
                    "product_id": str(row.get("product_id", "") or ""),
                    "sold": bool(row.get("sold_out", False)),
                }
            )
        self.tab_run.reload_alerts(formatted)
        self.tab_run.update_stats(0, len(formatted))

    def _reload_alerts(self) -> None:
        """从数据库重载提醒记录（标记售出 / 校验在架 / 黑名单后调用）。"""
        self._load_history()

    def _refresh_cookie_light(self) -> None:
        """Cookie 变更后刷新状态灯（tab_config 内部已刷新，此处同步表单）。"""
        pass  # 状态灯在 tab_config 内自维护

    # ------------------------------------------------------------------ #
    # 关闭流程
    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        """关闭窗口：优雅停止后台线程 + 保存配置。"""
        if self._worker_alive():
            proceed = QMessageBox.question(
                self, "确认退出", "监控正在运行，确定要退出吗？"
            )
            if proceed != QMessageBox.Yes:
                event.ignore()
                return
            self._worker.request_stop()
            if not self._worker.wait(CLOSE_JOIN_TIMEOUT * 1000):
                logger.debug("监控线程未在 %ss 内退出，继续关闭", CLOSE_JOIN_TIMEOUT)
        if self._sold_worker is not None and self._sold_worker.isRunning():
            self._sold_worker.request_stop()
            self._sold_worker.wait(CLOSE_JOIN_TIMEOUT * 1000)

        self._closing = True
        self._tick_timer.stop()
        try:
            logging.getLogger("xianyu_alert").removeHandler(self._qt_log_handler)
        except Exception:  # noqa: BLE001
            pass
        # 保存配置（Fernet 加密；尽力而为，不阻塞关闭）
        try:
            data = self._collect_config_dict()
            config_from_dict(data)
            save_raw_config(self.config_path, data)
        except Exception as exc:  # noqa: BLE001
            logger.debug("关闭时保存配置失败：%s", exc)
        event.accept()
