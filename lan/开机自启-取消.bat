@echo off
rem 取消开机自启：删除开机启动文件夹里的 Phone-PC Monitor 快捷方式（不需要管理员）
chcp 65001 >nul
cd /d "%~dp0"
title 取消开机自启

set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Phone-PC Monitor.lnk"

if exist "%LNK%" (
    del /f /q "%LNK%"
    if exist "%LNK%" (
        echo [失败] 删除失败，可能是权限或文件被占用。
        echo   怎么办：按 Win+R 输入 shell:startup 回车，手动删掉 Phone-PC Monitor 这个快捷方式。
        echo   路径：%LNK%
    ) else (
        echo [成功] 已取消开机自启，下次开机不会再自动启动。
    )
) else (
    echo [提示] 没找到开机自启项，说明本来就没开启。
    echo   路径：%LNK%
)

echo.
pause
