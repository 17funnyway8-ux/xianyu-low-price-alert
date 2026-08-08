@echo off
rem ===========================================================
rem  闲鱼低价提醒工具 —— 图形界面启动器
rem  双击本文件即可打开界面，无需懂命令行。
rem  优先使用隔离环境的 pythonw.exe（不弹黑框），否则回退系统 Python。
rem ===========================================================
setlocal
cd /d "%~dp0"

set "ISOLATED_PYW=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"
set "ISOLATED_PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

rem --- 1) 隔离环境的 pythonw.exe（首选） ---
if exist "%ISOLATED_PYW%" (
    start "" "%ISOLATED_PYW%" "%~dp0run_gui.pyw"
    goto :eof
)

rem --- 2) 隔离环境的 python.exe ---
if exist "%ISOLATED_PY%" (
    start "" "%ISOLATED_PY%" "%~dp0run_gui.pyw"
    goto :eof
)

rem --- 3) 系统 pythonw ---
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0run_gui.pyw"
    goto :eof
)

rem --- 4) 系统 python ---
where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "%~dp0run_gui.pyw"
    goto :eof
)

echo [错误] 未找到可用的 Python 解释器。
echo 请先安装 Python 3.8+ 并确保已加入 PATH，然后重试。
pause
