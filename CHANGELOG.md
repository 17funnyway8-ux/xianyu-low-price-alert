# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，条目按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 归类。

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
- `web/monitor_service.py` 向 `Monitor` 传入 `Storage | None`
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
