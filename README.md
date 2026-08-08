# 闲鱼低价提醒工具（xianyu-alert）

一个可长期后台运行的命令行监测工具：按关键词周期性抓取闲鱼最新商品，筛选出**新出现且价格低于阈值**的商品，去重后通过控制台 / 微信 / 邮件 / Telegram 推送提醒。

---

## 一、功能特性

| 需求 | 实现 |
| --- | --- |
| 关键词设置 | `config.yaml` 中可配置任意多个关键词 |
| 价格阈值 | 每个关键词独立配置 `max_price`，**严格小于**该值才提醒 |
| 排除词（v3.1） | `exclude_keywords`：标题命中任一排除词即跳过（回收 / 置换 / 收购 / 高价回收 / 收） |
| 必含词（v3.1） | `required_keywords`：标题必须包含全部；v3.3 起 GUI 添加关键词时**必含留空**由用户自填（config 解析层未显式配置时仍按旧行为自动提取） |
| 循环监测 | 按 `monitor.interval_seconds` 周期抓取，并与上一轮结果比对出「新商品」 |
| 提醒通知 | 通知内容包含 **商品名称 / 价格 / 商品链接 / 发布时间** 四要素 |
| 去重机制 | SQLite 持久化 `notified` 标志，同一商品**永不重复提醒**（跨重启有效） |

---

## 二、快速开始

### 1. 安装依赖

```bash
# 创建虚拟环境（Windows）
C:\Users\fun\.workbuddy\binaries\python\versions\3.13.12\python.exe -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
```

依赖：`requests`、`beautifulsoup4`、`PyYAML`（去重存储用标准库 `sqlite3`，无额外依赖）。

### 2. 离线演示（开箱即用，无需网络）

若 `config.yaml` 的 `fetcher.type` 为 **mock**（开发演示用假数据），可直接跑通完整链路：

```bash
python -m xianyu_alert.cli once --config config.yaml
```

输出示例：

```
============================================================
闲鱼低价提醒｜发现 2 个低价商品
============================================================
【1】
商品名称: Switch 捡漏特价 第0号 九成新
价格: ¥123.45
商品链接: https://www.goofish.com/item?id=1234567
发布时间: 2024-01-01 12:00
命中关键词: Switch
...
```

### 3. 切换到真实抓取

v3.2 起默认抓取方式即 **mtop**（真实抓取）。先获取 Cookie（见下节「获取闲鱼 Cookie」），再编辑 `config.yaml`：

```yaml
fetcher:
  type: "mtop"
monitor:
  cookies: "cookie2=...; _m_h5_tk=..."   # 由 `cli login` 自动写入，或手动填写
  interval_seconds: 600                   # 默认 600 秒（10 分钟），过短易触发风控
```

然后持续运行：

```bash
python -m xianyu_alert.cli run --config config.yaml
```

### 4. 获取闲鱼 Cookie

`login` 子命令可把 Cookie 便捷地写入 `config.yaml` 的 `monitor.cookies`，三种方式任选：

**方式 A（推荐 · 半自动）** —— 浏览器登录，自动提取：

```bash
# 一次性安装可选依赖（仅此功能需要，两条都要执行）
pip install -r requirements-cookie.txt
playwright install chromium

# 打开浏览器 → 登录闲鱼 → 检测到 _m_h5_tk 后自动写入配置
python -m xianyu_alert.cli login --config config.yaml
```

登录成功后会自动提取全部 Cookie（含 `_m_h5_tk`）并写入 `monitor.cookies`，可用 `once` 验证。等待登录超时为 120 秒。

**方式 B（脚本 / 粘贴）** —— 直接传入或按提示粘贴：

```bash
# 脚本模式：直接传入 Cookie 请求头字符串（适合自动化 / CI）
python -m xianyu_alert.cli login --config config.yaml --cookie-string "cookie2=...; _m_h5_tk=..."

# 交互模式：未装 Playwright 时运行 login 会打印安装提示并进入粘贴模式，
# 把复制的 Cookie 粘贴到终端回车即可存盘
python -m xianyu_alert.cli login --config config.yaml
```

**方式 C（纯手工兜底）** —— 从浏览器开发者工具复制：

