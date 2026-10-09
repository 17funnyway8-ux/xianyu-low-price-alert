# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，条目按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 归类。

## [未发布]

### 新增
- **CI 质量门禁**（`.github/workflows/ci.yml`）：push / PR 触发 ruff、mypy、Linux+Windows+macOS 三平台单测、覆盖率双门槛
- `requirements-dev.txt` 与 `requirements.lock`：开发依赖统一入口 + uv 通用锁定
- `scripts/quality_audit.sh`：一条命令输出 ruff / mypy / 测试 / 覆盖率四项指标
- `SECURITY.md`：漏洞与凭据泄露的私密报告渠道、响应时限
- `docs/` 分层为 `user/ `、`dev/`、`archive/`，并新增 `docs/README.md` 索引
- README 增加界面截图、CI/Release/License 徽章与常见问题章节
- **Docker Hub 多架构镜像发布**（`.github/workflows/docker-publish.yml`）：推 `17funnyway8/xianyu-alert`（linux/amd64 + linux/arm64），打 `v*` tag 自动发布，也可在 Actions 手动触发
- `docs/dev/` 与 README 增加「静默异常改为 contextlib.suppress + S110 门禁」说明

### 变更
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
