"""Cookie 自动获取（Playwright 路径）测试（v1.10.8，M06 补强）。

重评显示 `xianyu_alert/cookie.py` 覆盖率 75%，缺口最大的是
`acquire_via_playwright`（44 行）—— 这条路径正是"免扫码自动续期"的核心，
却几乎没有断言。

本文件用**注入的假 playwright 模块**（塞进 sys.modules）把整条链路离线跑通：
    未装 Playwright / profile 不存在却要求静默 / 启动失败 / 目录不可建 /
    正常抓到 `_m_h5_tk` / 等满超时仍未登录。
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert import cookie as cookie_mod  # noqa: E402
from xianyu_alert.cookie import LoginTimeout, PlaywrightUnavailable, acquire_via_playwright  # noqa: E402


class FakePage:
    def __init__(self) -> None:
        self.goto_calls: list = []

    def goto(self, url, wait_until=None):  # noqa: ANN001, ANN003
        self.goto_calls.append((url, wait_until))


class FakeContext:
    def __init__(self, cookies: list, pages: list | None = None) -> None:
        self._cookies = cookies
        self.pages = pages if pages is not None else [FakePage()]
        self.closed = 0

    def cookies(self) -> list:
        return [dict(c) for c in self._cookies]

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed += 1


class FakeChromium:
    def __init__(self, context=None, error: Exception | None = None) -> None:
        self.context = context
        self.error = error
        self.launch_kwargs: dict = {}

    def launch_persistent_context(self, **kwargs):  # noqa: ANN003
        self.launch_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def install_fake_playwright(chromium: FakeChromium) -> None:
    """把假 playwright.sync_api 塞进 sys.modules。

    注意：假包必须给 ``__path__``（声明为包），否则真 playwright 的 ``sync_api``
    在被其它路径导入时会去反查 ``playwright._impl``，撞上我们的假包报 AttributeError ——
    本文件第一版就踩了这个坑（真 playwright 其实装在环境里）。
    """
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: FakePlaywright(chromium)
    module.__path__ = []  # type: ignore[attr-defined]
    pkg = types.ModuleType("playwright")
    pkg.__path__ = []  # type: ignore[attr-defined]
    pkg.sync_api = module
    sys.modules["playwright"] = pkg
    sys.modules["playwright.sync_api"] = module


class CookieCase(unittest.TestCase):
    """公共夹具：临时 profile + **默认装好假 playwright**。

    每个用例都先装假包（而不是各测各装），避免"有的用例用真包、有的用假包"
    导致的顺序耦合。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = os.path.join(self._tmp.name, "browser_profile")
        self._patches = [
            mock.patch.object(cookie_mod, "profile_dir", return_value=self.profile),
            mock.patch.object(cookie_mod, "profile_ready", side_effect=lambda: os.path.isdir(self.profile)),
        ]
        for p in self._patches:
            p.start()
        self.install_fake(
            FakeChromium(
                context=FakeContext([{"name": cookie_mod.REQUIRED_COOKIE_NAME, "value": "default_token"}])
            )
        )

    def install_fake(self, chromium: FakeChromium) -> None:
        """换成本用例需要的假浏览器。"""
        install_fake_playwright(chromium)

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        for name in ("playwright", "playwright.sync_api"):
            sys.modules.pop(name, None)
        self._tmp.cleanup()


class TestUnavailable(unittest.TestCase):
    """未安装 Playwright：必须给出可执行的安装指引。"""

    def test_import_error_gives_install_hint(self) -> None:
        with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
            with self.assertRaises(PlaywrightUnavailable) as ctx:
                acquire_via_playwright()
        message = str(ctx.exception)
        self.assertIn("pip install playwright", message)
        self.assertIn("playwright install chromium", message)


