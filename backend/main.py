"""
WaterMark Pro — Backend API
FastAPI + Pillow  |  Python 3.9+

Run:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

Endpoints:
    GET  /              — API info
    GET  /health        — health check
    POST /api/watermark          — single image → returns watermarked image
    POST /api/watermark/batch    — multiple images → returns ZIP
"""

import io
import math
import os
import zipfile
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="WaterMark Pro API",
    description="Backend image-watermarking service for WaterMark Pro",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# Font helpers
# ─────────────────────────────────────────────────────────────
_FONT_SEARCH_PATHS = [
    # Windows
    "C:/Windows/Fonts/",
    # macOS
    "/Library/Fonts/",
    "/System/Library/Fonts/",
    # Linux
    "/usr/share/fonts/truetype/liberation/",
    "/usr/share/fonts/truetype/dejavu/",
    "/usr/share/fonts/truetype/freefont/",
    "/usr/share/fonts/",
]

_FONT_ALIASES = {
    "arial":             ["arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"],
    "arial black":       ["ariblk.ttf", "ArialBlack.ttf"],
    "georgia":           ["georgia.ttf", "Georgia.ttf"],
    "times new roman":   ["times.ttf", "Times New Roman.ttf", "LiberationSerif-Regular.ttf"],
    "courier new":       ["cour.ttf", "Courier New.ttf", "LiberationMono-Regular.ttf"],
    "verdana":           ["verdana.ttf", "Verdana.ttf"],
    "impact":            ["impact.ttf", "Impact.ttf"],
    "trebuchet ms":      ["trebuc.ttf"],
    "comic sans ms":     ["comic.ttf"],
}


def _load_font(name: str, size: int, bold: bool = False, italic: bool = False) -> ImageFont.FreeTypeFont:
    key = name.lower().strip()
    candidates = _FONT_ALIASES.get(key, [name + ".ttf", name])

    for base_dir in _FONT_SEARCH_PATHS:
        for fname in candidates:
            path = os.path.join(base_dir, fname)
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass

    # Final fallback — PIL default (no size control in older Pillow)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r, g, b, alpha)


# ─────────────────────────────────────────────────────────────
# Core watermark logic
# ─────────────────────────────────────────────────────────────
def _draw_text_at(
    layer: Image.Image,
    cx: int,
    cy: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill_rgba: tuple,
    stroke_rgba: tuple,
    stroke_width: int,
    underline: bool,
) -> None:
    d = ImageDraw.Draw(layer)
    font_size = font.size
    line_h = int(font_size * 1.3)
    lines = text.split("\n")
    start_y = cy - (len(lines) * line_h) // 2

    for i, line in enumerate(lines):
        ly = start_y + i * line_h
        bbox = d.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        lx = cx - tw // 2

        if stroke_width > 0:
            d.text(
                (lx, ly), line, font=font,
                fill=stroke_rgba,
                stroke_width=stroke_width,
                stroke_fill=stroke_rgba,
            )
        d.text((lx, ly), line, font=font, fill=fill_rgba)

        if underline:
            uw = max(1, int(font_size * 0.08))
            d.rectangle(
                [lx, ly + int(font_size * 0.5), lx + tw, ly + int(font_size * 0.5) + uw],
                fill=fill_rgba,
            )


