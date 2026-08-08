@echo off
REM ============================================================
REM 一键构建「完整版」exe（含 playwright，体积 100MB+）
REM 注意：完整版仍要求目标机执行一次：
REM     playwright install chromium
REM 浏览器内核（约 300MB）不由 PyInstaller 打包。
REM 产物：dist\闲鱼低价提醒工具_完整版.exe
REM ============================================================
setlocal
cd /d "%~dp0.."

echo [1/2] 检查 PyInstaller...
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo   未安装，正在安装 pyinstaller^>=6.0 ...
    python -m pip install "pyinstaller>=6.0" || goto :fail
)

echo [2/2] 构建完整版 exe（含 playwright）...
python -m PyInstaller "build/build_full.spec" --noconfirm || goto :fail

echo.
echo 构建完成：dist\闲鱼低价提醒工具_完整版.exe
echo 请记得在目标机执行：playwright install chromium
exit /b 0

:fail
echo 构建失败，请查看上方错误信息。
exit /b 1
