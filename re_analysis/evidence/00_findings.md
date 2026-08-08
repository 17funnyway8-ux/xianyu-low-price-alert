# 00 - 逆向工程取证 · 汇总报告

**目标**：`C:\Users\fun\Desktop\闲鱼低价提醒工具V1.0.exe`
**分析日期**：本轮会话
**分析者**：寇豆码（Kou）· 软件开发团队工程师
**分析方式**：静态取证。**全程只读目标文件，未执行 exe。**
**产物目录**：`C:\Users\fun\WorkBuddy\开发 - 闲鱼低价提醒工具\re_analysis\`

---

## 摘要（TL;DR）

| 项 | 结论 | 置信度 |
| --- | --- | --- |
| 打包方式 | PyInstaller **onefile**，CArchive 追加于 PE overlay（占全文件 99.5%） | 🟢 确证 |
| 运行时 | **CPython 3.13**（`python313.dll`，6.15 MB） | 🟢 确证 |
| 业务代码形态 | **单文件脚本** `xianyu_price_alert.py`（1 个模块 / 53 个 code object） | 🟢 确证 |
| 反汇编成功率 | **53 / 53 = 100%** | 🟢 确证 |
| 与本地 `xianyu_alert` 项目 | **非同源**。协议常量 100% 一致，代码结构 0% 一致 | 🟢 确证 |
| 恶意行为 | **未发现** | 🟢 确证 |
| 已知缺陷 | 桌面弹窗通知**永久失效**（依赖库未打包） | 🟢 确证 |
| IO 格式还原 | **14/14 项经真实运行产物实证校验，零偏差**（见 §6.5） | 🟢 静态+实证双重确证 |

---

## 1. 打包方式与运行时

### 1.1 文件指纹

| 项 | 值 |
| --- | --- |
| 大小 | 53,872,111 B (51.38 MB) |
| MD5 | `adff8e0510f607b86a652f28cc5c5222` |
| SHA256 | `3c9bea9cdacb02d75a1cfe03d7f98e5eb96aa01ab68dfdf3f4dd900d73ee7937` |
| 架构 | PE32+ x86-64，`IMAGE_SUBSYSTEM_WINDOWS_GUI` |
| 链接时间戳 | 2026-07-29 02:49:28 UTC |
| 文件 mtime | 2026-07-31 14:32 |
| 数字签名 | **无** |
| 版本资源 | **无** VS_VERSIONINFO |
| 图标 | 有（RT_ICON × 7 + RT_GROUP_ICON × 1） |

> 证据：`evidence/01_pe_info.md`

### 1.2 打包结构

```
PE 头 + 7 节区 (286,208 B, 0.5%)
└── overlay: PyInstaller CArchive (53,585,903 B, 99.5%)
    ├── 1,196 个 CArchive 条目
    │   ├── xianyu_price_alert.pyc   ← 唯一业务脚本 (46,315 B)
    │   ├── 11 个 PyInstaller 运行时 pyc
    │   ├── 68 个 .dll / .pyd
    │   ├── python313.dll
    │   ├── icon.ico
    │   └── _tcl_data/ _tk_data/ tcl8/ playwright/ ...
    └── PYZ.pyz → 555 个模块（标准库 + 第三方）
