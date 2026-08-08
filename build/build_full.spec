# -*- mode: python ; coding: utf-8 -*-
"""完整版 PyInstaller 打包配置（可选交付物）。

特性：
    - onefile + windowed，**包含 playwright**（GUI「获取 Cookie」方式一可用）；
    - 注意：完整版仍要求目标机执行一次
      `playwright install chromium`（浏览器内核约 300MB，不由 PyInstaller 打包），
      因此「环境无依赖」承诺只能由标准版兑现；
    - 适合想要自动登录体验、且接受一次性安装浏览器内核的用户。

构建：
    cd 项目根
    python -m PyInstaller build/build_full.spec --noconfirm
    # 或直接运行 build/build_full.bat
产物：dist/闲鱼低价提醒工具_完整版.exe
"""

import os

project_root = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(project_root, "build", "entry.py")],
    pathex=[project_root],
    binaries=[],
    datas=[],
    hiddenimports=[
        "yaml",
        # 完整版显式包含 playwright（GUI 自动登录用）
        "playwright.sync_api",
        "playwright.async_api",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
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
    name="闲鱼低价提醒工具_完整版",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, "icon.ico"),
)
