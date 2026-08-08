# 01 - PE 结构与元信息

> 证据来源：`pefile` 解析目标 exe（只读打开，未做任何写入）。

## 1.1 文件基本信息

| 项 | 值 |
| --- | --- |
| 路径 | `C:/Users/fun/Desktop/闲鱼低价提醒工具V1.0.exe` |
| 大小 | 53,872,111 字节 (51.38 MB) |
| 修改时间 (mtime) | 2026-07-31 14:32:22.659047 |
| MD5 | `adff8e0510f607b86a652f28cc5c5222` |
| SHA256 | `3c9bea9cdacb02d75a1cfe03d7f98e5eb96aa01ab68dfdf3f4dd900d73ee7937` |

## 1.2 PE 头

| 项 | 值 |
| --- | --- |
| Machine | 0x8664 (IMAGE_FILE_MACHINE_AMD64) |
| Optional Magic | 0x020B (PE32+ (64-bit)) |
| TimeDateStamp | 1785293368 (0x6A696A38) = 2026-07-29 02:49:28 UTC |
| Subsystem | 2 (IMAGE_SUBSYSTEM_WINDOWS_GUI) |
| NumberOfSections | 7 |
| ImageBase | 0x140000000 |
| AddressOfEntryPoint | 0xE120 |
| SizeOfImage | 0x4E000 (319,488) |
| DllCharacteristics | 0xC160 |
| Checksum | 0x033623B4 |

## 1.3 节区表

| # | 名称 | VirtualAddress | VirtualSize | RawSize | RawPtr | Characteristics | 熵 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | `.text` | 0x00001000 | 0x2C640 (181,824) | 0x2C800 (182,272) | 0x00000400 | 0x60000020 | 6.469 |
| 1 | `.rdata` | 0x0002E000 | 0x13D88 (81,288) | 0x13E00 (81,408) | 0x0002CC00 | 0x40000040 | 5.752 |
| 2 | `.data` | 0x00042000 | 0x4AF0 (19,184) | 0xE00 (3,584) | 0x00040A00 | 0xC0000040 | 1.816 |
| 3 | `.pdata` | 0x00047000 | 0x255C (9,564) | 0x2600 (9,728) | 0x00041800 | 0x40000040 | 5.470 |
| 4 | `.fptable` | 0x0004A000 | 0x100 (256) | 0x200 (512) | 0x00043E00 | 0xC0000040 | 0.000 |
| 5 | `.rsrc` | 0x0004B000 | 0x15DC (5,596) | 0x1600 (5,632) | 0x00044000 | 0x40000040 | 6.964 |
| 6 | `.reloc` | 0x0004D000 | 0x768 (1,896) | 0x800 (2,048) | 0x00045600 | 0x42000040 | 5.245 |

- 节区数据结束偏移：`0x45E00` (286,208)
- **Overlay（附加数据）大小：53,585,903 字节 (51.10 MB，占全文件 99.5%)**
- 结论：绝大部分体积位于 overlay，符合 PyInstaller onefile 把 CArchive 追加在 PE 尾部的特征。

## 1.4 数字签名

- **无 Authenticode 数字签名**（IMAGE_DIRECTORY_ENTRY_SECURITY 为空）。
- 证据：`DATA_DIRECTORY[4].VirtualAddress == 0 and .Size == 0`

## 1.5 导入表（关键 DLL）

| DLL | 导入符号数 | 示例符号 |
| --- | --- | --- |
| `USER32.dll` | 32 | `CreateWindowExW`, `ShutdownBlockReasonCreate`, `MsgWaitForMultipleObjects`, `ShowWindow`, `DestroyWindow`, `RegisterClassW`, `DefWindowProcW`, `PeekMessageW` |
| `COMCTL32.dll` | 1 |  |
| `KERNEL32.dll` | 105 | `GetACP`, `IsValidCodePage`, `GetStringTypeW`, `GetFileAttributesExW`, `SetEnvironmentVariableW`, `FlushFileBuffers`, `LCMapStringW`, `CompareStringW` |
| `ADVAPI32.dll` | 4 | `OpenProcessToken`, `GetTokenInformation`, `ConvertStringSecurityDescriptorToSecurityDescriptorW`, `ConvertSidToStringSidW` |
| `GDI32.dll` | 3 | `SelectObject`, `DeleteObject`, `CreateFontIndirectW` |

## 1.6 资源

| 资源类型 | 条目数 |
| --- | --- |
| RT_ICON | 7 |
| RT_GROUP_ICON | 1 |
| RT_MANIFEST | 1 |

- 图标资源 (RT_ICON) 存在：**是**

### VS_VERSIONINFO 版本资源

- **无 VS_VERSIONINFO 版本资源**（未在打包时指定 --version-file）。
