"""路径解析统一入口（支持 PyInstaller frozen 打包 + macOS 数据目录）。

设计目标（对齐 macOS 适配设计文档 §3.3）：
    - 源码模式：所有相对数据路径锚定「项目根」；
    - frozen 模式（Windows/Linux）：config.yaml / state/ 等落在 **exe 同目录**；
    - frozen + darwin（macOS .app）：数据落 `~/Library/Application Support/闲鱼低价提醒工具/`
      （.app 包内只读 + 签名校验，必须写用户可写目录）；
    - `XY_DATA_DIR` 环境变量**优先级最高**（Docker / LaunchAgent / 高级用户覆盖）；
    - 资源文件（icon.ico 等）在 frozen 时从 sys._MEIPASS 读取。

数据目录语义（共享知识）：
    `XY_DATA_DIR` > frozen+darwin → Application Support > frozen 其它 → exe 目录 >
    源码 → 项目根。config.yaml / state/ / secret.key / 日志统一锚定 `data_dir()`。

本模块只依赖标准库 os/sys，**不允许 import 包内其它模块**（避免循环依赖）。
"""

from __future__ import annotations

import os
import sys

#: macOS Application Support 子目录名（用户已确认中文）
APP_DIR_NAME = "闲鱼低价提醒工具"


def is_frozen() -> bool:
    """判断当前是否运行在 PyInstaller 打包的程序中。

    Returns:
        True 表示 frozen（sys.frozen 存在且为真）。
    """
    return bool(getattr(sys, "frozen", False))


def app_base_dir() -> str:
    """返回应用「程序本体」根目录。

    frozen  → exe 所在目录；
    源码    → 项目根（xianyu_alert/ 的上一级）。

    注意：本函数**不承担数据定位**（数据一律锚定 `data_dir()`），
    仅被 shortcut（Windows 专属）与既有测试引用。

    Returns:
        绝对路径字符串。
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return project_root()


def project_root() -> str:
    """返回项目根目录（本文件位于 `<项目根>/xianyu_alert/paths.py`）。

    Returns:
        项目根绝对路径（与 frozen 状态无关，源码模式恒可用）。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    """返回应用数据目录（所有可写数据统一锚定此处）。

    优先级（对齐 macOS 适配设计文档 §3.3）：
        1. `XY_DATA_DIR` 环境变量（Docker / LaunchAgent / 高级用户）；
        2. frozen + darwin → `~/Library/Application Support/闲鱼低价提醒工具/`
           （macOS .app 包内只读，必须落用户可写目录；无需设环境变量即可双击运行）；
        3. frozen 其它平台 → exe 同目录（现状保持，`把 exe 复制到任意目录双击即用`）；
        4. 源码模式 → 项目根（开发 / 测试行为不变）。

    Returns:
        数据目录绝对路径（可能尚不存在，调用方用 ensure_data_dir() 创建）。
    """
    env = os.environ.get("XY_DATA_DIR")
    if env:
        # 环境变量优先；支持 `~` 展开与相对路径（相对当前工作目录）
        return os.path.abspath(os.path.expanduser(env))
    if is_frozen() and sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"),
            "Library",
            "Application Support",
            APP_DIR_NAME,
        )
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return project_root()


def ensure_data_dir() -> str:
    """确保数据目录存在（幂等 makedirs），返回其绝对路径。

    启动时调用一次（GUI / CLI / 打包入口都走），保证
    config.yaml / state/ / secret.key 可写。

    Returns:
        数据目录绝对路径。
    """
    target = data_dir()
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        # 创建失败不阻塞调用方（后续写操作会自行报错并提示）
        pass
    return target


def resource_path(rel: str) -> str:
    """定位打包资源文件（icon.ico 等）。

    frozen  → sys._MEIPASS/rel（PyInstaller 解包临时目录）；
    源码    → 项目根/rel。

    Args:
        rel: 相对路径，例如 "icon.ico"。

    Returns:
        资源的绝对路径（可能不存在，调用方自行判断）。
    """
    rel = str(rel or "")
    if is_frozen():
        base = getattr(sys, "_MEIPASS", app_base_dir())
        return os.path.join(base, rel)
    return os.path.join(app_base_dir(), rel)


def default_config_path() -> str:
    """返回默认配置文件路径：data_dir()/config.yaml。"""
    return os.path.join(data_dir(), "config.yaml")


def resolve_data_path(rel: str) -> str:
    """把相对数据路径锚定到 data_dir()，绝对路径原样返回。

    用于 state/xxx.db、日志目录等：frozen 后统一落在数据目录。

    Args:
        rel: 配置中的路径（可能是相对路径或绝对路径）。

    Returns:
        规范化后的绝对路径。
    """
    rel = str(rel or "").strip()
    if not rel:
        return data_dir()
    if os.path.isabs(rel):
        return os.path.abspath(rel)
    return os.path.abspath(os.path.join(data_dir(), rel))


def default_state_dir() -> str:
    """返回并确保 state/ 目录存在（锚定 data_dir()）。

    Returns:
        state 目录绝对路径。
    """
    state_dir = os.path.join(data_dir(), "state")
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        # 目录创建失败不阻塞调用方，返回路径本身
        pass
    return state_dir
