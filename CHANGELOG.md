# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，条目按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 归类。

## [1.11.4] - 2026-10-10

### 新增
- **~monitor.autostart~（默认 ~false~）**：服务启动后自动开始监控。修掉可用性缺口 ——
  容器 / 进程重启后 monitor 不会自己恢复，页面上显示"未运行"，用户往往以为它还在盯盘。
- **请求节奏可观测**：新增 ~xianyu_alert/reqmeter.py~（进程级请求计量），每次真实出网
  （含重试与保活）都记账；~/healthz~ 与 ~/api/monitor/status~ 新增 ~requests~
  （累计 / 近 10 分钟 / 近 1 小时 / 最近一次时间），系统页新增「请求节奏」一行。
- **UA 与环境一致性**：默认 UA 同步到当前 Chrome 主版本；文档明确建议把
  ~monitor.user_agent~ 设成**拿 Cookie 那个浏览器**的 ~navigator.userAgent~
  （Cookie 里的风控指纹由该浏览器生成，UA 声明另一个系统会构成环境矛盾）。

### 变更
- **校验在架限流收紧**：单次上限 30 → **20 条**、请求间隔 1.5 → **3 秒**
  （这是唯一会连发请求的手动操作）；Qt 版此前是另一份硬编码副本，已同步并加断言防漂移。

### 测试
- 新增 ~tests/test_account_safety.py~（13 例）：请求计量窗口 / 抓取器记账 / autostart /
  UA 默认值与自定义 / 校验在架限流常量一致性。

## [1.11.3] - 2026-10-10

### 修复（账号安全加固 —— 来自一次全面排查，详见 账号安全排查报告_2026-10-10.md）
- **监测间隔硬下限**：`monitor.interval_seconds` 小于 **120** 时收敛到 120 并打 WARNING；
  Web / Tk / Qt 三端输入校验直接拒绝保存更小的值。此前只要求 > 0 —— 界面上把 600 改成 1
  也是"合法"的，等于 26 万次请求/天（账号最大的人为风险点）。
- **风控熔断（新增 `xianyu_alert/risk.py`）**：进程级熔断器，一旦观测到 RGV587：
  - 本轮**立刻停止抓取剩余页**（此前会继续把第 2、3 页打完）；
  - 本轮**不再抓下一个关键词**；
  - 进入冷却：首次 `间隔 x 3`（下限 300 秒），连续命中翻倍，上限 6 小时；
  - 冷却期内**监控轮次 / 保活探测 / 校验在架一律静默**（一个请求都不发），结束后自动恢复。
- **关键词间限速**：多个关键词之间按 `fetcher.page_sleep` 间隔（单次上限 5 秒），
  修掉"关键词一多就是背靠背请求"；只对真实抓取器（mtop / web）生效。
- **风控可观测**：`/healthz` 与 `/api/monitor/status` 新增 `risk`
  （active / remaining_seconds / hits / last_detail），系统页新增「风控熔断」一行。

### 变更（默认行为）
- 命中风控时**整轮放弃**（不再返回已抓到的部分页结果）—— 被限流时"继续消费数据"
  没有意义，把风控显式抛给上层才能开启冷却。

### 测试
- 新增 `tests/test_risk_guard.py`：熔断时长与上限、单轮中止、整轮跳过、关键词间限速、
  保活静默、校验在架拒绝、状态暴露、间隔下限，共 20 余个用例。

## [1.11.2] - 2026-10-10

### 修复（生产实证：保活重试风暴把账号打进风控）
- **保活失败后不再 30 秒重试一次**：`CookieKeeper` 增加失败冷却 —— 一次失败后至少等
  `max(interval, 300s)` 再试。此前失败不推进 `last_auth_at`，30 秒巡检立刻重试；
  被 RGV587 风控时等于自己制造请求风暴（线上日志实测：11:17:40 / 11:18:16 / 11:18:27 连续重试）。
- **保活只抓 1 页**：此前沿用 `fetcher.pages`（默认 3 页），一次探测就是 3 个请求；
  保活只需要一次鉴权请求，现在把 pages 覆盖为 1（该覆盖对真实抓取轮次无影响）。
- **保活优先挑启用中的关键词**：此前固定用 `keywords[0]`，若该词被用户停用（线上真实情况：
  `4080S 32G` 已停用），保活就等于对着「明确不想抓」的商品猛抓。

### 测试
- 测试 1345 → **1350**：新增失败冷却（失败挡住下一次巡检 / 成功不设冷却 / 冷却到期恢复）
  与探针目标（优先启用词、只抓 1 页、全停用时兜底）共 5 个用例。

## [1.11.1] - 2026-10-10

### 修复
- **「64G」其实想买两根 32G**：用户写 `64G` 的真实意图是**单条 32G × 2 条**，
  而闲鱼会把「16G×2 共32G」「16G×4 共64G」这类 16G 条子也搜出来（标题里确实写着
  32G/64G），v1.11.0 的总容量口径挡不住它们。
- 新增**模块规格**判定：关键词里写显式组合（`32G×2` / `2×32G` / `4根16G`）时，
  解析出「**单条容量**」与「**条数**」并据此判定：
  - 单条取组合里的容量，**标题另外出现的裸容量多半是总容量** —— 「光威天策 DDR4 3200
    32G（16G×2）」的单条是 16G 而不是 32G；
  - 「共64G / 一共32G」这类**总量**恒不计入单条容量（真实数据「单根16G，四根共64G」
    必须读成 16G×4 条，才能挡住它）；
  - 条数认「两条 / 两根 / 俩条 / 一对」与「单条 / 一根」，并**容忍被插入的空格**
    （真实标题里的「四 根共64G」）；
  - 单条或条数不足分别给出 `spec_module` / `spec_module_count` 原因码。
- **搜索词与规格分离不了时，直接写进关键词**：实测三个搜索串各抓满 3 页能召回的
  真·两根 32G 光威 DDR4-3200 商品数 —— `光威 3200 64G` 11 条 > `光威 3200 32G×2`
  8 条 > `光威 3200 32G` 7 条，且加 `×2` 不影响召回。因此推荐关键词
  `光威 3200 64G 32G×2`：**保留 64G 的召回，同时表达单条 ≥32G × 2 条**。

