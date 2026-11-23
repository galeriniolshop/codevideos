# -*- coding: utf-8 -*-
"""
PAPER COLLAGE STUDIO  v1.0
==========================
Generator video "paper collage" 9:16 (sampai 4K) otomatis dari satu TOPIK.

  Otak naskah : https://aiapifreekey.com  (API OpenAI-compatible, model free-ai, tanpa key)
  Visual      : kolase kertas sobek prosedural + pop-up teks / gambar / stiker emoji
  Audio       : SFX (hook, transisi, pop-up, closing) + backsound sintetis + voice-over (edge-tts)
  Output      : MP4 H.264 + AAC

Pemakaian:
  PaperCollage.exe                               -> buka GUI
  PaperCollage.exe /paper-collage "sejarah kopi" -> buka GUI & langsung generate
  PaperCollage.exe /paper-collage "sejarah kopi" --headless --res 4K --fps 30 --duration 30
"""
from __future__ import annotations

import asyncio
import datetime
import html
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from dataclasses import dataclass, asdict, field

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

APP = "Paper Collage Studio"
VERSION = "1.0"
API_BASE = "https://api.aiapifreekey.com/v1"
MODEL = "free-ai"
SR = 44100
TD = 0.55  # durasi transisi (detik)
NOWIN = 0x08000000 if os.name == "nt" else 0
BIC = getattr(Image, "Resampling", Image).BICUBIC
BIL = getattr(Image, "Resampling", Image).BILINEAR


# ----------------------------------------------------------------------------
# Util path
# ----------------------------------------------------------------------------
def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


ASSETS = os.path.join(app_dir(), "assets")


def user_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "PaperCollageStudio")
    os.makedirs(d, exist_ok=True)
    return d


def ffmpeg_exe() -> str:
    local = os.path.join(app_dir(), "ffmpeg.exe")
    if os.path.exists(local):
        return local
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


