@echo off
echo Building monitor-agent.exe...
pip install pyinstaller
pip install "setuptools<70"
pip install -r requirements.txt
pyinstaller --onefile --noconsole --name monitor-agent monitor-agent.py
echo Done! Output: dist\monitor-agent.exe
pause
