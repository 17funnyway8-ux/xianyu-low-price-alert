# -*- coding: utf-8 -*-
"""【逆向还原骨架】xianyu_price_alert.py

================================ 还原说明 ================================
来源     : C:\\Users\\fun\\Desktop\\闲鱼低价提醒工具V1.0.exe
           → PyInstaller CArchive 顶层 `xianyu_price_alert.pyc` (46,315 B)
co_filename : 'xianyu_price_alert.py'
运行时   : CPython 3.13 (python313.dll)
还原方式 : 与目标同版本(3.13)的 marshal.loads 读出 code object，
           dis.dis() 全量反汇编 + co_consts/co_names/co_varnames 结构化提取，
           据此手工还原等价源码。**未使用反编译器**（3.13 尚无可用反编译器）。
证据文件 : re_analysis/disasm/xianyu_price_alert.txt
           re_analysis/evidence/struct_xianyu_price_alert.md

可信度约定：
  - 无标注         → 由反汇编逐条指令直接确定，等价性高（可视为确证）
  - `# [不确定]`   → 反汇编可见但控制流/细节存在多种等价写法，语义推断
行号注释 `# L<n>` 对应原始源码行号（来自 code object 的 co_firstlineno / 行号表）。
=========================================================================
"""

from __future__ import annotations

# ---- L21~L33 模块导入（IMPORT_NAME 序列，顺序即原始顺序） ----
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import requests

# =========================================================================
# L39~L50 路径与常量
# =========================================================================

# L39-L42：冻结态取 exe 所在目录，否则取脚本所在目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resource_path(rel):
    """获取打包资源（如图标）的真实路径：冻结时从 _MEIPASS 取，否则从源码目录取。

    L45-L50。docstring 为 co_consts 原文。
    """
    # [不确定] 具体分支写法；反汇编可见 getattr(sys, 'frozen', False) 判断
    # 与 sys._MEIPASS / BASE_DIR 两条路径的 os.path.join(base, rel)
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), rel)
    return os.path.join(BASE_DIR, rel)


# L52-L54：三个数据文件均落在 BASE_DIR（= exe 同目录）
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen_items.json")
ALERT_LOG = os.path.join(BASE_DIR, "alerts.jsonl")

# L56-L62：mtop 接口常量（co_consts 原文，确证）
API_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
APP_KEY = "34839810"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
)
VERSION = "1.0.0"
AUTHOR = "花开半夏"

# L64-L76：默认配置（BUILD_CONST_KEY_MAP 11 项，键序与值逐一对应，确证）
DEFAULT_CONFIG = {
    "keyword": "iPhone 13",
    "max_price": 3000.0,
    "min_price": 0.0,
    "interval_minutes": 10,
    "pages": 1,
    "page_size": 30,
    "cookie": "",
    "sound": True,
    "toast": True,
    "bark_url": "",
    "webhook_url": "",
}


# =========================================================================
# L82~L110 工具函数
# =========================================================================

def dig(d, *keys, default=None):
    """安全地从嵌套字典里取值。

    L82-L91。签名由 co_varnames + SET_FUNCTION_ATTRIBUTE(kwdefaults) 确定。
    """
    # [不确定] 循环体细节；语义为逐层 dict.get，遇非 dict 或缺键返回 default
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        if k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


def parse_price(text) -> Optional[float]:
    """把闲鱼价格文本转成 float，处理 ￥、逗号、万 等。无法识别返回 None。

    L93-L110。常量表：'￥','¥',',',' ',('面议','电议','私聊','咨询'),'万',10000
    """
    if not text:
        return None
    s = str(text)
    # 去掉货币符号、千分位逗号与空格（LOAD_CONST '￥'/'¥'/','/' ' + replace）
    s = s.replace("￥", "").replace("¥", "").replace(",", "").replace(" ", "")
    # 非价格文案直接放弃
    if any(w in s for w in ("面议", "电议", "私聊", "咨询")):  # L102 <genexpr>
        return None
    mult = 1
    if "万" in s:
        mult = 10000
        s = s.replace("万", "")
    try:
        return float(s) * mult
    except Exception:  # [不确定] 异常类型，反汇编中为宽泛捕获
        return None


