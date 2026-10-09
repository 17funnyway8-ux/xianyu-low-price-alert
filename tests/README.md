# 测试目录索引

## 命名规范（v1.10.7 起）

```
test_<模块>_<方面>.py
```

- **模块**：与 `xianyu_alert/` / `web/` 下的模块对应（`cookie` / `storage` / `monitor` / `gui` / `web_api` …）
- **方面**：该文件聚焦的具体行为（`paging` / `boundaries` / `compat` / `rotation` …）
- 一个文件测多个模块时，用它**最主要**测的那个模块命名。

**为什么要改**：此前文件名是"版本 + QA 批次"（`test_qa_v3_6_extra.py`、`test_v1_8_2_token_refresh.py`），
想知道"storage 的测试在哪"只能靠猜或全文搜索 —— 测评里"测试文件仍按版本切分"指的就是这个。
现在按模块命名后，**按文件名就能定位**。

## 命名重构的对照表（v1.10.7）

| 旧名 | 新名 | 主题 |
|---|---|---|
| test_fetcher_v3.py | test_fetcher_paging.py | 翻页 / 限速 / 页级容错 / Cookie 过期 |
| test_gui_v3.py | test_gui_forms.py | 表单校验 / Cookie 六态 / 通道完整性 |
| test_gui_v3_2.py | test_gui_cookie_pool.py | 默认值 / 多 Cookie 池 |
| test_gui_v3_3.py | test_gui_keyword_rules.py | 新关键词必含词 / 预置排除词 |
| test_gui_v3_5.py | test_gui_preset_shutdown.py | 预置词持久化 / 关闭流程稳定性 |
| test_gui_v3_6.py | test_gui_blacklist.py | 增改拆分 / 黑名单 / 窗口尺寸 / UI 非阻塞 |
| test_gui_v3_7.py | test_gui_soldout.py | 关键词启停 / 售出标记 / 日志高亮 |
| test_qa_docker_extra.py | test_docker_web.py | Docker / Web 化业务逻辑 |
| test_qa_extra.py | test_boundaries_runtime.py | 阈值 / 多通道容错 / 抓取失败容忍 |
| test_qa_macos_extra.py | test_macos_adaptation.py | macOS 适配第二道防线 |
| test_qa_v1_8_extra.py | test_regression_v18.py | v1.8 增量回归 |
| test_qa_v3_1_extra.py | test_keyword_rules.py | 排除词 / 必含词 / 重新打包 |
| test_qa_v3_2_extra.py | test_cookie_pool_rotation.py | 池轮换 / 健康度边界 / 排序 / 保存 |
| test_qa_v3_3_extra.py | test_config_compat.py | 旧配置兼容 / 日志明细 / 通道标签 |
| test_qa_v3_4_extra.py | test_price_filter_server.py | 服务端价格筛选 / Cookie 添加 |
| test_qa_v3_4_extra_2.py | test_price_filter_boundaries.py | 同上第二批边界 |
| test_qa_v3_5_extra.py | test_shutdown_boundaries.py | v3.5 边界补充 |
| test_qa_v3_6_extra.py | test_gui_blacklist_extra.py | 黑名单 / 按钮拆分边界 |
| test_qa_v3_7_extra.py | test_keyword_enabled_boundaries.py | 启用态脏数据 / 迁移 / 日志标签 |
| test_qa_v3_extra.py | test_boundaries_core.py | parse_price / 加密 / Cookie 健康度边界 |
| test_v1_8_1_fixes.py | test_regression_session_leak.py | 1.8.1 修复回归 |
| test_v1_8_2_token_refresh.py | test_token_refresh.py | 令牌自动续期链路 |
| test_v1_8_3_image.py | test_product_image.py | 商品主图 |
| test_v1_9_cookie.py | test_cookie_layering.py | Cookie 分层诊断 / 空闲保活 |
| test_paths_v2.py | test_paths_advanced.py | 路径解析进阶 |

> 旧名保留在本表用于追溯（如查阅历史 issue / PR 里的文件名）。

## 几条约定

1. **不依赖真实网络、真实时间、真实显示**。网络用注入的假 session（见 `test_web_fetcher_http.py`）；
   时间用注入的时钟（见 `test_resilience.py`）；GUI 用 stub 或**探测+跳过**。
2. **需要 Tk / Qt 的类必须探测并在失败时 `SkipTest`**，否则在无显示环境会 Error，
   而且"能否通过"会变成**依赖模块字母序**的隐性行为（v1.10.7 修过一次这样的问题）。
3. **断言要来自已验证的行为**，不要凭直觉写期望值（例如"非 Windows 上 shortcut 应返回 0"
   就是错的，实际契约是返回 1 并说明原因）。
4. 新增模块时，**优先在既有文件里加用例**；确实主题不同再新建文件。

## 运行方式

```bash
python -m unittest discover -s tests            # 全量
python -m unittest tests.test_storage           # 单个文件
python -m coverage run --source=xianyu_alert,web -m unittest discover -s tests
python -m coverage report
```
