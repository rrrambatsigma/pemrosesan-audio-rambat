"""Pilot transkripsi ASR untuk file yang dipersoalkan pada QC audio.

Mengambil potongan tengah file (bukan intro/outro), mendeteksi bahasa otomatis,
lalu menyimpan transkrip + ringkasan metrik keterbacaan.

Contoh:
    python scripts/asr_pilot.py --ids compsci-databases-0186 --minutes 1
    python scripts/asr_pilot.py --ids A B C --minutes 5
    python scripts/asr_pilot.py --from-list data/reports/dnsmos_uji_dengar.csv --layak 1
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import soundfile as sf

from audio_pipeline.config import REPORTS_DIR, ROOT, SAMPLE_RATE, TMP_DIR, setup_console
from audio_pipeline.quality import _resample, read_audio

_DLL_HANDLES: list = []


def prepare_cuda_dlls() -> None:
    """Windows: ctranslate2 butuh cublas/cudnn dari paket torch (tidak ada di PATH)."""
    if os.name != "nt":
        return
    try:
        import torch
    except Exception:  # noqa: BLE001
        return
    lib = Path(torch.__file__).resolve().parent / "lib"
    if not lib.is_dir():
        return
    os.environ["PATH"] = f"{lib}{os.pathsep}{os.environ.get('PATH', '')}"
    try:
        _DLL_HANDLES.append(os.add_dll_directory(str(lib)))
    except (AttributeError, OSError):
        pass

QUALITY = REPORTS_DIR / "audio_quality.csv"
LISTEN = REPORTS_DIR / "dnsmos_uji_dengar.csv"
DEFAULT_OUT = REPORTS_DIR / "asr_pilot.csv"
DEFAULT_TDIR = ROOT / "data" / "transcripts" / "pilot"


def middle_audio(path: Path, minutes: float) -> tuple[np.ndarray, float]:
    """Ambil potongan tengah file (detik) dan resample ke 16 kHz."""
    info = sf.info(str(path))
    sr = int(info.samplerate)
    dur = info.frames / sr if sr else 0.0
    need = int(minutes * 60)
    if dur <= need:
        y, sr2 = read_audio(path)
        return _resample(y, sr2), dur
    start = max(0.0, dur / 2 - need / 2)
    with sf.SoundFile(str(path)) as f:
        f.seek(int(start * sr))
        block = f.read(int(need * sr), dtype="float32", always_2d=True)
    return _resample(block.mean(axis=1), sr), float(need)


def load_quality() -> dict[str, dict]:
    with open(QUALITY, newline="", encoding="utf-8-sig") as f:
        return {r["audio_id"]: r for r in csv.DictReader(f) if r.get("audio_id")}


def load_listen() -> dict[str, dict]:
    if not LISTEN.exists():
        return {}
    with open(LISTEN, newline="", encoding="utf-8-sig") as f:
        return {r["audio_id"]: r for r in csv.DictReader(f) if r.get("audio_id")}


def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", nargs="+", help="audio_id yang ditranskrip")
    ap.add_argument("--from-list", help="CSV berisi kolom audio_id (mis. dnsmos_uji_dengar.csv)")
    ap.add_argument("--layak", choices=["0", "1"], help="filter kolom layak pada --from-list")
    ap.add_argument("--minutes", type=float, default=5.0, help="durasi potongan tengah (menit)")
    ap.add_argument("--model", default="mobiuslabsgmbh/faster-whisper-large-v3-turbo")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="int8_float16")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--transcript-dir", default=str(DEFAULT_TDIR))
    a = ap.parse_args(argv)

    ids = list(a.ids or [])
    if a.from_list:
        with open(a.from_list, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if not r.get("audio_id"):
                    continue
                if a.layak is not None and str(r.get("layak", "")) != a.layak:
                    continue
                ids.append(r["audio_id"])
    ids = list(dict.fromkeys(ids))
    if not ids:
        sys.exit("Tidak ada audio_id. Pakai --ids atau --from-list.")

    quality = load_quality()
    listen = load_listen()
    missing = [i for i in ids if i not in quality]
    if missing:
        sys.exit(f"audio_id tidak ada di audio_quality.csv: {', '.join(missing)}")

    if a.device.startswith("cuda"):
        prepare_cuda_dlls()

    from faster_whisper import WhisperModel

    print(f"[asr] model {a.model} ({a.device}, {a.compute_type}) | {len(ids)} file "
          f"| potongan tengah {a.minutes:.0f} mnt")
    model = WhisperModel(a.model, device=a.device, compute_type=a.compute_type)

    tdir = Path(a.transcript_dir)
    if not tdir.is_absolute():
        tdir = ROOT / tdir
    tdir.mkdir(parents=True, exist_ok=True)
    tmp = TMP_DIR / "asr_pilot"
    tmp.mkdir(parents=True, exist_ok=True)

    out_path = Path(a.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    started = time.time()
    for i, aid in enumerate(ids, 1):
        row = quality[aid]
        path = ROOT / row["filepath"]
        y, proc_sec = middle_audio(path, a.minutes)
        wav = tmp / f"{aid}.wav"
        sf.write(str(wav), y, SAMPLE_RATE)

        t0 = time.time()
        segments, info = model.transcribe(
            str(wav), language=None, vad_filter=True, beam_size=5,
        )
        seg_list = list(segments)
        text = " ".join(s.text.strip() for s in seg_list).strip()
        words = len(text.split())
        logprob = float(np.mean([s.avg_logprob for s in seg_list])) if seg_list else None
        no_speech = float(np.mean([s.no_speech_prob for s in seg_list])) if seg_list else None
        comp = max((s.compression_ratio for s in seg_list), default=None)
        wpm = round(words / max(1e-9, proc_sec / 60), 1)

        txt = tdir / f"{aid}.txt"
        txt.write_text(
            "\n".join([
                f"audio_id   : {aid}",
                f"filepath   : {row['filepath']}",
                f"diproses   : {proc_sec / 60:.1f} menit (tengah file)",
                f"bahasa     : {info.language} (p={info.language_probability:.2f})",
                f"segmen     : {len(seg_list)} | kata: {words} | WPM: {wpm}",
                f"layak(manusia): {listen.get(aid, {}).get('layak', '')} "
                f"{listen.get(aid, {}).get('alasan', '')}",
                f"dnsmos_ovrl: {row['dnsmos_ovrl']} | snr: {row['snr_db']} | bw: {row['bandwidth_hz']}",
                "",
                text,
                "",
            ]),
            encoding="utf-8",
        )

        rows.append({
            "audio_id": aid,
            "durasi_file_menit": round(float(row["duration_s"]) / 60, 1),
            "diproses_menit": round(proc_sec / 60, 1),
            "bahasa": info.language,
            "prob_bahasa": round(info.language_probability, 3),
            "segmen": len(seg_list),
            "kata": words,
            "wpm": wpm,
            "avg_logprob": round(logprob, 3) if logprob is not None else "",
            "no_speech_mean": round(no_speech, 3) if no_speech is not None else "",
            "compression_max": round(comp, 2) if comp is not None else "",
            "repetisi": int(comp is not None and comp > 2.4),
            "dnsmos_ovrl": row["dnsmos_ovrl"],
            "layak_manusia": listen.get(aid, {}).get("layak", ""),
            "alasan": listen.get(aid, {}).get("alasan", ""),
            "transkrip_awal": text[:180].replace("\n", " "),
            "file_transkrip": str(txt.relative_to(ROOT)),
            "detik_proses": round(time.time() - t0, 1),
        })
        print(f"  [{i}/{len(ids)}] {aid}  {info.language} p={info.language_probability:.2f}  "
              f"kata={words} WPM={wpm}  ({time.time() - t0:.0f} dtk)")

        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nSelesai {len(rows)} file dalam {time.time() - started:.0f} dtk.")
    print(f"Ringkasan  : {out_path.relative_to(ROOT)}")
    print(f"Transkrip  : {tdir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