1. 浏览器登录 [goofish.com](https://www.goofish.com) → 按 `F12` 打开开发者工具 → 切到 **Network** 面板 → 在站内搜索任意关键词；
2. 找到发往 `h5api.m.goofish.com` 的请求 → 在 Request Headers 中**完整复制 Cookie 请求头的值**（必须包含 `_m_h5_tk`）→ 用方式 B 的 `--cookie-string` 存入。

> 提醒：Cookie 存入后由 **MtopFetcher**（或已废弃的 `WebFetcher`）自动携带。闲鱼商品列表数据走**带签名（sign）的 mtop 接口**，v3.2 起默认即 mtop 真实抓取；`web`（旧版 HTML 解析）对闲鱼实测无效、已标记废弃，GUI 不再展示，仅保留代码供向后兼容。

---

## 三、命令行用法

```bash
python -m xianyu_alert.cli run   [-c config.yaml] [-v] [--max-rounds N]  # 持续监测，Ctrl+C 优雅退出
python -m xianyu_alert.cli once  [-c config.yaml] [-v]                   # 只跑一轮，适合 cron / 计划任务
python -m xianyu_alert.cli list  [-c config.yaml] [--limit 50]           # 查看已提醒记录
python -m xianyu_alert.cli login [-c config.yaml] [--cookie-string "..."] # 获取 Cookie 并写入配置
python -m xianyu_alert.cli shortcut [--name "闲鱼低价提醒工具"]          # 在桌面创建快捷方式
python -m xianyu_alert.cli --version
```

Windows 计划任务 / Linux cron 示例（每 10 分钟一次）：

```cron
*/10 * * * * cd /path/to/project && /path/to/.venv/bin/python -m xianyu_alert.cli once
```

---

## 四、配置项说明（config.yaml）

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `keywords[].keyword` | str | 必填 | 搜索关键词，不可重复 |
| `keywords[].max_price` | float | 必填 | 价格阈值（正数），`price < max_price` 才提醒 |
| `keywords[].exclude_keywords` | list[str] | `[]` | v3.1 排除词：商品标题命中**任一**即跳过（子串匹配，大小写不敏感） |
| `keywords[].required_keywords` | list[str] | 自动提取 | v3.1 必含词：标题必须**全部包含**；空列表 `[]` = 不强制。未显式配置时自动从主关键词提取「数字+字母」片段（见「关键词过滤」一节） |
| `monitor.interval_seconds` | int | 600 | 监测间隔（秒），必须 > 0；v3.2 起默认 600（10 分钟），生产建议 600~900 |
| `monitor.user_agent` | str | 内置 Chrome UA | 请求 UA |
| `monitor.cookies` | str | `""` | 闲鱼登录态 Cookie；v3 起保存路径可自动 DPAPI 加密（`dpapi1:` 前缀）落盘 |
| `monitor.cookies_encrypted` | bool | `false` | Cookie 是否已加密（由保存路径自动维护，一般无需手改） |
| `monitor.cookie_pool` | list | `[]` | v3.2 多账号 Cookie 池：`[{name, cookie, enabled}]`，按轮次轮换取用（池优先、单值兜底），cookie 支持 DPAPI 密文 |
| `fetcher.type` | `mtop` \| `mock` \| `web` | `mtop` | 抓取器类型；`mtop` 为真实抓取（推荐，默认），`mock` 开发演示用，`web` 已废弃 |
| `fetcher.page_size` | int | 30 | 仅 mtop：每页拉取条数（1~100） |
| `fetcher.pages` | int | 1 | 仅 mtop：多页抓取总页数（翻页增加请求频率与风控风险） |
| `fetcher.page_sleep` | float | 2.0 | 仅 mtop：翻页之间的限速秒数 |
| `fetcher.mock_products_per_round` | int | 5 | 仅 mock：每轮生成商品数 |
| `fetcher.mock_fail_rounds` | list[int] | `[]` | 仅 mock：模拟抓取失败的轮次 |
| `storage.path` | str | `state/xianyu_alert.db` | SQLite 路径，目录自动创建；`:memory:` 表示内存库 |
| `notify.channels` | list | `[{type: console}]` | 通知通道列表，见下 |

### 通知通道参数

| type | 必填参数 |
| --- | --- |
| `console` | 无 |
| `serverchan` | `sendkey` |
| `email` | `smtp_host`、`smtp_port`、`username`、`password`、`to`（可选 `use_tls` / `use_ssl`） |
| `telegram` | `bot_token`、`chat_id` |
| `bark` | `url`（GET `{url}/{quote(msg)}`，iOS Bark 推送） |
| `webhook` | `url`（POST JSON，**企业微信机器人**，自动带 `Content-Type: application/json`；在企业微信群「添加群机器人」后复制 Webhook 地址） |

参数不完整的通道会被**自动跳过并打 warning**；若所有通道都不可用，会兜底为 `console`，保证提醒不会静默丢失。

---

## 四·五、关键词过滤：排除词 + 必含词（v3.1）

针对「搜到一堆回收商 / 贴牌杂牌帖子」的痛点，每个关键词规则下新增两个过滤字段。

### 1. 排除词 `exclude_keywords`

- 商品标题（含可能的 seller / location 字段）中若出现**任一**排除词（子串匹配），该商品**直接跳过**，不参与价格阈值检查、不会被提醒。
- 默认空列表 `[]`：**向后兼容**，现有 config.yaml 不写该字段也能照常运行。
- 典型用法：排除回收商 / 置换商帖子。

```yaml
keywords:
  - keyword: "光威 笔记本DDR4 3200 16G"
    max_price: 300
    exclude_keywords:
      - "回收"
      - "置换"
      - "收购"
      - "高价回收"
      - "收"
```

> 说明：预置排除词（默认 回收 / 置换 / 收购 / 高价回收 / 收）作为**模板**在 GUI 添加新关键词时自动预置
> （必含词留空由用户自填），并可在编辑弹窗中增删。
> **v3.5 起预置词可配置可持久化**：config.yaml 顶层 `preset_exclude_keywords`（缺省回退默认值）；
> GUI「编辑预置排除词」弹窗可增删（每行一个），保存后写回 config；
> 添加新关键词 / 点「添加预置排除词」时自动带上**当前配置**的预置词；
> 显式配置空列表 `[]` = 关闭自动预置。是否保留太宽的词（如「收」）由用户决定。

### 2. 必含词 `required_keywords`

- 商品标题**必须包含全部**必含词，任一缺失即跳过。
- 空列表 `[]` = 不强制要求（等同关闭该过滤）。
- **自动提取默认值（config 解析层）**：未显式配置该字段时，用正则从主关键词提取「数字+字母」片段
  （如 `16G`、`3200`、`DDR4`、`8GB`）作为默认必含词。例如
  `光威 笔记本DDR4 3200 16G` → 自动得到 `["DDR4", "3200", "16G"]`，
  保证搜「16G」时不会把 8G 套装当结果提醒。
  - 纯中文词（如「笔记本」）不提取，避免过滤过严；
  - 纯单个数字（如「第4号」的 `4`）无区分度，丢弃；
  - **v3.3 起 GUI 添加新关键词时必含词留空**（不再自动写入），由用户在编辑弹窗中自行填写；
    config 解析层对旧配置（未显式写该字段）仍保留自动提取，保证向后兼容；
  - 若自动提取导致误杀合法商品（例如你其实接受 8G×2=16G 套装），
    在配置里**显式写** `required_keywords` 手动增删即可。

```yaml
keywords:
  - keyword: "光威 笔记本DDR4 3200 16G"
    max_price: 300
    required_keywords: ["16G", "DDR4", "3200"]   # 也可写 [] 关闭，或删掉本字段走自动提取
```

### 3. 执行顺序与匹配规则

- 过滤发生在**抓取之后、价格阈值检查之前**（fetcher 保持纯抓取，过滤是业务规则）；
- 匹配大小写不敏感（统一 lowercase 后比较）：`16G` / `16g` 等价；
- 优先级：**必含词缺失 → 跳过；排除词命中 → 跳过**；二者可叠加；
- 被过滤的商品不进入「新商品」判定与已见记录，后续放宽规则时仍可按新商品提醒。

### 4. GUI 操作

「监控配置」页的关键词表格新增「排除 / 必含」摘要列：

- 选中某行 → 点「编辑排除/必含词」→ 弹窗中每行一个关键词编辑排除词 / 必含词 → 保存；
- 选中某行 → 点「添加预置排除词」→ 一键追加**当前配置**的预置词（默认 回收 / 置换 / 收购 / 高价回收 / 收）；
- 点「编辑预置排除词」→ 弹窗中每行一个定制预置词模板 → 保存后写回 config（v3.5，持久化）；
- **v3.3 起**新增关键词时会**自动预置排除词**（v3.5 起为**当前配置**的预置词）、**必含词留空**由用户自行填写（可在编辑弹窗中改）。

---

## 五、工作原理

```
每轮监测（对每个关键词）：
  1. fetcher.fetch(keyword)                       -> 本轮商品列表
  2. 关键词过滤（v3.1）                            -> 排除词命中 / 必含词缺失的商品直接跳过
  3. storage.get_previous_round_ids(keyword)      -> 上一轮商品 ID 集合
  4. new  = 过滤后本轮中不在上一轮集合里的商品      -> 「新出现」
  5. hit  = new 中 price < max_price 且 未提醒过   -> 「需要提醒」
  6. 逐个通知通道发送 hit -> storage.mark_notified
  7. storage.save_seen(过滤后的本轮商品)
     storage.set_previous_round_ids(过滤后的 ID 集合)  -> 供下一轮比对
```

**双保险设计**：
- 「上一轮 ID 集合」负责判断是否**新出现**（存 `meta` 表，跨重启有效）；
- 「`notified` 标志」负责**永久去重**（存 `product` 表，唯一约束 `(keyword, product_id)`）。

### 数据表结构

- `product(id, keyword, product_id, title, price, url, publish_time, first_seen, last_seen, notified)`，唯一约束 `(keyword, product_id)`
- `meta(key, value)`，`key = prev_ids:<关键词>`，`value` 为 JSON 数组

---

## 六、项目结构

```
config.yaml              # 演示配置（默认 mock，可直接运行）
config.example.yaml      # 完整配置模板（含所有通知通道示例）
icon.ico                 # 应用图标（从原 exe 提取，见 build/extract_icon.py）
requirements.txt
requirements-cookie.txt  # 可选依赖：Cookie 半自动获取（Playwright）
requirements-build.txt  # 构建期依赖：pyinstaller（仅打包需要）
README.md
xianyu_alert/
  __init__.py            # 版本号 1.5.0
  paths.py               # frozen/源码路径统一解析（exe 同目录 config/state）
  secure.py              # Cookie DPAPI 加密 / 解密 / 脱敏（零依赖）
  models.py              # Product 数据类
  config.py              # YAML 加载与校验（含 v3.1 排除词 / 必含词解析）
  filters.py             # v3.1 关键词过滤纯函数（排除词 / 必含词 / 自动提取）
  fetcher.py             # Fetcher / WebFetcher / MockFetcher / MtopFetcher（多页+过期检测）/ FetchError
  storage.py             # SQLite 去重与状态存储
  notifier.py            # Notifier / Console / ServerChan / Email / Telegram / Bark / Webhook + 工厂
  monitor.py             # 核心监测循环（含 Cookie 启动预检、v3.1 关键词过滤）
  cookie.py              # Cookie 获取（Playwright 半自动 / 终端粘贴）、过期检测、加密保存
  shortcut.py            # 桌面快捷方式创建（PowerShell 安全转义）
  cli.py                 # argparse 命令行入口
  gui.py                 # Tkinter 图形界面（六态状态灯 / 空状态引导 / 新通道表单 / 快捷方式 / 关于 / v3.1 过滤编辑）
build/
  entry.py               # 打包入口：带参数走 CLI，无参数启动 GUI
  闲鱼低价提醒工具.spec  # 标准版 PyInstaller 配置（排除 playwright）
  build_full.spec        # 完整版 PyInstaller 配置（含 playwright）
  build.bat / build_full.bat  # 一键构建脚本
  extract_icon.py        # 图标提取脚本
tests/
  test_models.py
  test_storage.py
  test_monitor.py
  test_notifier.py
  test_cookie.py
  test_paths.py
  test_secure.py
  test_fetcher_v3.py
  test_gui.py
  test_gui_v3.py
  test_shortcut.py
```

---

## 七、运行测试

测试全部使用 **MockFetcher + 内存 SQLite + mock 的网络请求**，不访问外网：

```bash
python -m unittest discover -s tests -v
```

覆盖内容：模型校验、SQLite 去重与持久化、四要素通知格式、Server酱/Telegram/邮件/Bark/Webhook 请求构造、监控主链路（新商品判定 / 阈值过滤 / 去重 / 抓取失败容错 / 多关键词隔离）、**Fernet Cookie 加密（fernet1: 跨平台 + dpapi1: 遗留前缀兼容）**、mtop 多页抓取、路径 frozen 适配与 **macOS 数据目录（XY_DATA_DIR / Application Support）**、GUI 纯函数、**Qt（PySide6）offscreen 逻辑测试**、快捷方式转义。

---

## 八、打包为独立 exe（Windows，环境无依赖）

### 8.1 两个版本的区别

| | 标准版（✅ 主推） | 完整版（可选） |
| --- | --- | --- |
| 构建配置 | `build/闲鱼低价提醒工具.spec` | `build/build_full.spec` |
| 产物 | `dist/闲鱼低价提醒工具.exe` | `dist/闲鱼低价提醒工具_完整版.exe` |
| 体积 | **15~25MB**（排除 playwright） | 100MB+（含 playwright） |
| 取 Cookie | 手动粘贴 / `login --cookie-string` / GUI 引导（**本就支持**） | 额外支持 GUI「打开浏览器登录」自动获取 |
| 环境要求 | **双击即用**，无需装 Python / 依赖 | 目标机仍需执行一次 `playwright install chromium`（浏览器内核约 300MB，不由 PyInstaller 打包） |
| 适用 | 绝大多数普通用户 | 想要自动登录体验、且接受一次性装浏览器内核的用户 |

> 结论：**默认交付标准版**。「环境无依赖」承诺只有标准版能兑现。

### 8.2 构建命令

```bash
# 一次性安装构建依赖
python -m pip install -r requirements-build.txt

# 标准版（主推）
python -m PyInstaller "build/闲鱼低价提醒工具.spec" --noconfirm
# 或直接双击 build\build.bat

# 完整版（可选）
python -m PyInstaller "build/build_full.spec" --noconfirm
# 或直接双击 build\build_full.bat
```

图标：`icon.ico` 从原 exe 提取（`build/extract_icon.py`，复用 RT_ICON 资产）。

### 8.3 双击使用方法

1. 把 `dist/闲鱼低价提醒工具.exe` 复制到任意目录（或直接双击）；
2. 首次启动 GUI，在「监控配置」页点击 **Cookie 管理** → **❓ 如何获取 Cookie？** 按手动步骤（登录 goofish.com → F12 → Network → 搜词 → 找 h5api.m.goofish.com 请求 → 复制含 `_m_h5_tk` 的 Cookie 头）粘贴保存；（标准版不含 playwright，自动登录入口已移除，也可用 `python -m xianyu_alert.cli login`）
3. 添加关键词与价格阈值、勾选通知通道（含 Bark / Webhook）；
4. 点击 **开始监控**。

路径说明（v3 frozen 适配）：
- `config.yaml` 生成在 **exe 同目录**；
- 状态库与日志落在 **exe 同目录 `state/`**（`state/xianyu_alert.db`、`state/xianyu_alert.log`）；
- Cookie 保存时自动 **DPAPI 加密**（`dpapi1:` 前缀），换机/换用户需重新登录。

命令行能力（同一 exe）：
```bash
闲鱼低价提醒工具.exe --version
闲鱼低价提醒工具.exe --help
闲鱼低价提醒工具.exe once --config config.yaml   # 跑一轮（windowed 输出见 state\xianyu_alert_cli.log）
闲鱼低价提醒工具.exe shortcut                      # 在桌面创建快捷方式
```

---

## 八·五、macOS M4 原生适配（PySide6 Qt 版 .app）

> 设计文档：`docs/macOS适配设计文档.md`　|　用户验收：`docs/macOS用户验收清单.md`

### 8.5.1 概览

- **GUI 路线**：macOS 端使用 **PySide6（Qt for macOS）**，新增 `xianyu_alert/gui_qt/` 包；
  Windows 端 Tkinter 原样保留（零回归），入口按 `sys.platform == "darwin"` 分发。
- **数据目录**：`paths.data_dir()` —— `XY_DATA_DIR` 环境变量优先 → frozen+darwin 落
  `~/Library/Application Support/闲鱼低价提醒工具/` → frozen 其它平台落 exe 目录 → 源码落项目根。
  `config.yaml / state/ / secret.key / 日志` 统一锚定 data_dir。
- **Cookie 加密**：DPAPI → **Fernet**（cryptography 库），跨平台加解密。新密文前缀
  `fernet1:`；`dpapi1:` 老密文识别为「无法解密 → 请重新登录」，不保留 DPAPI 代码。
  对外 4 函数签名不变，调用点零改动。
- **快捷方式**：`shortcut.supported()` 仅 Windows 返回 True；macOS GUI 不渲染该按钮，
  CLI `shortcut` 子命令提示不支持（.app 拖入「应用程序」/ Dock 即用）。

### 8.5.2 在 M4 Mac 上构建 .app

```bash
cd ~/xianyu-alert
bash build/macos_build.sh          # venv + 依赖 + PyInstaller + ad-hoc 签名 + 自检
# 产物：dist/闲鱼低价提醒工具.app（arm64，150~300MB）
# 数据目录：~/Library/Application Support/闲鱼低价提醒工具/
```

图标：`build/make_icns.py`（ico→iconset PNG，Pillow）+ `build/make_icns.sh`（iconutil 合成 icns，
仅 macOS）。Info.plist 已含 `NSAppSleepDisabled=true`（防 App Nap，7×24 挂机关键）。

### 8.5.3 可选开机自启（LaunchAgent）

```bash
bash scripts/install_launchagent.sh      # 登录自启 + 崩溃拉起
bash scripts/uninstall_launchagent.sh    # 卸载
```

### 8.5.4 源码模式在 macOS 上运行

```bash
pip install -r requirements.txt
pip install -r requirements-macos.txt    # PySide6>=6.6
python -m xianyu_alert.cli gui           # macOS 自动走 Qt GUI
```

> 注意：GUI 挂机与 headless daemon（`cli run`）**不可同时运行**（SQLite 单写者）。

---

## 九、⚠️ 重要局限说明（务必阅读）

闲鱼（goofish.com）是**强反爬 + 前端 JS 渲染**的站点，`WebFetcher` 属于「尽力而为」实现：

1. **数据由 JS 异步加载**：搜索结果主要通过带签名（`sign` / `_m_h5_tk`）的 mtop 接口拉取，纯 `requests` 得到的 HTML 往往只有页面骨架，**大概率解析不到商品卡片**。
2. **需要登录态**：未携带有效 Cookie 时，接口容易直接返回风控页或空数据。请在 `monitor.cookies` 填入浏览器登录后的 Cookie。
3. **页面结构会变**：站点 DOM / 内联 JSON 字段随时可能调整，解析选择器可能失效。
4. **频率限制**：`interval_seconds` 不建议小于 300 秒，过于频繁会触发风控甚至封号。

程序对此做了优雅降级：
- 网络异常 / 非 200 / 重试耗尽 → 抛 `FetchError`，被 `Monitor` 捕获，记录 warning 并继续处理其它关键词，**不会崩溃退出**；
- 请求成功但解析为空 → 返回空列表并打 warning 提示检查 Cookie。

**如需稳定的生产级抓取**，建议自行替换 `WebFetcher` 实现：
- 使用 Playwright / Selenium 驱动真实浏览器渲染；
- 或抓包闲鱼 App / H5 的 mtop 接口并实现签名算法。

只需继承 `xianyu_alert.fetcher.Fetcher` 并实现 `fetch(keyword) -> List[Product]`，即可无缝接入现有的筛选、去重与通知链路。

---

## 十、免责声明

本工具仅供个人学习与自用监测，请遵守目标站点的 robots 协议与服务条款，合理控制请求频率，勿用于商业爬取或对站点造成压力。
