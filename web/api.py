"""FastAPI 应用：REST API + SSE 日志流 + 静态单页。

路由（对齐 docs/v1.8_Docker化增量研判与执行方案.md §6 P1）：
    - GET  /                     → index.html（SPA）
    - GET  /healthz              → 200 + {status, monitor_running, last_round_at}
    - GET  /api/config           → 表单（Cookie 脱敏）
    - PUT  /api/config           → 保存 → 校验 → 写盘 → 热重启
    - POST /api/monitor/start    /stop /run_once
    - GET  /api/monitor/status
    - POST /api/cookie/save      → 复用 save_cookies_validated_encrypted（校验 + 内存加密 + 单次原子写盘）
    - POST /api/notify/test      → 复用 build_notifier + make_sample_product
    - GET  /api/records          → list_notified（include_sold / 排序）
    - POST /api/records/{id}/sold | /blacklist
    - GET  /api/logs/stream      → SSE 实时日志
    - /static/*                  → StaticFiles

安全约定（硬性约束）：
    - 任何 API 响应 / 日志 / 错误信息一律 mask_cookie 脱敏，永不出明文 Cookie；
    - Cookie 保存只走 `save_cookies_validated_encrypted`（校验拒绝则 400 + 中文原因），
      「校验 → 内存加密 → 单次原子写盘」，磁盘上不存在明文持久化窗口；
    - 统一 JSON 信封：成功 {"ok": true, ...}；失败 {"ok": false, "message": ...}。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from xianyu_alert import gui, secure
from xianyu_alert.config import ConfigError, NotifyChannel
from xianyu_alert.cookie import save_cookies_validated_encrypted
from xianyu_alert.notifier import build_notifier
from xianyu_alert.storage import Storage

from .monitor_service import (
    MonitorService,
    get_service,
    web_form_from_config,
)

#: 静态资源目录（web/static/）
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
#: SSE 心跳间隔（秒）：无日志时发送注释行保活
SSE_HEARTBEAT_SECONDS = 15.0
#: SSE 队列上限（超出丢最旧，保护浏览器内存）
SSE_QUEUE_MAX = 500


def ok(data: Optional[Dict[str, Any]] = None, message: str = "") -> Dict[str, Any]:
    """统一成功信封。"""
    payload: Dict[str, Any] = {"ok": True}
    if message:
        payload["message"] = message
    if data:
        payload.update(data)
    return payload


def fail(message: str, status_code: int = 400) -> JSONResponse:
    """统一失败信封（含脱敏要求：调用方自行保证 message 不含明文 Cookie）。"""
    return JSONResponse(status_code=status_code, content={"ok": False, "message": message})


app = FastAPI(title="闲鱼低价提醒工具 Web", version="1.8.0")


# ---------------------------------------------------------------------- #
# 请求体模型
# ---------------------------------------------------------------------- #
class CookieSaveBody(BaseModel):
    """Cookie 粘贴保存请求体。"""

    cookie: str = Field(..., description="浏览器复制的 Cookie 请求头字符串")


class NotifyTestBody(BaseModel):
    """通知通道测试请求体。"""

    channel_type: str = Field(..., description="通道类型（console/serverchan/email/telegram/bark/webhook）")
    options: Dict[str, Any] = Field(default_factory=dict, description="通道参数字典")


class BlacklistBody(BaseModel):
    """加入黑名单请求体（reason 可选）。"""

    reason: str = Field(default="人工剔除", description="加入原因")


class ConfigBody(BaseModel):
    """配置保存请求体：透传前端表单（与 GET /api/config 同构）。"""

    form: Dict[str, Any] = Field(..., description="Web 表单（keywords/channels/…）")


# ---------------------------------------------------------------------- #
# 页面与健康检查
# ---------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """返回单页应用入口。"""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz")
def healthz(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """健康检查：Web 存活 + monitor 线程状态 + 最近轮次时间。"""
    st = service.status()
    return {
        "status": "ok",
        "monitor_running": st["running"],
        "last_round_at": st["last_round_at"],
        "round_count": st["round_count"],
        "notified_count": st["notified_count"],
    }


# ---------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------- #
@app.get("/api/config")
def api_get_config(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """返回配置表单（Cookie 一律脱敏）。"""
    data = gui.load_raw_config(service.config_path)
    form = web_form_from_config(data)
    return ok(form)


@app.put("/api/config")
def api_put_config(body: ConfigBody, service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """保存配置：校验 → 停止 → 写盘 → 重载 → 热重启（若原在运行）。"""
    try:
        result = service.apply_config(body.form)
    except ConfigError as exc:
        return fail(str(exc), status_code=400)
    except (OSError, ValueError) as exc:
        return fail(f"配置保存失败：{exc}", status_code=400)
    return ok({"restarted": result.get("restarted", False)}, message=result.get("message", "配置已保存并生效"))


# ---------------------------------------------------------------------- #
# monitor 控制
# ---------------------------------------------------------------------- #
@app.post("/api/monitor/start")
def api_monitor_start(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """启动 monitor 后台线程。"""
    result = service.start()
    if not result.get("ok"):
        return fail(result.get("message", "启动失败"), status_code=409)
    return ok(message=result.get("message", "监测已启动"))


@app.post("/api/monitor/stop")
def api_monitor_stop(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """停止 monitor 后台线程。"""
    result = service.stop()
    return ok(message=result.get("message", "监测已停止"))


@app.post("/api/monitor/run_once")
def api_monitor_run_once(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """立即执行一轮监测（monitor 运行时返回 409）。"""
    result = service.run_once()
    if not result.get("ok"):
        return fail(result.get("message", "执行失败"), status_code=409)
    return ok({"notified": result.get("notified", 0)}, message=result.get("message", "单轮执行完成"))


@app.get("/api/monitor/status")
def api_monitor_status(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """返回运行状态（前端状态条轮询 2s）。"""
    return ok(service.status())


# ---------------------------------------------------------------------- #
# Cookie
# ---------------------------------------------------------------------- #
@app.post("/api/cookie/save")
def api_cookie_save(body: CookieSaveBody, service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """粘贴保存 Cookie：`save_cookies_validated_encrypted` 一次完成
    「校验（拒绝则 400 中文原因）→ 内存 Fernet 加密 → 单次原子写盘」。

    与桌面 CLI 路径（save_cookies_validated → ensure_cookie_encrypted）不同，
    本路径**磁盘上不存在明文持久化窗口**（设计 §4.2 / 共享知识 7）：校验非 ok
    不落盘；加密不可用 / 失败时 config.yaml 保持原样，绝不写明文。
    保存成功后触发 mtime 重载，让运行中的 monitor 下一轮换用新 Cookie。
    """
    cookie = str(body.cookie or "").strip()
    try:
        save_cookies_validated_encrypted(service.config_path, cookie)
    except ValueError as exc:
        # 错误信息本身不含明文（校验/加密文案来自 detect_cookie_health / 本函数）
        return fail(str(exc), status_code=400)
    except Exception as exc:  # noqa: BLE001 - 文件/YAML 读写异常统一转 500
        return fail(f"Cookie 保存失败：{exc}", status_code=500)

    # 立即重载（mtime 变化），让运行中的 monitor 下一轮换用新 Cookie
    try:
        service.reload_if_external_changed()
    except Exception:  # noqa: BLE001 - 重载失败不阻断保存成功回显
        pass

    return ok(
        {
            "masked": secure.mask_cookie(cookie) or "",
            "status": "ok",
        },
        message="Cookie 已更新并加密保存，下一轮生效",
    )


# ---------------------------------------------------------------------- #
# 通知测试
# ---------------------------------------------------------------------- #
@app.post("/api/notify/test")
def api_notify_test(body: NotifyTestBody) -> Dict[str, Any]:
    """测试发送：复用 build_notifier + make_sample_product（不落盘）。"""
    ctype = str(body.channel_type or "").strip().lower()
    channel = NotifyChannel(type=ctype, options=dict(body.options or {}))
    notifier = build_notifier(channel)
    if notifier is None:
        return fail("通道参数不完整，无法测试发送（请先填写必填参数）", status_code=400)
    sample = gui.make_sample_product(keyword="测试关键词")
    try:
        notifier.safe_notify([sample])
    except Exception as exc:  # noqa: BLE001 - 测试发送失败给出可读原因
        return fail(f"测试发送失败：{exc}", status_code=500)
    return ok(message="测试消息已发送")


# ---------------------------------------------------------------------- #
# 提醒记录
# ---------------------------------------------------------------------- #
@app.get("/api/records")
def api_list_records(
    include_sold: bool = Query(False, description="是否包含已售出/下架记录"),
    limit: int = Query(100, ge=1, le=500, description="最多返回条数"),
    sort: str = Query("time", description="排序列：time/keyword/title/price/publish"),
    order: str = Query("desc", description="asc 或 desc"),
    service: MonitorService = Depends(get_service),
) -> Dict[str, Any]:
    """列出提醒记录（默认排除已售出；支持 include_sold 与列排序）。"""
    try:
        rows = service.storage.list_notified(limit=limit, include_sold=include_sold)
    except Exception as exc:  # noqa: BLE001 - 数据库异常转 500
        return fail(f"读取提醒记录失败：{exc}", status_code=500)
    records: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        # 对齐 gui.sort_alert_rows 的列名约定（time/publish）
        item["time"] = item.get("last_seen", "")
        item["publish"] = item.get("publish_time", "")
        records.append(item)
    try:
        records = gui.sort_alert_rows(records, sort, ascending=str(order).lower() == "asc")
    except Exception:  # noqa: BLE001 - 排序失败回退默认顺序
        pass
    return ok({"records": records, "total": len(records)})


@app.post("/api/records/{product_id}/sold")
def api_mark_sold(product_id: str, service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """把某商品全部记录标记为「已售出/下架」（全局，跨关键词）。"""
    pid = str(product_id or "").strip()
    if not pid:
        return fail("product_id 不能为空", status_code=400)
    try:
        count = service.storage.mark_sold_out_by_id(pid, reason=gui.SOLD_REASON_MANUAL)
    except Exception as exc:  # noqa: BLE001
        return fail(f"标记失败：{exc}", status_code=500)
    return ok({"updated": count}, message="已标记为已售出/下架")


@app.post("/api/records/{product_id}/blacklist")
def api_blacklist(
    product_id: str,
    body: Optional[BlacklistBody] = None,
    service: MonitorService = Depends(get_service),
) -> Dict[str, Any]:
    """把某商品加入临时黑名单（不再提醒 / 不再进提醒记录）。"""
    pid = str(product_id or "").strip()
    if not pid:
        return fail("product_id 不能为空", status_code=400)
    reason = (body.reason if body is not None else "人工剔除") or "人工剔除"
    try:
        keyword = ""
        cur = service.storage.conn.execute(
            "SELECT keyword FROM product WHERE product_id = ? LIMIT 1", (pid,)
        )
        row = cur.fetchone()
        if row is not None:
            keyword = str(row["keyword"] or "")
        service.storage.add_blacklist(pid, keyword=keyword, reason=reason)
    except Exception as exc:  # noqa: BLE001
        return fail(f"加入黑名单失败：{exc}", status_code=500)
    return ok(message="已加入黑名单（不再提醒）")


# ---------------------------------------------------------------------- #
# SSE 实时日志
# ---------------------------------------------------------------------- #
def _sse_format(entry: Dict[str, str]) -> str:
    """把一条日志条目格式化为 SSE 事件。"""
    return f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"


async def sse_events(request: Request, service: MonitorService):
    """SSE 事件生成器：先回放环形缓冲，再订阅广播器实时推送。

    独立成模块级函数便于单元测试直接驱动（starlette TestClient 对无限
    StreamingResponse 的流式读取在部分版本存在兼容问题，故测试走本函数 +
    真实 Docker 冒烟验证双通道）。

    Args:
        request: 当前请求（用于检测客户端断开）。
        service: MonitorService 实例。

    Yields:
        SSE 文本块（`data: {...}\n\n` 或 `: connected` / `: keep-alive` 注释行）。
    """
    loop = asyncio.get_running_loop()
    sub_id, queue = service.broadcaster.subscribe(loop)
    try:
        # 连接建立即回放最近日志（避免刷新页面后白屏）
        yield ": connected\n\n"
        for entry in service.recent_logs(limit=200):
            yield _sse_format(entry)
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                yield _sse_format(data)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
    finally:
        service.broadcaster.unsubscribe(sub_id)


@app.get("/api/logs/stream")
async def api_logs_stream(request: Request, service: MonitorService = Depends(get_service)) -> StreamingResponse:
    """SSE 实时日志流：先回放环形缓冲，再订阅广播器。

    前端用 `EventSource('/api/logs/stream')` 订阅；无日志时每 15s 发心跳注释行。
    """
    return StreamingResponse(sse_events(request, service), media_type="text/event-stream")


# ---------------------------------------------------------------------- #
# 静态资源（挂载在 /static）
# ---------------------------------------------------------------------- #
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
