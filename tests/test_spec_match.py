"""关键词规格语义匹配测试（v1.11）：把「光威 3200 64G」这样的搜索词当规格用。

背景（线上实测）：关键词「光威 3200 64G」原本 71 条命中里只有 5 条真的是
光威 DDR4-3200 64G —— 8G / 16G / 32G、DDR3、DDR5，甚至金士顿 / 酷兽 /
海盗船的商品全部放行。三类根因：标题被插入空格、容量等价写法、关键词堆砌尾巴。

本文件分两部分：
    1. 纯函数单测（token 空白容忍 / 数字修复 / 容量算式 / 品牌锚定 / 规格解析）；
    2. **真实语料回归**：71 条线上标题逐条断言该不该命中。期望值由人工审阅
       71 条标题给出（不是用被测实现反推），因此能真正抓住"过松 / 过紧"的回归。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_alert.filters import (  # noqa: E402
    matches_required_keywords,
    required_keywords_default,
)
from xianyu_alert.spec_match import (  # noqa: E402
    build_spec,
    find_token,
    is_capacity_token,
    match_spec,
    parse_title_capacity,
    parse_title_modules,
    repair_spaced_numbers,
    token_present,
)


class TestTokenMatching(unittest.TestCase):
    """token 匹配必须容忍闲鱼插入的空白，但不能放宽到"删掉所有空白"。"""

    def test_plain_and_case_insensitive(self) -> None:
        self.assertTrue(token_present("金百达 DDR4 3200 16G", "16g"))
        self.assertTrue(token_present("金百达 ddr4 3200 16g", "DDR4"))
        self.assertFalse(token_present("金百达 DDR4 3200 8G", "16G"))

    def test_tolerates_injected_space(self) -> None:
        """真实数据：「DDR4  32 00MHz」里的 3200 被空格拆断。"""
        title = "光威（Gloway）16GB(8GBx2) DDR4  32 00 台式机内存条"
        self.assertFalse("3200" in title.lower())  # 字面子串确实查不到
        self.assertTrue(token_present(title, "3200"))
        self.assertGreaterEqual(find_token(title, "3200"), 0)

    def test_empty_token_never_matches(self) -> None:
        self.assertFalse(token_present("任意标题", ""))
        self.assertEqual(find_token("任意标题", ""), -1)

    def test_required_keywords_share_the_same_semantics(self) -> None:
        self.assertTrue(matches_required_keywords("DDR4  32 00 16G", ["3200", "16G"]))
        self.assertFalse(matches_required_keywords("DDR4  32 00 16G", ["3200", "64G"]))


class TestSpacedNumberRepair(unittest.TestCase):
    """数字修复只合并不紧跟字母的断点（DDR3 8GB 不能变成 38GB）。"""

    def test_merges_injected_split(self) -> None:
        self.assertIn("32GB", repair_spaced_numbers("俩条价出光威 DDR4 3200 3 2GB×2 笔记本内存条"))
        self.assertIn("16GB", repair_spaced_numbers("光威DDR5 6000MHz 1 6GB单条内存"))

    def test_does_not_glue_model_number_to_capacity(self) -> None:
        """「iPhone 15 128G」不能粘成 15128G（真实回归：容量丢了、频率还被算成 1512）。"""
        self.assertEqual(repair_spaced_numbers("iPhone 15 128G"), "iPhone 15 128G")
        self.assertEqual(parse_title_capacity("iPhone 15 128G")[0], 128)
        self.assertEqual(build_spec("iPhone 15 128G").frequency_mhz, 0)

    def test_does_not_merge_model_suffix(self) -> None:
        """「DDR3 8GB」的 3 属于 DDR3，合并会算成 38GB（真实踩过的坑）。"""
        self.assertIn("DDR3 8GB", repair_spaced_numbers("光威DDR3 8GB内存条"))
        self.assertNotIn("38GB", repair_spaced_numbers("光威DDR3 8GB内存条"))


class TestCapacityParsing(unittest.TestCase):
    """容量要算出来，不能只认字面。"""

    def test_various_forms(self) -> None:
        cases = {
            "光威 DDR4 3200 16G(8*2)套条": 16,
            "光威 DDR4 3200 8G×2 白色马甲": 16,
            "光威 DDR4 3200 8Gx4 四根都是套条，一共 32 G": 32,
            "光威 DDR4 3200 16G*2 套条": 32,
            "光威天策 DDR4 3200 16G×2 套装，两条一起出，16GX2共32GB价格1288": 32,
            "出自用光威战将笔记本内存条64G（32G*2）": 64,
            "光威 DDR4 3200 32GB 64GB 内存条 单条32G 两根32Gx2 64G": 64,
            "自用光威天策DDR4 16G 3200mhz，单根16G，四 根共64G": 64,
            "光威64G DDR4 3200 32G内存条套装": 64,
            "光威 GLOWAY DDR4 2666 16GB*4内存条": 64,
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(parse_title_capacity(title)[0], expected)

    def test_no_capacity_returns_zero(self) -> None:
        self.assertEqual(parse_title_capacity("一些光威内存，需要的联系，量大优惠")[0], 0)

    def test_frequency_is_not_capacity(self) -> None:
        """3200MHz / 16GHz 都不是容量。"""
        self.assertEqual(parse_title_capacity("光威 DDR4 3200MHz 内存条")[0], 0)
        self.assertEqual(parse_title_capacity("光威 DDR3 8GB 1600MHz 台式机内存条")[0], 8)

    def test_evidence_is_reported(self) -> None:
        """判定要能说出"凭什么"——证据片段随结果一起回传。"""
        total, evidence = parse_title_capacity("光威 DDR4 3200 8G×2")
        self.assertEqual(total, 16)
        self.assertTrue(any("8G×2" in item for item in evidence))


class TestSpecParsing(unittest.TestCase):
    """关键词 -> 规格。"""

    def test_guangwei_keyword(self) -> None:
        spec = build_spec("光威 3200 64G")
        self.assertEqual(spec.brand, "光威")
        self.assertEqual(spec.frequency_mhz, 3200)
        self.assertEqual(spec.capacity_gb, 64)
        self.assertEqual(spec.generation, "")
        self.assertFalse(spec.empty)

    def test_plain_word_keyword_has_no_spec(self) -> None:
        """纯文字关键词不产生规格约束（回归：「Switch OLED」的标题常只写 Switch）。"""
        self.assertTrue(build_spec("Switch OLED").empty)
        self.assertTrue(build_spec("").empty)

    def test_model_number_and_brand(self) -> None:
        spec = build_spec("4080S 32G")
        self.assertEqual(spec.brand, "")
        self.assertEqual(spec.frequency_mhz, 4080)
        self.assertEqual(spec.capacity_gb, 32)
        self.assertEqual(build_spec("iPhone 15 128G").brand, "苹果")
        self.assertEqual(build_spec("iPhone 15 128G").capacity_gb, 128)
        self.assertEqual(build_spec("光威 DDR4 3200 64G").generation, "DDR4")

    def test_capacity_token_helper(self) -> None:
        self.assertTrue(is_capacity_token("64G"))
        self.assertTrue(is_capacity_token("32gb"))
        self.assertFalse(is_capacity_token("DDR4"))
        self.assertFalse(is_capacity_token("3200"))


class TestModuleParsing(unittest.TestCase):
    """单条容量 / 条数解析：真实用途是「64G 其实想买两根 32G」。"""

    def test_explicit_pairs(self) -> None:
        cases = {
            "光威天策 DDR4 3200 32G×2 套条": (32, 2),
            "光威GLOWY DDR4 3200 32Gx2内存条": (32, 2),
            "光威 DDR4 3200 16G*2 套装": (16, 2),
            "光威 DDR4 3200 8Gx4 四根都是套条": (8, 4),
            "光威 GLOWAY 2×32G 套条": (32, 2),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                info = parse_title_modules(title)
                self.assertEqual((info.max_module_gb, info.stated_count), expected)

    def test_pairs_beat_bare_total(self) -> None:
        """「32G（16G×2）」的单条是 16G —— 裸容量 32G 是**总容量**，不能当单条。"""
        info = parse_title_modules("光威天策 DDR4 3200 32G（16G×2）星空黑内存条")
        self.assertEqual(info.max_module_gb, 16)
        self.assertEqual(info.stated_count, 2)

    def test_total_marker_is_not_a_module(self) -> None:
        """「单根16G，四根共64G」：64G 是总量，单条是 16G、条数是 4。"""
        info = parse_title_modules("自用光威天策DDR4 16G 3200mhz，单根16G，四 根共64G")
        self.assertEqual(info.max_module_gb, 16)
        self.assertEqual(info.stated_count, 4)

    def test_spoken_counts(self) -> None:
        self.assertEqual(parse_title_modules("光威 DDR4 3200 32G 内存条 两条一起出").stated_count, 2)
        self.assertEqual(parse_title_modules("光威 DDR4 3200 32G 内存条 单条出").stated_count, 1)
        self.assertIsNone(parse_title_modules("光威 DDR4 3200 32G 内存条").stated_count)

    def test_no_capacity_at_all(self) -> None:
        info = parse_title_modules("一些光威内存，需要的联系")
        self.assertEqual(info.max_module_gb, 0)
        self.assertIsNone(info.stated_count)


class TestTwoBy32Intent(unittest.TestCase):
    """用户真实意图：「我写 64G 是想买两根 32G」。

    语料是线上真实标题（含被误报的 16G×2 / 16G×4 与竞品堆砌）。
    """

    def setUp(self) -> None:
        self.spec = build_spec("光威 3200 32G×2")

    def test_spec_parses_module_structure(self) -> None:
        self.assertEqual(self.spec.module_gb, 32)
        self.assertEqual(self.spec.module_count, 2)
        self.assertEqual(self.spec.capacity_gb, 64)
        self.assertIn("单条 ≥32G×2", self.spec.summary())

    def test_true_two_by_32_passes(self) -> None:
        titles = [
            "光威天策 GLOWY DDR4 3200 32GB 64GB 内存条 镁光颗粒 单条32G 两根32Gx2 64G 2100打包",
            "刚买的就卖，俩条价出光威GLOWAY DDR4 3200 3 2GB×2笔记本内存条",
            "出自用光威战将笔记本内存条64G（32G*2）32G+DDR 4+3200MT/s",
            "光威GLOWY DDR4 3200 。32Gx2内存条，共两条，可组双通道64G。",
            "99新光威64G ddr4-3200 32G*2内存，顺丰包邮",
            "光威DDR4 64G（32Gx2）套装，型号DDR4 320 0 U-DIMM，白色马甲",
            "光威天策DDR4 3200 32G×2（64g）内存条，白色马甲台式机条",
        ]
        for title in titles:
            with self.subTest(title=title[:24]):
                self.assertTrue(match_spec(title, self.spec).passed)

    def test_sixteen_gig_kits_rejected(self) -> None:
        """这正是用户抱怨的那类：搜 32G/64G 时冒出来的「2 根 16G」。"""
        cases = {
            "光威天策 DDR4 3200 32G（16G×2）星空黑内存条": "spec_capacity",
            "光威 GLOWY DDR4 3200 16G×2 白色马甲内存套装32G": "spec_capacity",
            "自用光威天策DDR4 16G 3200mhz，单根16G，四 根共64G": "spec_module",
        }
        for title, reason in cases.items():
            with self.subTest(title=title[:24]):
                decision = match_spec(title, self.spec)
                self.assertFalse(decision.passed)
                self.assertEqual(decision.reason, reason)

    def test_single_stick_rejected_by_count(self) -> None:
        """标题广告 64G、正文「出一根」→ 条数不足。"""
        decision = match_spec("光威64G DDR4 3200 32G内存条套装 白色马甲 出一根出一根", self.spec)
        self.assertFalse(decision.passed)
        self.assertEqual(decision.reason, "spec_module_count")

    def test_competitor_still_rejected(self) -> None:
        decision = match_spec(
            "金士顿2666内存8gx2 金士顿2666内存16g 关联光威 芝奇 英睿达 海盗船", self.spec
        )
        self.assertEqual(decision.reason, "spec_brand")

    def test_plain_capacity_keyword_has_no_module_constraint(self) -> None:
        """关键词没写组合（只有 64G）时不额外约束单条 —— 行为与 v1.11.0 一致。"""
        self.assertEqual(build_spec("光威 3200 64G").module_gb, 0)


class TestSpecMatching(unittest.TestCase):
    """规格判定：品牌锚定 / 代际 / 频率 / 容量。"""

    def setUp(self) -> None:
        self.spec = build_spec("光威 3200 64G")

    def test_rejects_wrong_capacity(self) -> None:
        decision = match_spec("光威天策 8g ddr4 3200 单条 长鑫颗粒 CL16 铝马甲", self.spec)
        self.assertFalse(decision.passed)
        self.assertEqual(decision.reason, "spec_capacity")

    def test_rejects_wrong_frequency(self) -> None:
        decision = match_spec("光威GLOWAY DDR4 2666 16GB内存条", self.spec)
        self.assertEqual(decision.reason, "spec_frequency")

    def test_accepts_equivalent_capacity_forms(self) -> None:
        for title in (
            "刚买的就卖，俩条价出光威GLOWAY DDR4 3200 3 2GB×2笔记本内存条",
            "出自用光威战将笔记本内存条64G（32G*2）DDR4 3200MT/s",
            "光威天策 DDR4 3200 32G×2 套装 共64G",
        ):
            with self.subTest(title=title):
                self.assertTrue(match_spec(title, self.spec).passed)

    def test_brand_anchor_rejects_stuffed_titles(self) -> None:
        """关键词堆砌：竞品标题在尾部堆一串「关联 光威 芝奇…」蹭搜索。"""
        stuffed = [
            "金士顿2666内存8gx2 正常使用刚拆下来 金士顿2666内存ddr4 金士顿2666内存16g 成色如图 关联光威 芝奇 英睿达 海盗船 威刚",
            "酷兽 DDR4 3200 32G 笔记本内存条 两条一起出 共64G 可组双通道 光威",
            "美商海盗船VENGEANCE LPX DDR4 3200 64G（32G×2） 光威16g 3000频率出",
        ]
        for title in stuffed:
            with self.subTest(title=title[:20]):
                decision = match_spec(title, self.spec)
                self.assertFalse(decision.passed)
                self.assertEqual(decision.reason, "spec_brand")

    def test_brand_anchor_accepts_normal_titles(self) -> None:
        for title in (
            "光威 DDR4 3200 64G 内存条",
            "出自用光威战将笔记本内存条64G（32G*2）DDR4 3200MT/s",
            "闲置出 光威天策DDR4 3200 32G×2 共64G",
        ):
            with self.subTest(title=title):
                self.assertTrue(match_spec(title, self.spec).passed)

    def test_empty_spec_always_passes(self) -> None:
        decision = match_spec("任意标题", build_spec("Switch OLED"))
        self.assertTrue(decision.passed)
        self.assertEqual(decision.reason, "ok")

    def test_generation_requirement(self) -> None:
        spec = build_spec("光威 DDR4 3200 64G")
        self.assertEqual(match_spec("光威 DDR5 6400 64G", spec).reason, "spec_generation")

    def test_detail_is_human_readable(self) -> None:
        decision = match_spec("光威天策 8g ddr4 3200 单条", self.spec)
        self.assertIn("64G", decision.detail)


class TestRequiredKeywordsDefault(unittest.TestCase):
    """自动必含词：规格过滤开启时剔除容量 token（否则会误杀 32G×2=64G）。"""

    def test_spec_filter_on_drops_capacity_token(self) -> None:
        self.assertEqual(required_keywords_default("光威 笔记本DDR4 3200 16G", True), ["DDR4", "3200"])

    def test_spec_filter_off_keeps_legacy_behaviour(self) -> None:
        self.assertEqual(
            required_keywords_default("光威 笔记本DDR4 3200 16G", False), ["DDR4", "3200", "16G"]
        )

    def test_no_capacity_token_is_unchanged(self) -> None:
        self.assertEqual(required_keywords_default("4080S", True), ["4080S"])


# -*- coding: utf-8 -*-
#: 线上真实语料：2026-10-10 从 NAS Web UI 的 /api/records 导出的
#: 关键词「光威 3200 64G」全部 71 条命中标题（仅标题，不含任何账号信息）。
#: 每个元组为 (标题, 是否应当命中)；期望值由人工审阅逐条判定：
#:     应当命中 = 光威品牌 + DDR4-3200 + 总容量 ≥64G（广告容量也算）；
#:     其余一律不应命中（容量不足 / 频率不对 / 竞品或堆砌尾巴）。
CORPUS: list[tuple[str, bool]] = [
    # ---- 期望命中（5 条，人工判定）----
    ('刚买的就卖，俩条价出光威GLOWAY DDR4 3200 3 2GB×2笔记本内存条，SO-DIMM接口，型号VGM4SX32C22BG-SWANN，时序CL22-22-22-52，1.2V，双面颗粒，标签在，金手指干净，功能正常，插上即用，兼容主流笔记本，升级扩容没问题。自用升级淘汰，成色几乎全新，外观很新。支持自提，不议价，需要细节图私聊。', True),
    ('光威天策 GLOWY DDR4 3200 32GB 64GB 内存条 镁光颗粒 单条32G 两根32Gx2 64G 2100打包时序CL18-22-22-42，1.35V电压外观保存很好，金手指干净，无拆无修，功能正常。友情提示：暂不支持无理由退换货，有问题随时联系。', True),
    ('出自用光威战将笔记本内存条64G（32G*2）32G+DDR 4+3200MT/s，京东自营25年8月购入，终生质保。换台式机了，功能完好，包装全，无维修。  SO-DIMM接口，1.2V低电压，时序CL22，双面颗粒，插上即用，兼容主流笔记本，升级扩容很合适。标签都在，金手指干净，外观很新。  包邮...', True),
    ('自用光威天策DDR4 16G 3200mhz，单根16G，四 根共64G，京东自营购买，箱说全，只出本地，聊合适我给你送货。', True),
    ('光威64G DDR4 3200 32G内存条套装 白色马甲 时序CL18-22-22-42 1.35V 单面颗粒 支持intel和AMD主板 插上即用 性能稳定，出一根出一根，赣州市本地自提', True),  # 标题广告 64G，但正文「32G内存条套装…出一根」→ 从严仍按命中（容量以广告为准）
    # ---- 期望不命中（66 条：容量不足 / 频率不对 / 竞品…）----
    ('光威天策弈 8g ddr4 3200 单条 长鑫颗粒 CL16 铝马甲 台式机用 24年5月淘宝买的 性能稳 兼容好 包邮', False),
    ('出此电脑  单主机 1800 包邮  另27寸熊猫显示器+3 00i3-12100F 四核  光威16g 8*2 DDR4 3200MHz  昂达显卡GTX1060 5G  昂达H610E-B主板 512G固态m.2 电源 爱国者650额定500电脑年初才买的  显卡是以19年买的一手自用无拆无修', False),
    ('3600mhz枭鲸 DDR4内存条 8G×2共16G，刚拆机 ，成色超新，功能正常，包点亮包使用。支持intel和AMD主板。时序cl18。  标价=两条包邮！！喜欢直接拍！！！当天极速发货，不议价，主页还有其他D4  #主机硬件 #台式机内存条 #童装童鞋超值惠   关联金百达，光威，玖合ddr4内...', False),
    ('光威DDR3 8GB内存条 光威GLOWAY DDR3 8GB 1600MHz台式机内存条，型号WAR3U1600C11081C，双面颗粒，1.5V电压，U-DIMM规格，兼容Intel和AMD主板，插上即用。金手指无氧化，芯片与标签清晰完整。', False),
    ('光威DDR3 1600 8GB内存条 光威GLOWAY DDR3 1600MHz 8GB台式机内存条，双面颗粒，U-DIMM规格，兼容Intel和AMD主板，金手指干净，功能正常，插上即用。', False),
    ('光威D4  8G 3200 感兴趣的话点“我想要”和我私聊吧～', False),
    ('光威天策DDR4 3200 8G×2 白色马甲内存条 两条一起出，共16G，可组双通道。 型号TAC4U3200E18082C，时序CL18-22-22-42，电压1.35V，PC4-25600。 支持XMP一键超频，长鑫颗粒批次，可超3600兼容AMD/Intel主板，台式机升级扩容、办公...', False),
    ('闲置出 光威天策DDR4 3200 16G内存条，皓月白马甲 款，单根16G，原包装未拆，正品保证。支持intel和AMD主板，插上即用，性能稳定，兼容性好，装机升级都合适。', False),
    ('金士顿2666内存8gx2 正常使用刚拆下来 金士顿2666内存ddr4 金士顿2666内存16g  成色如图 马甲掉色了 包邮               关联金士顿芝奇英睿达海盗船威刚影驰科赋宏碁雷克沙阿斯加特光威七彩虹金百达ddr4 3200 3600 4000 c14 c15...', False),  # 竞品标题，尾部堆砌「关联…」骗搜索 → 必须挡掉
    ('光威天策 DDR5 24G 5600 台式机内存条，白色单条 ，淘宝1534全新未拆封还能开发票七天无理由 这个价格是没有国补的价格，也就是随便都能买的到的，之前有一千四百多的国补价，不过现在貌似已经下架或者涨价 那些挂的跟新的一样价格的二手货，甚至有些挂的比新的价格还贵的，你说搞不搞笑？纯纯传家宝，...', False),
    ('光威DDR5 16GB内存条光威DDR5 6000MHz 1 6GB单条内存，海力士M-Die颗粒，时序CL30，电压1.1V，金手指干净，无拆无修，适合台式机升级使用。', False),
    ('光威天策DDR5 6400 16G内存条 型号VGM5U64C48AG-DTACWN 时序CL48 1.4V 支持XMP 白色马甲 无灯 纯白配色 成色很新 功能正常 兼容AMD/Intel平台', False),
    ('光威16g3200hz ddr4 内存成色新 需要的联系 感兴趣的话点“我想要”和我私聊吧～', False),
    ('光威天策弈系列内存条 8G一条 3200MHz 频率 带铝制 马甲 散热好 性能稳 长鑫颗粒 延迟14 电压1.4V 台式机用 24年1月淘宝买的 买来没怎么用 99新 包邮 支持自提 价格可聊 喜欢直接拍 有问题随时问', False),
    ('光威 DDR4 3200 32GB C18白色 拆机件 成色还可以 有点使用痕迹 金手指干净 标签都在 功能正常 插上即用 性能稳定 支持intel/amd主板 台式机升级扩容用 兼容性好 时序CL18-22-22-42 电压1.35V', False),
    ('光威16g3200hz ddr4 内存成色新 需要的联系 感兴趣的话点“我想要”和我私聊吧～', False),
    ('光威 DDR4 3200 8Gx4 四根都是套条，一共 32 G。功能一切正常，成色如图，其中一根马甲上有些许划痕，不影响使用。 江浙沪包邮', False),
    ('一些光威内存，需要的联系，标价为8g单根，量大优惠 感兴趣的话点“我想要”和我私聊吧～', False),
    ('光威DDR4 3200 8GB内存条，白色马甲，单条8GB， 时序CL16-20-20-40，电压1.35V，支持AMD和Intel主板，适合台式机升级扩容，插上即用。成色还可以，金手指干净，标签清晰，功能正常。', False),
    ('光威天策 DDR5 6000 16G 单条 京东自营购入，正品行货，带购买记录 精选颗粒，CL46时序，XMP3.0， 适合AI电脑升级 包邮，不刀，售出不退 需要的直接拍，细节私聊', False),
    ('光威8G 3000  台式机拆机内存 实物拍照、成色如图、没有任何质量问题  一起有2根   标价为一根                 保修：店保7天      7天以后出问题自理！！！ 成都支持自提闪送(自费）  全国非偏远地区包邮 西藏，海南，新疆，内蒙，甘孜州，阿坝州...', False),
    ('内存条 ddr4 光威天策3200hz  16G容量 英睿达2666hz      8G容量 光威几乎未使用  23年买的 英睿达用了两年   20年买的 两根齐出', False),
    ('自用光威 天策16G*2 DDR4 3200MHz连号套条 白色 时序CL18 22 22 42 自己装机用 功能一切正常没毛病，金手指干净无氧化   可自提 非偏远地区包邮 不接受到手刀', False),
    ('光威GLOWY+DDR4+3200+16G×2套装，白色马甲 款，型号TAC4U3600E18162C，3200频率，SSTL+1.2V电压，U-DIMM接口，台式机专用，双通道配置，兼容intel和AMD主板，插上即用，性能稳定，办公游戏扩容都没问题  外观很新，金手指干净，标签都在，保存一直很好，...', False),
    ('【光威DDR3 4G台式机内存条】功能正常，成色还可以，适合 老电脑升级扩容。  个人闲置，两条一起出，可小刀～   支持邮寄，不包邮。下单前建议确认自己主板支持DDR3，二手闲置售出不退不换。', False),
    ('光威GLOWAY DDR4 4000 8G×2 内存条 白色马甲，时序CL15-15-15-28   三星B代颗粒，特挑颗粒 bcpb   XMP时序3600C14-15-15-38 金手指干净，标签清晰   功能正常，无拆无修，插上即用   支持AMD和Intel主板，建议双通道使用 大...', False),
    ('光威GLOWY DDR4 3600 8G×2 白色马甲套条 时序CL18-22-22-42 1.35V 支持intel/amd主板 插上即用 成色几乎全新 外观很新 金手指干净 无拆修 功能正常', False),
    ('光威GLOWY DDR4 3200 8GB内存条 白色马甲 成色还可以 有点使用痕迹 金手指干净 标签都在 功能正常 无拆无修 包邮 支持自提', False),
    ('光威天策DDR5 4800c40 16G一条 刚拆机 发货之 前拍点亮视频 兼容性问题不包退换谢谢', False),
    ('光威内存条，京东买的8G一条，两条16G一起出，全新没开封没 用过，型号天策DDR4 3200，皓月白，支持AMD和intel主板，插上即用，给有需要的人，不包邮，可自提。', False),
    ('光威ddr4 32g 3200带马甲内存条（未拆封）单条 1 200元，有2条， JD1400', False),
    ('海盗船VENGEANCE LPX 型号CM4X16GD3200 32g 黑色马甲 32G一共4条 单条出也行 成色还可以 有点使用痕迹 金手指干净 光威16gddr4 3000 四条 价格需要的来 乱出价的不回 功能正常 无拆无修 不包邮 支持自提 价格可聊 喜欢直接拍 ...', False),
    ('光威GLOWY DDR4 3200 16GB内存条，白色马甲 ，型号TAC4U3200E18161C，时序CL18-22-22-42，1.35V电压，单条16G，功能正常，成色几乎全新，外观干净。  支持AMD/Intel主板，插上即用，性能稳定，兼容性好，办公游戏都能用。无拆无修，无暗病，包邮，支持...', False),
    ('美商海盗船VENGEANCE LPX DDR4 3200 6 4G（32G×2） 一共4条 128g 价格是单条价格 伸手占便宜的别来 光威16g 3000频率出 价 支持AMD/Intel主板 插上即用 性能稳定 适合台式机升级 可自提不包邮', False),  # 竞品标题（海盗船），尾部挂光威 → 必须挡掉
    ('光威内存条 DDR5 16G 4800MT/S C40 功能 正常 上机即用  官方保内的可官方质保,过保的店保7天, 可自行下单，到店自提，商品实物拍摄， 所见即所得。 各种精品中高端电脑配件，原装正品，二手非全新，吹毛求疵者请绕道，可整机组装私聊', False),
    ('光威 GLOWY DDR4 3200 32GB内存条 白色马甲 UDIMM 时序CL18-22-22-42 电压1.35V 几乎全新 ，如图，有意私聊', False),
    ('光威天策 DDR4 3200 8G×2白色马甲内存套装，成色 几乎全新，外观很新，金手指干净，功能正常，插上就能用。支持AMD和Intel主板，办公、游戏都没问题。包邮，支持合肥自提。', False),
    ('GLOWAY 光威天策 8GB DDR4 3200MT/s UDIMM  本来想买来打游戏用，结果临时有事长时间不在家电脑也玩不成了，就把内存条低价出售了。', False),
    ('光威天策 32G 4800 16G×2内存套装，黑色马甲，金 手指干净，无拆无修，功能正常，具体看图。痛快拍下包邮', False),
    ('光威天策 32G 4800 16G×2内存套装，黑色马甲，金 手指干净，无拆无修，功能正常，具体看图。痛快拍下包邮', False),
    ('光威天策 32G 4800 16G×2内存套装，黑色马甲，金 手指干净，无拆无修，功能正常，具体看图。痛快拍下包邮', False),
    ('光威（Gloway）16GB(8GBx2) DDR4  32 00 台式机内存条 天策 马甲条 精选颗粒 CL18 皓月白 AI电脑配件升级', False),
    ('出光威天策白色内存条， 6000c36 台式机电脑主机用 （标价是一根的价格，） 图片实拍，白色散热马甲，金手指干净  具体型号/容量/频率可以私聊我看细节 自用闲置，适合升级扩容   仅支持自提', False),
    ('光威Gloway DDR4 16g8×2内存条，3200MH z，京东自营旗舰店购入，有购买记录，正品，功能正常，兼容性好，插上就能用，打游戏、办公都没问题，成色包好就用三个多月，还很少玩游戏，支持自提，喜欢直接拍，细节私聊～', False),
    ('光威GLOWAY DDR4 8G 3000MHz内存条，天策 系列，带马甲，原包装在。插上即用。单根出，包邮，可自提。', False),
    ('光威   笔记本内存条  ddr4  2666  16g 没怎么用过  单条价格  包邮', False),
    ('光威GLOWAY DDR4 16G 2666MHz台式机内存 条，型号TYA4U2666D19161C，原包装在，成色几乎全新，金手指干净，功能正常，插上即用 包邮，喜欢直接拍，细节私聊', False),
    ('光威悍将ddr4 16gb 2666全新 感兴趣的话点“我想要”和我私聊吧～', False),
    ('光威天策 DDR5 24GB 5600MHz 皓月白马甲条 全新未拆封  适合 9600x，9700x 以及一众x3d的cpu使用，amd不吃高频，结合上大三缓，网游性能差距和6000 c28大概在5%-8%（我自己实测）  对比动辄2000+的24g单条，这条子适合买回去过渡，打游戏开直播够用，...', False),
    ('光威天策DDR43200 16x2，全新未拆封 感兴趣的话点“我想要”和我私聊吧～', False),
    ('未拆封； 2根光威16g内存条ddr4；CL-22-22-22-52   长鑫颗粒；塑封膜都没拆；标价是两根价格，单根/双根出售均可；物品是未拆封寄出后不退不换，秒拍不发货，看好再拍！', False),
    ('光威天策DDR4 8GB内存条光威天策8GB DDR4 32 00MHz内存条，带散热马甲，原包装塑封完整，标签清晰，金手指无氧化痕迹，插主板能亮机使用。可刀标价两根', False),
    ('光威GLOWAY DDR4 2666 8GB笔记本内存条，S O-DIMM接口，PC4-21300，时序CL19-19-19-43，1.2V低电压。 适合笔记本升级扩容，Intel和AMD平台主流机型可用，戴尔、惠普、联想、华硕等常见品牌笔记本可匹配， 全新的 打包出', False),
    ('光威GLOWAY天煞DDR4 16G 3600MHz台式机内 存条，枪灰色马甲，单条16G，c18时序，型号VGM4UX36C18AG-STSAARI，Intel平台专用，原装未拆封在保，正品，性能稳定，插上即用，适合台式机升级扩容。本地自提，外地可邮寄，售出不退', False),
    ('光威DDR4 3200 16G(8*2)套条 成色如图，无磕碰，测试良好。 测试图在最后一张 外地包邮，支持自提，地址在广州新塘。', False),
    ('光威GLOWAY DDR4 3600 8G×2，白色马甲，三 星颗粒，时序CL18-22-22-42，1.35V，功能正常，金手指干净，标签都在，插上即用。成色有正常使用痕迹。当天确认返运费，宁波自提减10元。 连主板cpu打包1100元。', False),
    ('光威16G3200(8GX2)个人一手 感兴趣的话点“我想要”和我私聊吧～', False),
    ('光威天策 DDR4 3200 16G×2 白色马甲内存条套装 台式机内存， 两条一起出，16GX2共32GB价格1288 时序CL18-22-22-42，1.35V，支持XMP 支持intel/AMD主板，兼容性好，插上即用 成色很新，金手指干净，标签都在，功能正常，无拆无修 包邮，支持自提 喜欢直...', False),
    ('光威天策 DDR4 3600 8G×2白色套装 CL18时序 XMP支持 性能稳定 兼容intel/AMD主板 插上即用 精选颗粒 散热好 适合台式机升级扩容 游戏办公AI都能用 包邮 支持自提 价格可聊 喜欢直接拍 细节私聊', False),
    ('光威GLOWAY DDR4 3200 8G×2白色马甲内存条 ，时序CL18-22-22-42，1.35V，支持AMD/Intel主板，插上即用，性能稳定，办公游戏DIY都能用，成色几乎全新，金手指干净，无拆无修，健康度满分，南宁同城可自提，包邮非偏远地区，喜欢私聊', False),
    ('光威DDR4 16GB 2666MHz笔记本内存条  这不是 杂牌，不懂问豆包!都是G2304150序列号，内存颗粒都一致，仅PCB板颜色不一样标价单条，可以成对出，只有一对检测视频已上传，经过tm5压力测试，图吧，CPUz，aida64，飓风，5重检测!金手指干净，功能正常，兼容性好，但不对兼容性负责，无装机技术支持，售出不退不换，谨慎购买!上热门:三星 海力士 镁光 D3 16G 32G 16', False),
    ('光威 GLOWAY DDR4 2666MT/s 4GB 台式 机内存条，型号 STK4U2666D19041C，时序 CL18-18-18-43，1.2V 电压，黑色马甲，原包装拆封拍照，金手指干净，标签完整，外观全新。数量就这一根，单根出。售后换回的条子，260326 生产的吧，现在用不上了，就拆开...', False),
    ('光威GLOWAY DDR4 2666 16GB*4内存条，带 马甲，1.2V电压，XMP 2.0，时序19-19-19-43，支持intel和AMD主板，插上即用，兼容性好，具体参数见图。  成色几乎全新，外观干净，金手指无损，功能正常，无拆无修。自用升级闲置出。  二手物品，发货前需要什么资料都可以...', False),
    ('32g 3200 宇瞻ddr4 16g x2，台湾工业品牌， 工控机拆机，稳定性一流，功能一切正常标价单条，打包包邮关联 镁光c9 三星特挑bdie 海力士cjr djr  英睿达 威刚 金士顿 芝奇 海盗船 金百达 光威 阿斯加特 长鑫 ddr4 ddr5 4g 8g 16g 32g 64g 2133 2400 2666 3000 3200 3600 4000', False),
    ('32g 3200 宇瞻ddr4 16g x2，台湾工业品牌， 工控机拆机，稳定性一流，功能一切正常标价单条，打包包邮关联 镁光c9 三星特挑bdie 海力士cjr djr  英睿达 威刚 金士顿 芝奇 海盗船 金百达 光威 阿斯加特 长鑫 ddr4 ddr5 4g 8g 16g 32g 64g 2133 2400 2666 3000 3200 3600 4000', False),
    ('酷兽 DDR4 3200 32G 笔记本内存条 两条一起出 共64G 可组双通道 型号 CS4S3200D22161C SO-DIMM接口 1.2V电压 CL22时序 PC4-25600 光威旗下品牌 嘉合劲威出品 正品带防伪码 成色几乎全新 功能正常 插上即用 兼容Intel和AMD...', False),  # 竞品标题（酷兽），尾部挂光威 → 必须挡掉
]


class TestRealCorpusRegression(unittest.TestCase):
    """真实语料回归：71 条线上标题逐条验收。"""

    def test_corpus_expectations(self) -> None:
        spec = build_spec("光威 3200 64G")
        wrong: list[str] = []
        for title, expected in CORPUS:
            decision = match_spec(title, spec)
            if decision.passed != expected:
                wrong.append(f"{'漏放' if expected else '误放'}：{title[:60]}（{decision.reason}）")
        self.assertEqual(wrong, [], "规格过滤与人工判定不一致：\n" + "\n".join(wrong))

    def test_corpus_is_representative(self) -> None:
        """语料不能退化成"全是负样本"：必须含正样本，且总数符合线上快照。"""
        positives = [title for title, expected in CORPUS if expected]
        self.assertGreaterEqual(len(positives), 3)
        self.assertEqual(len(CORPUS), 71)

    def test_corpus_under_module_spec_keeps_only_true_two_by_32(self) -> None:
        """同一批线上语料加上「两根 32G」规格：16G×4 与「出一根」都被挡掉。"""
        spec = build_spec("光威 3200 32G×2")
        passed = [title for title, expected in CORPUS if expected and match_spec(title, spec).passed]
        self.assertEqual(len(passed), 3, "只应留下真正是 2×32G 的那 3 条")

    def test_naive_literal_matching_would_miss_the_true_64g(self) -> None:
        """反证：字面必含 "64G" 会漏掉「32G×2」这种真 64G（本模块存在的理由）。"""
        title = "刚买的就卖，俩条价出光威GLOWAY DDR4 3200 3 2GB×2笔记本内存条"
        self.assertFalse(matches_required_keywords(title, ["光威", "3200", "64G"]))
        self.assertTrue(match_spec(title, build_spec("光威 3200 64G")).passed)


if __name__ == "__main__":
    unittest.main()
