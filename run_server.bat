@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause