"""v1.8.1 源码级排查修复的回归测试。

覆盖三条真实缺陷（详见交付说明）：

  P0  Cookie 池「解密失败条目」在下次写盘时被静默删除（数据丢失）。
      触发场景：密钥更换 / secret.key 未随数据目录迁移。原因为
      `serialize_cookie_pool` 对「明文为空」的条目 `continue`，而
      `gui.config_to_form` 与 `MonitorService._read_pool_plaintext`
      都把解密失败条目的明文置空。

  P1  `parse_detail_sold_status` 把任何非 0 的 `itemStatus` 判为「已售出」，
      导致「校验在架」把仍在架的商品标记售出并从提醒列表隐藏。

  P1  `MtopFetcher.set_cookies` 不清空 session cookie jar，多账号轮换时
      上一账号残留的键（尤其 `_m_h5_tk_enc`）与新账号的 `_m_h5_tk` 配对。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import secure  # noqa: E402
from xianyu_alert.config import serialize_cookie_pool  # noqa: E402
from xianyu_alert.fetcher import MtopFetcher, parse_detail_sold_status  # noqa: E402
from web.monitor_service import MonitorService  # noqa: E402

#: 一条结构合法、含 _m_h5_tk 的 Cookie（时间戳为未来值，避免过期干扰）
SAMPLE_COOKIE = "unb=1; _m_h5_tk=abcdef1234_4102444800000"


# ---------------------------------------------------------------------- #
# P0：Cookie 池数据保真
# ---------------------------------------------------------------------- #
def test_serialize_keeps_entry_with_raw_cipher():
    """明文为空但带原始密文的条目必须保留（而不是被 continue 丢掉）。"""
    pool = [
        {"name": "healthy", "cookie": SAMPLE_COOKIE, "enabled": True},
        {"name": "broken", "cookie": "", "enabled": True, "_raw_cipher": "fernet1:Zm9v"},
    ]
    out = serialize_cookie_pool(pool, encrypt=True)
    assert [x["name"] for x in out] == ["healthy", "broken"], "解密失败条目被丢弃了"
    assert out[1]["cookie"] == "fernet1:Zm9v", "原密文未被原样回写"


def test_serialize_still_drops_truly_empty_entry():
    """既无明文也无密文的条目仍应跳过（不产生空壳条目）。"""
    out = serialize_cookie_pool([{"name": "empty", "cookie": "", "enabled": True}])
    assert out == []


def test_mixed_key_pool_survives_pool_action(monkeypatch):
    """端到端：换密钥导致条目无法解密后，做一次池操作不得删除任何条目。

    注意：这里**不用** pytest 的 `tmp_path` fixture —— 在受限沙盒里它的 basetemp
    （`/private/var/folders/.../pytest-of-*`）会被拒绝创建。改用
    `tempfile.mkdtemp()`（落在 /tmp 下），保证在任何环境都能跑起来。
    """
    tmp = Path(tempfile.mkdtemp(prefix="xy-v181-"))
    key_a = tmp / "key_a"
    key_b = tmp / "key_b"
    cfg = tmp / "config.yaml"

    # 用「密钥 A」加密一条 Cookie，模拟由另一套密钥写入的既有密文
    secure.set_key_file(str(key_a))
    legacy_cipher = secure.encrypt_text(SAMPLE_COOKIE)
    assert secure.is_encrypted(legacy_cipher)

    cfg.write_text(
        yaml.safe_dump(
            {
                "keywords": [{"keyword": "Switch", "max_price": 800}],
                "monitor": {
                    "cookies": "",
                    "cookie_pool": [
                        {"name": "acc-a", "cookie": legacy_cipher, "enabled": True}
                    ],
                },
                "fetcher": {"type": "mtop"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # 切成「密钥 B」：上面那条密文不再可解
    secure.set_key_file(str(key_b))
    monkeypatch.setenv("XY_DATA_DIR", str(tmp))
    service = MonitorService(config_path=str(cfg))
    try:
        listing = service.cookie_pool_list()
        pool = listing["pool"]
        assert len(pool) == 1, "解密失败的条目在展示列表里就消失了"
        # 应如实报告「无法解密」，而不是误报成「未配置」
        assert pool[0]["health_state"] == "invalid_encrypt", pool[0]

        # 任意池操作都会触发整池写盘 —— 这正是数据丢失的路径
        result = service.cookie_pool_action(action="toggle", name="acc-a")
        assert result["ok"] is True, result

        after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        entries = after["monitor"]["cookie_pool"]
        assert len(entries) == 1, "池操作后条目被删除了（数据丢失）"
        assert entries[0]["name"] == "acc-a"
        assert entries[0]["cookie"] == legacy_cipher, "原密文被改写/清空"
        assert entries[0]["enabled"] is False, "toggle 应已生效"
    finally:
        service.shutdown()
        secure.set_key_file(None)  # 恢复默认密钥路径，避免污染其它测试
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------- #
# P1：售出状态判定不得把未知码当成「已售出」
# ---------------------------------------------------------------------- #
def test_sold_status_online_is_true():
    assert parse_detail_sold_status({"itemDO": {"itemStatus": 0}}) is True
    assert parse_detail_sold_status({"itemDO": {"itemStatusStr": "在线"}}) is True


def test_sold_status_explicit_text_is_false():
    assert parse_detail_sold_status({"itemDO": {"itemStatusStr": "已售出"}}) is False
    assert parse_detail_sold_status({"itemDO": {"itemStatusStr": "已下架"}}) is False


def test_sold_status_unknown_code_is_none():
    """未出现过的枚举值必须判「无法判定」，不能误标为已售出。"""
    for code in (1, 2, 7, 99):
        assert parse_detail_sold_status({"itemDO": {"itemStatus": code}}) is None, code


def test_sold_status_missing_is_none():
    assert parse_detail_sold_status({}) is None
    assert parse_detail_sold_status(None) is None


# ---------------------------------------------------------------------- #
# P1：切换账号必须清空旧 cookie jar
# ---------------------------------------------------------------------- #
def test_set_cookies_clears_stale_jar_keys():
    old = "sid=old; _m_h5_tk=t1_4102444800000; _m_h5_tk_enc=oldenc"
    new = "uid=9; _m_h5_tk=t2_4102444800000"
    fetcher = MtopFetcher(cookies=old)
    try:
        assert "sid" in {c.name for c in fetcher.session.cookies}

        fetcher.set_cookies(new)

        names = {c.name for c in fetcher.session.cookies}
        assert "sid" not in names, "上一账号的 cookie 残留"
        assert "_m_h5_tk_enc" not in names, "旧 _m_h5_tk_enc 残留会与新 token 配错对"
        assert "uid" in names, "新账号 cookie 未被注入"
        assert fetcher.current_token() == "t2"
    finally:
        fetcher.close()


def test_set_cookies_to_empty_clears_jar():
    fetcher = MtopFetcher(cookies="sid=old; _m_h5_tk=t1_4102444800000")
    try:
        fetcher.set_cookies("")
        assert {c.name for c in fetcher.session.cookies} == set()
        assert fetcher.current_token() == ""
    finally:
        fetcher.close()
