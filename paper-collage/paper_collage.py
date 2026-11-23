#!/usr/bin/env python3
"""
/paper-collage [TOPIK]
======================
Bikin video vertikal bergaya paper-collage (stop-motion kertas) dari satu topik.

  Otak      : https://aiapifreekey.com  (API keyless, format OpenAI, model free-ai)
  Visual    : dirender prosedural dengan Pillow/numpy (kertas sobek, tape, polaroid, burst)
  Audio     : SFX + backsound disintesis sendiri (tanpa file lisensi), bisa di-override
  Output    : MP4 H.264, 9:16, 2160x3840 (4K), 30 fps, AAC 48 kHz

Pemakaian:
  python3 paper_collage.py "Kenapa langit berwarna biru"
  python3 paper_collage.py "Sejarah kopi" --duration 40 --quality 1080p
  python3 paper_collage.py "Tips tidur nyenyak" --quality preview      # cepat, buat cek
  python3 paper_collage.py --storyboard output/xxx/storyboard.json      # tanpa panggil API
"""
import argparse
import collections
import html
import io
import json
import math
import multiprocessing as mp
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# --------------------------------------------------------------------------- #
# Konfigurasi
# --------------------------------------------------------------------------- #
API_BASE = os.environ.get("PC_API_BASE", "https://api.aiapifreekey.com/v1")
API_MODEL = os.environ.get("PC_MODEL", "free-ai")
API_KEY = os.environ.get("PC_API_KEY", "free")  # isi string apa saja
UA = "paper-collage/1.0 (+https://aiapifreekey.com)"
BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

BASE_W, BASE_H = 1080, 1920      # koordinat desain (9:16)
SR = 48000                       # sample rate audio
FPS_OUT = 30
TR = 0.6                         # durasi transisi (detik)
POP = 0.34                       # durasi pop-in sprite (detik)

QUALITY = {"4k": 2.0, "1080p": 1.0, "preview": 0.5}

THEMES = {
    "kraft":   dict(bg=(206, 168, 124), accent=(226, 84, 62),   accent2=(40, 118, 140)),
    "mint":    dict(bg=(188, 226, 207), accent=(255, 107, 107), accent2=(255, 196, 80)),
    "peach":   dict(bg=(255, 214, 190), accent=(236, 84, 118),  accent2=(58, 98, 160)),
    "sky":     dict(bg=(184, 220, 245), accent=(255, 146, 66),  accent2=(56, 86, 170)),
    "lilac":   dict(bg=(214, 200, 241), accent=(255, 108, 146), accent2=(255, 208, 100)),
    "mustard": dict(bg=(243, 205, 98),  accent=(212, 62, 68),   accent2=(28, 88, 110)),
}
THEME_ORDER = list(THEMES)
PAPER = (252, 249, 241)
INK = (36, 30, 40)

MOODS = {
    "ceria":      dict(bpm=112, key=60, scale=[0, 2, 4, 7, 9],  swing=0.00, kick=[0, 2],
                       prog=[(0, [0, 4, 7, 11]), (9, [0, 3, 7, 10]), (5, [0, 4, 7, 11]), (7, [0, 4, 7, 10])]),
    "santai":     dict(bpm=82,  key=62, scale=[0, 3, 5, 7, 10], swing=0.14, kick=[0, 2],
                       prog=[(0, [0, 3, 7, 10]), (5, [0, 4, 7, 11]), (10, [0, 4, 7, 11]), (7, [0, 3, 7, 10])]),
    "dramatis":   dict(bpm=96,  key=57, scale=[0, 3, 5, 7, 10], swing=0.00, kick=[0, 1, 2, 3],
                       prog=[(0, [0, 3, 7]), (8, [0, 4, 7]), (3, [0, 4, 7]), (10, [0, 4, 7])]),
    "misteri":    dict(bpm=76,  key=60, scale=[0, 3, 5, 7, 10], swing=0.08, kick=[0],
                       prog=[(0, [0, 3, 7, 10]), (8, [0, 4, 7, 11]), (5, [0, 3, 7, 10]), (7, [0, 3, 7, 10])]),
    "inspiratif": dict(bpm=100, key=55, scale=[0, 2, 4, 7, 9],  swing=0.00, kick=[0, 2],
                       prog=[(0, [0, 4, 7, 11]), (7, [0, 4, 7, 10]), (9, [0, 3, 7, 10]), (5, [0, 4, 7, 11])]),
}

TRANSITIONS = ["slide", "tear", "iris", "flip"]
BGS = ["plain", "graph", "lined", "dots"]

# state global (di-set di main, di-fork ke worker)
S = 1.0          # skala render relatif ke 1080x1920
CW, CH = BASE_W, BASE_H
STOP_FPS = 15
NR = np.random.default_rng(1)
R = random.Random(1)
VIG = None
SEGS = []
ARGS = None


def P(x):
    return int(round(x * S))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def clamp(x, a, b):
    return max(a, min(b, x))


def ease_out_back(p):
    c1 = 1.70158
    c3 = c1 + 1
    p = clamp(p, 0, 1) - 1
    return 1 + c3 * p ** 3 + c1 * p ** 2


def smooth(p):
    p = clamp(p, 0, 1)
    return p * p * (3 - 2 * p)


def slugify(s):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:48] or "video"


def ink_for(fill):
    lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
    return INK if lum > 150 else (255, 255, 255)


# --------------------------------------------------------------------------- #
# 1) OTAK: aiapifreekey.com -> storyboard JSON
# --------------------------------------------------------------------------- #
LANGS = {"id": "Bahasa Indonesia", "en": "English", "ms": "Bahasa Melayu", "jv": "Basa Jawa"}


def system_prompt(lang):
    return f"""Kamu adalah sutradara video pendek vertikal (TikTok/Reels/Shorts) bergaya paper collage stop-motion.
Balas HANYA dengan satu objek JSON valid. Tanpa markdown, tanpa penjelasan, tanpa komentar.

Skema:
{{
  "title": "judul video, max 8 kata",
  "mood": "ceria | santai | dramatis | misteri | inspiratif",
  "theme": "kraft | mint | peach | sky | lilac | mustard",
  "hook": {{"text": "kalimat pembuka bikin penasaran, max 8 kata", "sub": "kalimat pendukung, max 10 kata"}},
  "scenes": [
    {{
      "heading": "judul scene, max 5 kata",
      "body": "1-2 kalimat padat dan akurat, max 22 kata",
      "popup": {{"type": "text | image",
                "text": "kata kunci pop-up, max 3 kata, HURUF KAPITAL",
                "image_query": "query foto dalam bahasa Inggris, 2-4 kata (hanya jika type=image)"}},
      "bg": "plain | graph | lined | dots",
      "transition": "slide | tear | iris | flip"
    }}
  ],
  "closing": {{"text": "kalimat penutup, max 8 kata", "cta": "ajakan singkat, max 4 kata"}}
}}

Aturan:
- Bahasa semua teks: {LANGS.get(lang, lang)}.
- Jumlah scene harus persis sesuai permintaan.
- Selang-seling popup type "text" dan "image"; image_query harus objek konkret yang mudah difoto.
- Hook harus kuat di 2 detik pertama (pertanyaan, fakta mengejutkan, atau klaim berani yang benar).
- Fakta harus akurat. Jangan mengarang angka atau kutipan.
- Variasikan transition dan bg antar scene."""


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("tidak ada JSON di balasan")
    raw = text[a:b + 1]
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    return json.loads(raw)


