# 结构化提取: xianyu_price_alert.pyc

- 源文件名 (co_filename)：`xianyu_price_alert.py`
- 嵌套 code object 总数：53
- 模块级 co_names 数量：57

## 模块级 co_names（全局引用名）

```
  __doc__, argparse, asyncio, hashlib, json, os
  re, sys, time, dataclasses, dataclass, asdict
  datetime, typing, Optional, requests, getattr, path
  dirname, executable, BASE_DIR, abspath, __file__, resource_path
  join, CONFIG_PATH, SEEN_PATH, ALERT_LOG, API_URL, APP_KEY
  USER_AGENT, VERSION, AUTHOR, DEFAULT_CONFIG, dig, float
  parse_price, Item, XianyuClient, Notifier, set, load_seen
  save_seen, str, dedup_key, dict, load_config, save_config
  print, bool, monitor, cmd_test, cmd_setup, run_gui
  create_desktop_shortcut, main, __name__
```

## 全部 code object 清单（签名 / 行号）

| 限定名 | 签名 | 首行号 | 局部变量数 |
| --- | --- | --- | --- |
| `<module>` | `<module>()` | 1 | 0 |
| `<module>.resource_path` | `resource_path(rel)` | 45 | 1 |
| `<module>.dig` | `dig(d, *keys, default)` | 82 | 5 |
| `<module>.parse_price` | `parse_price(text)` | 93 | 2 |
| `<module>.parse_price.<genexpr>` | `<genexpr>(.0)` | 102 | 2 |
| `<module>.Item` | `Item()` | 112 | 0 |
| `<module>.XianyuClient` | `XianyuClient()` | 127 | 0 |
| `<module>.XianyuClient.__init__` | `__init__(self, cookie)` | 128 | 2 |
| `<module>.XianyuClient._extract_token` | `_extract_token(cookie)` | 132 | 2 |
| `<module>.XianyuClient._sign` | `_sign(self, timestamp, data_json)` | 139 | 4 |
| `<module>.XianyuClient._cookie_dict` | `_cookie_dict(cookie)` | 143 | 5 |
| `<module>.XianyuClient.search` | `search(self, keyword, page, page_size)` | 153 | 11 |
| `<module>.XianyuClient.parse_response` | `parse_response(self, json_data, keyword)` | 202 | 18 |
| `<module>.Notifier` | `Notifier()` | 243 | 0 |
| `<module>.Notifier.__init__` | `__init__(self, config, log_fn)` | 244 | 3 |
| `<module>.Notifier.notify` | `notify(self, item, keyword, threshold)` | 248 | 5 |
| `<module>.Notifier._toast` | `_toast(self, title, msg)` | 264 | 5 |
| `<module>.Notifier._sound` | `_sound(self)` | 275 | 2 |
| `<module>.Notifier._bark` | `_bark(self, msg)` | 284 | 3 |
| `<module>.Notifier._webhook` | `_webhook(self, msg)` | 291 | 2 |
| `<module>.Notifier._log` | `_log(self, item, keyword, threshold)` | 299 | 6 |
| `<module>.load_seen` | `load_seen()` | 314 | 1 |
| `<module>.save_seen` | `save_seen(seen)` | 324 | 2 |
| `<module>.dedup_key` | `dedup_key(item)` | 332 | 1 |
| `<module>.load_config` | `load_config()` | 338 | 4 |
| `<module>.save_config` | `save_config(config)` | 350 | 2 |
| `<module>.monitor` | `monitor(config, log_fn, stop_event, once)` | 358 | 21 |
| `<module>.cmd_test` | `cmd_test(config)` | 421 | 6 |
| `<module>.cmd_setup` | `cmd_setup(config)` | 439 | 6 |
| `<module>.run_gui` | `run_gui(config)` | 463 | 21 |
| `<module>.run_gui.apply_icon` | `apply_icon(win)` | 471 | 1 |
| `<module>.run_gui.show_about` | `show_about()` | 500 | 6 |
| `<module>.run_gui.add_field` | `add_field(label, key, width)` | 534 | 5 |
| `<module>.run_gui.log_put` | `log_put(msg)` | 568 | 1 |
| `<module>.run_gui.consume` | `consume()` | 571 | 1 |
| `<module>.run_gui.sync_config_from_fields` | `sync_config_from_fields()` | 581 | 3 |
| `<module>.run_gui.validate_cookie` | `validate_cookie()` | 591 | 1 |
| `<module>.run_gui.set_buttons_state` | `set_buttons_state(monitoring)` | 606 | 1 |
| `<module>.run_gui.start` | `start()` | 618 | 0 |
| `<module>.run_gui.stop` | `stop()` | 634 | 0 |
| `<module>.run_gui.test_search` | `test_search()` | 639 | 1 |
| `<module>.run_gui.test_search.do_test` | `do_test()` | 644 | 5 |
| `<module>.run_gui.test_search.restore` | `restore()` | 664 | 0 |
| `<module>.run_gui.fetch_cookie` | `fetch_cookie()` | 678 | 1 |
| `<module>.run_gui._cookie_worker` | `_cookie_worker()` | 701 | 11 |
| `<module>.run_gui._cookie_worker.<genexpr>` | `<genexpr>(.0)` | 724 | 2 |
| `<module>.run_gui._cookie_worker.<genexpr>` | `<genexpr>(.0)` | 727 | 2 |
| `<module>.run_gui._cookie_worker.<lambda>` | `<lambda>(v)` | 730 | 1 |
| `<module>.run_gui._cookie_worker.<lambda>` | `<lambda>()` | 745 | 0 |
| `<module>.run_gui.fetch_done` | `fetch_done()` | 747 | 0 |
| `<module>.run_gui.fetch_cancel_now` | `fetch_cancel_now()` | 750 | 0 |
| `<module>.create_desktop_shortcut` | `create_desktop_shortcut()` | 781 | 9 |
| `<module>.main` | `main()` | 829 | 5 |

