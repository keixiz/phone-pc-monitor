@echo off
rem 把 monitor-lan.py 打包成单文件 exe（PyInstaller --onefile，前端 static 一起打进去）
rem 输出：dist\phone-pc-monitor.exe

chcp 65001 >nul
cd /d "%~dp0"
title 打包 phone-pc-monitor.exe

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
    echo   怎么办：到 https://www.python.org/downloads/ 安装 Python 3.8 或更高版本，
    echo   安装时勾选 "Add python.exe to PATH"，然后重新双击本文件。
    echo.
    pause
    exit /b 1
)

%PY% -c "import sys" >nul 2>&1
if errorlevel 1 set "PY=py"
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [错误] 找到了 python 命令，但它执行不了（可能是微软商店的占位程序）。
    echo   请到 设置 - 应用 - 高级应用设置 - 应用执行别名 里关掉 python，
    echo   再重新安装 python.org 的 Python 并勾选 Add to PATH。
    echo.
    pause
    exit /b 1
)

rem ---------- 二、检查文件 ----------
if not exist "monitor-lan.py" (
    echo [错误] 同目录下找不到 monitor-lan.py，无法打包。
    echo.
    pause
    exit /b 1
)
if not exist "static\index.html" (
    echo [警告] 没找到 static\index.html ，打出来的 exe 打开页面会是 404。
    echo        请确认 lan\static\index.html 存在再打包。
    echo.
)

echo ==========================================
echo   打包 phone-pc-monitor.exe
echo ==========================================
echo.

rem ---------- 三、装打包工具 ----------
echo [1/4] 安装 / 升级 PyInstaller...
%PY% -m pip install --upgrade --disable-pip-version-check pyinstaller
if errorlevel 1 (
    echo.
    echo [失败] PyInstaller 安装失败。怎么办：
    echo   1. 确认电脑能上网，重新双击本文件再试；
    echo   2. 换国内镜像，在 cmd 里执行：
    echo      %PY% -m pip install --upgrade pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

echo.
echo [2/4] 修一下 setuptools（部分环境下 PyInstaller 会因为它报错）...
%PY% -m pip install --disable-pip-version-check "setuptools<70"
if errorlevel 1 echo       这一安装失败了，先继续尝试打包...

echo.
echo [3/4] 安装运行依赖...
%PY% -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo [失败] 依赖安装失败，没法打包。怎么办：
    echo   1. 确认电脑能上网，重新双击本文件再试；
    echo   2. 换国内镜像：
    echo      %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

rem ---------- 四、打包 ----------
echo.
echo [4/4] 开始打包（大约 1-3 分钟，不要关窗口）...
echo       关键参数 --add-data "static;static" 会把前端页面打进 exe，少了它打开就是空白/404。
echo.
%PY% -m PyInstaller --onefile --noconsole --name phone-pc-monitor --add-data "static;static" monitor-lan.py
if errorlevel 1 (
    echo.
    echo [失败] 打包失败。怎么办：
    echo   1. 先单独双击 启动.bat ，确认程序本身能跑起来；
    echo   2. 关掉可能还在运行的 phone-pc-monitor.exe 再试；
    echo   3. 把 build 和 dist 文件夹删掉后重试；还不行就按上面的英文报错搜一下；
    echo   4. 如果提示 permission denied，多半是杀毒软件锁住了文件，先加白名单。
    echo.
    pause
    exit /b 1
)

echo.
echo [成功] 打包完成：dist\phone-pc-monitor.exe
echo.
echo 用法：
echo   1. 把 dist\phone-pc-monitor.exe 复制到一个单独的文件夹，例如 桌面\PhonePC；
echo   2. 双击运行，首次启动会在 exe 旁边自动生成 config.json，
echo      访问地址写在同目录的 访问地址.txt 里；
echo   3. 换成别的电脑跑，记得先运行 开放防火墙.bat 放行端口。
echo.
pause
