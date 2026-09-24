@echo off
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python -m pip install -r requirements-cabbage.txt
if errorlevel 1 goto failed
.venv\Scripts\python -m cabbage doctor
if errorlevel 1 goto failed
echo Setup complete. Read START-HERE-RU.md for commands.
pause
exit /b 0
:failed
echo Setup failed. Python 3.12 and internet access are required. See the error above.
pause
exit /b 1
