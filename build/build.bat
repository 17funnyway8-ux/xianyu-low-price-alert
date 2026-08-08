@echo off
REM ============================================================
REM 一键构建「标准版」exe（排除 playwright，体积 15~25MB）
REM 用法：双击本脚本，或在命令行执行 build\build.bat
REM 产物：dist\闲鱼低价提醒工具.exe
REM ============================================================
setlocal
cd /d "%~dp0.."

echo [1/2] 检查 PyInstaller...
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo   未安装，正在安装 pyinstaller^>=6.0 ...
    python -m pip install "pyinstaller>=6.0" || goto :fail
)

echo [2/2] 构建标准版 exe（onefile + windowed，排除 playwright）...
python -m PyInstaller "build/闲鱼低价提醒工具.spec" --noconfirm || goto :fail

echo.
echo 构建完成：dist\闲鱼低价提醒工具.exe
exit /b 0

:fail
echo 构建失败，请查看上方错误信息。
exit /b 1