# =========================================================================
# L112~L125 数据模型
# =========================================================================

@dataclass
class Item:
    """搜索结果条目。

    L112-L125。字段名与顺序取自 Item 的 co_consts:
    ('item_id','title','price','price_text','url','location','seller','image')
    dataclass 装饰器无参（LOAD_NAME dataclass → CALL 0）。
    """

    item_id: str
    title: str
    price: Optional[float]
    price_text: str
    url: str
    location: str
    seller: str
    image: str
    # [不确定] 各字段是否有默认值；co_consts 末尾出现 `()` 与 `None`，
    # 推测部分字段带默认值（如 image=""），但不影响接口语义。


# =========================================================================
# L127~L241 闲鱼客户端
# =========================================================================

class XianyuClient:
    """闲鱼 mtop 搜索客户端。L127-L241。"""

    def __init__(self, cookie):
        """L128-L130。仅两行：保存 cookie 并抽取 token。"""
        self.cookie = cookie
        self.token = self._extract_token(cookie)

    @staticmethod
    def _extract_token(cookie):
        """从 Cookie 里取 _m_h5_tk 的下划线前半段。L132-L137。

        常量：正则 '_m_h5_tk=([a-zA-Z0-9]+)_'，group(1)。
        """
        m = re.search(r"_m_h5_tk=([a-zA-Z0-9]+)_", cookie)
        if not m:
            raise RuntimeError(
                "Cookie 中未找到 _m_h5_tk，请确认已登录闲鱼并复制完整 Cookie。"
            )
        return m.group(1)

    def _sign(self, timestamp, data_json):
        """mtop 签名算法。L139-L141。**逐指令确证，无推测**。

        反汇编（disasm/xianyu_price_alert.txt, CODE OBJECT `_sign`）：
            BUILD_STRING 7  ← token,'&',timestamp,'&',APP_KEY,'&',data_json
            hashlib.md5(raw.encode('utf-8')).hexdigest()
        """
        raw = f"{self.token}&{timestamp}&{APP_KEY}&{data_json}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _cookie_dict(cookie):
        """把 Cookie 头字符串拆成 dict。L143-L151。

        常量：';' 分割，'=' 按首个等号 split(…, 1)。
        """
        out = {}
        for part in cookie.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
        return out

    def search(self, keyword, page=1, page_size=30):
        """调用 mtop PC 搜索接口。L153-L200。默认值 (1, 30) 来自 co_consts。

        全部键名/值均为 BUILD_CONST_KEY_MAP 直读，确证。
        """
        # L154：毫秒时间戳字符串
        ts = str(int(time.time() * 1000))

        # L155-L169：业务参数（13 键，BUILD_CONST_KEY_MAP 13）
        data_obj = {
            "pageNumber": str(page),
            "keyword": keyword,
            "fromFilter": False,
            "rowsPerPage": page_size,
            "sortValue": "",
            "sortField": "",
            "customDistance": "",
            "gps": "",
            "propValueStr": "",
            "customGps": "",
            "searchReqFromPage": "pcSearch",
            "extraFilterValue": "",
            "userPositionJson": "",
        }

        # L170：紧凑 JSON，且必须与签名用的串**完全一致**
        data_json = json.dumps(data_obj, separators=(",", ":"), ensure_ascii=False)

        # L171
        sign = self._sign(ts, data_json)

        # L172-L185：query string（12 键，BUILD_CONST_KEY_MAP 12）
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": ts,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.taobao.idlemtopsearch.pc.search",
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.search.0.0",
            "spm_pre": "a21ybx.search.searchInput.0",
        }

        # L186-L191：请求头（4 键）
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.goofish.com",
            "Referer": "https://www.goofish.com/",
        }

        # L192-L198：POST，body 为 data=<data_json>，超时 20s
        resp = requests.post(
            API_URL,
            params=params,
            headers=headers,
            cookies=self._cookie_dict(self.cookie),
            data={"data": data_json},
            timeout=20,
        )

        # L200
        return self.parse_response(resp.json(), keyword)

    def parse_response(self, json_data, keyword):
        """解析 mtop 响应为 Item 列表。L202-L241。"""
        items = []  # L203

        # L204-L206：ret[0] 必须以 'SUCCESS' 开头
        ret = json_data.get("ret")
        if not (ret and str(ret[0]).startswith("SUCCESS")):
            raise RuntimeError(f"接口返回错误：{ret}")

        # L207：data.resultList
        result_list = dig(json_data, "data", "resultList", default=[])

        # L208-L241
        for entry in result_list:
            main = dig(entry, "data", "item", "main")
            if not main or not isinstance(main, dict):  # L210-L211
                continue

            ex = dig(main, "exContent", default={})            # L212
            click = dig(main, "clickParam", "args", default={})  # L213

            # L214：价格文本优先 clickParam.args.price，回退 exContent.price
            price_text = str(click.get("price") or ex.get("price") or "")
            price = parse_price(price_text)                     # L215

            # L216：itemId 三级回退
            item_id = str(
                ex.get("itemId") or click.get("itemId") or ex.get("id") or ""
            ).strip()

            # L217-L218：标题三级回退 + 兜底文案
            title = (
                ex.get("title")
                or dig(ex, "detailParams", "title")
                or click.get("title")
                or "未知标题"
            )

            location = ex.get("area", "")          # L219
            seller = ex.get("userNickName", "")    # L220
            image = ex.get("picUrl", "")           # L221 (回退键 'image')

            # L222+ ：详情页 URL；无 item_id 时回退到搜索页
            # [不确定] 拼接的确切分支，但常量确证为下面两个前缀
            url = (
                f"https://www.goofish.com/item/{item_id}"
                if item_id
                else f"https://www.goofish.com/search?q={keyword}"
            )

            items.append(
                Item(
                    item_id=item_id,
                    title=title,
                    price=price,
                    price_text=price_text,
                    url=url,
                    location=location,
                    seller=seller,
                    image=image,
                )
            )
        return items


