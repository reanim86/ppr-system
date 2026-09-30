@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo Установка зависимостей Системы ППР...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt

echo.
echo Готово. Для запуска:
echo   1) run_server.bat  - сервер (открыть http://localhost:8000 на ПК инженера)
echo   2) run_notifier.bat - фоновые уведомления на ПК инженера
echo.
pause