### 测试
- `tests/test_spec_match.py` 41 个用例：新增模块解析单测、真实语料下的
  「两根 32G」意图用例（含 16G×2 / 16G×4 / 「出一根」三类必须挡住的反例），
  以及**同一批 71 条线上语料在模块规格下只留 3 条真 2×32G** 的回归断言。

## [1.11.0] - 2026-10-10

### 新增
- **规格语义过滤（`xianyu_alert/spec_match.py`）**：把**搜索关键词本身**当规格逐条判定 ——
  品牌**锚定**（目标品牌必须是标题里最先出现的品牌）、代际、频率 / 型号、容量**下限**。
  默认开启，顶层 / 单关键词 `spec_filter` 均可关闭。
- `tests/test_spec_match.py`：除纯函数单测外，含**线上真实语料回归** ——
  关键词「光威 3200 64G」的 71 条命中标题逐条断言是否应当命中；期望值由**人工审阅**给出，
  不是用被测实现反推，因此能真正抓住"过松 / 过紧"的回归。

### 修复
- **搜索词里的规格不再被无视**：关键词「光威 3200 64G」原先只按必含词「光威」过滤，
  71 条命中里混进 8G / 16G / 32G、DDR3、DDR5，甚至金士顿 / 酷兽 / 海盗船
  （竞品在标题尾部堆「关联 光威 芝奇…」骗搜索）。现在只提醒真正满足规格的 **5 条**。
- **标题被插入空格导致的漏判**：闲鱼反爬会把 `3200` 写成 `32 00`、`32GB` 写成 `3 2GB`。
  必含词 / 排除词 / 规格判定统一改为**容忍 token 内部空白**的匹配；数字修复只合并
  「不紧跟字母数字」且合并结果属于常见容量的断点 —— `DDR3 8GB` 不会被读成 38GB、
  `iPhone 15 128G` 不会被读成 15128G。
- **容量等价写法被误杀**：`32G×2` / `4×16G` / `4根16G` / `共64G` 都是 64G，
  字面要求出现 `64G` 会漏掉它们；自动必含词因此不再包含容量 token。

### 变更（默认行为，可回退）
- `keywords[].required_keywords` 未显式配置时，自动提取**不再包含容量 token**（如 `16G`）——
  容量交给容量算式判定。要恢复 v1.10 行为：给该关键词写 `spec_filter: false`。
- 品牌锚定只认**已收录品牌**（内存 / 3C 常见品牌 30+）；未收录的品牌不参与锚定（fail-open）。

### 测试
- 测试总数 1299 → **1333**；README / README_EN 的镜像标签同步到 `1.11.0`。
- `spec_filter` 关闭开关经 GUI / Web 表单**保真往返**（Tk / Qt / Web 三条保存路径 + 对应回归测试），
  不会出现"配置文件里写了 false、界面保存一次又变回 true"的静默重置。

## [1.10.14] - 2026-10-10

### 文档
- **README 新增「升级到新版本（日常）」**：四步升级命令（备份 → 改 tag → `pull` + `up -d --force-recreate` → 用 `/healthz` 确认版本），
  并解释两个关键点：
  - compose 的镜像 tag 是**钉死**的，所以 `docker compose up -d` **不会**自动升级 ——
    这正是「发了新版本但机器上还是旧版」的根因；
  - 只改 tag 必须配 `--force-recreate`，否则 `up -d` 可能判定"配置未变"而复用旧容器。
- 同时说明升级**不需要手改配置**：新版本会补齐默认值、给数据库幂等补列，
  `state/` 与 `secret.key` 原样保留。

### 说明（现场处置记录）
- 本次据此把一台 NAS 上的部署从 **1.9.4 升到 1.10.13**：备份数据 → 改 compose tag →
  `docker compose pull && up -d --force-recreate` → `/healthz` 回显 1.10.13 且 healthy；
  83 条提醒记录 / 59 条已提醒完整保留、日志 0 ERROR。
- 该实例日志里还观察到 v1.9+ 的令牌自愈在生产环境生效：
  `FAIL_SYS_TOKEN_EXOIRED` → 自动用新 token 重算签名并重试成功。

## [1.10.13] - 2026-10-10

### 文档
- **README 全面刷新**（内容此前滞后于实际能力近 10 轮）：
  - 配置要点表**补 6 个真实存在的配置项**：`monitor.keepalive_enabled` /
    `monitor.keepalive_interval_seconds`、`notify.quiet_hours` /
    `notify.aggregate_seconds` / `notify.retry_attempts`，并注明"静默与聚合默认关闭，
    升级后行为与之前一致"。
  - 测试数 **934 → 1299**；CI 描述改为 **7 项必过检查**表格
    （Lint/类型 · 三平台矩阵 · Qt offscreen + 覆盖率门禁 · Web 契约 + 86 项冒烟 + 62 项 DOM e2e · 覆盖率门槛）。
  - 新增「**免扫码自动续期**」一节：持久化浏览器 profile 的三步做法，
    以及 v1.9 四层凭据模型为何不会把"可自愈的令牌过期"误报成"需要重新登录"。
  - 新增「**密钥轮换**」一节：`secure status` / `secure rotate` 的用法与安全语义
    （先用旧密钥解密，失败即整体中止，自动备份）。
  - 核心价值补两条：**通知可控**（静默/聚合/重试）与**凭据可轮换**；命中记录补卖家 / 地区 / 原价。

### 修复（运维补推）
- v1.10.10 ~ v1.10.12 的 **Docker Hub 与 GHCR 镜像**在 Docker Hub 429 限额恢复后**全部补推成功**。
  澄清：`release.yml:build-docker`（推 GHCR）与 `docker-publish.yml`（推 Docker Hub）**不是重复构建**，
  而是双注册表发布；两者同时失败只是因为都要从 Docker Hub 拉基础镜像。

## [1.10.12] - 2026-10-10

