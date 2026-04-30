@echo off
chcp 65001 >nul
cd /d "%~dp0backend"
title WaterMark Pro — ตั้งค่า Google Credentials

echo.
echo  ╔═══════════════════════════════════════════════╗
echo  ║     ตั้งค่า Google Drive Credentials           ║
echo  ╚═══════════════════════════════════════════════╝
echo.
echo  สคริปต์นี้จะช่วยขอ Refresh Token จาก Google
echo  เพื่อให้แอพสามารถนำเข้าและอัพโหลดรูปไป Google Drive ได้
echo.
echo  ก่อนรัน ต้องมี:
echo    1. GOOGLE_CLIENT_ID   จาก console.cloud.google.com
echo    2. GOOGLE_CLIENT_SECRET  (จาก OAuth 2.0 Client)
echo    3. เพิ่ม Authorized redirect URI: http://localhost:8080
echo.
echo  กด Enter เพื่อเริ่ม หรือ Ctrl+C เพื่อออก
pause >nul

echo.
echo  กำลังติดตั้ง packages ที่จำเป็น...
pip install httpx python-dotenv -q --disable-pip-version-check 2>nul

echo.
python get_refresh_token.py

echo.
echo  ════════════════════════════════════════════════
echo  ถ้าได้ GOOGLE_REFRESH_TOKEN แล้ว:
echo    - สำหรับ Local: ใส่ใน backend\.env
echo    - สำหรับ Vercel: ใส่ใน Project Settings → Environment Variables
echo  ════════════════════════════════════════════════
echo.
pause
