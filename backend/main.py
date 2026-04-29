"""
WaterMark Pro — Backend API
FastAPI + Pillow + Google Drive API v3

Run:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

Config (.env file หรือ environment variables):
    GOOGLE_CLIENT_ID=…          # OAuth 2.0 Client ID
    GOOGLE_CLIENT_SECRET=…      # OAuth 2.0 Client Secret
    GOOGLE_REFRESH_TOKEN=…      # Refresh Token (ได้จาก OAuth 2.0 Playground)
    ALLOWED_ORIGINS=*           # CORS origins

Endpoints:
    GET  /                              — API info
    GET  /health                        — health check
    GET  /api/drive/status              — check if Drive credentials are configured
    POST /api/drive/list-folder         — list images in a Drive folder
    POST /api/drive/watermark-folder    — download folder → watermark → ZIP
    POST /api/drive/watermark-files     — download file list → watermark → ZIP
    POST /api/watermark                 — single image → watermarked image
    POST /api/watermark/batch           — multiple images → ZIP
"""

import io
import json
import math
import os
import time
import uuid
import zipfile
import asyncio
import httpx
from typing import Optional, List
from dotenv import load_dotenv

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from PIL import Image, ImageDraw, ImageFont

# HEIC / HEIF support — must be registered before any Image.open() call
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # pillow-heif not installed; HEIC files will raise an error at open time

load_dotenv()

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")
ALLOWED_ORIGINS      = os.getenv("ALLOWED_ORIGINS", "*").split(",")
DRIVE_API_BASE       = "https://www.googleapis.com/drive/v3"
DRIVE_DL_TIMEOUT     = 30    # seconds per file
DRIVE_LIST_PAGE      = 1000  # files per API page

_DRIVE_CONFIGURED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN)


# ─────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────
# ไดเรกทอรีที่ index.html อยู่ (หนึ่งระดับเหนือ backend/)
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

