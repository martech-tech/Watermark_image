"""
get_refresh_token.py — รันครั้งเดียวเพื่อรับ Refresh Token
─────────────────────────────────────────────────────────────
วิธีใช้:
    1. ก่อนรัน script นี้ ต้องเพิ่ม Authorized redirect URI ใน Google Cloud Console:
       console.cloud.google.com → OAuth 2.0 Client ID ของคุณ → Edit
       → Authorized redirect URIs → เพิ่ม: http://localhost:8080

    2. รัน:
           python get_refresh_token.py

    3. เบราว์เซอร์จะเปิดขึ้นให้อนุมัติ → กลับมาที่ terminal
       คัดลอก GOOGLE_REFRESH_TOKEN แล้วใส่ใน Vercel Environment Variables
"""

import http.server
import os
import threading
import urllib.parse
import webbrowser

try:
    import httpx
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx"])
    import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── รับ credentials ───────────────────────────────────────
CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

if not CLIENT_ID:
    CLIENT_ID = input("👉 วาง GOOGLE_CLIENT_ID : ").strip()
if not CLIENT_SECRET:
    CLIENT_SECRET = input("👉 วาง GOOGLE_CLIENT_SECRET : ").strip()

REDIRECT_URI = "http://localhost:8080"
# drive.readonly  → อ่านไฟล์ใน Drive (นำเข้ารูป)
# drive.file      → สร้าง/แก้ไขไฟล์ที่แอพสร้าง (อัพโหลดรูปลายน้ำ)
SCOPE = (
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/drive.file"
)

# ─── สร้าง authorization URL ───────────────────────────────
auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
    "client_id":     CLIENT_ID,
    "redirect_uri":  REDIRECT_URI,
    "response_type": "code",
    "scope":         SCOPE,
    "access_type":   "offline",
    "prompt":        "consent",   # จำเป็นเพื่อให้ได้ refresh_token ทุกครั้ง
})

# ─── รับ authorization code ด้วย local server ────────────────
code_holder: dict = {}

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            code_holder["code"] = qs["code"][0]
            body = "<h2>&#x2705; Done! You can close this tab.</h2>".encode("utf-8")
        elif "error" in qs:
            code_holder["error"] = qs["error"][0]
            body = f"<h2>&#x274C; {qs['error'][0]}</h2>".encode("utf-8")
        else:
            body = b"<h2>Please wait...</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass   # ปิด access log

server = http.server.HTTPServer(("127.0.0.1", 8080), _Handler)
thread = threading.Thread(target=server.handle_request, daemon=True)
thread.start()

print("\n🔗 กำลังเปิดเบราว์เซอร์เพื่อขออนุญาต...")
print("   (ถ้าไม่เปิดอัตโนมัติ ให้คัดลอก URL ด้านล่างไปวางในเบราว์เซอร์)\n")
print(auth_url, "\n")
webbrowser.open(auth_url)

thread.join(timeout=120)   # รอสูงสุด 2 นาที

if "error" in code_holder:
    print(f"\n❌ ผู้ใช้ปฏิเสธ: {code_holder['error']}")
    raise SystemExit(1)

code = code_holder.get("code")
if not code:
    print("\n❌ ไม่ได้รับ authorization code — ตรวจสอบว่า http://localhost:8080 อยู่ใน Authorized redirect URIs")
    raise SystemExit(1)

# ─── แลก code เป็น tokens ─────────────────────────────────
print("🔄 กำลังแลก authorization code เป็น token...")
resp = httpx.post(
    "https://oauth2.googleapis.com/token",
    data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    },
    timeout=15,
)
data = resp.json()

if "error" in data:
    print(f"\n❌ Token exchange ล้มเหลว: {data.get('error_description', data['error'])}")
    raise SystemExit(1)

refresh_token = data.get("refresh_token")
if not refresh_token:
    print("\n❌ ไม่ได้รับ refresh_token — ลองรัน script ใหม่อีกครั้ง")
    raise SystemExit(1)

# ─── แสดงผล ────────────────────────────────────────────────
print("\n" + "=" * 60)
print("✅  ได้ Refresh Token แล้ว! คัดลอกค่าด้านล่างไปใส่ Vercel")
print("=" * 60)
print(f"\nGOOGLE_REFRESH_TOKEN={refresh_token}\n")
print("=" * 60)
print("\n📋  ขั้นตอนต่อไป:")
print("   1. ไปที่ vercel.com → โปรเจกต์ของคุณ → Settings → Environment Variables")
print("   2. เพิ่ม GOOGLE_REFRESH_TOKEN โดยวางค่าด้านบน")
print("   3. Redeploy โปรเจกต์ใน Vercel")
print("\n💡  Refresh Token นี้ไม่มีวันหมดอายุ ใช้ได้ถาวร\n")