## 各 code object 的常量表（co_consts）

### `<module>`

```python
'\n闲鱼低价提醒小工具\n====================\n按关键词搜索闲鱼商品，当商品价格低于设定阈值时，桌面弹窗 + 声音 + 日志提醒。\n\n原理：调用闲鱼 H5 搜索接口（mtop.taobao.idlemtopsearch.pc.search），\n      使用登录后的 _m_h5_tk Cookie + MD5 签名完成鉴权。\n\n用法：\n  python xianyu_price_alert.py            # 启动持续监控（按 config.json）\n  python xianyu_price_alert.py --once     # 只扫描一轮就退出（适合配合系统定时任务）\n  python xianyu_price_alert.py --test     # 测试一次搜索并打印结果（验证 Cookie 是否可用）\n  pyth... (截断, 共561字符)
0
None
('dataclass', 'asdict')
('datetime',)
('Optional',)
'frozen'
False
'config.json'
'seen_items.json'
'alerts.jsonl'
'https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/'
'34839810'
'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0'
'1.0.0'
'花开半夏'
'iPhone 13'
3000.0
0.0
10
1
30
''
True
('keyword', 'max_price', 'min_price', 'interval_minutes', 'pages', 'page_size', 'cookie', 'sound', 'toast', 'bark_url', 'webhook_url')
('default',)
'return'
'Item'
'XianyuClient'
'Notifier'
'seen'
'item'
'config'
'once'
'__main__'
```

### `<module>.resource_path`

```python
'获取打包资源（如图标）的真实路径：冻结时从 _MEIPASS 取，否则从源码目录取。'
'frozen'
False
```

### `<module>.dig`

```python
'安全地从嵌套字典里取值。'
```

### `<module>.parse_price`

```python
'把闲鱼价格文本转成 float，处理 ￥、逗号、万 等。无法识别返回 None。'
None
'￥'
''
'¥'
','
' '
('面议', '电议', '私聊', '咨询')
'万'
10000
1
```

### `<module>.Item`

```python
'Item'
112
'item_id'
'title'
'price'
'price_text'
'url'
'location'
'seller'
'image'
()
None
```

### `<module>.XianyuClient`

```python
'XianyuClient'
127
'cookie'
'return'
'timestamp'
'data_json'
'keyword'
'page'
'page_size'
'json_data'
('cookie', 'token')
None
(1, 30)
('',)
```

### `<module>.XianyuClient._extract_token`