app = FastAPI(title="WaterMark Pro API", version="1.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# Font helpers
# ─────────────────────────────────────────────────────────────
_FONT_DIRS = [
    "C:/Windows/Fonts/",
    "/Library/Fonts/",
    "/System/Library/Fonts/",
    "/usr/share/fonts/truetype/liberation/",
    "/usr/share/fonts/truetype/dejavu/",
    "/usr/share/fonts/",
]
_FONT_ALIASES = {
    "arial":           ["arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"],
    "arial black":     ["ariblk.ttf"],
    "georgia":         ["georgia.ttf"],
    "times new roman": ["times.ttf", "LiberationSerif-Regular.ttf"],
    "courier new":     ["cour.ttf", "LiberationMono-Regular.ttf"],
    "verdana":         ["verdana.ttf"],
    "impact":          ["impact.ttf"],
    "trebuchet ms":    ["trebuc.ttf"],
    "comic sans ms":   ["comic.ttf"],
}


def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    candidates = _FONT_ALIASES.get(name.lower().strip(), [name + ".ttf"])
    for d in _FONT_DIRS:
        for f in candidates:
            p = os.path.join(d, f)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _hex_rgba(hex_color: str, alpha: int = 255) -> tuple:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


# ─────────────────────────────────────────────────────────────
# Watermark engine
# ─────────────────────────────────────────────────────────────
def _draw_text(layer, cx, cy, text, font, fill, stroke_fill, stroke_w, underline):
    d = ImageDraw.Draw(layer)
    fs = font.size
    lh = int(fs * 1.3)
    lines = text.split("\n")
    sy = cy - len(lines) * lh // 2
    for i, line in enumerate(lines):
        ly = sy + i * lh
        bbox = d.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        lx = cx - tw // 2
        if stroke_w > 0:
            d.text((lx, ly), line, font=font, fill=stroke_fill,
                   stroke_width=stroke_w, stroke_fill=stroke_fill)
        d.text((lx, ly), line, font=font, fill=fill)
        if underline:
            uw = max(1, int(fs * 0.08))
            d.rectangle([lx, ly + int(fs * 0.5), lx + tw, ly + int(fs * 0.5) + uw], fill=fill)


def _draw_img(layer, cx, cy, wm_img, canvas_w, size_pct, opacity):
    dw = int(canvas_w * size_pct)
    if dw <= 0 or not wm_img:
        return
    dh = max(1, int(dw * wm_img.height / wm_img.width))
    resized = wm_img.resize((dw, dh), Image.LANCZOS).convert("RGBA")
    r, g, b, a = resized.split()
    a = a.point(lambda p: int(p * opacity))
    resized.putalpha(a)
    layer.paste(resized, (cx - dw // 2, cy - dh // 2), resized)


def apply_watermark(
    img: Image.Image, *,
    wm_type="text", text="© Copyright", font_name="Arial", font_size=36,
    color="#ffffff", stroke_color="#000000", stroke_width=2,
    bold=False, italic=False, underline=False,
    wm_img: Optional[Image.Image] = None, wm_img_size_pct=0.25,
    opacity=0.7, rotation=-30.0, x_pct=50.0, y_pct=50.0,
    tiled=False, tile_spacing=100,
    out_format="jpeg", quality=92,
    resize_w=None, resize_h=None, keep_aspect=True,
    **_,
) -> bytes:
    base = img.convert("RGBA")
    bw, bh = base.size

    if resize_w or resize_h:
        r = bw / bh
        if keep_aspect:
            if resize_w and resize_h:
                resize_h = int(resize_w / r) if bw / bh > resize_w / resize_h else resize_h
                resize_w = int(resize_h * r) if bw / bh <= resize_w / resize_h else resize_w
            elif resize_w:
                resize_h = int(resize_w / r)
            else:
                resize_w = int(resize_h * r)
        base = base.resize((resize_w or bw, resize_h or bh), Image.LANCZOS)
        bw, bh = base.size

    alpha_val = int(opacity * 255)
    font = _load_font(font_name, font_size)
    fill = _hex_rgba(color, alpha_val)
    stroke_fill = _hex_rgba(stroke_color, alpha_val)
    wm_layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))

    def place(layer, cx, cy):
        if wm_type in ("text", "both") and text:
            _draw_text(layer, cx, cy, text, font, fill, stroke_fill, stroke_width, underline)
        if wm_type in ("image", "both") and wm_img:
            _draw_img(layer, cx, cy, wm_img, bw, wm_img_size_pct, opacity)

    if tiled:
        max_len = max((len(l) for l in text.split("\n")), default=4) if text else 4
        cw = max(60, int(max_len * font_size * 0.6 + tile_spacing + 20))
        ch = int(font_size * 1.3 + tile_spacing)
        pad = max(cw, ch)
        for row in range(-1, math.ceil(bh / ch) + 3):
            for col in range(-1, math.ceil(bw / cw) + 3):
                cx = col * cw + (0 if row % 2 == 0 else cw // 2)
                cy = row * ch + ch // 2
                tile = Image.new("RGBA", (pad * 2, pad * 2), (0, 0, 0, 0))
                place(tile, pad, pad)
                if rotation != 0:
                    tile = tile.rotate(-rotation, expand=False, resample=Image.BICUBIC)
                wm_layer.paste(tile, (cx - pad, cy - pad), tile)
    else:
        x, y = int(bw * x_pct / 100), int(bh * y_pct / 100)
        size = int(math.sqrt(bw * bw + bh * bh)) + font_size * 2
        tmp = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        place(tmp, size // 2, size // 2)
        if rotation != 0:
            tmp = tmp.rotate(-rotation, expand=False, resample=Image.BICUBIC)
        wm_layer.paste(tmp, (x - size // 2, y - size // 2), tmp)

    result = Image.alpha_composite(base, wm_layer)

    if out_format == "jpeg":
        final = Image.new("RGB", result.size, (255, 255, 255))
        final.paste(result, mask=result.split()[3])
    else:
        final = result

    buf = io.BytesIO()
    kw = {"format": out_format.upper()}
    if out_format in ("jpeg", "webp"):
        kw["quality"] = quality
    final.save(buf, **kw)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# Watermark settings parser
# ─────────────────────────────────────────────────────────────
def _wm_settings(
    wm_type, text, font_name, font_size, color, stroke_color, stroke_width,
    bold, italic, underline, opacity, rotation, x_pct, y_pct,
    tiled, tile_spacing, out_format, quality,
    resize_w, resize_h, keep_aspect,
):
    return dict(
        wm_type=wm_type, text=text, font_name=font_name, font_size=font_size,
        color=color, stroke_color=stroke_color, stroke_width=stroke_width,
        bold=bold, italic=italic, underline=underline,
        opacity=opacity, rotation=rotation, x_pct=x_pct, y_pct=y_pct,
        tiled=tiled, tile_spacing=tile_spacing,
        out_format=out_format, quality=quality,
        resize_w=resize_w or None, resize_h=resize_h or None, keep_aspect=keep_aspect,
    )


# ─────────────────────────────────────────────────────────────
# Google Drive API helpers
# ─────────────────────────────────────────────────────────────
async def _get_access_token(client: httpx.AsyncClient) -> str:
    """Exchange refresh token for a short-lived access token."""
    if not _DRIVE_CONFIGURED:
        raise HTTPException(
            400,
            "ยังไม่ได้ตั้งค่า Google OAuth credentials ใน .env — "
            "ต้องมี GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET และ GOOGLE_REFRESH_TOKEN"
        )
    r = await client.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN,
            "grant_type":    "refresh_token",
        },
        timeout=15,
    )
    data = r.json()
    if "error" in data:
        raise HTTPException(400, f"OAuth error: {data.get('error_description', data['error'])}")
    return data["access_token"]


async def drive_list_folder(
    folder_id: str,
    token: str,
    recursive: bool = False,
    client: httpx.AsyncClient = None,
    depth: int = 0,
) -> list[dict]:
    """Return list of image file dicts from a Drive folder."""
    files = []
    page_token = None
    headers = {"Authorization": f"Bearer {token}"}

    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "files(id,name,mimeType),nextPageToken",
            "pageSize": DRIVE_LIST_PAGE,
        }
        if page_token:
            params["pageToken"] = page_token

        r = await client.get(f"{DRIVE_API_BASE}/files", params=params, headers=headers, timeout=20)
        data = r.json()

        if "error" in data:
            raise HTTPException(400, f"Drive API: {data['error']['message']}")

        imgs = [f for f in data.get("files", []) if f["mimeType"].startswith("image/")]
        dirs = [f for f in data.get("files", []) if f["mimeType"] == "application/vnd.google-apps.folder"]
        files.extend(imgs)

        if recursive:
            for d in dirs:
                sub = await drive_list_folder(d["id"], token, True, client, depth + 1)
                files.extend(sub)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return files


async def drive_download_image(file_id: str, token: str, client: httpx.AsyncClient) -> bytes:
    """Download a single Drive file by ID."""
    url = f"{DRIVE_API_BASE}/files/{file_id}"
    r = await client.get(
        url,
        params={"alt": "media"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=DRIVE_DL_TIMEOUT,
        follow_redirects=True,
    )
    if r.status_code != 200:
        raise ValueError(f"HTTP {r.status_code}")
    return r.content


# ─────────────────────────────────────────────────────────────
# Routes — basic
# ─────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    """Serve the frontend app."""
    index = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index, media_type="text/html")
    return Response("index.html not found", status_code=404)


@app.get("/health")
def health():
    return {"status": "ok", "drive_configured": _DRIVE_CONFIGURED}


# ─────────────────────────────────────────────────────────────
# Routes — Google Drive
# ─────────────────────────────────────────────────────────────
@app.get("/api/drive/file/{file_id}")
async def proxy_drive_file(file_id: str):
    """
    Proxy a single Drive file to the browser.
    - OAuth credentials อยู่ที่ server เท่านั้น ไม่เปิดเผยใน browser
    - หลีกเลี่ยงปัญหา CORS เมื่อ browser พยายามดึงโดยตรง
    - รองรับไฟล์ส่วนตัวของ developer (ไม่ต้องแชร์สาธารณะ)
    """
    async with httpx.AsyncClient() as client:
        token = await _get_access_token(client)
        r = await client.get(
            f"{DRIVE_API_BASE}/files/{file_id}",
            params={"alt": "media"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=DRIVE_DL_TIMEOUT,
            follow_redirects=True,
        )
    if r.status_code == 404:
        raise HTTPException(404, "ไม่พบไฟล์ใน Drive — ตรวจสอบว่า ID ถูกต้อง")
    if r.status_code == 403:
        raise HTTPException(403, "ไม่มีสิทธิ์เข้าถึงไฟล์ — ตรวจสอบ OAuth credentials")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"ดาวน์โหลดไม่ได้: HTTP {r.status_code}")

    content_type = r.headers.get("content-type", "image/jpeg").lower().split(";")[0].strip()
    content      = r.content

    # Convert HEIC/HEIF → JPEG so all browsers can display it
    if content_type in ("image/heic", "image/heif"):
        try:
            img = Image.open(io.BytesIO(content))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=92)
            content      = buf.getvalue()
            content_type = "image/jpeg"
        except Exception:
            pass  # serve original bytes; browser will show its own error

    return Response(content=content, media_type=content_type)


@app.get("/api/drive/status")
def drive_status():
    return {
        "configured": _DRIVE_CONFIGURED,
        "message": (
            "✅ OAuth 2.0 credentials พร้อมใช้งาน"
            if _DRIVE_CONFIGURED else
            "⚠️ ยังไม่ได้ตั้งค่า — ใส่ GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN ใน .env"
        ),
    }


@app.post("/api/drive/list-folder")
async def list_drive_folder(
    folder_id: str = Form(...),
    recursive: bool = Form(False),
):
    async with httpx.AsyncClient() as client:
        token = await _get_access_token(client)
        files = await drive_list_folder(folder_id, token, recursive, client)
    return {"count": len(files), "files": files}


@app.post("/api/drive/watermark-folder")
async def watermark_drive_folder(
    folder_id: str = Form(...),
    recursive: bool = Form(False),
    # Watermark settings
    wm_type: str = Form("text"),
    text: str = Form("© Copyright"),
    font_name: str = Form("Arial"),
    font_size: int = Form(36),
    color: str = Form("#ffffff"),
    stroke_color: str = Form("#000000"),
    stroke_width: int = Form(2),
    bold: bool = Form(False),
    italic: bool = Form(False),
    underline: bool = Form(False),
    opacity: float = Form(0.7),
    rotation: float = Form(-30.0),
    x_pct: float = Form(50.0),
    y_pct: float = Form(50.0),
    tiled: bool = Form(False),
    tile_spacing: int = Form(100),
    out_format: str = Form("jpeg"),
    quality: int = Form(92),
    resize_w: Optional[int] = Form(None),
    resize_h: Optional[int] = Form(None),
    keep_aspect: bool = Form(True),
    filename_prefix: str = Form("wm_"),
    zip_folder: str = Form("watermarked"),
    wm_image: Optional[UploadFile] = File(None),
):
    wm_img_obj = None
    if wm_image:
        raw = await wm_image.read()
        wm_img_obj = Image.open(io.BytesIO(raw)).convert("RGBA")

    settings = _wm_settings(
        wm_type, text, font_name, font_size, color, stroke_color, stroke_width,
        bold, italic, underline, opacity, rotation, x_pct, y_pct,
        tiled, tile_spacing, out_format, quality, resize_w, resize_h, keep_aspect,
    )
    settings["wm_img"] = wm_img_obj

    async with httpx.AsyncClient() as client:
        token = await _get_access_token(client)
        files = await drive_list_folder(folder_id, token, recursive, client)
        if not files:
            raise HTTPException(404, "ไม่พบรูปในโฟลเดอร์")

        ext = "jpg" if out_format == "jpeg" else out_format
        zip_buf = io.BytesIO()
        ok, errors = 0, []

        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                try:
                    img_bytes = await drive_download_image(f["id"], token, client)
                    img = Image.open(io.BytesIO(img_bytes))
                    result = apply_watermark(img, **settings)
                    base_name = os.path.splitext(f["name"])[0]
                    zf.writestr(f"{zip_folder}/{filename_prefix}{base_name}.{ext}", result)
                    ok += 1
                except Exception as e:
                    errors.append({"file": f["name"], "error": str(e)})

    if ok == 0:
        raise HTTPException(500, {"message": "ประมวลผลล้มเหลวทุกไฟล์", "errors": errors})

    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf, media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="watermarked.zip"',
            "X-Processed": str(ok),
            "X-Errors": str(len(errors)),
            "X-Total": str(len(files)),
        },
    )


