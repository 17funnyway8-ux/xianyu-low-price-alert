"""闲鱼 Cookie 分层凭据模型与诊断（v1.9 新增）。

背景：闲鱼的"登录"不是单个 Cookie，而是**四层寿命差异极大的凭据**。
把它们混在一起判"有效/过期"，就会出现"令牌过期 → 误判登录失效 → 要求重新扫码"
以及"剩余时间显示成 4 小时"这类问题。本模块把分层语义固化下来：

    [1] 登录态（session）   cookie2 / unb / sgcookie / _tb_token_ / tracknick ...
        决定"你是谁"；被服务端吊销才需要重新登录，本地无法判断过期。
    [2] 会话凭据（havana）  havana_lgc2_77 / _hvn_lgc_ / havana_lgc_exp
        客户端登录凭据，**带显式过期时间**（实测约 30 天），可免扫码刷新。
    [3] 签名令牌（token）   _m_h5_tk / _m_h5_tk_enc
        mtop 请求签名用；值内嵌的时间戳是**过期时刻**（不是签发时刻！），
        每次请求由服务端滑动续期，短命但可自愈。
    [4] 风控指纹（risk）    tfstk / x5sec / isg / cna / sdkSilent ...
        平台信任凭证；只做存在性展示，不参与"可用性"判定。

**核心结论（可用性规则）**：只要登录态字段在场，这份 Cookie 就**值得发起请求** ——
令牌过期会被服务端下发新令牌、并由 fetcher 自动重试续期；只有当登录态缺失、
密文无法解密、或会话凭据已过期时，才判为不可用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------- #
# 字段分层定义
# ---------------------------------------------------------------------- #
#: 登录态关键字段（判定"登录态是否在场"的依据）
SESSION_ESSENTIAL_FIELDS: tuple[str, ...] = ("cookie2",)
#: 登录态辅助字段（用于判断登录态的"强度"）
SESSION_SUPPORT_FIELDS: tuple[str, ...] = (
    "unb", "sgcookie", "_tb_token_", "tracknick", "csg", "t", "_samesite_flag_",
)
#: 会话凭据字段（Havana 客户端登录凭据）
HAVANA_FIELDS: tuple[str, ...] = ("havana_lgc2_77", "_hvn_lgc_", "havana_lgc_exp")
#: 会话凭据显式过期字段
HAVANA_EXP_FIELD = "havana_lgc_exp"
#: mtop 签名令牌字段
TOKEN_FIELD = "_m_h5_tk"
TOKEN_ENC_FIELD = "_m_h5_tk_enc"
#: 风控指纹字段（仅展示）
RISK_FIELDS: tuple[str, ...] = (
    "tfstk", "x5sec", "x5secdata", "isg", "cna", "sdkSilent", "mtop_partitioned_detect",
)

#: 续期窗口下限（毫秒）：令牌剩余不足这么久就提示"即将过期"
TOKEN_RENEWAL_WINDOW_MIN_MS = 15 * 60 * 1000
#: 未知实测 TTL 时的兜底续期窗口
TOKEN_RENEWAL_WINDOW_FALLBACK_MS = 30 * 60 * 1000

# ---------------------------------------------------------------------- #
# 顶层状态码
# ---------------------------------------------------------------------- #
STATE_OK = "ok"
STATE_TOKEN_EXPIRING = "token_expiring"
STATE_TOKEN_EXPIRED = "token_expired"
STATE_NO_TOKEN = "no_token"
STATE_SESSION_MISSING = "session_missing"
STATE_HAVANA_EXPIRED = "havana_expired"
STATE_MISSING = "missing"
STATE_INVALID_ENCRYPT = "invalid_encrypt"

#: 状态 -> 严重级别（前端配色 / 提醒去抖都用它）
STATE_SEVERITY: dict[str, str] = {
    STATE_OK: "ok",
    STATE_TOKEN_EXPIRING: "warn",
    STATE_TOKEN_EXPIRED: "warn",
    STATE_NO_TOKEN: "warn",
    STATE_HAVANA_EXPIRED: "warn",
    STATE_SESSION_MISSING: "error",
    STATE_MISSING: "error",
    STATE_INVALID_ENCRYPT: "error",
}

#: 状态 -> 中文短标签
STATE_LABEL: dict[str, str] = {
    STATE_OK: "有效",
    STATE_TOKEN_EXPIRING: "令牌即将过期",
    STATE_TOKEN_EXPIRED: "令牌已过期（可自愈）",
    STATE_NO_TOKEN: "缺签名令牌（首轮自动申领）",
    STATE_HAVANA_EXPIRED: "会话凭据已过期",
    STATE_SESSION_MISSING: "登录态缺失",
    STATE_MISSING: "未配置",
    STATE_INVALID_ENCRYPT: "密文无法解密",
}

#: 可用性规则：这些状态虽然"有警告"，但仍然值得发起请求
USABLE_STATES: tuple[str, ...] = (
    STATE_OK, STATE_TOKEN_EXPIRING, STATE_TOKEN_EXPIRED, STATE_NO_TOKEN,
)


def parse_cookie_fields(cookie_str: str) -> dict[str, str]:
    """把 Cookie 请求头字符串解析为字段字典（同名取最后一个）。

    Args:
        cookie_str: 形如 a=1; b=2 的 Cookie 头。

    Returns:
        字段名 -> 值；空串与非法片段自动跳过。
    """
    fields: dict[str, str] = {}
    for segment in str(cookie_str or "").split(";"):
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        key = key.strip()
        if key:
            fields[key] = value.strip()
    return fields


def token_expires_at_ms(cookie_str: str) -> int | None:
    """解析 _m_h5_tk 内嵌时间戳 —— 语义是**过期时刻**（毫秒）。

    2026-10-09 实测：容器时间 18:29 时该时间戳为 20:57（未来 2.5 小时），
    因此它只能是"过期时刻"。旧代码按"签发时刻 + 固定 90 分钟"解读，会多算
    一个 TTL，导致真实过期后仍被判为有效（静默失效）。

    Args:
        cookie_str: Cookie 头字符串。

    Returns:
        毫秒时间戳；无令牌或无 13 位数字后缀时返回 None。
    """
    value = parse_cookie_fields(cookie_str).get(TOKEN_FIELD, "")
    if "_" not in value:
        return None
    suffix = value.rsplit("_", 1)[-1]
    if len(suffix) != 13 or not suffix.isdigit():
        return None
    return int(suffix)


def havana_expires_at_ms(cookie_str: str) -> int | None:
    """解析 havana_lgc_exp（会话凭据过期时刻，毫秒）。

    Args:
        cookie_str: Cookie 头字符串。

    Returns:
        毫秒时间戳；字段缺失或非数字返回 None。兼容 10 位秒级写法。
    """
    raw = parse_cookie_fields(cookie_str).get(HAVANA_EXP_FIELD, "")
    if not raw.isdigit():
        return None
    value = int(raw)
    return value * 1000 if value < 10**12 else value


def renewal_window_ms(observed_token_ttl_ms: int | None = None) -> int:
    """续期窗口：令牌剩余不足该值即视为"即将过期"。

    有实测 TTL 时取 TTL/6（沿用旧比例，但基于**实测值**而非写死的 90 分钟），
    并保证不低于 15 分钟。

    Args:
        observed_token_ttl_ms: 实测令牌有效期（毫秒），未知传 None。

    Returns:
        续期窗口（毫秒）。
    """
    if observed_token_ttl_ms and observed_token_ttl_ms > 0:
        return max(TOKEN_RENEWAL_WINDOW_MIN_MS, int(observed_token_ttl_ms) // 6)
    return TOKEN_RENEWAL_WINDOW_FALLBACK_MS


@dataclass(frozen=True)
class LayerInfo:
    """单层凭据的状态快照（供前端分层渲染）。"""

    key: str
    label: str
    state: str
    summary: str
    detail: str = ""
    expires_at_ms: int | None = None
    remaining_ms: int | None = None
    present_fields: tuple[str, ...] = ()
    absent_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "summary": self.summary,
            "detail": self.detail,
            "expires_at_ms": self.expires_at_ms,
            "remaining_ms": self.remaining_ms,
            "present_fields": list(self.present_fields),
            "absent_fields": list(self.absent_fields),
        }


@dataclass(frozen=True)
class CookieDiagnosis:
    """整份 Cookie 的分层诊断结论。"""

    state: str
    severity: str
    label: str
    #: 是否值得发起请求（令牌过期仍为 True —— 服务端会下发新令牌并自动重试）
    usable: bool
    #: 不可用时的阻断原因（可直接展示给用户）
    blocking_reason: str
    summary: str
    #: 一句话结论（保留状态关键词，供通知文案与界面复用）
    reason: str = ""
    layers: tuple[LayerInfo, ...] = field(default_factory=tuple)
    recommendations: tuple[str, ...] = field(default_factory=tuple)
    token_expires_at_ms: int | None = None
    token_remaining_ms: int | None = None
    havana_expires_at_ms: int | None = None
    havana_remaining_ms: int | None = None
    session_present: bool = False
    risk_present: bool = False
    now_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典（Web API / GUI 共用）。"""
        return {
            "state": self.state,
            "severity": self.severity,
            "label": self.label,
            "usable": self.usable,
            "blocking_reason": self.blocking_reason,
            "summary": self.summary,
            "reason": self.reason,
            "layers": [layer.to_dict() for layer in self.layers],
            "recommendations": list(self.recommendations),
            "token_expires_at_ms": self.token_expires_at_ms,
            "token_remaining_ms": self.token_remaining_ms,
            "havana_expires_at_ms": self.havana_expires_at_ms,
            "havana_remaining_ms": self.havana_remaining_ms,
            "session_present": self.session_present,
            "risk_present": self.risk_present,
            "now_ms": self.now_ms,
        }

    def layer(self, key: str) -> LayerInfo | None:
        """按 key 取某一层；不存在返回 None。"""
        for item in self.layers:
            if item.key == key:
                return item
        return None


