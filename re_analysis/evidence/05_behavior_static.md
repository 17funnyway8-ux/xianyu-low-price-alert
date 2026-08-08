# 05 - 静态行为线索与安全观察

> **本报告全程未执行目标 exe。** 所有结论来自 PE 解析、CArchive 解包、以及业务模块
> `xianyu_price_alert.pyc` 的字节码反汇编与常量表。
>
> 标注约定：
> - 🟢 **确证** — 有直接的字符串常量或字节码指令支撑，可指向具体位置
> - 🟡 **推测** — 由多条间接证据推断，未见直接指令

---

## 5.1 网络行为

### 5.1.1 完整域名/URL 清单（业务模块全量字符串扫描结果）

扫描方式：递归 `co_consts` 收集全部 310 个字符串常量，正则 `https?://[^\s"'<>\\)]+` 提取。
产物：`evidence/_business_strings.txt`

| # | URL | 用途 | 触发路径 | 证据 |
| --- | --- | --- | --- | --- |
| 1 | `https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/` | 闲鱼商品搜索（POST） | `XianyuClient.search` | 🟢 确证：模块级 `API_URL`，`search` 中 `LOAD_GLOBAL API_URL` → `requests.post` |
| 2 | `https://www.goofish.com` | 请求头 `Origin` | `XianyuClient.search` | 🟢 确证：`headers` BUILD_CONST_KEY_MAP |
| 3 | `https://www.goofish.com/` | 请求头 `Referer`；playwright 登录页 `page.goto` | `search` / `_cookie_worker` | 🟢 确证 |
| 4 | `https://www.goofish.com/item/` | 商品详情链接**拼接前缀**（仅出现在消息文本中，不主动请求） | `parse_response` | 🟢 确证 |
| 5 | `https://www.goofish.com/search?q=` | 搜索页链接**拼接前缀**（无 item_id 时的回退，不主动请求） | `parse_response` | 🟢 确证 |

**除上述之外，业务代码中不存在任何其它硬编码 URL 或域名。** 🟢 确证（全量字符串扫描无遗漏）

### 5.1.2 用户可配置的出站地址（非硬编码）

| 配置键 | 方法 | 请求方式 | 证据 |
| --- | --- | --- | --- |
| `bark_url` | `Notifier._bark` | `requests.get(bark_url.rstrip('/') + '/' + quote(msg), timeout=10)` | 🟢 确证（L286-L287 反汇编） |
| `webhook_url` | `Notifier._webhook` | `requests.post(webhook_url, data=json.dumps({"msgtype":"text","text":{"content":msg}}), timeout=10)` | 🟢 确证（L293-L295 反汇编） |

> ⚠️ 这两个地址**由用户自己在 config.json 填写**，默认值为空字符串（`DEFAULT_CONFIG` 中
> `"bark_url": ""`, `"webhook_url": ""`），且 `notify()` 里有 `if self.config.get('bark_url')`
> 的非空判断才会发送。**不构成隐蔽外传**。🟢 确证

### 5.1.3 出站数据内容

| 目的地 | 发送的数据 | 是否含用户敏感信息 |
| --- | --- | --- |
| goofish mtop 接口 | 搜索关键词、页码、页大小、appKey、时间戳、md5 sign；**Cookie 通过 `cookies=` 参数原样带上** | ✔ 含用户闲鱼登录态（这是接口的必要条件，发往闲鱼官方域名） |
| bark_url（可选） | 提醒消息文本：商品标题、价格、地区、卖家昵称、链接 | ✖ 不含 Cookie / 不含本机信息 |
| webhook_url（可选） | 同上 | ✖ 同上 |

🟢 **确证：不存在把 Cookie、配置文件、本机标识发往第三方或作者服务器的代码路径。**
依据：全部 `requests.get` / `requests.post` 调用点共 3 处（`search` / `_bark` / `_webhook`），
已逐条反汇编核对目标 URL 与 payload 构造。

---

## 5.2 文件系统行为

