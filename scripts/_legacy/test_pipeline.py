"""Tes pipeline 1 video: unduh -> audio 16 kHz mono -> transkripsi.

Contoh:
    python scripts/test_pipeline.py "https://www.youtube.com/watch?v=XXXX" --name sample01 --minutes 5
    python scripts/test_pipeline.py "D:\\rekaman\\kuliah.mp3" --name sample02 --lang id
Argumen --lang: id, en, atau auto.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401
from audio_pipeline.config import ROOT


def setup_path():
    env_dir = Path(sys.executable).parent
    extra = [r"C:\ffmpeg\bin", str(env_dir / "Scripts")]
    os.environ["PATH"] = ";".join(extra) + ";" + os.environ["PATH"]
    # Pakai DLL CUDA bawaan torch (cuBLAS/cuDNN) supaya CTranslate2 bisa memakai GPU.
    try:
        import torch
        lib = Path(torch.__file__).parent / "lib"
        if lib.exists():
            os.add_dll_directory(str(lib))
            os.environ["PATH"] = str(lib) + ";" + os.environ["PATH"]
    except Exception as e:  # noqa: BLE001
        print("Peringatan: torch tidak bisa dimuat:", e)


def run(cmd):
    print(">", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


def download(src, raw_dir, name):
    if Path(src).exists():
        print("Memakai file lokal:", src)
        return Path(src)
    out = raw_dir / f"{name}.%(ext)s"
    run(["yt-dlp", "-f", "bestaudio", "-x", "--audio-format", "wav",
         "--no-playlist", "-o", str(out), src])
    return raw_dir / f"{name}.wav"


def to_16k(src, dst, minutes):
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if minutes > 0:
        cmd += ["-t", str(int(minutes * 60))]
    cmd += ["-ac", "1", "-ar", "16000", str(dst)]
    run(cmd)


def transcribe(wav, model_name, lang, device, compute_type):
    from faster_whisper import WhisperModel
    print(f"Memuat model {model_name} ({device}, {compute_type}) ...")
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(wav),
        language=None if lang == "auto" else lang,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        beam_size=5,
        condition_on_previous_text=False,  # mengurangi halusinasi berulang
    )
    print(f"Bahasa terdeteksi: {info.language} (p={info.language_probability:.2f}), "
          f"durasi audio: {info.duration:.0f} dtk")
    return [
        dict(start=round(s.start, 2), end=round(s.end, 2), text=s.text.strip(),
             avg_logprob=round(s.avg_logprob, 3),
             no_speech_prob=round(s.no_speech_prob, 3),
             compression_ratio=round(s.compression_ratio, 2))
        for s in segments
    ], info.duration


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="URL video atau path file audio/video lokal")
    ap.add_argument("--name", default="sample01")
    ap.add_argument("--minutes", type=float, default=5, help="0 = seluruh durasi")
    ap.add_argument("--model", default="large-v3-turbo",
                    help="large-v3-turbo | medium | small")
    ap.add_argument("--lang", default="id", help="id | en | auto")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args()

    setup_path()
    root = Path(a.root)
    raw, aud, trn = root / "data/raw", root / "data/audio", root / "data/transcripts"
    for d in (raw, aud, trn):
        d.mkdir(parents=True, exist_ok=True)

    src = download(a.source, raw, a.name)
    wav = aud / f"{a.name}.wav"
    to_16k(src, wav, a.minutes)

    t0 = time.time()
    try:
        rows, dur = transcribe(wav, a.model, a.lang, "cuda", "int8_float16")
    except Exception as e:  # noqa: BLE001
        print(f"\nGPU gagal ({type(e).__name__}: {e})\n-> mencoba CPU (lebih lambat)\n")
        rows, dur = transcribe(wav, a.model, a.lang, "cpu", "int8")
    took = time.time() - t0

    out = trn / f"{a.name}.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (trn / f"{a.name}.txt").write_text(
        "\n".join(r["text"] for r in rows), encoding="utf-8")

    print(f"\nSelesai: {len(rows)} segmen, {took:.0f} dtk untuk {dur:.0f} dtk audio "
          f"(RTF {took / max(dur, 1):.2f})")
    print("Hasil:", out)
    print("\n--- 15 segmen pertama ---")
    for r in rows[:15]:
        print(f"[{r['start']:7.1f}-{r['end']:7.1f}] {r['text']}")


if __name__ == "__main__":
    main()
