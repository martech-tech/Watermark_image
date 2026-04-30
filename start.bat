@echo off
chcp 65001 >nul
cd /d "%~dp0backend"
title WaterMark Pro — Local Server

echo.
echo  ╔═══════════════════════════════════════════════╗
echo  ║         WaterMark Pro  —  Local Mode           ║
echo  ║   ใส่ลายน้ำรูปภาพ ด้วยทรัพยากรเครื่องตัวเอง      ║
echo  ╚═══════════════════════════════════════════════╝
echo.

rem ── ตรวจสอบ Python ─────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
  echo  [ERROR] ไม่พบ Python — ดาวน์โหลดได้ที่ https://python.org
  pause & exit /b 1
)

rem ── ตรวจสอบ .env ────────────────────────────────────
if not exist .env (
  echo  ไม่พบไฟล์ .env — กำลังสร้างจาก template...
  copy .env.example .env >nul
  echo.
  echo  กรุณาใส่ credentials ใน backend\.env แล้วรันสคริปต์นี้ใหม่
  echo  (กำลังเปิดไฟล์ .env ให้แก้ไข)
  echo.
  start notepad .env
  pause & exit /b 1
)

rem ── ติดตั้ง packages ────────────────────────────────
echo  กำลังตรวจสอบ Python packages...
pip install -r requirements.txt -q --disable-pip-version-check 2>nul
if errorlevel 1 (
  echo  [WARN] pip install มีข้อผิดพลาด — ลองรันต่อไป
)

rem ── เริ่ม server ────────────────────────────────────
echo.
echo  ✅  Server พร้อมใช้งาน
echo  🌐  http://localhost:8000
echo.
echo  กด Ctrl+C เพื่อหยุด server
echo  ════════════════════════════════════════════════
echo.

rem เปิดเบราว์เซอร์หลังจาก 1.5 วินาที
start /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8000"

python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

echo.
echo  Server หยุดแล้ว
pause
