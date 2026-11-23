# -*- coding: utf-8 -*-
"""Paper Collage Studio - versi Streamlit (Streamlit Community Cloud / self-host)."""
import os
import re
import tempfile
import time
import traceback

import streamlit as st

import paper_collage as pc

st.set_page_config(page_title="Paper Collage Studio", page_icon="✂️", layout="wide")

# Streamlit Cloud: resource kecil -> HD/FHD saja. Self-host: set env PCS_MAX_RES=4K untuk membuka 2K/4K.
MAX_RES = os.environ.get("PCS_MAX_RES", "FHD").upper()
RES_OPTIONS = {"FHD": ["HD", "FHD"], "2K": ["HD", "FHD", "2K"], "4K": ["HD", "FHD", "2K", "4K"]}.get(MAX_RES, ["HD", "FHD"])

st.markdown(
    """
<style>
.block-container{padding-top:1.6rem;max-width:1200px}
h1.pcs{font-family:Impact,'Arial Black',sans-serif;letter-spacing:1px;color:#ffb703;margin:0}
.pcs-sub{color:#9c8f84;margin:0 0 1rem 0}
.cmd{font-family:Consolas,monospace;color:#ffb703;font-weight:700;font-size:1.1rem;margin-bottom:-0.4rem}
div[data-testid="stForm"]{border:2px solid #ffb703;border-radius:16px;background:#0d0b0a}
.stButton>button,.stFormSubmitButton>button{background:#fb5607;color:#fff;border:0;font-weight:800;border-radius:12px}
</style>
<h1 class="pcs">✂ PAPER COLLAGE STUDIO</h1>
<p class="pcs-sub">Topik → naskah AI → video 9:16 kolase kertas + SFX + backsound + pop-up</p>
""",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("ℹ️ Info")
    st.caption(
        f"Otak naskah: `{pc.API_BASE}` (model `{pc.MODEL}`, tanpa key).\n\n"
        "Server cloud berbagi CPU & RAM, jadi resolusi dibatasi. Untuk 4K pakai versi desktop (.exe) "
        "atau jalankan app ini di komputer sendiri dengan `PCS_MAX_RES=4K`."
    )
    st.caption("Jangan ubah isian atau tutup tab saat render berjalan, prosesnya akan berhenti.")

with st.form("main"):
    st.markdown('<div class="cmd">/paper-collage</div>', unsafe_allow_html=True)
    topic_raw = st.text_input("TOPIK", placeholder="mis: Sejarah kopi di Nusantara", label_visibility="collapsed")
    style_name = st.selectbox("Gaya visual", ["Paper Collage", "Motion Graphics Flat"],
                              help="Paper Collage = kolase kertas sobek. Motion Graphics Flat = bentuk datar, "
                                   "wipe & slide, cocok buat konten modern/korporat.")
    c1, c2, c3, c4, c5 = st.columns(5)
    res = c1.selectbox("Kualitas", RES_OPTIONS, index=RES_OPTIONS.index("HD"))
    fps = c2.selectbox("FPS", [24, 30], index=0)
    scenes = c3.slider("Scene isi", 3, 8, 5)
    dur = c4.number_input("Durasi (detik)", min_value=0, max_value=300, value=0, step=5,
                          help="0 = otomatis (ikuti panjang voice-over). Kalau diisi, durasi tiap scene "
                               "discale supaya total video mendekati angka ini.")
    lang = c5.selectbox("Bahasa", ["Indonesia", "English"])
    d1, d2, d3, d4 = st.columns(4)
    mood = d1.selectbox("Mood musik", ["auto"] + list(pc.MOODS))
    voice_on = d2.checkbox("Voice-over", value=True)
    voice = d3.selectbox("Suara", pc.VOICES["id"] + pc.VOICES["en"])
    use_bing = d4.checkbox("Foto Bing", value=True, help="Gambar diambil dari Bing Image Search (tanpa API key)")
    e1, e2, e3 = st.columns(3)
    mus_vol = e1.slider("Volume backsound", 0.0, 1.2, 0.5)
    sfx_vol = e2.slider("Volume SFX", 0.0, 1.2, 0.9)
    cam = e3.checkbox("Kamera zoom halus", value=True)
    music_file = st.file_uploader("Backsound sendiri (opsional, kosong = otomatis)", type=["mp3", "wav", "m4a", "ogg"])
    go = st.form_submit_button("▶  GENERATE VIDEO", use_container_width=True)

if go:
    topic = re.sub(r"^/?paper-collage\s*", "", (topic_raw or "").strip(), flags=re.I).strip().strip('"')
    if not topic:
        st.warning("Isi topik dulu.")
        st.stop()
    st.session_state.pop("result", None)
    code = "id" if lang == "Indonesia" else "en"
    if code == "id" and voice.startswith("en-"):
        voice = pc.VOICES["id"][0]
    if code == "en" and voice.startswith("id-"):
        voice = pc.VOICES["en"][0]
    work = tempfile.mkdtemp(prefix="pcs_web_")
    music_path = ""
    if music_file is not None:
        music_path = os.path.join(work, "music" + os.path.splitext(music_file.name)[1])
        with open(music_path, "wb") as f:
            f.write(music_file.getbuffer())
    settings = pc.Settings(
        topic=topic, outdir=work, res=res, fps=int(fps), scenes=int(scenes), lang=code, mood=mood,
        voice_on=voice_on, voice=voice, music_path=music_path, music_vol=float(mus_vol), sfx_vol=float(sfx_vol),
        duration=float(dur), style="flat" if style_name.startswith("Motion") else "paper",
        use_bing=use_bing, gpu=False, camera=cam,
    )

    left, right = st.columns([1, 2])
    prev_slot = left.empty()
    bar = right.progress(0.0, text="Mulai ...")
    log_slot = right.empty()
    lines = []

    class CB:
        def log(self, m):
            lines.append(f"[{time.strftime('%H:%M:%S')}] {m}")
            log_slot.code("\n".join(lines[-10:]), language="text")

        def progress(self, f, s):
            bar.progress(min(max(f, 0.0), 1.0), text=s)

        def preview(self, im):
            prev_slot.image(im, width=270)

        def script(self, s):
            st.session_state["script"] = s

    try:
        out = pc.Pipeline(settings, CB()).run()
        with open(out, "rb") as f:
            video = f.read()
        txt_path = out[:-4] + ".txt"
        txt = open(txt_path, "rb").read() if os.path.exists(txt_path) else b""
        st.session_state["result"] = {"video": video, "txt": txt, "name": os.path.basename(out)}
    except Exception:
        st.error("Terjadi kesalahan saat membuat video.")
        st.code(traceback.format_exc())
    finally:
        try:
            for fn in os.listdir(work):
                os.remove(os.path.join(work, fn))
            os.rmdir(work)
        except Exception:
            pass

res_state = st.session_state.get("result")
if res_state:
    st.success("Selesai!")
    a, b = st.columns([1, 2])
    with a:
        st.video(res_state["video"])
    with b:
        st.download_button("⬇️ Unduh MP4", res_state["video"], file_name=res_state["name"], mime="video/mp4",
                           use_container_width=True)
        if res_state["txt"]:
            st.download_button("⬇️ Unduh naskah & hashtag (.txt)", res_state["txt"],
                               file_name=res_state["name"][:-4] + ".txt", mime="text/plain", use_container_width=True)
        sc = st.session_state.get("script")
        if sc:
            with st.expander("Lihat naskah", expanded=True):
                st.markdown(f"**{sc['title']}**  \nHOOK: {sc['hook']['text']}")
                for i, s_ in enumerate(sc["scenes"], 1):
                    st.markdown(f"{i}. **{s_['headline']}** {s_['emoji']} — {s_['narration']}  \n"
                                f"   pop-up: {' | '.join(s_['popups'])}")
                st.markdown(f"CLOSING: {sc['closing']['text']}  \n" + " ".join("#" + h for h in sc["hashtags"]))
