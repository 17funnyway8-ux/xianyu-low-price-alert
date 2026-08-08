# -*- mode: python ; coding: utf-8 -*-
"""macOS 专用 PyInstaller 打包配置（M4 arm64 .app）。

对齐 macOS 适配设计文档 §3.5：
    - EXE → COLLECT → BUNDLE（macOS .app 结构）；`console=False`（windowed）；
    - `target_arch='arm64'`（M4 默认 arm64，显式声明防回归）；
    - **排除 playwright**（标准版不带浏览器自动化，同 Windows 标准版）；
    - Info.plist 关键项：
        `NSAppSleepDisabled=true`（防 App Nap，7×24 挂机关键）、
        `NSHighResolutionCapable=true`（Retina）、
        `LSMinimumSystemVersion=13.0`；
    - 图标 `build/icon.icns`（由 icon.ico 经 build/make_icns.py + make_icns.sh 生成）；
    - 构建后 ad-hoc 签名：`build/macos_build.sh` 内执行
      `codesign --force --deep --sign - "dist/闲鱼低价提醒工具.app"`。

构建（必须在 M4 Mac 上执行，PyInstaller 不支持跨平台交叉打包）：
    cd 项目根
    bash build/macos_build.sh
产物：dist/闲鱼低价提醒工具.app
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
        # PyYAML / cryptography 偶发不被静态分析捕获，显式声明
        "yaml",
        "cryptography",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 标准版排除 playwright 整棵依赖树（Cookie 走粘贴/CLI，无需浏览器自动化）
        "playwright",
        "playwright.sync_api",
        "playwright.async_api",
        "playwright._impl",
        "playwright._impl._driver",
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
    [],
    exclude_binaries=True,
    name="闲鱼低价提醒工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed：.app 不弹终端
    disable_windowed_traceback=False,
    argv_emulation=True,  # macOS：Finder 双击启动参数处理（标准做法）
    target_arch="arm64",  # M4 显式 arm64（防回归 x86_64 兼容构建）
    codesign_identity=None,  # 构建后由 macos_build.sh 统一 ad-hoc 签名
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="闲鱼低价提醒工具",
)

app = BUNDLE(
    coll,
    name="闲鱼低价提醒工具.app",
    icon=os.path.join(project_root, "build", "icon.icns"),
    bundle_identifier="com.xianyu-alert.app",
    info_plist={
        "CFBundleName": "闲鱼低价提醒工具",
        "CFBundleDisplayName": "闲鱼低价提醒工具",
        "CFBundleShortVersionString": "1.7.0",
        "CFBundleVersion": "1.7.0",
        "CFBundleExecutable": "闲鱼低价提醒工具",
        "NSHighResolutionCapable": True,
        # 关键：抑制 App Nap，保证 7×24 挂机时后台轮询不被系统降频
        "NSAppSleepDisabled": True,
        "LSMinimumSystemVersion": "13.0",
    },
)