def ask_brain(topic, n_scenes, lang, retries=6):
    body = {
        "model": API_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt(lang)},
            {"role": "user", "content": f"Topik: {topic}\nJumlah scene: {n_scenes}"},
        ],
        "temperature": 0.8,
        "max_tokens": 2500,
    }
    data = json.dumps(body).encode()
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            API_BASE.rstrip("/") + "/chat/completions", data=data, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                js = json.loads(r.read().decode("utf-8", "replace"))
            content = js["choices"][0]["message"]["content"]
            return extract_json(content)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 503):
                wait = float(e.headers.get("Retry-After") or (2 + attempt * 2))
                log(f"  [otak] {last}, tunggu {wait:.0f}s (percobaan {attempt}/{retries})")
                time.sleep(wait)
                continue
            raise RuntimeError(f"API error {e.code}: {e.read()[:200]!r}")
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            last = f"balasan tidak valid ({e})"
            log(f"  [otak] {last}, ulangi ({attempt}/{retries})")
            time.sleep(1.2)  # batas 1 req/detik
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
            log(f"  [otak] jaringan: {last}, ulangi ({attempt}/{retries})")
            time.sleep(2 + attempt)
    raise RuntimeError(f"Gagal mendapat storyboard dari API: {last}")


def clip(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0]


def normalize(sb, topic, n_scenes, args):
    mood = args.mood or sb.get("mood")
    theme = args.theme or sb.get("theme")
    out = {
        "topic": topic,
        "title": clip(sb.get("title") or topic, 80),
        "mood": mood if mood in MOODS else "ceria",
        "theme": theme if theme in THEMES else "kraft",
    }
    hk = sb.get("hook") or {}
    out["hook"] = {"text": clip(hk.get("text") or topic, 70), "sub": clip(hk.get("sub"), 80)}
    scenes = []
    for i, s in enumerate((sb.get("scenes") or [])[:10]):
        pop = s.get("popup") or {}
        ptype = pop.get("type") if pop.get("type") in ("text", "image") else ("image" if i % 2 else "text")
        ptext = clip(pop.get("text") or s.get("heading") or "", 28).upper()
        scenes.append({
            "heading": clip(s.get("heading") or f"Poin {i + 1}", 48),
            "body": clip(s.get("body"), 190),
            "popup": {"type": ptype, "text": ptext, "image_query": clip(pop.get("image_query") or ptext, 60)},
            "bg": s.get("bg") if s.get("bg") in BGS else BGS[i % len(BGS)],
            "transition": s.get("transition") if s.get("transition") in TRANSITIONS else TRANSITIONS[i % 4],
        })
    if not scenes:
        raise RuntimeError("Storyboard kosong: tidak ada scene.")
    out["scenes"] = scenes
    cl = sb.get("closing") or {}
    out["closing"] = {"text": clip(cl.get("text") or "Sampai jumpa!", 70), "cta": clip(cl.get("cta") or "Follow ya!", 30)}
    return out


# --------------------------------------------------------------------------- #
# 2) Font, bentuk kertas, sprite
# --------------------------------------------------------------------------- #
TITLE_FONTS = ["Anton", "BebasNeue", "LilitaOne", "Bangers", "Poppins-ExtraBold", "Montserrat-ExtraBold",
               "Impact", "ariblk", "DejaVuSans-Bold", "arialbd", "Inter", "OpenSans", "DejaVuSans"]
BODY_FONTS = ["Caveat", "PatrickHand", "ShadowsIntoLight", "ComicNeue", "comic", "Poppins-SemiBold",
              "Poppins-Medium", "Nunito", "DMSans", "OpenSans", "DejaVuSans", "arial"]
_FONT_INDEX = None


def font_index():
    global _FONT_INDEX
    if _FONT_INDEX is None:
        idx = {}
        dirs = ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
                os.path.expanduser("~/.local/share/fonts"), "/Library/Fonts", "/System/Library/Fonts",
                os.path.expanduser("~/Library/Fonts"), "C:\\Windows\\Fonts",
                str(Path(__file__).parent / "assets" / "fonts")]
        for d in dirs:
            for root, _, files in os.walk(d):
                for f in files:
                    if f.lower().endswith((".ttf", ".otf")):
                        idx.setdefault(f.lower(), os.path.join(root, f))
        _FONT_INDEX = idx
    return _FONT_INDEX


def find_font(names, override=None):
    if override and os.path.exists(override):
        return override
    idx = font_index()
    for n in names:
        n = n.lower()
        for k, v in idx.items():
            if k.startswith(n):
                return v
    return None


@lru_cache(maxsize=256)
def get_font(path, size, bold=False):
    size = max(8, int(size))
    if path:
        try:
            f = ImageFont.truetype(path, size)
            if bold:
                try:
                    f.set_variation_by_name("Bold")
                except Exception:
                    pass
            return f
        except Exception:
            pass
    return ImageFont.load_default(size)