所有路径均基于 `BASE_DIR`。🟢 确证：`BASE_DIR = os.path.dirname(sys.executable)`（冻结态分支，
模块级 L39-L40 反汇编 `getattr(sys,'frozen',False)` → `os.path.dirname(sys.executable)`）。
即 **exe 所在目录**（此处 = 用户桌面）。

| 路径 | 读 | 写 | 时机 | 内容 | 证据 |
| --- | --- | --- | --- | --- | --- |
| `<exe目录>/config.json` | ✔ | ✔ | 启动时（不存在则写默认值）、`--setup`、GUI「开始监控」 | 配置 JSON | 🟢 `load_config`/`save_config` 反汇编 |
| `<exe目录>/seen_items.json` | ✔ | ✔ | 每轮扫描结束 | 去重 key 的 JSON 数组 | 🟢 `load_seen`/`save_seen` |
| `<exe目录>/alerts.jsonl` | ✖ | ✔（追加 `'a'`） | 每次触发提醒 | 每行一条 JSON 提醒记录 | 🟢 `Notifier._log` |
| `<MEIPASS>/icon.ico` | ✔ | ✖ | GUI 启动、创建快捷方式 | 应用图标 | 🟢 `resource_path('icon.ico')` |
| `%TEMP%\_MEIxxxxxx\` | ✔ | ✔ | 进程启动/退出 | PyInstaller onefile 自解压临时目录 | 🟢 onefile 打包的固有行为 |
| `~\Desktop\闲鱼低价提醒.lnk` | ✖ | ✔ | **仅 `--shortcut` 参数触发** | Windows 快捷方式 | 🟢 `create_desktop_shortcut` |

> ⚠️ **实际影响提示**：该 exe 目前位于用户桌面，若运行会在**桌面**生成
> `config.json` / `seen_items.json` / `alerts.jsonl` 三个文件。🟢 确证（BASE_DIR 推导链完整）
>
> **实证确认**：桌面上已存在这三个文件（mtime 2026-07-31 14:52，为用户此前自行运行所产生，
> 非本次分析造成）。其内容与本报告静态推导的格式 **14/14 项完全吻合**。
> 详见 `00_findings.md` §6.5。
>
> 🔴 **由此，"Cookie 明文落盘"从推断升级为已发生的事实**：桌面 `config.json` 中确实
> 存有 673 字符的闲鱼登录 Cookie 明文（含 `_m_h5_tk=`）。建议提醒用户：该文件不应
> 随桌面截图/文件分享外传；不再使用时应清空 `cookie` 字段。

**未发现**：读取用户文档/浏览器数据目录、遍历磁盘、读取其它应用配置的代码。🟢 确证（全量字符串扫描无相关路径）

---

## 5.3 子进程与系统调用

| 行为 | 触发条件 | 证据 | 评估 |
| --- | --- | --- | --- |
| `subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-Command", <脚本>])` | **仅当命令行带 `--shortcut`** | 🟢 确证：`create_desktop_shortcut` co_consts 含全部 6 个参数字符串 | ⚠️ **值得关注但合理**：`-ExecutionPolicy Bypass` 是创建 .lnk 的常见做法；脚本内容已完整还原（见 `recovered/xianyu_price_alert.py`），**仅调用 `WScript.Shell.CreateShortcut`，无其它操作** |
| 启动 Chromium 浏览器（有头） | 仅当用户点击 GUI「🔑 获取Cookie」 | 🟢 确证：`_cookie_worker` 中 `sync_playwright()` → `launch(headless=False)` | ✅ 有头模式 = 用户全程可见，非静默 |
| `winsound.Beep(1000,350)` / `Beep(1500,350)` | 触发提醒且 `sound=true` | 🟢 确证 | ✅ 无害 |

**未发现**：`os.system`、`os.popen`、`ctypes` 调用 Win32 API、`shell=True`、下载并执行外部文件。
🟢 确证（模块级与各函数 `co_names` 中无 `system`/`popen`/`ctypes`/`urlretrieve`/`exec`/`eval`/`compile`）

