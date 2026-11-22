@echo off
rem 给监控服务放行 Windows 防火墙入站端口（需要管理员权限）
rem 端口从 config.json 读取，读不到就用默认 8000；重复运行会替换旧规则，不会叠加

chcp 65001 >nul
cd /d "%~dp0"
title 开放防火墙

rem ---------- 一、检查管理员权限 ----------
rem fltmc 在家庭版也能判管理员；老系统再退回 net session
fltmc >nul 2>&1
if errorlevel 1 net session >nul 2>&1
if errorlevel 1 (
    echo [需要管理员权限]
    echo   请找到本文件，右键 → 以管理员身份运行。
    echo   只有改防火墙规则需要管理员，其他脚本都不需要。
    echo.
    pause
    exit /b 1
)

rem ---------- 二、从 config.json 读端口 ----------
set "PORT=8000"
set "PORTTMP=%TEMP%\phone-pc-monitor-port.tmp"

rem 用 PowerShell 解析 config.json（文件不存在或解析失败时输出 0，后面会退回 8000）
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $j = Get-Content -LiteralPath 'config.json' -Raw -Encoding UTF8 | ConvertFrom-Json; if ($j.port) { [int]$j.port } else { 0 } } catch { 0 }" > "%PORTTMP%" 2>nul

if exist "%PORTTMP%" for /f "usebackq delims=" %%p in ("%PORTTMP%") do set "PORT=%%p"
if exist "%PORTTMP%" del /f /q "%PORTTMP%" >nul 2>&1

rem 端口必须是不以 0 开头的正整数，否则退回 8000
if "%PORT%"=="" set "PORT=8000"
echo %PORT%| findstr /r /c:"^[1-9][0-9]*$" >nul
if errorlevel 1 set "PORT=8000"

echo 读到端口：%PORT%
echo.

rem ---------- 三、替换并添加防火墙规则 ----------
echo 正在放行 TCP %PORT% 端口...
netsh advfirewall firewall delete rule name="Phone-PC Monitor" >nul 2>&1
netsh advfirewall firewall add rule name="Phone-PC Monitor" dir=in action=allow protocol=TCP localport=%PORT% profile=any
if errorlevel 1 (
    echo.
    echo [失败] 添加防火墙规则失败。怎么办：
    echo   1. 确认是“以管理员身份运行”的，不是直接双击；
    echo   2. 手动添加：控制面板 → 系统和安全 → Windows Defender 防火墙 → 高级设置
    echo      → 入站规则 → 新建规则 → 端口 → TCP → 输入 %PORT% → 允许连接 → 完成；
    echo   3. 如果装了第三方安全软件（360、火绒等），也要放行 python.exe 的入站。
    echo.
    pause
    exit /b 1
)

echo.
echo [成功] 已放行入站 TCP %PORT% 端口（规则名：Phone-PC Monitor，所有网络）。
echo.

if exist "访问地址.txt" (
    echo 程序记录的访问地址：
    type "访问地址.txt"
    echo.
)

echo 下一步：双击 启动.bat 启动服务，窗口里会打印 http://电脑IP:%PORT%/?token=...
echo 完整地址同时写在同目录的 访问地址.txt 里，复制到手机浏览器打开即可。
echo.
pause
