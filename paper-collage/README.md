# /paper-collage [TOPIK]

Satu perintah → video vertikal **9:16, 2160×3840 (4K), 30 fps** bergaya paper-collage stop-motion,
lengkap dengan **SFX hook, SFX closing, SFX tiap transisi, SFX pop-up text/image, dan backsound**.

```
python3 paper_collage.py "Kenapa langit berwarna biru"
```

## Cara kerja

| Tahap | Apa yang terjadi |
|---|---|
| 1. Otak | Memanggil `https://api.aiapifreekey.com/v1/chat/completions` (model `free-ai`, tanpa key) untuk menyusun storyboard JSON: hook, scene, teks pop-up, query gambar, mood musik, transisi, closing. Auto-retry untuk 429/503. |
| 2. Visual | Dirender prosedural: kertas sobek, tape, kartu buku tulis, polaroid, burst sticker, efek "boil" stop-motion (15 fps, diduplikasi ke 30 fps). |
| 3. Audio | SFX dan backsound disintesis sendiri (tanpa lisensi). Backsound otomatis ducking saat SFX berbunyi. |
| 4. Encode | ffmpeg → H.264 High, BT.709, AAC 256 kbps, loudness −14 LUFS (standar TikTok/Reels/Shorts). |

Struktur video: **Hook (±4 dtk) → N scene (heading + catatan + pop-up) → Closing + CTA**.
Tiap pergantian segmen memakai transisi sobek: `slide`, `tear`, `iris`, `flip`, masing-masing dengan SFX sendiri.

## Instalasi

```
pip install -r requirements.txt     # pillow, numpy
# butuh ffmpeg di PATH:  brew install ffmpeg  |  sudo apt install ffmpeg  |  winget install ffmpeg
```

## Opsi

```
python3 paper_collage.py "TOPIK" [opsi]

--duration 30          target durasi (detik); menentukan jumlah scene
--lang id              id | en | ms | jv ...
--quality 4k           4k (2160x3840) | 1080p | preview (540x960, cepat untuk cek)
--mood santai          ceria | santai | dramatis | misteri | inspiratif  (default: pilihan AI)
--theme mint           kraft | mint | peach | sky | lilac | mustard
--storyboard FILE      pakai storyboard.json hasil edit sendiri (tanpa panggil API)
--no-web-images        jangan ambil foto dari Bing Image Search (pakai sticker saja)
--workers 4            jumlah proses render paralel
--codec libx264        libx264 | libx265 | h264_nvenc | h264_videotoolbox (encode GPU)
--preset medium --crf 16
--stop-fps 15          10 | 15 | 30 (kecepatan "boil" stop-motion)
--dry-run              hanya storyboard + audio
```

**Alur kerja yang disarankan:** jalankan sekali dengan `--quality preview` → edit
`output/<topik>/storyboard.json` kalau ada kalimat yang kurang pas → render final dengan
`--storyboard output/<topik>/storyboard.json --quality 4k`.

## Pakai aset sendiri (opsional)

Taruh file di `assets/`, otomatis dipakai menggantikan hasil sintesis/unduhan:

```
assets/sfx/       hook.wav  closing.wav  pop.wav  pop2.wav  paper.wav  whoosh.wav  rip.wav  iris.wav  flip.wav
assets/music/     lagu apa saja (mp3/wav/m4a), dipilih acak, di-loop, fade in/out
assets/images/    foto; dicocokkan dengan kata di nama file (mis. blue-sky.jpg untuk query "blue sky")
assets/fonts/     .ttf/.otf tambahan (Anton + Caveat/Patrick Hand paling cocok untuk gaya ini)
```

## Slash command (Claude Code)

File `.claude/commands/paper-collage.md` sudah disertakan. Buka Claude Code di folder ini lalu ketik:

```
/paper-collage Sejarah kopi di Nusantara
```

## Catatan penting

- **Privasi & limit API.** aiapifreekey.com adalah layanan publik: IP dan permintaan Anda tercatat, dan limitnya
  1 request/detik serta 10.000 request/hari per IP. Tool ini hanya mengirim topik (1 request per video).
  Jangan memasukkan data rahasia/pribadi ke topik.
- **Akurasi.** Model `free-ai` berukuran 35B dan bisa keliru soal fakta, angka, dan nama. Selalu baca
  `storyboard.json` sebelum merender final dan terbitkan.
- **Lisensi foto.** Foto diambil dari Bing Image Search (tanpa API key) langsung dari situs aslinya, jadi lisensinya
  mengikuti situs tersebut. Sumbernya dicatat di `credits.txt`; periksa sebelum dipublikasikan, atau pakai
  `--no-web-images` / `assets/images/`.
- **Waktu render.** Sebagai gambaran, 4K di satu core CPU ±8 detik per detik video (preset `ultrafast`);
  `--workers` dan encoder GPU (`--codec h264_nvenc`) mempercepat banyak. Font Latin yang dipakai otomatis dicari di sistem.
