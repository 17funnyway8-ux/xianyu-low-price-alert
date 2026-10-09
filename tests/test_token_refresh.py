"""v1.8.2 回归测试：登录令牌的自动续期链路。

覆盖 2026-09-24 基于 NAS 生产实例实测所做的四项修正：

  1. **TTL 校准**：`_m_h5_tk` 实测有效期 90 分钟（响应的 `Max-Age=5400`），
     此前代码假定 24 小时 —— 差值区间内会把已失效的令牌误报为「正常」。
  2. **`_m_h5_tk_enc` 成对吸收**：实测令牌续期时服务端同时下发 `_tk` 与 `_enc`，
     只更新 `_tk` 会在下一轮 `set_cookies()` 重建 cookie jar 时造成两者错配。
  3. **失败分层**：`FetchError.kind` 区分 token / session / risk / config，
     且**会话标记必须优先于风控标记**（风控标记里含宽泛的 `SM::`，
     否则 `FAIL_SYS_SESSION_EXPIRED::SM::…` 会被误判成「该降速」）。
  4. **令牌节流落盘**：服务端刷新的令牌写回配置，进程重启不再退回旧令牌。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import secure  # noqa: E402
from xianyu_alert.cookie import (  # noqa: E402
    PROFILE_DIR_NAME,
    TOKEN_EXPIRING_SOON_MS,
    TOKEN_TTL_MS,
    profile_dir,
)
from xianyu_alert.fetcher import (  # noqa: E402
    MTOP_TOKEN_COOKIE,
    MTOP_TOKEN_ENC_COOKIE,
    FetchError,
    MtopFetcher,
)


class _FakeResp:
    """requests 响应替身：只有 `cookies`（jar）与 `headers`（Set-Cookie）。"""

    def __init__(self, cookies=None, headers=None):
        self.cookies = cookies if cookies is not None else {}
        self.headers = headers if headers is not None else {}


class _RetStubFetcher(MtopFetcher):
    """把 `_post_once` 换成预设 ret 序列，用于验证失败分层（不发网络请求）。"""

    def __init__(self, rets):
        super().__init__(cookies="a=1; _m_h5_tk=tok_4102444800000; _m_h5_tk_enc=e")
        self._rets = list(rets)

    def _post_once(self, payload, api_name=None, api_url=None, timeout=None):  # noqa: ANN001
        ret = self._rets.pop(0) if self._rets else "SUCCESS"
        return {"ret": [ret]}


# ---------------------------------------------------------------------- #
# 1. TTL 校准
# ---------------------------------------------------------------------- #
def test_ttl_matches_measured_value():
    """锁定实测值：2026-09-24 从 mtop 响应的 Set-Cookie 读出 `Max-Age=5400`。"""
    assert TOKEN_TTL_MS == 5400 * 1000, "TTL 与实测不符，请核对 Set-Cookie 的 Max-Age"


def test_expiring_window_is_a_fraction_of_ttl():
    """预警窗口必须显著小于 TTL，否则令牌一签发就报「即将过期」。"""
    assert 0 < TOKEN_EXPIRING_SOON_MS < TOKEN_TTL_MS
    assert TOKEN_EXPIRING_SOON_MS <= TOKEN_TTL_MS // 2


def test_profile_dir_sits_under_data_dir():
    """浏览器 profile 必须落在数据目录下（随数据卷迁移，免重复扫码）。"""
    tmp = Path(tempfile.mkdtemp(prefix="xy-prof-"))
    old = os.environ.get("XY_DATA_DIR")
    try:
        os.environ["XY_DATA_DIR"] = str(tmp)
        assert profile_dir() == str(tmp / PROFILE_DIR_NAME)
    finally:
        if old is None:
            os.environ.pop("XY_DATA_DIR", None)
        else:
            os.environ["XY_DATA_DIR"] = old
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------- #
# 2. _m_h5_tk 与 _m_h5_tk_enc 成对吸收
# ---------------------------------------------------------------------- #
def test_absorb_token_updates_both_tk_and_enc():
    fetcher = MtopFetcher(cookies="a=1; _m_h5_tk=old_1000; _m_h5_tk_enc=oldenc")
    try:
        resp = _FakeResp(
            cookies={MTOP_TOKEN_COOKIE: "new_2000", MTOP_TOKEN_ENC_COOKIE: "newenc"}
        )
        fetcher._absorb_token(resp)
        assert fetcher._cookie_dict[MTOP_TOKEN_COOKIE] == "new_2000"
        assert fetcher._cookie_dict[MTOP_TOKEN_ENC_COOKIE] == "newenc", "配对值未同步"
        assert fetcher.token_refreshed() is True, "未置脏标记，上层不会落盘"
    finally:
        fetcher.close()


def test_absorb_token_parses_set_cookie_header_without_jar():
    """没有 cookie jar 时，走 Set-Cookie 文本解析兜底，且同样要拿到 `_enc`。"""
    fetcher = MtopFetcher(cookies="a=1; _m_h5_tk=old_1000")
    try:
        resp = _FakeResp(cookies={}, headers={"Set-Cookie": "b=2; _m_h5_tk=new_3000; _m_h5_tk_enc=encx"})
        fetcher._absorb_token(resp)
        assert fetcher._cookie_dict[MTOP_TOKEN_COOKIE] == "new_3000"
        assert fetcher._cookie_dict[MTOP_TOKEN_ENC_COOKIE] == "encx"
    finally:
        fetcher.close()


def test_rotation_does_not_drop_absorbed_enc():
    """吸收新令牌后再做一次 Cookie 轮换，重建 jar 时 `_enc` 不得被旧值覆盖。

    这正是「只更新 `_tk` 不更新 `_enc`」会引发的故障路径：`set_cookies()` 会
    清空 jar 并用 `_cookie_dict` 重建，若 `_enc` 还是旧的，两者就错配了。
    """
    fetcher = MtopFetcher(cookies="a=1; _m_h5_tk=old_1000; _m_h5_tk_enc=oldenc")
    try:
        fetcher._absorb_token(
            _FakeResp(cookies={MTOP_TOKEN_COOKIE: "new_2000", MTOP_TOKEN_ENC_COOKIE: "newenc"})
        )
        fetcher.set_cookies(fetcher.refreshed_cookie_string())
        assert fetcher._cookie_dict[MTOP_TOKEN_ENC_COOKIE] == "newenc"
        assert fetcher.session.cookies.get(MTOP_TOKEN_ENC_COOKIE) == "newenc"
    finally:
        fetcher.close()


def test_token_refreshed_flag_can_be_cleared():
    fetcher = MtopFetcher(cookies="a=1; _m_h5_tk=old_1000")
    try:
        fetcher._absorb_token(_FakeResp(cookies={MTOP_TOKEN_COOKIE: "new_2000"}))
        assert fetcher.token_refreshed() is True
        fetcher.clear_token_refreshed()
        assert fetcher.token_refreshed() is False
    finally:
        fetcher.close()


# ---------------------------------------------------------------------- #
# 3. 失败分层（kind）与判定顺序
# ---------------------------------------------------------------------- #
def test_risk_failure_is_kind_risk():
    fetcher = _RetStubFetcher(["RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!"])
    try:
        with pytest.raises(FetchError) as ei:
            fetcher._search("test")
        assert ei.value.kind == "risk"
        # 风控文案必须**明确否掉**「重新登录」这个错误方向
        assert "重新登录" in str(ei.value)
        assert "不是" in str(ei.value)
    finally:
        fetcher.close()


def test_session_marker_wins_over_risk_marker():
    """`FAIL_SYS_SESSION_EXPIRED::SM::…` 必须判为 session，而不是 risk。

    风控标记里有宽泛的 `SM::`，若判定顺序写反，就会把「该重新登录」
    误报成「该降速」—— 实测中登录态失效的返回正是这种带 `SM::` 的形态。
    """
    fetcher = _RetStubFetcher(["FAIL_SYS_SESSION_EXPIRED::SM::登录态已失效"])
    try:
        with pytest.raises(FetchError) as ei:
            fetcher._search("test")
        assert ei.value.kind == "session", "会话标记必须优先于风控标记"
    finally:
        fetcher.close()


def test_token_failure_after_two_rounds_is_kind_token():
    """令牌标记在第一轮触发自动重试，第二轮仍失败才抛出，且 kind=token。"""
    fetcher = _RetStubFetcher(
        ["FAIL_SYS_TOKEN_EXOIRED::令牌过期", "FAIL_SYS_TOKEN_EXOIRED::令牌过期"]
    )
    try:
        with pytest.raises(FetchError) as ei:
            fetcher._search("test")
        assert ei.value.kind == "token"
    finally:
        fetcher.close()


def test_missing_token_is_kind_config():
    fetcher = MtopFetcher(cookies="a=1; b=2")
    try:
        with pytest.raises(FetchError) as ei:
            fetcher._check_cookies()
        assert ei.value.kind == "config"
    finally:
        fetcher.close()


def test_fetch_error_defaults_to_network_kind():
    assert FetchError("boom").kind == "network"


# ---------------------------------------------------------------------- #
# 4. 刷新后的令牌节流落盘
# ---------------------------------------------------------------------- #
class _PersistStub:
    """只实现 `_persist_refreshed_token` 需要的三个方法。"""

    def __init__(self, cookie: str):
        self._cookie = cookie
        self._dirty = True
        self.clear_calls = 0

    def token_refreshed(self) -> bool:
        return self._dirty

    def refreshed_cookie_string(self) -> str:
        return self._cookie

    def clear_token_refreshed(self) -> None:
        self._dirty = False
        self.clear_calls += 1


def test_token_persist_writes_ciphertext_and_is_throttled(monkeypatch):
    """落盘的必须是密文；且节流窗口内第二次调用不再写盘。"""
    tmp = Path(tempfile.mkdtemp(prefix="xy-v182-"))
    try:
        monkeypatch.setenv("XY_DATA_DIR", str(tmp))
        secure.set_key_file(str(tmp / "secret.key"))
        cfg = tmp / "config.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "keywords": [{"keyword": "Switch", "max_price": 800}],
                    "monitor": {"cookies": "", "cookie_pool": []},
                    "fetcher": {"type": "mtop"},
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        from web.monitor_service import MonitorService

        service = MonitorService(config_path=str(cfg))
        try:
            stub = _PersistStub(
                "unb=9; _m_h5_tk=newtok_4102444800000; _m_h5_tk_enc=newenc"
            )
            assert service._persist_refreshed_token(stub) is True
            assert stub.clear_calls == 1, "落盘成功后应清除脏标记"

            saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
            cipher = str(saved["monitor"]["cookies"])
            assert cipher.startswith("fernet1:"), "令牌必须以密文落盘"
            assert "newtok_4102444800000" not in cipher, "落盘内容泄漏了明文令牌"

            # 节流：同一窗口内再调不会被写盘
            stub._dirty = True
            assert service._persist_refreshed_token(stub) is False
            assert stub.clear_calls == 1
        finally:
            service.shutdown()
    finally:
        secure.set_key_file(None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_token_persist_noop_when_not_refreshed():
    """没有刷新过就不应写盘（避免每轮无意义地重建 Config）。"""
    tmp = Path(tempfile.mkdtemp(prefix="xy-v182b-"))
    try:
        from web.monitor_service import MonitorService

        service = MonitorService(config_path=str(tmp / "config.yaml"))
        try:
            stub = _PersistStub("unb=1; _m_h5_tk=x_1")
            stub._dirty = False
            assert service._persist_refreshed_token(stub) is False
            assert stub.clear_calls == 0
        finally:
            service.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
