# -*- mode: python ; coding: utf-8 -*-
"""标准版 PyInstaller 打包配置（主推交付物，v1.8.0）。

特性：
    - onefile + windowed：双击即用，无需安装 Python / 依赖 / playwright；
    - **排除 playwright**（Cookie 走 `login --cookie-string` / 手动粘贴 / GUI 引导，
      标准版不依赖浏览器自动化）；
    - 入口 `build/entry.py`：带命令行参数走 CLI，无参数启动 GUI；
    - 图标复用原 exe 提取的 `icon.ico`（见 build/extract_icon.py）。

构建：
    cd 项目根
    python -m PyInstaller build/闲鱼低价提醒工具.spec --noconfirm
    # 或直接运行 build/build.bat
产物：dist/闲鱼低价提醒工具.exe
"""

import os

# SPECPATH = 本 spec 所在目录（PyInstaller 注入）；据此定位项目根
project_root = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(project_root, "build", "entry.py")],
    pathex=[project_root],
    binaries=[],
    datas=[],
    hiddenimports=[
        # PyYAML 偶发不被静态分析捕获，显式声明
        "yaml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 关键：标准版必须把 playwright 整棵依赖树排除干净，
        # 否则 cookie.py 内的延迟 import 会被 PyInstaller 打包，体积暴涨至 100MB+
        "playwright",
        "playwright.sync_api",
        "playwright.async_api",
        "playwright._impl",
        "playwright._impl._driver",
        # 关键：PySide6（Qt）仅供 macOS 端 gui_qt 使用，Windows 端走 Tkinter。
        # gui_qt 虽为函数内延迟导入，但 PyInstaller 静态分析该包时仍会收集
        # PySide6 整树（体积 +30MB 以上），必须整棵排除：
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "shiboken6",
        # 打包机上的开发工具也不应混入产物
        "pytest",
        "unittest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="闲鱼低价提醒工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 必须禁用：UPX 压缩会损坏 cryptography 的原生 _rust.pyd 导致 exe 启动崩溃（已知坑）
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed：不弹黑色控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, "icon.ico"),
)
