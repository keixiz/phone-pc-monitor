@echo off
echo ==========================================
echo  Build monitor-agent.exe
echo ==========================================
echo.

echo [1/4] Installing PyInstaller...
pip install --upgrade pyinstaller
if errorlevel 1 (
    echo install failed
    pause
    exit /b 1
)

echo.
echo [2/4] Fixing setuptools...
pip install "setuptools<70"
if errorlevel 1 (
    echo setuptools fix failed, continuing...
)

echo.
echo [3/4] Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo deps install failed
    pause
    exit /b 1
)

echo.
echo [4/4] Building exe...
pyinstaller --onefile --noconsole --name monitor-agent monitor-agent.py
if errorlevel 1 (
    echo build failed
    pause
    exit /b 1
)

echo.
echo Done! Output: dist\monitor-agent.exe
pause