### 测试（M16 Qt worker + M15 Tk 守卫）
- 新增 `tests/test_gui_qt_workers.py`（8 条）：`MonitorWorker.run()` 的
  **命中投递 / 单轮异常不终止 / 启动失败不崩窗 / 停止信号响应 / 资源必然关闭**，
  以及循环模式下"等待间隔时被打断"的即时退出；`LogBridge` 的跨线程投递。
  **`gui_qt/workers.py` 35% → 64%**，`gui_qt` 整体 **65% → 67%**。
- `tests/test_gui_handlers.py` 增补 8 条**守卫分支**用例（Tk，不需真实窗口）：
  校验在架的四种拒绝路径（运行中 / 无记录 / 非 mtop / 配置有误）、
  标记售出与加黑名单的"未选中 / 缺商品 ID"提示契约。
  **`xianyu_alert/gui/app.py` 54% → 57%**。

### 说明
- v1.10.10 起的 **Docker 镜像推送**仍受 **Docker Hub 429** 阻塞（多次重跑均失败）。
  已确认这不是仓库配置问题：workflow 先登录再构建，属于共享出口的限额；
  本机与 NAS 都无法代推（NAS 无 arm64 模拟且未登录 Docker Hub）。
  影响范围：容器用户暂时拉不到新标签；Windows/macOS 资产与 GitHub Release 正常。

## [1.10.11] - 2026-10-10

### 测试（M18 前端契约 + M24 脚本）
- **e2e 冒烟断言 68 → 86 项**：补上 v1.9~v1.10 新增能力的契约 ——
  `/healthz` 的**部署形态快照**（字段齐全、kind=custom、env_override、
  与 data_dir 一致）、`/api/config` 的 **cookies_masked 脱敏**与
  `cookies_undecryptable`、`/api/cookie/status` 的**分层诊断**
  （diagnosis / keepalive 结构）、`/api/cookie/pool` 键齐全、
  以及**非法 JSON body 必须返回 JSON 信封而不是 HTML 堆栈**。
- 新增 `tests/test_scripts.py`（9 条，M24）：文档检查器 subprocess 级 smoke、
  **「scripts/ 下每个脚本都必须在 scripts/README.md 登记」** 的治理检查、
  `run.sh` 可执行性与未知子命令契约、e2e `Report` 的计数逻辑单测。

### 修复
- 治理检查**当场咬出一个真实缺口**：`scripts/check_docs.py`（CI 文档门禁实际调用的脚本）
  **既没登记也没进统一入口**。已在 `scripts/README.md` 补登记，并同步更新 e2e 断言数说明。

### 说明
- v1.10.10 的 **Docker 镜像未能推送**：`Build Docker image` 作业连续多次因
  **Docker Hub 429（限流）** 失败 —— 该 workflow **已先登录再构建**，属于共享出口的
  整体限额，非仓库配置问题。GitHub Release 与 Windows/macOS 资产不受影响。
  处置：稍后重跑该作业；或参考 README 在自有机器上 `docker buildx build --push`。

## [1.10.10] - 2026-10-10

### 变更
- **Cookie 池操作抽成纯函数（M15 / M16 共用）**：新增 `pool_toggle_entry` /
  `pool_expired_indexes` / `pool_disable_indexes` / `pool_delete_entry` /
  `pool_upsert_entry` / `pool_summary`。此前 Tk 的「Cookie 管理」对话框（`on_manage_cookies`，
  367 行）与 Qt 的 `CookieDialog` **各写了一遍**「切换启用 / 停用过期 / 删除 / 增改」，
  既难测（要 Tk / Qt 环境）又容易两边跑偏。纯函数不碰控件、**不改入参**、返回新列表，
  两个 GUI 现在共用同一套语义。

### 修复
- **统一两版「⏹ 自动停用过期项」的判定标准**：Tk 用
  `detect_cookie_health(...) not in ("ok", "expiring")`，Qt 用 `cookie_prefers_rotation` ——
  **同一个按钮在两版界面里行为不同**。按 v1.9 四层凭据模型，令牌层问题（缺 `_m_h5_tk`、
  令牌过期）**可自愈，不应被自动停用**；只有登录态缺失 / 无法解密这类服务端大概率会拒的
  才需要停用。现已统一为后者（Tk 侧之前会把"仅缺令牌"的条目也停掉）。

### 测试
- 新增 `tests/test_cookie_pool_ops.py`（22 条）：切换 / 过期判定 / 批量停用 / 删除 / 新增与替换 /
  概况统计，逐条断言**不可变性与越界安全**，并把「什么算需要停用」的口径写进文档字符串。
- 全量 1252 → **1274**。

### 说明
- 抽纯函数后，两个 GUI 的对话框回调各减少 4~6 行重复逻辑；Tk 侧改动由 CI（xvfb）验证。

## [1.10.9] - 2026-10-10

### 修复
- **Qt 版「编辑筛选」按钮点了就崩**：`MonitorConfigTab._on_edit_filters` 调用
  `table_keywords.update_row(kw, price, ...)`，而签名是
  `update_row(old_keyword, new_keyword, price, enabled, summary)` —— **少传一个新名参数**，
  调用即 `TypeError: missing 1 required positional argument: 'price'`。
  这是本轮为该方法补测试时**当场暴露**的（此前 Qt 版这条路径没有任何用例）。
- `KeywordEditDialog.result()` 改为**全函数**：此前只在点过「确定」后才设置 `_result`，
  未确定就调用会抛 `AttributeError`；现在以构造入参兜底（语义即「未编辑时返回原值」）。

### 测试（M16：Qt 版用例与覆盖率）
- 新增 `tests/test_gui_qt_dialogs.py`（27 条）：关键词编辑对话框（校验/回传/脏价格）、
  预置词 / 通道 / 黑名单原因 / 刷新 Cookie 对话框、Cookie 池对话框（校验规则 / 增改 / 回传）、
  配置页签的增删改与开关（含「编辑筛选」——正是上面那个 bug）、MonitorWorker 生命周期。
