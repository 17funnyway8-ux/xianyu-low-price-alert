"""FastAPI 应用：REST API + SSE 日志流 + 静态单页（P2 全功能 + P3 认证）。

路由（对齐 docs/v1.8_P2P3_增量设计_Web全功能与打磨.md §2 路由表）：
    - GET  /                              → index.html（SPA，免认证）
    - GET  /healthz                       → 200 + {status, monitor_running, ...}（免认证）
    - GET  /static/*                      → 静态资源（免认证）
    - （以下全部挂 api_router，prefix=/api，受 require_auth 保护）
    - GET/PUT  /api/config                → 表单（Cookie 脱敏）/ 保存 → 校验 → 写盘 → 热重启
    - POST /api/monitor/start | stop | run_once | detail_only
    - GET  /api/monitor/status
    - POST /api/cookie/save               → 复用 save_cookies_validated_encrypted
    - GET/POST /api/cookie/pool           → Cookie 池（P2-01）
    - POST /api/notify/test
    - GET  /api/records                   → list_notified（include_sold / 排序）
    - POST /api/records/check_shelf | check_shelf/status | check_shelf/cancel | clear（P2）
    - POST /api/records/{id}/sold | unmark | blacklist
    - GET  /api/blacklist                 → 黑名单列表（P2-02）
    - POST /api/blacklist/{product_id}/restore
    - GET  /api/logs/stream               → SSE 实时日志（受认证保护，前端 fetch 流式订阅）

安全约定（硬性约束）：
    - 任何 API 响应 / 日志 / 错误信息一律 mask_cookie 脱敏，永不出明文 Cookie；
    - Cookie 保存 / 池落盘只走加密路径（fernet1:），磁盘无明文持久化窗口；
    - 统一 JSON 信封：成功 {"ok": true, ...}；失败 {"ok": false, "message": ...}；
    - 认证（P3-01）：`XY_WEB_TOKEN` 非空时除 /healthz、/、/static/* 外全部 401，
      `secrets.compare_digest` 常数时间比较；未设 token 时行为与 P1 完全一致。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from xianyu_alert import __version__, gui, paths, secure
from xianyu_alert.config import ConfigError, NotifyChannel
from xianyu_alert.cookie import save_cookies_validated_encrypted
from xianyu_alert.notifier import build_notifier

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


# ---------------------------------------------------------------------- #
# 认证依赖（P3-01）：XY_WEB_TOKEN 非空时 /api/* 全量校验
# ---------------------------------------------------------------------- #
def require_auth(request: Request) -> None:
    """Bearer token 认证依赖（挂载于 api_router，保护全部 /api/* 路由）。

    - `XY_WEB_TOKEN` 未设置（默认本机 127.0.0.1 场景）→ 直接放行（与 P1 行为一致）；
    - 设置后：未携带 / 错误 Authorization 头 → 401（`secrets.compare_digest` 常数时间）；
    - `/healthz`、`/`、`/static/*` 不经过本依赖（挂在 app 上），天然豁免（R3）。
    """
    token = os.environ.get("XY_WEB_TOKEN", "").strip()
    if not token:
        return
    header = request.headers.get("Authorization", "")
    provided = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
    if not provided or not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="未认证或 token 错误")


app = FastAPI(title="闲鱼低价提醒工具 Web", version="1.8.0")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """统一失败信封：把 FastAPI 抛出的 HTTPException（401/404/409…）转成 {ok:false, message}。"""
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "message": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """统一失败信封（R5）：把 FastAPI 的 422 请求体校验错误转成 {ok:false, message}。

    取首个错误字段路径 + 英文 msg 拼成中文提示，保持 422 状态码。
    例：body 缺 product_ids → {"ok": false, "message": "参数校验失败: product_ids Field required"}。
    """
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body",))
    msg = str(first.get("msg", "参数校验失败"))
    detail = f"参数校验失败: {loc} {msg}".strip() if loc else f"参数校验失败: {msg}"
    return JSONResponse(status_code=422, content={"ok": False, "message": detail})


#: 受认证保护的 API 路由器（prefix=/api；/healthz、/、/static/* 仍挂 app 豁免认证）
api_router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


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


class CookiePoolActionBody(BaseModel):
    """Cookie 池操作请求体（P2-01，action 驱动）。"""

    action: str = Field(..., description="add/update/delete/toggle/set_default/refresh_selected/auto_disable_expired")
    name: Optional[str] = Field(None, description="目标条目名称（add 必填；其余按 name 定位）")
    new_name: Optional[str] = Field(None, description="update 改名后的新名称")
    cookie: Optional[str] = Field(None, description="add/update/refresh_selected 的明文 Cookie")
    force_missing_token: bool = Field(False, description="add 缺 _m_h5_tk 时前端二次确认后置 true")


class CheckShelfBody(BaseModel):
    """校验在架请求体（P2-03，product_ids 上限 30 由服务层截断）。"""

    product_ids: List[str] = Field(..., description="待校验商品 ID 列表（≤30）")


class MonitorDetailOnlyBody(BaseModel):
    """明细日志开关请求体（P2-11）。"""

    enabled: bool = Field(..., description="true=仅展示命中（不打印抓取明细）")


# ---------------------------------------------------------------------- #
# 页面与健康检查（免认证）
# ---------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """返回单页应用入口。"""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz")
def healthz(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """健康检查：Web 存活 + monitor 线程状态 + 最近轮次时间。

    P2-16 关于弹窗需要：`version`（xianyu_alert.__version__）与
    `data_dir`（paths.data_dir()，容器内即 XY_DATA_DIR=/app/data）。
    """
    st = service.status()
    return {
        "status": "ok",
        "version": str(__version__),
        "data_dir": paths.data_dir(),
        "monitor_running": st["running"],
        "last_round_at": st["last_round_at"],
        "round_count": st["round_count"],
        "notified_count": st["notified_count"],
    }


# ---------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------- #
@api_router.get("/config")
def api_get_config(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """返回配置表单（Cookie 一律脱敏）。"""
    data = gui.load_raw_config(service.config_path)
    form = web_form_from_config(data)
    return ok(form)


@api_router.put("/config")
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
@api_router.post("/monitor/start")
def api_monitor_start(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """启动 monitor 后台线程。"""
    result = service.start()
    if not result.get("ok"):
        return fail(result.get("message", "启动失败"), status_code=409)
    return ok(message=result.get("message", "监测已启动"))


@api_router.post("/monitor/stop")
def api_monitor_stop(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """停止 monitor 后台线程。"""
    result = service.stop()
    return ok(message=result.get("message", "监测已停止"))


@api_router.post("/monitor/run_once")
def api_monitor_run_once(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """立即执行一轮监测（monitor 运行时返回 409）。"""
    result = service.run_once()
    if not result.get("ok"):
        return fail(result.get("message", "执行失败"), status_code=409)
    return ok({"notified": result.get("notified", 0)}, message=result.get("message", "单轮执行完成"))


@api_router.get("/monitor/status")
def api_monitor_status(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """返回运行状态（前端状态条轮询 2s；含 P2 新增 detail_only 字段）。"""
    return ok(service.status())


@api_router.post("/monitor/detail_only")
def api_monitor_detail_only(
    body: MonitorDetailOnlyBody, service: MonitorService = Depends(get_service)
) -> Dict[str, Any]:
    """P2-11：设置明细日志开关（true=仅展示命中）。"""
    service.set_detail_only(body.enabled)
    return ok({"detail_only": service._detail_only}, message="明细日志已开启" if body.enabled else "明细日志已关闭")


# ---------------------------------------------------------------------- #
# Cookie
# ---------------------------------------------------------------------- #
@api_router.post("/cookie/save")
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


@api_router.get("/cookie/pool")
def api_cookie_pool(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """P2-01：返回 Cookie 池脱敏列表（name/enabled/health/expire/masked，绝不出明文）。"""
    try:
        result = service.cookie_pool_list()
    except Exception as exc:  # noqa: BLE001
        return fail(f"读取 Cookie 池失败：{exc}", status_code=500)
    return ok(result)


@api_router.post("/cookie/pool")
def api_cookie_pool_action(
    body: CookiePoolActionBody, service: MonitorService = Depends(get_service)
) -> Dict[str, Any]:
    """P2-01：Cookie 池操作（add/update/delete/toggle/set_default/refresh_selected/auto_disable_expired）。"""
    try:
        result = service.cookie_pool_action(
            action=body.action,
            name=body.name,
            new_name=body.new_name,
            cookie=body.cookie,
            force_missing_token=body.force_missing_token,
        )
    except ValueError as exc:
        return fail(str(exc), status_code=400)
    except Exception as exc:  # noqa: BLE001 - 文件/YAML/加密异常统一转 500
        return fail(f"Cookie 池操作失败：{exc}", status_code=500)
    if not result.get("ok"):
        return fail(result.get("message", "操作失败"), status_code=result.get("code", 400))
    payload: Dict[str, Any] = {"message": result.get("message", "操作成功")}
    if result.get("pool") is not None:
        payload["pool"] = result["pool"]
    return ok(payload)


# ---------------------------------------------------------------------- #
# 通知测试
# ---------------------------------------------------------------------- #
@api_router.post("/notify/test")
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
@api_router.get("/records")
def api_list_records(
    include_sold: bool = Query(False, description="是否包含已售出/下架记录"),
    limit: int = Query(100, ge=1, le=500, description="最多返回条数"),
    sort: str = Query("time", description="排序列：time/keyword/title/price/publish"),
    order: str = Query("desc", description="asc 或 desc"),
    service: MonitorService = Depends(get_service),
) -> Dict[str, Any]:
    """列出提醒记录（默认排除已售出与黑名单；支持 include_sold 与列排序）。"""
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


@api_router.post("/records/check_shelf", status_code=202)
def api_check_shelf(
    body: CheckShelfBody, service: MonitorService = Depends(get_service)
) -> JSONResponse:
    """P2-03：校验在架（异步批处理，202 立即返回 + 前端轮询 status）。

    预检失败返回 400/409（信封同统一格式，状态码语义 R6）。
    """
    result = service.start_check_shelf(body.product_ids)
    if not result.get("ok"):
        return fail(result.get("message", "校验在架启动失败"), status_code=result.get("code", 400))
    return JSONResponse(
        status_code=202,
        content={"ok": True, "accepted": True, "count": result.get("count", 0)},
    )


@api_router.get("/records/check_shelf/status")
def api_check_shelf_status(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """P2-03：校验在架进度（前端轮询 2s）。"""
    return ok(service.check_shelf_status())


@api_router.post("/records/check_shelf/cancel")
def api_check_shelf_cancel(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """P2-03：请求中止校验在架批处理。"""
    result = service.cancel_check_shelf()
    return ok({"cancelled": result.get("cancelled", False)}, message=result.get("message", "已请求中止"))


@api_router.post("/records/clear")
def api_clear_records(service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """P2-05：清空去重记录（product + meta，保留 blacklist；monitor 运行时 409）。"""
    result = service.clear_records()
    if not result.get("ok"):
        return fail(result.get("message", "清空失败"), status_code=result.get("code", 500))
    return ok({"deleted": result.get("deleted", 0)}, message=result.get("message", "已清空"))


@api_router.post("/records/{product_id}/sold")
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


@api_router.post("/records/{product_id}/unmark")
def api_unmark_sold(product_id: str, service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """P2-04：把商品恢复为在架（撤销售出标记，幂等）。"""
    result = service.unmark_record(product_id)
    if not result.get("ok"):
        return fail(result.get("message", "恢复失败"), status_code=result.get("code", 400))
    return ok({"updated": result.get("updated", 0)}, message=result.get("message", "已恢复为在架"))


@api_router.post("/records/{product_id}/blacklist")
def api_blacklist(
    product_id: str,
    body: Optional[BlacklistBody] = None,
    service: MonitorService = Depends(get_service),
) -> Dict[str, Any]:
    """把某商品加入临时黑名单（不再提醒 / 不再进提醒记录；P2 前端支持填写原因）。"""
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
# 黑名单（P2-02）
# ---------------------------------------------------------------------- #
@api_router.get("/blacklist")
def api_blacklist_list(
    limit: int = Query(100, ge=1, le=1000, description="最多返回条数"),
    service: MonitorService = Depends(get_service),
) -> Dict[str, Any]:
    """P2-02：列出黑名单（product_id/keyword/reason/created_at，4 字段原样）。"""
    try:
        rows = service.storage.list_blacklist(limit=limit)
    except Exception as exc:  # noqa: BLE001
        return fail(f"读取黑名单失败：{exc}", status_code=500)
    items = [dict(row) for row in rows]
    return ok({"items": items, "total": len(items)})


@api_router.post("/blacklist/{product_id}/restore")
def api_blacklist_restore(product_id: str, service: MonitorService = Depends(get_service)) -> Dict[str, Any]:
    """P2-02：把商品移出黑名单（恢复提醒，幂等）。"""
    pid = str(product_id or "").strip()
    if not pid:
        return fail("product_id 不能为空", status_code=400)
    try:
        removed = service.storage.remove_blacklist(pid)
    except Exception as exc:  # noqa: BLE001
        return fail(f"恢复失败：{exc}", status_code=500)
    return ok({"removed": removed}, message=f"已把商品 {pid} 移出黑名单（恢复提醒）")


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


@api_router.get("/logs/stream")
async def api_logs_stream(request: Request, service: MonitorService = Depends(get_service)) -> StreamingResponse:
    """SSE 实时日志流：先回放环形缓冲，再订阅广播器。

    **受认证保护**（挂 api_router）：前端用 `connectStream()`（fetch 流式订阅，
    可携带 Authorization 头）；无日志时每 15s 发心跳注释行。
    """
    return StreamingResponse(sse_events(request, service), media_type="text/event-stream")


# ---------------------------------------------------------------------- #
# 挂载：受保护 API 路由 + 静态资源（静态资源免认证）
# ---------------------------------------------------------------------- #
app.include_router(api_router)

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
