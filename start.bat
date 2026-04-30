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

rem ── หา port ว่าง (ลองทีละ port) ───────────────────
set PORT=8000
call :check_port %PORT%
if "%PORT_FREE%"=="0" (
  set PORT=8001
  call :check_port 8001
)
if "%PORT_FREE%"=="0" (
  set PORT=8002
  call :check_port 8002
)
if "%PORT_FREE%"=="0" (
  set PORT=8080
  call :check_port 8080
)
if "%PORT_FREE%"=="0" (
  set PORT=9000
)

echo.
echo  Server starting on port %PORT%...
echo  Open browser at: http://localhost:%PORT%
echo  Press Ctrl+C to stop
echo.

start /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:%PORT%"
python -m uvicorn main:app --host 127.0.0.1 --port %PORT% --reload

echo.
echo  Server stopped.
pause
exit /b 0

rem ── ตรวจสอบว่า port ว่างหรือเปล่า ─────────────────
:check_port
set PORT_FREE=1
netstat -an 2>nul | find ":%1 " | find "LISTENING" >nul 2>&1
if not errorlevel 1 set PORT_FREE=0
exit /b 0
