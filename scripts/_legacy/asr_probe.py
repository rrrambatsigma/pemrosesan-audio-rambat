"""Transkripsi uji (probe) beberapa file: deteksi bahasa + kualitas transkrip.

Dipakai untuk memutuskan file yang diperdebatkan pada uji dengar QC, dan sebagai
pilot sebelum transkripsi penuh. Hanya membaca audio, tidak mengubah dataset.

Contoh:
    python scripts/asr_probe.py --ids teknik-control_robotics-0115 --seconds 300
    python scripts/asr_probe.py --ids a b c --model medium --seconds 300
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time

import _bootstrap  # noqa: F401
import numpy as np
import soundfile as sf
import torch

from audio_pipeline.config import ROOT, setup_console
from audio_pipeline.quality import AUDIO_FIELDS

OUT = ROOT / "data" / "reports" / "asr_probe.csv"
TXT_DIR = ROOT / "data" / "transcripts" / "probe"
SR = 16000
FIELDS = [
    "audio_id", "filepath", "model", "start_s", "analyzed_sec", "lang",
    "lang_prob", "segments", "words", "wpm", "avg_logprob", "avg_no_speech",
    "avg_compression", "verdict", "preview",
]


def read_middle(path, seconds: float) -> tuple[np.ndarray, float, float]:
    """Ambil potongan tengah file (hindari intro/outro bermusik)."""
    info = sf.info(str(path))
    sr = int(info.samplerate)
    dur = info.frames / sr if sr else 0.0
    take = min(float(seconds), dur)
    start = max(0.0, dur / 2 - take / 2)
    with sf.SoundFile(str(path)) as f:
        f.seek(int(start * sr))
        y = f.read(int(take * sr), dtype="float32", always_2d=True)
    mono = y.mean(axis=1) if y.size else np.zeros(0, dtype=np.float32)
    if sr != SR and mono.size:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(sr, SR)
        mono = resample_poly(mono, SR // g, sr // g).astype(np.float32)
    return mono, take, start


def verdict(row: dict) -> str:
    """Label ringkas untuk memudahkan pembacaan; bukan pengganti membaca teks."""
    problems = []
    if row["wpm"] < 20:
        problems.append("wpm_rendah")
    if row["avg_logprob"] < -1.0:
        problems.append("ragu")
    if row["avg_compression"] > 2.4:
        problems.append("repetitif")
    if row["words"] < 40:
        problems.append("sedikit_kata")
    return ";".join(problems) if problems else "wajar"


def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Probe ASR: bahasa + kualitas transkrip")
    ap.add_argument("--ids", nargs="+", required=True, help="audio_id (boleh beberapa)")
    ap.add_argument("--seconds", type=float, default=300.0)
    ap.add_argument("--model", default="medium")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    args = ap.parse_args(argv)

    qc = list(csv.DictReader(open(ROOT / "data" / "reports" / "audio_quality.csv",
                                  encoding="utf-8-sig")))
    by_id = {r["audio_id"]: r for r in qc}
    missing = [i for i in args.ids if i not in by_id]
    if missing:
        sys.exit(f"audio_id tidak ditemukan: {', '.join(missing)}")

    device = ("cuda" if torch.cuda.is_available() else "cpu") if args.device == "auto" else args.device
    compute = "int8_float16" if device == "cuda" else "int8"
    print(f"[asr] model={args.model} device={device} compute={compute} "
          f"| {len(args.ids)} file × {args.seconds:.0f} dtk")

    from faster_whisper import WhisperModel

    started = time.time()
    model = WhisperModel(args.model, device=device, compute_type=compute)
    print(f"[asr] model siap dalam {time.time() - started:.1f} dtk")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    TXT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []

    for i, audio_id in enumerate(args.ids, 1):
        r = by_id[audio_id]
        path = ROOT / r["filepath"]
        t0 = time.time()
        try:
            y, took, start = read_middle(path, args.seconds)
            segments, info = model.transcribe(
                y,
                language=None,
                task="transcribe",
                vad_filter=True,
                beam_size=5,
                condition_on_previous_text=False,
            )
            segs = list(segments)
            text = " ".join(s.text.strip() for s in segs).strip()
            words = len(text.split())
            avg_lp = float(np.mean([s.avg_logprob for s in segs])) if segs else -9.0
            avg_ns = float(np.mean([s.no_speech_prob for s in segs])) if segs else 1.0
            avg_cr = float(np.mean([s.compression_ratio for s in segs])) if segs else 0.0
            row = {
                "audio_id": audio_id,
                "filepath": r["filepath"],
                "model": args.model,
                "start_s": round(start, 1),
                "analyzed_sec": round(took, 1),
                "lang": info.language,
                "lang_prob": round(float(info.language_probability), 3),
                "segments": len(segs),
                "words": words,
                "wpm": round(words / max(took / 60, 1e-6), 1),
                "avg_logprob": round(avg_lp, 3),
                "avg_no_speech": round(avg_ns, 3),
                "avg_compression": round(avg_cr, 3),
                "preview": text[:300].replace("\n", " "),
            }
            row["verdict"] = verdict(row)
            (TXT_DIR / f"{audio_id}.txt").write_text(text, encoding="utf-8")
            print(f"[{i}/{len(args.ids)}] {audio_id}")
            print(f"    lang={row['lang']} ({row['lang_prob']:.2f}) words={words} "
                  f"wpm={row['wpm']:.0f} logprob={avg_lp:.2f} {row['verdict']} "
                  f"({time.time() - t0:.0f} dtk)")
            print(f"    {text[:180]}")
        except Exception as e:  # noqa: BLE001
            row = {k: "" for k in FIELDS}
            row.update(audio_id=audio_id, filepath=r["filepath"], model=args.model,
                       verdict=f"ERROR {type(e).__name__}: {str(e)[:80]}")
            print(f"[{i}/{len(args.ids)}] {audio_id} GAGAL: {type(e).__name__}: {e}")
        rows.append(row)

    new = not OUT.exists()
    with open(OUT, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"\n[asr] {len(rows)} baris -> {OUT.relative_to(ROOT)}")
    print(f"[asr] transkrip  -> {TXT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