- **`gui_qt` 覆盖率 55% → 65%**（dialogs 54%→82%、tab_config 56%→68%、widgets 73%→85%）。
- **CI 新增 Qt 覆盖率门禁**：此前 gui_qt 只有用例数、没有数字门禁（主门禁环境无 PySide6，
  该包被排除）。现在在装了 PySide6 的作业里单独度量并要求 **≥ 60%**。

### 说明
- 本地一度用 `timeout` 跑测试，但 macOS 没有该命令；改用「后台运行 + 看门狗 kill」。
- Qt 测试**必须屏蔽 QMessageBox**：本轮第一次运行正是被未屏蔽的模态框挂住（本项目早期踩过同样的坑）。

## [1.10.8] - 2026-10-10

### 修复
- **配置顶层不是映射时抛出可读的 ConfigError**：`load_config` 此前只挡了"文件为空"，
  若 YAML 顶层是列表/标量，会一路走到 `raw.get(...)` 抛 `AttributeError` ——
  用户看到的是 Python 堆栈而不是"配置哪里写错了"，与本模块"精确报错"的契约不符。
  现在报：`配置文件顶层必须是映射（键值对形式），当前是 list：<路径>`。

### 测试（覆盖率补强，M06 / M10 / M11）
- 新增 `tests/test_config_parsing.py`（13 条）：表驱动钉住**每个字段的报错文案**
  （keywords / monitor / fetcher / notify / cookie_pool），以及少数"回退 + 告警"的点
  与文件级路径。**`config.py` 81% → 87%**。
- 新增 `tests/test_singleton_edges.py`（16 条）：权限失败放行 / `strict=True` 抛出 /
  被占用 / 探测失败保守判忙 / 释放与持有者查询。**`singleton.py` 80% → 94%**。
- 新增 `tests/test_cookie_capture.py`（9 条）：用**注入的假 playwright 包**把
  "免扫码自动续期"整条链路离线跑通（未装依赖 / 无 profile 快速失败 / 启动失败 /
  目录不可建 / 正常取到 `_m_h5_tk` / 等满超时）。**`cookie.py` 75% → 87%**。

### 说明（本轮踩的坑）
- 写假 playwright 包时**必须给 `__path__`**：否则真 playwright 的 `sync_api` 在被导入时
  会去反查 `playwright._impl`，撞上假包报 `AttributeError`（环境里其实装着真包）。
- 配置测试第一版按"脏数据一律回退"写，跑出来 11 个失败 —— 实际契约是**结构性错误直接明确报错**。
  这提醒：**先写期望再对齐实现**时，期望必须来自真实行为。

## [1.10.7] - 2026-10-10

### 变更
- **测试文件按模块重组（M20）**：26 个"版本 + QA 批次"命名的文件（`test_qa_v3_6_extra.py`、
  `test_v1_8_2_token_refresh.py` …）改为 **`test_<模块>_<方面>.py`**（如 `test_gui_blacklist.py`、
  `test_token_refresh.py`、`test_cookie_pool_rotation.py`）。文档里的旧名引用同步更新。
- **新增 `tests/README.md`**：命名规范、**新旧对照表**（可追溯历史 issue/PR）、
  四条测试约定（不依赖真实网络/时间/显示；Tk/Qt 必须探测+跳过；断言要来自已验证行为）与运行方式。

### 修复
- **Tk 测试缺守卫导致的"顺序敏感"失败**：`test_cookie_pool_rotation.py` 的
  `TestSaveBehaviorExtra` 直接调用 `tkinter.Tk()` **没有探测守卫**，而在无显示环境（本地开发机、
  slim 容器）会直接 Error。更隐蔽的是：**能否通过取决于模块的字母序**（前面的模块若已初始化过
  Tcl，这里就可能侥幸成功），于是**重命名测试文件就会莫名其妙让套件变红** ——
  本次重命名正好把它暴露出来。已统一为仓库既有模式（探测失败 → `SkipTest`），行为与顺序无关。

## [1.10.6] - 2026-10-10

### 测试（覆盖率补强，继续为"均分 9 分"补齐短板）
- 新增 `tests/test_gui_handlers.py`（16 条）：**Tk 主窗口 handler 的直接测试** ——
  用 SimpleNamespace + `__get__` 绑定，无需真实 Tk 窗口/xvfb。
  覆盖 `_set_running`（loop/once 两模式的按钮态）、`_handle_ui_message`（6 种消息分发 + 未知类型）、
  `on_clear_records`（运行中拒绝 / 用户取消 / 真清空）、`_tick`（关闭中早退 / 运行中 / 停止态）。
  **`xianyu_alert/gui/app.py` 覆盖率 49% → 53%**。
- 新增 `tests/test_cli_commands.py`（12 条）：once/list/shortcut/secure 的**真实执行**，
  以及 `main()` 的四条出口（ConfigError→2 / KeyboardInterrupt→0 / 未预期异常→1 /
  单实例冲突→2）。**`xianyu_alert/cli.py` 覆盖率 70% → 83%**。

### 说明
- 本轮测试暴露了两处**我自己写错的断言**（shortcut 在非 Windows 返回 1 是既有契约、
  run 的日志走 logging 不进 stdout），都已按实际契约改正 —— 这是"先写期望再对齐实现"的正常代价，
  但也提醒：断言必须来自**已验证的行为**，而不是想当然。

## [1.10.5] - 2026-10-10

### 变更
- **冷启动重评**：不看历史预估，直接按当前代码的客观证据（规模 / 覆盖率 / 专项用例 /
  端到端记录）对 24 个模块重新打分。结论：**均分 8.91（重评）→ 本轮补强后约 8.94**，
  **未达到 9.05 的目标**。重评报告见仓库外的《项目模块化体检与评分_14轮重评》。

### 测试
- 新增 `tests/test_web_fetcher_http.py`（13 条）：WebFetcher 的网络层此前几乎没断言 ——
  覆盖 200/非 200/异常重试/重试耗尽/URL 编码/空解析/会话关闭；
  **`xianyu_alert/fetcher/web.py` 覆盖率 56% → 100%**。
