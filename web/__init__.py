"""闲鱼低价提醒工具 —— Web 基础版（P1）。

容器 / 远程访问入口：FastAPI + 原生 HTML/JS 单页 + monitor 后台线程。

版本号沿用 `xianyu_alert.__version__`（与桌面版保持一致，不重复维护）。
业务核心（monitor / fetcher / storage / notifier / cookie / secure /
config / paths / singleton）零改动，全部原样复用。
"""

from __future__ import annotations

from xianyu_alert import __version__

__all__ = ["__version__"]
