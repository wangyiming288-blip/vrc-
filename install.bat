@echo off
chcp 65001 >nul
title 安装 KugouOSC 依赖
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [错误] 没有找到 python 命令。
    echo 请先安装 Python 3.9 ~ 3.12，安装时务必勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

echo ============================================
echo   正在安装依赖，请稍候……
echo ============================================
echo.

python -m pip install --upgrade pip
python -m pip install python-osc requests

echo.
echo 正在安装 SMTC 读取库 winsdk ……
python -m pip install winsdk

if errorlevel 1 (
    echo.
    echo winsdk 安装失败，改用 winrt 系列重试 ……
    python -m pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections
)

echo.
echo ============================================
echo   安装完成！按任意键退出。
echo ============================================
pause >nul
