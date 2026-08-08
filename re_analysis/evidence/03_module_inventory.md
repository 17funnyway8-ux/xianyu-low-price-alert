# 03 - 模块清单：业务代码 / 第三方库 / 运行时

## 3.1 业务模块（核心发现）

| 文件 | 大小 | co_filename | code object 数 |
| --- | --- | --- | --- |
| `xianyu_price_alert.pyc` | 46,315 B | `xianyu_price_alert.py` | 53 |

> **关键结论**：CArchive 顶层只有 **1 个** 业务脚本 `xianyu_price_alert.pyc`，
> 其 `co_filename` 为 `xianyu_price_alert.py`。**归档中不存在 `xianyu_alert` 包**（已用
> `find . -iname '*xianyu*'` 全量搜索确认，仅命中该单文件）。
> 即：本 exe 是一个**单文件脚本**的打包产物，不是本地模块化项目的打包产物。

## 3.2 第三方库（按顶层包聚合）

| 顶层包 | pyc 数 | 合计字节 | 版本证据 |
| --- | --- | --- | --- |
| `_distutils_hack` | 2 | 10,950 | setuptools 残留 |
| `certifi` | 2 | 2,267 | 随 requests 附带，无 dist-info（未知版本） |
| `charset_normalizer` | 7 | 106,476 | 含 `ada92cb5d92a588d1b93__mypyc.cp313-win_amd64.pyd`（mypyc 编译版，≥3.x） |
| `greenlet` | 1 | 796 | `greenlet/` 顶层目录 + `_greenlet.cp313-win_amd64.pyd` |
| `idna` | 6 | 149,930 | requests 依赖 |
| `packaging` | 14 | 287,753 | setuptools 依赖 |
| `playwright` | 64 | 2,589,788 | `playwright-1.61.0.dist-info/` 目录 → **1.61.0**（确证） |
| `pyee` | 3 | 19,509 | playwright 依赖 |
| `requests` | 18 | 237,811 | 无 dist-info（未知版本） |
| `setuptools` | 119 | 1,852,941 | 打包环境残留 |
| `urllib3` | 36 | 462,194 | 无 dist-info（未知版本） |

> **注意**：`yaml`(PyYAML) 与 `bs4`(beautifulsoup4) **均未出现**在归档中。
> 而本地项目 `requirements.txt` 明确依赖这两者 → 强差分证据（见 04）。

## 3.3 运行时 / 二进制

| 文件 | 大小 | 作用 |
| --- | --- | --- |
| `VCRUNTIME140.dll` | 121.6 KB | MSVC 运行时 |
| `_asyncio.pyd` | 57.5 KB | asyncio（playwright 依赖） |
| `_hashlib.pyd` | 54.5 KB | hashlib（MD5 签名依赖） |
| `_socket.pyd` | 71.0 KB | socket |
| `_ssl.pyd` | 164.0 KB | TLS 支持（https 请求） |
| `_tkinter.pyd` | 54.5 KB | tkinter C 扩展 → GUI 使用 tkinter |
| `libcrypto-3-x64.dll` | 7.6 MB | OpenSSL 3.x |
| `libssl-3-x64.dll` | 1.5 MB | OpenSSL 3.x |
| `playwright/driver/node.exe` | 88.0 MB | — |
| `python313.dll` | 5.9 MB | CPython 3.13 解释器主体（**确证 Python 版本**） |
| `tcl86t.dll` | 1.8 MB | — |
| `tk86t.dll` | 1.5 MB | — |
| `ucrtbase.dll` | 1.1 MB | — |

### Tcl/Tk 数据目录（tkinter GUI 确证）

- `_tcl_data/` — 830 个文件
- `_tk_data/` — 87 个文件
- `tcl8/` — 5 个文件