def diagnose_cookie(
    cookie_str: str,
    *,
    now_ms: int | None = None,
    observed_token_ttl_ms: int | None = None,
) -> CookieDiagnosis:
    """对一份 Cookie 做分层诊断（纯函数，便于单测）。

    Args:
        cookie_str: Cookie 头字符串（应为明文）。
        now_ms: 当前时间（毫秒），None 取系统时间。
        observed_token_ttl_ms: 实测令牌有效期，用于自适应续期窗口。

    Returns:
        诊断结论；usable 表示"是否值得发起请求"。
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    raw = str(cookie_str or "").strip()
    if not raw:
        return CookieDiagnosis(
            state=STATE_MISSING, severity="error", label=STATE_LABEL[STATE_MISSING],
            usable=False, blocking_reason="未配置 Cookie",
            summary="未配置 Cookie，无法抓取", now_ms=now,
            recommendations=(
                "在「监控配置 -> Cookie」粘贴浏览器 Cookie，或点「获取 Cookie」扫码登录",
            ),
        )

    fields = parse_cookie_fields(raw)
    session_hit = tuple(f for f in (SESSION_ESSENTIAL_FIELDS + SESSION_SUPPORT_FIELDS) if f in fields)
    session_present = any(f in fields for f in SESSION_ESSENTIAL_FIELDS)
    session_strong = session_present and any(f in fields for f in SESSION_SUPPORT_FIELDS)
    havana_hit = tuple(f for f in HAVANA_FIELDS if f in fields)
    risk_hit = tuple(f for f in RISK_FIELDS if f in fields)
    token_present = TOKEN_FIELD in fields
    token_exp = token_expires_at_ms(raw) if token_present else None
    havana_exp = havana_expires_at_ms(raw)
    window = renewal_window_ms(observed_token_ttl_ms)
    token_remain: int | None = None

    layers: list[LayerInfo] = []

    # ---- [1] 登录态 ----
    if session_strong:
        session_state, session_summary = "ok", "登录态在场（cookie2 + 身份字段）"
    elif session_present:
        session_state, session_summary = "warn", "登录态在场，但伴随字段偏少"
    else:
        session_state, session_summary = "error", "缺少登录态字段（cookie2）"
    layers.append(LayerInfo(
        key="session", label="登录态", state=session_state, summary=session_summary,
        detail="决定服务端认为你是谁；只能由服务端吊销，本地无法判断过期",
        present_fields=session_hit,
        absent_fields=tuple(f for f in SESSION_ESSENTIAL_FIELDS if f not in fields),
    ))

    # ---- [2] 会话凭据（Havana）----
    if havana_exp:
        remain = havana_exp - now
        if remain <= 0:
            havana_state, havana_summary = "error", "会话凭据已过期"
            havana_detail = "需要免扫码刷新或重新扫码登录"
        elif remain < 24 * 3600 * 1000:
            havana_state, havana_summary = "warn", "会话凭据将在 24 小时内过期"
            havana_detail = "建议提前执行「免扫码刷新」续期"
        else:
            havana_state, havana_summary = "ok", "会话凭据有效"
            havana_detail = "到期前可用持久化浏览器 profile 免扫码刷新，无需重新扫码"
    elif havana_hit:
        havana_state, havana_summary = "warn", "存在会话凭据但无显式过期字段"
        havana_detail = "无法判断剩余时间，建议保持保活开启"
    else:
        havana_state, havana_summary = "info", "无会话凭据字段"
        havana_detail = "部分老版本导出不含该字段，不影响抓取"
    layers.append(LayerInfo(
        key="havana", label="会话凭据", state=havana_state, summary=havana_summary,
        detail=havana_detail, expires_at_ms=havana_exp,
        remaining_ms=(havana_exp - now) if havana_exp else None,
        present_fields=havana_hit,
    ))

    # ---- [3] 签名令牌 ----
    if not token_present:
        token_state, token_summary = "warn", "缺少签名令牌"
        token_detail = "首轮请求会由服务端下发新令牌（自动申领）"
    elif token_exp is None:
        token_state, token_summary = "warn", "令牌无时间戳，无法判断剩余"
        token_detail = "历史样本兼容：按可用对待"
    else:
        token_remain = token_exp - now
        if token_remain <= 0:
            token_state = "warn"
            token_summary = "令牌已过期（会自动续期，无需重新登录）"
            token_detail = "下次请求服务端会下发新令牌并自动重试一次；持续失败才说明登录态失效"
        elif token_remain <= window:
            token_state, token_summary = "warn", "令牌即将过期（下次请求自动续期）"
            token_detail = "保持监控或保活开启即可无缝续期"
        else:
            token_state, token_summary = "ok", "令牌有效"
            token_detail = "每次请求都会滑动续期，只要不长时间空闲就不会过期"
    layers.append(LayerInfo(
        key="token", label="签名令牌", state=token_state, summary=token_summary,
        detail=token_detail, expires_at_ms=token_exp, remaining_ms=token_remain,
        present_fields=tuple(f for f in (TOKEN_FIELD, TOKEN_ENC_FIELD) if f in fields),
        absent_fields=tuple(f for f in (TOKEN_FIELD, TOKEN_ENC_FIELD) if f not in fields),
    ))

    # ---- [4] 风控指纹 ----
    layers.append(LayerInfo(
        key="risk", label="风控指纹", state="ok" if risk_hit else "info",
        summary=("在场（环境一致性好）" if risk_hit else "缺失（不影响抓取）"),
        detail="由真实浏览器产生；本项目不伪造，仅保持环境稳定",
        present_fields=risk_hit,
    ))

    # ---- 可用性判定：令牌过期不阻断 ----
    # ---- 可用性判定：只要"有内容且可解析"就值得试一次 ----
    # v1.9 原则：**服务端才是权威**。本地预检只做提示、不做硬阻断 ——
    # 令牌过期可自愈；登录态是否真的失效，只能由一次真实请求回答。
    # 旧实现按"令牌过期"剔除条目，导致本可成功的一轮在配置层就直接失败。
    blocking = ""
    recommendations: list[str] = []
    if not session_present:
        state = STATE_SESSION_MISSING
        blocking = "本地判断：登录态字段（cookie2）缺失，很可能需要重新登录"
        recommendations.append(
            "仍会尝试一次抓取；若失败请重新扫码登录：python -m xianyu_alert.cli login",
        )
    elif havana_exp is not None and havana_exp - now <= 0:
        state = STATE_HAVANA_EXPIRED
        blocking = "本地判断：会话凭据已过期，请求很可能被拒"
        recommendations.append("先试「免扫码刷新」（复用持久化浏览器 profile），失败再扫码登录")
    elif not token_present:
        state = STATE_NO_TOKEN
        recommendations.append("直接抓取即可，首轮会下发新令牌")
    elif token_exp is None:
        state = STATE_OK
    elif token_exp - now <= 0:
        state = STATE_TOKEN_EXPIRED
        recommendations.append("无需操作：下次抓取会自动续期；若持续失败再检查登录态")
    elif token_exp - now <= window:
        state = STATE_TOKEN_EXPIRING
    else:
        state = STATE_OK

    # 非空且可解析 → 一律值得发起请求（服务端会给出权威结论）
    usable = True
    # 一句话结论（保留状态关键词，供通知文案与界面复用）
    if state == STATE_OK:
        reason = "有效（登录态与令牌均正常）"
    elif state == STATE_TOKEN_EXPIRING:
        reason = "令牌即将过期，下次抓取会自动续期"
    elif state == STATE_TOKEN_EXPIRED:
        reason = "登录令牌已过期（可自愈）：下次抓取会自动申请新令牌；持续失败才需重新登录"
    elif state == STATE_NO_TOKEN:
        reason = "缺少 _m_h5_tk（首轮抓取会自动申领）"
    elif state == STATE_SESSION_MISSING:
        reason = "登录态字段（cookie2）缺失，很可能需要重新登录（仍会尝试一次抓取）"
    elif state == STATE_HAVANA_EXPIRED:
        reason = "会话凭据已过期，建议先免扫码刷新，否则需重新登录"
    else:
        reason = blocking or "Cookie 状态需要关注"

    severity = STATE_SEVERITY.get(state, "warn")
    label = STATE_LABEL.get(state, state)
    if state in (STATE_TOKEN_EXPIRED, STATE_TOKEN_EXPIRING, STATE_NO_TOKEN):
        summary = "可用：令牌层会自动续期，不需要重新登录"
    elif state == STATE_OK:
        summary = "可用：登录态与令牌均正常"
    else:
        summary = f"仍会尝试一次抓取（{blocking}）"

    return CookieDiagnosis(
        state=state,
        severity=severity,
        label=label,
        usable=usable,
        blocking_reason=blocking,
        summary=summary,
        reason=reason,
        layers=tuple(layers),
        recommendations=tuple(recommendations),
        token_expires_at_ms=token_exp,
        token_remaining_ms=token_remain,
        havana_expires_at_ms=havana_exp,
        havana_remaining_ms=(havana_exp - now) if havana_exp else None,
        session_present=session_present,
        risk_present=bool(risk_hit),
        now_ms=now,
    )