```python
None
'_m_h5_tk=([a-zA-Z0-9]+)_'
'Cookie 中未找到 _m_h5_tk，请确认已登录闲鱼并复制完整 Cookie。'
1
```

### `<module>.XianyuClient._sign`

```python
None
'&'
'utf-8'
```

### `<module>.XianyuClient._cookie_dict`

```python
None
';'
'='
1
```

### `<module>.XianyuClient.search`

```python
None
1000
False
''
'pcSearch'
('pageNumber', 'keyword', 'fromFilter', 'rowsPerPage', 'sortValue', 'sortField', 'customDistance', 'gps', 'propValueStr', 'customGps', 'searchReqFromPage', 'extraFilterValue', 'userPositionJson')
(',', ':')
('separators', 'ensure_ascii')
'2.7.2'
'1.0'
'originaljson'
'json'
'20000'
'mtop.taobao.idlemtopsearch.pc.search'
'AutoLoginOnly'
'a21ybx.search.0.0'
'a21ybx.search.searchInput.0'
('jsv', 'appKey', 't', 'sign', 'v', 'type', 'dataType', 'timeout', 'api', 'sessionOption', 'spm_cnt', 'spm_pre')
'application/x-www-form-urlencoded'
'https://www.goofish.com'
'https://www.goofish.com/'
('User-Agent', 'Content-Type', 'Origin', 'Referer')
'data'
20
('params', 'headers', 'cookies', 'data', 'timeout')
```

### `<module>.XianyuClient.parse_response`

```python
None
'ret'
0
'SUCCESS'
'接口返回错误：'
'data'
'resultList'
('default',)
'item'
'main'
'exContent'
'clickParam'
'args'
'price'
''
'itemId'
'id'
'title'
'detailParams'
'未知标题'
'area'
'userNickName'
'picUrl'
'image'
'https://www.goofish.com/item/'
'https://www.goofish.com/search?q='
('item_id', 'title', 'price', 'price_text', 'url', 'location', 'seller', 'image')
```

### `<module>.Notifier`

```python
'Notifier'
243
'config'
'item'
'keyword'
'threshold'
('config', 'log_fn')
None
```

### `<module>.Notifier.notify`

```python
None
'【闲鱼捡漏】'
'\n价格：￥'
'.2f'
'（设定阈值 ￥'
'）\n地区：'
'\u3000卖家：'
'\n链接：'
'toast'
True
'闲鱼低价提醒'
'sound'
'bark_url'
'webhook_url'
```

### `<module>.Notifier._toast`

```python
None
0
('toast',)
('notification',)
('title', 'message')
```

### `<module>.Notifier._sound`

```python
None
0
1000
350
0.12
1500
```

### `<module>.Notifier._bark`

```python
None
'bark_url'
'/'
10
('timeout',)
```

### `<module>.Notifier._webhook`

```python
None
'webhook_url'
'text'
'content'
('msgtype', 'text')
10
('data', 'timeout')
```

### `<module>.Notifier._log`

```python
None
'keyword'
'threshold'
'%Y-%m-%d %H:%M:%S'
'time'
'a'
'utf-8'
('encoding',)
False
('ensure_ascii',)
'\n'
```

### `<module>.load_seen`

```python
None
'r'
'utf-8'
('encoding',)
```

### `<module>.save_seen`

```python
None
'w'
'utf-8'
('encoding',)
False
2
('ensure_ascii', 'indent')
```

### `<module>.dedup_key`

```python
None
'|'
```

### `<module>.load_config`

```python
None
'w'
'utf-8'
('encoding',)
False
2
('ensure_ascii', 'indent')
'r'
```

### `<module>.save_config`

```python
None
'w'
'utf-8'
('encoding',)
False
2
('ensure_ascii', 'indent')
```

### `<module>.monitor`

```python
None
'cookie'
'未配置 Cookie，请先运行 --setup 或在 config.json 填写。'
('log_fn',)
'keyword'
'max_price'
'min_price'
0
1
'interval_minutes'
10
60
'pages'
'page_size'
30
'开始监控：关键词='
' 阈值≤￥'
' 间隔='
'分钟'
'监控已停止。'
('page', 'page_size')
2
'['
'%H:%M:%S'
'] 本轮扫描 '
' 条，触发 '
' 条提醒（已记录 '
' 个去重商品）'
'] 出错：'
5
```

