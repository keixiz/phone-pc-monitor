@echo off
rem 开机自启：把程序加到当前用户的启动文件夹（不需要管理员权限）
rem 同目录有 phone-pc-monitor.exe 就指向它（静默无窗口）；没有就用 后台启动.vbs + python
chcp 65001 >nul
cd /d "%~dp0"
title 设置开机自启

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\Phone-PC Monitor.lnk"
if not exist "%STARTUP%" (
    echo [错误] 找不到开机启动文件夹：
    echo   %STARTUP%
    echo   怎么办：按 Win+R 输入 shell:startup 回车，确认能打开后重试。
    pause
    exit /b 1
)

if exist "%~dp0phone-pc-monitor.exe" goto use_exe
if exist "%~dp0后台启动.vbs" goto use_vbs
echo [错误] 同目录既没有 phone-pc-monitor.exe，也没有 后台启动.vbs。
echo   请把本脚本放到程序所在文件夹再运行。
pause
exit /b 1

:use_exe
set "PP_TARGET=%~dp0phone-pc-monitor.exe"
set "PP_ARGS="
set "PP_DESC=Phone-PC Monitor (exe)"
goto make

:use_vbs
set "PP_TARGET=%SystemRoot%\System32\wscript.exe"
set PP_ARGS="%~dp0后台启动.vbs"
set "PP_DESC=Phone-PC Monitor (python)"
goto make

:make
set "PP_LNK=%LNK%"
set "PP_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w = New-Object -ComObject WScript.Shell; $s = $w.CreateShortcut($env:PP_LNK); $s.TargetPath = $env:PP_TARGET; if ($env:PP_ARGS) { $s.Arguments = $env:PP_ARGS }; $s.WorkingDirectory = $env:PP_DIR; $s.Description = $env:PP_DESC; $s.IconLocation = ($env:SystemRoot + '\System32\shell32.dll,21'); $s.Save()"
if not exist "%LNK%" (
    echo.
    echo [失败] 快捷方式没有创建成功。怎么办：
    echo   1^) Win+R 输入 shell:startup 回车；
    echo   2^) 右键 新建 快捷方式，目标填：
    echo      %PP_TARGET% %PP_ARGS%
    echo   3^) 名称填 Phone-PC Monitor。
    echo.
    pause
    exit /b 1
)
echo.
echo [成功] 已加入开机自启
echo   快捷方式：%LNK%
echo   启动目标：%PP_TARGET% %PP_ARGS%
echo.
echo 取消：双击同目录的 开机自启-取消.bat
pause
