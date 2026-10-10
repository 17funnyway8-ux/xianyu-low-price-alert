"""Cookie 池管理（CookiePoolMixin，v1.9.7 从 MonitorService 拆出）。

含健康诊断、池的增删改、密文读写与「免扫码」刷新。"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import time
from datetime import datetime
from typing import Any

import yaml

from xianyu_alert import gui, secure  # noqa: F401  # gui 防御性导入 tkinter，容器可 import
from xianyu_alert.config import (
    serialize_cookie_pool,
)
from xianyu_alert.cookie import (
    HEALTH_INVALID_ENCRYPT,
    HEALTH_MISSING,
    TOKEN_TTL_MS,
    cookie_has_token,
    cookie_is_usable,
    cookie_token_timestamp,
    detect_cookie_health,
)

from .constants import (
    TOKEN_PERSIST_MIN_INTERVAL,
)

logger = logging.getLogger(__name__)


class CookiePoolMixin:
    # 下列属性/方法由宿主 MonitorService（__init__ 或其它 mixin）提供：
    # mixin 与宿主共享同一实例状态，这里声明仅为类型可见性，不做初始化。
    _config: Any
    _fetcher: Any
    _keeper: Any
    _last_auth_at: Any
    _last_token_persist_at: Any
    _lock: Any
    config: Any
    config_path: Any
    reload_if_external_changed: Any
    def cookie_status(self) -> dict[str, Any]:
        """v1.9：当前 Cookie 的**分层诊断** + 保活状态（API / 界面共用）。"""
        from xianyu_alert.credential import diagnose_cookie

        config = self.config
        monitor_cfg = config.monitor
        diagnosis = diagnose_cookie(str(getattr(monitor_cfg, "cookies", "") or "")).to_dict()
        keeper = self._keeper.snapshot() if self._keeper is not None else {"running": False}
        return {
            "diagnosis": diagnosis,
            "keepalive": {
                "enabled": bool(getattr(monitor_cfg, "keepalive_enabled", True)),
                "interval_seconds": int(
                    getattr(monitor_cfg, "keepalive_interval_seconds", 1800) or 0
                ),
                "last_auth_at": self._last_auth_at,
                "thread": keeper,
            },
        }

    def cookie_pool_list(self) -> dict[str, Any]:
        """读磁盘 config 的 monitor.cookie_pool（解密为明文）→ 逐条 detect_cookie_health
        + mask_cookie 脱敏 + expire 时间 → 返回展示列表（**绝不出明文**，R4）。

        Returns:
            {
                "pool_used": bool,           # 池中是否有「启用+健康」条目（轮换语义）
                "pool": [{"name", "enabled", "health_state", "health_reason",
                          "expire_at", "masked"}, ...],
                "single": {"health_state", "health_reason", "masked"},
                "default_name": str,         # P3：当前默认账号名（单值命中池条目→条目名；
                                             #     单值独立存在→脱敏标识；未设置→空串）
                "default_is_pool": bool,     # default_name 是否为池条目名（true）或单值脱敏（false）
                "message": 可选提示（池空/无健康条目时回退单值）。
            }
        """
        items = self._read_pool_plaintext()
        pool: list[dict[str, Any]] = []
        for item in items:
            cookie = str(item.get("cookie") or "")
            raw_cipher = str(item.get("_raw_cipher") or "")
            if raw_cipher and not cookie:
                # 解密失败：如实显示「无法解密」，不要误报成「未配置」
                # （否则用户会以为条目是空的，而实际是密钥不对）。
                state, reason = (
                    HEALTH_INVALID_ENCRYPT,
                    "密文无法解密（密钥变更/丢失），请恢复 secret.key 或重新登录",
                )
            else:
                state, reason = detect_cookie_health(cookie)
            pool.append(
                {
                    "name": str(item.get("name") or ""),
                    "enabled": bool(item.get("enabled", True)),
                    "health_state": state,
                    "health_reason": reason,
                    "expire_at": self._cookie_expire_text(
                        cookie, enabled=bool(item.get("enabled", True))
                    ),
                    "masked": secure.mask_cookie(cookie) or "",
                }
            )
        with self._lock:
            single_raw = str(self._config.monitor.cookies or "") if self._config else ""
        single_state, single_reason = detect_cookie_health(single_raw)
        single = {
            "health_state": single_state,
            "health_reason": single_reason,
            "masked": secure.mask_cookie(single_raw) or "",
        }
        # P3：当前默认账号名——单值命中池条目 → 显示条目名（set_default 语义）；
        # 单值独立存在 → 显示其脱敏标识（min 变更，复用既有字段）；单值为空 → 未配置
        # （池轮换场景由前端结合 pool_used 提示）。
        default_name = ""
        default_is_pool = False
        if single_raw:
            matched = next(
                (it for it in items if str(it.get("cookie") or "") == single_raw), None
            )
            if matched is not None:
                default_name = str(matched.get("name") or "")
                default_is_pool = True
            else:
                default_name = secure.mask_cookie(single_raw) or ""
                default_is_pool = False
        # pool_used：池中是否有「启用 + 非空 + 健康」条目（resolve_cookie_for_round 语义）。
        # 注意 items 是 dict（非 CookiePoolItem），不能用 pool_enabled_cookies（属性访问），
        # 这里直接按 dict 键过滤，与 cookie.pool_usable_cookies 语义对齐。
        usable: list[str] = []
        for item in items:
            if not bool(item.get("enabled", True)):
                continue
            ck = str(item.get("cookie") or "")
            if not ck:
                continue
            try:
                state, _reason = detect_cookie_health(ck)
            except Exception:  # noqa: BLE001 - 检测异常按不可用处理
                continue
            if cookie_is_usable(ck):
                usable.append(ck)
        pool_used = bool(usable)
        result: dict[str, Any] = {
            "pool_used": pool_used,
            "pool": pool,
            "single": single,
            "default_name": default_name,
            "default_is_pool": default_is_pool,
        }
        if not pool_used:
            result["message"] = "池为空或无健康条目，将回退单值 Cookie"
        return result

    def cookie_pool_action(
        self,
        action: str,
        name: str | None = None,
        new_name: str | None = None,
        cookie: str | None = None,
        force_missing_token: bool = False,
    ) -> dict[str, Any]:
        """action 分发（add/update/delete/toggle/set_default/refresh_selected/auto_disable_expired）。

        所有写路径：内存明文操作 → `serialize_cookie_pool(items, encrypt=True)`
        （fernet1: 密文）→ 原子写盘 → `reload_if_external_changed()`（R1/R4）。

        Returns:
            成功：{"ok": True, "message": str, "pool": 展示列表?}；
            失败：{"ok": False, "message": str, "code": int}。
        """
        action = str(action or "").strip().lower()
        items = self._read_pool_plaintext()

        def _find_idx(target: str) -> int:
            for i, item in enumerate(items):
                if item.get("name") == target:
                    return i
            return -1

        def _dup_name(target: str, exclude_idx: int = -1) -> bool:
            for i, item in enumerate(items):
                if i == exclude_idx:
                    continue
                if item.get("name") == target:
                    return True
            return False

        def _persist() -> list[dict[str, Any]]:
            """加密写盘 + mtime 重载 + 返回刷新后的池展示列表（只取 pool 数组）。"""
            self._write_pool_encrypted(items)
            self.reload_if_external_changed()
            return self.cookie_pool_list()["pool"]

        if action == "add":
            nm = str(name or "").strip()
            ck = str(cookie or "").strip()
            if not nm:
                return {"ok": False, "message": "条目名称不能为空", "code": 400}
            if not ck:
                return {"ok": False, "message": "Cookie 内容不能为空", "code": 400}
            if _dup_name(nm):
                return {"ok": False, "message": f"已存在同名条目「{nm}」", "code": 400}
            if not cookie_has_token(ck) and not force_missing_token:
                return {
                    "ok": False,
                    "message": "缺少 _m_h5_tk，mtop 抓取很可能失败，仍要添加吗？",
                    "code": 400,
                }
            items.append({"name": nm, "cookie": ck, "enabled": True})
            pool = _persist()
            return {"ok": True, "message": f"已添加 Cookie 条目「{nm}」", "pool": pool}

        if action == "update":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            new_nm = str(new_name or "").strip()
            if new_nm:
                if _dup_name(new_nm, exclude_idx=idx):
                    return {"ok": False, "message": f"已存在同名条目「{new_nm}」", "code": 400}
                items[idx]["name"] = new_nm
            if cookie is not None:
                ck = str(cookie or "").strip()
                if not ck:
                    return {"ok": False, "message": "Cookie 内容不能为空", "code": 400}
                state, reason = detect_cookie_health(ck)
                # v1.9：只有"未配置/密文无法解密"才拒绝；令牌过期可自愈，不再拦截
                if state in (HEALTH_MISSING, HEALTH_INVALID_ENCRYPT):
                    return {
                        "ok": False,
                        "message": f"Cookie 无效（{state}）：{reason}，未保存任何改动",
                        "code": 400,
                    }
                items[idx]["cookie"] = ck
            pool = _persist()
            return {"ok": True, "message": f"已更新条目「{new_nm or nm}」", "pool": pool}

        if action == "delete":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            items.pop(idx)
            pool = _persist()
            return {"ok": True, "message": f"已删除条目「{nm}」", "pool": pool}

        if action == "toggle":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            items[idx]["enabled"] = not bool(items[idx].get("enabled", True))
            pool = _persist()
            now_state = "启用" if items[idx]["enabled"] else "停用"
            return {"ok": True, "message": f"已{now_state}条目「{nm}」", "pool": pool}

        if action == "set_default":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            ck = str(items[idx].get("cookie") or "")
            state, reason = detect_cookie_health(ck)
            # v1.9：设为默认不再要求"令牌未过期"——服务端会下发新令牌自愈
            if state in (HEALTH_MISSING, HEALTH_INVALID_ENCRYPT):
                return {
                    "ok": False,
                    "message": f"条目「{nm}」当前不可用（{state}）：{reason}",
                    "code": 400,
                }
            self._write_single_cookie_encrypted(ck)
            self.reload_if_external_changed()
            return {"ok": True, "message": f"已把「{nm}」设为默认 Cookie，下一轮生效"}

        if action == "refresh_selected":
            nm = str(name or "").strip()
            idx = _find_idx(nm)
            if idx < 0:
                return {"ok": False, "message": f"条目「{nm}」不存在", "code": 400}
            ck = str(cookie or "").strip()
            if not ck:
                return {"ok": False, "message": "请粘贴新的 Cookie 内容", "code": 400}
            state, reason = detect_cookie_health(ck)
            if state in (HEALTH_MISSING, HEALTH_INVALID_ENCRYPT):
                return {
                    "ok": False,
                    "message": f"Cookie 无效（{state}）：{reason}，未保存任何改动",
                    "code": 400,
                }
            items[idx]["cookie"] = ck
            pool = _persist()
            return {"ok": True, "message": f"已刷新条目「{nm}」", "pool": pool}

        if action == "auto_disable_expired":
            disabled = 0
            for item in items:
                if not bool(item.get("enabled", True)):
                    continue
                state, _reason = detect_cookie_health(str(item.get("cookie") or ""))
                if state in ("expired", "no_token", "missing", "invalid_encrypt"):
                    item["enabled"] = False
                    disabled += 1
            pool = _persist() if disabled else self.cookie_pool_list()["pool"]
            return {
                "ok": True,
                "message": f"已自动停用 {disabled} 个过期/无效条目（保留条目）",
                "pool": pool,
            }

        return {"ok": False, "message": f"未知操作：{action}", "code": 400}

    def _read_pool_plaintext(self) -> list[dict[str, Any]]:
        """读取磁盘 config 的 monitor.cookie_pool 并逐条解密为明文。

        Returns:
            [{"name": str, "cookie": str(明文), "enabled": bool, "_raw_cipher": 可选}, ...]。
            解密失败条目：cookie 置空，并额外带上 `_raw_cipher`（原始 fernet1: 密文）——
            写盘时由 `serialize_cookie_pool` 原样回写，保证「密钥变更 / secret.key
            未随数据迁移」不会导致该条目被删除（数据保真，v1.8.1 修正）。
        """
        data = gui.load_raw_config(self.config_path)
        monitor = data.get("monitor") if isinstance(data, dict) else None
        monitor = monitor if isinstance(monitor, dict) else {}
        items: list[dict[str, Any]] = []
        for entry in monitor.get("cookie_pool") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            raw = str(entry.get("cookie") or "").strip()
            if not name:
                continue
            cookie = raw
            decrypt_failed = False
            if secure.is_encrypted(raw):
                decrypted = secure.decrypt_text(raw)
                if not decrypted:
                    logger.warning(
                        "Cookie 池条目 %s 密文无法解密（保留原密文，不删除条目）", name
                    )
                    cookie = ""
                    decrypt_failed = True
                else:
                    cookie = decrypted
            try:
                enabled = bool(entry.get("enabled", True))
            except Exception:  # noqa: BLE001 - 脏数据容错
                enabled = True
            item: dict[str, Any] = {"name": name, "cookie": cookie, "enabled": enabled}
            if decrypt_failed and raw:
                item["_raw_cipher"] = raw
            items.append(item)
        return items

    def _write_pool_encrypted(self, items: list[dict[str, Any]]) -> None:
        """把明文池序列化为 fernet1: 密文并**原子写盘**（同目录临时文件 + os.replace）。

        磁盘上不存在明文持久化窗口（R4）；写盘后由调用方触发 reload。
        """
        data = gui.load_raw_config(self.config_path)
        if not isinstance(data, dict):
            data = {}
        monitor = data.get("monitor")
        if not isinstance(monitor, dict):
            monitor = {}
        serialized = serialize_cookie_pool(items, encrypt=True)
        monitor["cookie_pool"] = serialized
        data["monitor"] = monitor
        parent = os.path.dirname(os.path.abspath(self.config_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                yaml.safe_dump(
                    data, fp, allow_unicode=True, sort_keys=False, default_flow_style=False
                )
            os.replace(tmp_path, self.config_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        logger.info("Cookie 池已加密写盘（%d 条，fernet1: 密文）", len(serialized))

    def _write_single_cookie_encrypted(self, cookie: str) -> None:
        """把明文 Cookie 加密写入 monitor.cookies（fernet1:），原子写盘。

        Raises:
            ValueError: 加密不可用（Fernet 密钥缺失 / encrypt_text 降级返回明文）。
        """
        cipher = secure.encrypt_text(cookie)
        if not secure.is_encrypted(cipher):
            raise ValueError("Cookie 加密不可用（Fernet 密钥缺失或不可用），未保存任何改动")
        data = gui.load_raw_config(self.config_path)
        if not isinstance(data, dict):
            data = {}
        monitor = data.get("monitor")
        if not isinstance(monitor, dict):
            monitor = {}
        monitor["cookies"] = cipher
        monitor["cookies_encrypted"] = True
        data["monitor"] = monitor
        parent = os.path.dirname(os.path.abspath(self.config_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                yaml.safe_dump(
                    data, fp, allow_unicode=True, sort_keys=False, default_flow_style=False
                )
            os.replace(tmp_path, self.config_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        logger.info("已把默认 Cookie 加密写入 monitor.cookies（fernet1: 密文）")

    # ------------------------------------------------------------------ #
    # 服务端刷新的令牌：节流落盘
    # ------------------------------------------------------------------ #
    def _persist_refreshed_token(self, fetcher: Any = None) -> bool:
        """把 fetcher 内存中「服务端刷新的令牌」节流写回磁盘。

        背景（2026-09-24 实测）：mtop 令牌由服务端在响应里**滑动续期**，
        `fetcher._absorb_token` 只更新内存字典与 session jar —— 进程一重启就
        退回 `config.yaml` 里的旧令牌，表现为「刚才还能抓，重启后立刻过期」。

        这里在每轮结束后检查 fetcher 的脏标记并落盘：
          - 走与 Cookie 保存**同一条**原子加密写路径（绝不写明文）；
          - 写盘后 mtime 变化 → `reload_if_external_changed()` → 运行中的
            monitor 下一轮就用上新 Config；
          - 按 `TOKEN_PERSIST_MIN_INTERVAL` 节流，避免每轮都重建 Config。

        Args:
            fetcher: 指定要落盘的抓取器；None 时取当前常驻 monitor 的 fetcher
                （手动 `run_once` 用的是临时 fetcher，必须显式传入）。

        Returns:
            True 表示本次确实执行了写盘。
        """
        target = fetcher if fetcher is not None else self._fetcher
        if target is None:
            return False
        checker = getattr(target, "token_refreshed", None)
        if not callable(checker) or not checker():
            return False
        getter = getattr(target, "refreshed_cookie_string", None)
        cookie = ""
        if callable(getter):
            try:
                cookie = str(getter() or "").strip()
            except Exception:  # noqa: BLE001 - 取值失败按无内容处理
                cookie = ""
        if not cookie:
            return False
        # v1.11.8：**先同步内存**（磁盘写入仍按间隔节流）。
        # 此前节流期内连内存都不更新 → 保活探测每次都用旧令牌 → 每次多付一次
        # "令牌过期重算签名"的重试（线上日志：14:28/14:58 两次探测各 2 个请求）。
        self._sync_cookie_to_memory(cookie)
        now = time.monotonic()
        if now - self._last_token_persist_at < TOKEN_PERSIST_MIN_INTERVAL:
            return False
        try:
            self._write_single_cookie_encrypted(cookie)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响本轮抓取
            logger.warning("刷新后的令牌落盘失败（不影响本轮抓取）：%s", exc)
            return False
        self._last_token_persist_at = now
        clearer = getattr(target, "clear_token_refreshed", None)
        if callable(clearer):
            clearer()
        with contextlib.suppress(Exception): # 重载失败也无妨，下轮 mtime 检测会补上
            self.reload_if_external_changed()
        logger.info("已将服务端刷新的登录令牌写回配置（下次重启无需重新登录）")
        return True

    def _sync_cookie_to_memory(self, cookie: str) -> None:
        """把刷新后的 Cookie 同步进**内存** Config（v1.11.8）。

        为什么不能只靠落盘节流：节流是为了少写盘（重建 Config 有成本），但内存态
        必须立刻生效 —— 否则紧接着的保活探测会用旧令牌，每次白付一个"令牌过期重试"。

        Args:
            cookie: 刷新后的 Cookie 请求头字符串（明文）。
        """
        config = self._config
        if config is None or not cookie:
            return
        with contextlib.suppress(Exception):
            config.monitor.cookies = cookie

    def _cookie_expire_text(self, cookie: str, enabled: bool = True) -> str:
        """计算 Cookie 过期时间展示文本（未知 → 「未知」；停用/空 → 「—」）。"""
        raw = str(cookie or "").strip()
        if not enabled or not raw:
            return "—"
        ts = cookie_token_timestamp(raw)
        if ts is None:
            return "未知"
        expire_ms = int(ts) + TOKEN_TTL_MS
        return datetime.fromtimestamp(expire_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------ #
    # P2-02 / P2-04 / P2-05：黑名单 / 售出撤销 / 清空记录
    # ------------------------------------------------------------------ #
    def refresh_cookie_via_browser(self, timeout: float | None = None) -> dict[str, Any]:
        """用持久化浏览器 profile **免扫码**刷新 Cookie 并写盘。

        这是「日常续期」的一键入口：只要 profile 里的登录态还在，整个过程无需
        任何人工操作；登录态确实失效时，返回明确原因与补救步骤（需人工登录一次）。

        注意：本方法依赖可选的 Playwright。主镜像（python:3.13-alpine）**没有**
        安装它（装上会让镜像从 ~31MB 涨到 ~1.5GB），因此容器内调用会返回
        环境不支持的提示 —— 这是有意的「优雅降级」，不是缺陷。

        Args:
            timeout: 等待上限秒数；None 时用静默模式默认值。

        Returns:
            {"ok": bool, "message": str, "code": int?, "state": str?, "reason": str?}。
        """
        from xianyu_alert.cookie import (
            LoginTimeout,
            PlaywrightUnavailable,
            acquire_via_playwright,
            detect_cookie_health,
            profile_ready,
            save_cookies_validated_encrypted,
        )

        # 前置检查：没有 profile 就谈不上「静默」—— 快速失败并给出可操作指引，
        # 避免真的去启动浏览器（本机装了 Playwright 时那会白等满超时）。
        if not profile_ready():
            return {
                "ok": False,
                "message": (
                    "本机尚未建立浏览器登录 profile，无法免扫码刷新。"
                    "请先在能打开浏览器的机器上运行一次 `cli cookie refresh` 完成人工登录"
                    "（会生成 browser_profile 目录），再把它复制到本机数据目录；"
                    "或直接把 Cookie 粘贴到下方输入框。"
                ),
                "code": 409,
            }

        try:
            cookie = acquire_via_playwright(timeout=timeout, headless=True)
        except PlaywrightUnavailable as exc:
            return {
                "ok": False,
                "message": (
                    f"当前环境未安装 Playwright，无法自动刷新（{exc}）。"
                    "可改为：在能打开浏览器的机器上运行 `cli cookie refresh`，"
                    "或直接在下方粘贴 Cookie。"
                ),
                "code": 400,
            }
        except LoginTimeout as exc:
            return {"ok": False, "message": str(exc), "code": 409}
        except Exception as exc:  # noqa: BLE001 - 兜底，避免 500 栈泄露到前端
            return {"ok": False, "message": f"刷新失败：{exc}", "code": 500}

        try:
            save_cookies_validated_encrypted(self.config_path, cookie)
        except ValueError as exc:
            return {"ok": False, "message": str(exc), "code": 400}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"Cookie 写入失败：{exc}", "code": 500}

        with contextlib.suppress(Exception): # 重载失败不阻断成功回显
            self.reload_if_external_changed()

        state, reason = detect_cookie_health(cookie)
        logger.info("已免扫码刷新 Cookie（状态 %s）", state)
        return {
            "ok": True,
            "message": "Cookie 已免扫码刷新并加密保存，下一轮生效",
            "state": state,
            "reason": reason,
        }
