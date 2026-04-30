@echo off
cd /d "%~dp0backend"
title WaterMark Pro - Setup Google Credentials

echo.
echo  ================================
echo   Setup Google Drive Credentials
echo  ================================
echo.
echo  Before running, make sure you have:
echo    1. GOOGLE_CLIENT_ID    (from console.cloud.google.com)
echo    2. GOOGLE_CLIENT_SECRET
echo    3. Authorized redirect URI added: http://localhost:8080
echo.
echo  Press any key to start, or Ctrl+C to cancel.
pause >nul

echo.
echo  Installing required packages...
pip install httpx python-dotenv -q --disable-pip-version-check 2>nul

echo.
python get_refresh_token.py

echo.
echo  ================================
echo  After getting GOOGLE_REFRESH_TOKEN:
echo    - Local:  add to backend\.env
echo    - Vercel: add to Project Settings - Environment Variables
echo  ================================
echo.
pause
