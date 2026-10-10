# 闲鱼低价提醒工具（xianyu-alert）

[![CI](https://github.com/17funnyway8-ux/xianyu-low-price-alert/actions/workflows/ci.yml/badge.svg)](https://github.com/17funnyway8-ux/xianyu-low-price-alert/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/17funnyway8-ux/xianyu-low-price-alert?label=release)](https://github.com/17funnyway8-ux/xianyu-low-price-alert/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/)
[![Docker Hub](https://img.shields.io/badge/docker-17funnyway8%2Fxianyu--alert-blue?logo=docker)](https://hub.docker.com/r/17funnyway8/xianyu-alert)

**自托管的闲鱼「捡漏」监控**：按关键词周期性抓取最新商品，筛出**新出现且价格低于阈值**的，去重后推送到 控制台 / 微信 / 邮件 / Telegram / Bark / 企业微信。
提供 **Docker Web**（手机随时看）与 **Windows / macOS 桌面版**（本机 7×24 挂机）双形态，同一套配置与数据。

[English](README_EN.md) · [快速开始](#-docker-compose-快速开始推荐) · [常见问题](#-常见问题与排障) · [文档索引](#-文档索引) · [安全边界](#-安全边界)

![态势总览](docs/images/overview.png)

---

## 📷 界面预览

Docker Web 版（下面的截图来自真实运行的实例，mock 数据）：

| 命中战果 | 监控配置 |
|---|---|
| ![命中战果](docs/images/hits.png) | ![监控配置](docs/images/targets.png) |

**通知与系统**（通道配置、Cookie 池、实时日志）

![通知与系统](docs/images/system.png)

---

## ✨ 核心价值

- **双形态覆盖全部场景**：Docker Web（随时随地的手机 / 远程管理）+ Windows exe / macOS .app（本机 7×24 挂机），同一套配置与数据。
- **抓取路径主流且克制**：走闲鱼 mtop 签名接口（行业共识路线），纯 `requests` 轻量实现——镜像仅约 130MB，零外部 API 成本（对比 Playwright 重方案 1GB+）。
- **精确过滤，少打扰**：关键词 + 独立价格阈值，支持**排除词**（回收 / 置换等）、**必含词**（16G / DDR4 等）与 v1.11 的**规格语义过滤**（品牌锚定 / 代际 / 频率 / 容量，见下文），双重去重保证同一商品**永不重复提醒**；命中记录带**卖家 / 地区 / 原价**，便于判断成色。
- **通知可控**：支持**静默时段**（如 `23:00-07:00`，可跨午夜）、**命中聚合**（窗口内合并成一条，避免刷屏）与**渠道重试次数** —— 密集命中时不再连环打扰。
- **多账号 Cookie 池 + 自动续期**：按轮次轮换取用，过期自动停用并推送提醒；支持**免扫码静默刷新**（持久化浏览器 profile，登录一次后自动续），并按 **v1.9 四层凭据模型**（会话 / 令牌 / 登录态 / 密文）给出分层诊断；**空闲保活**按配置间隔定期续期，长时间挂机不易掉线。
- **凭据可轮换**：`fernet` 密钥支持一键轮换（`xianyu-alert secure rotate`）—— 生成新密钥并把配置里的密文**全部重加密**，旧密钥与配置自动备份；磁盘无明文，全接口脱敏。
- **Web 全功能**：关键词 / 过滤词 / Cookie 池 / 6 种通知通道 / 运行监控（校验在架、售出撤销、黑名单、清空记录）/ SSE 实时日志；远程访问可开 `Bearer` token 认证。
- **开箱即用**：镜像已发布 **Docker Hub（amd64 + arm64 多架构）**，`docker run` 两条命令起服务，无需克隆仓库；也可从源码构建。
- **工程可靠**：**1299 个全 mock 测试**（无外网依赖，20 秒跑完）+ CI **7 项必过检查**（Lint/类型 · 三平台单测矩阵 · Qt 界面 offscreen · Web 契约 + 前后端 e2e · 覆盖率门槛）+ 双平台自动构建发布 + 进程单实例锁（崩溃自动释放，冲突时给出可操作诊断）+ SQLite 热备指引。

---

## 🚀 Docker Compose 快速开始（推荐）

Docker 版 = **FastAPI Web 界面（:8080）+ monitor 后台线程 + CLI 调试**三合一，一键常驻运行，数据全部落在宿主机卷，删容器不丢数据。

镜像地址：`17funnyway8/xianyu-alert`（标签 `latest` / `1.11.5` / `sha-<commit>`）

### 1. 部署（二选一）

**方式 A：直接用现成镜像（最快，不用克隆仓库）**

```bash
mkdir -p xianyu-data && sudo chown -R 1000:1000 xianyu-data   # ⚠️ 镜像以非 root(uid 1000) 运行

docker run -d --name xianyu-alert \
  -p 127.0.0.1:8080:8080 \
  -e XY_DATA_DIR=/app/data -e TZ=Asia/Shanghai \
  -v "$PWD/xianyu-data:/app/data" \
  --restart unless-stopped \
  17funnyway8/xianyu-alert:1.11.5
```

**方式 B：用 docker compose（含健康检查与资源限制，推荐长期使用）**

```yaml
# docker-compose.yml（精简可部署版；完整注释版见仓库根目录 docker-compose.yml）
services:
  xianyu-alert:
    image: 17funnyway8/xianyu-alert:1.11.5   # 想自己构建：保留下面这行并加 --build
    # build: .
    container_name: xianyu-alert
    restart: unless-stopped          # 宿主机重启 / 崩溃自动拉起
    environment:
      XY_DATA_DIR: /app/data         # 数据目录（config / 密钥 / SQLite 全部落卷）
      TZ: Asia/Shanghai              # 提醒记录时间正确
      # XY_WEB_TOKEN: "change-me-随机串"   # 远程访问时启用 Bearer 认证（见下文）
    ports:
      - "127.0.0.1:8080:8080"        # 默认仅本机访问；远程改 "8080:8080"
    volumes:
      - ./xianyu-data:/app/data      # ⚠️ 备份时 config.yaml / secret.key / state/ 三件套一起备
    healthcheck:
      test: ["CMD", "python", "-c",
        "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: "0.5"
```

### 2. 启动 / 使用

```bash
# 启动常驻 Web（拉取 Docker Hub 现成镜像）
docker compose -p xianyu-alert up -d

# 想从源码构建（改过代码 / 无外网时）：把 compose 里的 image 换成 build: .，然后
# docker compose -p xianyu-alert up -d --build

# 健康检查（HTTP /healthz，含 monitor 线程状态）
curl http://127.0.0.1:8080/healthz

# 打开 Web 界面
open http://127.0.0.1:8080/

# 查看日志 / 停止（SIGTERM 优雅退出）
docker compose -p xianyu-alert logs -f
docker compose -p xianyu-alert down
```

### 3. 远程访问（可选）

1. 把 `ports` 改为 `"8080:8080"`；
2. 设置 `XY_WEB_TOKEN` 为强随机串并重启容器；
3. 此后除 `/healthz`、`/`、`/static/*` 外，全部 API 要求 `Authorization: Bearer <token>`（前端首次访问会弹 token 输入框）；
4. 进阶：再用 Caddy `basic_auth` 反代或 Tailscale 组网做第二层保护。

未设 token 时默认仅 `127.0.0.1` 可访问。

> 容器内 CLI 调试：`docker compose -p xianyu-alert run --rm xianyu-alert cookie status --config config.example.yaml`。
> Web 运行中**不可**执行 `once` / `run`（会与 Web 进程抢单实例锁，返回退出码 2）。

### 4. 桌面版 → Docker 数据迁移

把桌面版三件套复制到卷对应位置即可（桌面版数据目录：Windows exe 同目录 / macOS `~/Library/Application Support/闲鱼低价提醒工具/`）：

```bash
cp <桌面版>/config.yaml                 ./xianyu-data/config.yaml
cp <桌面版>/secret.key                  ./xianyu-data/secret.key      # 缺失则存量 Cookie 无法解密
cp <桌面版>/state/xianyu_alert.db       ./xianyu-data/state/xianyu_alert.db
```

> ⚠️ 老版 Windows `dpapi1:` 密文跨平台不可解（预期降级），Web 里重新粘贴 Cookie 即可。

---

## ⬆️ 从旧版升级（root → 非 root 必读）

v1.8.2 起镜像改为**非 root（uid 1000）**运行。从更早的 root 版升级时，数据目录里会残留
root 属主的文件（最典型是 ```state/instance.lock```），新容器可能启动失败或反复重启。

**三步升级**：

```bash
# 1) 备份数据目录（务必先做）
cp -a <数据目录> <数据目录>.bak-$(date +%Y%m%d)

# 2) 修正属主 —— 二选一：
#    A. 把数据交给容器用户（需要 sudo）
sudo chown -R 1000:1000 <数据目录>
#    B. 让容器跟随数据属主（NAS 上推荐，无需 sudo）
#       docker-compose.yml 里设置： user: "<属主uid>:<属组gid>"   例如 "1005:1001"

# 3) 起容器并确认
docker compose -p xianyu-alert up -d
curl -s http://127.0.0.1:8899/healthz | python3 -m json.tool | head -20
```

| 现象 | 原因 | 处置 |
|---|---|---|
| 日志「无法创建单实例锁文件」 | 旧 root 文件残留 | 删除 ```state/instance.lock```，或按上面第 2 步修正属主 |
| 日志「已有实例运行中」 | 同上（锁文件被误判为活跃） | 启动时会打印**可操作诊断**（含 PID 与处置），照提示做即可 |
| 容器反复重启 | 数据目录不可写 | ```docker compose logs xianyu-alert``` 看具体路径，再按第 2 步处理 |

### 查看当前部署形态（排查路径问题）

```/healthz``` 现在直接返回部署形态快照，不用再猜数据落在哪：

```bash
curl -s http://127.0.0.1:8899/healthz | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['deployment'])"
# {'kind': 'custom', 'label': '自定义数据目录', 'data_dir': '/app/data', ...}
```

### 固定镜像版本（可复现部署）

```latest``` 会随发布移动；需要「每次都跑同一个镜像」时用 digest 固定：

```bash
docker pull 17funnyway8/xianyu-alert@sha256:<digest>
# digest 可在 Docker Hub 的 tags 页面，或 docker inspect 的输出中找到
```

### 升级到新版本（日常）

**compose 里的镜像 tag 是钉死的**（便于复现部署），因此 `docker compose up -d` **不会**把服务升到新版本 ——
这也是「明明发了新版本，NAS 上却还是旧版」的常见原因。升级必须显式改 tag：

```bash
cd <你的部署目录>

# 1) 备份数据（config.yaml + secret.key + state/ 都要，三件套缺一不可）
cp -a data data.bak-$(date +%Y%m%d-%H%M)

# 2) 把 tag 改成目标版本（版本号从 Releases 页复制，这里用变量避免写死）
TARGET=<最新版本>
#   macOS 用： sed -i '' -e "s|17funnyway8/xianyu-alert:.*|17funnyway8/xianyu-alert:$TARGET|" docker-compose.yml
sed -i "s|17funnyway8/xianyu-alert:.*|17funnyway8/xianyu-alert:$TARGET|" docker-compose.yml
grep image: docker-compose.yml          # 确认改对了

# 3) 拉新镜像并**强制重建**容器
docker compose pull && docker compose up -d --force-recreate

# 4) 确认版本（/healthz 会回显当前版本与数据目录）
curl -s http://127.0.0.1:8899/healthz | python3 -m json.tool | head -6
```

> **为什么要 `--force-recreate`**：只改 tag 时 `up -d` 可能判定「配置未变」而复用旧容器，
> 强制重建最稳妥（镜像 tag 变了本来就该换容器）。

> **升级不需要手改配置**：新版本会给旧配置补齐默认值（如 `notify.quiet_hours`）、
> 给数据库**幂等补列**；`state/` 与 `secret.key` 原样保留，历史提醒记录不丢。
> 升级后 `/healthz` 的 `deployment` 字段可直接看到识别到的部署形态与数据目录。

---

## 📦 桌面版（Windows / macOS）

无需 Docker 的轻量选择，双击即用：

- **Windows**：从 [GitHub Releases](https://github.com/17funnyway8-ux/xianyu-low-price-alert/releases) 下载 `xianyu-low-price-alert-win64-<tag>.exe`（onefile，无需安装 Python）。
- **macOS**：下载 `xianyu-low-price-alert-macos-arm64-<tag>.zip`（.app，M 系列芯片）。

使用要点：

1. 首次启动会生成 `config.yaml`；数据落在 **exe/.app 同目录 `state/`**（macOS 为 `~/Library/Application Support/闲鱼低价提醒工具/`）；
2. 在 GUI「监控配置」添加关键词与价格阈值，勾选通知通道，填入 Cookie 后点击**开始监控**；
3. 同数据目录下**只允许一个实例运行**（单实例锁，崩溃后 OS 自动释放，无需人工删锁）。

源码模式运行：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
# macOS GUI 另需：.venv/bin/pip install -r requirements-macos.txt
.venv/bin/python -m xianyu_alert.cli run   --config config.yaml   # 持续监测
.venv/bin/python -m xianyu_alert.cli once  --config config.yaml   # 只跑一轮（适合 cron）
.venv/bin/python -m xianyu_alert.cli gui                          # 启动图形界面
```

---

## 🔑 获取闲鱼 Cookie

Cookie 是真实抓取的**必需前提**（保存前会自动校验：过期 / 缺 `_m_h5_tk` 会拒绝保存）。三种方式任选：

- **方式 A（推荐 · 半自动）**：安装可选依赖后自动打开浏览器登录并提取：

  ```bash
  pip install -r requirements-cookie.txt
  playwright install chromium
  python -m xianyu_alert.cli login --config config.yaml
  ```

- **方式 B（脚本 / 粘贴）**：浏览器登录后从开发者工具复制 Cookie 请求头，直接传入或粘贴：

  ```bash
  python -m xianyu_alert.cli login --config config.yaml \
    --cookie-string "cookie2=...; _m_h5_tk=..."
  ```

- **方式 C（Web 界面）**：Docker 版无需浏览器——打开「监控配置 → Cookie 管理（池）」添加 / 刷新条目，保存时自动校验并 **Fernet 加密落盘**。

> 巡检小工具：`python -m xianyu_alert.cli cookie status --config config.yaml` 只检测单值 + Cookie 池各条健康状态（脱敏回显、不写入配置），适合脚本 / SSH 远程巡检。

### 免扫码自动续期（v1.9+）

登录态失效是长期挂机最常见的故障。本工具用**持久化浏览器 profile** 把「扫码」变成一次性动作：

1. 在有浏览器的机器上执行一次 `python -m xianyu_alert.cli login`（会打开浏览器完成登录）；
2. 把生成的 `browser_profile/` 目录复制到目标机的数据目录（Docker 即 `/app/data`）；
3. 此后接口会在需要时**静默刷新** `_m_h5_tk`，无需任何人工操作；容器内无显示器也能续期。

凭据状态按 **v1.9 四层模型**分层诊断（会话凭据 / 令牌层 / 登录态 / 密文可解密性），
因此「令牌过期」这类**可自愈**的问题不会被误报成「要重新登录」。
健康状态可直接看 `GET /healthz` 与 Web 界面的状态灯；空闲保活（`monitor.keepalive_*`）会定期触发续期。

### 密钥轮换

`secret.key` 泄漏或需要换机时，用一条命令换钥并重加密全部密文：

```bash
python -m xianyu_alert.cli secure status --config config.yaml   # 看密钥位置与恢复指引
python -m xianyu_alert.cli secure rotate --config config.yaml   # 轮换密钥并重加密（自动备份旧密钥与配置）
```

轮换会先用**旧密钥解密、失败即整体中止**，不会留下「一半新一半旧」的配置；旧密钥与配置都带时间戳备份。

---

## ⚙️ 配置要点（config.yaml）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `keywords[].keyword` | 必填 | 搜索关键词，不可重复 |
| `keywords[].max_price` | 必填 | 价格阈值，`price < max_price` 才提醒 |
| `keywords[].exclude_keywords` | `[]` | 排除词：标题命中**任一**即跳过（回收 / 置换 / 收购…） |
| `keywords[].required_keywords` | 自动提取 | 必含词：标题必须**全部包含**（如 `DDR4`）；`[]` = 不强制。v1.11 起自动提取**不再包含容量 token**（容量交给规格过滤） |
| `keywords[].spec_filter` | `true` | 规格语义过滤：把搜索词当规格判定（品牌锚定 / 代际 / 频率 / 容量）；`false` = 退回字面匹配 |
| `spec_filter`（顶层） | `true` | 上者的全局默认值，单个关键词可覆盖 |
| `monitor.interval_seconds` | 600 | 监测间隔秒数，生产建议 **600~900**；**v1.11.3 起硬下限 120**（更低会被收敛并告警） |
| `monitor.autostart` | `false` | **v1.11.4**：服务启动后是否自动开始监控。`true` = 容器/进程重启后自己接着盯盘，不再"以为它在盯、其实早就停了" |
| `monitor.user_agent` | 内置 Chrome | 浏览器 UA。**v1.11.4 起强调与环境一致**：建议填你拿 Cookie 那个浏览器的 `navigator.userAgent`（指纹与声明别自相矛盾） |
| `monitor.cookies` | `""` | 闲鱼 Cookie，保存时自动 Fernet 加密（`fernet1:`） |
| `monitor.cookie_pool` | `[]` | 多账号池：`[{name, cookie, enabled}]` 按轮次轮换（池优先、单值兜底） |
| `monitor.keepalive_enabled` | `true` | 空闲保活：长时间没有鉴权请求时定期续期，避免挂机掉线 |
| `monitor.keepalive_interval_seconds` | 1800 | 保活间隔（秒）；低于 300 视为关闭 |
| `fetcher.type` | `mtop` | `mtop` 真实抓取（默认）/ `mock` 离线演示 |
| `fetcher.pages` | 1 | 多页抓取页数（翻页增加请求频率与风控风险） |
| `storage.path` | `state/xianyu_alert.db` | SQLite 路径，目录自动创建；`:memory:` 为内存库 |
| `notify.channels` | `[{type: console}]` | 通知通道列表，见下 |
| `notify.quiet_hours` | `""` | 静默时段，如 `23:00-07:00`（**可跨午夜**）；期间命中先攒着，出静默期再发 |
| `notify.aggregate_seconds` | 0 | 聚合窗口：窗口内的命中**合并成一条**通知，0 = 不聚合 |
| `notify.retry_attempts` | 1 | 单个渠道发送失败的重试次数（1 = 不重试） |

通知通道：`console`（无参数）· `serverchan`（`sendkey`）· `email`（`smtp_host/smtp_port/username/password/to`）· `telegram`（`bot_token/chat_id`）· `bark`（`url`）· `webhook`（`url`，POST JSON，适配企业微信机器人）。

参数不完整的通道自动跳过并打 warning；所有通道都不可用时兜底为 `console`，保证提醒不静默丢失。
静默时段与聚合默认关闭，升级后行为与之前完全一致。完整模板见 `config.example.yaml`。

### 🎯 规格语义过滤（v1.11）：把「光威 3200 64G」当规格用

闲鱼搜索本身很模糊：搜 `光威 3200 64G` 会返回 8G / 16G / 32G、DDR3、DDR5，甚至把标题尾部堆了
「关联 光威 芝奇…」的**金士顿 / 酷兽**也塞进来。v1.11 起默认把**搜索关键词本身解析成规格**逐条判定：

| 维度 | 判定方式 | 为什么不能靠字面匹配 |
| --- | --- | --- |
| 品牌 | **锚定**：目标品牌必须是标题里**最先出现**的品牌 | 竞品标题在尾部堆一串品牌蹭搜索，按「包含」判定必然放行 |
| 代际 | 关键词写了 `DDR4` 就必须出现 | —— |
| 频率 / 型号 | 关键词里的 3~4 位数字（`3200`、`4080`）必须出现，且**容忍标题里被插入的空格** | 闲鱼反爬会把 `3200` 写成 `32 00`，字面 `in` 会漏判 |
| 容量 | 算**总容量**后比下限：`8G×2`=16G、`32G×2`=64G、`4根16G`=64G、`共64G`=64G | 真 64G 的标题常常**根本不写 `64G`**，字面必含会误杀 |
| **单条容量 / 条数** | 关键词里写组合（`32G×2`）时，要求标题体现**单条 ≥32G 且 ≥2 条**；`共64G` 这类总量不算单条，「64G 单条 / 出一根」也不算 | 很多人写 `64G` 其实是想买**两根 32G**，而闲鱼会把「16G×2 共32G」「16G×4 共64G」一起搜出来 —— 标题里确实写着 32G/64G |

效果（线上真实快照）：关键词 `光威 3200 64G` 的 71 条命中里，旧版 71 条全提醒；
新版只提醒真正满足规格的 **5 条** —— 其中包括「光威 ... 32GB×2」这种**不写 64G** 的真 64G。

**「64G」= 两根 32G 怎么写**：把组合写进关键词即可，闲鱼的模糊检索对多出来的
`32G×2` 不敏感（实测召回不降）：

```yaml
- keyword: "光威 3200 64G 32G×2"   # 搜索用整串；规格解析出「单条 ≥32G、≥2 条、总量 ≥64G」
  max_price: 2100
```

实测三个搜索串各抓满 3 页，能召回的**真·两根 32G 光威 DDR4-3200** 商品：
`光威 3200 64G` = 11 条、`光威 3200 32G×2` = 8 条、`光威 3200 32G` = 7 条 ——
所以推荐保留 `64G` 作为搜索词，再用 `32G×2` 补上规格。

- 规格直接来自关键词，**无需额外配置**；只想按「必含词 / 排除词」字面过滤，就在该关键词下写
  `spec_filter: false`（或顶层 `spec_filter: false` 全局关闭）。
- **普通词不参与规格解析**：想强制「笔记本」「国行」这类词出现在标题里，请写进 `required_keywords`。
- 自动必含词不再包含容量 token（`16G`），否则会与容量算式打架（`32G×2` 不含字面 `64G`）。

---

## 🧪 测试 / CI

全部测试使用 MockFetcher + 内存 SQLite + mock 网络请求，**不访问外网**：

```bash
python -m unittest discover -s tests
```

**1389 个测试**覆盖模型校验、SQLite 去重持久化、通知构造、监控调度、Cookie 加密 / 分层诊断 / 密钥轮换、多页抓取与网页兜底解析、路径与部署形态、Tk / Qt 两套界面、CLI 子命令、脚本治理。

CI 的 **7 项必过检查**（PR 上全部绿色才可合并）：

| 检查 | 内容 |
| --- | --- |
| Lint & Type check | ruff + mypy |
| Test (ubuntu / windows / macos) | 三平台单测矩阵，全 mock、无外网依赖 |
| Qt GUI tests (offscreen) | Qt 界面用例 + **gui_qt 覆盖率门禁（≥60%）** |
| Web UI contract + e2e | 前后端**机械契约**校验 + 真实 HTTP 冒烟（86 项）+ jsdom DOM 交互 e2e（62 项） |
| Coverage gate | 整体与核心模块覆盖率门槛 |

打 `v*` tag 时另有 `.github/workflows/release.yml` 自动构建 Windows exe + macOS .app 并发布 GitHub Release，`docker-publish.yml` 推送多架构镜像。

---

## 🧠 工作原理（30 秒版）

每轮对每个关键词：**抓取** → **关键词过滤**（排除词命中 / 必含词缺失 / 规格不符即跳过）→ 与**上一轮结果**比对出「新出现」→ 新商品中 `price < max_price` 且未提醒过的 → 发送通知并标记。双保险去重：`prev_ids` 判断是否新出现（跨重启有效），`notified` 标志保证同一商品永不重复提醒（跨重启有效）。

---

## ⚠️ 注意事项

- **风控**：闲鱼是强反爬站点，接口带签名且需要登录态。请合理控制频率（**间隔硬下限 120 秒**，生产建议 600~900 秒）、优先使用多 Cookie 池轮换；页面结构 / 签名随时可能变动，遇到 `RGV587` 或 `FAIL_SYS_*` 错误说明请求过频或 Cookie 失效。
- **v1.11.3 起的四道账号保护**（都是实测踩坑后的补丁）：
  1. **间隔下限**：`monitor.interval_seconds` 低于 120 会被收敛到 120 并告警，Web / GUI 直接禁止保存更小的值 —— 秒级轮询会把请求量放大到十万量级；
  2. **风控熔断**：一旦命中 `RGV587`，本轮**立即停止抓取剩余页与剩余关键词**，并进入冷却（首次 `间隔 × 3`，连续命中翻倍，上限 6 小时）；冷却期内**监控 / 保活 / 校验在架一律静默**；
  3. **保活退避**：保活探测固定只抓 1 页、只挑启用中的关键词，失败后至少安静 `max(间隔, 300s)`；
  4. **关键词间限速**：多个关键词之间按 `fetcher.page_sleep` 间隔，不再背靠背请求。
  熔断状态可在「通知与系统」页看到，也可一条命令查看：`curl -s localhost:8899/healthz`（`risk.active` / `risk.remaining_seconds`）。
- **v1.11.4 的可用性与可观测性补强**：
  1. **`monitor.autostart: true`**：重启后自动接着监控（默认 false，行为不变）；
  2. **请求节奏可见**：`/healthz` 与状态接口新增 `requests`（累计 / 近 10 分钟 / 近 1 小时 / 最近一次时间），系统页新增「请求节奏」一行；
  3. **UA 与环境一致**：默认 UA 跟随当前 Chrome 主版本，并支持按你的浏览器对齐；
  4. **校验在架限流收紧**：单次上限 30 → **20 条**、间隔 1.5 → **3 秒**（唯一会连发请求的手动操作）。
- **备份三件套**：`config.yaml`（配置 + 密文 Cookie）+ `secret.key`（Fernet 密钥，**缺失则存量 Cookie 无法解密**）+ `state/xianyu_alert.db`（提醒记录）必须**一起备份**。SQLite 热备示例见 `docker-compose.yml` 注释 / [docs/v1.8_Docker化增量研判与执行方案.md](docs/dev/v1.8_Docker化增量研判与执行方案.md)。
- **免责声明**：本工具仅供个人学习与自用监测。请遵守目标站点 robots 协议与服务条款，合理控制请求频率，勿用于商业爬取或对站点造成压力；因使用本工具产生的账号风险由使用者自行承担。

---

## 🔐 安全边界

本工具会接触你的闲鱼登录态，请务必了解以下边界：

| 项 | 说明 |
| --- | --- |
| **默认无 Web 认证** | compose 默认只绑 `127.0.0.1`，且不启用认证。**一旦把端口暴露到公网（或端口绑定为 0.0.0.0），必须设置 `XY_WEB_TOKEN`**，否则同一网段内任何人都能改你的关键词 / 间隔、随时触发抓取 —— 这是把请求量放大、进而导致账号被风控的最短路径 |
| **控制面即油门** | 无认证时，能把 `interval_seconds` 改成 1、能加任意多关键词；v1.11.3 已给间隔加硬下限，但**访问面仍应由你自己收口**（token 或仅绑内网） |
| **Cookie 加密落盘** | 落盘为 Fernet 密文（`fernet1:`），接口返回统一脱敏；但 `secret.key` 与 `config.yaml` 同卷，**密钥泄漏等同 Cookie 泄漏** |
| **备份三件套** | `config.yaml` + `secret.key` + `state/` 必须一起备份 / 迁移，缺一不可 |
| **容器运行身份** | 镜像以非 root（uid 1000）运行；挂载宿主目录前请先 `chown -R 1000:1000 ./xianyu-data` |
| **漏洞报告** | 请勿在公开 Issue 披露安全问题，走 [SECURITY.md](SECURITY.md) 的私密渠道 |

---

## ❓ 常见问题与排障

### 部署与运行

**Q：Docker 起来后打不开 8080？**
先看健康检查 `curl http://127.0.0.1:8080/healthz`。容器在跑但端口不通，多半是端口被占用（compose 默认只绑 `127.0.0.1`）。看日志：`docker compose logs -f`。

**Q：容器报 Permission denied / 写不进数据卷（v1.8.2 起）**
镜像已改为**非 root（uid 1000）**运行，首次部署或从旧版本升级时需让宿主目录属主对齐：

```bash
mkdir -p xianyu-data && sudo chown -R 1000:1000 xianyu-data
```

**Q：提示「已有实例运行中」？**
单实例锁生效：同一数据目录只能跑一个进程。Web 在跑时不要再执行 `cli once/run`（会抢锁）；要手动跑一轮，用界面上的「立即执行一轮」。

**Q：macOS 打开桌面版提示「无法验证开发者」？**
macOS 版是 ad-hoc 签名（自用），首次打开需在「系统设置 → 隐私与安全性」点「仍要打开」，或右键 → 打开。

### 抓取与 Cookie

**Q：出现 `RGV587_ERROR` / `FAIL_SYS_*`？**
风控命中：请求过频或 Cookie 失效。把间隔调到 ≥300 秒、启用多 Cookie 池轮换，或重新登录刷新 Cookie。

**Q：Cookie 会自己刷新吗？**
v1.8.2 起支持无感续期（服务端下发新令牌时自动吸收、节流落盘）。会话彻底失效时才需重新登录：Web 界面「通知与系统 → Cookie 池」粘贴新的 Cookie 字符串。

**Q：`secret.key` 丢了会怎样？**
**存量 Cookie 永久无法解密**（落盘是 Fernet 密文）。备份必须三件套一起：`config.yaml` + `secret.key` + `state/`。

**Q：想先离线试玩 / 抓不到商品？**
把 `fetcher.type` 设为 `mock`（见仓库根 `config.poc.yaml`），生成确定性假数据、完全不访问闲鱼，适合验证部署与通知链路。

### 通知与安全

**Q：通知没收到怎么排查？**
界面「通知与系统」里点测试发送；Bark / Telegram 检查 token 与网络出口；邮件通道用 SMTP 授权码而非登录密码。任何通道失败都只记日志，不会中断监控。

**Q：把 Web 暴露到公网安全吗？**
**默认不建议。** compose 默认只绑 `127.0.0.1` 且不启用认证。确需远程访问务必设置 `XY_WEB_TOKEN`，并放在反向代理 + HTTPS 之后，详见 [安全边界](#-安全边界)。

**Q：Windows exe 被杀软误报？**
PyInstaller 单文件打包的常见误报。可用仓库内 `build/*.spec` 自行重新构建，或添加信任。

---

## 📄 文档索引

完整的分层索引见 **[docs/README.md](docs/README.md)**。最常用的几份：

| 文档 | 内容 |
| --- | --- |
| [docs/user/维护交接文档.md](docs/user/维护交接文档.md) | 运维 / 排障 / 备份迁移（最全，使用者优先看这份） |
| [docs/dev/开发状态与续作指南.md](docs/dev/开发状态与续作指南.md) | 当前状态、架构地图、验证体系（接手开发先看这份） |
| [docs/dev/v1.8_Docker化增量研判与执行方案.md](docs/dev/v1.8_Docker化增量研判与执行方案.md) | Docker 部署细节、备份、迁移 |
| [docs/dev/macOS适配设计文档.md](docs/dev/macOS适配设计文档.md) | macOS Qt 适配与构建 |
| [CHANGELOG.md](CHANGELOG.md) · [SECURITY.md](SECURITY.md) · [LICENSE](LICENSE) | 版本历史 / 安全策略 / 许可证 |
| `config.example.yaml` · `docker-compose.yml` | 配置模板 · 部署编排 |

---

## 📄 许可证

本项目采用 [MIT License](LICENSE)。

免责声明见上方「注意事项」：本工具仅供个人学习与自用监测，请遵守目标站点的服务条款，使用风险由使用者自行承担。

## 抓取路径说明：mtop 为主，网页解析为兜底

工具默认走 **mtop 签名接口**（见上表 `fetcher.type`）。当 mtop 不可用、或你需要一条不依赖签名接口的
降级通道时，可用**网页解析**（WebFetcher）直接解析搜索结果页。

| 路径 | 依赖 | 稳定性 | 何时用 |
|---|---|---|---|
| mtop（默认） | 签名接口 + 登录态 | 高（接口契约相对稳定） | 日常 |
| 网页解析（兜底） | 页面结构 | **中**（平台改版可能失效） | mtop 异常时降级、结构调研 |

### 网页解析怎么工作（三级策略，从稳到脆）

1. **内联 JSON**：页面脚本里的初始数据（`window.__INIT_DATA__` 之类）——结构最稳；
2. **JSON-LD**：`application/ld+json` 里的 schema.org 商品数据——跨站点通用约定；
3. **DOM 卡片**：遍历「链接里带商品 ID」的卡片。**刻意不依赖具体 class**，
   因此平台改 class 名时往往仍能命中；真正的脆弱点只在卡片内部的标题/价格/图片提取。

### 平台改版了怎么办（改一处数据，不改逻辑）

所有选择器集中在 `xianyu_alert/web_parse.py` 的 `WebSelectors`（图片属性、标题来源、价格 class 关键词）。
平台改版时**只改这张表**，并用 `tests/test_webfetcher.py` 的 HTML 夹具做离线回归（不必联网）。

### 解析不出来怎么排查（0 条时看日志）

网页解析失败时日志会打出一行**诊断**，直接说明扫描了多少、各自为何被跳过：

```
[web] 网页解析 0 条（策略=dom，HTML 8231 字节）：扫描链接 42，无商品ID 38，缺标题 0，缺价格 4，重复 0，非法 0 | 多数卡片缺价格：平台可能改了价格节点，检查 WEB_SELECTORS.price_class_patterns
```

| 诊断 | 含义 | 处置 |
|---|---|---|
| 扫描链接 0 | 页面是空壳 | 多半未登录 / 被风控拦截，先查 Cookie 与请求频率 |
| 多数「缺价格」 | 价格节点改名 | 查 `price_class_patterns` |
| 多数「缺标题」 | 标题节点改名 | 查 `title_class_patterns` |
| 无商品 ID | 链接形式变了 | 查 `xianyu_alert/parsing.py` 的 `_ID_PATTERNS` |

---

## 开机自启（三平台统一）

一条命令即可让工具随登录自动运行 —— 三平台共用同一套逻辑，**不再需要照文档手敲脚本**：

```bash
xianyu-alert autostart status          # 查看当前状态
xianyu-alert autostart enable          # 开启（登录后自动启动）
xianyu-alert autostart disable         # 关闭
xianyu-alert autostart status --json   # 机器可读
```

图形界面里也有「🚀 开机自启」按钮（Tk / Qt 均有），一键开关并提示结果。

| 平台 | 机制 | 落地位置 |
|---|---|---|
| macOS | LaunchAgent（launchctl） | `~/Library/LaunchAgents/com.xianyu-alert.gui.plist` |
| Linux | **systemd --user**（无需 root） | `~/.config/systemd/user/xianyu-alert.service` |
| Windows | 启动文件夹快捷方式 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` |

**崩溃会自动拉起，主动退出不会**（macOS 用 `KeepAlive.SuccessfulExit=false`，
Linux 用 `Restart=on-failure`）—— 避免出现关不掉的守护进程。

> 改造前：Windows 只在**桌面**建快捷方式（其实并不会自启），macOS 要用户自己跑
> `scripts/install_launchagent.sh`，Linux 完全没有这个能力。
> 现在那个脚本仍可用，但它与 CLI 是同一份逻辑（推荐直接用 CLI）。

---
