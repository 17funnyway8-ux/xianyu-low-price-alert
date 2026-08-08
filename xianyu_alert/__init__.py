"""闲鱼低价提醒工具（xianyu_alert）。

一个周期性监测工具：
    抓取闲鱼关键词搜索结果 -> 筛选「新出现」商品 -> 价格低于阈值 -> 去重 -> 发送通知。

模块划分：
    models    : Product 数据模型
    paths     : frozen/源码路径统一解析（exe 同目录 config/state）
    secure    : Cookie DPAPI 加密 / 解密 / 脱敏（零依赖）
    config    : YAML 配置加载与校验
    fetcher   : 抓取器（WebFetcher / MockFetcher / MtopFetcher 多页 + 过期检测）
    storage   : SQLite 去重与状态持久化
    notifier  : 多通道通知（控制台 / Server酱 / 邮件 / Telegram / Bark / Webhook）
    monitor   : 核心监测循环
    shortcut  : 桌面快捷方式创建（安全转义）
    cli       : 命令行入口
"""

from __future__ import annotations

__version__ = "1.7.0"
__all__ = ["__version__"]