### `<module>.cmd_test`

```python
None
'cookie'
'请先在 config.json 填写 cookie（可运行 python xianyu_price_alert.py --setup）。'
'keyword'
'page_size'
30
('page_size',)
'搜索失败：'
'关键词「'
'」搜索到 '
' 条：'
'￥'
'.2f'
'('
')'
'  '
' | '
32
'\n若能看到商品且价格正常，说明 Cookie 有效，直接运行本脚本即可开始监控。'
```

### `<module>.cmd_setup`

```python
None
'=== 闲鱼低价提醒 初始化 ==='
'搜索关键词 ['
'keyword'
''
']: '
'提醒价格阈值(元) ['
'max_price'
'扫描间隔(分钟) ['
'interval_minutes'
'每页条数 ['
'page_size'
'粘贴浏览器 Cookie（登录闲鱼后复制，留空保持不变）: '
'cookie'
'配置已保存到 config.json，运行 python xianyu_price_alert.py 开始监控。'
```

### `<module>.run_gui`

```python
None
0
('ttk', 'scrolledtext', 'messagebox')
'icon.ico'
'闲鱼低价提醒'
False
(660, 600)
2
'x'
'+'
'ew'
8
(6, 4)
('row', 'column', 'columnspan', 'sticky', 'padx', 'pady')
1
('weight',)
('', 11, 'bold')
('text', 'font')
'w'
('row', 'column', 'sticky')
'关于'
('text', 'command', 'width')
'e'
'搜索关键词'
'keyword'
'价格阈值(元)'
'max_price'
'间隔(分钟)'
'interval_minutes'
'每页条数'
'page_size'
'Cookie'
'cookie'
60
('width',)
'Cookie 必须包含 _m_h5_tk=...；可直接点下方「获取Cookie」自动登录获取，或手动从浏览器开发者工具复制完整 Cookie'
'gray'
('', 8)
('text', 'foreground', 'font')
10
(0, 4)
14
('height',)
6
'nsew'
('row', 'column', 'columnspan', 'padx', 'pady', 'sticky')
'monitoring'
(6, 6)
3
'开始监控'
('text', 'command')
('row', 'column', 'padx', 'sticky')
'测试搜索'
'停止'
'disabled'
('text', 'command', 'state')
'🔑 获取Cookie'
'已登录'
'取消获取'
(48,)
```

### `<module>.run_gui.apply_icon`

```python
'把应用图标应用到任意窗口（主窗口 + 所有子窗口复用）。'
None
```

### `<module>.run_gui.show_about`

```python
None
'关于'
False
'闲鱼低价提醒小工具'
('', 13, 'bold')
('text', 'font')
16
(14, 2)
('padx', 'pady')
'版本：v'
('', 10)
2
'作者：'
'horizontal'
('orient',)
'x'
8
('fill', 'padx', 'pady')
'主要功能：\n• 按关键词监控闲鱼商品\n• 价格低于设定阈值时，桌面弹窗 + 声音 + 日志提醒\n• 软件内一键自动获取 Cookie（需 playwright）\n• 自动去重，避免重复提醒'
'left'
('text', 'justify', 'font')
4
'说明：Cookie 约 24 小时有效，过期请重新获取。'
'gray'
('', 9)
('text', 'foreground', 'font')
(2, 8)
'确定'
10
('text', 'command', 'width')
(0, 14)
('pady',)
'+'
```

### `<module>.run_gui.add_field`

```python
None
14
('text', 'width')
0
8
4
'w'
('row', 'column', 'padx', 'pady', 'sticky')
''
('value',)
('textvariable', 'width')
1
'we'
```

### `<module>.run_gui.consume`

```python
None
'end'
'\n'
200
```

### `<module>.run_gui.sync_config_from_fields`

```python
None
('max_price', 'min_price')
0.0
('interval_minutes', 'page_size', 'pages')
```

### `<module>.run_gui.validate_cookie`