def _draw_img_at(
    layer: Image.Image,
    cx: int,
    cy: int,
    wm_img: Image.Image,
    canvas_w: int,
    size_pct: float,
    opacity: float,
) -> None:
    dw = int(canvas_w * size_pct)
    if dw <= 0:
        return
    ratio = wm_img.width / wm_img.height
    dh = max(1, int(dw / ratio))
    resized = wm_img.resize((dw, dh), Image.LANCZOS).convert("RGBA")

    # Apply opacity to alpha channel
    r, g, b, a = resized.split()
    a = a.point(lambda p: int(p * opacity))
    resized.putalpha(a)

    layer.paste(resized, (cx - dw // 2, cy - dh // 2), resized)


def apply_watermark(
    img: Image.Image,
    *,
    wm_type: str = "text",
    # Text settings
    text: str = "© Copyright",
    font_name: str = "Arial",
    font_size: int = 36,
    color: str = "#ffffff",
    stroke_color: str = "#000000",
    stroke_width: int = 2,
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
    # Image settings
    wm_img: Optional[Image.Image] = None,
    wm_img_size_pct: float = 0.25,
    # Common
    opacity: float = 0.7,
    rotation: float = -30.0,
    x_pct: float = 50.0,
    y_pct: float = 50.0,
    tiled: bool = False,
    tile_spacing: int = 100,
    shadow: bool = False,
    multiply: bool = False,
    # Output
    out_format: str = "jpeg",
    quality: int = 92,
    resize_w: Optional[int] = None,
    resize_h: Optional[int] = None,
    keep_aspect: bool = True,
) -> bytes:
    """Return watermarked image as bytes."""

    base = img.convert("RGBA")
    bw, bh = base.size

    # Optional resize
    if resize_w or resize_h:
        if keep_aspect:
            ratio = bw / bh
            if resize_w and resize_h:
                if bw / bh > resize_w / resize_h:
                    resize_h = int(resize_w / ratio)
                else:
                    resize_w = int(resize_h * ratio)
            elif resize_w:
                resize_h = int(resize_w / ratio)
            else:
                resize_w = int(resize_h * ratio)
        base = base.resize((resize_w or bw, resize_h or bh), Image.LANCZOS)
        bw, bh = base.size

    alpha_val = int(opacity * 255)
    font = _load_font(font_name, font_size, bold, italic)
    fill_rgba = _hex_to_rgba(color, alpha_val)
    stroke_rgba = _hex_to_rgba(stroke_color, alpha_val)

    wm_layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))

    def place_wm(layer: Image.Image, cx: int, cy: int) -> None:
        if wm_type in ("text", "both") and text:
            _draw_text_at(layer, cx, cy, text, font, fill_rgba, stroke_rgba, stroke_width, underline)
        if wm_type in ("image", "both") and wm_img:
            _draw_img_at(layer, cx, cy, wm_img, bw, wm_img_size_pct, opacity)

    if tiled:
        max_line_len = max((len(l) for l in text.split("\n")), default=4) if text else 4
        cell_w = max(60, int(max_line_len * font_size * 0.6 + tile_spacing + 20))
        cell_h = int(font_size * 1.3 + tile_spacing)
        pad = max(cell_w, cell_h)
        cols = math.ceil(bw / cell_w) + 3
        rows = math.ceil(bh / cell_h) + 3

        for row in range(-1, rows):
            for col in range(-1, cols):
                cx = col * cell_w + (cell_w // 2 if row % 2 == 0 else 0)
                cy = row * cell_h + cell_h // 2

                tile = Image.new("RGBA", (pad * 2, pad * 2), (0, 0, 0, 0))
                place_wm(tile, pad, pad)
                if rotation != 0:
                    tile = tile.rotate(-rotation, expand=False, resample=Image.BICUBIC)

                ox = cx - pad
                oy = cy - pad
                # Clip paste region to layer bounds
                wm_layer.paste(tile, (ox, oy), tile)
    else:
        x = int(bw * x_pct / 100)
        y = int(bh * y_pct / 100)

        # Create oversized tmp for rotation
        size = int(math.sqrt(bw * bw + bh * bh)) + font_size * 2
        tmp = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        place_wm(tmp, size // 2, size // 2)
        if rotation != 0:
            tmp = tmp.rotate(-rotation, expand=False, resample=Image.BICUBIC)
        wm_layer.paste(tmp, (x - size // 2, y - size // 2), tmp)

    # Composite onto base
    if multiply:
        result = Image.alpha_composite(base, wm_layer)
    else:
        result = Image.alpha_composite(base, wm_layer)

    # Convert back to RGB for JPEG
    if out_format == "jpeg":
        final = Image.new("RGB", result.size, (255, 255, 255))
        final.paste(result, mask=result.split()[3])
    elif out_format == "webp":
        final = result
    else:
        final = result

    buf = io.BytesIO()
    save_kwargs = {"format": out_format.upper().replace("JPEG", "JPEG")}
    if out_format in ("jpeg", "webp"):
        save_kwargs["quality"] = quality
    if out_format == "webp":
        save_kwargs["method"] = 4
    final.save(buf, **save_kwargs)
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────
# Shared form parser
# ─────────────────────────────────────────────────────────────
def _parse_form(
    wm_type: str,
    text: str,
    font_name: str,
    font_size: int,
    color: str,
    stroke_color: str,
    stroke_width: int,
    bold: bool,
    italic: bool,
    underline: bool,
    opacity: float,
    rotation: float,
    x_pct: float,
    y_pct: float,
    tiled: bool,
    tile_spacing: int,
    shadow: bool,
    multiply: bool,
    out_format: str,
    quality: int,
    resize_w: Optional[int],
    resize_h: Optional[int],
    keep_aspect: bool,
) -> dict:
    return dict(
        wm_type=wm_type,
        text=text,
        font_name=font_name,
        font_size=font_size,
        color=color,
        stroke_color=stroke_color,
        stroke_width=stroke_width,
        bold=bold,
        italic=italic,
        underline=underline,
        opacity=opacity,
        rotation=rotation,
        x_pct=x_pct,
        y_pct=y_pct,
        tiled=tiled,
        tile_spacing=tile_spacing,
        shadow=shadow,
        multiply=multiply,
        out_format=out_format,
        quality=quality,
        resize_w=resize_w or None,
        resize_h=resize_h or None,
        keep_aspect=keep_aspect,
    )


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "name": "WaterMark Pro API",
        "version": "1.0.0",
        "endpoints": {
            "POST /api/watermark":       "Single image → returns watermarked file",
            "POST /api/watermark/batch": "Multiple images → returns ZIP",
            "GET  /health":              "Health check",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/watermark")
async def watermark_single(
    image: UploadFile = File(..., description="Source image"),
    wm_image: Optional[UploadFile] = File(None, description="Watermark image (for image/both mode)"),
    # Watermark
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
    shadow: bool = Form(False),
    multiply: bool = Form(False),
    # Output
    out_format: str = Form("jpeg"),
    quality: int = Form(92),
    resize_w: Optional[int] = Form(None),
    resize_h: Optional[int] = Form(None),
    keep_aspect: bool = Form(True),
    filename_prefix: str = Form("wm_"),
):
    try:
        src_bytes = await image.read()
        src_img = Image.open(io.BytesIO(src_bytes))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"ไม่สามารถเปิดไฟล์รูปได้: {e}")

    wm_img_obj = None
    if wm_image:
        try:
            wm_bytes = await wm_image.read()
            wm_img_obj = Image.open(io.BytesIO(wm_bytes)).convert("RGBA")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"ไม่สามารถเปิดไฟล์ลายน้ำได้: {e}")

    try:
        result_bytes = apply_watermark(
            src_img,
            wm_img=wm_img_obj,
            **_parse_form(
                wm_type, text, font_name, font_size, color, stroke_color, stroke_width,
                bold, italic, underline, opacity, rotation, x_pct, y_pct,
                tiled, tile_spacing, shadow, multiply,
                out_format, quality, resize_w, resize_h, keep_aspect,
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ประมวลผลล้มเหลว: {e}")

    ext = out_format if out_format != "jpeg" else "jpg"
    orig_name = os.path.splitext(image.filename or "image")[0]
    out_name = f"{filename_prefix}{orig_name}.{ext}"

    mime = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(out_format, "image/jpeg")
    return StreamingResponse(
        io.BytesIO(result_bytes),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


@app.post("/api/watermark/batch")
async def watermark_batch(
    images: list[UploadFile] = File(..., description="Source images (multiple)"),
    wm_image: Optional[UploadFile] = File(None, description="Watermark image"),
    # Watermark
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
    shadow: bool = Form(False),
    multiply: bool = Form(False),
    # Output
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
        try:
            wm_bytes = await wm_image.read()
            wm_img_obj = Image.open(io.BytesIO(wm_bytes)).convert("RGBA")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"ไม่สามารถเปิดไฟล์ลายน้ำได้: {e}")

    settings = _parse_form(
        wm_type, text, font_name, font_size, color, stroke_color, stroke_width,
        bold, italic, underline, opacity, rotation, x_pct, y_pct,
        tiled, tile_spacing, shadow, multiply,
        out_format, quality, resize_w, resize_h, keep_aspect,
    )

    ext = out_format if out_format != "jpeg" else "jpg"
    zip_buf = io.BytesIO()
    ok_count = 0
    errors = []

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, upload in enumerate(images):
            try:
                src_bytes = await upload.read()
                src_img = Image.open(io.BytesIO(src_bytes))
                result_bytes = apply_watermark(src_img, wm_img=wm_img_obj, **settings)
                orig_name = os.path.splitext(upload.filename or f"image_{i+1}")[0]
                out_name = f"{filename_prefix}{orig_name}.{ext}"
                zf.writestr(f"{zip_folder}/{out_name}", result_bytes)
                ok_count += 1
            except Exception as e:
                errors.append({"file": upload.filename, "error": str(e)})

    if ok_count == 0:
        raise HTTPException(status_code=500, detail={"message": "ประมวลผลทุกไฟล์ล้มเหลว", "errors": errors})

    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="watermarked.zip"',
            "X-Processed": str(ok_count),
            "X-Errors": str(len(errors)),
        },
    )