- 新增 `tests/test_keepalive.py`（19 条）：保活此前无专项文件 ——
  覆盖 due 的全部边界、tick 的三条异常路径、**线程生命周期（start/stop/循环真的在跑）**
  （正是 v1.9.1 出过 bug 的地方）；
  **`xianyu_alert/keepalive.py` 覆盖率 87% → 100%**。

## [1.10.4] - 2026-10-10

### 新增
- **部署形态声明式矩阵（M12）**：新增 `xianyu_alert/deployment.py`，把此前一串 if/else 的
  形态判断改为**可枚举的矩阵**（custom / macos-app / portable / source）：
  - 新增部署形态 = 往 `FORMS` 加一条，检测/日志/测试自动覆盖；
  - `paths.data_dir()` 改为复用矩阵 —— **单一真相**，不会再出现"加了形态却漏改"；
  - `detect_deployment()` / `describe_deployment()` 可打印当前形态与数据落点。
- **`/healthz` 返回部署形态快照**：`{"kind": "custom", "label": "自定义数据目录", "data_dir": "/app/data", ...}`
  —— NAS 上排查"数据到底落在哪"一条 curl 就够，不用再猜。

### 文档
- **README 新增「从旧版升级（root → 非 root 必读）」（M22）**：
  三步升级（备份 → 修正属主 → 起容器确认）、三种典型现象的处置对照表、
  `/healthz` 查看部署形态、以及**用 digest 固定镜像版本**做可复现部署。

### 测试
- 新增 `tests/test_deployment_matrix.py`（13 条）：矩阵不变量（唯一性/优先级/可达性）、
  逐形态断言（含"自定义卷优先于一切"）、`paths` 与矩阵同源。

## [1.10.3] - 2026-10-10

### 新增
- **数据模型补字段（M03）**：`Product` 新增 `seller`（卖家）/ `location`（地区）/
  `original_price`（原价）与派生属性 `discount_text`；
  **一律追加在字段末尾**，既有按位置传参的调用点不受影响。
- **一路打穿**：mtop 解析从 `exContent` 取出这三个字段；存储层用既有的幂等迁移机制
  补出 `seller` / `location` / `original_price` 三列（老库打开即自动补齐，老数据仍可读）；
  `NotifiedRecord` 同步扩展（含 `original_price_text`）。
- **Qt 版「黑名单管理」（M16）**：补齐此前只有「加入黑名单」、**没有查看/移除入口**的缺口；
  菜单「记录 → 黑名单管理」，可查看条目并移除选中项。移除动作通过注入回调完成，便于单测。

### 测试
- 新增 `tests/test_product_profile.py`（11 条）：字段默认值 / 位置参数兼容 / 归一化 /
  派生属性 / mtop 解析 / 落库取出 / **老库迁移补列**。
- `tests/test_gui_qt.py` 新增 5 条黑名单管理对话框用例（CI 的 Qt offscreen 任务执行）。

## [1.10.2] - 2026-10-10

### 新增
- **通知静默时段 + 命中聚合（M13）**：`notify.quiet_hours`（如 `23:00-07:00`，支持跨午夜）与
  `notify.aggregate_seconds` —— 密集命中不再逐条刷屏，夜里不再被吵醒。
  聚合/静默逻辑抽成纯状态机（`xianyu_alert/notify_policy.py`），可注入时钟完整离线测试。
- **渠道级重试（M13）**：`notify.retry_attempts`；重试次数作为**渠道实例属性**由配置注入，
  不改变 `safe_notify` 的调用签名（自定义渠道零改动）。
- **单实例锁可操作诊断（M11）**：`lock_diagnosis()` / `is_lock_stale()` ——
  区分「真被占用」与「旧版本残留的锁文件」，并给出各自处置；权限失败时日志带出恢复指引。
- **密钥轮换（M08）**：`secure.rotate_key()` 与 CLI `xianyu-alert secure rotate` ——
  生成新密钥并把配置里全部密文重加密，旧密钥与配置自动备份；另有
  `xianyu-alert secure status` 输出密钥状态与恢复指引。

### 变更
- 配置新增字段：`notify.quiet_hours` / `notify.aggregate_seconds` / `notify.retry_attempts`
  （脏数据一律回退默认，不阻断启动）。

### 修复
- `lock_diagnosis()` 早期版本会对不存在的锁文件做探测，而探测用 `O_CREAT` 会**顺手建出文件**
  （诊断函数不该有写副作用）；已改为先判存在。

### 测试
- 新增 `tests/test_resilience.py`（28 条）：锁诊断与陈旧锁、静默时段（含跨午夜与边界）、
  聚合窗口、渠道重试、配置字段容错、密钥轮换（含密文重加密、备份、失败中止）、CLI JSON。

## [1.10.1] - 2026-10-10

### 新增
- **存储层类型化记录（M09）**：新增 `xianyu_alert/records.py` ——
  `NotifiedRecord` / `SoldOutRecord` / `BlacklistEntry`；
  `Storage.list_notified` / `list_sold_out` / `list_blacklist` 改为返回这些对象。
  **保留字典式访问**（`row["title"]`），因此既有调用点零改动；新代码可用 `rec.title`
  获得类型、补全与派生属性（`price_text` / `is_sold_out` / `display_title`）。
- **配置版本号与迁移（M10）**：配置新增 `config_version`（当前 v1）；
  `migrate_config()` 识别旧版配置并补默认值 + 提示，遇到更新版本只警告不阻断
  （降级可用好过拒绝启动）。

### 变更
- **Web 层不再有裸 SQL**：`web/api.py` 里那条 `SELECT keyword FROM product ...`
  收敛为存储层具名方法 `find_keyword_by_product_id()`。
- `_parse_monitor` 抽出 `_parse_keepalive()`，长函数瘦身。

### 修复
- 记录对象的 `from_row` 早期版本误用 `list(row)`（sqlite3.Row 迭代出的是**值**而不是列名），
  会导致字段静默为空；已改为 `row.keys()` 并补注释与测试。

### 测试
- 新增 `tests/test_storage_records.py`（20 条）：记录语义（字典式 / 类型化 / 容错 / 不可变）、
  三个 list 的类型与取值、反查方法、配置版本与迁移三分支。