# =========================================================================
# L243~L312 通知器
# =========================================================================

class Notifier:
    """多通道提醒。L243-L312。"""

    def __init__(self, config, log_fn=None):
        """L244-L246。"""
        self.config = config
        self.log_fn = log_fn

    def notify(self, item, keyword, threshold):
        """组装消息并分发到各通道。L248-L262。消息模板逐字符确证。"""
        # L249-L252：BUILD_STRING 12
        msg = (
            f"【闲鱼捡漏】{item.title}\n"
            f"价格：￥{item.price:.2f}（设定阈值 ￥{threshold:.2f}）\n"
            f"地区：{item.location}\u3000卖家：{item.seller}\n"
            f"链接：{item.url}"
        )

        self._log(item, keyword, threshold)                 # L253
        if self.config.get("toast", True):                  # L254
            self._toast("闲鱼低价提醒", msg)                 # L255
        if self.config.get("sound", True):                  # L256
            self._sound()                                   # L257
        if self.config.get("bark_url"):                     # L258
            self._bark(msg)                                 # L259
        if self.config.get("webhook_url"):                  # L260
            self._webhook(msg)                              # L261
        if self.log_fn:                                     # L262
            self.log_fn(msg)

    def _toast(self, title, msg):
        """Windows 桌面通知：win11toast 优先，plyer 兜底。L264-L273。

        反汇编确证：IMPORT_NAME win11toast (from ... import toast)
                    → asyncio.run(...)   ← 说明 win11toast.toast 走协程
                    except Exception → IMPORT_NAME plyer (from ... import notification)
                    → notification.notify(title=..., message=...)
        """
        try:
            from win11toast import toast
            # [不确定] asyncio.run 包裹的确切表达式，反汇编可见 asyncio.run(...)
            asyncio.run(toast(title, msg))
        except Exception:
            try:
                from plyer import notification
                notification.notify(title=title, message=msg)
            except Exception:
                pass

    def _sound(self):
        """两声蜂鸣。L275-L282。确证：1000Hz/350ms → sleep 0.12 → 1500Hz/350ms。"""
        try:
            import winsound
            winsound.Beep(1000, 350)
            time.sleep(0.12)
            winsound.Beep(1500, 350)
        except Exception:
            pass

    def _bark(self, msg):
        """Bark 推送（iOS）。L284-L289。"""
        try:
            # 确证：config['bark_url'].rstrip('/') + '/' + requests.utils.quote(msg)
            url = (
                self.config["bark_url"].rstrip("/")
                + "/"
                + requests.utils.quote(msg)
            )
            requests.get(url, timeout=10)
        except Exception:
            pass

    def _webhook(self, msg):
        """企业微信/钉钉风格 webhook。L291-L297。

        确证 payload：{"msgtype": "text", "text": {"content": msg}}
        注意用的是 `data=json.dumps(...)` 而非 `json=`。
        """
        try:
            requests.post(
                self.config["webhook_url"],
                data=json.dumps({"msgtype": "text", "text": {"content": msg}}),
                timeout=10,
            )
        except Exception:
            pass

    def _log(self, item, keyword, threshold):
        """追加一行 JSONL 到 alerts.jsonl。L299-L312。"""
        try:
            rec = asdict(item)                                    # L301
            rec["keyword"] = keyword                              # L302
            rec["threshold"] = threshold                          # L303
            rec["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # L304
            with open(ALERT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass


# =========================================================================
# L314~L356 持久化
# =========================================================================

def load_seen():
    """读取已提醒去重集合。L314-L322。失败一律返回空 set。"""
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    """写回去重集合。L324-L330。json.dump(list, ensure_ascii=False, indent=2)。"""
    try:
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def dedup_key(item):
    """去重键。L332-L335。确证。

    有 item_id 用 item_id；否则 f"{title}|{seller}|{price_text}"（BUILD_STRING 5）。
    """
    if item.item_id:
        return item.item_id
    return f"{item.title}|{item.seller}|{item.price_text}"


def load_config():
    """读取配置，缺失则先写默认配置；再用 DEFAULT_CONFIG 补齐缺失键。L338-L348。"""
    if not os.path.exists(CONFIG_PATH):                     # L339
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:  # L340
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)  # L341
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:     # L342
        cfg = json.load(f)                                  # L343
    for k, v in DEFAULT_CONFIG.items():                     # L345
        cfg.setdefault(k, v)                                # L346 [不确定] setdefault vs if-not-in
    return cfg


def save_config(config):
    """写回配置。L350-L356。"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# =========================================================================
# L358~L419 监控主循环
# =========================================================================

def monitor(config, log_fn=print, stop_event=None, once=False):
    """核心监控循环。L358-L419。控制流逐条指令还原，可信度高。"""
    if not config.get("cookie"):                                        # L359
        log_fn("未配置 Cookie，请先运行 --setup 或在 config.json 填写。")  # L360
        return                                                          # L361

    client = XianyuClient(config["cookie"])            # L362
    notifier = Notifier(config, log_fn=log_fn)         # L363
    seen = load_seen()                                 # L364

    keyword = config["keyword"]                                        # L365
    threshold = float(config["max_price"])                             # L366
    min_price = float(config.get("min_price", 0))                      # L367
    interval = max(1, int(config.get("interval_minutes", 10))) * 60    # L368 (秒)
    pages = max(1, int(config.get("pages", 1)))                        # L369
    page_size = int(config.get("page_size", 30))                       # L370

    log_fn(f"开始监控：关键词={keyword} 阈值≤￥{threshold} 间隔={interval // 60}分钟")  # L372

    while True:                                        # L373
        if stop_event and stop_event.is_set():         # L374
            log_fn("监控已停止。")                      # L375
            return                                     # L376
        try:                                           # L377
            all_items = []                             # L378
            for p in range(1, pages + 1):              # L379
                items = client.search(keyword, page=p, page_size=page_size)  # L380
                all_items.extend(items)                # L381
                if p < pages:                          # L382
                    time.sleep(2)                      # L383 翻页限速 2s

            found = 0                                  # L384
            for it in all_items:                       # L385
                key = dedup_key(it)                    # L386
                if key in seen:                        # L387-L388
                    continue
                if it.price is None:                   # L389-L390
                    continue
                if it.price > threshold:               # L391-L392
                    continue
                if it.price < min_price:               # L393-L394
                    continue
                notifier.notify(it, keyword, threshold)  # L395
                seen.add(key)                            # L396
                found += 1                               # L397

            save_seen(seen)                              # L398
            log_fn(
                f"[{datetime.now().strftime('%H:%M:%S')}] 本轮扫描 {len(all_items)} 条，"
                f"触发 {found} 条提醒（已记录 {len(seen)} 个去重商品）"
            )                                            # L399-L400
        except Exception as e:                           # L401 [不确定] 变量名 e 确证
            log_fn(f"[{datetime.now().strftime('%H:%M:%S')}] 出错：{e}")  # L402
            # [不确定] 出错后是否额外 sleep 5；co_consts 中存在常量 5

        if once:                                         # L404
            return                                       # L405

        # L408-L418：可中断休眠，每次 1 秒粒度轮询 stop_event
        slept = 0                                        # L408
        while slept < interval:                          # L409
            if stop_event and stop_event.is_set():       # L410
                log_fn("监控已停止。")
                return
            time.sleep(min(1, interval - slept))         # [不确定] 使用了 min(...)，常量表有 min
            slept += 1


# =========================================================================
# L421~L461 命令行子功能
# =========================================================================

def cmd_test(config):
    """--test：跑一次搜索并打印结果，验证 Cookie。L421-L437。"""
    if not config.get("cookie"):
        print("请先在 config.json 填写 cookie（可运行 python xianyu_price_alert.py --setup）。")
        return
    client = XianyuClient(config["cookie"])
    keyword = config["keyword"]
    try:
        items = client.search(keyword, page_size=config.get("page_size", 30))
    except Exception as e:
        print(f"搜索失败：{e}")
        return
    print(f"关键词「{keyword}」搜索到 {len(items)} 条：")
    for it in items:
        # [不确定] 标题截断长度 32 来自常量表
        price_s = f"￥{it.price:.2f}" if it.price is not None else f"({it.price_text})"
        print(f"  {price_s} | {it.title[:32]} | {it.url}")
    print("\n若能看到商品且价格正常，说明 Cookie 有效，直接运行本脚本即可开始监控。")


def cmd_setup(config):
    """--setup：交互式填写配置。L439-L461。提示语逐条确证。"""
    print("=== 闲鱼低价提醒 初始化 ===")
    config["keyword"] = input(f"搜索关键词 [{config.get('keyword', '')}]: ") or config.get("keyword", "")
    config["max_price"] = input(f"提醒价格阈值(元) [{config.get('max_price', '')}]: ") or config.get("max_price")
    config["interval_minutes"] = input(f"扫描间隔(分钟) [{config.get('interval_minutes', '')}]: ") or config.get("interval_minutes")
    config["page_size"] = input(f"每页条数 [{config.get('page_size', '')}]: ") or config.get("page_size")
    ck = input("粘贴浏览器 Cookie（登录闲鱼后复制，留空保持不变）: ")
    if ck:
        config["cookie"] = ck
    save_config(config)
    print("配置已保存到 config.json，运行 python xianyu_price_alert.py 开始监控。")
    # [不确定] 各字段的 float()/int() 转换位置；反汇编中存在类型转换调用


# =========================================================================
# L463~L779 图形界面（tkinter）
# =========================================================================

def run_gui(config):
    """tkinter 图形界面。L463-L779。此处还原结构与全部文案，事件逻辑给要点。

    窗口: 标题 '闲鱼低价提醒'，resizable(False, False)，尺寸 660x600。
    图标: resource_path('icon.ico') → iconbitmap。
    """
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox  # L465 确证
    import threading
    import queue

    def apply_icon(win):
        """把应用图标应用到任意窗口（主窗口 + 所有子窗口复用）。L471-L478。"""
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass

    root = tk.Tk()
    root.title("闲鱼低价提醒")           # L480 附近
    root.resizable(False, False)
    # 尺寸常量 (660, 600) 来自 co_consts，配合 'x' / '+' 拼 geometry 串并居中

    def show_about():
        """「关于」对话框。L500-L532。文案全部确证。"""
        win = tk.Toplevel(root)
        win.title("关于")
        win.resizable(False, False)
        apply_icon(win)
        ttk.Label(win, text="闲鱼低价提醒小工具", font=("", 13, "bold")).pack(padx=16, pady=(14, 2))
        ttk.Label(win, text=f"版本：v{VERSION}", font=("", 10)).pack(pady=2)
        ttk.Label(win, text=f"作者：{AUTHOR}", font=("", 10)).pack(pady=2)
        ttk.Separator(win, orient="horizontal").pack(fill="x", padx=8, pady=8)
        ttk.Label(
            win,
            text=(
                "主要功能：\n"
                "• 按关键词监控闲鱼商品\n"
                "• 价格低于设定阈值时，桌面弹窗 + 声音 + 日志提醒\n"
                "• 软件内一键自动获取 Cookie（需 playwright）\n"
                "• 自动去重，避免重复提醒"
            ),
            justify="left",
            font=("", 10),
        ).pack(padx=16, pady=4)
        ttk.Label(
            win,
            text="说明：Cookie 约 24 小时有效，过期请重新获取。",
            foreground="gray",
            font=("", 9),
        ).pack(padx=16, pady=(2, 8))
        ttk.Button(win, text="确定", command=win.destroy, width=10).pack(pady=(0, 14))

    vars_map = {}

    def add_field(label, key, width=14):
        """网格化添加一个 Label+Entry。L534-L566。"""
        # [不确定] row 计数由闭包外部变量维护
        v = tk.StringVar(value=str(config.get(key, "")))
        vars_map[key] = v
        return v

    # L536-L556：五个输入字段（标签文案 + 配置键，确证）
    #   '搜索关键词'    -> keyword
    #   '价格阈值(元)'  -> max_price
    #   '间隔(分钟)'    -> interval_minutes
    #   '每页条数'      -> page_size
    #   'Cookie'        -> cookie   （Entry width=60）
    # L557：Cookie 提示语（灰色 8 号字）
    #   'Cookie 必须包含 _m_h5_tk=...；可直接点下方「获取Cookie」自动登录获取，
    #    或手动从浏览器开发者工具复制完整 Cookie'

    log_queue = queue.Queue()
    # L559-L563：scrolledtext.ScrolledText(height=14) 作为日志区

    def log_put(msg):
        """线程安全地投递日志。L568-L569。"""
        log_queue.put(msg)

    def consume():
        """主线程 200ms 轮询日志队列。L571-L579。"""
        while not log_queue.empty():
            txt_log.insert("end", log_queue.get() + "\n")
            txt_log.see("end")
        root.after(200, consume)

    def sync_config_from_fields():
        """把界面输入写回 config。L581-L589。

        确证：('max_price','min_price') 走 float(默认0.0)，
              ('interval_minutes','page_size','pages') 走 int。
        """
        for k, v in vars_map.items():
            config[k] = v.get()
        for k in ("max_price", "min_price"):
            config[k] = float(config.get(k) or 0.0)
        for k in ("interval_minutes", "page_size", "pages"):
            config[k] = int(config.get(k) or 0)

    def validate_cookie():
        """校验 Cookie 是否含 _m_h5_tk。L591-L604。文案确证。"""
        if not config.get("cookie", ""):
            messagebox.showinfo("提示", "请先填写 Cookie 再操作。")
            return False
        if "_m_h5_tk=" not in config["cookie"]:
            messagebox.showerror(
                "Cookie 格式错误",
                "Cookie 中未找到 _m_h5_tk 字段。\n"
                "请登录 https://www.goofish.com 后，在浏览器开发者工具里找到 "
                "h5api.m.goofish.com 的请求，复制完整 Cookie 填入。",
            )
            return False
        return True

    def set_buttons_state(monitoring):
        """按钮禁用/启用切换。L606-L616。'disabled' / 'normal'。"""

    stop_event = threading.Event()

    def start():
        """「开始监控」。L618-L632。起 daemon 线程跑 monitor。"""
        sync_config_from_fields()
        if not validate_cookie():
            return
        save_config(config)
        stop_event.clear()
        set_buttons_state(True)
        threading.Thread(
            target=monitor, args=(config, log_put, stop_event, False), daemon=True
        ).start()
        log_put("监控已启动……")

    def stop():
        """「停止」。L634-L637。"""
        stop_event.set()
        set_buttons_state(False)
        log_put("正在停止（当前轮结束后生效）……")

    def test_search():
        """「测试搜索」。L639-L676。daemon 线程内跑一次搜索。"""

        def do_test():
            """L644-L662。"""
            try:
                client = XianyuClient(config["cookie"])
                items = client.search(config["keyword"], page_size=config.get("page_size", 30))
            except Exception as e:
                log_put(f"测试失败：{e}")
                return
            if not items:
                log_put("没有搜到商品，可能是 Cookie 失效或关键词无结果。")
                return
            log_put(f"搜索「{config['keyword']}」得到 {len(items)} 条：")
            for it in items:
                price_s = f"￥{it.price:.2f}" if it.price is not None else f"({it.price_text})"
                log_put(f"  {price_s} | {it.title} | {it.url}")

        def restore():
            """L664-L666。200ms 后恢复按钮。"""

        sync_config_from_fields()
        if not validate_cookie():
            return
        threading.Thread(target=do_test, daemon=True).start()

    def fetch_cookie():
        """「🔑 获取Cookie」。L678-L699。检测 playwright 是否可用。"""
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except Exception:
            messagebox.showerror(
                "需要安装组件",
                "自动获取 Cookie 需要 playwright。\n\n"
                "请先在命令行运行：\n"
                "  pip install playwright\n"
                "  playwright install chromium\n\n"
                "安装并重启程序后即可一键获取。\n"
                "（也可以手动在浏览器开发者工具复制 Cookie 填入。）",
            )
            return
        threading.Thread(target=_cookie_worker, daemon=True).start()

    def _cookie_worker():
        """Playwright 有头浏览器抓 Cookie。L701-L746。**关键安全观察点**。

        确证要点：
          - sync_playwright() → launch(headless=False)   ← 有头，用户可见
          - goto('https://www.goofish.com/', timeout=30000)
          - 最长轮询 240 次（对应文案「4 分钟内未检测到登录态」），每次 sleep 1s
          - 命中条件：cookies 中存在 name == '_m_h5_tk'
          - 组装：'; '.join(f"{c['name']}={c['value']}" for c in cookies)
          - 成功后回填 config['cookie'] 并 log「✅ 已自动获取 Cookie（共 N 项）并填入！」
        """
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            ctx = browser.new_context()
            page = ctx.new_page()
            log_put("🌐 已在浏览器打开闲鱼，请扫码 / 登录……")
            try:
                page.goto("https://www.goofish.com/", timeout=30000)
            except Exception as e:
                log_put(f"打开登录页出错：{e}")
            for _ in range(240):
                cookies = ctx.cookies()
                if any(c["name"] == "_m_h5_tk" for c in cookies):      # L724 <genexpr>
                    s = "; ".join(f"{c['name']}={c['value']}" for c in cookies)  # L727 <genexpr>
                    root.after(0, lambda v=s: vars_map["cookie"].set(v))  # L730 <lambda>
                    log_put(f"✅ 已自动获取 Cookie（共 {len(cookies)} 项）并填入！")
                    break
                time.sleep(1)
            else:
                log_put("⏰ 4 分钟内未检测到登录态，请确认已登录后点「已登录」。")
            # [不确定] browser.close() 的确切位置（with 块结束时必然关闭）

    def fetch_done():
        """「已登录」按钮。L747-L749。"""

    def fetch_cancel_now():
        """「取消获取」按钮。L750-L752。log「已取消自动获取。」"""

    # L620-L660 附近按钮区文案（确证）：
    #   '开始监控' / '测试搜索' / '停止'(初始 state='disabled') /
    #   '🔑 获取Cookie' / '已登录' / '取消获取' / '关于'
    consume()
    root.mainloop()
    return 0


# =========================================================================
# L781~L827 桌面快捷方式
# =========================================================================

def create_desktop_shortcut():
    """在桌面创建带应用图标的快捷方式，双击即启动图形界面。

    使用 Windows WScript.Shell 创建 .lnk；图标取自同目录下的 icon.ico。
    返回快捷方式路径；失败返回 None。

    L781-L827。docstring 为 co_consts 原文。
    **安全观察点：会调用 powershell 子进程。**
    """
    import subprocess

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    lnk = os.path.join(desktop, "闲鱼低价提醒.lnk")

    if getattr(sys, "frozen", False):
        target = sys.executable
        args = ""
    else:
        # [不确定] 非冻结态取 python.exe（常量 'python.exe' 确证）
        target = os.path.join(os.path.dirname(sys.executable), "python.exe")
        args = os.path.abspath(__file__)

    icon = resource_path("icon.ico")
    workdir = BASE_DIR

    # 确证：下面的 PowerShell 脚本由 6 段常量 BUILD_STRING 拼成
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell\n'
        f'$lnk = $ws.CreateShortcut("{lnk}")\n'
        f'$lnk.TargetPath = "{target}"\n'
        f'$lnk.Arguments = "{args}"\n'
        f'$lnk.WorkingDirectory = "{workdir}"\n'
        f'$lnk.IconLocation = "{icon},0"\n'
        f'$lnk.Description = "闲鱼低价提醒小工具"\n'
        f'$lnk.Save()\n'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as e:
        print(f"创建桌面快捷方式失败：{e}")
        return None
    print(f"已创建桌面快捷方式：{lnk}")
    return lnk


# =========================================================================
# L829~ 入口
# =========================================================================

def main():
    """命令行入口。L829-末尾。argparse 全部选项确证。"""
    parser = argparse.ArgumentParser(description="闲鱼低价提醒小工具")
    parser.add_argument("--test", action="store_true", help="测试一次搜索并打印结果")
    parser.add_argument("--setup", action="store_true", help="交互式填写配置")
    parser.add_argument("--gui", action="store_true", help="强制打开图形界面")
    parser.add_argument("--console", action="store_true", help="无界面，后台持续监控")
    parser.add_argument("--once", action="store_true", help="只扫描一轮就退出")
    parser.add_argument("--shortcut", action="store_true", help="在桌面创建带图标的快捷方式")
    args = parser.parse_args()

    config = load_config()

    if args.shortcut:
        create_desktop_shortcut()
        return 0
    if args.setup:
        cmd_setup(config)
        return 0
    if args.test:
        cmd_test(config)
        return 0
    if args.console:
        monitor(config, once=args.once)
        return 0

    # 默认走 GUI，失败回退后台监控（文案确证）
    try:
        return run_gui(config)
    except Exception as e:
        print(f"无法启动图形界面（{e}），改为后台监控。也可用 --test / --setup 先配置。")
        monitor(config, once=args.once)
        return 0


if __name__ == "__main__":
    sys.exit(main())
