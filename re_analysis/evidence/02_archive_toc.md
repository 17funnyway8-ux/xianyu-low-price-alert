# 02 - PyInstaller 归档解包清单

> 工具：`pyinstxtractor-ng 2026.7.3`（只读打开目标 exe，输出至 `re_analysis/extracted/`）

## 2.1 解包总览

| 项 | 值 |
| --- | --- |
| CArchive 条目数 | 1196（工具报告） |
| PYZ 归档条目数 | 555（工具报告） |
| PyInstaller 版本特征 | 2.1+ （CArchive cookie 88 字节格式） |
| Python 运行时版本 | 3.13（工具自动识别） |
| 归档载荷长度 | 53,585,903 字节 |
| 落盘文件总数 | 1750 |
| ├─ .pyc | 567 |
| ├─ .dll/.pyd | 72 |
| └─ 其它数据文件 | 1111 |

## 2.2 pyc 分类统计

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| business | 1 | 业务代码（CArchive 顶层、非 PyInstaller 运行时） |
| runtime | 11 | PyInstaller 引导/运行时钩子 |
| thirdparty | 272 | 第三方依赖 |
| stdlib | 283 | CPython 标准库 |

## 2.3 CArchive 顶层 pyc（全部列出）

| 文件 | 大小 | 分类 |
| --- | --- | --- |
| `pyi_rth__tkinter.pyc` | 1,133 B | runtime |
| `pyi_rth_inspect.pyc` | 2,795 B | runtime |
| `pyi_rth_multiprocessing.pyc` | 2,349 B | runtime |
| `pyi_rth_pkgutil.pyc` | 1,559 B | runtime |
| `pyi_rth_setuptools.pyc` | 1,020 B | runtime |
| `pyiboot01_bootstrap.pyc` | 2,036 B | runtime |
| `pyimod01_archive.pyc` | 4,930 B | runtime |
| `pyimod02_importers.pyc` | 31,802 B | runtime |
| `pyimod03_ctypes.pyc` | 6,450 B | runtime |
| `pyimod04_pywin32.pyc` | 1,679 B | runtime |
| `struct.pyc` | 305 B | runtime |
| `xianyu_price_alert.pyc` | 46,315 B | business |

## 2.4 体积 Top 30 二进制

| 文件 | 大小 |
| --- | --- |
| `playwright/driver/node.exe` | 88.0 MB |
| `libcrypto-3-x64.dll` | 7.6 MB |
| `python313.dll` | 5.9 MB |
| `tcl86t.dll` | 1.8 MB |
| `libssl-3-x64.dll` | 1.5 MB |
| `tk86t.dll` | 1.5 MB |
| `ucrtbase.dll` | 1.1 MB |
| `unicodedata.pyd` | 682.5 KB |
| `MSVCP140.dll` | 628.4 KB |
| `_decimal.pyd` | 266.5 KB |
| `ada92cb5d92a588d1b93__mypyc.cp313-win_amd64.pyd` | 221.0 KB |
| `pyexpat.pyd` | 203.5 KB |
| `_ssl.pyd` | 164.0 KB |
| `_lzma.pyd` | 149.0 KB |
| `VCRUNTIME140.dll` | 121.6 KB |
| `_ctypes.pyd` | 120.0 KB |
| `_bz2.pyd` | 71.0 KB |
| `_socket.pyd` | 71.0 KB |
| `greenlet/_greenlet.cp313-win_amd64.pyd` | 70.5 KB |
| `_asyncio.pyd` | 57.5 KB |
| `_hashlib.pyd` | 54.5 KB |
| `_tkinter.pyd` | 54.5 KB |
| `VCRUNTIME140_1.dll` | 48.6 KB |
| `_overlapped.pyd` | 42.5 KB |
| `api-ms-win-crt-math-l1-1-0.dll` | 30.4 KB |
| `libffi-8.dll` | 27.5 KB |
| `api-ms-win-crt-convert-l1-1-0.dll` | 26.5 KB |
| `api-ms-win-crt-stdio-l1-1-0.dll` | 26.4 KB |
| `api-ms-win-core-file-l1-1-0.dll` | 26.4 KB |
| `api-ms-win-crt-runtime-l1-1-0.dll` | 26.4 KB |