## [1.10.0] - 2026-10-10

### 新增
- **配置热更（M04）**：轮次边界按文件 mtime 检测变化并就地重载 —— 改关键词 / 排除词 /
  必含词 / 阈值后**下一轮立即生效**，不必重启或整体重载；热更失败自动沿用旧配置。
- **过滤原因回传（M04）**：新增 `FilterDecision`，判定同时给出原因
  （`missing_required` / `excluded`）与命中的具体词；日志打印本轮原因分布，
  排障时能直接回答「这条为什么没提醒」。
- **轮次级指标（M05）**：每轮记录耗时 / 抓取 / 过滤（含原因分布）/ 新出现 / 命中 / 失败关键词，
  经 `Monitor.metrics()` 暴露（最近 200 轮 + 累计均值）。

### 变更
- **单一时间源（M05）**：保活节拍并入主循环（`_maybe_keepalive` + `_interruptible_sleep`），
  Web 服务在监控运行期间停掉独立保活线程、停止后再恢复；CLI / Tk / Qt / Web 的构造点
  均传入 `config_path` 以启用热更。
- `run_forever` 支持外部 `stop_event` 与 `on_round` 回调（供服务层复用同一循环）。

### 测试
- 新增 `tests/test_monitor_scheduler.py`（21 条）：过滤原因与优先级、热更（含失败回退）、
  保活三条分支、可中断睡眠、轮次指标累计。
## [1.9.9] - 2026-10-10

### 重构
- **抓取层 `fetcher.py`（1451 行）拆为包** `xianyu_alert/fetcher/`：
  `constants` / `base`（Fetcher ABC / FetchError）/ `mtop_api`（**mtop 纯函数：签名、令牌、请求体、解析，无 IO**）/ `mtop` / `web` / `mock` / `factory`（build_fetcher）。
- **兼容性**：模块级 59 个名字与四个抓取器类的公开成员**零缺失**（重构前后 dir() 机械比对），
  `from xianyu_alert.fetcher import MtopFetcher, build_fetcher` 等调用点无需改动。
- 拆分时显式声明**依赖 DAG**（constants -> base -> mtop_api -> mtop -> factory），
  避免「互相引用」形成的循环导入（第一版就没绕开，已修正）。

### 文档
- 维护交接文档与开发状态指南同步到新包路径。

## [1.9.8] - 2026-10-10

### 新增
- **开机自启三平台统一**（改造前是 Windows 桌面快捷方式 / macOS 模板+手动脚本 / Linux 无）：
  - 新模块 `xianyu_alert/autostart.py`：status / enable / disable 一套接口，
    macOS 用 LaunchAgent、Linux 用 **systemd --user（无需 root）**、Windows 用启动文件夹快捷方式；
  - CLI：`xianyu-alert autostart status|enable|disable [--json]`；
  - GUI：Tk 与 Qt 均新增「🚀 开机自启」一键按钮。
- **退出语义明确**：崩溃自动拉起，**主动退出不拉起**（KeepAlive.SuccessfulExit=false / Restart=on-failure），
  避免出现关不掉的守护进程。
- `tests/test_autostart.py`（28 条）：通过注入 runner + home，**三平台行为在一台机器上离线可测**，
  含命令失败、写盘失败、无 systemd 环境等失败路径。

### 变更
- `scripts/install_launchagent.sh` 与 CLI 现在是同一份逻辑（脚本保留兼容，推荐直接用 CLI），
  避免模板与代码两套真相。

## [1.9.7] - 2026-10-10

### 重构
- **Web 后端 `monitor_service.py`（1561 行）拆为包** `web/monitor_service/`：
  `constants` / `logging_bridge`（SSE 广播与日志桥）/ `forms`（表单<->配置纯转换）/ `cookie_pool` / `shelf_check` / `keepalive` / `service`。
- **主服务类按功能拆 mixin**：`CookiePoolMixin`（530 行）/ `ShelfCheckMixin`（168 行）/ `KeepaliveMixin`（57 行），本体只剩生命周期与状态（**service.py 1561 -> 489 行**）。
- **兼容性**：模块级命名空间（56 个名字）与 `MonitorService` 的 35 个成员**零缺失**
  （重构前后 dir() 机械比对），`from web.monitor_service import get_service` 等调用点无需改动。
- **打桩目标注意**：`mock.patch.object(ms, "build_fetcher")` 需指向真正持有该名字的子模块
  （在架校验 worker 现位于 `shelf_check`，故为 `ms.shelf_check`）；仓库内 3 处测试已同步。

## [1.9.6] - 2026-10-10

### 重构
- **网页兜底采集（全项目评分最低模块）**：解析层独立成 `xianyu_alert/web_parse.py`（399 行），
  改为三级策略并带诊断报告：
  1. 内联 JSON（原有）；2. **JSON-LD / schema.org（新增，跨站点通用）**；3. DOM 卡片（原有，不依赖具体 class）。
- **选择器集中成声明式数据** `WebSelectors`（图片属性 / 标题来源 / 价格 class 关键词）：
  平台改版时只改这张表、不改逻辑，并可用 HTML 夹具离线回归。
- **共享解析原语抽到 `xianyu_alert/parsing.py`**（商品 ID / 价格 / 发布时间）：此前 mtop 与网页两条
  路径各自持有一份实现，改动容易只改一边；现在只有一份（`fetcher` 保持再导出，调用点零改动）。
- `fetcher.py` 1682 → 1451 行（删掉 156 行与解析层重复的私有实现）。

### 修复
- 修复内联 JSON 遍历顺序被栈反转的问题（商品顺序影响展示与优先级，现在按文档顺序输出）。

### 测试
- 新增 `tests/test_webfetcher.py`（19 条）：此前该模块**没有独立测试**；用例用 HTML 夹具覆盖
  三级策略、选择器注入、改版场景、诊断输出与异常输入。核心覆盖率 78.8% → **81.2%**。

### 文档
- README 补「抓取路径说明：mtop 为主、网页解析为兜底」：三级策略、改版修复方式、
  0 条时的诊断日志与排查对照表；README_EN 同步；故障排查手册补第 9 节。