def wrap_lines(text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if font.getlength(test) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def fit_text(text, path, max_w, max_h, size, min_size, bold=False, gap=1.18):
    while True:
        font = get_font(path, size, bold)
        lines = wrap_lines(text, font, max_w)
        h = len(lines) * size * gap
        widest = max(font.getlength(l) for l in lines)
        if (h <= max_h and widest <= max_w) or size <= min_size:
            return font, lines, size
        size *= 0.93


def draw_block(draw, lines, font, cx, cy, size, fill, gap=1.18):
    """Gambar baris-baris teks rata tengah, terpusat secara visual (berdasarkan tinggi huruf kapital)."""
    hb = font.getbbox("H")
    cap_top, cap_h = hb[1], hb[3] - hb[1]
    pitch = size * gap
    total = (len(lines) - 1) * pitch + cap_h
    y = cy - total / 2 - cap_top + size * 0.03
    for l in lines:
        draw.text((cx - font.getlength(l) / 2, y), l, font=font, fill=fill)
        y += pitch


def draw_lines(draw, lines, font, cx, top, size, fill, gap=1.18, stroke=0, stroke_fill=None):
    y = top
    for l in lines:
        w = font.getlength(l)
        draw.text((cx - w / 2, y), l, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)
        y += size * gap


# ---- polygon helpers (koordinat piksel, sudah termasuk pad) ----
def torn_poly(w, h, rough, step, ox, oy):
    pts = []
    corners = [(0, 0), (w, 0), (w, h), (0, h)]
    normals = [(0, -1), (1, 0), (0, 1), (-1, 0)]
    for i in range(4):
        p0, p1, nrm = corners[i], corners[(i + 1) % 4], normals[i]
        L = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        n = max(2, int(L / max(2, step)))
        j = 0.0
        for k in range(n):
            t = k / n
            j = 0.55 * j + R.uniform(-rough, rough)
            pts.append((p0[0] + (p1[0] - p0[0]) * t + nrm[0] * j + ox, p0[1] + (p1[1] - p0[1]) * t + nrm[1] * j + oy))
    return pts


def star_poly(d, spikes, inner, jitter, ox, oy):
    pts, r = [], d / 2
    for i in range(spikes * 2):
        a = math.pi * i / spikes - math.pi / 2
        rr = r if i % 2 == 0 else r * inner
        rr *= 1 + R.uniform(-jitter, jitter)
        pts.append((ox + r + rr * math.cos(a), oy + r + rr * math.sin(a)))
    return pts


def blob_poly(d, jitter, ox, oy):
    pts, r, j = [], d / 2, 0.0
    for i in range(56):
        a = 2 * math.pi * i / 56
        j = 0.6 * j + R.uniform(-jitter, jitter)
        rr = r * (1 + j)
        pts.append((ox + r + rr * math.cos(a), oy + r + rr * math.sin(a)))
    return pts


def heart_poly(d, ox, oy):
    pts = []
    for i in range(96):
        t = 2 * math.pi * i / 96
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((ox + d / 2 + x / 34 * d, oy + d / 2 + y / 34 * d + d * 0.04))
    return pts


def sprite_from_poly(size, poly, fill, painter=None, edge=True, alpha=255, shadow=0.42):
    """size=(W,H) piksel (sudah termasuk pad). Return RGBA dengan bayangan lembut."""
    W, H = size
    ss = 2 if S < 1.5 else 1
    big = Image.new("L", (W * ss, H * ss), 0)
    ImageDraw.Draw(big).polygon([(x * ss, y * ss) for x, y in poly], fill=255)
    mask = big.resize((W, H), Image.LANCZOS) if ss > 1 else big
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if edge:
        inner = mask.filter(ImageFilter.GaussianBlur(P(3.5))).point(lambda v: 255 if v >= 250 else 0)
        layer.paste(Image.new("RGBA", (W, H), PAPER + (255,)), (0, 0), mask)
        layer.paste(Image.new("RGBA", (W, H), tuple(fill) + (255,)), (0, 0), inner)
    else:
        layer.paste(Image.new("RGBA", (W, H), tuple(fill) + (255,)), (0, 0), mask)
    # grain kertas
    arr = np.asarray(layer).astype(np.int16)
    arr[..., :3] += NR.normal(0, 3.0, (H, W, 1)).astype(np.int16)
    layer = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")
    if painter:
        painter(layer, ImageDraw.Draw(layer), W, H)
    a = layer.getchannel("A")
    if alpha < 255:
        a = a.point(lambda v: int(v * alpha / 255))
        layer.putalpha(a)
    if shadow > 0:
        sh = a.filter(ImageFilter.GaussianBlur(P(11))).point(lambda v: int(v * shadow))
        sl = Image.new("RGBA", (W, H), (24, 14, 10, 0))
        sl.putalpha(sh)
        sl = ImageChops.offset(sl, P(5), P(11))
        return Image.alpha_composite(sl, layer)
    return layer


PAD = lambda: P(58)


def paper_sprite(w, h, fill, painter=None, rough=4.5, edge=True, alpha=255, shadow=0.42):
    pad = PAD()
    W, H = w + 2 * pad, h + 2 * pad
    poly = torn_poly(w, h, P(rough), P(15), pad, pad)
    return sprite_from_poly((W, H), poly, fill, painter, edge, alpha, shadow)


def burst_sprite(d, fill, text, font_path, text_fill=None, spikes=14):
    pad = PAD()
    poly = star_poly(d, spikes, 0.80, 0.045, pad, pad)
    size = (d + 2 * pad, d + 2 * pad)
    tf = text_fill or ink_for(fill)

    def painter(layer, draw, W, H):
        font, lines, sz = fit_text(text, font_path, d * 0.66, d * 0.50, d * 0.20, d * 0.07, True, 1.08)
        draw_block(draw, lines, font, W / 2, H / 2, sz, tf, 1.08)

    return sprite_from_poly(size, poly, fill, painter)


def blob_sprite(d, fill, text=None, font_path=None, text_fill=None):
    pad = PAD()
    poly = blob_poly(d, 0.035, pad, pad)

    def painter(layer, draw, W, H):
        if text:
            font = get_font(font_path, d * 0.42, True)
            draw_block(draw, [text], font, W / 2, H / 2, d * 0.42, text_fill or ink_for(fill), 1.0)

    return sprite_from_poly((d + 2 * pad, d + 2 * pad), poly, fill, painter if text else None)


def tape_sprite(w, h, color):
    pad = PAD()
    z = P(9)
    pts = [(z, 0), (w - z, 0), (w, h * .25), (w - z, h * .5), (w, h * .75), (w - z, h), (z, h), (0, h * .75),
           (z, h * .5), (0, h * .25)]
    poly = [(x + pad, y + pad) for x, y in pts]

    def painter(layer, draw, W, H):
        step = P(22)
        for x in range(pad - step, pad + w + step, step):
            draw.line([(x, pad), (x + step // 2, pad + h)], fill=(255, 255, 255, 70), width=P(8))

    return sprite_from_poly((w + 2 * pad, h + 2 * pad), poly, color, painter, edge=False, alpha=205, shadow=0.25)


def heart_sprite(d, fill):
    pad = PAD()
    return sprite_from_poly((d + 2 * pad, d + 2 * pad), heart_poly(d, pad, pad), fill)


class Sprite:
    def __init__(self, img, cx, cy, rot=0.0, t0=0.0, spin=0.0, pop=True):
        self.img, self.cx, self.cy, self.rot, self.t0, self.spin, self.pop = img, cx, cy, rot, t0, spin, pop


def place(canvas, spr, cx, cy, rot, scale):
    im = spr
    if abs(scale - 1) > 0.004:
        im = im.resize((max(2, int(im.width * scale)), max(2, int(im.height * scale))), Image.BILINEAR)
    if abs(rot) > 0.05:
        im = im.rotate(rot, resample=Image.BICUBIC, expand=True)
    canvas.paste(im, (int(cx - im.width / 2), int(cy - im.height / 2)), im)


# --------------------------------------------------------------------------- #
# 3) Background kertas & dekorasi
# --------------------------------------------------------------------------- #
def make_vignette():
    yy = np.linspace(-1, 1, CH, dtype=np.float32)[:, None]
    xx = np.linspace(-1, 1, CW, dtype=np.float32)[None, :]
    r2 = (xx ** 2 + yy ** 2) / 2
    return (1 - 0.16 * r2)[..., None]


def make_background(kind, color):
    small = NR.normal(0, 1, (max(2, CH // 48), max(2, CW // 48))).astype(np.float32)
    blot = np.asarray(Image.fromarray(small, "F").resize((CW, CH), Image.BICUBIC))
    grain = NR.normal(0, 1, (CH, CW)).astype(np.float32)
    arr = np.array(color, dtype=np.float32)[None, None, :] + (blot * 3.0 + grain * 2.2)[..., None]
    arr *= VIG
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img, "RGBA")
    dark = tuple(int(c * 0.72) for c in color)
    if kind == "graph":
        st = P(60)
        for x in range(0, CW, st):
            d.line([(x, 0), (x, CH)], fill=dark + (55,), width=max(1, P(2)))
        for y in range(0, CH, st):
            d.line([(0, y), (CW, y)], fill=dark + (55,), width=max(1, P(2)))
    elif kind == "lined":
        st = P(74)
        for y in range(P(210), CH, st):
            d.line([(0, y), (CW, y)], fill=(70, 120, 190, 70), width=max(1, P(2)))
        d.line([(P(120), 0), (P(120), CH)], fill=(220, 70, 70, 110), width=max(1, P(3)))
    elif kind == "dots":
        st, r = P(66), P(3.2)
        for y in range(st // 2, CH, st):
            for x in range(st // 2, CW, st):
                d.ellipse([x - r, y - r, x + r, y + r], fill=dark + (70,))
    return img


DECOR_SLOTS = [(110, 150), (975, 130), (80, 1690), (990, 1700), (1005, 900), (70, 960), (540, 70), (540, 1850)]


def make_decor(th, n, t_base=0.0):
    out = []
    slots = R.sample(DECOR_SLOTS, min(n, len(DECOR_SLOTS)))
    for i, (x, y) in enumerate(slots):
        kind = R.choice(["tape", "blob", "star", "tape"])
        col = R.choice([th["accent"], th["accent2"], PAPER])
        if kind == "tape":
            img = tape_sprite(P(R.randint(210, 300)), P(R.randint(62, 84)), col)
        elif kind == "blob":
            img = blob_sprite(P(R.randint(110, 190)), col)
        else:
            img = burst_sprite(P(R.randint(120, 190)), col, "", None, spikes=R.choice([8, 10, 12]))
        out.append(Sprite(img, x + R.uniform(-30, 30), y + R.uniform(-30, 30), R.uniform(-38, 38),
                          t_base + 0.04 + i * 0.07, R.choice([-25, 25])))
    return out


# --------------------------------------------------------------------------- #
# 4) Builder scene
# --------------------------------------------------------------------------- #
class Scene:
    def __init__(self, name, bg, sprites, dur, events, trans_in="slide"):
        self.name, self.bg, self.sprites, self.dur, self.events, self.trans_in = name, bg, sprites, dur, events, trans_in
        self.start = 0.0

    def render(self, t, seed):
        canvas = self.bg.copy()
        rng = random.Random(seed)
        for sp in self.sprites:
            lt = t - sp.t0
            if lt < 0:
                continue
            p = clamp(lt / POP, 0, 1)
            sc = max(0.02, ease_out_back(p)) if sp.pop else 1.0
            rot = sp.rot + (1 - smooth(p)) * sp.spin + rng.uniform(-0.55, 0.55)
            dx, dy = rng.uniform(-1, 1) * P(3.2), rng.uniform(-1, 1) * P(3.2)
            place(canvas, sp.img, sp.cx * S + dx, sp.cy * S + dy, rot, sc)
        return canvas


def title_strips(text, th, t0, y_center, fonts, max_size=150, max_h=720):
    """Teks besar dipecah per baris, tiap baris = strip kertas sobek."""
    font, lines, sz = fit_text(text.upper(), fonts["title"], P(900), P(max_h), P(max_size), P(60), True, 1.0)
    sprites, events, rows = [], [], len(lines)
    row_h = sz * 1.42
    styles = [(PAPER, INK), (th["accent"], None), (INK, PAPER), (th["accent2"], None)]
    for i, line in enumerate(lines):
        fill, tcol = styles[i % len(styles)]
        tcol = tcol or ink_for(fill)
        w = int(font.getlength(line) + P(90))
        h = int(sz * 1.3)

        def painter(layer, draw, W, H, line=line, tcol=tcol, w=w, h=h):
            draw_block(draw, [line], font, W / 2, H / 2, sz, tcol, 1.0)

        spr = paper_sprite(w, h, fill, painter, edge=(fill == PAPER or True))
        cy = y_center + (i - (rows - 1) / 2) * row_h / S
        sprites.append(Sprite(spr, 540, cy, (-2.6, 2.2, -1.6, 2.8)[i % 4], t0 + i * 0.4, (-12, 12)[i % 2]))
        events.append((t0 + i * 0.4, "pop", 0.85 + 0.05 * i))
    bottom = y_center + (rows * row_h / S) / 2
    return sprites, events, bottom


def build_hook(sb, th, fonts):
    bg = make_background("graph", th["bg"])
    sprites = make_decor(th, 6)
    st, ev, bottom = title_strips(sb["hook"]["text"], th, 0.15, 740, fonts)
    sprites += st
    events = [(0.0, "hook", 1.0)] + ev
    if sb["hook"]["sub"]:
        font, lines, sz = fit_text(sb["hook"]["sub"], fonts["body"], P(780), P(220), P(62), P(36), False)
        h = int(len(lines) * sz * 1.2 + P(70))

        def painter(layer, draw, W, H):
            draw_block(draw, lines, font, W / 2, H / 2, sz, INK, 1.2)

        sprites.append(Sprite(paper_sprite(P(860), h, (255, 252, 240), painter), 540, bottom + 150 + h / S / 2, 1.6,
                              1.5, 8))
        events.append((1.5, "paper", 0.8))
    burst = burst_sprite(P(260), th["accent"], "WOW!", fonts["title"])
    sprites.append(Sprite(burst, 880, 330, 12, 0.9, 40))
    events.append((0.9, "pop2", 0.7))
    return Scene("hook", bg, sprites, 3.9, events)


def _bing_items(query, count=35):
    """Scrape Bing Image Search (tanpa API key) -> list dict {murl, turl, title, page}."""
    for url, params in (
        # endpoint async: isinya hanya kartu hasil, paling stabil tanpa cookie
        ("https://www.bing.com/images/async",
         {"q": query, "first": 0, "count": count, "scenario": "ImageBasicHover",
          "datsrc": "I", "layout": "RowBased", "mmasync": 1}),
        # cadangan: halaman pencarian biasa
        ("https://www.bing.com/images/search", {"q": query, "form": "HDRSC2", "first": 1}),
    ):
        try:
            req = urllib.request.Request(
                url + "?" + urllib.parse.urlencode(params),
                headers={"User-Agent": BING_UA, "Accept-Language": "en-US,en;q=0.9,id-ID;q=0.8"})
            with urllib.request.urlopen(req, timeout=20) as r:
                page = r.read().decode("utf-8", "ignore")
            if 'class="iusc"' not in page:
                continue
            items = []
            for mt in re.finditer(r'class="iusc"', page):
                a, b = page.rfind("<a ", 0, mt.start()), page.find(">", mt.end())
                if a < 0 or b < 0:
                    continue
                m = re.search(r'\sm="([^"]*)"', page[a:b])
                if not m:
                    continue
                try:
                    d = json.loads(html.unescape(m.group(1)))
                except Exception:
                    continue
                mu, tu = (d.get("murl") or "").strip(), (d.get("turl") or "").strip()
                if mu or tu:
                    items.append({"murl": mu, "turl": tu, "title": str(d.get("t") or "").strip(),
                                  "page": str(d.get("purl") or "").strip()})
            if items:
                return items
        except Exception:
            continue
    return []


def _bing_title(raw, fallback):
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


def _bing_image(query, min_side=480, max_try=8):
    """Ambil 1 foto dari Bing. Return (Image RGB, judul, url sumber) atau None."""
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    if not q:
        return None
    fallback, tries = None, 0
    for it in _bing_items(q):
        mu, tu = it["murl"], it["turl"]
        if not mu or mu.split("?")[0].lower().endswith((".svg", ".gif", ".tif", ".tiff", ".ico")):
            continue
        if tries >= max_try:
            break
        # 1) gambar asli -> 2) thumbnail Bing (crop persegi 1200px)
        for u in (mu, (tu + "&w=1200&h=1200&c=7") if tu else None):
            if not u:
                continue
            try:
                req = urllib.request.Request(u, headers={"User-Agent": BING_UA})
                with urllib.request.urlopen(req, timeout=25) as r:
                    im = Image.open(io.BytesIO(r.read())).convert("RGB")
            except Exception:
                continue
            tries += 1
            small = min(im.size)
            if small < 240:  # placeholder 80x80 dari Bing
                continue
            title = _bing_title(it["title"], q)
            rec = (im, title, it["page"])
            if small >= min_side:
                return rec
            if fallback is None:
                fallback = rec
    return fallback


def get_image_for(query, args, credits):
    """Foto lokal (assets/images) -> Bing Image Search (tanpa API key) -> None."""
    toks = set(re.findall(r"[a-z0-9]+", query.lower()))
    img_dir = Path(args.assets) / "images"
    if img_dir.exists():
        best, score = None, 0
        for f in img_dir.iterdir():
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                sc = len(toks & set(re.findall(r"[a-z0-9]+", f.stem.lower())))
                if sc > score:
                    best, score = f, sc
        if best:
            try:
                return Image.open(best).convert("RGB")
            except Exception:
                pass
    if args.no_web_images:
        return None
    got = _bing_image(query)
    if got:
        im, title, page = got
        credits.append(f"{query}: {title} | {page or '-'} | sumber: Bing Image Search "
                       f"(cek lisensi di situs asal)")
        return im
    log(f"  [gambar] '{query}' tidak ditemukan di Bing; pakai sticker.")
    return None


def polaroid_sprite(photo, caption, th, fonts):
    ph = P(560)
    m, cap_h = P(38), P(118)
    w, h = ph + 2 * m, ph + m + cap_h
    sq = min(photo.size)
    photo = photo.crop(((photo.width - sq) // 2, (photo.height - sq) // 2,
                        (photo.width + sq) // 2, (photo.height + sq) // 2)).resize((ph, ph), Image.LANCZOS)

    def painter(layer, draw, W, H):
        pad = PAD()
        layer.paste(photo, (pad + m, pad + m))
        font, lines, sz = fit_text(caption, fonts["body"], w - 2 * m, cap_h * 0.8, P(60), P(30), False, 1.0)
        draw_block(draw, lines[:1], font, W / 2, pad + m + ph + cap_h / 2 + P(4), sz, INK, 1.0)

    return paper_sprite(w, h, PAPER, painter, rough=3.2), h / S


def build_scene(sc, idx, th, fonts, args, credits):
    bg = make_background(sc["bg"], th["bg"])
    sprites = make_decor(th, 5)
    events = []

    # nomor scene
    sprites.append(Sprite(blob_sprite(P(150), th["accent2"], f"{idx:02d}", fonts["title"]), 130, 120, -8, 0.1, -30))

    # heading: dua lapis kertas
    font, lines, sz = fit_text(sc["heading"], fonts["title"], P(840), P(240), P(98), P(56), True, 1.08)
    hh = int(len(lines) * sz * 1.08 + P(66))

    def hpaint(layer, draw, W, H):
        draw_block(draw, lines, font, W / 2, H / 2, sz, INK, 1.08)

    head_back = paper_sprite(P(930), hh, th["accent"])
    head = paper_sprite(P(900), hh, PAPER, hpaint)
    hcy = 330
    sprites.append(Sprite(head_back, 548, hcy + 12, 2.4, 0.42, 14))
    sprites.append(Sprite(head, 540, hcy, -1.8, 0.5, -14))
    events.append((0.5, "paper", 0.9))
    head_bottom = hcy + hh / S / 2

    # body: kartu buku tulis
    body_bottom = head_bottom
    if sc["body"]:
        bfont, blines, bsz = fit_text(sc["body"], fonts["body"], P(800), P(470), P(62), P(38), False, 1.22)
        bh = int(len(blines) * bsz * 1.22 + P(90))

        def bpaint(layer, draw, W, H):
            pad = PAD()
            y0 = pad + P(52)
            for k in range(len(blines)):
                yy = y0 + int((k + 1) * bsz * 1.22) - P(4)
                draw.line([(pad + P(26), yy), (W - pad - P(26), yy)], fill=(150, 185, 225, 255), width=max(1, P(2)))
            draw.line([(pad + P(70), pad), (pad + P(70), H - pad)], fill=(235, 130, 130, 255), width=max(1, P(3)))
            for k, l in enumerate(blines):
                draw.text((pad + P(96), y0 + k * bsz * 1.22 - bsz * 0.05), l, font=bfont, fill=INK)

        bcy = head_bottom + 60 + bh / S / 2
        sprites.append(Sprite(paper_sprite(P(900), bh, (255, 252, 238), bpaint, rough=3.0), 540, bcy, 1.3, 1.0, 10))
        sprites.append(Sprite(tape_sprite(P(230), P(70), th["accent2"]), 330, bcy - bh / S / 2 + 6, -7, 1.12, 20))
        sprites.append(Sprite(tape_sprite(P(230), P(70), th["accent2"]), 760, bcy - bh / S / 2 + 6, 8, 1.16, -20))
        events.append((1.0, "pop", 0.55))
        body_bottom = bcy + bh / S / 2

    # popup
    pop = sc["popup"]
    t_pop = 1.85
    photo = get_image_for(pop["image_query"], args, credits) if pop["type"] == "image" else None
    if photo is not None:
        spr, ph = polaroid_sprite(photo, pop["text"] or pop["image_query"].title(), th, fonts)
        cy = clamp(body_bottom + 50 + ph / 2, 1230, 1400)
        sprites.append(Sprite(spr, 540, cy, -3.2, t_pop, -30))
        sprites.append(Sprite(tape_sprite(P(250), P(74), th["accent"]), 540, cy - ph / 2 + 12, 3, t_pop + 0.1, 25))
        events.append((t_pop, "pop2", 0.9))
    elif pop["text"]:
        d = 700
        fill = th["accent"] if idx % 2 else th["accent2"]
        spr = burst_sprite(P(d), fill, pop["text"], fonts["title"])
        cy = clamp(body_bottom + 40 + d / 2, 1250, 1380)
        sprites.append(Sprite(spr, 540, cy, 4.0, t_pop, 45))
        events.append((t_pop, "pop", 0.95))

    dur = clamp(3.0 + len(sc["body"]) / 32.0, 4.4, 6.0)
    return Scene(f"scene{idx}", bg, sprites, dur, events, sc["transition"])


def build_closing(sb, th, fonts):
    bg = make_background("dots", th["bg"])
    sprites = make_decor(th, 6)
    st, ev, bottom = title_strips(sb["closing"]["text"], th, 0.5, 640, fonts, 130, 560)
    sprites += st
    events = list(ev)
    cta = burst_sprite(P(640), th["accent"], sb["closing"]["cta"], fonts["title"], spikes=16)
    sprites.append(Sprite(cta, 540, max(bottom + 330, 1210), -4, 1.5, 40))
    events.append((1.5, "pop2", 1.0))
    sprites.append(Sprite(heart_sprite(P(230), (232, 64, 96)), 880, 1010, 12, 2.1, 50))
    events.append((2.1, "pop", 0.7))
    return Scene("closing", bg, sprites, 4.8, events, "tear")


def build_timeline(sb, args, credits):
    global SEGS
    fonts = {"title": find_font(TITLE_FONTS, args.font_title), "body": find_font(BODY_FONTS, args.font_body)}
    log(f"  font judul: {fonts['title']}\n  font isi  : {fonts['body']}")
    first = THEME_ORDER.index(sb["theme"])
    theme_at = lambda i: THEMES[THEME_ORDER[(first + i) % len(THEME_ORDER)]]
    segs = [build_hook(sb, theme_at(0), fonts)]
    for i, sc in enumerate(sb["scenes"], 1):
        segs.append(build_scene(sc, i, theme_at(i), fonts, args, credits))
    segs.append(build_closing(sb, theme_at(len(sb["scenes"]) + 1), fonts))
    t = 0.0
    sfx_events = []
    for i, sg in enumerate(segs):
        sg.start = t
        if i > 0:
            sfx_events.append((t, {"slide": "whoosh", "tear": "rip", "iris": "iris", "flip": "flip"}[sg.trans_in], 0.9))
        for (lt, kind, g) in sg.events:
            sfx_events.append((t + lt, kind, g))
        t += sg.dur - (TR if i < len(segs) - 1 else 0)
    sfx_events.append((segs[-1].start + 0.0, "closing", 1.0))
    SEGS = segs
    return t, sfx_events


# --------------------------------------------------------------------------- #
# 5) Transisi & render frame
# --------------------------------------------------------------------------- #
_FIELDS = {}


def jag(rng, n):
    """Noise 1D halus bertingkat -> tepi sobekan kertas (bukan 'berbulu')."""
    def layer(k, amp):
        a = rng.normal(0, 1, n // k + 3)
        return amp * np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return (layer(40, 1.0) + layer(14, 0.35) + layer(5, 0.07)).astype(np.float32)


def get_field(kind, seed):
    key = (kind, seed)
    if key in _FIELDS:
        return _FIELDS[key]
    rng = np.random.default_rng(seed)
    yy = np.linspace(0, 1, CH, dtype=np.float32)[:, None]
    xx = np.linspace(0, 1, CW, dtype=np.float32)[None, :]
    j = jag(rng, CW)
    if kind == "slide":
        f = (1 - yy) + 0.028 * j[None, :]
    elif kind == "tear":
        f = 0.62 * xx + 0.38 * yy + 0.03 * j[None, :]
    else:  # iris
        dx, dy = (xx - 0.5) * (CW / CH), (yy - 0.5)
        ang = np.arctan2(dy, dx)
        idx = ((ang + np.pi) / (2 * np.pi) * (CW - 1)).astype(np.int32)
        f = np.hypot(dx, dy) / 0.75 + 0.03 * j[idx]
    f = f.astype(np.float32)
    if len(_FIELDS) > 6:
        _FIELDS.clear()
    _FIELDS[key] = f
    return f


def transition_frame(kind, a, b, p, seed, accent):
    if kind == "flip":
        canvas = Image.new("RGB", (CW, CH), accent)
        src, q = (a, 1 - 2 * p) if p < 0.5 else (b, 2 * p - 1)
        w = max(2, int(CW * smooth(q)))
        im = src.resize((w, CH), Image.BILINEAR)
        shade = 1 - 0.35 * (1 - smooth(q))
        if shade < 0.999:
            im = Image.fromarray((np.asarray(im).astype(np.float32) * shade).astype(np.uint8))
        canvas.paste(im, ((CW - w) // 2, 0))
        return canvas
    f = get_field(kind, seed)
    pp = smooth(p) * 1.14
    A, B = np.asarray(a), np.asarray(b)
    out = A.copy()
    shadow = ((f >= pp) & (f < pp + 0.035))[..., None]
    out = np.where(shadow, (A * 0.78).astype(np.uint8), out)
    band = ((f >= pp - 0.022) & (f < pp))[..., None]
    out = np.where(band, np.array(PAPER, dtype=np.uint8)[None, None, :], out)
    newm = (f < pp - 0.022)[..., None]
    out = np.where(newm, B, out)
    return Image.fromarray(out)


def render_frame(n):
    t = n / STOP_FPS
    i = 0
    for k, sg in enumerate(SEGS):
        if sg.start <= t + 1e-9:
            i = k
    sg = SEGS[i]
    if i > 0 and t < sg.start + TR:
        prev = SEGS[i - 1]
        p = (t - sg.start) / TR
        a = prev.render(t - prev.start, n * 7 + 1)
        b = sg.render(t - sg.start, n * 7 + 2)
        accent = tuple(int(c) for c in THEMES[THEME_ORDER[0]]["accent"])
        img = transition_frame(sg.trans_in, a, b, p, 1000 + i, accent)
    else:
        img = sg.render(t - sg.start, n * 7 + 3)
    return img.tobytes()


# --------------------------------------------------------------------------- #
# 6) AUDIO: SFX + backsound (sintesis, bisa di-override via assets/)
# --------------------------------------------------------------------------- #
def tax(d):
    return np.arange(int(d * SR), dtype=np.float64) / SR


def white(d):
    return NR.normal(0, 1, int(d * SR))


def lp_sweep(x, f0, f1):
    n = len(x)
    a = (1 - np.exp(-2 * np.pi * np.geomspace(f0, f1, n) / SR)).tolist()
    xl, y, s = x.tolist(), [], 0.0
    for i in range(n):
        s += a[i] * (xl[i] - s)
        y.append(s)
    return np.array(y)


def hp(x, k=200):
    return x - np.convolve(x, np.ones(k) / k, "same")


def norm(x, peak=1.0):
    m = np.max(np.abs(x)) or 1.0
    return x / m * peak


def fade(x, a=0.004, b=0.02):
    n = len(x)
    ia, ib = max(1, int(a * SR)), max(1, int(b * SR))
    x = x.copy()
    x[:ia] *= np.linspace(0, 1, ia)
    x[-ib:] *= np.linspace(1, 0, ib)
    return x


def s_whoosh(d=0.6, up=True):
    t = tax(d)
    x = lp_sweep(white(d), 300, 7500) if up else lp_sweep(white(d), 7500, 300)
    return fade(norm(hp(x) * np.sin(np.pi * (t / d) ** 0.8) ** 2, 0.85))


def s_pop(f0=900, f1=240, d=0.16):
    t = tax(d)
    ph = 2 * np.pi * np.cumsum(f1 + (f0 - f1) * np.exp(-t * 42)) / SR
    x = np.sin(ph) * np.exp(-t * 26) + 0.25 * white(d) * np.exp(-t * 380)
    return fade(norm(x, 0.9), 0.001, 0.02)


def s_pop2(d=0.5):
    a = s_pop(1250, 330, 0.2)
    sp = hp(white(d)) * np.exp(-tax(d) * 9) * 0.12
    out = np.zeros(int(d * SR))
    out[:len(a)] += a
    out += sp
    for k, f in enumerate([2093, 2637, 3136]):
        t0 = int(0.05 * k * SR)
        t = tax(d - 0.05 * k)
        out[t0:t0 + len(t)] += 0.16 * np.sin(2 * np.pi * f * t) * np.exp(-t * 12)
    return fade(norm(out, 0.85))


def s_rip(d=0.55):
    n = hp(white(d), 120)
    env = np.convolve(np.abs(white(d)), np.ones(500) / 500, "same")
    env = (env / env.max()) ** 1.4
    return fade(norm(n * env * np.linspace(1, 0.25, len(n)), 0.85), 0.003, 0.05)


def s_paper(d=0.28):
    return fade(norm(s_rip(d), 0.5), 0.003, 0.05)


def s_iris(d=0.6):
    a = s_whoosh(d, False)
    p = s_pop(700, 200, 0.18)
    a[int(0.38 * SR):int(0.38 * SR) + len(p)] += 0.7 * p[:len(a) - int(0.38 * SR)]
    return norm(a, 0.85)


def s_flip(d=0.42):
    a = s_whoosh(d, True)
    p = s_pop(1000, 300, 0.12)
    k = int(0.2 * SR)
    a[k:k + len(p)] += 0.6 * p[:len(a) - k]
    return norm(a, 0.85)


def bell(f, d, amp=0.3):
    t = tax(d)
    x = np.zeros_like(t)
    for i, (m, a) in enumerate([(1, 1), (2.76, 0.4), (5.4, 0.2), (8.9, 0.1)]):
        x += a * np.sin(2 * np.pi * f * m * t) * np.exp(-t * (3 + 2.2 * i))
    return amp * x


def s_hook(d=1.6):
    t = tax(d)
    out = np.zeros(len(t))
    out += 0.9 * np.sin(2 * np.pi * np.cumsum(55 + 90 * np.exp(-t * 30)) / SR) * np.exp(-t * 5)
    out += 0.5 * lp_sweep(white(d), 6000, 800) * np.exp(-t * 16)
    for k, f in enumerate([659.3, 783.99, 987.77, 1318.5]):
        i = int((0.04 + 0.07 * k) * SR)
        b = bell(f, d - 0.3, 0.22)
        out[i:i + len(b)] += b[:len(out) - i]
    return fade(norm(out, 0.95), 0.001, 0.12)


def s_closing(d=2.6):
    t = tax(d)
    out = 0.6 * np.sin(2 * np.pi * 80 * t) * np.exp(-t * 7)
    for k, f in enumerate([1046.5, 1318.5, 1568.0, 2093.0]):
        i = int(0.13 * k * SR)
        b = bell(f, d - 0.13 * k, 0.28)
        out[i:i + len(b)] += b[:len(out) - i]
    out += hp(white(d)) * np.exp(-t * 3) * 0.05
    return fade(norm(out, 0.9), 0.001, 0.25)


SYNTH = {"hook": s_hook, "pop": s_pop, "pop2": s_pop2, "paper": s_paper, "whoosh": s_whoosh, "rip": s_rip,
         "iris": s_iris, "flip": s_flip, "closing": s_closing}


def load_audio(path):
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def find_asset(folder, stem):
    d = Path(folder)
    if d.exists():
        for ext in (".wav", ".mp3", ".ogg", ".m4a", ".flac"):
            f = d / (stem + ext)
            if f.exists():
                return f
    return None


def midi(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def put(out, t0, sig):
    i = int(t0 * SR)
    if i >= len(out):
        return
    e = min(len(out), i + len(sig))
    out[i:e] += sig[:e - i]


def synth_bgm(dur, mood):
    P_ = MOODS[mood]
    beat = 60.0 / P_["bpm"]
    bar = beat * 4
    out = np.zeros(int(dur * SR))
    rnd = random.Random(R.random())
    for b in range(int(dur / bar) + 2):
        t0 = b * bar
        off, ivs = P_["prog"][b % len(P_["prog"])]
        root = P_["key"] + off
        while root > 60:
            root -= 12
        while root < 45:
            root += 12
        # pad
        for iv in ivs:
            f = midi(root + iv + 12)
            t = tax(bar + 0.35)
            env = np.minimum(1, t / 0.09) * np.minimum(1, (bar + 0.35 - t) / 0.3) * np.exp(-t * 0.35)
            put(out, t0, 0.05 * env * (np.sin(2 * np.pi * f * t) + 0.5 * np.sin(2 * np.pi * f * 1.004 * t)
                                       + 0.2 * np.sin(2 * np.pi * 2 * f * t)))
        # bass
        for bt in (0, 2, 2.75 if mood == "ceria" else 3.5):
            t = tax(beat * 1.6)
            put(out, t0 + bt * beat, 0.32 * np.sin(2 * np.pi * midi(root - 12) * t) * np.exp(-t * 3.2))
        # drum
        for bt in P_["kick"]:
            t = tax(0.35)
            put(out, t0 + bt * beat, 0.5 * np.sin(2 * np.pi * np.cumsum(45 + 95 * np.exp(-t * 35)) / SR) * np.exp(-t * 9))
        for bt in (1, 3):
            t = tax(0.22)
            put(out, t0 + bt * beat, 0.15 * lp_sweep(white(0.22), 6000, 3000) * np.exp(-t * 18))
        for e in range(8):
            sw = P_["swing"] * beat / 2 if e % 2 else 0
            t = tax(0.07)
            put(out, t0 + e * beat / 2 + sw, hp(white(0.07), 40) * np.exp(-t * 65) * (0.05 + 0.03 * rnd.random()))
        # melodi pluck
        for e in range(8):
            if rnd.random() < 0.33:
                n = P_["key"] + 12 + rnd.choice(P_["scale"]) + rnd.choice([0, 12])
                t = tax(0.5)
                sw = P_["swing"] * beat / 2 if e % 2 else 0
                put(out, t0 + e * beat / 2 + sw,
                    0.10 * (np.sin(2 * np.pi * midi(n) * t) + 0.3 * np.sin(4 * np.pi * midi(n) * t)) * np.exp(-t * 7))
    out = np.convolve(out, np.ones(4) / 4, "same")
    for _ in range(int(dur * 7)):  # vinyl crackle
        out[rnd.randrange(len(out))] += rnd.uniform(-0.05, 0.05)
    out = norm(out, 0.9)
    return fade(out, 1.0, 2.0)


def build_audio(total, sfx_events, mood, args, wav_path):
    N = int((total + 1.0) * SR)
    L, Rr = np.zeros(N), np.zeros(N)
    sfx_dir = Path(args.assets) / "sfx"
    cache = {}
    duck = np.ones(N)
    for t, kind, gain in sfx_events:
        if kind not in cache:
            f = find_asset(sfx_dir, kind)
            cache[kind] = norm(load_audio(f), 0.9) if f else SYNTH[kind]()
        x = cache[kind] * gain
        pan = R.uniform(-0.35, 0.35)
        i = int(t * SR)
        e = min(N, i + len(x))
        if i < N:
            L[i:e] += x[:e - i] * math.cos((pan + 1) * math.pi / 4)
            Rr[i:e] += x[:e - i] * math.sin((pan + 1) * math.pi / 4)
            d0, d1 = max(0, i - int(0.02 * SR)), min(N, i + int(0.45 * SR))
            duck[d0:d1] = np.minimum(duck[d0:d1], 0.5)
    duck = np.convolve(duck, np.ones(int(0.06 * SR)) / int(0.06 * SR), "same")
    music = None
    mdir = Path(args.assets) / "music"
    files = [f for f in mdir.iterdir() if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg", ".flac")] if mdir.exists() else []
    if files:
        f = R.choice(files)
        log(f"  backsound: {f.name}")
        m = load_audio(f)
        m = np.tile(m, int(N / len(m)) + 1)[:N]
        music = fade(norm(m, 0.9), 1.0, 2.0)
    else:
        log(f"  backsound: sintesis mood '{mood}'")
        music = np.zeros(N)
        b = synth_bgm(total + 1.0, mood)
        music[:len(b)] = b[:N]
    music = music * 0.30 * duck
    L, Rr = L * 0.95 + music, Rr * 0.95 + music
    mix = np.stack([L, Rr], 1)
    mix = np.tanh(mix * 1.15) / np.tanh(1.15)
    mix = mix / max(1e-9, np.max(np.abs(mix))) * 0.9
    pcm = (mix * 32767).astype("<i2")
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


# --------------------------------------------------------------------------- #
# 7) Encode
# --------------------------------------------------------------------------- #
def encode(total_frames, wav, out_mp4, args):
    codec = args.codec
    venc = ["-c:v", codec]
    if codec == "libx264":
        venc += ["-preset", args.preset, "-crf", str(args.crf), "-profile:v", "high"]
    elif codec == "libx265":
        venc += ["-preset", args.preset, "-crf", str(args.crf + 3), "-tag:v", "hvc1"]
    else:  # nvenc / videotoolbox / dll
        venc += ["-b:v", "45M"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{CW}x{CH}",
           "-framerate", str(STOP_FPS), "-i", "-", "-i", str(wav),
           "-vf", f"fps={FPS_OUT},scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
           *venc, "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "256k", "-ar", str(SR),
           "-movflags", "+faststart", "-shortest", str(out_mp4)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t0 = time.time()

    def write(b):
        try:
            proc.stdin.write(b)
        except BrokenPipeError:
            raise RuntimeError("ffmpeg berhenti lebih awal (cek codec/opsi di atas)")

    done = 0

    def tick():
        nonlocal done
        done += 1
        if done % 15 == 0 or done == total_frames:
            el = time.time() - t0
            eta = el / done * (total_frames - done)
            log(f"  frame {done}/{total_frames} ({done / total_frames:5.1%})  sisa ~{eta:4.0f}s")

    if args.workers <= 1 or os.name == "nt":
        for n in range(total_frames):
            write(render_frame(n))
            tick()
    else:
        with mp.get_context("fork").Pool(args.workers) as pool:
            pend = collections.deque()
            for n in range(total_frames):
                pend.append(pool.apply_async(render_frame, (n,)))
                if len(pend) >= args.workers * 2:
                    write(pend.popleft().get())
                    tick()
            while pend:
                write(pend.popleft().get())
                tick()
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg gagal menyelesaikan encode")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    global S, CW, CH, STOP_FPS, NR, R, VIG, ARGS
    ap = argparse.ArgumentParser(prog="paper-collage", description="Video paper-collage 9:16 4K dari satu topik.")
    ap.add_argument("topic", nargs="*", help="topik video")
    ap.add_argument("--duration", type=float, default=30, help="target durasi detik (default 30)")
    ap.add_argument("--lang", default="id", help="bahasa teks: id, en, ms, jv ... (default id)")
    ap.add_argument("--quality", choices=list(QUALITY), default="4k", help="4k=2160x3840, 1080p, preview=540x960")
    ap.add_argument("--out", default="output", help="folder output")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--storyboard", help="pakai storyboard.json yang sudah ada (lewati API)")
    ap.add_argument("--mood", choices=list(MOODS), help="paksa mood backsound")
    ap.add_argument("--theme", choices=list(THEMES), help="paksa tema warna awal")
    ap.add_argument("--assets", default=str(Path(__file__).parent / "assets"), help="folder assets (images/sfx/music)")
    ap.add_argument("--no-web-images", action="store_true", help="jangan ambil foto dari Bing Image Search")
    ap.add_argument("--font-title")
    ap.add_argument("--font-body")
    ap.add_argument("--stop-fps", type=int, choices=[10, 15, 30], default=15, help="frame rate stop-motion")
    ap.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)))
    ap.add_argument("--codec", default="libx264", help="libx264 | libx265 | h264_nvenc | h264_videotoolbox")
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--crf", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true", help="hanya storyboard + audio, tanpa video")
    args = ap.parse_args()
    ARGS = args

    topic = " ".join(args.topic).strip()
    if not topic and not args.storyboard:
        ap.error("isi TOPIK, contoh: paper_collage.py \"Kenapa langit biru\"")

    seed = args.seed if args.seed is not None else random.randrange(1, 10 ** 6)
    R, NR = random.Random(seed), np.random.default_rng(seed)
    S = QUALITY[args.quality]
    CW, CH = int(BASE_W * S) // 2 * 2, int(BASE_H * S) // 2 * 2
    STOP_FPS = args.stop_fps
    VIG = make_vignette()

    # --- otak ---
    if args.storyboard:
        raw = json.loads(Path(args.storyboard).read_text(encoding="utf-8"))
        topic = topic or raw.get("topic") or raw.get("title", "video")
        n_scenes = len(raw.get("scenes", []))
        sb = normalize(raw, topic, n_scenes, args)
        outdir = Path(args.storyboard).parent
        log(f"[1/4] Storyboard dari file: {args.storyboard}")
    else:
        n_scenes = int(clamp(round((args.duration - 7.5) / 4.8), 2, 8))
        log(f"[1/4] Otak: minta storyboard ke {API_BASE} (model {API_MODEL}, {n_scenes} scene)")
        sb = normalize(ask_brain(topic, n_scenes, args.lang), topic, n_scenes, args)
        outdir = Path(args.out) / slugify(topic)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "storyboard.json").write_text(json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"      judul: {sb['title']} | mood: {sb['mood']} | tema: {sb['theme']} | scene: {len(sb['scenes'])}")

    # --- visual ---
    log(f"[2/4] Menyusun {len(sb['scenes']) + 2} segmen kertas ({CW}x{CH})")
    credits = []
    total, sfx_events = build_timeline(sb, args, credits)
    if credits:
        (outdir / "credits.txt").write_text("\n".join(credits), encoding="utf-8")
    log(f"      total durasi {total:.1f}s, {len(sfx_events)} event SFX")
    log("      segmen: " + ", ".join(f"{sg.name}@{sg.start:.1f}s" for sg in SEGS))

    # --- audio ---
    log("[3/4] Audio: SFX hook/transisi/pop-up/closing + backsound")
    wav = outdir / "audio_mix.wav"
    build_audio(total, sfx_events, sb["mood"], args, wav)
    if args.dry_run:
        log(f"Dry-run selesai. Storyboard & audio di {outdir}")
        return

    # --- render + encode ---
    out_mp4 = outdir / f"{slugify(topic)}_{CW}x{CH}.mp4"
    total_frames = int(math.ceil(total * STOP_FPS))
    log(f"[4/4] Render {total_frames} frame stop-motion @ {STOP_FPS}fps -> encode {FPS_OUT}fps ({args.workers} worker)")
    encode(total_frames, wav, out_mp4, args)
    log(f"\nSELESAI: {out_mp4}")
    print(out_mp4)


if __name__ == "__main__":
    main()
