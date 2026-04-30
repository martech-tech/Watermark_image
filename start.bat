@echo off
cd /d "%~dp0backend"
title WaterMark Pro - Local Server

echo.
echo  ================================
echo   WaterMark Pro  -  Local Mode
echo  ================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo  [ERROR] Python not found
  echo  Download: https://python.org
  pause
  exit /b 1
)

if not exist .env (
  echo  .env not found - creating from template...
  copy .env.example .env >nul
  echo.
  echo  Please fill in your credentials in backend\.env
  echo  then run this script again.
  echo.
  start notepad .env
  pause
  exit /b 1
)

echo  Installing Python packages...
pip install -r requirements.txt -q --disable-pip-version-check 2>nul

echo.
echo  Server starting...
echo  Open browser at: http://localhost:8000
echo  Press Ctrl+C to stop
echo.

start /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8000"

python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

echo.
echo  Server stopped.
pause