## [1.9.5] - 2026-10-10

### 重构
- **gui.py（3932 行单文件）拆为 `xianyu_alert/gui/` 包**，按职责分层：
  `constants.py`（常量）/ `helpers.py`（39 个纯函数）/ `app.py`（QueueLogHandler + 主窗口类 + 入口）
  / `__init__.py`（门面）。最大单文件从 3932 行降到 2826 行。
- **兼容性**：模块级命名空间（118 个名字）与 `XianyuAlertGUI` 的 72 个成员
  **零缺失**（重构前后 dir() 机械比对），既有 `from xianyu_alert.gui import X` 无需改动。
- **打桩目标注意**：`mock.patch("xianyu_alert.gui.X")` 必须指向真正持有该名字的模块。
  主窗口逻辑现在 app.py，故为 `xianyu_alert.gui.app.X`；仓库内 33 处测试已同步。
  （`main` 例外：cli 通过门面属性动态取用，仍打 `xianyu_alert.gui.main`。）
- CI 覆盖率排除规则同步为 `*/gui/*`。

## [1.9.4] - 2026-10-09

### 修复
- **CLI 的 stdout 不再被日志污染**：logging 此前输出到 stdout，导致
  `xianyu-alert cookie status --json | jq` 这类管道消费方解析失败（容器实测发现）。
  现按 Unix 惯例：诊断信息走 stderr，stdout 只留数据。
  并新增严格回归测试：断言行内命令的**整段 stdout** 必须可被 json.loads 解析。

## [1.9.3] - 2026-10-09

### 新增
- **CLI 机器可读输出**：once / list / cookie status 支持 --json（单行 JSON，字段视为稳定接口），
  便于 cron、NAS 巡检与外部脚本消费
- **config validate**：校验配置文件并打印摘要（关键词、抓取方式、Cookie 分层状态、保活设置）；
  无效时退出码 1 并给出精确原因 —— 改完配置先验一遍
- **cookie keepalive**：查看/开关空闲保活与调整间隔（此前只能手改 YAML）
- --help 补常用示例，命令可发现性提升

### 修复
- 修复 cmd_cookie_status 的"局部导入晚于使用"陷阱：函数内后段才 import 的
  detect_cookie_health / secure，被新增的 JSON 分支提前引用 —— 配了 Cookie 池再跑
  --json 会直接 UnboundLocalError（由 ruff F823/F821 抓出，已提前到函数顶部导入）

## [1.9.2] - 2026-10-09

### 修复
- **保活探测的令牌落盘**：keepalive_probe 使用一次性 fetcher，服务端在响应里下发的
  新 _m_h5_tk 只留在这个临时实例内存中，导致"会话其实已被续期、本地却一直显示令牌已过期"。
  现复用与正常轮次相同的 **节流落盘**（_persist_refreshed_token），NAS 实测保活成功后
  令牌剩余时间立即由负数变为数小时

## [1.9.1] - 2026-10-09

### 修复
- **保活线程接线修正**：Web 入口此前自建了一个 MonitorService 临时实例并在其上启动保活，
  而 API（get_service）用的是进程内单例 —— 导致保活虽然启动，但 /api/cookie/status
  读到的却是"未运行"（状态误报）。改为入口与 API 共用同一单例
- **GUI 校验规则统一到 v1.9 语义**（顺带修复 CI 的 Qt 任务连续卡死）：可用性放宽后，
  Qt/Tk 对话框会接受此前被拒的输入，测试随即走到未 patch 的模态对话框上无限阻塞；
  现统一用 cookie_accept_state()（只有"未配置 / 密文无法解密"拒绝），
  "自动停用过期项"改用 cookie_prefers_rotation()（令牌过期可自愈，不再自动停用）

## [1.9.0] - 2026-10-09

### 新增
- **Cookie 分层凭据模型**（xianyu_alert/credential.py）：把"登录"拆成四层分别判定 ——
  登录态（cookie2/unb/sgcookie）、会话凭据（havana_lgc_exp，**实测 30 天**）、
  签名令牌（_m_h5_tk）、风控指纹；新增 GET /api/cookie/status 与「监控配置」页的分层展示，
  界面不再把"令牌过期"笼统地报成"Cookie 失效"
- **空闲保活**（xianyu_alert/keepalive.py，默认开启，每 30 分钟）：与是否在跑监控解耦，
  只要服务在运行就定期发一次轻量请求维持令牌滑动续期 —— "抓一次就停、几小时后要重新登录"的
  场景不再出现；配置项 monitor.keepalive_enabled / keepalive_interval_seconds

### 变更
- **修正 _m_h5_tk 内嵌时间戳语义**：它是**过期时刻**，不是签发时刻。旧实现按"签发时刻 + 固定
  90 分钟"计算，会把真实剩余时间多算一个 TTL（界面显示成"4 小时"），并在真实过期后继续判为有效
- **令牌过期不再阻断抓取**：mtop 令牌可由服务端在下次请求下发新值并自动重试续期，因此
  Cookie 池轮换、单值回退、保存校验都不再因"令牌过期"剔除条目；只有登录态缺失 /
  会话凭据过期 / 密文无法解密才判为不可用
- **Cookie 池降级提醒**只看"真正需要重新登录"的条目，令牌层问题不再触发降级告警
- 默认令牌有效期估值 90 分钟 -> 4 小时（以实测为准，仅作兜底文案）

### 修复
- 修复预检把可自愈的"令牌过期"当成不可用，导致整轮抓取在配置层就失败的问题（真实故障复盘：
  容器日志出现"池中所有 Cookie 已过期/无效…本轮抓取将失败"，而此时登录态实际仍有 30 天）

## [1.8.5] - 2026-10-09

### 新增
- 顶栏新增明确的**「开始监控 / 停止监控」按钮**：此前只有可点击的状态胶囊，用户看不出能点，
  导致"只能开始、找不到停止"的误解；现在按钮文案随运行状态切换，状态胶囊保留为指示器

