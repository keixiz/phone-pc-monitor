@echo off
rem 把 后台启动.vbs 加进当前用户的开机启动项（不需要管理员权限）
rem 取消：双击同目录的 开机自启-取消.bat

chcp 65001 >nul
cd /d "%~dp0"
title 设置开机自启

set "VBS=%~dp0后台启动.vbs"
if not exist "%VBS%" (
    echo [错误] 同目录下找不到 后台启动.vbs ，请确认文件完整。
    echo.
    pause
    exit /b 1
)

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\Phone-PC Monitor.lnk"

if not exist "%STARTUP%" (
    echo [错误] 找不到开机启动文件夹：
    echo   %STARTUP%
    echo   怎么办：按 Win+R 输入 shell:startup 回车，确认文件夹能打开后重试。
    echo.
    pause
    exit /b 1
)

echo 正在创建开机启动快捷方式...
rem 路径用环境变量传给 PowerShell，路径里有单引号/百分号也不会拼坏
set "PP_LNK=%LNK%"
set "PP_VBS=%VBS%"
set "PP_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w = New-Object -ComObject WScript.Shell; $s = $w.CreateShortcut($env:PP_LNK); $s.TargetPath = ($env:SystemRoot + '\System32\wscript.exe'); $s.Arguments = ([char]34 + $env:PP_VBS + [char]34); $s.WorkingDirectory = $env:PP_DIR; $s.Description = 'Phone-PC Monitor 开机自启'; $s.IconLocation = ($env:SystemRoot + '\System32\shell32.dll,21'); $s.Save()"
set "PP_LNK="
set "PP_VBS="
set "PP_DIR="

if not exist "%LNK%" (
    echo.
    echo [失败] 快捷方式没有创建成功。怎么办：
    echo   1. 按 Win+R 输入 shell:startup 回车，打开启动文件夹；
    echo   2. 在里面右键 → 新建 → 快捷方式；
    echo   3. 位置填：wscript.exe "%VBS%"
    echo   4. 名称随便填，例如 Phone-PC Monitor。
    echo.
    pause
    exit /b 1
)

echo.
echo [成功] 已加入开机自启，快捷方式位置：
echo   %LNK%
echo.
echo 电脑开机登录后会静默在后台启动，不弹黑窗口。
echo 想取消：双击同目录的 开机自启-取消.bat 。
echo.
pause