@app.post("/api/drive/watermark-files")
async def watermark_drive_files(
    file_ids: str = Form(..., description="Comma-separated Drive file IDs"),
    file_names: str = Form("", description="Comma-separated filenames (optional)"),
    # Watermark settings
    wm_type: str = Form("text"),
    text: str = Form("© Copyright"),
    font_name: str = Form("Arial"),
    font_size: int = Form(36),
    color: str = Form("#ffffff"),
    stroke_color: str = Form("#000000"),
    stroke_width: int = Form(2),
    bold: bool = Form(False),
    italic: bool = Form(False),
    underline: bool = Form(False),
    opacity: float = Form(0.7),
    rotation: float = Form(-30.0),
    x_pct: float = Form(50.0),
    y_pct: float = Form(50.0),
    tiled: bool = Form(False),
    tile_spacing: int = Form(100),
    out_format: str = Form("jpeg"),
    quality: int = Form(92),
    resize_w: Optional[int] = Form(None),
    resize_h: Optional[int] = Form(None),
    keep_aspect: bool = Form(True),
    filename_prefix: str = Form("wm_"),
    zip_folder: str = Form("watermarked"),
    wm_image: Optional[UploadFile] = File(None),
):
    ids = [i.strip() for i in file_ids.split(",") if i.strip()]
    names_list = [n.strip() for n in file_names.split(",") if n.strip()]

    wm_img_obj = None
    if wm_image:
        raw = await wm_image.read()
        wm_img_obj = Image.open(io.BytesIO(raw)).convert("RGBA")

    settings = _wm_settings(
        wm_type, text, font_name, font_size, color, stroke_color, stroke_width,
        bold, italic, underline, opacity, rotation, x_pct, y_pct,
        tiled, tile_spacing, out_format, quality, resize_w, resize_h, keep_aspect,
    )
    settings["wm_img"] = wm_img_obj
    ext = "jpg" if out_format == "jpeg" else out_format

    async with httpx.AsyncClient() as client:
        token = await _get_access_token(client)
        zip_buf = io.BytesIO()
        ok, errors = 0, []

        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, fid in enumerate(ids):
                fname = names_list[i] if i < len(names_list) else f"image_{i+1}.{ext}"
                try:
                    img_bytes = await drive_download_image(fid, token, client)
                    img = Image.open(io.BytesIO(img_bytes))
                    result = apply_watermark(img, **settings)
                    base_name = os.path.splitext(fname)[0]
                    zf.writestr(f"{zip_folder}/{filename_prefix}{base_name}.{ext}", result)
                    ok += 1
                except Exception as e:
                    errors.append({"id": fid, "file": fname, "error": str(e)})

    if ok == 0:
        raise HTTPException(500, {"message": "ประมวลผลล้มเหลว", "errors": errors})

    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf, media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="watermarked.zip"',
            "X-Processed": str(ok),
            "X-Errors": str(len(errors)),
        },
    )


