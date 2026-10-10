"""MtopFetcher：mtop 签名接口采集（主路径）。"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import requests

from ..config import DEFAULT_USER_AGENT
from ..models import Product
from ..parsing import (  # noqa: F401 - 再导出：既有调用点与测试无需改动
    extract_product_id,
    parse_price,
    parse_publish_time,
)
from ..reqmeter import REQ_METER
from .base import (
    Fetcher,
    FetchError,
)
from .constants import (
    _RET_RISK_MARKERS,
    _RET_SESSION_MARKERS,
    _RET_TOKEN_MARKERS,
    BASE_URL,
    COOKIE_GUIDE,
    MTOP_API_NAME,
    MTOP_APP_KEY,
    MTOP_DETAIL_API_NAME,
    MTOP_DETAIL_URL,
    MTOP_TOKEN_COOKIE,
    MTOP_TOKEN_ENC_COOKIE,
    MTOP_URL,
    PAGE_SLEEP,
)
from .mtop_api import (
    _contains_any,
    _ret_text,
    build_detail_payload,
    build_search_payload,
    extract_token,
    mtop_sign,
    parse_cookie_string,
    parse_detail_sold_status,
    parse_mtop_result_list,
)

logger = logging.getLogger(__name__)

# 闲鱼 Web 站点
class MtopFetcher(Fetcher):
    """通过闲鱼 h5api mtop 接口抓取「最新发布」商品。

    这是**唯一能真正拿到闲鱼数据**的实现。必须携带用户登录后的 Cookie
    （含 `_m_h5_tk`），可用 `python -m xianyu_alert.cli login` 一键获取。

    实现要点：
        1. 签名 `md5(token&t&appKey&data)`，token 取 `_m_h5_tk` 下划线前半段；
        2. 内部维护 requests.Session，服务端刷新的 `_m_h5_tk` 会被自动吸收；
        3. 命中「令牌过期」时用新 token 重算 sign **自动重试一次**（mtop 标准行为）；
        4. 命中风控 / 登录态失效时抛出 FetchError 并给出可操作的中文指引。

    Attributes:
        cookies: 原始 Cookie 请求头字符串。
        user_agent: 浏览器 UA。
        timeout: 单次请求超时（秒）。
        retries: 网络异常时的总尝试次数。
        page_size: 每次搜索拉取的商品条数。
        pages: 多页抓取的总页数（默认 1 不改变现状）。
        page_sleep: 翻页之间的限速秒数。
    """

    name = "mtop"

    def __init__(
        self,
        cookies: str = "",
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        retries: int = 3,
        backoff_base: float = 1.5,
        page_size: int = 30,
        pages: int = 1,
        page_sleep: float = PAGE_SLEEP,
        session: requests.Session | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        """初始化 mtop 抓取器。

        Args:
            cookies: 登录后的 Cookie 请求头字符串（必须含 `_m_h5_tk`）。
            user_agent: 浏览器 UA。
            timeout: 请求超时秒数。
            retries: 网络异常时的总尝试次数（>=1）。
            backoff_base: 指数退避基数。
            page_size: 每页商品数（1~100）。
            pages: 多页抓取总页数（>=1；翻页会增加请求频率，有风控风险）。
            page_sleep: 翻页间隔秒数（限速）。
            session: 可注入的 requests.Session（便于测试）。
            sleep_func: 可注入的 sleep 实现（便于测试中免等待）。
        """
        self.cookies: str = str(cookies or "").strip()
        self.user_agent: str = user_agent or DEFAULT_USER_AGENT
        self.timeout: float = float(timeout)
        self.retries: int = max(1, int(retries))
        self.backoff_base: float = float(backoff_base)
        self.page_size: int = max(1, min(100, int(page_size)))
        self.pages: int = max(1, int(pages))
        self.page_sleep: float = float(page_sleep) if page_sleep is not None else PAGE_SLEEP
        self.session: requests.Session = session if session is not None else requests.Session()
        self._sleep: Callable[[float], None] = sleep_func or time.sleep
        #: v3.4 服务端价格筛选：价格上限（元），None 表示不过滤
        self._max_price: float | None = None

        #: Cookie 字典，作为 token 的权威来源（服务端刷新后就地更新）
        self._cookie_dict: dict[str, str] = parse_cookie_string(self.cookies)
        #: 本进程内是否吸收过服务端下发的新令牌（供上层决定是否落盘）
        self._token_refreshed: bool = False
        self._sync_session_cookies()

    # ------------------------------------------------------------------ #
    # Cookie / token 管理
    # ------------------------------------------------------------------ #
    def _sync_session_cookies(self) -> None:
        """把 Cookie 字典写入 session.cookies（失败不影响主流程）。"""
        for name, value in self._cookie_dict.items():
            try:
                self.session.cookies.set(name, value, domain=".goofish.com")
            except Exception:  # noqa: BLE001 - 注入的假 session 可能不支持
                logger.debug("[mtop] 写入 session cookie 失败：%s", name)

    def set_cookies(self, cookie_str: str) -> None:
        """轮换时替换 Cookie（v3.2 多 Cookie 池）。

        更新请求头、token 权威字典与会话 Cookie，保证下一次请求
        使用新账号的登录态；服务端后续刷新的 `_m_h5_tk` 仍会被吸收。

        v1.8.1 修正：切换前**先清空 session.cookies**。原实现只按同名覆盖，
        上一账号残留、而新账号没有的键（尤其 `_m_h5_tk_enc`）会与新账号的
        `_m_h5_tk` 配成一对，导致 mtop 签名校验失败或账号串号 ——
        多 Cookie 池轮换实际上形同虚设。

        Args:
            cookie_str: 新的 Cookie 请求头字符串（可为空串）。
        """
        self.cookies = str(cookie_str or "").strip()
        self._cookie_dict = parse_cookie_string(self.cookies)
        try:
            self.session.cookies.clear()
        except Exception:  # noqa: BLE001 - 注入的假 session 可能不支持
            logger.debug("[mtop] 清空 session cookie 失败（忽略）")
        self._sync_session_cookies()
        logger.debug("[mtop] 已切换 Cookie（长度 %d）", len(self.cookies))

    def current_token(self) -> str:
        """返回当前用于签名的 token（`_m_h5_tk` 下划线前半段）。"""
        return extract_token(self._cookie_dict.get(MTOP_TOKEN_COOKIE, ""))

    def token_refreshed(self) -> bool:
        """本进程内是否吸收过服务端下发的**新**令牌。

        供上层（MonitorService）判断"内存里的登录态比磁盘新"，
        进而决定是否落盘 —— 避免每次重启都退回令牌已过期的旧配置。
        """
        return bool(self._token_refreshed)

    def refreshed_cookie_string(self) -> str:
        """把当前内存中的 Cookie 字典拼回请求头字符串（用于持久化）。

        Returns:
            `k1=v1; k2=v2` 形式；无可用键时返回空串。
        """
        parts = [f"{k}={v}" for k, v in self._cookie_dict.items() if str(v or "").strip()]
        return "; ".join(parts)

    def clear_token_refreshed(self) -> None:
        """清除「令牌已刷新」脏标记（落盘成功后由上层调用）。"""
        self._token_refreshed = False

    def set_max_price(self, max_price: float | None) -> None:
        """设置服务端价格筛选上限（v3.4）。

        下次 `_search` 构造请求体时会把阈值写入
        `propValueStr.searchFilter="priceRange:0,{max_price};"` 并置
        `fromFilter=true`，使接口返回「最新发布且价格<max_price」的
        商品，与闲鱼网页行为一致。

        Args:
            max_price: 价格上限（元）；None 表示不过滤价格。
        """
        try:
            self._max_price = float(max_price) if max_price is not None else None
        except (TypeError, ValueError):  # pragma: no cover - 防御脏数据
            self._max_price = None
        logger.debug("[mtop] 服务端价格上限已更新：%s", self._max_price)

    def _absorb_token(self, response: Any) -> str:
        """从响应的 Set-Cookie 中吸收新的 `_m_h5_tk` 及其配对 `_m_h5_tk_enc`。

        mtop 的令牌是**滑动续期**的：服务端会在响应里下发一对新的
        `_m_h5_tk` + `_m_h5_tk_enc`（2026-09-24 实测 `Max-Age=5400`）。
        两者必须**成对更新** —— `_enc` 是服务端回验用的配对值，只换 `_tk`
        会让下一轮 `set_cookies()` 用 `_cookie_dict` 重建 cookie jar 时
        把旧的 `_enc` 写回去，造成 `_tk`/`_enc` 错配、签名校验失败。

        Args:
            response: requests 响应对象（或结构兼容的测试替身）。

        Returns:
            吸收到的新 `_m_h5_tk` 值；没有则返回空串。
        """
        new_value = ""
        new_enc = ""
        # 1) 优先走 requests 的 cookie jar
        try:
            jar = getattr(response, "cookies", None)
            if jar is not None:
                new_value = str(jar.get(MTOP_TOKEN_COOKIE) or "")
                new_enc = str(jar.get(MTOP_TOKEN_ENC_COOKIE) or "")
        except Exception:  # noqa: BLE001 - jar 实现各异，失败即降级
            new_value = ""
            new_enc = ""
        # 2) 降级：手工解析 Set-Cookie 响应头
        if not new_value or not new_enc:
            with contextlib.suppress(Exception):
                headers = getattr(response, "headers", None) or {}
                raw = str(headers.get("Set-Cookie") or headers.get("set-cookie") or "")
                if not new_value:
                    match = re.search(r"_m_h5_tk=([^;,\s]+)", raw)
                    if match:
                        new_value = match.group(1)
                if not new_enc:
                    match_enc = re.search(r"_m_h5_tk_enc=([^;,\s]+)", raw)
                    if match_enc:
                        new_enc = match_enc.group(1)

        changed = False
        if new_value and new_value != self._cookie_dict.get(MTOP_TOKEN_COOKIE):
            self._cookie_dict[MTOP_TOKEN_COOKIE] = new_value
            with contextlib.suppress(Exception):
                self.session.cookies.set(MTOP_TOKEN_COOKIE, new_value, domain=".goofish.com")
            changed = True
            logger.debug("[mtop] 已吸收服务端刷新的 %s", MTOP_TOKEN_COOKIE)
        if new_enc and new_enc != self._cookie_dict.get(MTOP_TOKEN_ENC_COOKIE):
            self._cookie_dict[MTOP_TOKEN_ENC_COOKIE] = new_enc
            with contextlib.suppress(Exception):
                self.session.cookies.set(MTOP_TOKEN_ENC_COOKIE, new_enc, domain=".goofish.com")
            changed = True
            logger.debug("[mtop] 已吸收服务端刷新的 %s", MTOP_TOKEN_ENC_COOKIE)
        if changed:
            # 只置标记，不在这里写盘：fetcher 不持有配置路径，
            # 落盘由 MonitorService 在轮次结束后节流执行。
            self._token_refreshed = True
        return new_value

    def _check_cookies(self) -> None:
        """校验 Cookie 是否具备发起 mtop 请求的条件。

        Raises:
            FetchError: Cookie 为空，或不含 `_m_h5_tk`。
        """
        if not self.cookies and not self._cookie_dict:
            raise FetchError(COOKIE_GUIDE, kind="config")
        if not self._cookie_dict.get(MTOP_TOKEN_COOKIE):
            raise FetchError(
                f"Cookie 中缺少 {MTOP_TOKEN_COOKIE}，无法计算 mtop 签名，说明 Cookie 已失效或不完整。"
                "请重新运行 `python -m xianyu_alert.cli login` 获取。",
                kind="config",
            )

    def check_cookie_health(self) -> tuple[bool, str]:
        """检查 Cookie 是否过期 / 临期（不阻断请求）。

        解析 `_m_h5_tk` 内嵌的 13 位毫秒时间戳（有效期见 `cookie.TOKEN_TTL_MS`，
        实测 90 分钟、且**每次请求都会由服务端滑动续期**）：
        过期或临期时返回 (False, 提示文案)，但**不拦截请求**——
        具体请求是否成功由服务端决定，这里只负责给出可操作的提示。
        「过期」只表示距上次成功请求已超过有效期，抓取时通常会自动换回新令牌。

        Returns:
            (是否健康, 原因文案)。
        """
        from ..cookie import cookie_expiry_status, token_expiring_text, token_ttl_text

        # 以 `_cookie_dict` 里的 token 为准：服务端会在响应中续期 `_m_h5_tk`，
        # `_absorb_token` 只更新该字典（mtop 签名的权威来源），而 `self.cookies`
        # 仍是首次注入的原始串 —— 用后者检测会在 token 实际已续期后，每次抓取
        # 都打出一条「Cookie 已过期」的假告警（日志噪音，误导排查方向）。
        token = self._cookie_dict.get(MTOP_TOKEN_COOKIE, "")
        source = f"{MTOP_TOKEN_COOKIE}={token}" if token else self.cookies
        status = cookie_expiry_status(source)
        if status == "expired":
            return (
                False,
                f"登录令牌已过期（{token_ttl_text()}内未续期）—— 抓取时会自动申请新令牌；"
                "若持续失败再考虑重新登录",
            )
        if status == "expiring":
            return False, f"令牌即将过期（剩余不足 {token_expiring_text()}），抓取时会自动续期"
        if status == "missing":
            return False, "未配置登录 Cookie，无法发起 mtop 请求"
        if status == "no_token":
            return False, f"Cookie 中缺少 {MTOP_TOKEN_COOKIE}，无法计算 mtop 签名"
        if status == "unknown":
            return True, "Cookie 未包含可解析的 _m_h5_tk 时间戳，无法判断是否过期"
        return True, "Cookie 状态正常"

    def check_item_status(self, product_id: str, timeout: float | None = None) -> bool | None:
        """校验单个商品是否仍在架（v3.7 需求 3，方案 B 详情接口判定）。

        走闲鱼商品详情接口 `mtop.taobao.idle.pc.detail`（**实测可用**）：
        - 返回 True 表示在架（itemDO.itemStatusStr="在线" / itemStatus=0）；
        - 返回 False 表示已售出 / 已下架（itemStatusStr 变为其它文案）；
        - 返回 None 表示无法判定（请求失败 / 风控 / 响应结构缺失），
          调用方应跳过该商品而不是误判为售出。

        注意：内部走 `_post_once`，会按 `self.retries`（默认 3）重试并做指数退避，
        因此单个商品的**最坏耗时**约为 `retries × timeout + 退避总和`，
        并不等同于「单次请求」。批量校验必须由调用方控制节奏（GUI / Web
        「校验在架」按固定间隔限速），并且取消操作只会在**两个商品之间**生效 ——
        最坏要等当前商品的重试跑完。

        Args:
            product_id: 商品 ID（数字串）。
            timeout: 覆盖默认超时秒数；None 使用构造时的 self.timeout。

        Returns:
            True = 在架；False = 已售出/下架；None = 无法判定。
        """
        pid = str(product_id or "").strip()
        if not pid:
            return None
        try:
            self._check_cookies()
        except FetchError:
            return None
        try:
            result = self._post_once(
                build_detail_payload(pid),
                api_name=MTOP_DETAIL_API_NAME,
                api_url=MTOP_DETAIL_URL,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 详情校验是尽力而为，任何失败都按「无法判定」处理
            logger.warning("[mtop] 校验商品 %s 详情失败：%s", pid, exc)
            return None
        ret_text = _ret_text(result)
        if "SUCCESS" not in ret_text.upper():
            logger.warning("[mtop] 校验商品 %s 详情接口返回异常：%s", pid, ret_text)
            return None
        return parse_detail_sold_status(result.get("data"))

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        """构造 mtop 请求头。"""
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": BASE_URL + "/",
            "Origin": BASE_URL,
            "Connection": "keep-alive",
        }

    def _build_params(self, timestamp: str, sign: str, api_name: str = MTOP_API_NAME) -> dict[str, str]:
        """构造 mtop query 参数。

        Args:
            timestamp: 13 位毫秒时间戳。
            sign: mtop 签名。
            api_name: 目标接口名（默认搜索接口；v3.7 详情校验传详情接口名）。
        """
        return {
            "jsv": "2.7.2",
            "appKey": MTOP_APP_KEY,
            "t": timestamp,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api_name,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.search.0.0",
        }

    def _post_once(
        self,
        payload: dict[str, Any],
        api_name: str = MTOP_API_NAME,
        api_url: str = MTOP_URL,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """用当前 token 签名并发起一次请求（含网络层重试）。

        Args:
            payload: 请求体（搜索为搜索体；v3.7 详情校验为详情体）。
            api_name: 目标接口名（默认搜索接口）。
            api_url: 目标接口 URL（默认搜索接口）。
            timeout: 覆盖默认超时秒数；None 使用构造时的 self.timeout。

        Returns:
            解析后的响应 JSON 字典。

        Raises:
            FetchError: 网络重试耗尽，或响应不是合法 JSON。
        """
        token = self.current_token()
        timestamp = str(int(time.time() * 1000))
        data = json.dumps(payload, separators=(",", ":"))
        sign = mtop_sign(token, timestamp, MTOP_APP_KEY, data)

        params = self._build_params(timestamp, sign, api_name=api_name)
        body = {"data": data}
        request_timeout = self.timeout if timeout is None else float(timeout)

        last_error: BaseException | None = None
        # v1.11.4：每一次真实出网都记账（含重试与保活），供状态页展示请求节奏
        request_kind = "detail" if api_name == MTOP_DETAIL_API_NAME else "search"
        for attempt in range(1, self.retries + 1):
            REQ_METER.note(request_kind)
            try:
                response = self.session.post(
                    api_url,
                    params=params,
                    data=body,
                    headers=self._headers(),
                    timeout=request_timeout,
                )
                status = getattr(response, "status_code", 200)
                if status != 200:
                    raise FetchError(f"mtop 接口返回 HTTP {status}")
                # 无论成功失败都吸收服务端下发的新 token
                self._absorb_token(response)
                try:
                    result = response.json()
                except Exception as exc:  # noqa: BLE001 - 非 JSON 响应
                    raise FetchError(f"mtop 响应不是合法 JSON：{exc}") from exc
                if not isinstance(result, dict):
                    raise FetchError(f"mtop 响应结构异常（期望 dict，实际 {type(result).__name__}）")
                return result
            except Exception as exc:  # noqa: BLE001 - 统一转换为 FetchError
                last_error = exc
                if attempt < self.retries:
                    delay = self.backoff_base ** attempt
                    logger.warning(
                        "[mtop] 请求失败（第 %d/%d 次）：%s，%.1fs 后重试",
                        attempt, self.retries, exc, delay,
                    )
                    self._sleep(delay)
        raise FetchError(f"请求闲鱼 mtop 接口失败，已重试 {self.retries} 次：{last_error}")

    def _search(self, keyword: str, page_number: int = 1) -> dict[str, Any]:
        """执行一次搜索，并处理 mtop 的业务返回码。

        令牌过期时会用服务端新下发的 `_m_h5_tk` 重算签名**自动重试一次**。

        Args:
            keyword: 搜索关键词。
            page_number: 页码（从 1 开始）。

        Returns:
            成功的响应 JSON。

        Raises:
            FetchError: 风控 / 登录态失效 / 重试后仍失败。
        """
        payload = build_search_payload(
            keyword,
            page_number=page_number,
            rows_per_page=self.page_size,
            max_price=self._max_price,
        )

        for round_index in range(2):  # 最多 2 次：首发 + 令牌过期后重试 1 次
            result = self._post_once(payload)
            ret_text = _ret_text(result)

            if "SUCCESS" in ret_text.upper():
                return result

            if _contains_any(ret_text, _RET_TOKEN_MARKERS):
                if round_index == 0:
                    logger.warning("[mtop] 令牌过期（%s），已用新 token 重算签名重试", ret_text)
                    continue
                raise FetchError(
                    f"mtop 令牌连续两次过期（{ret_text}）。令牌本应由服务端自动续期，"
                    "连续失败通常说明登录态也已失效，请重新获取 Cookie。",
                    kind="token",
                )

            # 注意判定顺序：会话标记必须排在风控之前。
            # `_RET_RISK_MARKERS` 里有宽泛的 `SM::`，而登录态失效的真实返回形如
            # `FAIL_SYS_SESSION_EXPIRED::SM::…` —— 若先判风控，会把"该重新登录"
            # 误报成"该降速"，给出完全错误的处置建议。
            if _contains_any(ret_text, _RET_SESSION_MARKERS):
                raise FetchError(
                    f"闲鱼登录态已失效（{ret_text}）。"
                    "请重新运行 `python -m xianyu_alert.cli login` 获取 Cookie。",
                    kind="session",
                )

            if _contains_any(ret_text, _RET_RISK_MARKERS):
                raise FetchError(
                    f"触发闲鱼风控（{ret_text}）。这**不是** Cookie 失效 —— "
                    "重新登录解决不了：请调大 monitor.interval_seconds（建议 ≥300 秒）、"
                    "启用多账号 Cookie 池轮换，或更换出口网络后重试。",
                    kind="risk",
                )

            raise FetchError(f"mtop 接口返回异常：{ret_text or '(空 ret)'}", kind="unknown")

        # 理论上不可达（循环内必然 return 或 raise）
        raise FetchError("mtop 搜索失败：未获得有效响应")  # pragma: no cover

    # ------------------------------------------------------------------ #
    def fetch(self, keyword: str) -> list[Product]:
        """按关键词抓取最新发布的商品（支持多页 + 页级容错）。

        - 默认 `pages=1` 与旧行为完全一致（单页请求）；
        - `pages>1` 时循环抓取第 1..N 页，页间 `sleep(page_sleep)` 限速；
        - 单页失败只 warning，**不丢整轮**；全部页失败才抛 FetchError；
        - 跨页按 product_id 去重合并。

        Args:
            keyword: 搜索关键词。

        Returns:
            Product 列表；接口正常但无结果时返回空列表。

        Raises:
            FetchError: Cookie 缺失 / 风控 / 登录失效 / 全部页失败。
        """
        self._check_cookies()
        ok, reason = self.check_cookie_health()
        if not ok:
            logger.warning("[mtop] %s", reason)

        logger.info(
            "[mtop] 抓取关键词「%s」（最新发布，每页 %d 条，共 %d 页，页间隔 %.1fs）",
            keyword, self.page_size, self.pages, self.page_sleep,
        )

        all_products: list[Product] = []
        seen: set = set()
        failed_pages = 0
        last_error: BaseException | None = None
        #: v1.11.3：首个风控异常。命中风控必须**整轮中止并向上抛**，
        #: 而不是像普通页失败那样"跳过该页"——否则上层（monitor）收不到
        #: 风控信号，也就无法开启熔断冷却。
        risk_error: FetchError | None = None

        for page in range(1, self.pages + 1):
            try:
                result = self._search(keyword, page_number=page)
                data = result.get("data")
                data = data if isinstance(data, dict) else {}
                result_list = data.get("resultList")
                page_products = parse_mtop_result_list(result_list, keyword)
                new_items = [p for p in page_products if p.product_id not in seen]
                for product in new_items:
                    seen.add(product.product_id)
                    all_products.append(product)
                logger.info(
                    "[mtop] 第 %d/%d 页解析到 %d 个商品（去重后累计 %d）",
                    page, self.pages, len(page_products), len(all_products),
                )
            except FetchError as exc:
                # 页级容错：单页失败只记录 warning，继续抓取其余页
                failed_pages += 1
                last_error = exc
                logger.warning(
                    "[mtop] 第 %d/%d 页抓取失败：%s（已跳过该页，继续抓取其余页）",
                    page, self.pages, exc,
                )
                if getattr(exc, "kind", "") == "risk":
                    # v1.11.3：命中风控就别再打剩余页了 —— 被限流时"继续试探"
                    # 只会把风控越撞越紧（线上实测：一小时内 129 条 RGV587）。
                    logger.warning(
                        "[mtop] 命中风控，**停止抓取剩余 %d 页**（v1.11.3 熔断）",
                        self.pages - page,
                    )
                    risk_error = exc
                    break
            if page < self.pages:
                self._sleep(self.page_sleep)

        if risk_error is not None:
            raise FetchError(f"[mtop] 命中闲鱼风控，本轮抓取中止：{risk_error}", kind="risk")

        if failed_pages == self.pages:
            detail = f"（最后错误：{last_error}）" if last_error is not None else ""
            raise FetchError(f"[mtop] 全部 {self.pages} 页均抓取失败，放弃本轮抓取{detail}")

        if not all_products:
            logger.warning(
                "[mtop] 关键词「%s」未解析到任何商品（%d 页）。"
                "可能是该关键词确实无新品，或返回结构已变化。",
                keyword,
                self.pages,
            )
        else:
            logger.info("[mtop] 关键词「%s」解析到 %d 个商品（共 %d 页）", keyword, len(all_products), self.pages)
        return all_products

    def close(self) -> None:
        """关闭内部 session。"""
        with contextlib.suppress(Exception): # 关闭失败不影响主流程
            self.session.close()


# ---------------------------------------------------------------------- #
# 旧版 HTML 抓取器（对闲鱼实测无效，保留作通用示例）
# ⚠️ v3.2 起已标记「废弃（legacy）」：GUI 不再展示、config.example.yaml
#    不再推荐、默认值改为 mtop。**代码保留**仅用于向后兼容既有 config.yaml
#    中显式配置了 `fetcher.type: web` 的场景；新配置请改用 mtop / mock。
# ---------------------------------------------------------------------- #

__all__ = [
    "MtopFetcher",
]
