@echo off
rem 预设配置：生成 config.json，把端口和 token 预设好（exe 第一次运行就会直接用这份配置）
rem 想改默认值：改下面 DEF_PORT / DEF_TOKEN 两行；DEF_TOKEN 留空 = 每次随机生成强令牌
chcp 65001 >nul
cd /d "%~dp0"
title 预设端口和令牌

rem ===== 默认值（可直接改） =====
set "DEF_PORT=8000"
set "DEF_TOKEN="
rem ==============================

set "PORT=%DEF_PORT%"
if "%PORT%"=="" set "PORT=8000"
set "TOKEN=%DEF_TOKEN%"

if not "%TOKEN%"=="" goto have_token
for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N').Substring(0,4)"`) do set "TOKEN=%%t"
:have_token
if "%TOKEN%"=="" (
    echo [错误] 生成令牌失败。可以手动打开 config.json 自己填一个 token。
    pause
    exit /b 1
)

if exist "config.json" (
    copy /y "config.json" "config.json.bak" >nul
    echo [提示] 原来的 config.json 已备份为 config.json.bak
)

set "PP_PORT=%PORT%"
set "PP_TOKEN=%TOKEN%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = [ordered]@{ host='0.0.0.0'; port=[int]$env:PP_PORT; token=$env:PP_TOKEN; name=''; source='screen'; fps=3; jpeg_quality=70; camera_index=0; motion_threshold=8.0; demo=$false; notify=@{ channel='off'; topic=''; server='https://ntfy.sh'; sendkey='' } }; $c | ConvertTo-Json -Depth 4 | Out-File -Encoding utf8 -Path 'config.json'"
if errorlevel 1 (
    echo [失败] 写入 config.json 失败，检查文件是否被占用或没有写权限。
    pause
    exit /b 1
)

echo.
echo [完成] 已写入 config.json
echo   端口：%PORT%
echo   令牌：%TOKEN%
echo.
echo 手机浏览器访问（把 IP 换成电脑的热点/局域网 IP）：
echo   http://192.168.137.1:%PORT%/?token=%TOKEN%
echo.
echo 改端口后请重新运行 开放防火墙.bat 放行新端口。
echo 想固定用别的令牌：编辑本文件顶部的 DEF_TOKEN 后重新运行。
pause