# ─────────────────────────────────────────────────────────────
# Routes — Local image watermark (upload → process)
# ─────────────────────────────────────────────────────────────
@app.post("/api/watermark")
async def watermark_single(
    image: UploadFile = File(...),
    wm_image: Optional[UploadFile] = File(None),
    wm_type: str = Form("text"),
    text: str = Form("© Copyright"),
    font_name: str = Form("Arial"),
    font_size: int = Form(36),
    color: str = Form("#ffffff"),
    stroke_color: str = Form("#000000"),
    stroke_width: int = Form(2),
    bold: bool = Form(False),
    italic: bool = Form(False),
    underline: bool = Form(False),
    opacity: float = Form(0.7),
    rotation: float = Form(-30.0),
    x_pct: float = Form(50.0),
    y_pct: float = Form(50.0),
    tiled: bool = Form(False),
    tile_spacing: int = Form(100),
    out_format: str = Form("jpeg"),
    quality: int = Form(92),
    resize_w: Optional[int] = Form(None),
    resize_h: Optional[int] = Form(None),
    keep_aspect: bool = Form(True),
    filename_prefix: str = Form("wm_"),
):
    try:
        src = Image.open(io.BytesIO(await image.read()))
    except Exception as e:
        raise HTTPException(400, f"เปิดไฟล์ไม่ได้: {e}")

    wm_img_obj = None
    if wm_image:
        wm_img_obj = Image.open(io.BytesIO(await wm_image.read())).convert("RGBA")

    settings = _wm_settings(
        wm_type, text, font_name, font_size, color, stroke_color, stroke_width,
        bold, italic, underline, opacity, rotation, x_pct, y_pct,
        tiled, tile_spacing, out_format, quality, resize_w, resize_h, keep_aspect,
    )
    settings["wm_img"] = wm_img_obj

    try:
        result = apply_watermark(src, **settings)
    except Exception as e:
        raise HTTPException(500, f"ประมวลผลล้มเหลว: {e}")

    ext = "jpg" if out_format == "jpeg" else out_format
    orig = os.path.splitext(image.filename or "image")[0]
    mime = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(out_format, "image/jpeg")
    return StreamingResponse(
        io.BytesIO(result), media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename_prefix}{orig}.{ext}"'},
    )