### 变更
- **内容区宽度自适应**：上限由写死的 1080px 改为 min(1720px, 96vw)（可用 CSS 变量 --shell-max 单点调整），
  2194px 视口下左右留白由约 557px 降至约 237px；错误条 / 未保存条 / 页脚同步收紧留白
- **命中战果宽屏多列**：视口 >=1280px 时列表改为自适应网格（每列最小 620px），
  用宽度换"一屏看更多"，而不是把单行拉长；总览页的小列表保持单列

## [1.8.4] - 2026-10-09

### 新增
- **商品大图查看**：命中战果缩略图支持**悬停预览**（桌面端浮出中图，_800x800 规格）与**点击全屏大图**（原图，
  实测样例 1272x1696，自适应视口 92vw/84vh）；大图内可用左右方向键在同一列表的商品间连续翻看、Esc 关闭；
  列表缩略图改用 CDN 小图变体（约 7KB，原图约 70KB），列表更轻；触屏与窄屏走"点击看大图"（无 hover 也可用）

### 修复
- 修复样式层的一个真问题：全屏大图容器声明 display:flex 覆盖了 [hidden] 的 display:none，
  导致隐藏状态下遮罩仍铺满屏幕并拦截页面全部点击（由端到端交互测试发现）

### 变更
- 列表缩略图由 56px 调整为 64px（窄屏 48px），总览页的小列表同步获得缩略图

## [1.8.3] - 2026-10-09

### 新增
- **CI 质量门禁**（`.github/workflows/ci.yml`）：push / PR 触发 ruff、mypy、Linux+Windows+macOS 三平台单测、覆盖率双门槛
- `requirements-dev.txt` 与 `requirements.lock`：开发依赖统一入口 + uv 通用锁定
- `scripts/quality_audit.sh`：一条命令输出 ruff / mypy / 测试 / 覆盖率四项指标
- `SECURITY.md`：漏洞与凭据泄露的私密报告渠道、响应时限
- `docs/` 分层为 `user/ `、`dev/`、`archive/`，并新增 `docs/README.md` 索引
- README 增加界面截图、CI/Release/License 徽章与常见问题章节
- **命中战果展示商品主图**：后端解析 mtop 的 picUrl 与网页卡片图（协议相对 / http 统一升级 https），库表新增 image_url 列（旧库自动迁移），前端卡片左侧渲染 56px 缩略图，无图显示占位
- **Docker Hub 多架构镜像发布**（`.github/workflows/docker-publish.yml`）：推 `17funnyway8/xianyu-alert`（linux/amd64 + linux/arm64），打 `v*` tag 自动发布，也可在 Actions 手动触发
- `docs/dev/` 与 README 增加「静默异常改为 contextlib.suppress + S110 门禁」说明

### 变更
- **升级到本版需注意**：库表会**自动**新增 image_url 列（幂等），但**存量记录不会有图**，只有新命中的商品才带主图 —— 需要旧记录补图要另做「回填」
- 若此前用 root 运行旧镜像，数据卷里可能残留 root 属主的 state/instance.lock，升级到非 root 镜像后需删除该文件并按数据属主设置 user（详见部署文档）
- **容器改为非 root（uid 1000）运行**：挂载宿主数据卷前需 `chown -R 1000:1000`（README 已说明）
- **README / docker-compose 默认改用已发布镜像**（`17funnyway8/xianyu-alert:1.8.2`），无需克隆仓库即可部署；从源码构建改为可选路径
- `gui_qt` 的 24 个 Qt 用例改为**独立 CI job 执行**（此前因未装 PySide6 全量 skip；与其他用例同进程会 segfault）
- 打包流水线的依赖安装统一走 `requirements-dev.txt`，消除测试依赖漂移
- 许可证明确为 MIT

### 移除
- `re_analysis/`（第三方 exe 的逆向分析资料）移出公开仓库，迁至私有归档仓库

### 修复
- `gui_qt/app.py` 对 `sqlite3.Row` 调用 `.get()`（运行到"提醒记录"列表会抛 AttributeError）
- `web/monitor_service/` 向 `Monitor` 传入 `Storage | None`
- `WebFetcher` 无 `check_item_status` 能力却直接调用（改为能力探测）
- `web/api.py` 23 处返回注解与 FastAPI 响应模型冲突
- Linux CI 上 Tk 测试因无 DISPLAY 直接失败（改用 xvfb）

## [1.8.2] - 2026-10-05

### 新增
- **Cookie 无感续期**：服务端下发新令牌时按 `_tk`/`_enc` 成对吸收，TTL 动态校准（不再硬编码小时数），节流落盘重启不倒退
- **失败分层**：抓取异常归类为 `token` / `session` / `risk` / `config` / `network` / `unknown`，给出对应处置指引
- **Web 前端全量重写**：`view-*` 分层 + 状态层 + 命令面板（旧"按 tab 堆逻辑"结构淘汰）
- **五层验证体系**：契约静态比对 / HTTP 冒烟 / jsdom DOM 端到端 / 部署资源校验 / live DOM
- Playwright 持久化 profile，减少扫码刷新频率
- 飞牛 fnOS NAS 部署编排（`docker-compose.nas.yml`）

### 修复
- Cookie 池被静默清空（P0）：`serialize_cookie_pool` 对空 cookie 条目的处理
- 售出判定收紧；多账号轮换时 cookie jar 残留导致串号
- SSE 收到 401 后日志句柄不归零（启用 token 时日志区空白）
- Web 端 storage 重载竞态（改为 build-then-swap）

## [1.8.1] - 2026-09-22

### 修复
- 源码级排查修复 14 处缺陷：Cookie 池数据保真、售出判定、轮换清理、表单保存路径不得明文写 Cookie 等
- 新增 `tests/test_regression_session_leak.py`（9 条）

## [1.8.0] - 2026-08-09

### 新增
- Cookie 自动刷新
- 进程单实例锁（崩溃自动释放）
- Windows / macOS 双平台自动构建与发布

## [1.7.0] - 2026-08-08

### 新增
- 首个公开版本：关键词 + 价格阈值监控、多通道通知、Docker Web 与桌面双形态
