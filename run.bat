@echo off
chcp 65001 >nul
title 酷狗音乐 - VRChat OSC
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 python 命令，请先运行 install.bat
    pause
    exit /b 1
)

python "kugou_osc.py"

echo.
echo 程序已退出。按任意键关闭窗口。
pause >nul