```python
None
'cookie'
''
'提示'
'请先填写 Cookie 再操作。'
False
'_m_h5_tk='
'Cookie 格式错误'
'Cookie 中未找到 _m_h5_tk 字段。\n请登录 https://www.goofish.com 后，在浏览器开发者工具里找到 h5api.m.goofish.com 的请求，复制完整 Cookie 填入。'
True
```

### `<module>.run_gui.set_buttons_state`

```python
None
'disabled'
('state',)
'normal'
```

### `<module>.run_gui.start`

```python
None
True
False
('target', 'args', 'daemon')
'监控已启动……'
```

### `<module>.run_gui.stop`

```python
None
False
'正在停止（当前轮结束后生效）……'
```

### `<module>.run_gui.test_search`

```python
None
False
True
('target', 'daemon')
```

### `<module>.run_gui.test_search.do_test`

```python
None
'cookie'
'keyword'
'page_size'
30
('page_size',)
'测试失败：'
'搜索「'
'」得到 '
' 条：'
'￥'
'.2f'
'('
')'
'  '
' | '
'没有搜到商品，可能是 Cookie 失效或关键词无结果。'
```

### `<module>.run_gui.test_search.restore`

```python
None
200
False
```

### `<module>.run_gui.fetch_cookie`

```python
None
0
('sync_playwright',)
'需要安装组件'
'自动获取 Cookie 需要 playwright。\n\n请先在命令行运行：\n  pip install playwright\n  playwright install chromium\n\n安装并重启程序后即可一键获取。\n（也可以手动在浏览器开发者工具复制 Cookie 填入。）'
True
'disabled'
('state',)
('target', 'daemon')
```

### `<module>.run_gui._cookie_worker`

```python
None
0
('sync_playwright',)
False
('headless',)
'🌐 已在浏览器打开闲鱼，请扫码 / 登录……'
'https://www.goofish.com/'
30000
('timeout',)
'打开登录页出错：'
240
'; '
'✅ 已自动获取 Cookie（共 '
' 项）并填入！'
True
1
'⏰ 4 分钟内未检测到登录态，请确认已登录后点「已登录」。'
'自动获取 Cookie 失败：'
```

### `<module>.run_gui._cookie_worker.<genexpr>`

```python
'name'
'_m_h5_tk'
None
```

### `<module>.run_gui._cookie_worker.<genexpr>`

```python
'name'
'='
'value'
None
```

### `<module>.run_gui._cookie_worker.<lambda>`

```python
None
'cookie'
```

### `<module>.run_gui._cookie_worker.<lambda>`

```python
None
'normal'
('state',)
```

### `<module>.run_gui.fetch_cancel_now`

```python
None
'已取消自动获取。'
```

### `<module>.create_desktop_shortcut`

```python
'在桌面创建带应用图标的快捷方式，双击即启动图形界面。\n\n使用 Windows WScript.Shell 创建 .lnk；图标取自同目录下的 icon.ico。\n返回快捷方式路径；失败返回 None。\n'
0
None
'~'
'Desktop'
'闲鱼低价提醒.lnk'
'frozen'
False
'python.exe'
'icon.ico'
'$ws = New-Object -ComObject WScript.Shell\n$lnk = $ws.CreateShortcut("'
'")\n$lnk.TargetPath = "'
'"\n$lnk.Arguments = "'
'"\n$lnk.WorkingDirectory = "'
'"\n$lnk.IconLocation = "'
',0"\n$lnk.Description = "闲鱼低价提醒小工具"\n$lnk.Save()\n'
'powershell'
'-NoProfile'
'-ExecutionPolicy'
'Bypass'
'-Command'
True
('check', 'capture_output', 'text')
'创建桌面快捷方式失败：'
'已创建桌面快捷方式：'
```

### `<module>.main`

```python
None
'闲鱼低价提醒小工具'
('description',)
'--test'
'store_true'
'测试一次搜索并打印结果'
('action', 'help')
'--setup'
'交互式填写配置'
'--gui'
'强制打开图形界面'
'--console'
'无界面，后台持续监控'
'--once'
'只扫描一轮就退出'
'--shortcut'
'在桌面创建带图标的快捷方式'
True
('once',)
False
0
'无法启动图形界面（'
'），改为后台监控。也可用 --test / --setup 先配置。'
```
