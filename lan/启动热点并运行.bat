@echo off
rem 一键：先尝试打开 Windows 移动热点，再启动监控服务（热点开不起来也继续启动服务）
rem 手机连上电脑热点后，用本窗口里打印的地址访问

chcp 65001 >nul
cd /d "%~dp0"
title Phone-PC Monitor · 热点启动

echo ===============================================
echo    开 启 电 脑 热 点 并 启 动 监 控
echo ===============================================
echo.

echo [1/2] 尝试开启 Windows 移动热点...
echo.
if exist "开启热点.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0开启热点.ps1"
    if errorlevel 1 (
        echo.
        echo [提示] 自动开热点没成功，不影响后面的服务启动。
        echo        手动打开：设置 → 网络和 Internet → 移动热点
        echo.
    )
) else (
    echo [提示] 没找到 开启热点.ps1 ，跳过开热点这一步。
    echo        手动打开：设置 → 网络和 Internet → 移动热点
    echo.
)

echo [2/2] 启动监控服务...
echo.
call "%~dp0启动.bat"

echo.
echo 已退出。想再启动一次，双击 启动.bat 即可。
echo 关闭热点：双击 关闭热点.ps1 ，或 设置 → 网络和 Internet → 移动热点
timeout /t 8 >nul
