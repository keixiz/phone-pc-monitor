@echo off
rem 摄像头检测：运行程序自检并把结果写到 摄像头检测.txt，然后显示出来（不会启动监控服务）
chcp 65001 >nul
cd /d "%~dp0"
title 摄像头检测

if exist "phone-pc-monitor.exe" goto run_exe
if exist "monitor-lan.py" goto run_py
echo [错误] 同目录没有 phone-pc-monitor.exe 或 monitor-lan.py
pause
exit /b 1

:run_exe
echo 正在检测摄像头（几秒到十几秒，请稍候）...
"phone-pc-monitor.exe" --probe
goto show

:run_py
echo 正在检测摄像头（几秒到十几秒，请稍候）...
python monitor-lan.py --probe
goto show

:show
echo.
if exist "摄像头检测.txt" (
  type "摄像头检测.txt"
) else (
  echo [提示] 没有生成检测报告，可能程序启动失败，看同目录 monitor-lan.log
)
echo.
echo 把上面的「结论」和「检测过程」发给开发者即可定位问题。
pause