class TestProfilePreconditions(CookieCase):
    """profile 存在性与目录创建。"""

    def test_silent_without_profile_fails_fast(self) -> None:
        """headless 且无 profile：必须**立刻**失败，不能白等超时。"""
        with self.assertRaises(LoginTimeout) as ctx:
            acquire_via_playwright(headless=True)
        self.assertIn("尚未建立浏览器登录 profile", str(ctx.exception))

    def test_unwritable_profile_dir_raises_login_timeout(self) -> None:
        with mock.patch("xianyu_alert.cookie.os.makedirs", side_effect=OSError("read-only")):
            with self.assertRaises(LoginTimeout) as ctx:
                acquire_via_playwright(headless=False)
        self.assertIn("无法创建浏览器 profile 目录", str(ctx.exception))

    def test_launch_failure_reports_playwright_unavailable(self) -> None:
        # 必须有 profile，否则会先被"无 profile"分支拦下（那一条由上面的用例覆盖）
        os.makedirs(self.profile, exist_ok=True)
        self.install_fake(FakeChromium(error=RuntimeError("chromium 未安装")))
        with self.assertRaises(PlaywrightUnavailable) as ctx:
            acquire_via_playwright(headless=True)
        self.assertIn("playwright install chromium", str(ctx.exception))


class TestSuccessfulCapture(CookieCase):
    """正常抓到关键 Cookie。"""

    def _profile_exists(self) -> None:
        os.makedirs(self.profile, exist_ok=True)

    def test_returns_cookie_header_with_required_token(self) -> None:
        self._profile_exists()
        context = FakeContext(
            [
                {"name": "cookie2", "value": "abc", "domain": ".goofish.com"},
                {"name": "_m_h5_tk", "value": "tok_1791559226130", "domain": ".goofish.com"},
            ]
        )
        chromium = FakeChromium(context=context)
        install_fake_playwright(chromium)
        header = acquire_via_playwright(headless=True, timeout=5)
        self.assertIn("_m_h5_tk=tok_1791559226130", header)
        self.assertIn("cookie2=abc", header)

    def test_launch_uses_persistent_profile_and_container_args(self) -> None:
        """必须用持久化 profile（否则每次都要重新扫码）+ 容器安全参数。"""
        self._profile_exists()
        chromium = FakeChromium(context=FakeContext([{"name": "_m_h5_tk", "value": "t"}]))
        self.install_fake(chromium)
        acquire_via_playwright(headless=True, timeout=5)
        self.assertEqual(chromium.launch_kwargs.get("user_data_dir"), self.profile)
        self.assertTrue(chromium.launch_kwargs.get("headless"))
        args = chromium.launch_kwargs.get("args") or []
        self.assertIn("--no-sandbox", args, "容器内 root 运行必须关沙盒")
        self.assertIn("--disable-dev-shm-usage", args)

    def test_new_page_when_no_pages(self) -> None:
        self._profile_exists()
        context = FakeContext([{"name": "_m_h5_tk", "value": "t"}], pages=[])
        self.install_fake(FakeChromium(context=context))
        header = acquire_via_playwright(headless=True, timeout=5)
        self.assertIn("_m_h5_tk=t", header)
        self.assertTrue(context.pages, "无页面时应新建页面")

    def test_reuses_existing_page(self) -> None:
        self._profile_exists()
        page = FakePage()
        context = FakeContext([{"name": "_m_h5_tk", "value": "t"}], pages=[page])
        self.install_fake(FakeChromium(context=context))
        acquire_via_playwright(headless=True, timeout=5)
        self.assertTrue(page.goto_calls, "应复用已有页面并导航")


class TestTimeout(CookieCase):
    """等满超时仍未取到关键 Cookie。"""

    def test_timeout_raises_login_timeout(self) -> None:
        os.makedirs(self.profile, exist_ok=True)
        context = FakeContext([{"name": "cookie2", "value": "abc"}])  # 没有 _m_h5_tk
        self.install_fake(FakeChromium(context=context))
        with self.assertRaises(LoginTimeout):
            acquire_via_playwright(headless=True, timeout=1)


if __name__ == "__main__":
    unittest.main()