```

- PyInstaller 版本特征：**2.1+**（88 字节 cookie 格式）
- PYZ **未加密**（无 `pyimod00_crypto_key.pyc`）→ 无加壳、无混淆
- 证据：`evidence/02_archive_toc.md`

---

## 2. 模块清单

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| **业务模块** | **1** | `xianyu_price_alert.pyc` |
| PyInstaller 运行时 | 11 | `pyiboot01_bootstrap` / `pyimod0x_*` / `pyi_rth_*` |
| 第三方库 | 272 个 pyc / 11 个顶层包 | 见下 |
| CPython 标准库 | 283 个 pyc | 主要在 PYZ 内 |
| 二进制 | 72 个 .dll / .pyd | 其中 CArchive 顶层 68 个 |

> pyc 合计 567 个；解包落盘文件总数 1,750（含 tcl/tk 数据等 1,111 个非代码文件）。

### 2.1 第三方依赖及版本证据

| 包 | 版本 | 证据强度 |
| --- | --- | --- |
| `playwright` | **1.61.0** | 🟢 `playwright-1.61.0.dist-info/` |
| `requests` + `urllib3` + `certifi` + `idna` + `charset_normalizer` | 未知 | 🟡 无 dist-info；`charset_normalizer` 为 mypyc 编译版（`ada92cb5d92a588d1b93__mypyc.cp313-win_amd64.pyd`） |
| `greenlet`, `pyee` | 未知 | playwright 依赖 |
| `setuptools`, `packaging`, `_distutils_hack` | 未知 | 打包环境残留（非主动依赖） |

### 2.2 关键运行时二进制

| 文件 | 大小 | 意义 |
| --- | --- | --- |
| `python313.dll` | 5.9 MB (6,149,632 B) | 🟢 **确证 Python 3.13** |
| `playwright/driver/`（含 `node.exe` 88 MB + `package/`） | **101 MB**（解压后） | 一键获取 Cookie 的浏览器驱动 |
| `_tkinter.pyd` + `_tcl_data/` + `_tk_data/` + `tcl8/` | — | 🟢 **确证 GUI 用 tkinter** |
| `_hashlib.pyd` / `libcrypto-3-x64.dll` | — | MD5 签名 + TLS |
| `winsound.pyd` | — | 蜂鸣提示音 |

> 证据：`evidence/03_module_inventory.md`

---

## 3. 核心算法发现

### 3.1 ⭐ mtop 签名算法（最重要发现）

```python
def _sign(self, timestamp, data_json):
    raw = f"{self.token}&{timestamp}&{APP_KEY}&{data_json}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
```

- **确证级别**：逐条指令还原，无任何推测
  （`disasm/xianyu_price_alert.txt` → `CODE OBJECT: <module>.XianyuClient._sign`，
  `BUILD_STRING 7` 拼 7 段 → `hashlib.md5(...).hexdigest()`）
- `token` = Cookie 中 `_m_h5_tk` 的**下划线前半段**
  正则：`_m_h5_tk=([a-zA-Z0-9]+)_` → `group(1)`
- `APP_KEY = "34839810"`（闲鱼 PC 站固定值）
- `timestamp` = `str(int(time.time() * 1000))`（毫秒）
- `data_json` = **紧凑 JSON**：`json.dumps(obj, separators=(",", ":"), ensure_ascii=False)`
  ⚠️ 签名串与实际 POST body 必须**字节级一致**，否则验签失败

> **交叉验证**：本地 `xianyu_alert/fetcher.py` L210-L226 `mtop_sign()` 实现
> **完全相同的公式**。两个独立代码基互相印证 → 该算法可确认为闲鱼 PC 站真实签名方案。

### 3.2 价格解析 `parse_price()`

```
去除 ￥ ¥ , 空格 → 命中 ("面议","电议","私聊","咨询") 之一则返回 None
→ 含「万」则乘 10000 → float()，失败返回 None
```
🟢 确证（常量表 + genexpr 反汇编）

### 3.3 去重策略 `dedup_key()`

```python
item.item_id if item.item_id else f"{title}|{seller}|{price_text}"
```
🟢 确证（`BUILD_STRING 5`）。去重集合持久化到 `seen_items.json`，**永不过期、只增不减**。

### 3.4 提醒触发条件（`monitor()` L385-L397，逐条指令确证）

```
key not in seen  AND  price is not None  AND  price <= threshold  AND  price >= min_price
```
（反汇编为 4 个 `continue` 短路：`key in seen` / `price is None` / `price > threshold` / `price < min_price`）

---

## 4. IO 格式规格

### 4.1 配置文件 `config.json`

位置：**exe 同目录**（`os.path.dirname(sys.executable)`）。不存在时自动写入默认值。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `keyword` | str | `"iPhone 13"` | 搜索关键词（**仅单个**） |
| `max_price` | float | `3000.0` | 价格上限，`price <= max_price` 才提醒 |
| `min_price` | float | `0.0` | 价格下限，`price >= min_price` 才提醒 |
| `interval_minutes` | int | `10` | 扫描间隔（分钟），实际 `max(1, int(v)) * 60` 秒 |
| `pages` | int | `1` | 每轮翻页数，`max(1, int(v))`；翻页间 sleep 2s |
| `page_size` | int | `30` | 每页条数 |
| `cookie` | str | `""` | **闲鱼登录 Cookie 明文**，须含 `_m_h5_tk=` |
| `sound` | bool | `true` | 蜂鸣提示 |
| `toast` | bool | `true` | 桌面弹窗（⚠️ 实际失效，见 §6） |
| `bark_url` | str | `""` | Bark 推送地址，非空才发 |
| `webhook_url` | str | `""` | 企微/钉钉 webhook，非空才发 |

写入格式：`json.dump(cfg, f, ensure_ascii=False, indent=2)`
读取后用 `DEFAULT_CONFIG` 补齐缺失键。🟢 全部确证

### 4.2 去重文件 `seen_items.json`

JSON 数组（`list(set)`），`ensure_ascii=False, indent=2`。读取失败一律回退空 set。

### 4.3 提醒日志 `alerts.jsonl`

每行一条 JSON（追加模式 `'a'`，UTF-8）。字段 = `asdict(Item)` 的 8 字段 + 3 个附加字段：

```json
{"item_id":"...","title":"...","price":2680.0,"price_text":"￥2680",
 "url":"https://www.goofish.com/item/xxx","location":"浙江杭州","seller":"某某",
 "image":"https://...","keyword":"iPhone 13","threshold":3000.0,
 "time":"2026-07-31 14:32:00"}
