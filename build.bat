@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name QianyinAI --icon "tools\whaletalk.ico" --collect-all webview --collect-all clr_loader --hidden-import clr --hidden-import pythonnet --distpath "." tools\desktop_launcher.py
if errorlevel 1 (echo BUILD FAILED & pause & exit /b 1)
echo Build OK: QianyinAI.exe
pause
