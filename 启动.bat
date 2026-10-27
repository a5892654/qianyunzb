@echo off
chcp 936 >nul
cd /d "%~dp0"

if exist "QianyinAI.exe" (
  start "" "%~dp0QianyinAI.exe" %*
  exit /b
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/2] Creating Python environment, first run only...
  where uv >/dev/null 2>nul
  if errorlevel 1 (
    echo.
    echo uv not found. Run:  pip install uv
    echo Or manually:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
  )
  uv venv --python 3.12 .venv
  uv pip install --python .venv\Scripts\python.exe -r requirements.txt
  if errorlevel 1 (
    echo Dependency install failed, check network.
    pause
    exit /b 1
  )
  echo [2/2] Environment ready
)

echo.
echo  Qianyin AI starting... close this window to quit
echo.
".venv\Scripts\python.exe" main.py %*
if errorlevel 1 pause