@app.post("/api/watermark/batch")
async def watermark_batch(
    images: List[UploadFile] = File(...),
    wm_image: Optional[UploadFile] = File(None),
    wm_type: str = Form("text"),
    text: str = Form("© Copyright"),
    font_name: str = Form("Arial"),
    font_size: int = Form(36),
    color: str = Form("#ffffff"),
    stroke_color: str = Form("#000000"),
    stroke_width: int = Form(2),
    bold: bool = Form(False),
    italic: bool = Form(False),
    underline: bool = Form(False),
    opacity: float = Form(0.7),
    rotation: float = Form(-30.0),
    x_pct: float = Form(50.0),
    y_pct: float = Form(50.0),
    tiled: bool = Form(False),
    tile_spacing: int = Form(100),
    out_format: str = Form("jpeg"),
    quality: int = Form(92),
    resize_w: Optional[int] = Form(None),
    resize_h: Optional[int] = Form(None),
    keep_aspect: bool = Form(True),
    filename_prefix: str = Form("wm_"),
    zip_folder: str = Form("watermarked"),
):
    wm_img_obj = None
    if wm_image:
        wm_img_obj = Image.open(io.BytesIO(await wm_image.read())).convert("RGBA")

    settings = _wm_settings(
        wm_type, text, font_name, font_size, color, stroke_color, stroke_width,
        bold, italic, underline, opacity, rotation, x_pct, y_pct,
        tiled, tile_spacing, out_format, quality, resize_w, resize_h, keep_aspect,
    )
    settings["wm_img"] = wm_img_obj
    ext = "jpg" if out_format == "jpeg" else out_format

    zip_buf = io.BytesIO()
    ok, errors = 0, []

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for upload in images:
            try:
                src = Image.open(io.BytesIO(await upload.read()))
                result = apply_watermark(src, **settings)
                base = os.path.splitext(upload.filename or f"img_{ok+1}")[0]
                zf.writestr(f"{zip_folder}/{filename_prefix}{base}.{ext}", result)
                ok += 1
            except Exception as e:
                errors.append({"file": upload.filename, "error": str(e)})

    if ok == 0:
        raise HTTPException(500, {"message": "ประมวลผลล้มเหลวทุกไฟล์", "errors": errors})

    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf, media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="watermarked.zip"',
            "X-Processed": str(ok),
            "X-Errors": str(len(errors)),
        },
    )


