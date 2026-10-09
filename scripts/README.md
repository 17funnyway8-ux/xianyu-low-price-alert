# scripts/ 索引

本目录的脚本按**生命周期**分工，别再把一次性调试脚本和长期维护的入口混在一起。

## CI 使用（改动必须同步 .github/workflows/ci.yml）
| 脚本 | 用途 |
|---|---|
| `check_web_contract.py` | 前端 ↔ 后端**机械契约**交叉校验（路由 / 通道元数据 / 配置表单 / 记录字段），有 P0 则退出码 1 |
| `e2e_web_smoke.py` | 真实 uvicorn + socket + SQLite 的 API 冒烟（68 项断言） |
| `e2e_web_dom.js` | jsdom 加载真实页面 + DOM 交互 e2e（62 项断言，需 `npm ci`） |
| `web_contract_probe.js` | 被 `check_web_contract.py` 调用：抓取前端数据层真实发出的 URL/Method |

## 日常运维
| 脚本 | 用途 |
|---|---|
| `quality_audit.sh` | 一键跑 ruff + mypy + 单测 + 覆盖率（提交前自检） |
| `run.sh` | 统一入口：`./scripts/run.sh quality\|contract\|e2e\|all` |
| `verify_all.sh` | 发布前全量校验 |
| `verify_web_deploy.py` | 部署后 Web 端到端校验（对已运行实例） |
| `install_launchagent.sh` / `uninstall_launchagent.sh` / `com.xianyu-alert.gui.plist` | macOS 开机自启（LaunchAgent） |
| `e2e_web_dom_live.js` | 对**已运行实例**做 DOM 走查（需自备 jsdom） |

## archive/（不再维护）
发布验证期或某次排障的一次性脚本，保留作历史参考，**不保证仍可运行**：
`exe_gui_smoke.py`、`gui_smoke_v35.py`、`gui_smoke_v36*.py`、`observe_exe_exit.py`、
`stress_close_*.py`、`verify_docker_p0.sh`。

## 约定
1. **进 CI 的脚本**必须是确定性的、可在干净环境跑通，且有明确的退出码语义（0 = 通过）。
2. **一次性脚本**用完就放 `archive/`，不要留在 `scripts/` 根目录。
3. 新增脚本请在本文档登记一行。
