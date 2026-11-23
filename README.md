# Paper Collage Studio — versi Streamlit

## Deploy ke Streamlit Community Cloud
1. Upload isi folder ini ke repo GitHub (app.py, paper_collage.py, requirements.txt, .streamlit/, assets/).
2. https://share.streamlit.io → *New app* → pilih repo → Main file: `app.py` → Deploy.
3. Selesai. ffmpeg ikut terpasang lewat paket `imageio-ffmpeg` (tidak perlu packages.txt).

## Jalankan di komputer sendiri
    pip install -r requirements.txt
    streamlit run app.py
Untuk membuka 2K/4K: set `PCS_MAX_RES=4K` (Windows: `set PCS_MAX_RES=4K`) sebelum `streamlit run`.

## Gaya visual
Pilih lewat dropdown **Gaya visual** (Streamlit), option menu **VISUAL** (desktop), atau `--style` (CLI):

| Gaya | Tampilan |
|---|---|
| `Paper Collage` (default) | kolase kertas sobek, stiker, tape, tekstur kertas |
| `Motion Graphics Flat` | warna solid bold, bentuk geometris, teks besar, animasi wipe/slide/pop, caption pil di bawah |

Keduanya memakai pipeline yang sama (naskah AI, foto Bing, voice-over, SFX, backsound, encoder), jadi durasi, scene, dan resolusi berlaku sama.

## Durasi video
- Kolom **Durasi (detik)**: isi angka untuk menentukan panjang video, atau `0` untuk otomatis (mengikuti panjang voice-over).
- Kalau diisi, durasi tiap scene discale proporsional supaya total video pas dengan angka itu. Target lebih panjang = tiap scene ditahan lebih lama; target lebih pendek = tiap scene diperpendek.
- Batas minimum: video tidak bisa lebih pendek dari voice-over + animasi. Kalau target tidak tercapai, dipakai durasi minimum dan muncul peringatan di log.
- Naskah AI juga menyesuaikan: untuk durasi pendek, `narration` tiap scene diminta lebih sedikit kata.
- Versi CLI: `python paper_collage.py "TOPIK" --headless --duration 30` (angka detik; tanpa flag = otomatis).

## Performa
- TTS dijalankan paralel (10 potongan sekaligus), gambar Bing diambil di thread terpisah saat TTS berjalan, dan filter audio memakai moving-average O(N).
- Render memakai cache latar per langkah zoom, jadi kamera zoom tidak lagi me-resize frame penuh tiap frame.
- Patokan (CPU biasa, HD, voice-over + foto Bing): 8 scene / 45 detik ≈ 40 detik; 5 scene / 30 detik ≈ 33 detik.
- Di Streamlit Community Cloud (1 CPU, tanpa GPU) perkiraan 3–5× lebih lama. Untuk mempercepat: kurangi **Scene isi** (3–5), pendekkan **Durasi**, pakai **HD**, dan matikan **Kamera zoom halus** kalau buru-buru.

## Batasan di cloud gratis
- CPU/RAM bersama dan tanpa GPU: pakai HD/FHD, video 30–60 detik. 4K tidak realistis.
- Render berjalan selama sesi browser terbuka; jangan tutup tab / ubah isian saat berjalan.
- Hasil tidak disimpan permanen: unduh MP4 setelah selesai.
- API naskah menghitung limit per IP; IP server cloud bisa dipakai bersama banyak aplikasi, jadi sesekali kena 429/503 (otomatis di-retry).
- Voice-over (edge-tts) dan foto dari Bing Image Search (tanpa API key) butuh internet keluar; bila diblokir, video tetap jadi tanpa itu.
- Font bawaan: Anton, Archivo Black, Poppins (lisensi OFL). Emoji diambil dari OpenMoji (CC BY-SA 4.0), kreditnya otomatis ada di file .txt.
