"""部署形态识别（v1.10.4，M12）。

背景：路径解析此前是一串 if/else（XY_DATA_DIR -> frozen+darwin -> frozen -> 源码），
逻辑小而关键，但**新增一种部署形态时很容易漏**（测评原话）。这里把它改成
**声明式矩阵**：每种形态一条记录（检测条件 + 数据目录规则 + 说明），按顺序匹配。

收益：
    1. 新增形态 = 往 FORMS 里加一条，检测/日志/测试都自动覆盖到；
    2. 矩阵本身可以被测试逐一枚举（tests/test_deployment_matrix.py），
       避免"加了形态却没人测"；
    3. 运行期可以打印"当前识别为哪种形态、数据落在哪"，
       NAS 上排查路径问题时不用再猜（describe()）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

#: 目录名（与 paths.APP_DIR_NAME 保持一致，这里重复声明以避免循环依赖）
APP_DIR_NAME = "闲鱼低价提醒工具"


@dataclass(frozen=True)
class DeploymentForm:
    """一种部署形态的判定规则。

    Attributes:
        kind: 机器可读标识（custom / macos-app / portable / source）。
        label: 中文短名（用于日志与界面）。
        description: 这条形态是什么、数据为什么落在这里。
        matches: 检测函数 (env, platform, frozen) -> bool。
        data_dir: 数据目录计算函数。
    """

    kind: str
    label: str
    description: str
    matches: Callable[[dict, str, bool], bool]
    data_dir: Callable[[dict, str, bool], str]


@dataclass(frozen=True)
class DeploymentInfo:
    """当前部署形态的快照（可 JSON 序列化）。"""

    kind: str
    label: str
    description: str
    data_dir: str
    frozen: bool
    platform: str
    env_override: bool

    def to_dict(self) -> dict:
        """转成字典（供 /healthz、日志、诊断命令使用）。"""
        return {
            "kind": self.kind,
            "label": self.label,
            "data_dir": self.data_dir,
            "frozen": self.frozen,
            "platform": self.platform,
            "env_override": self.env_override,
        }

    def describe(self) -> str:
        """一行人类可读描述（排查路径问题的第一手信息）。"""
        origin = "XY_DATA_DIR 环境变量" if self.env_override else self.label
        return f"部署形态：{self.label}（{self.kind}）｜数据目录：{self.data_dir}｜来源：{origin}"


#: 部署形态矩阵（**按顺序匹配，第一条命中生效**）。
#: 新增形态请同时更新 tests/test_deployment_matrix.py 的期望表。
FORMS: tuple[DeploymentForm, ...] = (
    DeploymentForm(
        kind="custom",
        label="自定义数据目录",
        description="通过 XY_DATA_DIR 指定（Docker / LaunchAgent / 高级用户）",
        matches=lambda env, platform, frozen: bool(env.get("XY_DATA_DIR")),
        data_dir=lambda env, platform, frozen: os.path.abspath(
            os.path.expanduser(str(env.get("XY_DATA_DIR")))
        ),
    ),
    DeploymentForm(
        kind="macos-app",
        label="macOS 应用包",
        description="打包后的 .app 内部只读，数据必须落用户可写目录（无需设环境变量即可双击运行）",
        matches=lambda env, platform, frozen: frozen and platform == "darwin",
        data_dir=lambda env, platform, frozen: os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", APP_DIR_NAME
        ),
    ),
    DeploymentForm(
        kind="portable",
        label="绿色便携版",
        description="打包后的可执行文件旁边（把 exe 复制到任意目录双击即用）",
        matches=lambda env, platform, frozen: frozen,
        data_dir=lambda env, platform, frozen: os.path.dirname(os.path.abspath(sys.executable)),
    ),
    DeploymentForm(
        kind="source",
        label="源码运行",
        description="源码模式，数据落项目根（开发与测试行为不变）",
        matches=lambda env, platform, frozen: True,
        data_dir=lambda env, platform, frozen: os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        ),
    ),
)


def detect_deployment(
    env: dict | None = None,
    platform: str | None = None,
    frozen: bool | None = None,
) -> DeploymentInfo:
    """识别当前部署形态（可注入环境/平台/冻结态，便于测试枚举矩阵）。

    Args:
        env: 环境变量字典；None 用 os.environ。
        platform: sys.platform；None 用 sys.platform。
        frozen: 是否 PyInstaller 打包；None 用 sys.frozen。

    Returns:
        DeploymentInfo（kind / label / data_dir 等）。
    """
    environment = dict(os.environ) if env is None else dict(env)
    plat = sys.platform if platform is None else platform
    is_frozen_flag = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)

    for form in FORMS:
        if form.matches(environment, plat, is_frozen_flag):
            return DeploymentInfo(
                kind=form.kind,
                label=form.label,
                description=form.description,
                data_dir=form.data_dir(environment, plat, is_frozen_flag),
                frozen=is_frozen_flag,
                platform=plat,
                env_override=form.kind == "custom",
            )
    # FORMS 最后一条 matches 恒为 True，理论上不可达；显式兜底避免"静默返回 None"
    raise RuntimeError("没有匹配到任何部署形态（FORMS 矩阵被破坏）")


def describe_deployment() -> str:
    """当前部署形态的一行描述（供日志 / 诊断）。"""
    return detect_deployment().describe()