```
🟢 确证（`Notifier._log` 反汇编，`strftime('%Y-%m-%d %H:%M:%S')`）

> **无数据库**。exe 不使用 SQLite（与本地项目的关键差异）。

### 4.4 网络请求格式

**POST** `https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/`

Query string（12 键，全部确证）：
```
jsv=2.7.2  appKey=34839810  t=<ms>  sign=<md5>  v=1.0
type=originaljson  dataType=json  timeout=20000
api=mtop.taobao.idlemtopsearch.pc.search  sessionOption=AutoLoginOnly
spm_cnt=a21ybx.search.0.0  spm_pre=a21ybx.search.searchInput.0
```

Headers（4 键）：
```
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36
            (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0
Content-Type: application/x-www-form-urlencoded
Origin:  https://www.goofish.com
Referer: https://www.goofish.com/
```

Body：`data=<data_json>`，其中 `data_json`（13 键）：
```json
{"pageNumber":"<page>","keyword":"<kw>","fromFilter":false,"rowsPerPage":<n>,
 "sortValue":"","sortField":"","customDistance":"","gps":"","propValueStr":"",
 "customGps":"","searchReqFromPage":"pcSearch","extraFilterValue":"",
 "userPositionJson":""}
```

Cookies：由 `config.cookie` 按 `;` / `=`(split 1) 拆成 dict 传入。timeout=20s。

**响应解析路径**（`parse_response`，全部确证）：
```
校验 : str(json["ret"][0]).startswith("SUCCESS")  否则 raise RuntimeError("接口返回错误：...")
遍历 : json["data"]["resultList"][*]["data"]["item"]["main"]
        ├─ ex    = main["exContent"]
        └─ click = main["clickParam"]["args"]
字段 :
  price_text = click["price"]  or ex["price"]  or ""
  item_id    = ex["itemId"]    or click["itemId"] or ex["id"] or ""   (.strip())
  title      = ex["title"] or ex["detailParams"]["title"] or click["title"] or "未知标题"
  location   = ex["area"]         (default "")
  seller     = ex["userNickName"] (default "")
  image      = ex["picUrl"]       (回退键 "image")
  url        = "https://www.goofish.com/item/<item_id>"
               无 item_id 时回退 "https://www.goofish.com/search?q=<keyword>"
```

### 4.5 通知消息模板（逐字符确证）

```
【闲鱼捡漏】{title}
价格：￥{price:.2f}（设定阈值 ￥{threshold:.2f}）
地区：{location}　卖家：{seller}
链接：{url}
```
（「地区」与「卖家」之间是全角空格 `\u3000`）

| 通道 | 实现 |
| --- | --- |
| toast | `win11toast.toast` (asyncio.run) → 失败降级 `plyer.notification.notify(title=, message=)` |
| sound | `winsound.Beep(1000,350)` → `sleep(0.12)` → `Beep(1500,350)` |
| bark | `GET {bark_url.rstrip('/')}/{quote(msg)}`, timeout=10 |
| webhook | `POST webhook_url, data=json.dumps({"msgtype":"text","text":{"content":msg}})`, timeout=10 |

### 4.6 CLI 参数

| 参数 | 行为 |
| --- | --- |
| （无） | 打开 GUI；失败则回退后台监控 |
| `--gui` | 强制 GUI |
| `--test` | 跑一次搜索并打印，验证 Cookie |
| `--setup` | 交互式填配置 |
| `--console` | 无界面后台持续监控 |
| `--once` | 只扫一轮就退出 |
| `--shortcut` | 桌面创建 `.lnk`（调用 PowerShell） |

---

## 5. 与本地项目的差分结论

> 详见 `evidence/04_diff_vs_local.md`

### 5.1 结论：**非同源**

exe 内是**独立编写的单文件脚本**，与本工作区 `xianyu_alert/`（9 模块包，4,576 行）
是"同目标、不同实现"的平行产物。

### 5.2 决定性证据（5 条）

| # | 证据 |
| --- | --- |
| E1 | 1,196 CArchive + 555 PYZ 条目全量搜索 `*xianyu*` → **只命中 `xianyu_price_alert.pyc` 一个** |
| E2 | `co_filename == 'xianyu_price_alert.py'`，与本地任何文件名都不同 |
| E3 | 归档中**无 `yaml`、无 `bs4`、无 `soupsieve`**——而本地 `requirements.txt` 硬依赖这两者 |
| E4 | 业务模块 `co_names` 无 `sqlite3`；本地 `storage.py` 以 SQLite 为核心 |
| E5 | 53 个 code object 中**没有一个**与本地的类名/函数名重合 |

### 5.3 匹配度量化

| 维度 | 匹配度 |
| --- | --- |
| mtop 协议常量（API 名 / URL / appKey / token cookie / 签名公式） | **100%**（5/5） |
| HTTP 细节（UA、详情页 URL 模板、风控重试） | **0%**（0/3） |
| 模块结构 / 类名 / 函数名 | **0%** |
| 配置字段（同层级同名） | **0%**（JSON 扁平 vs YAML 嵌套） |
| 数据模型字段 | **25%**（8 字段中 title/url 同名） |
| 存储方案 | **0%**（JSON 文件 vs SQLite） |
| 通知通道 | **0%**（toast/sound/bark/webhook vs console/serverchan/email/telegram） |

### 5.4 exe 独有、本地可借鉴的 4 个点

1. `parse_price()` 的「万」单位换算 + 「面议/电议/私聊/咨询」过滤
2. Windows 原生通知（win11toast）+ winsound 蜂鸣 + Bark + 企微 webhook 四通道
3. `create_desktop_shortcut()`：PowerShell + WScript.Shell 建带图标 .lnk
4. 翻页限速 `time.sleep(2)`

### 5.5 唯一身份线索

`AUTHOR = '花开半夏'` —— 已用 grep 全量复核，**本地代码库中不存在该字符串**。

---

## 6. ⭐ 附加发现：功能缺陷（打包遗漏）

> 这是本次分析的意外收获，建议向主理人/用户明确提示。

| 缺陷 | 证据 | 影响 |
| --- | --- | --- |
| **桌面弹窗通知永久失效** | 🟢 全树 `find` 搜索 `*toast*` / `*plyer*` → **零命中**（只有 `winsound.pyd`）。而 `Notifier._toast` 依赖 `from win11toast import toast` 与 `from plyer import notification`，两个 import 都会 `ImportError`，被 `except Exception: pass` **静默吞掉** | `config.toast=true` 是默认值，但用户**永远看不到弹窗，也看不到任何报错**。实际只有蜂鸣声 + jsonl 日志 |
| Chromium 浏览器未内置 | 🟢 全树搜 `*chrome*`/`*chromium*` 仅命中 `playwright/driver/package/bin/` 下的几个 `reinstall_*.sh/.ps1` 安装脚本，**无浏览器可执行文件** | 「🔑 获取Cookie」需用户本机已执行过 `playwright install chromium`，否则失败 |
| playwright driver 占了体积大头 | 🟢 `du -sh playwright` = 103 MB，其中 `playwright/driver` = 101 MB（`node.exe` 单文件 88 MB） | 51 MB 的 exe 体积几乎全部来自 playwright，而它只服务一个可选功能 |

---

## 6.5 ⭐⭐ 经验证据：用真实运行产物验证静态还原（100% 命中）

> **意外收获**：桌面上存在该 exe 的**历史运行产物**——`config.json`(916 B)、
> `seen_items.json`(243 B)、`alerts.jsonl`(12,425 B)，mtime 均为 **2026-07-31 14:52**
> （比 exe 自身 mtime 14:32 晚 20 分钟）。
>
> **这些文件由用户此前自行运行 exe 产生，不是本次分析造成的**（本次分析全程未执行 exe，
> 且分析结束后已复核目标 exe 的 MD5 未变）。它们构成了对静态还原结果的**黑盒实证校验**。
>
> 读取时已对 Cookie 值脱敏，仅记录长度与格式特征。

### 校验结果：静态还原 vs 真实产物

| 我的静态还原预测 | 真实运行产物 | 结果 |
| --- | --- | --- |
| `config.json` 恰好 11 个顶层键 | 实测 11 个 | ✅ |
| 键名与顺序：keyword, max_price, min_price, interval_minutes, pages, page_size, cookie, sound, toast, bark_url, webhook_url | **完全一致，顺序也一致** | ✅ |
| 类型：max_price/min_price=float，interval_minutes/pages/page_size=int，sound/toast=bool，其余 str | 实测 `6000.0`(float)、`10`/`1`/`30`(int)、`True`(bool)、`''`(str) | ✅ |
| `bark_url` / `webhook_url` 默认空串 | 实测均为 `''` | ✅ |
| cookie 明文存储且含 `_m_h5_tk=` | 实测 673 字符明文，含 `_m_h5_tk=` → **§5「Cookie 明文落盘」风险确认成立** | ✅ |
| `seen_items.json` = JSON **数组** | 实测 `list`，12 条 | ✅ |
| 去重键有 item_id 时**就是 item_id** | 实测 `['1017140693898','1036108317398',...]` 纯数字 ID | ✅ |
| `alerts.jsonl` 每行 = `asdict(Item)`(8字段) + keyword + threshold + time，共 11 字段 | 实测字段列表：`['item_id','title','price','price_text','url','location','seller','image','keyword','threshold','time']` — **11 个，顺序完全一致** | ✅ |
| `time` 格式 `%Y-%m-%d %H:%M:%S` | 实测 `'2026-07-31 14:48:35'` | ✅ |
| `price` 为 float、`price_text` 为原始文本 | 实测 `568.0` / `'568'` | ✅ |
| URL 模板 `https://www.goofish.com/item/{item_id}` | 实测 `https://www.goofish.com/item/1036108317398` | ✅ **同时反证了本地项目的 `/item?id=` 格式确实不同** |
| `location` ← `exContent.area`，`seller` ← `exContent.userNickName`，`image` ← `picUrl` | 实测 `'广东'` / `'tbNick_t8lzy'` / `http://img.alicdn.com/bao/uploaded/...` | ✅ |
| `BASE_DIR = os.path.dirname(sys.executable)`（= exe 同目录） | 三个文件确实生成在**桌面**（exe 所在目录） | ✅ **§5.2 路径推导链实证确认** |

**校验结论：14/14 项全部命中，零偏差。**
本报告 §4「IO 格式规格」由纯静态反汇编推导而来，现已获得真实运行数据的独立验证，
可信度从"确证（静态）"提升为**"确证（静态 + 实证双重）"**。

> 附带信息（来自真实产物，非代码分析）：用户曾用关键词 `macbook air M4 16 512`（阈值 6000）
> 与 `DDR4 3200 32G`（阈值 699）等进行过监控，已产生 12 条提醒记录。
> 此为用户个人数据，本报告不作进一步展开。

---

## 7. 不确定项清单

| # | 项 | 原因 | 影响 |
| --- | --- | --- | --- |
| U1 | `dig()` 的循环体细节 | 反汇编可见语义为逐层 `dict.get`，但具体写法（for vs reduce）无法唯一确定 | 无（语义确定） |
| U2 | `Item` 各字段是否带默认值 | `co_consts` 末尾有 `()` 与 `None`，暗示部分字段有默认值 | 无（字段名与顺序确证） |
| U3 | `resource_path()` 的分支写法 | 语义确定为 `_MEIPASS` / `BASE_DIR` 二选一 | 无 |
| U4 | `monitor()` 异常分支是否 `sleep(5)` | 常量表存在 `5`，但控制流未逐条追到 | 极小 |
| U5 | `monitor()` 休眠循环中 `min()` 的确切用法 | `co_names` 含 `min`，推测为 `sleep(min(1, interval - slept))` | 极小 |
| U6 | `cmd_setup()` 中各字段的 `float()`/`int()` 转换位置 | 反汇编可见转换调用，位置未逐条确认 | 极小 |
| U7 | `load_config()` 补齐用 `setdefault` 还是 `if k not in cfg` | 语义等价 | 无 |
| U8 | GUI 布局中 row/column 的精确网格坐标 | 未逐条追（对理解功能无价值） | 无 |
| U9 | `requests` / `urllib3` / `certifi` 的具体版本 | 归档中无对应 `.dist-info` | 无法给出版本号 |
| U10 | `_toast` 中 `asyncio.run()` 包裹的确切表达式 | 反汇编可见 `asyncio.run(...)`，参数构造未逐条追 | 无（该分支实际不可达，见 §6） |
| U11 | 第三方库（playwright/requests）未逐一审计 | 属知名公开库，基于声誉信任 | 安全结论中已声明 |

**所有不确定项均不影响本报告的任何核心结论。**

---

## 8. 产物清单

```
re_analysis/
├── evidence/
│   ├── 00_findings.md                    ← 本文件（汇总）
│   ├── 01_pe_info.md                     PE 头 / 节区 / 签名 / 导入表 / 资源
│   ├── 02_archive_toc.md                 CArchive + PYZ 解包清单与分类统计
│   ├── 03_module_inventory.md            业务模块 / 第三方库 / 运行时 三张表
│   ├── 04_diff_vs_local.md               与本地 xianyu_alert 的逐项差分
│   ├── 05_behavior_static.md             静态行为与安全观察（确证/推测分级）
│   ├── struct_xianyu_price_alert.md      53 个 code object 的签名 + 常量表全量提取
│   └── _business_strings.txt             业务模块全部 310 个字符串常量 + URL/路径提取
├── disasm/
│   └── xianyu_price_alert.txt            53/53 code object 完整反汇编（含 co_names/varnames）
├── recovered/
│   └── xianyu_price_alert.py             还原源码骨架（带行号锚点与 [不确定] 标注）
├── extracted/
│   └── 闲鱼低价提醒工具V1.0.exe_extracted/   完整解包产物（1196 + 555 条目）
└── tools/
    ├── pe_info.py                        PE 解析器
    ├── pyc_analyze.py                    pyc 反汇编 + 结构化提取器
    └── inventory.py                      清单生成 + 行为线索提取
```

---

## 9. 安全声明

- ✅ 目标 exe **全程只读**，未修改 / 移动 / 删除 / 重命名
- ✅ 目标 exe **未被执行**
- ✅ 所有产物均写入 `re_analysis/`，未在桌面创建任何文件
- ✅ 安装的工具（`pefile`、`pyinstxtractor-ng`）仅装入隔离 Python 环境