---

## 5.4 安全观察项逐条核查

| 观察项 | 结论 | 证据 |
| --- | --- | --- |
| **开机自启动** | ❌ 无 | 🟢 确证：无 `Run` 注册表键字符串、无启动文件夹路径、无 `schtasks`/`sc create` |
| **注册表操作** | ❌ 无 | 🟢 确证：无 `winreg` 导入（模块级 IMPORT_NAME 列表已全量列出：argparse/asyncio/hashlib/json/os/re/sys/time/dataclasses/datetime/typing/requests；函数内延迟导入仅 win11toast/plyer/winsound/tkinter/playwright/subprocess） |
| **上传用户数据到第三方** | ❌ 无 | 🟢 确证，见 5.1.3 |
| **Cookie 外传** | ❌ 无（Cookie 只发往 `h5api.m.goofish.com`，即闲鱼官方） | 🟢 确证：`search` 是唯一使用 `self.cookie` 的网络调用点 |
| **Cookie 明文存储** | ⚠️ **是** | 🟢 确证：Cookie 以明文写入 `config.json`（`save_config` → `json.dump`）。**这是隐私风险点**：闲鱼登录态明文落盘，任何能读该文件的程序/人都可冒用账号。属于设计缺陷而非恶意行为 |
| **加密/混淆通信** | ❌ 无 | 🟢 确证：仅 HTTPS + 明文 JSON；`hashlib` 只用于 mtop 签名 |
| **代码混淆/加壳** | ❌ 无 | 🟢 确证：pyc 可正常 `marshal.loads`，53/53 code object 反汇编成功；PYZ 未加密（无 `pyimod00_crypto_key.pyc`） |
| **数字签名** | ❌ 无 | 🟢 确证：`IMAGE_DIRECTORY_ENTRY_SECURITY` 为空（见 `01_pe_info.md` §1.4）。**无法验证发布者身份** |
| **反调试/反分析** | ❌ 无 | 🟢 确证：解包与反汇编全程无阻碍 |
| **网络请求频率** | 温和 | 🟢 确证：默认 `interval_minutes=10`（10 分钟一轮）、`pages=1`；多页时翻页间 `time.sleep(2)`。不构成 DoS |

---

## 5.5 隐私与合规提示（面向使用者）

1. 🟢 **确证**：程序会把用户的**闲鱼登录 Cookie 明文**保存在 `config.json`。若该文件随 exe 位于
   桌面，且用户分享桌面截图/文件，存在账号泄露风险。建议：使用后清空该字段，或把 exe 移到独立目录。
2. 🟢 **确证**：`alerts.jsonl` 会长期累积用户的搜索关注记录（关键词、看过的商品、卖家昵称）。
3. 🟢 **确证**：无数字签名 + 无版本资源 + 无发布者信息，Windows SmartScreen 大概率会拦截。
   来源可信度只能靠本报告的代码级审查背书。
4. 🟡 **推测**：`--shortcut` 使用 `-ExecutionPolicy Bypass` 可能触发部分 EDR 告警，但脚本内容
   已完整还原确认无害。

---

## 5.6 整体安全结论

> 🟢 **在完成对全部 53 个 code object 的反汇编审查后，未发现任何恶意行为。**
>
> 程序行为与其宣称功能（闲鱼关键词低价监控 + 桌面提醒）**完全一致**，不存在后门、
> 数据外传、持久化驻留或权限提升。
>
> 唯二需要提示用户的是**设计层面的隐私弱点**（Cookie 明文落盘、文件写在 exe 同目录）
> 和**分发层面的信任缺失**（无数字签名）。
>
> 覆盖度声明：业务代码 1/1 个模块、53/53 个 code object 已全部反汇编并人工审阅；
> 第三方依赖（playwright/requests 等）为公开知名库，未逐一审计（🟡 该部分为基于
> 库知名度的信任假设，非确证）。