# ----------------------------------------------------------------------------
# Font
# ----------------------------------------------------------------------------
_FONT_CACHE: dict = {}
FONT_DIRS = [
    os.path.join(ASSETS, "fonts"),
    r"C:\Windows\Fonts",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/noto",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
]
ROLE_FONTS = {
    "display": ["display.ttf", "impact.ttf", "ariblk.ttf", "Impact.ttf", "DejaVuSans-Bold.ttf"],
    "body": ["body.ttf", "ariblk.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],
    "hand": ["hand.ttf", "segoeprb.ttf", "segoepr.ttf", "comicbd.ttf", "DejaVuSans-Bold.ttf"],
    "emoji": ["seguiemj.ttf", "NotoColorEmoji.ttf", "AppleColorEmoji.ttc"],
}


def find_font(role: str):
    for c in ROLE_FONTS[role]:
        for d in FONT_DIRS:
            p = os.path.join(d, c)
            if os.path.exists(p):
                return p
    return None


def load_font(role: str, size: int):
    key = (role, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    p = find_font(role)
    try:
        if role == "emoji":
            f = ImageFont.truetype(p, size, layout_engine=ImageFont.LAYOUT_BASIC)
        else:
            f = ImageFont.truetype(p, size)
    except Exception:
        try:
            f = ImageFont.load_default(size)
        except Exception:
            f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def wrap_lines(text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if font.getlength(t) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def fit_text(text, role, start, minimum, max_w, max_lines):
    size = start
    while True:
        font = load_font(role, size)
        lines = wrap_lines(text, font, max_w)
        widest = max(font.getlength(l) for l in lines)
        if (len(lines) <= max_lines and widest <= max_w) or size <= minimum:
            return font, size, lines
        size = max(minimum, int(size * 0.93))


# ----------------------------------------------------------------------------
# Easing
# ----------------------------------------------------------------------------
def _c(p):
    return min(max(p, 0.0), 1.0)


def ease_out_back(p, s=1.70158):
    p = _c(p) - 1
    return 1 + (s + 1) * p ** 3 + s * p ** 2


def ease_out_cubic(p):
    return 1 - (1 - _c(p)) ** 3


def ease_in_out(p):
    p = _c(p)
    return p * p * (3 - 2 * p)


def ease_out_bounce(p):
    p = _c(p)
    if p < 1 / 2.75:
        return 7.5625 * p * p
    if p < 2 / 2.75:
        p -= 1.5 / 2.75
        return 7.5625 * p * p + 0.75
    if p < 2.5 / 2.75:
        p -= 2.25 / 2.75
        return 7.5625 * p * p + 0.9375
    p -= 2.625 / 2.75
    return 7.5625 * p * p + 0.984375


# ----------------------------------------------------------------------------
# Kertas, stiker, kolase
# ----------------------------------------------------------------------------
PAPERS = [(244, 236, 216), (251, 224, 138), (190, 227, 208), (246, 198, 208),
          (188, 217, 238), (245, 156, 133), (205, 190, 235), (255, 255, 255)]
INK = (34, 28, 26)
THEMES = [  # (dasar, blok, strip)
    ((222, 196, 158), (244, 236, 216), (245, 156, 133)),
    ((190, 227, 208), (246, 198, 208), (251, 224, 138)),
    ((188, 217, 238), (244, 236, 216), (245, 156, 133)),
    ((246, 198, 208), (188, 217, 238), (251, 224, 138)),
    ((251, 224, 138), (190, 227, 208), (205, 190, 235)),
    ((205, 190, 235), (244, 236, 216), (190, 227, 208)),
]
HOOK_THEME = ((236, 98, 74), (251, 224, 138), (34, 28, 26))
CLOSE_THEME = ((34, 28, 26), (251, 224, 138), (236, 98, 74))
LETTER_COLORS = [(255, 255, 255), (251, 224, 138), (255, 120, 90), (190, 227, 208)]


def U(S, v):
    return int(round(v * S))


def torn_poly(x0, y0, w, h, amp, step, rnd):
    pts = []
    nx = max(2, int(w / step))
    ny = max(2, int(h / step))
    for i in range(nx + 1):
        pts.append((x0 + w * i / nx, y0 + rnd.uniform(-amp, amp)))
    for i in range(1, ny + 1):
        pts.append((x0 + w + rnd.uniform(-amp, amp), y0 + h * i / ny))
    for i in range(1, nx + 1):
        pts.append((x0 + w * (1 - i / nx), y0 + h + rnd.uniform(-amp, amp)))
    for i in range(1, ny):
        pts.append((x0 + rnd.uniform(-amp, amp), y0 + h * (1 - i / ny)))
    return pts


def star_pts(cx, cy, r1, r2, n, rot=-90, jitter=0.0, rnd=None):
    pts = []
    for i in range(n * 2):
        r = r1 if i % 2 == 0 else r2
        if jitter and rnd:
            r *= 1 + rnd.uniform(-jitter, jitter)
        a = math.radians(rot + i * 180 / n)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def paper_fill(size, color, nrng, amp=4.0):
    w, h = size
    n = nrng.normal(0, amp, (h, w)).astype(np.float32)
    arr = np.clip(np.array(color, np.float32)[None, None, :] + n[..., None], 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def cutout(mask, fill, S, ow=10, shadow=True, outline=(255, 255, 255)):
    """Mask(L)+fill -> stiker RGBA dengan outline putih & bayangan lembut."""
    w, h = mask.size
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    om = mask
    if ow > 0:
        k = max(1, U(S, ow))
        om = mask.filter(ImageFilter.GaussianBlur(k * 0.8)).point(lambda v: 255 if v > 16 else 0)
    if shadow:
        sh = om.filter(ImageFilter.GaussianBlur(max(1, U(S, 9)))).point(lambda v: int(v * 0.5))
        layer = Image.new("RGBA", (w, h), (30, 20, 10, 255))
        layer.putalpha(sh)
        out.alpha_composite(layer, (U(S, 6), U(S, 11)))
    if ow > 0:
        layer = Image.new("RGBA", (w, h), outline + (255,))
        layer.putalpha(om)
        out.alpha_composite(layer)
    f = fill.convert("RGBA") if isinstance(fill, Image.Image) else Image.new("RGBA", (w, h), tuple(fill) + (255,))
    f.putalpha(ImageChops.multiply(f.getchannel("A"), mask))
    out.alpha_composite(f)
    return out


def paper_block(S, wd, hd, color, rnd, nrng, amp=6, step=24, ow=10, torn=True, margin=44):
    m = U(S, margin)
    w, h = U(S, wd), U(S, hd)
    mask = Image.new("L", (w + 2 * m, h + 2 * m), 0)
    pts = torn_poly(m, m, w, h, U(S, amp) if torn else 0, U(S, step), rnd)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    return cutout(mask, paper_fill(mask.size, color, nrng), S, ow=ow)


def text_card(text, S, rnd, nrng, color=(244, 236, 216), ink=INK, role="body", size=64,
              max_w=780, caps=True, accent=None, pad=34, min_size=34, max_lines=3, torn=True):
    txt = text.upper() if caps else text
    font, sz, lines = fit_text(txt, role, U(S, size), U(S, min_size), U(S, max_w - 2 * pad), max_lines)
    lh = int(sz * 1.14)
    tw = int(max(font.getlength(l) for l in lines))
    th = lh * len(lines)
    m = U(S, 44)
    w = tw + 2 * U(S, pad)
    h = th + int(1.5 * U(S, pad))
    mask = Image.new("L", (w + 2 * m, h + 2 * m), 0)
    ImageDraw.Draw(mask).polygon(torn_poly(m, m, w, h, U(S, 5) if torn else 0, U(S, 22), rnd), fill=255)
    fill = paper_fill(mask.size, color, nrng)
    d = ImageDraw.Draw(fill)
    y = m + (h - th) // 2
    for l in lines:
        x = m + (w - font.getlength(l)) / 2
        if accent:
            d.text((x + U(S, 3), y + U(S, 3)), l, font=font, fill=accent)
        d.text((x, y), l, font=font, fill=ink)
        y += lh
    return cutout(mask, fill, S, ow=9)


def letter_sticker(text, S, nrng, size=120, max_w=940, color=(255, 255, 255), max_lines=3,
                   role="display", ink=INK, stroke=7, ow=15):
    """Huruf kertas-gunting: isi berwarna, garis tinta, outline putih tebal."""
    font, sz, lines = fit_text(text.upper(), role, U(S, size), U(S, 50), U(S, max_w), max_lines)
    lh = int(sz * 1.08)
    tw = int(max(font.getlength(l) for l in lines))
    th = lh * len(lines)
    m = U(S, 60)
    mask = Image.new("L", (tw + 2 * m, th + 2 * m), 0)
    fill = Image.new("RGB", mask.size, ink)
    dm, df = ImageDraw.Draw(mask), ImageDraw.Draw(fill)
    sw = U(S, stroke)
    y = m
    for l in lines:
        x = m + (tw - font.getlength(l)) / 2
        dm.text((x, y), l, font=font, fill=255, stroke_width=sw, stroke_fill=255)
        df.text((x, y), l, font=font, fill=color)
        y += lh
    return cutout(mask, fill, S, ow=ow)


def tape(S, rnd, wd=200, hd=62, rot=0, color=(240, 225, 160, 175)):
    w, h = U(S, wd), U(S, hd)
    img = Image.new("RGBA", (w + 20, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    z = max(3, U(S, 7))
    pts = [(10, 0)]
    for i in range(1, 8):
        pts.append((10 + (-z if i % 2 else 0), h * i / 8))
    pts += [(10 + w, h)]
    for i in range(7, 0, -1):
        pts.append((10 + w + (z if i % 2 else 0), h * i / 8))
    d.polygon(pts, fill=color)
    return img.rotate(rot, expand=True, resample=BIC)


def burst_sticker(S, size_d, color, rnd, nrng, n=14):
    m = U(S, 60)
    sz = U(S, size_d)
    mask = Image.new("L", (sz + 2 * m, sz + 2 * m), 0)
    c = m + sz / 2
    ImageDraw.Draw(mask).polygon(star_pts(c, c, sz / 2, sz / 2 * 0.80, n, jitter=0.05, rnd=rnd), fill=255)
    return cutout(mask, paper_fill(mask.size, color, nrng), S, ow=12)


def star_sticker(S, size_d, color, rnd, nrng, n=5, rot=-90):
    m = U(S, 50)
    sz = U(S, size_d)
    mask = Image.new("L", (sz + 2 * m, sz + 2 * m), 0)
    c = m + sz / 2
    ImageDraw.Draw(mask).polygon(star_pts(c, c, sz / 2, sz / 4.2, n, rot=rot), fill=255)
    return cutout(mask, paper_fill(mask.size, color, nrng), S, ow=9)


def _render_emoji(ch, px):
    """Render 1 emoji berwarna -> RGBA (kotak px x px). Raise jika font tidak ada / glyph tofu."""
    path = find_font("emoji")
    if not path:
        raise ValueError("font emoji tidak ada")
    bitmap = path.lower().endswith(("notocoloremoji.ttf", ".ttc"))
    fsz = 109 if bitmap else int(px * 0.78)
    f = load_font("emoji", fsz)

    def draw(c):
        im = Image.new("RGBA", (fsz * 2, fsz * 2), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((fsz // 4, fsz // 4), c, font=f, embedded_color=True)
        return im
    a, miss = draw(ch), draw("\U0010FFFF")
    bb = a.getchannel("A").getbbox()
    if bb is None or (bb[2] - bb[0]) < 8:
        raise ValueError("glyph kosong")
    if ImageChops.difference(a, miss).getbbox() is None:
        raise ValueError("glyph tofu")
    a = a.crop(bb)
    sc = px * 0.9 / max(a.size)
    return a.resize((max(1, int(a.width * sc)), max(1, int(a.height * sc))), BIC)


USED_OPENMOJI = False


def _openmoji(ch, px):
    """Cadangan: unduh PNG emoji OpenMoji (CC BY-SA 4.0) bila font emoji sistem tidak ada / gagal."""
    global USED_OPENMOJI
    import requests
    cp = "-".join(f"{ord(c):X}" for c in ch if ord(c) != 0xFE0F)
    cache = os.path.join(tempfile.gettempdir(), "pcs_emoji")
    os.makedirs(cache, exist_ok=True)
    for name in (cp, cp + "-FE0F"):
        path = os.path.join(cache, name + ".png")
        if not os.path.exists(path):
            r = requests.get(f"https://cdn.jsdelivr.net/gh/hfg-gmuend/openmoji@15.0.0/color/618x618/{name}.png",
                             timeout=10)
            if r.status_code == 200 and r.content[:4] == b"\x89PNG":
                with open(path, "wb") as f:
                    f.write(r.content)
        if os.path.exists(path):
            a = Image.open(path).convert("RGBA")
            bb = a.getchannel("A").getbbox()
            if bb:
                a = a.crop(bb)
                sc = px * 0.9 / max(a.size)
                USED_OPENMOJI = True
                return a.resize((max(1, int(a.width * sc)), max(1, int(a.height * sc))), BIC)
    raise ValueError("emoji tidak ditemukan")


def emoji_sticker(ch, size_d, S, rnd, nrng, fallback_color=(251, 224, 138)):
    ch = (ch or "").strip()
    ch = ch[:2] if len(ch) > 1 and ch[1] == "\ufe0f" else ch[:1]
    sz = U(S, size_d)
    m = U(S, 50)
    try:
        if not ch or ord(ch[0]) < 0x2190:
            raise ValueError("bukan emoji")
        try:
            g = _render_emoji(ch, sz)
        except Exception:
            g = _openmoji(ch, sz)
        tmp = Image.new("RGBA", (sz + 2 * m, sz + 2 * m), (0, 0, 0, 0))
        tmp.alpha_composite(g, ((tmp.width - g.width) // 2, (tmp.height - g.height) // 2))
        return _emoji_cut(tmp, S)
    except Exception:
        return star_sticker(S, size_d, fallback_color, rnd, nrng, n=7)


def _emoji_cut(tmp, S):
    alpha = tmp.getchannel("A")
    rgb = Image.new("RGB", tmp.size, (255, 255, 255))
    rgb.paste(tmp, (0, 0), tmp)
    return cutout(alpha, rgb, S, ow=14)


def photo_card(photo, S, rnd, nrng, wd=800, hd=640, caption=""):
    w, h = U(S, wd), U(S, hd)
    m = U(S, 56)
    b = U(S, 24)
    bottom = U(S, 84)
    mask = Image.new("L", (w + 2 * m, h + 2 * m), 0)
    ImageDraw.Draw(mask).polygon(torn_poly(m, m, w, h, U(S, 4), U(S, 26), rnd), fill=255)
    fill = paper_fill(mask.size, (252, 250, 244), nrng, 3)
    iw, ih = w - 2 * b, h - b - bottom
    pw, ph = photo.size
    sc = max(iw / pw, ih / ph)
    p2 = photo.resize((max(1, int(pw * sc)), max(1, int(ph * sc))), BIC)
    l, t = (p2.width - iw) // 2, (p2.height - ih) // 2
    fill.paste(p2.crop((l, t, l + iw, t + ih)), (m + b, m + b))
    if caption:
        d = ImageDraw.Draw(fill)
        f, _, ln = fit_text(caption, "hand", U(S, 46), U(S, 26), iw, 1)
        d.text((m + (w - f.getlength(ln[0])) / 2, m + h - bottom + U(S, 14)), ln[0], font=f, fill=INK)
    card = cutout(mask, fill, S, ow=0)
    for cx, rot in ((0.18, -14), (0.82, 12)):
        tp = tape(S, rnd, 190, 58, rot)
        card.alpha_composite(tp, (int(card.width * cx - tp.width / 2), int(m - tp.height / 2)))
    return card


# ----------------------------------------------------------------------------
# Gaya 2: Motion Graphics Flat
# ----------------------------------------------------------------------------
FLAT_THEMES = [
    {"bg": (24, 28, 46), "onbg": (255, 255, 255), "accent": (255, 196, 0), "accent2": (255, 92, 96),
     "panel": (255, 255, 255), "ink": (24, 28, 46), "soft": (255, 255, 255)},
    {"bg": (246, 243, 236), "onbg": (26, 28, 38), "accent": (255, 107, 61), "accent2": (0, 168, 204),
     "panel": (255, 255, 255), "ink": (26, 28, 38), "soft": (26, 28, 38)},
    {"bg": (11, 61, 74), "onbg": (255, 255, 255), "accent": (255, 211, 105), "accent2": (120, 220, 200),
     "panel": (255, 255, 255), "ink": (11, 61, 74), "soft": (255, 255, 255)},
    {"bg": (58, 22, 60), "onbg": (255, 255, 255), "accent": (255, 138, 170), "accent2": (150, 210, 255),
     "panel": (255, 255, 255), "ink": (58, 22, 60), "soft": (255, 255, 255)},
    {"bg": (255, 244, 214), "onbg": (40, 30, 20), "accent": (255, 107, 61), "accent2": (60, 90, 200),
     "panel": (255, 255, 255), "ink": (40, 30, 20), "soft": (40, 30, 20)},
]
FLAT_TRANSITIONS = ["slide_l", "zoom", "slide_u", "drop", "zoom", "slide_l", "slide_u", "zoom"]


def _c4(c):
    return c if len(c) == 4 else tuple(c) + (255,)


def _mix(c1, c2, t):
    """Campur dua warna RGB (flat design: variasi tonal, tanpa alpha)."""
    return tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))


def flat_bg(W, H, S, th, seed):
    """Latar flat: warna solid + bentuk geometris besar + titik-titik."""
    rnd = random.Random(seed)
    bg = th["bg"]
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img, "RGBA")
    ac, ac2 = th["accent"], th["accent2"]
    r = int(W * rnd.uniform(0.42, 0.58))
    cx, cy = int(W * rnd.uniform(0.5, 0.95)), int(H * rnd.uniform(-0.06, 0.1))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_mix(bg, th["soft"], 0.09))
    r2 = int(W * rnd.uniform(0.16, 0.26))
    cx2, cy2 = int(W * rnd.uniform(-0.06, 0.14)), int(H * rnd.uniform(0.5, 0.66))
    d.ellipse((cx2 - r2, cy2 - r2, cx2 + r2, cy2 + r2), outline=_mix(bg, ac2, 0.5), width=max(2, U(S, 12)))
    y0 = int(H * rnd.uniform(0.6, 0.72))
    d.polygon([(0, y0), (W, y0 - int(H * 0.11)), (W, y0 - int(H * 0.11) + U(S, 24)), (0, y0 + U(S, 24))],
              fill=_mix(bg, ac, 0.12))
    step = U(S, 44)
    for gy in range(3):
        for gx in range(5):
            px, py = U(S, 58) + gx * step, U(S, 1160) + gy * step
            d.ellipse((px - U(S, 5), py - U(S, 5), px + U(S, 5), py + U(S, 5)), fill=_mix(bg, th["soft"], 0.22))
    for _ in range(6):
        px, py = rnd.uniform(0, W), rnd.uniform(0, H)
        rr = U(S, rnd.uniform(6, 16))
        d.ellipse((px - rr, py - rr, px + rr, py + rr), fill=_mix(bg, ac2, 0.45))
    return img


def flat_text(text, S, role="display", size=150, color=(255, 255, 255), max_w=940, max_lines=3,
              line_gap=1.02, align="center"):
    txt = text.upper() if role == "display" else text
    font, sz, lines = fit_text(txt, role, U(S, size), U(S, 36), U(S, max_w), max_lines)
    lh = int(sz * line_gap)
    w = int(max(font.getlength(l) for l in lines)) + U(S, 8)
    h = lh * len(lines) + U(S, 6)
    img = Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, l in enumerate(lines):
        x = (w - font.getlength(l)) / 2 if align == "center" else 0
        d.text((x, i * lh), l, font=font, fill=_c4(color))
    return img


def flat_pill(text, S, bg, fg, size=54, padx=40, pady=20, radius=None, caps=True, role="body"):
    txt = str(text).upper() if caps else str(text)
    font, sz, lines = fit_text(txt, role, U(S, size), U(S, 24), U(S, 980), 1)
    tw = int(font.getlength(lines[0])) if lines else U(S, 20)
    px, py = U(S, padx), U(S, pady)
    w, h = max(1, tw + 2 * px), max(1, int(sz * 1.25) + 2 * py)
    r = h // 2 if radius is None else U(S, radius)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=_c4(bg))
    ImageDraw.Draw(img).text((px, h // 2), lines[0], font=font, fill=_c4(fg), anchor="lm")
    return img


def flat_panel(w, h, color, S=1, radius=30):
    img = Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), radius=max(1, U(S, radius)), fill=_c4(color))
    return img


def flat_badge_num(txt, S, bg, fg, size=64):
    font, sz, lines = fit_text(txt, "display", U(S, size), U(S, 30), U(S, 300), 1)
    side = int(sz * 1.5)
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, side - 1, side - 1), radius=int(side * 0.26), fill=_c4(bg))
    d.text((side // 2, side // 2 + U(S, 2)), lines[0], font=font, fill=_c4(fg), anchor="mm")
    return img


def flat_photo(photo, S, th, wd=840, hd=640, radius=40):
    w, h = U(S, wd), U(S, hd)
    pw, ph = photo.size
    sc = max(w / pw, h / ph)
    p2 = photo.resize((max(1, int(pw * sc)), max(1, int(ph * sc))), BIC)
    l, t = (p2.width - w) // 2, (p2.height - h) // 2
    p2 = p2.crop((l, t, l + w, t + h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=U(S, radius), fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(p2, (0, 0), mask)
    ImageDraw.Draw(out).rounded_rectangle((0, 0, w - 1, h - 1), radius=U(S, radius),
                                          outline=_c4(th["accent"]), width=max(2, U(S, 10)))
    return out


def flat_emoji(ch, size_d, S, th):
    """Emoji di dalam lingkaran warna aksen (tanpa outline kertas)."""
    sz = U(S, size_d)
    img = Image.new("RGBA", (max(1, sz), max(1, sz)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, sz - 1, sz - 1), fill=_c4(th["accent"]))
    try:
        try:
            g = _render_emoji((ch or "✨")[0], int(sz * 0.6))
        except Exception:
            g = _openmoji((ch or "✨")[0], int(sz * 0.6))
        img.alpha_composite(g, ((sz - g.width) // 2, (sz - g.height) // 2))
    except Exception:
        d.ellipse((sz * 0.32, sz * 0.32, sz * 0.68, sz * 0.68), fill=_c4(th["bg"]))
    return img


def flat_shape(kind, size_d, S, color):
    sz = max(2, U(S, size_d))
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = _c4(color)
    if kind == "ring":
        d.ellipse((0, 0, sz - 1, sz - 1), outline=c, width=max(2, U(S, 10)))
    elif kind == "triangle":
        d.polygon([(sz / 2, 0), (sz, sz), (0, sz)], fill=c)
    elif kind == "square":
        d.rounded_rectangle((0, 0, sz - 1, sz - 1), radius=int(sz * 0.2), fill=c)
    elif kind == "plus":
        t = sz * 0.26
        d.rounded_rectangle((sz / 2 - t / 2, 0, sz / 2 + t / 2, sz), radius=t / 2, fill=c)
        d.rounded_rectangle((0, sz / 2 - t / 2, sz, sz / 2 + t / 2), radius=t / 2, fill=c)
    elif kind == "bolt":
        d.polygon([(sz * 0.62, 0), (sz * 0.18, sz * 0.55), (sz * 0.46, sz * 0.55), (sz * 0.38, sz),
                   (sz * 0.82, sz * 0.45), (sz * 0.54, sz * 0.45)], fill=c)
    else:
        d.ellipse((0, 0, sz - 1, sz - 1), fill=c)
    return img


def flat_caption(text, S, th, max_w=900):
    return flat_pill(text, S, th["panel"], th["ink"], size=54, padx=42, pady=22, radius=26, caps=False)


def make_background(W, H, S, theme, seed):
    rnd = random.Random(seed)
    nrng = np.random.default_rng(seed)
    base, blk, strip = theme
    sm = nrng.normal(0, 1, (max(2, H // 64), max(2, W // 64))).astype(np.float32)
    cloud = np.asarray(Image.fromarray(np.clip(sm * 40 + 128, 0, 255).astype(np.uint8)).resize((W, H), BIC),
                       dtype=np.float32) - 128
    noise = cloud * 0.12 + nrng.normal(0, 2.4, (H, W)).astype(np.float32)
    arr = np.empty((H, W, 3), np.uint8)
    for c in range(3):
        arr[..., c] = np.clip(base[c] + noise, 0, 255).astype(np.uint8)
    bg = Image.fromarray(arr, "RGB")
    d = ImageDraw.Draw(bg)
    dark = tuple(int(v * 0.86) for v in base)
    # halftone
    for gy in range(9):
        for gx in range(7):
            r = U(S, 3 + gx * 0.9) if gx < 7 else 0
            cx, cy = U(S, 1080 - 30 - gx * 34), U(S, 620 + gy * 34)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=dark)
    for gy in range(7):
        for gx in range(5):
            r = U(S, 2 + gx * 0.9)
            cx, cy = U(S, 30 + gx * 34), U(S, 1010 + gy * 34)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=dark)
    # strip atas & blok bawah
    sp = paper_block(S, 1240, 330, strip, rnd, nrng, amp=7)
    sp = sp.rotate(-2.2, expand=True, resample=BIC)
    bg.paste(sp, (int(W / 2 - sp.width / 2), U(S, 280) - sp.height // 2), sp)
    bp = paper_block(S, 1260, 760, blk, rnd, nrng, amp=9, step=30)
    bp = bp.rotate(1.6, expand=True, resample=BIC)
    bg.paste(bp, (int(W / 2 - bp.width / 2), U(S, 1500) - bp.height // 2), bp)
    # doodle
    inkc = tuple(int(v * 0.55 + 90) for v in base)
    for _ in range(3):
        x0, y0 = rnd.uniform(60, 900), rnd.uniform(520, 640)
        pts = [(U(S, x0 + i * 22), U(S, y0 + rnd.uniform(-10, 10))) for i in range(7)]
        d.line(pts, fill=inkc, width=max(2, U(S, 4)), joint="curve")
    for (sx, sy, sz) in ((90, 110, 70), (985, 1190, 60)):
        st = star_sticker(S, sz, rnd.choice(PAPERS[:6]), rnd, nrng, rot=rnd.choice((-90, -70)))
        bg.paste(st, (U(S, sx) - st.width // 2, U(S, sy) - st.height // 2), st)
    return bg


# ----------------------------------------------------------------------------
# Elemen & scene
# ----------------------------------------------------------------------------
class El:
    def __init__(self, img, cx, cy, t_in, rot=0.0, anim="pop", t_out=None, dur=0.42, z=0, idle=True, sfx=None):
        self.img, self.cx, self.cy, self.t_in = img, cx, cy, t_in
        self.rot, self.anim, self.t_out, self.dur = rot, anim, t_out, dur
        self.z, self.idle, self.sfx = z, idle, sfx
        self.cache = {}


class Scene:
    def __init__(self, kind, bg, D):
        self.kind, self.bg, self.D = kind, bg, D
        self.els: list[El] = []
        self.sfx: list = []  # (t, nama, gain)
        self.voice = None  # (t_mulai, array)

    def add(self, img, cx, cy, t_in, **kw):
        el = El(img, cx, cy, t_in, **kw)
        self.els.append(el)
        if el.sfx:
            self.sfx.append((t_in, el.sfx, 1.0))
        return el

    def finalize(self):
        self.els.sort(key=lambda e: (e.z, e.t_in))


def _xform(img, scale, rot):
    if abs(scale - 1) > 0.005:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), BIL)
    if abs(rot) > 0.01:
        img = img.rotate(rot, expand=True, resample=BIC)
    return img


def _alpha(img, a):
    """Turunkan opacity sprite (untuk animasi fade)."""
    if a >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda v: int(v * a)))
    return out


def _zoomed_bg(sc, zq, W, H):
    """Latar yang sudah di-zoom, di-cache per langkah zoom (murah: latar statis)."""
    cache = getattr(sc, "_bgz", None)
    if cache is None:
        cache = sc._bgz = {}
    k = round(zq, 3)
    im = cache.get(k)
    if im is None:
        im = sc.bg.resize((max(1, int(W * k)), max(1, int(H * k))), BIL) if k > 1.001 else sc.bg.copy()
        if len(cache) > 2:  # jaga RAM di cloud
            cache.pop(next(iter(cache)))
        cache[k] = im
    return im


def render_scene(sc: Scene, t, W, H, S, cam=True):
    bucket = int(t * 12)
    rj = random.Random(bucket * 7919 + 13)
    shake = cam and sc.kind == "hook" and t < 0.6 and getattr(sc, "style", "paper") != "flat"
    z = 1.0
    if cam:
        z = 1 + 0.04 * min(1.0, t / max(sc.D, 0.1))
    # jalur cepat: latar di-zoom (cache), elemen cukup digeser -> tanpa resize full-frame
    zq = round(z * 200) / 200 if (cam and not shake and z > 1.001) else 1.0
    if zq > 1.0:
        bgz = _zoomed_bg(sc, zq, W, H)
        fr = Image.new("RGB", (W, H), sc.bg.getpixel((0, 0)))
        fr.paste(bgz, ((W - bgz.width) // 2, (H - bgz.height) // 2))
    else:
        fr = sc.bg.copy()
    for el in sc.els:
        if t < el.t_in or (el.t_out is not None and t >= el.t_out):
            continue
        age = t - el.t_in
        p = min(1.0, age / el.dur)
        scl, dx, dy, rot = 1.0, 0.0, 0.0, el.rot
        if el.anim == "pop":
            scl = ease_out_back(p)
            rot += (1 - p) * 9 * math.sin(p * 10)
        elif el.anim == "stamp":
            scl = 1 + (1 - ease_out_cubic(p)) * 1.3
            rot += (1 - p) * -6
        elif el.anim == "slide_l":
            dx = -(1 - ease_out_cubic(p)) * W * 0.95
        elif el.anim == "slide_r":
            dx = (1 - ease_out_cubic(p)) * W * 0.95
        elif el.anim == "drop":
            dy = -(1 - ease_out_bounce(p)) * H * 0.6
        elif el.anim == "fade_up":
            dy += (1 - ease_out_cubic(p)) * U(S, 40)
        if scl < 0.03:
            continue
        jx = jy = 0
        if el.idle and p >= 1:
            rot += rj.choice((-0.6, 0.0, 0.6))
            jx, jy = rj.choice((-1, 0, 1)) * U(S, 3), rj.choice((-1, 0, 1)) * U(S, 3)
            dy += math.sin(math.floor(t * 12) / 12 * 2.0 + el.cx) * U(S, 4)
        key = (round(scl, 2), round(rot, 1))
        spr = el.cache.get(key)
        if spr is None:
            if len(el.cache) > 14:
                el.cache.clear()
            spr = _xform(el.img, scl, rot)
            el.cache[key] = spr
        x = int((el.cx - W / 2) * zq + W / 2 + dx + jx - spr.width / 2)
        y = int((el.cy - H / 2) * zq + H / 2 + dy + jy - spr.height / 2)
        if el.anim == "wipe_r":  # buka dari kiri ke kanan (motion graphics)
            full_w = spr.width
            w = int(full_w * max(0.0, ease_out_cubic(p)))
            if w < 2:
                continue
            spr = spr.crop((0, 0, w, spr.height))
            x = int((el.cx - W / 2) * zq + W / 2 + dx + jx - full_w / 2)
        elif el.anim == "fade_up":
            if p <= 0.001:
                continue
            spr = _alpha(spr, min(1.0, p))
        fr.paste(spr, (x, y), spr)
    if shake:
        cw, ch = int(W / z), int(H / z)
        amp = U(S, 16) * (1 - t / 0.6)
        sx, sy = int(rj.uniform(-amp, amp)), int(rj.uniform(-amp, amp))
        x0 = min(max((W - cw) // 2 + sx, 0), W - cw)
        y0 = min(max((H - ch) // 2 + sy, 0), H - ch)
        fr = fr.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), BIL)
    if sc.kind == "hook" and t < 0.18 and getattr(sc, "style", "paper") != "flat":
        fr = Image.blend(fr, Image.new("RGB", (W, H), (255, 255, 255)), 0.8 * (1 - t / 0.18))
    return fr


# ----------------------------------------------------------------------------
# Transisi
# ----------------------------------------------------------------------------
TRANSITIONS = ["slide_l", "torn_up", "zoom", "drop", "slide_u", "torn_up", "slide_l", "zoom"]


def _zoom(img, z):
    W, H = img.size
    cw, ch = int(W / z), int(H / z)
    x0, y0 = (W - cw) // 2, (H - ch) // 2
    return img.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), BIL)


def transition(A, B, p, kind, S, seed):
    W, H = A.size
    e = ease_in_out(p)
    if kind == "slide_l":
        out = Image.new("RGB", (W, H))
        out.paste(A, (-int(W * 0.22 * e), 0))
        x = int(W * (1 - e))
        out.paste(B, (x, 0))
        sw = U(S, 70)
        grad = Image.linear_gradient("L").rotate(90).resize((sw, H)).point(lambda v: int(v * 0.0 + (255 - v) * 0.35))
        dark = Image.new("RGB", (sw, H), (20, 12, 8))
        out.paste(dark, (x - sw, 0), grad.transpose(Image.FLIP_LEFT_RIGHT))
        return out
    if kind == "slide_u":
        out = Image.new("RGB", (W, H))
        out.paste(A, (0, -int(H * 0.2 * e)))
        y = int(H * (1 - e))
        out.paste(B, (0, y))
        sh = U(S, 70)
        grad = Image.linear_gradient("L").resize((W, sh)).point(lambda v: int((255 - v) * 0.35))
        out.paste(Image.new("RGB", (W, sh), (20, 12, 8)), (0, y - sh), grad.transpose(Image.FLIP_TOP_BOTTOM))
        return out
    if kind == "drop":
        out = A.copy()
        y = -int(H * (1 - ease_out_bounce(p)) * 0.9)
        out.paste(B, (0, y))
        return out
    if kind == "zoom":
        return Image.blend(_zoom(A, 1 + 0.5 * e), _zoom(B, 1.45 - 0.45 * e), e)
    # torn_up : sobekan kertas naik dari bawah
    rng = np.random.default_rng(seed)
    tooth = U(S, 7)
    jag = np.repeat(rng.uniform(-1, 1, W // tooth + 2), tooth)[:W] * U(S, 14)
    xs = np.linspace(0, 2 * math.pi, W)
    jag += (np.sin(xs * 1.7 + seed) * 0.6 + np.sin(xs * 4.3 + seed * 2) * 0.4) * U(S, 40)
    amp = U(S, 90)
    edge = ((1 - e) * (H + 2 * amp) - amp + jag)[None, :]
    a, b = np.asarray(A), np.asarray(B)
    ys = np.arange(H, dtype=np.float32)[:, None]
    rim = U(S, 16)
    mb = ys >= edge + rim
    mr = (ys >= edge) & ~mb
    band = U(S, 46)
    fac = np.where((ys < edge) & (ys > edge - band), 0.62 + 0.38 * (edge - ys) / band, 1.0).astype(np.float32)
    out = np.where(mb[..., None], b, (a * fac[..., None]).astype(np.uint8))
    out[mr] = (251, 247, 238)
    return Image.fromarray(out, "RGB")


# ----------------------------------------------------------------------------
# Audio sintetis
# ----------------------------------------------------------------------------
def _t(d):
    return np.arange(int(d * SR), dtype=np.float32) / SR


def sfx_pop():
    t = _t(0.16)
    f = 180 + 900 * np.exp(-t * 20)
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 26)
    rng = np.random.default_rng(1)
    x += rng.normal(0, 1, len(t)) * np.exp(-t * 180) * 0.25
    return (x * 0.9).astype(np.float32)


def sfx_whoosh(d=0.65):
    n = int(d * SR)
    rng = np.random.default_rng(7)
    w = rng.normal(0, 1, n).astype(np.float32)
    ks = (96, 40, 16, 6, 2)
    lp = {k: np.convolve(w, np.ones(k) / k, "same") for k in ks + tuple(4 * k for k in ks)}
    t = np.arange(n) / n
    s = 4 * np.sin(np.pi * t * 0.5 + 0.0) ** 1.0
    x = np.zeros(n, np.float32)
    for i, k in enumerate(ks):
        band = lp[k] - lp[4 * k]
        x += band * np.exp(-((s - i) ** 2) / 0.9)
    x *= np.sin(np.pi * t) ** 1.4
    return (x / (np.abs(x).max() + 1e-6) * 0.8).astype(np.float32)


def sfx_hit():
    t = _t(1.5)
    f = 38 + 90 * np.exp(-t * 6)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 2.6)
    rng = np.random.default_rng(3)
    nz = np.convolve(rng.normal(0, 1, len(t)), np.ones(5) / 5, "same") * np.exp(-t * 6) * 0.55
    clk = rng.normal(0, 1, len(t)) * np.exp(-t * 220) * 0.5
    x = boom + nz + clk
    return (x / (np.abs(x).max() + 1e-6) * 0.95).astype(np.float32)


def sfx_paper():
    rng = np.random.default_rng(11)
    n = int(0.38 * SR)
    w = rng.normal(0, 1, n).astype(np.float32)
    w = w - np.convolve(w, np.ones(10) / 10, "same")
    env = np.zeros(n, np.float32)
    for _ in range(7):
        c = rng.integers(0, n - 2000)
        L = int(rng.integers(900, 3200))
        env[c:c + L] = np.maximum(env[c:c + L], np.hanning(min(L, n - c))[:len(env[c:c + L])] * rng.uniform(0.4, 1))
    return (w * env * 0.8).astype(np.float32)


def sfx_chime():
    x = np.zeros(int(1.8 * SR), np.float32)
    for i, f in enumerate((784, 1046.5, 1318.5, 1568)):
        t = _t(1.5)
        w = (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t)) * np.exp(-t * 3.2) * 0.35
        o = int(i * 0.09 * SR)
        x[o:o + len(w)] += w
    return x


SYNTH_SFX = {"pop": sfx_pop, "whoosh": sfx_whoosh, "hit": sfx_hit, "paper": sfx_paper, "chime": sfx_chime}
_SFX_CACHE: dict = {}


def decode_audio(path):
    cmd = [ffmpeg_exe(), "-v", "error", "-i", path, "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "2",
           "-ar", str(SR), "-"]
    raw = subprocess.run(cmd, capture_output=True, creationflags=NOWIN).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).copy()


def get_sfx(name):
    if name in _SFX_CACHE:
        return _SFX_CACHE[name]
    arr = None
    for ext in (".wav", ".mp3", ".ogg", ".m4a"):
        p = os.path.join(ASSETS, "sfx", name + ext)
        if os.path.exists(p):
            try:
                arr = decode_audio(p)
                break
            except Exception:
                pass
    if arr is None:
        m = SYNTH_SFX[name]()
        arr = np.stack([m, m], 1)
    _SFX_CACHE[name] = arr
    return arr


MOODS = {
    "upbeat": dict(bpm=112, prog=[[60, 64, 67], [55, 59, 62], [57, 60, 64], [53, 57, 60]], kick=4),
    "chill": dict(bpm=84, prog=[[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]], kick=2),
    "dramatic": dict(bpm=100, prog=[[57, 60, 64], [53, 57, 60], [55, 59, 62], [52, 56, 59]], kick=2),
    "mysterious": dict(bpm=90, prog=[[57, 60, 63], [53, 56, 60], [50, 53, 57], [52, 56, 59]], kick=2),
    "inspiring": dict(bpm=104, prog=[[62, 66, 69], [55, 59, 62], [59, 62, 66], [57, 61, 64]], kick=4),
}


def _mf(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def synth_music(dur, mood, seed=5):
    cfg = MOODS.get(mood, MOODS["upbeat"])
    spb = 60.0 / cfg["bpm"]
    n = int((dur + 3) * SR)
    out = np.zeros(n, np.float32)
    rng = np.random.default_rng(seed)
    cache = {}

    def put(t0, w):
        a = int(t0 * SR)
        if a >= n:
            return
        e = min(n, a + len(w))
        out[a:e] += w[:e - a]

    def pluck(f, d=0.32, v=0.07):
        k = ("p", round(f, 1))
        if k not in cache:
            t = _t(d)
            cache[k] = ((np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t)) * np.exp(-t * 9)).astype(np.float32)
        return cache[k] * v

    def bass(f, d, v=0.2):
        t = _t(d)
        return (np.sin(2 * np.pi * f * t) + 0.2 * np.sin(4 * np.pi * f * t)) * np.exp(-t * 2.2) * v

    def pad(f, d, v=0.04):
        t = _t(d)
        env = np.minimum(1, t / 0.35) * np.minimum(1, (d - t) / 0.5)
        return (np.sin(2 * np.pi * f * t) + np.sin(2 * np.pi * f * 1.004 * t)) * env * v

    def kick():
        t = _t(0.28)
        f = 50 + 110 * np.exp(-t * 28)
        return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 11) * 0.5

    def hat():
        w = rng.normal(0, 1, int(0.05 * SR))
        w = w - np.convolve(w, np.ones(4) / 4, "same")
        return w * np.exp(-_t(0.05) * 70) * 0.05

    nb = int((dur + 3) / spb) + 1
    for b in range(nb):
        chord = cfg["prog"][(b // 4) % len(cfg["prog"])]
        t0 = b * spb
        if b % 4 == 0:
            for m in chord:
                put(t0, pad(_mf(m), spb * 4.1))
        if b % 2 == 0:
            put(t0, bass(_mf(chord[0] - 24), spb * 1.9))
        for h in range(2):
            m = chord[(b * 2 + h) % 3] + 12
            put(t0 + h * spb / 2, pluck(_mf(m)))
        if cfg["kick"] == 4 or b % 2 == 0:
            put(t0, kick())
        put(t0 + spb / 2, hat())
    out = np.convolve(out, np.ones(5) / 5, "same")
    cr = (rng.random(n) > 0.9993).astype(np.float32) * rng.normal(0, 0.03, n).astype(np.float32)
    out += cr
    out = out[:int(dur * SR)]
    fi, fo = int(0.5 * SR), int(2.0 * SR)
    out[:fi] *= np.linspace(0, 1, fi)
    out[-fo:] *= np.linspace(1, 0, fo)
    return (out / (np.abs(out).max() + 1e-6) * 0.8).astype(np.float32)


def _box_smooth(x, k):
    """Rata-rata bergerak (pengganti np.convolve): O(N), bukan O(N*k)."""
    k = max(1, int(k))
    if k <= 1 or len(x) < k:
        return x
    xp = np.pad(x, (k // 2, k - 1 - k // 2), mode="edge")
    c = np.cumsum(np.insert(xp, 0, 0.0))
    return ((c[k:] - c[:-k]) / k).astype(np.float32)


def mix_audio(total, sfx_events, voices, music, sfx_vol, mus_vol, out_wav):
    N = int(total * SR) + SR // 2
    master = np.zeros((N, 2), np.float32)
    # backsound (+ ducking saat voice-over)
    if music is not None and len(music):
        m = music if music.ndim == 2 else np.stack([music, np.roll(music, 40)], 1)
        reps = int(math.ceil(N / len(m)))
        m = np.tile(m, (reps, 1))[:N].copy()
        duck = np.ones(N, np.float32)
        for t0, v in voices:
            a, b = int(t0 * SR), min(N, int(t0 * SR) + len(v))
            duck[a:b] = 0.38
        k = int(0.25 * SR)
        duck = _box_smooth(duck, k)
        fade = np.ones(N, np.float32)
        end = int(total * SR)
        fo = min(int(2.2 * SR), end)
        fade[end - fo:end] = np.linspace(1, 0, fo)
        fade[end:] = 0
        master += m * (duck * fade * mus_vol)[:, None]
    for t0, v in voices:
        a = int(t0 * SR)
        e = min(N, a + len(v))
        master[a:e] += v[:e - a] * 1.0
    gains = {"pop": 0.55, "whoosh": 0.65, "hit": 0.95, "paper": 0.5, "chime": 0.6}
    for t0, name, g in sfx_events:
        s = get_sfx(name)
        a = int(t0 * SR)
        if a >= N:
            continue
        e = min(N, a + len(s))
        master[a:e] += s[:e - a] * gains.get(name, 0.6) * g * sfx_vol
    pk = float(np.abs(master).max())
    if pk > 0.95:
        master *= 0.95 / pk
    pcm = (np.clip(master, -1, 1) * 32767).astype("<i2")
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


def trim_silence(a, thr=0.01):
    idx = np.where(np.abs(a).max(axis=1) > thr)[0]
    if len(idx) == 0:
        return a
    return a[max(0, idx[0] - 200): idx[-1] + int(0.08 * SR)]


# ----------------------------------------------------------------------------
# LLM (aiapifreekey.com) & sumber gambar
# ----------------------------------------------------------------------------
class Cancelled(Exception):
    pass


def llm_chat(messages, log, max_tokens=3500, retries=6, cancel=None):
    import requests
    last = None
    for i in range(retries):
        if cancel and cancel.is_set():
            raise Cancelled()
        try:
            r = requests.post(
                API_BASE + "/chat/completions",
                headers={"Content-Type": "application/json", "Authorization": "Bearer free"},
                json={"model": MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": 0.8},
                timeout=180,
            )
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or (2 + i * 2))
                log(f"API sibuk ({r.status_code}), coba lagi {wait:.0f}s ...")
                time.sleep(min(wait, 30))
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Cancelled:
            raise
        except Exception as e:  # noqa
            last = e
            log(f"API error: {e}")
            time.sleep(2 + i)
    raise RuntimeError(f"Gagal menghubungi API: {last}")


def _clean_json(s):
    """Perbaikan ringan: kutip pintar, koma nyangkut, karakter kontrol."""
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    s = re.sub(r",\s*([}\]])", r"\1", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", s)
    return s


def _escape_inner_quotes(s):
    """Escape tanda kutip yang salah tempat di dalam nilai, mis: "narration":"Kata "kopi" enak"."""
    out, i, n, in_str = [], 0, len(s), False
    while i < n:
        c = s[i]
        if not in_str:
            if c == '"':
                in_str = True
            out.append(c)
            i += 1
            continue
        if c == "\\":
            out.append(s[i:i + 2])
            i += 2
            continue
        if c == '"':
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j >= n or s[j] in ":,}]":
                out.append('"')
                in_str = False
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_json_loose(txt):
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("JSON tidak ditemukan")
    s = txt[a:b + 1].strip().lstrip("\ufeff")
    last = None
    for cand in (s, _clean_json(s), _escape_inner_quotes(s), _clean_json(_escape_inner_quotes(s))):
        try:
            return json.loads(cand)
        except Exception as e:  # noqa
            last = e
    raise ValueError(f"JSON tidak valid ({last})")


# ----------------------------------------------------------------------------
# Foto dari Bing Image Search (tanpa API key)
# ----------------------------------------------------------------------------
BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BING_HEADERS = {"User-Agent": BING_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,id-ID;q=0.8"}
BING_SKIP_EXT = (".svg", ".gif", ".tif", ".tiff", ".ico", ".bmp")


def _bing_search_images(query, count=35):
    """Scrape hasil Bing Image Search. Kembalikan list dict {murl, turl, title, page}."""
    import requests
    endpoints = [
        # endpoint async: isinya hanya kartu hasil, paling stabil tanpa cookie
        ("https://www.bing.com/images/async",
         {"q": query, "first": 0, "count": count, "scenario": "ImageBasicHover",
          "datsrc": "I", "layout": "RowBased", "mmasync": 1}),
        # cadangan: halaman pencarian biasa
        ("https://www.bing.com/images/search", {"q": query, "form": "HDRSC2", "first": 1}),
    ]
    for attempt in range(2):  # retry sekali kalau kena throttle / captcha sesaat
        for url, params in endpoints:
            try:
                r = requests.get(url, params=params, headers=BING_HEADERS, timeout=20)
                if r.status_code != 200 or 'class="iusc"' not in r.text:
                    continue
                items = []
                for mt in re.finditer(r'class="iusc"', r.text):
                    a = r.text.rfind("<a ", 0, mt.start())
                    b = r.text.find(">", mt.end())
                    if a < 0 or b < 0:
                        continue
                    m = re.search(r'\sm="([^"]*)"', r.text[a:b])
                    if not m:
                        continue
                    try:
                        d = json.loads(html.unescape(m.group(1)))
                    except Exception:
                        continue
                    mu, tu = (d.get("murl") or "").strip(), (d.get("turl") or "").strip()
                    if not mu and not tu:
                        continue
                    items.append({"murl": mu, "turl": tu, "title": str(d.get("t") or "").strip(),
                                  "page": str(d.get("purl") or "").strip()})
                if items:
                    return items
            except Exception:  # noqa
                continue
        if attempt == 0:
            time.sleep(1.5)
    return []


def _bing_load(url, timeout=25, max_mb=15):
    """Unduh URL gambar -> PIL.Image RGB (tolak placeholder 80x80 milik Bing)."""
    import requests
    from io import BytesIO
    r = requests.get(url, headers=BING_HEADERS, timeout=timeout, stream=True)
    r.raise_for_status()
    if int(r.headers.get("Content-Length") or 0) > max_mb * 1024 * 1024:
        raise ValueError("file terlalu besar")
    im = Image.open(BytesIO(r.content))
    im.load()
    if min(im.size) < 160:  # placeholder/error image dari Bing
        raise ValueError("resolusi terlalu kecil")
    return im.convert("RGB")


def _bing_clean_title(raw, fallback):
    """Judul hasil Bing sering 'Nama Foto | Nama Situs' -> ambil bagian paling deskriptif."""
    t = re.sub(r"\s+", " ", str(raw or "")).strip()
    if t:
        parts = [p.strip(" .|") for p in re.split(r"\s*[|\u2013\u2014]\s*|\s+-\s+", t) if p.strip(" .|")]
        if parts:
            t = max(parts, key=len)
    if not t:
        t = fallback
    if len(t) > 55:
        t = t[:55].rsplit(" ", 1)[0]
    return t


def fetch_bing_image(query, log, used=None):
    """Ambil 1 foto dari Bing Image Search (tanpa API key).

    Return (PIL.Image RGB, judul, url halaman sumber) atau (None, None, None).
    `used` = set murl yang sudah dipakai scene lain, biar tidak dobel.
    """
    if not query:
        return None, None, None
    used = used if used is not None else set()
    q = re.sub(r"\s+", " ", str(query)).strip()
    if not q:
        return None, None, None
    try:
        items = _bing_search_images(q)
    except Exception as e:  # noqa
        log(f"Bing error untuk '{q}': {e}")
        items = []
    if not items:
        log(f"Bing: tidak ada hasil untuk '{q}'.")
        return None, None, None
    fallback = None
    tries = 0
    for it in items:
        mu, tu = it["murl"], it["turl"]
        if not mu or mu in used:
            continue
        if mu.split("?")[0].lower().endswith(BING_SKIP_EXT):
            continue
        if tries >= 8:  # jangan download terlalu banyak kalau hasilnya jelek
            break
        # 1) gambar asli (resolusi penuh) -> 2) thumbnail Bing (crop persegi 1200px)
        for url in (mu, (tu + "&w=1200&h=1200&c=7") if tu else None):
            if not url:
                continue
            try:
                im = _bing_load(url)
            except Exception:  # noqa
                continue
            tries += 1
            smallest = min(im.size)
            if smallest < 240:
                continue
            title = _bing_clean_title(it["title"], q)
            rec = (im, title, it["page"], mu)
            if smallest >= 480:  # cukup tajam buat kartu foto 800x640
                used.add(mu)
                return rec[:3]
            if fallback is None:
                fallback = rec
    if fallback:
        used.add(fallback[3])
        return fallback[:3]
    log(f"Bing: semua kandidat untuk '{q}' gagal diunduh.")
    return None, None, None


def scene_min_duration(sc):
    """Durasi terpendek yang aman: semua animasi selesai + voice-over selesai."""
    m = 3.0
    for el in sc.els:
        m = max(m, el.t_in + el.dur + 0.5)
    if sc.voice:
        m = max(m, sc.voice[0] + len(sc.voice[1]) / SR + 0.6)
    return m


def fit_scenes_duration(scenes, starts, target, log=None):
    """Skala durasi tiap scene supaya total video mendekati `target` detik.

    - target lebih panjang dari durasi alami -> tiap scene ditambah hold-time proporsional.
    - target lebih pendek -> tiap scene diperpendek sampai batas minimum (voice + animasi).
    Return `starts` yang sudah dihitung ulang.
    """
    if not target or target <= 0 or not scenes:
        return starts
    n = len(scenes)
    nat = [sc.D for sc in scenes]
    mins = [scene_min_duration(sc) for sc in scenes]
    total_nat = sum(nat) - TD * (n - 1)
    total_min = sum(mins) - TD * (n - 1)
    if target >= total_nat:
        k = (target + TD * (n - 1)) / max(sum(nat), 1e-6)
        new = [D * k for D in nat]
    elif target <= total_min:
        new = list(mins)
        if log:
            log(f"Durasi target {target:.0f}s lebih pendek dari minimum {total_min:.1f}s "
                f"(panjang voice-over/animasi). Pakai {total_min:.1f}s.")
    else:
        frac = (target - total_min) / max(1e-6, total_nat - total_min)
        new = [mn + (D - mn) * frac for D, mn in zip(nat, mins)]
    for sc, D in zip(scenes, new):
        sc.D = D
    out = [0.0]
    for i in range(1, n):
        out.append(out[-1] + scenes[i - 1].D - TD)
    return out


def tts_save(text, voice, rate, path):
    import edge_tts

    async def go():
        await edge_tts.Communicate(text, voice, rate=rate).save(path)
    asyncio.run(go())


def tts_save_batch(items, voice, rate):
    """TTS banyak teks sekaligus (paralel) -> jauh lebih cepat dari satu per satu."""
    import edge_tts

    async def go():
        async def one(text, path):
            await edge_tts.Communicate(text, voice, rate=rate).save(path)

        await asyncio.gather(*[one(t, p) for t, p in items if t])
    asyncio.run(go())


# ----------------------------------------------------------------------------
# Settings & Pipeline
# ----------------------------------------------------------------------------
RES = {"4K": 2160, "2K": 1440, "FHD": 1080, "HD": 720}


@dataclass
class Settings:
    topic: str = ""
    outdir: str = os.path.join(os.path.expanduser("~"), "Videos", "PaperCollage")
    res: str = "4K"
    fps: int = 30
    scenes: int = 6
    lang: str = "id"
    mood: str = "auto"
    voice_on: bool = True
    voice: str = "id-ID-ArdiNeural"
    voice_rate: str = "+8%"
    music_path: str = ""
    music_vol: float = 0.5
    sfx_vol: float = 0.9
    duration: float = 0.0  # detik; 0 = otomatis (ikuti panjang voice-over)
    style: str = "paper"  # paper | flat
    use_bing: bool = True
    img_folder: str = ""
    gpu: bool = False
    camera: bool = True
    script: dict | None = None  # untuk pengujian (lewati LLM)


class Pipeline:
    def __init__(self, st: Settings, cb):
        self.st, self.cb = st, cb
        self.cancel = threading.Event()

    # -- util
    def log(self, m):
        self.cb.log(m)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _images_async(self, script):
        """Ambil foto Bing di thread terpisah -> paralel dengan voice-over."""
        holder, logs = {}, []

        def job():
            try:
                ph, cr, used = {}, [], set()
                for i, sc in enumerate(script["scenes"]):
                    img, title, src = fetch_bing_image(sc["image_query"], logs.append, used)
                    if img is not None:
                        ph[i] = (img, title)
                        cr.append(f"Scene {i + 1}: Bing Images - {title}" + (f" | {src}" if src else ""))
                    time.sleep(0.2)
                holder["r"] = (ph, cr)
            except Exception as e:  # noqa
                holder["e"] = e

        th = threading.Thread(target=job, daemon=True)
        th.start()

        def join():
            th.join()
            for m in logs:
                self.log(m)
            if "e" in holder:
                self.log(f"Gambar Bing gagal ({holder['e']}).")
            return holder.get("r", ({}, []))

        return join

    # -- naskah
    def make_script(self):
        st = self.st
        lang = "Bahasa Indonesia" if st.lang == "id" else "English"
        schema = ('{"title":"","mood":"upbeat|chill|dramatic|mysterious|inspiring",'
                  '"hook":{"text":"","narration":"","emoji":""},'
                  '"scenes":[{"headline":"","narration":"","popups":["",""],"emoji":"","image_query":""}],'
                  '"closing":{"text":"","narration":"","emoji":""},"hashtags":["",""]}')
        sysm = ("You are an expert viral short-video scriptwriter for TikTok/Reels/Shorts in paper-collage style. "
                "Reply with ONLY one valid minified JSON object. No markdown, no commentary.")
        n_sc = max(1, int(st.scenes))
        if st.duration and st.duration > 0:
            # perkiraan: 0.36 detik per kata (edge-tts +8%) + overhead hook/closing/transisi
            cap = max(6, min(24, round((st.duration - (15.0 + TD * (n_sc + 1))) / n_sc / 0.36)))
            dur_hint = (f"Target durasi video: {st.duration:.0f} detik. narration tiap scene maks {cap} kata "
                        f"supaya totalnya pas. ")
        else:
            cap = 24
            dur_hint = ""
        usr = (f"Topik: {st.topic}\nBahasa naskah: {lang}\nJumlah scene isi: {st.scenes}\n"
               f"Skema JSON persis: {schema}\n" + dur_hint +
               "Aturan: hook.text maks 7 kata, bikin penasaran. headline maks 5 kata. narration 1-2 kalimat pendek "
               f"(maks {cap} kata), enak dibaca lantang. popups 2-3 frasa pendek (maks 4 kata) berisi fakta/kata kunci. "
               "emoji = tepat 1 emoji yang relevan. image_query = kata kunci pencarian gambar Bing dalam bahasa "
               "Inggris, 2-5 kata, berupa objek konkret yang mudah difoto (contoh: \"coffee beans plantation\", "
               "\"batik textile pattern\"). Jangan pakai nama orang. Jangan mengarang angka/tanggal yang tidak pasti. "
               "closing.text = ajakan follow/share maks 7 kata. hashtags 4-6 item tanpa tanda #.")
        msgs = [{"role": "system", "content": sysm}, {"role": "user", "content": usr}]
        for attempt in range(3):
            raw = llm_chat(msgs, self.log, cancel=self.cancel)
            try:
                data = parse_json_loose(raw)
                if data.get("scenes"):
                    return data
            except Exception as e:  # noqa
                self.log(f"Format JSON belum valid ({e}), minta ulang ...")
            msgs.append({"role": "assistant", "content": raw})
            msgs.append({"role": "user", "content": "Output tidak valid. Kirim ulang HANYA JSON valid sesuai skema."})
            time.sleep(1.1)
        raise RuntimeError("Naskah dari AI tidak valid setelah 3 percobaan.")

    @staticmethod
    def normalize(data, topic, n):
        def s(x, d=""):
            return str(x).strip() if x else d
        hook = data.get("hook") or {}
        if isinstance(hook, str):
            hook = {"text": hook}
        clos = data.get("closing") or {}
        if isinstance(clos, str):
            clos = {"text": clos}
        out = {"title": s(data.get("title"), topic), "mood": s(data.get("mood"), "upbeat").lower(),
               "hook": {"text": s(hook.get("text"), topic), "narration": s(hook.get("narration")),
                        "emoji": s(hook.get("emoji"), "🔥")},
               "closing": {"text": s(clos.get("text"), "Follow untuk lainnya!"), "narration": s(clos.get("narration")),
                           "emoji": s(clos.get("emoji"), "❤")},
               "hashtags": [s(h).lstrip("#") for h in (data.get("hashtags") or []) if s(h)][:6], "scenes": []}
        emo = ["✨", "💡", "🌍", "🚀", "📌", "🎯", "🧠", "⭐"]
        for i, sc in enumerate((data.get("scenes") or [])[:max(n, 1) + 2]):
            pops = [s(p) for p in (sc.get("popups") or []) if s(p)][:3]
            out["scenes"].append({"headline": s(sc.get("headline"), f"Fakta {i + 1}"), "narration": s(sc.get("narration")),
                                  "popups": pops, "emoji": s(sc.get("emoji"), emo[i % len(emo)]),
                                  "image_query": s(sc.get("image_query"))})
        if out["mood"] not in MOODS:
            out["mood"] = "upbeat"
        return out

    # -- jalankan
    def run(self):
        st = self.st
        t_start = time.time()
        os.makedirs(st.outdir, exist_ok=True)
        W = RES.get(st.res, 2160)
        H = W * 16 // 9
        S = W / 1080.0
        work = tempfile.mkdtemp(prefix="pcs_")
        try:
            # 1. naskah
            self.cb.progress(0.01, "Menulis naskah dengan AI ...")
            raw = st.script if st.script else self.make_script()
            script = self.normalize(raw, st.topic, st.scenes)
            self.cb.script(script)
            self.log(f"Naskah siap: {script['title']} ({len(script['scenes'])} scene, mood {script['mood']})")
            self.check()

            # 3. gambar (Bing dijalankan di thread terpisah, paralel dengan voice-over)
            photos, credits = {}, []
            img_join = None
            if st.img_folder and os.path.isdir(st.img_folder):
                files = [os.path.join(st.img_folder, f) for f in sorted(os.listdir(st.img_folder))
                         if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
                random.Random(1).shuffle(files)
                for i in range(len(script["scenes"])):
                    if i < len(files):
                        try:
                            photos[i] = (Image.open(files[i]).convert("RGB"), os.path.splitext(os.path.basename(files[i]))[0])
                        except Exception:
                            pass
            elif st.use_bing:
                img_join = self._images_async(script)

            # 2. voice-over (semua potongan di-TTS paralel)
            segs = [("hook", script["hook"]["narration"] or script["hook"]["text"])]
            segs += [(f"s{i}", sc["narration"]) for i, sc in enumerate(script["scenes"])]
            segs += [("close", script["closing"]["narration"] or script["closing"]["text"])]
            voices = [None] * len(segs)
            if st.voice_on:
                self.check()
                jobs = [(i, txt, os.path.join(work, f"v{i}.mp3")) for i, (k, txt) in enumerate(segs) if txt]
                self.cb.progress(0.05, f"Voice-over {len(jobs)} potongan ...")
                try:
                    tts_save_batch([(t, p) for _, t, p in jobs], st.voice, st.voice_rate)
                except Exception as e:  # noqa
                    self.log(f"Voice-over gagal ({e}). Lanjut tanpa voice-over.")
                    jobs = []
                for i, txt, p in jobs:
                    try:
                        voices[i] = trim_silence(decode_audio(p))
                    except Exception:  # noqa
                        pass

            if img_join is not None:
                self.cb.progress(0.17, "Menunggu gambar Bing ...")
                photos, credits = img_join()

            # 4. susun scene
            self.cb.progress(0.18, "Menyusun kolase kertas ...")
            if st.style == "flat":
                scenes, starts = self.build_scenes_flat(script, voices, photos, W, H, S)
            else:
                scenes, starts = self.build_scenes(script, voices, photos, W, H, S)
            total = starts[-1] + scenes[-1].D
            if st.duration:
                self.log(f"Durasi video: {total:.1f} detik (target {st.duration:.0f} detik)")
            else:
                self.log(f"Durasi video: {total:.1f} detik")

            # 5. audio
            self.cb.progress(0.22, "Meracik SFX & backsound ...")
            events = []
            for i, sc in enumerate(scenes):
                for (t, name, g) in sc.sfx:
                    events.append((starts[i] + t, name, g))
                if i > 0:
                    events.append((starts[i], "whoosh", 1.0))
            events.append((0.0, "hit", 1.0))
            vlist = [(starts[i] + sc.voice[0], sc.voice[1]) for i, sc in enumerate(scenes) if sc.voice]
            mood = script["mood"] if st.mood == "auto" else st.mood
            if st.music_path and os.path.exists(st.music_path):
                music = decode_audio(st.music_path)
            else:
                lib = [os.path.join(ASSETS, "music", f) for f in os.listdir(os.path.join(ASSETS, "music"))
                       if f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a"))] if os.path.isdir(os.path.join(ASSETS, "music")) else []
                music = decode_audio(random.choice(lib)) if lib else synth_music(total, mood)
            wav = os.path.join(work, "mix.wav")
            mix_audio(total, events, vlist, music, st.sfx_vol, st.music_vol, wav)
            self.check()

            # 6. render
            slug = re.sub(r"[^\w\-]+", "_", st.topic.strip())[:40].strip("_") or "video"
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_mp4 = os.path.join(st.outdir, f"{slug}_{stamp}.mp4")
            self.render(scenes, starts, total, W, H, S, wav, out_mp4, work, t_start)

            # 7. file teks pendamping
            with open(out_mp4[:-4] + ".txt", "w", encoding="utf-8") as f:
                f.write(script["title"] + "\n\n")
                f.write("HOOK: " + script["hook"]["text"] + "\n\n")
                for i, sc in enumerate(script["scenes"], 1):
                    f.write(f"[{i}] {sc['headline']}\n    {sc['narration']}\n")
                f.write("\nCLOSING: " + script["closing"]["text"] + "\n\n")
                f.write(" ".join("#" + h.replace(" ", "") for h in script["hashtags"]) + "\n\n")
                if USED_OPENMOJI:
                    credits.append("Emoji: OpenMoji (https://openmoji.org) CC BY-SA 4.0")
                if credits:
                    f.write("Kredit gambar (cek lisensi sebelum monetisasi):\n" + "\n".join(credits) + "\n")
            return out_mp4
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # -- scene builder
    def build_scenes(self, script, voices, photos, W, H, S):
        st = self.st
        rnd = random.Random(42)
        nrng = np.random.default_rng(42)
        lang_id = st.lang == "id"
        scenes, starts = [], []
        n = len(script["scenes"])
        cur = 0.0

        def captions(sc, text, vs, vl):
            words = text.split()
            if not words or vl <= 0:
                return
            chunks, c = [], []
            for w in words:
                c.append(w)
                if len(c) >= 4 or w.endswith((".", "!", "?", ",")) and len(c) >= 2:
                    chunks.append(" ".join(c))
                    c = []
            if c:
                chunks.append(" ".join(c))
            tot = sum(len(x) for x in chunks)
            t = vs
            for ch in chunks:
                dt = vl * len(ch) / tot
                card = text_card(ch, S, rnd, nrng, color=(255, 255, 255), role="body", size=54, max_w=880,
                                 caps=True, pad=26, min_size=36, max_lines=2, torn=False)
                sc.add(card, W / 2, U(S, 1700), t, t_out=t + dt, anim="pop", dur=0.16, z=9, idle=False)
                t += dt

        def vo_len(i):
            return len(voices[i]) / SR if voices[i] is not None else None

        # ---- HOOK
        vl = vo_len(0)
        D = max(3.8, (0.45 + vl + 0.9) if vl else 3.8)
        sc = Scene("hook", make_background(W, H, S, HOOK_THEME, 1), D)
        sc.add(burst_sticker(S, 1000, PAPERS[1], rnd, nrng, 16), W / 2, U(S, 850), 0.0, anim="pop", dur=0.35, z=1)
        sc.add(letter_sticker(script["hook"]["text"], S, nrng, size=170, max_w=860, color=(236, 70, 52), max_lines=4),
               W / 2, U(S, 850), 0.1, rot=-4, anim="stamp", dur=0.34, z=3, sfx="hit")
        sc.add(emoji_sticker(script["hook"]["emoji"], 380, S, rnd, nrng), W / 2, U(S, 320), 0.55, rot=8, z=2, sfx="pop")
        sc.add(text_card(script["title"], S, rnd, nrng, color=PAPERS[7], role="hand", size=62, max_w=900, caps=False,
                         max_lines=2), W / 2, U(S, 1330), 1.25, rot=2.5, z=2, sfx="pop")
        if vl:
            sc.voice = (0.4, voices[0])
            captions(sc, segs_text(script, 0), 0.4, vl)
        sc.finalize()
        scenes.append(sc)
        starts.append(0.0)
        cur = D - TD

        # ---- SCENE ISI
        for i, data in enumerate(script["scenes"]):
            vl = vo_len(i + 1)
            D = max(TD + 4.6, (TD + 0.3 + vl + 1.0) if vl else 0)
            sc = Scene("body", make_background(W, H, S, THEMES[i % len(THEMES)], 100 + i), D)
            blk = THEMES[i % len(THEMES)][1]
            vs = TD + 0.3
            sc.add(letter_sticker(data["headline"], S, nrng, size=118, max_w=930,
                                  color=LETTER_COLORS[i % len(LETTER_COLORS)], max_lines=2),
                   W / 2, U(S, 285), TD * 0.7, rot=rnd.choice((-3, 2.5)), anim="pop", z=4, sfx="paper")
            ph = photos.get(i)
            if ph:
                sc.add(photo_card(ph[0], S, rnd, nrng, 800, 640, ph[1] or ""), W / 2, U(S, 905), TD + 0.2,
                       rot=rnd.choice((-3, 3)), anim="pop", z=3, sfx="pop")
                sc.add(emoji_sticker(data["emoji"], 230, S, rnd, nrng), U(S, 880), U(S, 600), TD + 0.7, rot=12, z=5,
                       sfx="pop")
            else:
                sc.add(burst_sticker(S, 640, PAPERS[1 + (i + 1) % 6], rnd, nrng, 12),
                       W / 2, U(S, 905), TD + 0.15, anim="pop", z=2)
                sc.add(emoji_sticker(data["emoji"], 430, S, rnd, nrng), W / 2, U(S, 905), TD + 0.3, rot=-6, z=3,
                       sfx="pop")
            pops = data["popups"] or []
            span = (vl or 3.0)
            colors = [c for c in PAPERS if c != blk and c != (255, 255, 255)]
            cards = []
            for j, ptxt in enumerate(pops):
                cards.append(text_card(ptxt, S, rnd, nrng, color=colors[(i + j) % len(colors)], role="body",
                                       size=60, max_w=800, max_lines=1, min_size=30))
            y = U(S, 1215)
            for j, card in enumerate(cards):
                vis = card.height - U(S, 70)
                tj = vs + 0.5 + (j + 0.4) * span / (len(pops) + 0.6)
                x = W / 2 + U(S, (-80 if j % 2 == 0 else 80))
                sc.add(card, x, y + vis / 2, tj, rot=(-3 if j % 2 == 0 else 3),
                       anim=("slide_l" if j % 2 == 0 else "slide_r"), dur=0.45, z=6, sfx="pop")
                y += vis + U(S, 18)
            if vl:
                sc.voice = (vs, voices[i + 1])
                captions(sc, data["narration"], vs, vl)
            sc.finalize()
            scenes.append(sc)
            starts.append(cur)
            cur += D - TD

        # ---- CLOSING
        vl = vo_len(len(voices) - 1)
        D = max(TD + 4.8, (TD + 0.3 + vl + 1.6) if vl else 0)
        sc = Scene("close", make_background(W, H, S, CLOSE_THEME, 999), D)
        sc.add(letter_sticker(script["closing"]["text"], S, nrng, size=140, max_w=900, color=(251, 224, 138),
                              max_lines=3), W / 2, U(S, 520), TD + 0.1, rot=-3, anim="stamp", dur=0.36, z=4)
        sc.sfx.append((TD + 0.1, "chime", 1.0))
        sc.add(burst_sticker(S, 560, PAPERS[3], rnd, nrng, 12), W / 2, U(S, 1000), TD + 0.5, z=1)
        sc.add(emoji_sticker(script["closing"]["emoji"], 380, S, rnd, nrng), W / 2, U(S, 1000), TD + 0.65, rot=6,
               z=2, sfx="pop")
        sc.add(text_card("FOLLOW & SHARE!" if lang_id else "FOLLOW & SHARE!", S, rnd, nrng, color=PAPERS[1],
                         role="body", size=76, max_w=860), W / 2, U(S, 1290), TD + 1.1, rot=2, z=5, sfx="pop")
        for j, h in enumerate(script["hashtags"][:4]):
            sc.add(text_card("#" + h.replace(" ", ""), S, rnd, nrng, color=PAPERS[(j + 2) % 6], role="hand", size=50,
                             max_w=500, caps=False, max_lines=1),
                   W / 2 + U(S, -230 if j % 2 == 0 else 230), U(S, 1440 + (j // 2) * 120), TD + 1.5 + j * 0.25,
                   rot=rnd.choice((-3, 3)), z=5, sfx="pop")
        if vl:
            sc.voice = (TD + 0.3, voices[-1])
            captions(sc, segs_text(script, -1), TD + 0.3, vl)
        sc.finalize()
        scenes.append(sc)
        starts.append(cur)
        starts = fit_scenes_duration(scenes, starts, st.duration, self.log)
        return scenes, starts

    # -- scene builder (gaya motion graphics flat)
    def build_scenes_flat(self, script, voices, photos, W, H, S):
        lang_id = self.st.lang == "id"
        scenes, starts = [], []
        cur = 0.0
        hook_th = FLAT_THEMES[0]

        def vo_len(i):
            return len(voices[i]) / SR if voices[i] is not None else None

        def captions(sc, text, vs, vl, th):
            words = text.split()
            if not words or vl <= 0:
                return
            chunks, c = [], []
            for w in words:
                c.append(w)
                if len(c) >= 4 or w.endswith((".", "!", "?", ",")) and len(c) >= 2:
                    chunks.append(" ".join(c))
                    c = []
            if c:
                chunks.append(" ".join(c))
            tot = sum(len(x) for x in chunks) or 1
            t = vs
            for ch in chunks:
                dt = vl * len(ch) / tot
                sc.add(flat_caption(ch, S, th), W / 2, U(S, 1770), t, t_out=t + dt,
                       anim="fade_up", dur=0.18, z=9, idle=False)
                t += dt

        # ---- HOOK
        vl = vo_len(0)
        D = max(3.8, (0.45 + vl + 0.9) if vl else 3.8)
        sc = Scene("hook", flat_bg(W, H, S, hook_th, 11), D)
        sc.style = "flat"
        sc.add(flat_pill("★ " + ("FAKTA MENARIK" if lang_id else "DID YOU KNOW"), S,
                         hook_th["accent"], hook_th["bg"], size=42),
               W / 2, U(S, 240), 0.05, anim="fade_up", dur=0.35, z=3, idle=False)
        sc.add(flat_text(script["hook"]["text"], S, "display", 150, hook_th["onbg"], 940, 4),
               W / 2, U(S, 760), 0.12, anim="wipe_r", dur=0.55, z=4)
        sc.add(flat_panel(U(S, 420), U(S, 22), hook_th["accent"], S), W / 2, U(S, 1170), 0.62,
               anim="wipe_r", dur=0.4, z=4, idle=False)
        sc.add(flat_emoji(script["hook"]["emoji"], 250, S, hook_th), U(S, 850), U(S, 1370), 0.78,
               anim="pop", z=3, sfx="pop")
        sc.add(flat_text(script["title"], S, "body", 60, hook_th["onbg"], 900, 2),
               W / 2, U(S, 1580), 1.0, anim="fade_up", dur=0.45, z=3, idle=False)
        if vl:
            sc.voice = (0.4, voices[0])
            captions(sc, script["hook"]["narration"] or script["hook"]["text"], 0.4, vl, hook_th)
        sc.finalize()
        scenes.append(sc)
        starts.append(0.0)
        cur = D - TD

        # ---- SCENE ISI
        for i, data in enumerate(script["scenes"]):
            th = FLAT_THEMES[i % len(FLAT_THEMES)]
            vl = vo_len(i + 1)
            D = max(TD + 4.6, (TD + 0.3 + vl + 1.0) if vl else 0)
            bg = flat_bg(W, H, S, th, 100 + i)
            wm = flat_text(f"{i + 1:02d}", S, "display", 520, th["soft"], 900, 1)
            wm.putalpha(wm.getchannel("A").point(lambda v: int(v * 0.07)))
            bg.paste(wm, (int(W - wm.width * 0.72), int(H - wm.height * 0.92)), wm)
            sc = Scene("body", bg, D)
            sc.style = "flat"
            sc.add(flat_badge_num(f"{i + 1:02d}", S, th["accent"], th["bg"], size=58),
                   W / 2, U(S, 170), TD + 0.05, anim="pop", z=3)
            sc.add(flat_text(data["headline"], S, "display", 108, th["onbg"], 900, 2),
                   W / 2, U(S, 380), TD + 0.12, anim="wipe_r", dur=0.5, z=4)
            ph = photos.get(i)
            if ph:
                sc.add(flat_photo(ph[0], S, th, 840, 640, 40), W / 2, U(S, 950), TD + 0.2,
                       anim="pop", z=3, sfx="pop")
            else:
                sc.add(flat_shape(("ring", "square", "triangle", "circle")[i % 4], 620, S, th["accent2"]),
                       W / 2, U(S, 950), TD + 0.2, anim="pop", z=2, sfx="pop")
            sc.add(flat_emoji(data["emoji"], 190, S, th), U(S, 930), U(S, 630), TD + 0.72,
                   rot=10, anim="pop", z=5, sfx="pop")
            pops = data["popups"] or []
            span = vl or 3.0
            for j, ptxt in enumerate(pops):
                pill = flat_pill(ptxt, S, th["accent"], th["bg"], size=52)
                tj = TD + 0.5 + (j + 0.4) * span / (len(pops) + 0.6)
                x = W / 2 + U(S, -120 if j % 2 == 0 else 120)
                sc.add(pill, x, U(S, 1360 + j * 130), tj,
                       anim=("slide_l" if j % 2 == 0 else "slide_r"), dur=0.45, z=6, sfx="pop")
            if vl:
                sc.voice = (TD + 0.3, voices[i + 1])
                captions(sc, data["narration"], TD + 0.3, vl, th)
            sc.finalize()
            scenes.append(sc)
            starts.append(cur)
            cur += D - TD

        # ---- CLOSING
        vl = vo_len(len(voices) - 1)
        D = max(TD + 4.8, (TD + 0.3 + vl + 1.6) if vl else 0)
        th = FLAT_THEMES[-1]
        sc = Scene("close", flat_bg(W, H, S, th, 999), D)
        sc.style = "flat"
        sc.add(flat_pill("★ " + ("IKUTI TERUS" if lang_id else "FOLLOW"), S, th["accent"], th["bg"], size=42),
               W / 2, U(S, 260), TD + 0.05, anim="fade_up", dur=0.35, z=3, idle=False)
        sc.add(flat_text(script["closing"]["text"], S, "display", 140, th["onbg"], 920, 3),
               W / 2, U(S, 680), TD + 0.1, anim="wipe_r", dur=0.55, z=4)
        sc.add(flat_emoji(script["closing"]["emoji"], 260, S, th), W / 2, U(S, 1080), TD + 0.55,
               anim="pop", z=3, sfx="pop")
        for j, h in enumerate(script["hashtags"][:4]):
            sc.add(flat_pill("#" + h.replace(" ", ""), S, th["panel"], th["ink"], size=44),
                   W / 2 + U(S, -250 if j % 2 == 0 else 250), U(S, 1300 + (j // 2) * 120),
                   TD + 0.9 + j * 0.12, anim="pop", z=4, sfx="pop")
        if vl:
            sc.voice = (TD + 0.3, voices[-1])
            captions(sc, script["closing"]["narration"] or script["closing"]["text"], TD + 0.3, vl, th)
        sc.finalize()
        scenes.append(sc)
        starts.append(cur)
        starts = fit_scenes_duration(scenes, starts, self.st.duration, self.log)
        return scenes, starts

    # -- render + encode
    def render(self, scenes, starts, total, W, H, S, wav, out_mp4, work, t_start):
        st = self.st
        fps = st.fps
        ff = ffmpeg_exe()
        use_gpu = False
        if st.gpu:
            r = subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.2",
                                "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, creationflags=NOWIN)
            use_gpu = r.returncode == 0
            self.log("Encoder: NVIDIA NVENC" if use_gpu else "NVENC tidak tersedia, pakai CPU (x264)")
        vcodec = (["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0", "-profile:v", "high"]
                  if use_gpu else ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-profile:v", "high"])
        cmd = [ff, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
               "-i", "-", "-i", wav, "-vf", "scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p"]
        cmd += vcodec + ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                         "-c:a", "aac", "-b:a", "320k", "-ar", "44100", "-movflags", "+faststart", "-shortest", out_mp4]
        errlog = open(os.path.join(work, "ffmpeg.log"), "w+")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errlog, creationflags=NOWIN)
        q: queue.Queue = queue.Queue(maxsize=3)
        failed = []

        def writer():
            try:
                while True:
                    b = q.get()
                    if b is None:
                        break
                    proc.stdin.write(b)
            except Exception as e:  # noqa
                failed.append(e)
                while q.get() is not None:
                    pass
        th = threading.Thread(target=writer, daemon=True)
        th.start()
        n = len(scenes)
        pool = FLAT_TRANSITIONS if self.st.style == "flat" else TRANSITIONS
        kinds = [pool[i % len(pool)] for i in range(n)]
        frames = int(math.ceil(total * fps))
        t0 = time.time()
        try:
            for fi in range(frames):
                self.check()
                if failed:
                    break
                t = fi / fps
                k = max(i for i in range(n) if starts[i] <= t + 1e-9)
                if k > 0 and t < starts[k] + TD:
                    A = render_scene(scenes[k - 1], t - starts[k - 1], W, H, S, st.camera)
                    B = render_scene(scenes[k], t - starts[k], W, H, S, st.camera)
                    img = transition(A, B, (t - starts[k]) / TD, kinds[k], S, k * 31 + 5)
                else:
                    img = render_scene(scenes[k], t - starts[k], W, H, S, st.camera)
                q.put(img.tobytes())
                if fi % 3 == 0:
                    el = time.time() - t0
                    eta = el / (fi + 1) * (frames - fi - 1)
                    self.cb.progress(0.25 + 0.72 * (fi + 1) / frames,
                                     f"Render frame {fi + 1}/{frames}  ·  sisa ≈ {int(eta // 60)}m {int(eta % 60)}s")
                if fi % max(1, fps) == 0:
                    self.cb.preview(img.resize((270, 480), BIL))
        except Cancelled:
            q.put(None)
            th.join()
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()
            try:
                os.remove(out_mp4)
            except Exception:
                pass
            raise
        finally:
            if th.is_alive():
                q.put(None)
            th.join()
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()
        if self.cancel.is_set():
            try:
                os.remove(out_mp4)
            except Exception:
                pass
            raise Cancelled()
        if proc.returncode != 0:
            errlog.seek(0)
            raise RuntimeError("ffmpeg gagal:\n" + errlog.read()[-800:])
        self.cb.progress(1.0, f"Selesai dalam {int((time.time() - t_start) // 60)}m {int((time.time() - t_start) % 60)}s")


def segs_text(script, idx):
    if idx == 0:
        return script["hook"]["narration"] or script["hook"]["text"]
    return script["closing"]["narration"] or script["closing"]["text"]


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def cfg_path():
    return os.path.join(user_dir(), "config.json")


def load_cfg() -> dict:
    try:
        with open(cfg_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(d: dict):
    try:
        with open(cfg_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def placeholder_preview():
    W, H = 270, 480
    S = W / 1080
    bg = make_background(W, H, S, THEMES[0], 3)
    rnd, nrng = random.Random(3), np.random.default_rng(3)
    t = letter_sticker("PAPER COLLAGE", S, nrng, size=130, max_w=900, color=(255, 255, 255), max_lines=2)
    bg.paste(t, (W // 2 - t.width // 2, int(H * 0.17)), t)
    e = emoji_sticker("✂", 380, S, rnd, nrng)
    bg.paste(e, (W // 2 - e.width // 2, int(H * 0.42)), e)
    c = text_card("ketik topik, lalu generate", S, rnd, nrng, color=PAPERS[1], role="hand", size=58, max_w=860,
                  caps=False, max_lines=2)
    bg.paste(c, (W // 2 - c.width // 2, int(H * 0.74)), c)
    return bg


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
VOICES = {
    "id": ["id-ID-ArdiNeural", "id-ID-GadisNeural"],
    "en": ["en-US-GuyNeural", "en-US-AriaNeural", "en-US-JennyNeural", "en-GB-RyanNeural"],
}


def run_gui(prefill="", autostart=False, preset=None):
    import customtkinter as ctk
    from tkinter import filedialog, messagebox

    ACC, ACC2, BG, PANEL, PANEL2, PAPER = "#ffb703", "#fb5607", "#14110f", "#1f1a17", "#2a2320", "#f4ecd8"
    ctk.set_appearance_mode("dark")

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title(f"{APP}  ·  AI Paper-Collage Video Generator")
            self.geometry("1240x820")
            self.minsize(1100, 720)
            self.configure(fg_color=BG)
            self.cfg = load_cfg()
            self.q: queue.Queue = queue.Queue()
            self.pipe = None
            self.last_out = None
            self._img = None
            self._build()
            self.after(60, self._poll)
            if prefill:
                self.topic.delete(0, "end")
                self.topic.insert(0, prefill)
            if autostart and prefill:
                self.after(500, self.start)

        # ---------- UI
        def _build(self):
            g = self.cfg
            self.grid_columnconfigure(1, weight=1)
            self.grid_rowconfigure(0, weight=1)
            left = ctk.CTkScrollableFrame(self, width=430, fg_color=PANEL, corner_radius=18)
            left.grid(row=0, column=0, sticky="nsw", padx=(16, 8), pady=16)
            right = ctk.CTkFrame(self, fg_color="transparent")
            right.grid(row=0, column=1, sticky="nsew", padx=(8, 16), pady=16)
            right.grid_columnconfigure(0, weight=1)
            right.grid_rowconfigure(1, weight=1)

            ctk.CTkLabel(left, text="✂  PAPER COLLAGE", font=ctk.CTkFont("Segoe UI Black", 28), text_color=ACC).pack(
                anchor="w", padx=18, pady=(16, 0))
            ctk.CTkLabel(left, text="STUDIO · 9:16 · hingga 4K · SFX + backsound + voice-over",
                         font=ctk.CTkFont("Segoe UI", 12), text_color="#9c8f84").pack(anchor="w", padx=20, pady=(0, 12))

            cmd = ctk.CTkFrame(left, fg_color="#0d0b0a", corner_radius=14, border_width=2, border_color=ACC)
            cmd.pack(fill="x", padx=14, pady=6)
            ctk.CTkLabel(cmd, text="/paper-collage", font=ctk.CTkFont("Consolas", 16, "bold"),
                         text_color=ACC).pack(anchor="w", padx=14, pady=(10, 0))
            self.topic = ctk.CTkEntry(cmd, placeholder_text="ketik TOPIK, mis: Sejarah kopi di Nusantara",
                                      height=44, font=ctk.CTkFont("Segoe UI", 15), fg_color="#17120f",
                                      border_width=0)
            self.topic.pack(fill="x", padx=12, pady=(6, 10))
            self.topic.bind("<Return>", lambda e: self.start())
            self.btn = ctk.CTkButton(cmd, text="▶  GENERATE VIDEO", height=46, fg_color=ACC2, hover_color="#d94700",
                                     font=ctk.CTkFont("Segoe UI Black", 15), corner_radius=12, command=self.start)
            self.btn.pack(fill="x", padx=12, pady=(0, 12))

            def sec(t):
                ctk.CTkLabel(left, text=t, font=ctk.CTkFont("Segoe UI", 12, "bold"), text_color=ACC).pack(
                    anchor="w", padx=20, pady=(14, 2))

            sec("KUALITAS")
            self.res = ctk.CTkSegmentedButton(left, values=["4K", "2K", "FHD", "HD"], selected_color=ACC2,
                                              selected_hover_color="#d94700", unselected_color=PANEL2)
            self.res.set(g.get("res", "4K"))
            self.res.pack(fill="x", padx=18)
            row = ctk.CTkFrame(left, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=(8, 0))
            ctk.CTkLabel(row, text="FPS").pack(side="left")
            self.fps = ctk.CTkSegmentedButton(row, values=["24", "30"], selected_color=ACC2, unselected_color=PANEL2)
            self.fps.set(str(g.get("fps", 30)))
            self.fps.pack(side="left", padx=10)
            self.gpu = ctk.CTkSwitch(row, text="GPU NVENC", progress_color=ACC2)
            if g.get("gpu"):
                self.gpu.select()
            self.gpu.pack(side="right")

            sec("NASKAH")
            r2 = ctk.CTkFrame(left, fg_color="transparent")
            r2.pack(fill="x", padx=18)
            self.lang = ctk.CTkOptionMenu(r2, values=["Indonesia", "English"], width=130, fg_color=PANEL2,
                                          button_color=ACC2, command=self._lang_changed)
            self.lang.set("Indonesia" if g.get("lang", "id") == "id" else "English")
            self.lang.pack(side="left")
            self.mood = ctk.CTkOptionMenu(r2, values=["auto"] + list(MOODS), width=150, fg_color=PANEL2, button_color=ACC2)
            self.mood.set(g.get("mood", "auto"))
            self.mood.pack(side="right")
            r3 = ctk.CTkFrame(left, fg_color="transparent")
            r3.pack(fill="x", padx=18, pady=(8, 0))
            ctk.CTkLabel(r3, text="Scene isi").pack(side="left")
            self.sc_lbl = ctk.CTkLabel(r3, text=str(g.get("scenes", 6)), text_color=ACC, width=24)
            self.sc_lbl.pack(side="right")
            self.scenes = ctk.CTkSlider(left, from_=3, to=10, number_of_steps=7, progress_color=ACC,
                                        button_color=ACC2, command=lambda v: self.sc_lbl.configure(text=str(int(v))))
            self.scenes.set(g.get("scenes", 6))
            self.scenes.pack(fill="x", padx=18)

            r4 = ctk.CTkFrame(left, fg_color="transparent")
            r4.pack(fill="x", padx=18, pady=(10, 0))
            ctk.CTkLabel(r4, text="Durasi video").pack(side="left")
            self.dur_lbl = ctk.CTkLabel(r4, text=self._dur_txt(g.get("duration", 0)), text_color=ACC, width=60)
            self.dur_lbl.pack(side="right")
            self.duration = ctk.CTkSlider(left, from_=0, to=300, number_of_steps=60, progress_color=ACC,
                                          button_color=ACC2, command=lambda v: self.dur_lbl.configure(text=self._dur_txt(v)))
            self.duration.set(g.get("duration", 0))
            self.duration.pack(fill="x", padx=18)

            sec("SUARA & MUSIK")
            self.vo = ctk.CTkSwitch(left, text="Voice-over (edge-tts)", progress_color=ACC2)
            if g.get("voice_on", True):
                self.vo.select()
            self.vo.pack(anchor="w", padx=20)
            self.voice = ctk.CTkOptionMenu(left, values=VOICES["id"], fg_color=PANEL2, button_color=ACC2)
            self.voice.pack(fill="x", padx=18, pady=(6, 0))
            self._lang_changed(self.lang.get(), g.get("voice"))
            self.music_path = g.get("music_path", "")
            mr = ctk.CTkFrame(left, fg_color="transparent")
            mr.pack(fill="x", padx=18, pady=(8, 0))
            self.music_lbl = ctk.CTkLabel(mr, text=self._music_txt(), anchor="w", text_color="#c9bcae")
            self.music_lbl.pack(side="left", fill="x", expand=True)
            ctk.CTkButton(mr, text="Pilih musik", width=90, fg_color=PANEL2, hover_color="#3a2f2a",
                          command=self._pick_music).pack(side="right")
            ctk.CTkButton(mr, text="Auto", width=50, fg_color=PANEL2, hover_color="#3a2f2a",
                          command=self._clear_music).pack(side="right", padx=4)
            for label, attr, key, dv in (("Volume backsound", "mv", "music_vol", 0.5), ("Volume SFX", "sv", "sfx_vol", 0.9)):
                ctk.CTkLabel(left, text=label, text_color="#c9bcae").pack(anchor="w", padx=20, pady=(8, 0))
                s = ctk.CTkSlider(left, from_=0, to=1.2, progress_color=ACC, button_color=ACC2)
                s.set(g.get(key, dv))
                s.pack(fill="x", padx=18)
                setattr(self, attr, s)

            sec("VISUAL")
            self.style = ctk.CTkOptionMenu(left, values=["Paper Collage", "Motion Graphics Flat"],
                                           fg_color=PANEL2, button_color=ACC2)
            self.style.set("Motion Graphics Flat" if g.get("style") == "flat" else "Paper Collage")
            self.style.pack(fill="x", padx=18, pady=(0, 8))
            self.bing = ctk.CTkSwitch(left, text="Foto pop-up dari Bing (tanpa API key)", progress_color=ACC2)
            if g.get("use_bing", g.get("use_wiki", True)):
                self.bing.select()
            self.bing.pack(anchor="w", padx=20)
            self.cam = ctk.CTkSwitch(left, text="Kamera zoom halus", progress_color=ACC2)
            if g.get("camera", True):
                self.cam.select()
            self.cam.pack(anchor="w", padx=20, pady=(6, 0))

            sec("FOLDER OUTPUT")
            orow = ctk.CTkFrame(left, fg_color="transparent")
            orow.pack(fill="x", padx=18, pady=(0, 18))
            self.out = ctk.CTkEntry(orow, fg_color="#17120f", border_width=0)
            self.out.insert(0, g.get("outdir", Settings().outdir))
            self.out.pack(side="left", fill="x", expand=True)
            ctk.CTkButton(orow, text="…", width=36, fg_color=PANEL2, hover_color="#3a2f2a",
                          command=self._pick_out).pack(side="right", padx=(6, 0))

            # ----- kanan
            head = ctk.CTkFrame(right, fg_color=PANEL, corner_radius=18)
            head.grid(row=0, column=0, sticky="ew")
            head.grid_columnconfigure(1, weight=1)
            self.prev = ctk.CTkLabel(head, text="", width=270, height=480)
            self.prev.grid(row=0, column=0, rowspan=4, padx=16, pady=16)
            self._set_preview(placeholder_preview())
            ctk.CTkLabel(head, text="STATUS", font=ctk.CTkFont("Segoe UI", 12, "bold"), text_color=ACC).grid(
                row=0, column=1, sticky="sw", padx=10, pady=(24, 0))
            self.stage = ctk.CTkLabel(head, text="Siap. Masukkan topik lalu klik GENERATE.", anchor="w",
                                      font=ctk.CTkFont("Segoe UI", 15), wraplength=520, justify="left")
            self.stage.grid(row=1, column=1, sticky="ew", padx=10)
            self.bar = ctk.CTkProgressBar(head, height=16, progress_color=ACC2, fg_color=PANEL2, corner_radius=8)
            self.bar.set(0)
            self.bar.grid(row=2, column=1, sticky="ew", padx=10, pady=10)
            bt = ctk.CTkFrame(head, fg_color="transparent")
            bt.grid(row=3, column=1, sticky="nw", padx=10)
            self.b_cancel = ctk.CTkButton(bt, text="■ Batal", width=90, fg_color="#5b2a2a", hover_color="#7a3636",
                                          state="disabled", command=self.cancel)
            self.b_cancel.pack(side="left")
            self.b_play = ctk.CTkButton(bt, text="▶ Putar", width=90, fg_color=PANEL2, hover_color="#3a2f2a",
                                        state="disabled", command=lambda: self._open(self.last_out))
            self.b_play.pack(side="left", padx=8)
            self.b_folder = ctk.CTkButton(bt, text="📂 Folder", width=100, fg_color=PANEL2, hover_color="#3a2f2a",
                                          command=lambda: self._open(self.out.get()))
            self.b_folder.pack(side="left")

            tabs = ctk.CTkTabview(right, fg_color=PANEL, corner_radius=18, segmented_button_selected_color=ACC2)
            tabs.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
            tabs.add("Log")
            tabs.add("Naskah")
            self.logbox = ctk.CTkTextbox(tabs.tab("Log"), fg_color="#0d0b0a", font=ctk.CTkFont("Consolas", 12))
            self.logbox.pack(fill="both", expand=True)
            self.scriptbox = ctk.CTkTextbox(tabs.tab("Naskah"), fg_color="#0d0b0a", font=ctk.CTkFont("Segoe UI", 13))
            self.scriptbox.pack(fill="both", expand=True)
            self._log(f"{APP} v{VERSION} siap. Otak naskah: {API_BASE} (model {MODEL}).")

        # ---------- helper UI
        def _lang_changed(self, v, keep=None):
            code = "id" if v == "Indonesia" else "en"
            self.voice.configure(values=VOICES[code])
            self.voice.set(keep if keep in VOICES[code] else VOICES[code][0])

        def _music_txt(self):
            return ("♪ " + os.path.basename(self.music_path)) if self.music_path else "♪ Backsound: auto-generate sesuai mood"

        def _dur_txt(self, v=None):
            v = self.duration.get() if v is None else v
            return "Auto" if not v or v <= 0 else f"{int(v)} detik"

        def _pick_music(self):
            p = filedialog.askopenfilename(filetypes=[("Audio", "*.mp3 *.wav *.m4a *.ogg")])
            if p:
                self.music_path = p
                self.music_lbl.configure(text=self._music_txt())

        def _clear_music(self):
            self.music_path = ""
            self.music_lbl.configure(text=self._music_txt())

        def _pick_out(self):
            d = filedialog.askdirectory()
            if d:
                self.out.delete(0, "end")
                self.out.insert(0, d)

        def _open(self, p):
            if not p:
                return
            try:
                if os.name == "nt":
                    os.startfile(p)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", p])
            except Exception as e:
                messagebox.showerror(APP, str(e))

        def _log(self, m):
            self.logbox.insert("end", f"[{time.strftime('%H:%M:%S')}] {m}\n")
            self.logbox.see("end")

        def _set_preview(self, pil):
            self._img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(270, 480))
            self.prev.configure(image=self._img)

        # ---------- aksi
        def collect(self) -> Settings:
            raw = self.topic.get().strip()
            raw = re.sub(r"^/?paper-collage\s*", "", raw, flags=re.I).strip().strip('"')
            return Settings(
                topic=raw, outdir=self.out.get().strip(), res=self.res.get(), fps=int(self.fps.get()),
                scenes=int(self.scenes.get()), lang="id" if self.lang.get() == "Indonesia" else "en",
                mood=self.mood.get(), voice_on=bool(self.vo.get()), voice=self.voice.get(),
                music_path=self.music_path, music_vol=float(self.mv.get()), sfx_vol=float(self.sv.get()),
                use_bing=bool(self.bing.get()), gpu=bool(self.gpu.get()), camera=bool(self.cam.get()),
                duration=float(self.duration.get()),
                style="flat" if self.style.get().startswith("Motion") else "paper",
                img_folder=self.cfg.get("img_folder", ""))

        def start(self):
            if self.pipe:
                return
            st = self.collect()
            if not st.topic:
                messagebox.showwarning(APP, "Isi topik dulu, mis: Sejarah kopi di Nusantara")
                return
            d = asdict(st)
            d.pop("topic")
            d.pop("script")
            save_cfg(d)
            self.pipe = Pipeline(st, _Bridge(self.q))
            self.btn.configure(state="disabled", text="⏳  SEDANG MEMBUAT ...")
            self.b_cancel.configure(state="normal")
            self.b_play.configure(state="disabled")
            self.bar.set(0)
            self._log(f"Mulai: '{st.topic}'  [{st.res} · {st.fps}fps]")
            threading.Thread(target=self._work, daemon=True).start()

        def _work(self):
            try:
                out = self.pipe.run()
                self.q.put(("done", out))
            except Cancelled:
                self.q.put(("cancel", None))
            except Exception:
                self.q.put(("error", traceback.format_exc()))

        def cancel(self):
            if self.pipe:
                self.pipe.cancel.set()
                self._log("Membatalkan ...")

        def _poll(self):
            try:
                while True:
                    k, a = self.q.get_nowait()
                    if k == "log":
                        self._log(a)
                    elif k == "prog":
                        self.bar.set(a[0])
                        self.stage.configure(text=a[1])
                    elif k == "preview":
                        self._set_preview(a)
                    elif k == "script":
                        self._show_script(a)
                    elif k in ("done", "cancel", "error"):
                        self.pipe = None
                        self.btn.configure(state="normal", text="▶  GENERATE VIDEO")
                        self.b_cancel.configure(state="disabled")
                        if k == "done":
                            self.last_out = a
                            self.b_play.configure(state="normal")
                            self.stage.configure(text="✅ Selesai: " + a)
                            self._log("Selesai → " + a)
                        elif k == "cancel":
                            self.stage.configure(text="Dibatalkan.")
                            self.bar.set(0)
                        else:
                            self._log(a)
                            self.stage.configure(text="❌ Terjadi kesalahan, lihat tab Log.")
            except queue.Empty:
                pass
            self.after(60, self._poll)

        def _show_script(self, s):
            self.scriptbox.delete("1.0", "end")
            t = f"{s['title']}\n\nHOOK  : {s['hook']['text']}\n\n"
            for i, sc in enumerate(s["scenes"], 1):
                t += f"{i}. {sc['headline']}  {sc['emoji']}\n   {sc['narration']}\n   pop-up: {' | '.join(sc['popups'])}\n\n"
            t += f"CLOSING: {s['closing']['text']}\n" + " ".join("#" + h for h in s["hashtags"])
            self.scriptbox.insert("1.0", t)

    class _Bridge:
        def __init__(self, q):
            self.q = q

        def log(self, m):
            self.q.put(("log", m))

        def progress(self, f, s):
            self.q.put(("prog", (f, s)))

        def preview(self, im):
            self.q.put(("preview", im))

        def script(self, s):
            self.q.put(("script", s))

    app = App()
    app.mainloop()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
class _ConsoleCB:
    def __init__(self, logfile):
        self.f = logfile
        self._last = -1

    def log(self, m):
        line = f"[{time.strftime('%H:%M:%S')}] {m}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        self.f.write(line + "\n")
        self.f.flush()

    def progress(self, f, s):
        pct = int(f * 100)
        if pct != self._last and pct % 5 == 0:
            self._last = pct
            self.log(f"{pct}%  {s}")

    def preview(self, im):
        pass

    def script(self, s):
        pass


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    for d in ("music", "sfx", "fonts", "images"):
        os.makedirs(os.path.join(ASSETS, d), exist_ok=True)
    low = [a.lower() for a in args]
    topic, headless, opts = "", False, {}
    cmd_i = next((i for i, a in enumerate(low) if a in ("/paper-collage", "--paper-collage", "-paper-collage")), None)
    rest = args[cmd_i + 1:] if cmd_i is not None else args
    words, i = [], 0
    while i < len(rest):
        a = rest[i]
        if a == "--headless":
            headless = True
        elif a in ("--res", "--fps", "--out", "--scenes", "--lang", "--mood", "--duration", "--style"):
            opts[a[2:]] = rest[i + 1] if i + 1 < len(rest) else ""
            i += 1
        elif a == "--no-voice":
            opts["no_voice"] = True
        elif a == "--gpu":
            opts["gpu"] = True
        elif not a.startswith("--"):
            words.append(a)
        i += 1
    topic = " ".join(words).strip()
    if not headless:
        run_gui(prefill=topic, autostart=bool(topic) and cmd_i is not None)
        return 0
    if not topic:
        print('Pemakaian: PaperCollage.exe /paper-collage "TOPIK" --headless [--res 4K|2K|FHD|HD] [--fps 30] '
              '[--out FOLDER] [--scenes 6] [--duration 30] [--lang id|en] [--no-voice] [--gpu]')
        return 2
    cfg = load_cfg()
    st = Settings(**{k: v for k, v in cfg.items() if k in Settings.__dataclass_fields__ and k not in ("topic", "script")})
    st.topic = topic
    st.res = opts.get("res", st.res)
    st.fps = int(opts.get("fps", st.fps))
    st.outdir = opts.get("out", st.outdir)
    st.scenes = int(opts.get("scenes", st.scenes))
    st.lang = opts.get("lang", st.lang)
    st.mood = opts.get("mood", st.mood)
    st.duration = float(opts.get("duration", st.duration) or 0)
    st.style = "flat" if str(opts.get("style", st.style)).lower().startswith("flat") else "paper"
    if st.lang == "en" and st.voice.startswith("id-"):
        st.voice = VOICES["en"][0]
    if st.lang == "id" and st.voice.startswith("en-"):
        st.voice = VOICES["id"][0]
    if opts.get("no_voice"):
        st.voice_on = False
    if opts.get("gpu"):
        st.gpu = True
    os.makedirs(st.outdir, exist_ok=True)
    with open(os.path.join(st.outdir, "paper_collage.log"), "a", encoding="utf-8") as lf:
        cb = _ConsoleCB(lf)
        try:
            out = Pipeline(st, cb).run()
            cb.log("SELESAI -> " + out)
            return 0
        except Exception:
            cb.log(traceback.format_exc())
            return 1


if __name__ == "__main__":
    sys.exit(main())
