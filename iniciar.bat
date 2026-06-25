@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Configurando ambiente pela primeira vez...
    python -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    .venv\Scripts\playwright install chromium
)
start "" .venv\Scripts\pythonw.exe start.py