# ─────────────────────────────────────────────────────────────
# Google Drive Upload helpers
# ─────────────────────────────────────────────────────────────
_UPLOAD_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",  "webp": "image/webp",
    "gif": "image/gif",  "bmp":  "image/bmp",
    "tif": "image/tiff", "tiff": "image/tiff",
}

# ── In-memory job store (resets on server restart; fine for local use) ──
# Structure:  job_id → { status, total, uploaded, errors, files, error_list, created_at }
_UPLOAD_JOBS: dict[str, dict] = {}

# Max concurrent Drive API calls per background job (keeps memory + rate-limit low)
_DRIVE_SEM_LIMIT = 2


async def drive_upload_file(
    client: httpx.AsyncClient,
    token: str,
    folder_id: str,
    filename: str,
    content: bytes,
    mime_type: str = "image/jpeg",
) -> dict:
    """Upload one file to a Drive folder using multipart upload."""
    boundary = "wmpr0_mp_boundary"
    metadata = json.dumps({"name": filename, "parents": [folder_id]})
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{metadata}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--".encode("utf-8")

    r = await client.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        content=body,
        timeout=120,
    )
    if r.status_code not in (200, 201):
        raise ValueError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


async def _run_upload_job(job_id: str, folder_id: str, files: list[dict]) -> None:
    """
    Background task: upload all files to Drive with bounded concurrency.
    Runs after the HTTP response is already sent — browser can close the tab.
    """
    job = _UPLOAD_JOBS[job_id]
    job["status"] = "uploading"
    sem = asyncio.Semaphore(_DRIVE_SEM_LIMIT)

    async def _one(fd: dict) -> None:
        name = fd["name"]
        async with sem:
            try:
                # Each file gets its own short-lived client to avoid timeouts
                async with httpx.AsyncClient(
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=2)
                ) as client:
                    token = await _get_access_token(client)
                    info  = await drive_upload_file(
                        client, token, folder_id, name, fd["content"], fd["mime"]
                    )
                file_id = info.get("id", "")
                job["files"].append({
                    "name": info.get("name"),
                    "id":   file_id,
                    "url":  f"https://drive.google.com/file/d/{file_id}/view",
                })
                job["uploaded"] += 1
            except Exception as exc:
                job["errors"] += 1
                job["error_list"].append({"file": name, "error": str(exc)})
            finally:
                fd["content"] = None  # free image bytes from RAM as soon as possible

    await asyncio.gather(*[_one(fd) for fd in files])
    job["status"] = "done"
    job["finished_at"] = time.time()

    # Auto-cleanup after 2 hours so the dict doesn't grow forever
    await asyncio.sleep(7200)
    _UPLOAD_JOBS.pop(job_id, None)


