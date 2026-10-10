"""关键词规格语义匹配：把「光威 3200 64G」这样的搜索词真正当规格用（v1.11）。

真实背景（线上 71 条命中记录实测，关键词「光威 3200 64G」）：

    只按「必含词=光威」过滤 → 71/71 全部命中，其中 8G / 16G / 32G、DDR3、
    DDR5，甚至「金士顿」「酷兽」「海盗船」的商品统统放行，命中页没法看。

三类根因，各需不同对策：

    1. **标题被插入空格**（闲鱼反爬）：标题里会出现 "DDR4  32 00"、
       "3 2GB×2" 这种断点。字面子串 "3200" in title 会漏判 →
       改用**容忍空白的 token 匹配**（"32 00" 也能命中 "3200"）。
       但不能简单「删掉所有空白」再匹配："DDR3 8GB" 删空白后成
       "ddr38gb"，会被读成 38GB。所以数字修复**只合并"不紧跟字母"的断点**。
    2. **容量有等价写法**："32G×2" / "4×16G" / "4根16G" / "共64G"
       都是 64G。要求字面出现 "64G" 会误杀真 64G 商品 →
       用**容量算式**求总容量再比大小。
    3. **标题关键词堆砌**：真正的竞品在标题尾部堆一串「关联 光威 芝奇 英睿达…」
       来蹭搜索 → 用**品牌锚定**：目标品牌必须是标题中*最先出现*的品牌，
       堆砌尾巴里的品牌不算数。

本模块是纯函数（只吃字符串、只吐判定），不依赖网络 / 存储 / GUI，便于单元测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

#: 品牌 -> 别名元组（别名一律小写）。同名不同写法视为同一实体。
#: 只收录二手 3C 常见品牌；未收录的品牌不会参与「锚定」，判定会相应放宽（fail-open）。
BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "光威": ("光威", "gloway", "glowy"),
    "金士顿": ("金士顿", "kingston"),
    "海盗船": ("海盗船", "corsair", "vengeance"),
    "芝奇": ("芝奇", "g.skill", "gskill"),
    "英睿达": ("英睿达", "crucial"),
    "威刚": ("威刚", "adata", "xpg"),
    "金百达": ("金百达", "kingbank"),
    "三星": ("三星", "samsung"),
    "海力士": ("海力士", "hynix", "skhynix"),
    "镁光": ("镁光", "micron"),
    "阿斯加特": ("阿斯加特", "asgard"),
    "光威天策": (),  # 光威子系列，故意留空以免被当成竞品
    "七彩虹": ("七彩虹", "colorful"),
    "影驰": ("影驰", "galax"),
    "宇瞻": ("宇瞻", "apacer"),
    "枭鲸": ("枭鲸",),
    "玖合": ("玖合", "jiuhe"),
    "酷兽": ("酷兽", "cuso"),
    "宏想": ("宏想",),
    "金泰克": ("金泰克", "tigo"),
    "台电": ("台电", "teclast"),
    "联想": ("联想", "lenovo"),
    "戴尔": ("戴尔", "dell"),
    "惠普": ("惠普", "hp"),
    "华硕": ("华硕", "asus"),
    "微星": ("微星", "msi"),
    "技嘉": ("技嘉", "gigabyte"),
    "苹果": ("苹果", "apple", "iphone", "ipad", "macbook"),
    "小米": ("小米", "xiaomi", "redmi"),
    "华为": ("华为", "huawei", "荣耀", "honor"),
    "英特尔": ("英特尔", "intel"),
    "amd": ("amd", "锐龙", "ryzen"),
    "英伟达": ("英伟达", "nvidia", "geforce"),
}

#: 别名 -> 规范品牌名（模块加载时构造一次）
_ALIAS_TO_BRAND: dict[str, str] = {
    alias: brand for brand, aliases in BRAND_ALIASES.items() for alias in aliases
}

#: 容量："16G" / "16GB" / "16 g"。右边界要求后面不是数字或字母，
#: 避免 "16GHz" 这类误命中；左边界防止从 "3200" 中间切出 "200G"。
_CAPACITY_RE = re.compile(r"(?<![\d.])(\d{1,4})\s*(?:gb|g)(?![0-9a-z])", re.IGNORECASE)
#: 尺寸×数量："8G×2" / "8GB*2" / "8g x2"
_CAPACITY_MUL_AFTER_RE = re.compile(r"(\d{1,4})\s*(?:gb|g)\s*[*x×]\s*(\d{1,2})(?![\d.])", re.IGNORECASE)
#: 数量×尺寸："2*16G" / "2×16G"
_CAPACITY_MUL_BEFORE_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*[*x×]\s*(\d{1,4})\s*(?:gb|g)(?![0-9a-z])", re.IGNORECASE
)
#: 中文量词："4根16G" / "两条16G"
_CAPACITY_MUL_CN_RE = re.compile(r"(\d{1,2})\s*(?:根|条|个)\s*(\d{1,4})\s*(?:gb|g)(?![0-9a-z])", re.IGNORECASE)
#: 显式总量："共64G" / "一共 32 G"
_CAPACITY_TOTAL_RE = re.compile(r"(?:共|一共|总共|合计)\s*(\d{1,3})\s*(?:gb|g)(?![0-9a-z])", re.IGNORECASE)
#: 闲鱼会把空格插进数字中间（"3 2GB×2" 其实是 "32GB×2"）。
#: 两条护栏，缺一不可：
#:   1. 断点前的数字**不紧跟字母数字** —— "DDR3 8GB" 里的 3 属于 DDR3，
#:      绝不能合并成 "38GB"；
#:   2. 合并后的数字必须是**常见容量**（见 _PLAUSIBLE_CAPACITY_GB）——
#:      "iPhone 15 128G" 里 "15" 是型号、不能和 128 粘成 "15128G"
#:      （真实回归：容量丢失、频率还被算成 1512）。
_SPACED_NUMBER_RE = re.compile(r"(?<![0-9a-zA-Z])(\d{1,3}) +(\d{1,3})(?=\s*(?:gb|g)(?![0-9a-z]))", re.IGNORECASE)
#: 合并后允许的容量取值（内存 / 存储的常见规格）。不在其中就不合并。
_PLAUSIBLE_CAPACITY_GB = frozenset({2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256})
#: 容量 token（"64G" / "32gb"）—— 这类要求必须走容量算式，不能字面匹配：
#: 真 64G 的标题常写「32G×2」「4根16G」而根本不出现 "64G"。
_CAPACITY_TOKEN_RE = re.compile(r"\d+\s*(?:gb|g)", re.IGNORECASE)
#: 关键词里的频率 / 型号数字（3200 / 5600 / 4080…）
_NUMERIC_TOKEN_RE = re.compile(r"\d{3,4}")
#: 代际
_GENERATIONS = ("DDR5", "DDR4", "DDR3")

#: 视为「频率 / 型号」的数字范围（低于 1000 的（如 16、64）属于容量或型号尾缀）
_FREQUENCY_MIN = 1000
_FREQUENCY_MAX = 9000
#: 容量算式里允许的最大单条容量与条数（防脏数据算出天文数字）
_MAX_MODULE_GB = 256
_MAX_MODULE_COUNT = 8


@dataclass(frozen=True)
class KeywordSpec:
    """从搜索关键词解析出的规格要求（全部可空；为空表示该项不约束）。

    Attributes:
        brand: 目标品牌（规范名，如「光威」）；空串表示关键词未指定品牌。
        capacity_gb: 容量下限（GB）；0 表示关键词未指定容量。
        frequency_mhz: 频率 / 型号数字（如 3200、4080）；0 表示未指定。
        generation: 代际（DDR4 / DDR5 / DDR3）；空串表示未指定。

    说明：**普通中文 / 英文词不在这里强制**（如「笔记本」「国行」）—— 那种
    "必须出现某个词"的需求交给既有的 `required_keywords`，避免把所有搜索词
    都变成硬性条件（真实回归：「Switch OLED」的标题常只写「Switch」，一刀切会误杀）。
    本模块只管**高精度**的型号类约束：品牌、代际、频率 / 型号数字、容量。
    """

    brand: str = ""
    capacity_gb: int = 0
    frequency_mhz: int = 0
    generation: str = ""

    @property
    def empty(self) -> bool:
        """是否没有任何规格约束（此时匹配恒真）。"""
        return not (self.brand or self.capacity_gb or self.frequency_mhz or self.generation)

    def summary(self) -> str:
        """人类可读摘要，用于日志 / GUI / 文档示例。"""
        parts: list[str] = []
        if self.brand:
            parts.append(f"品牌 {self.brand}")
        if self.generation:
            parts.append(self.generation)
        if self.frequency_mhz:
            parts.append(f"{self.frequency_mhz}MHz")
        if self.capacity_gb:
            parts.append(f"容量 ≥{self.capacity_gb}G")
        return " · ".join(parts) if parts else "（无规格约束）"


@dataclass(frozen=True)
class SpecDecision:
    """规格匹配判定结果。

    Attributes:
        passed: 是否通过。
        reason: ok / spec_brand / spec_generation / spec_frequency / spec_capacity。
        detail: 人类可读说明（含证据）。
    """

    passed: bool
    reason: str = "ok"
    detail: str = ""


@lru_cache(maxsize=1024)
def _token_pattern(token: str) -> re.Pattern[str]:
    """把 token 编译成「字符之间允许任意空白」的正则（闲鱼会插入空格）。"""
    return re.compile(r"\s*".join(re.escape(ch) for ch in token), re.IGNORECASE)


def find_token(text: str, token: str) -> int:
    """在文本中查找 token，**容忍 token 内部被插入的空白**。

    Args:
        text: 待查文本（大小写不敏感）。
        token: 目标片段，如 "3200"、"DDR4"、"光威"。

    Returns:
        命中的起始下标；未命中返回 -1。空 token 一律返回 -1。
    """
    if not text or not token:
        return -1
    match = _token_pattern(str(token)).search(str(text))
    return match.start() if match else -1


def token_present(text: str, token: str) -> bool:
    """find_token 的布尔封装。"""
    return find_token(text, token) >= 0


def is_capacity_token(token: str) -> bool:
    """判断一个 token 是否是容量写法（"64G" / "32GB"）。

    Args:
        token: 关键词片段。

    Returns:
        True 表示应当用容量算式判定，而不是字面匹配。
    """
    return bool(_CAPACITY_TOKEN_RE.fullmatch(str(token or "").strip()))


def repair_spaced_numbers(text: str) -> str:
    """把被注入空格拆断的容量数字拼回去（"3 2GB" → "32GB"）。

    只作用于「数字 + 空白 + 数字 + 容量单位」这一种形态，且：
        - 断点前不是字母数字 —— "DDR3 8GB" 不会变成 "38GB"；
        - 合并结果必须是常见容量 —— "iPhone 15 128G" 不会变成 "15128G"。

    Args:
        text: 原始标题。

    Returns:
        修复后的文本（反复合并直到稳定）。
    """
    def _merge(match: re.Match[str]) -> str:
        merged = int(match.group(1) + match.group(2))
        if merged in _PLAUSIBLE_CAPACITY_GB:
            return match.group(1) + match.group(2)
        return match.group(0)

    current = str(text or "")
    while True:
        fixed = _SPACED_NUMBER_RE.sub(_merge, current)
        if fixed == current:
            return current
        current = fixed


def parse_title_capacity(title: str) -> tuple[int, list[str]]:
    """解析标题里的总容量（GB）与证据片段。

    综合五种写法取**最大**值：单条容量（"16G"）、尺寸×数量（"8G×2"）、
    数量×尺寸（"2×16G"）、中文量词（"4根16G"）、显式总量（"共64G"）。
    取最大值的理由：标题常同时写「单条32G」与「两根共64G」，买家关心的是总量。

    Args:
        title: 商品标题。

    Returns:
        (总容量GB, 证据片段列表)；解析不出时返回 (0, [])。
    """
    repaired = repair_spaced_numbers(title)
    values: list[tuple[int, str]] = []

    def _add(size: int, count: int, evidence: str) -> None:
        if 1 <= size <= _MAX_MODULE_GB and 1 <= count <= _MAX_MODULE_COUNT:
            values.append((size * count, evidence))

    for match in _CAPACITY_MUL_AFTER_RE.finditer(repaired):
        _add(int(match.group(1)), int(match.group(2)), match.group(0).strip())
    for match in _CAPACITY_MUL_BEFORE_RE.finditer(repaired):
        _add(int(match.group(2)), int(match.group(1)), match.group(0).strip())
    for match in _CAPACITY_MUL_CN_RE.finditer(repaired):
        _add(int(match.group(2)), int(match.group(1)), match.group(0).strip())
    for match in _CAPACITY_TOTAL_RE.finditer(repaired):
        _add(int(match.group(1)), 1, match.group(0).strip())
    for match in _CAPACITY_RE.finditer(repaired):
        _add(int(match.group(1)), 1, match.group(0).strip())

    if not values:
        return 0, []
    return max(value for value, _ in values), [evidence for _, evidence in values]


def _detect_brand(text: str) -> str:
    """返回文本中**最先出现**的品牌规范名（未识别到返回空串）。"""
    lowered = str(text or "").lower()
    best: tuple[int, str] = (len(lowered) + 1, "")
    for alias, brand in _ALIAS_TO_BRAND.items():
        position = lowered.find(alias)
        if 0 <= position < best[0]:
            best = (position, brand)
    return best[1]


def build_spec(keyword: str) -> KeywordSpec:
    """从搜索关键词解析规格要求（「关键词即规格」）。

    例：
        "光威 3200 64G"      -> 品牌 光威 / 3200MHz / 容量 ≥64G
        "4080S 32G"          -> 4080 / 容量 ≥32G（未识别品牌则不锚定）
        "Switch OLED"        -> 无数字规格，只按字面必含词处理
        "iPhone 15 128G 国行" -> 苹果 / 容量 ≥128G / 必须含「国行」

    Args:
        keyword: 搜索关键词。

    Returns:
        KeywordSpec；无法解析出任何约束时返回空规格。
    """
    text = str(keyword or "").strip()
    if not text:
        return KeywordSpec()

    repaired = repair_spaced_numbers(text)
    brand = _detect_brand(repaired)

    capacity = 0
    for match in _CAPACITY_MUL_AFTER_RE.finditer(repaired):
        size, count = int(match.group(1)), int(match.group(2))
        if 1 < count <= _MAX_MODULE_COUNT and size <= _MAX_MODULE_GB:
            capacity = max(capacity, size * count)
    for match in _CAPACITY_MUL_CN_RE.finditer(repaired):
        size, count = int(match.group(2)), int(match.group(1))
        if 1 < count <= _MAX_MODULE_COUNT and size <= _MAX_MODULE_GB:
            capacity = max(capacity, size * count)
    for match in _CAPACITY_RE.finditer(repaired):
        size = int(match.group(1))
        if size <= _MAX_MODULE_GB:
            capacity = max(capacity, size)

    frequency = 0
    for token in _NUMERIC_TOKEN_RE.findall(repaired):
        value = int(token)
        if _FREQUENCY_MIN <= value <= _FREQUENCY_MAX and value != capacity:
            frequency = max(frequency, value)

    generation = next((item for item in _GENERATIONS if item.lower() in repaired.lower()), "")

    return KeywordSpec(
        brand=brand,
        capacity_gb=capacity,
        frequency_mhz=frequency,
        generation=generation,
    )


def match_spec(title: str, spec: KeywordSpec) -> SpecDecision:
    """判断标题是否满足规格要求。

    判定顺序（先判最"硬"的品牌，便于给出最有解释力的原因）：
        品牌锚定 -> 代际 -> 频率 -> 容量。

    Args:
        title: 商品标题（大小写不敏感）。
        spec: build_spec 解析出的规格。

    Returns:
        SpecDecision；spec 为空规格时恒为通过。
    """
    text = str(title or "")
    if spec.empty:
        return SpecDecision(True, "ok")

    if spec.brand:
        # 品牌要按**别名**找：关键词写 "iPhone" 时标题里就是 iPhone，不能去找"苹果"
        aliases = BRAND_ALIASES.get(spec.brand, (spec.brand,))
        position = min(
            (found for found in (find_token(text, alias) for alias in aliases) if found >= 0),
            default=-1,
        )
        if position < 0:
            return SpecDecision(False, "spec_brand", f"标题未出现品牌「{spec.brand}」")
        first_brand = _detect_brand(text)
        if first_brand and first_brand != spec.brand:
            return SpecDecision(
                False,
                "spec_brand",
                f"标题最先出现的品牌是「{first_brand}」而非「{spec.brand}」（疑似关键词堆砌）",
            )

    if spec.generation and not token_present(text, spec.generation):
        return SpecDecision(False, "spec_generation", f"标题未出现 {spec.generation}")

    if spec.frequency_mhz and not token_present(text, str(spec.frequency_mhz)):
        return SpecDecision(False, "spec_frequency", f"标题未出现 {spec.frequency_mhz}")

    if spec.capacity_gb:
        total, evidence = parse_title_capacity(text)
        if total < spec.capacity_gb:
            shown = "、".join(evidence[:3]) if evidence else "无容量描述"
            return SpecDecision(
                False, "spec_capacity", f"总容量 {total}G < 要求的 {spec.capacity_gb}G（证据：{shown}）"
            )

    return SpecDecision(True, "ok")
