@echo off
rem 手机监控电脑 · 局域网版：双击启动（自动装依赖 → 自动生成 config.json → 打印手机访问地址）
rem 需要 Python 3.8+，安装时务必勾选 "Add python.exe to PATH"

chcp 65001 >nul
cd /d "%~dp0"
title Phone-PC Monitor
setlocal

echo ===============================================
echo    手 机 监 控 电 脑  ·  局 域 网 版
echo ===============================================
echo.

rem ---------- 一、找 Python ----------
set "PY="
where python >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY (
    where py >nul 2>&1
    if not errorlevel 1 set "PY=py"
)

if not defined PY (
    echo [错误] 没有找到 Python。
    echo   怎么办：打开 https://www.python.org/downloads/ 下载安装 Python 3.8 或更高版本，
    echo   安装第一步务必勾选 "Add python.exe to PATH"，装好后重新双击本文件。
    echo.
    pause
    exit /b 1
)

rem 验证能不能真的执行（有些机器上的 python 是微软商店的占位程序）
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 set "PY=py"
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [错误] 找到了 python 命令，但它执行不了。
    echo   如果刚才弹出了微软商店，那是假的 python：
    echo   请到 设置 - 应用 - 高级应用设置 - 应用执行别名 里关掉 python，
    echo   再到 https://www.python.org/downloads/ 安装真 Python 并勾选 Add to PATH。
    echo.
    pause
    exit /b 1
)
echo 已找到 Python，继续。
echo.

rem ---------- 二、检查文件是否齐全 ----------
if not exist "monitor-lan.py" (
    echo [错误] 同目录下找不到 monitor-lan.py。
    echo   请把本文件和 monitor-lan.py 放在同一个文件夹里。
    echo.
    pause
    exit /b 1
)
if not exist "static\index.html" (
    echo [警告] 没有找到 static\index.html，手机打开页面会是 404。
    echo        请确认 lan\static\index.html 存在。
    echo.
)

rem ---------- 三、检查依赖 ----------
echo [1/2] 检查运行依赖...
set "NEED_INSTALL=0"
%PY% -c "import aiohttp, mss, PIL, psutil, pynput" >nul 2>&1
if errorlevel 1 set "NEED_INSTALL=1"

if "%NEED_INSTALL%"=="1" (
    if not exist "requirements.txt" (
        echo [错误] 同目录下缺少 requirements.txt，无法自动安装依赖。
        echo   请确认 lan\requirements.txt 存在后重试。
        echo.
        pause
        exit /b 1
    )
    echo       首次运行，正在安装依赖，请稍候（大约 1-3 分钟）...
    echo.
    %PY% -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [失败] 依赖安装失败。怎么办：
        echo   1. 确认电脑能上网，再双击本文件重试一次；
        echo   2. 换国内镜像重试，在 cmd 里执行下面这行：
        echo      %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        echo   3. 还不行，把上面 pip 的报错信息发出来排查。
        echo.
        pause
        exit /b 1
    )
    %PY% -c "import aiohttp, mss, PIL, psutil, pynput" >nul 2>&1
    if errorlevel 1 (
        echo       依赖装完了，但导入仍然失败，请把上面的 pip 输出发出来看看。
        echo.
        pause
        exit /b 1
    )
    echo       依赖安装完成。
    echo.
) else (
    echo       依赖齐全，跳过安装。
    echo.
)

rem ---------- 四、启动服务 ----------
echo [2/2] 启动监控服务...
echo.
echo -----------------------------------------------
echo   运行期间请不要关闭这个窗口！
echo   关闭这个窗口 = 停止服务，手机就看不到电脑了。
echo.
echo   手机访问地址：下面的日志里会打印 http://电脑IP:端口/?token=...
echo   同时会写进本目录的 访问地址.txt ，复制到手机浏览器打开即可。
echo   首次运行会自动生成 config.json ，token 就在里面。
echo -----------------------------------------------
echo.

%PY% monitor-lan.py %*
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
    echo 服务已停止，退出码 %RC%。
    echo 如果上面有报错，按提示处理后重新双击本文件。
) else (
    echo 服务已停止。
)
echo.
pause
endlocal