@app.post("/api/drive/upload")
async def upload_to_drive(
    background_tasks: BackgroundTasks,
    folder_id: str = Form(...),
    images: List[UploadFile] = File(...),
):
    """
    Queue watermarked images for background upload to Google Drive.

    Returns a job_id immediately — the actual Drive uploads happen in a
    background task so the browser can close the tab without stopping the job.
    Poll GET /api/drive/job/{job_id} to track progress.
    """
    # Read all file bytes NOW (before the request body is released)
    files: list[dict] = []
    for upload in images:
        content = await upload.read()
        ext  = (upload.filename or "img.jpg").rsplit(".", 1)[-1].lower()
        mime = _UPLOAD_MIME.get(ext, "image/jpeg")
        name = upload.filename or f"image.{ext}"
        files.append({"name": name, "content": content, "mime": mime})

    job_id = uuid.uuid4().hex[:12]
    _UPLOAD_JOBS[job_id] = {
        "status":      "queued",
        "total":       len(files),
        "uploaded":    0,
        "errors":      0,
        "files":       [],
        "error_list":  [],
        "created_at":  time.time(),
        "finished_at": None,
    }

    background_tasks.add_task(_run_upload_job, job_id, folder_id, files)

    return {"job_id": job_id, "queued": len(files)}


@app.get("/api/drive/job/{job_id}")
async def get_upload_job(job_id: str):
    """Poll upload job status. Returns progress + list of uploaded file URLs."""
    job = _UPLOAD_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found — server may have restarted")
    return job


# ─────────────────────────────────────────────────────────────
# Serve frontend (index.html) — must be last
# ─────────────────────────────────────────────────────────────
@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    """Serve index.html for every non-API path so the app works at http://localhost:8000/"""
    index = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index, media_type="text/html")
    return Response(
        "index.html not found — make sure backend/ is inside the project root",
        status_code=404,
    )
