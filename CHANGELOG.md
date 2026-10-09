# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，条目按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 归类。

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
- 新增 `tests/test_v1_8_1_fixes.py`（9 条）

## [1.8.0] - 2026-08-09

### 新增
- Cookie 自动刷新
- 进程单实例锁（崩溃自动释放）
- Windows / macOS 双平台自动构建与发布

## [1.7.0] - 2026-08-08

### 新增
- 首个公开版本：关键词 + 价格阈值监控、多通道通知、Docker Web 与桌面双形态
