"""CLI pemeriksaan kualitas audio dataset (klip 60 detik).

Contoh:
    python scripts/qc.py --stages basic --workers 4
    python scripts/qc.py --stages basic,score --label fisika --limit 20
    python scripts/qc.py --stages report

Output default:
    data/reports/audio_quality.csv
    data/reports/audio_quality_summary.csv
    data/reports/audio_quality_flagged.csv
    data/reports/audio_quality_reject.csv

Idempoten: file yang metrik dasarnya sudah ada dilewati, kecuali ``--overwrite``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import soundfile as sf

from audio_pipeline.config import ROOT, setup_console
from audio_pipeline.quality import (
    AUDIO_FIELDS,
    SAMPLE_RATE,
    THRESHOLDS,
    analyze_basic,
    evaluate,
    extract_sample,
    merge_thresholds,
    read_audio,
)

REPORTS = ROOT / "data" / "reports"
DEFAULT_OUT = REPORTS / "audio_quality.csv"
SUMMARY_OUT = REPORTS / "audio_quality_summary.csv"
FLAGGED_OUT = REPORTS / "audio_quality_flagged.csv"
REJECT_OUT = REPORTS / "audio_quality_reject.csv"
MANIFEST = ROOT / "data" / "manifest.csv"

STAGE_FIELD = {
    "basic": "duration_s",
    "score": "dnsmos_ovrl",
    "events": "music_ratio",
    "diar": "overlap_ratio",
    "asr": "asr_wpm",
}


# --------------------------------------------------------------------------- #
# util CSV
# --------------------------------------------------------------------------- #
def _key(path: str | Path) -> str:
    """Kunci konsisten: path relatif terhadap ROOT, posix, lowercase."""
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    try:
        p = p.resolve().relative_to(ROOT.resolve())
    except (ValueError, OSError):
        p = Path(path)
    return p.as_posix().lower()


NUMERIC_FIELDS = {
    "duration_s", "sr", "channels", "readable", "analyzed_sec", "peak_dbfs",
    "rms_dbfs", "lufs", "lufs_estimated", "clip_ratio", "clip_runs",
    "clip_longest", "dc_offset", "zero_energy", "silence_ratio",
    "channel_imbalance_db", "mono_cancel_db", "snr_db", "noise_floor_dbfs",
    "speech_level_dbfs", "speech_ratio_vad", "pause_count", "mean_pause_s",
    "longest_pause_s", "pause_ratio", "onset_rate_hz", "rolloff95_hz",
    "bandwidth_hz", "telephone_band", "rt60_proxy_s", "decay_db_per_s",
    "dnsmos_sig", "dnsmos_bak", "dnsmos_ovrl", "dnsmos_p808", "music_ratio",
    "applause_ratio", "laughter_ratio", "noise_ratio", "diar_speakers",
    "overlap_ratio", "asr_wpm", "asr_words", "quality_score",
}


def load_rows(out: Path) -> dict[str, dict]:
    if not out.exists():
        return {}
    with open(out, newline="", encoding="utf-8-sig") as f:
        rows = {}
        for r in csv.DictReader(f):
            row = dict(r)
            for col in NUMERIC_FIELDS:
                if col in row:
                    value = _num(row[col])
                    row[col] = None if value != value else value  # NaN -> None
            rows[_key(r["filepath"])] = row
        return rows


def save_rows(rows: dict[str, dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=AUDIO_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows.values():
            writer.writerow({k: row.get(k, "") for k in AUDIO_FIELDS})
    try:
        os.replace(tmp, out)
    except PermissionError:
        print(f"  [!] {out.name} sedang dibuka (Excel?). Data tertahan di {tmp.name}.")


def _num(value):
    if value in (None, "", "None"):
        return np.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


# --------------------------------------------------------------------------- #
# manifest & file discovery
# --------------------------------------------------------------------------- #
def load_manifest_map() -> dict[str, dict]:
    if not MANIFEST.exists():
        return {}
    with open(MANIFEST, newline="", encoding="utf-8-sig") as f:
        return {_key(r.get("filepath", "")): r for r in csv.DictReader(f) if r.get("filepath")}


def discover(scan_dir: Path, manifest: dict, include_sample: bool) -> list[Path]:
    files = sorted(p for p in scan_dir.rglob("*.wav") if p.is_file())
    if not include_sample:
        files = [p for p in files if not p.name.lower().startswith("sample01")]
    return files


def enrich(row: dict, path: Path, manifest: dict) -> None:
    rel = path.relative_to(ROOT).as_posix()
    m = manifest.get(_key(rel))
    if m:
        row["audio_id"] = m.get("clip_id", "")
        row["video_id"] = m.get("video_id", "")
        row["category"] = m.get("label", "")
        row["subtopic"] = m.get("bab", "")
        row["manifest_status"] = m.get("status", "")
        return
    parts = path.relative_to(ROOT / "data" / "raw").parts if (ROOT / "data" / "raw") in path.parents else ()
    row["category"] = parts[0] if parts else ""
    row["subtopic"] = parts[1] if len(parts) > 1 else ""
    row["manifest_status"] = "tidak_di_manifest"
    # fallback: nama file berakhiran <audio_id>-<video_id>.wav; audio_id selalu
    # diakhiri nomor urut 4 digit, sedangkan video_id bisa mengandung tanda '-'
    m = re.search(r"-\d{4}-(.+)$", path.stem)
    row["video_id"] = m.group(1) if m else ""


def refresh_derived(row: dict, th: dict) -> None:
    """Hitung ulang flags, skor, dan gate dari metrik yang sudah ada."""
    if row.get("duration_s") in (None, "") and row.get("readable") in (None, ""):
        row["gate"] = ""
        row["gate_reason"] = ""
        return
    row.update(evaluate(row, th))


# --------------------------------------------------------------------------- #
# stage basic
# --------------------------------------------------------------------------- #
def stage_basic(files: list[Path], rows: dict, out: Path, workers: int,
                sample_sec: float, manifest: dict, limit: int, th: dict,
                overwrite: bool = False) -> None:
    todo = [p for p in files if overwrite or not rows.get(_key(p), {}).get("duration_s")]
    if limit:
        todo = todo[:limit]
    if not todo:
        print("[basic] semua file sudah punya metrik dasar, dilewati.")
        return
    print(f"[basic] {len(todo)} file, {workers} worker, sample {sample_sec:.0f} dtk")
    from audio_pipeline.quality import _init_worker

    started = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=max(1, workers), initializer=_init_worker) as ex:
        futures = {ex.submit(analyze_basic, str(p), sample_sec): p for p in todo}
        for fut in as_completed(futures):
            path = futures[fut]
            key = _key(path)
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001
                row = {k: None for k in AUDIO_FIELDS}
                row.update(filepath=str(path), readable=0,
                           error=f"{type(e).__name__}: {e}"[:200])
                row["quality_score"] = 0
            row["filepath"] = path.relative_to(ROOT).as_posix()
            enrich(row, path, manifest)
            refresh_derived(row, th)
            rows[key] = row
            done += 1
            if done % 25 == 0 or done == len(todo):
                save_rows(rows, out)
                rate = done / max(1e-9, time.time() - started)
                print(f"  {done}/{len(todo)}  ({rate:.2f} file/dtk, "
                      f"ETA {(len(todo) - done) / max(rate, 1e-9) / 60:.1f} mnt)")
    save_rows(rows, out)


# --------------------------------------------------------------------------- #
# stage score (DNSMOS)
# --------------------------------------------------------------------------- #
def stage_score(files: list[Path], rows: dict, out: Path, sample_sec: float,
                limit: int, th: dict, overwrite: bool) -> None:
    from audio_pipeline.quality_models import dnsmos_scores

    todo = []
    for p in files:
        row = rows.get(_key(p), {})
        if overwrite or not row.get("dnsmos_ovrl"):
            todo.append(p)
    if limit:
        todo = todo[:limit]
    if not todo:
        print("[score] semua file sudah punya DNSMOS, dilewati.")
        return
    print(f"[score] {len(todo)} file (DNSMOS, CPU)")
    started = time.time()
    for i, p in enumerate(todo, 1):
        key = _key(p)
        row = rows.get(key) or {k: None for k in AUDIO_FIELDS}
        row["filepath"] = p.relative_to(ROOT).as_posix()
        try:
            info = sf.info(str(p))
            y, _ = extract_sample(p, info, sample_sec=sample_sec)
            if y.size:
                row.update(dnsmos_scores(y))
        except Exception as e:  # noqa: BLE001
            row["error"] = f"score: {type(e).__name__}: {e}"[:200]
        refresh_derived(row, th)
        rows[key] = row
        if i % 25 == 0 or i == len(todo):
            save_rows(rows, out)
            rate = i / max(1e-9, time.time() - started)
            print(f"  {i}/{len(todo)}  ({rate:.2f} file/dtk, "
                  f"ETA {(len(todo) - i) / max(rate, 1e-9) / 60:.1f} mnt)")
    save_rows(rows, out)


def _device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


# --------------------------------------------------------------------------- #
# stage events (AST)
# --------------------------------------------------------------------------- #
def stage_events(files: list[Path], rows: dict, out: Path, sample_sec: float,
                 limit: int, th: dict, device: str, overwrite: bool,
                 only_flagged: bool = False) -> None:
    from audio_pipeline.quality_models import classify_events

    todo = []
    for p in files:
        row = rows.get(_key(p), {})
        if only_flagged and row.get("gate") not in ("review", "reject"):
            continue
        if overwrite or not row.get("music_ratio"):
            todo.append(p)
    if limit:
        todo = todo[:limit]
    if not todo:
        print("[events] semua file sudah punya klasifikasi, dilewati.")
        return
    print(f"[events] {len(todo)} file (AST AudioSet, device={device})")
    started = time.time()
    for i, p in enumerate(todo, 1):
        key = _key(p)
        row = rows.get(key) or {k: None for k in AUDIO_FIELDS}
        row["filepath"] = p.relative_to(ROOT).as_posix()
        try:
            info = sf.info(str(p))
            y, _ = extract_sample(p, info, sample_sec=sample_sec)
            if y.size:
                row.update(classify_events(y, device=device))
        except Exception as e:  # noqa: BLE001
            row["error"] = f"events: {type(e).__name__}: {e}"[:200]
        refresh_derived(row, th)
        rows[key] = row
        if i % 10 == 0 or i == len(todo):
            save_rows(rows, out)
            rate = i / max(1e-9, time.time() - started)
            print(f"  {i}/{len(todo)}  ({rate:.2f} file/dtk, "
                  f"ETA {(len(todo) - i) / max(rate, 1e-9) / 60:.1f} mnt)")
    save_rows(rows, out)


def _middle_sample(path: Path, sec: float) -> np.ndarray:
    info = sf.info(str(path))
    sr = int(info.samplerate)
    dur = info.frames / sr if sr else 0.0
    if dur <= sec:
        y, sr2 = read_audio(path)
        from audio_pipeline.quality import _resample

        return _resample(y, sr2)
    start = max(0.0, dur / 2 - sec / 2)
    with sf.SoundFile(str(path)) as f:
        f.seek(int(start * sr))
        block = f.read(int(sec * sr), dtype="float32", always_2d=True)
    from audio_pipeline.quality import _resample

    return _resample(block.mean(axis=1), sr)


# --------------------------------------------------------------------------- #
# stage diar (pyannote)
# --------------------------------------------------------------------------- #
def stage_diar(files: list[Path], rows: dict, out: Path, limit: int, th: dict,
               device: str, only_flagged: bool, overwrite: bool) -> None:
    from audio_pipeline.quality_models import diarization_stats

    todo = []
    for p in files:
        row = rows.get(_key(p), {})
        if not overwrite and row.get("overlap_ratio"):
            continue
        if only_flagged and row.get("gate") not in ("review", "reject"):
            continue
        todo.append(p)
    if limit:
        todo = todo[:limit]
    if not todo:
        print("[diar] tidak ada file yang perlu diproses.")
        return
    try:
        from audio_pipeline.quality_models import _load_diarization

        _load_diarization(device)
    except Exception as e:  # noqa: BLE001
        print(f"[diar] dilewati: {type(e).__name__}: {e}")
        return
    print(f"[diar] {len(todo)} file (pyannote, device={device})")
    for i, p in enumerate(todo, 1):
        key = _key(p)
        row = rows.get(key) or {k: None for k in AUDIO_FIELDS}
        row["filepath"] = p.relative_to(ROOT).as_posix()
        try:
            y = _middle_sample(p, 120.0)
            row.update(diarization_stats(y, device=device))
        except Exception as e:  # noqa: BLE001
            row["error"] = f"diar: {type(e).__name__}: {e}"[:200]
        refresh_derived(row, th)
        rows[key] = row
        if i % 5 == 0 or i == len(todo):
            save_rows(rows, out)
            print(f"  {i}/{len(todo)}")
    save_rows(rows, out)


# --------------------------------------------------------------------------- #
# stage asr (WPM, opsional)
# --------------------------------------------------------------------------- #
def stage_asr(files: list[Path], rows: dict, out: Path, limit: int, th: dict,
              device: str, overwrite: bool) -> None:
    from faster_whisper import WhisperModel

    todo = [p for p in files
            if overwrite or not rows.get(_key(p), {}).get("asr_wpm")]
    if limit:
        todo = todo[:limit]
    if not todo:
        print("[asr] semua file sudah punya WPM, dilewati.")
        return
    print(f"[asr] {len(todo)} file (faster-whisper small, device={device})")
    model = WhisperModel("small", device=device, compute_type="int8_float16"
                         if device == "cuda" else "int8")
    for i, p in enumerate(todo, 1):
        key = _key(p)
        row = rows.get(key) or {k: None for k in AUDIO_FIELDS}
        row["filepath"] = p.relative_to(ROOT).as_posix()
        try:
            y = _middle_sample(p, 60.0)
            tmp = REPORTS / "_tmp_asr.wav"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(tmp), y, SAMPLE_RATE)
            segments, info = model.transcribe(str(tmp), language="id", vad_filter=True)
            text = " ".join(s.text.strip() for s in segments)
            words = len(text.split())
            dur_min = max(1e-6, len(y) / SAMPLE_RATE / 60)
            row.update(asr_wpm=round(words / dur_min, 1), asr_words=words,
                       asr_lang=info.language)
        except Exception as e:  # noqa: BLE001
            row["error"] = f"asr: {type(e).__name__}: {e}"[:200]
        refresh_derived(row, th)
        rows[key] = row
        if i % 10 == 0 or i == len(todo):
            save_rows(rows, out)
            print(f"  {i}/{len(todo)}")
    tmp = REPORTS / "_tmp_asr.wav"
    if tmp.exists():
        tmp.unlink(missing_ok=True)
    save_rows(rows, out)


# --------------------------------------------------------------------------- #
# laporan
# --------------------------------------------------------------------------- #
def apply_relative_flags(rows: dict, th: dict) -> None:
    """Flag gema berbasis peringkat dataset, bukan ambang RT60 absolut."""
    vals = [float(r["rt60_proxy_s"]) for r in rows.values()
            if r.get("rt60_proxy_s") not in (None, "")]
    if len(vals) < 20:
        return
    pct = float(th["warn"].get("rt60_percentile", 90.0))
    thr = float(np.percentile(vals, pct))
    for row in rows.values():
        rt = row.get("rt60_proxy_s")
        if rt in (None, "") or float(rt) < thr:
            continue
        flags = [f for f in str(row.get("flags") or "").split(";") if f]
        if "gema_relatif" not in flags:
            flags.append("gema_relatif")
        row["flags"] = ";".join(flags)
    print(f"[reverb] p90 rt60_proxy = {thr:.2f} s -> flag gema_relatif "
          f"({sum(1 for r in rows.values() if 'gema_relatif' in str(r.get('flags') or ''))} file)")


def write_summary(rows: dict, th: dict) -> None:
    import pandas as pd

    records = []
    for row in rows.values():
        rec = dict(row)
        for col in ("lufs", "snr_db", "dnsmos_ovrl", "dnsmos_sig", "dnsmos_bak",
                    "rt60_proxy_s", "bandwidth_hz", "speech_ratio_vad",
                    "clip_ratio", "silence_ratio", "quality_score", "duration_s"):
            rec[col] = _num(rec.get(col))
        rec["gate"] = str(rec.get("gate") or "")
        records.append(rec)
    if not records:
        return
    df = pd.DataFrame(records)
    df["category"] = df["category"].fillna("").replace("", "?")
    df["subtopic"] = df["subtopic"].fillna("").replace("", "?")
    # Hanya baris yang sudah dianalisis (punya durasi) yang masuk laporan.
    df = df[df["duration_s"].notna()].copy()
    if df.empty:
        return
    df["gate"] = df["gate"].replace("", "?")

    agg = df.groupby(["category", "subtopic"], dropna=False).agg(
        n=("filepath", "size"),
        n_accept=("gate", lambda s: int((s == "accept").sum())),
        n_review=("gate", lambda s: int((s == "review").sum())),
        n_reject=("gate", lambda s: int((s == "reject").sum())),
        dur_median_min=("duration_s", lambda s: round(s.median() / 60, 1)),
        lufs_median=("lufs", "median"),
        snr_median=("snr_db", "median"),
        speech_median=("speech_ratio_vad", "median"),
        dnsmos_ovrl_median=("dnsmos_ovrl", "median"),
        dnsmos_sig_median=("dnsmos_sig", "median"),
        dnsmos_bak_median=("dnsmos_bak", "median"),
        rt60_median=("rt60_proxy_s", "median"),
        bandwidth_median=("bandwidth_hz", "median"),
        score_median=("quality_score", "median"),
        score_p10=("quality_score", lambda s: s.quantile(0.1)),
    ).reset_index()
    agg["pct_review"] = (agg["n_review"] / agg["n"] * 100).round(1)
    agg["pct_reject"] = (agg["n_reject"] / agg["n"] * 100).round(1)
    for col in agg.columns:
        if agg[col].dtype.kind == "f":
            agg[col] = agg[col].round(2)
    agg.to_csv(SUMMARY_OUT, index=False, encoding="utf-8-sig")

    flagged = df[df["gate"].isin(["review", "reject"])].copy()
    cols = ["filepath", "video_id", "audio_id", "category", "subtopic",
            "duration_s", "gate", "gate_reason", "lufs", "snr_db",
            "speech_ratio_vad", "dnsmos_ovrl", "rt60_proxy_s", "bandwidth_hz",
            "quality_score", "flags"]
    if not flagged.empty:
        flagged.sort_values("quality_score")[cols].to_csv(
            FLAGGED_OUT, index=False, encoding="utf-8-sig"
        )
    else:
        df.iloc[0:0][cols].to_csv(FLAGGED_OUT, index=False, encoding="utf-8-sig")

    rejected = df[df["gate"] == "reject"].sort_values("quality_score")
    reject_cols = ["filepath", "video_id", "audio_id", "category", "subtopic",
                   "duration_s", "gate_reason", "quality_score", "flags"]
    rejected[reject_cols].to_csv(REJECT_OUT, index=False, encoding="utf-8-sig")

    n = int(len(df))
    n_acc = int((df["gate"] == "accept").sum())
    n_rev = int((df["gate"] == "review").sum())
    n_rej = int((df["gate"] == "reject").sum())
    print(f"\nRingkasan : {SUMMARY_OUT.relative_to(ROOT)}")
    print(f"Review    : {FLAGGED_OUT.relative_to(ROOT)}  ({n_rev} file)")
    print(f"Reject    : {REJECT_OUT.relative_to(ROOT)}  ({n_rej} file)")
    print(f"Total file: {n} | accept {n_acc} ({n_acc / max(1, n) * 100:.1f}%) "
          f"| review {n_rev} ({n_rev / max(1, n) * 100:.1f}%) "
          f"| reject {n_rej} ({n_rej / max(1, n) * 100:.1f}%)")
    worst = df.sort_values("quality_score").head(5)
    for _, r in worst.iterrows():
        reason = str(r.get("gate_reason") or "")
        score = r["quality_score"]
        score_text = f"{int(score):>3}" if pd.notna(score) else "  ?"
        print(f"  {score_text}  {r['gate']:<6}  "
              f"{reason[:60]:<60} {r['filepath']}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(
        description="Periksa kualitas audio dataset -> data/reports/*.csv",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--stages", default="basic",
                    help="basic,score,events,diar,asr,report (pisahkan koma)")
    ap.add_argument("--scan-dir", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sample-sec", type=float, default=0.0, help="0 = seluruh file")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--label", nargs="+", help="batasi ke label tertentu")
    ap.add_argument("--bab", nargs="+", help="batasi ke bab/topik tertentu")
    ap.add_argument("--only-flagged", action="store_true")
    ap.add_argument("--overwrite", action="store_true",
                    help="hitung ulang kolom stage yang dipilih")
    ap.add_argument("--include-sample01", action="store_true")
    ap.add_argument("--thresholds", help="file JSON berisi override ambang")
    a = ap.parse_args(argv)

    stages = [s.strip() for s in a.stages.split(",") if s.strip()]
    allowed = set(STAGE_FIELD) | {"report"}
    unknown = [s for s in stages if s not in allowed]
    if unknown:
        sys.exit(f"Stage tidak dikenal: {', '.join(unknown)}")

    th = THRESHOLDS
    if a.thresholds:
        with open(a.thresholds, encoding="utf-8") as f:
            th = merge_thresholds(THRESHOLDS, json.load(f))

    out = Path(a.out)
    if not out.is_absolute():
        out = ROOT / out
    scan_dir = Path(a.scan_dir)
    if not scan_dir.is_absolute():
        scan_dir = ROOT / scan_dir

    manifest = load_manifest_map()
    files = discover(scan_dir, manifest, a.include_sample01)
    if a.label:
        files = [p for p in files
                 if (manifest.get(_key(p.relative_to(ROOT))) or {}).get("label") in a.label
                 or p.relative_to(scan_dir).parts[0] in a.label]
    if a.bab:
        files = [p for p in files
                 if (manifest.get(_key(p.relative_to(ROOT))) or {}).get("bab") in a.bab]
    if not files:
        sys.exit(f"Tidak ada WAV di {scan_dir}")

    rows = load_rows(out)
    for p in files:  # pastikan metadata manifest terisi walau belum dihitung
        key = _key(p)
        row = rows.get(key) or {k: None for k in AUDIO_FIELDS}
        row["filepath"] = p.relative_to(ROOT).as_posix()
        enrich(row, p, manifest)
        rows[key] = row

    # Terapkan ulang flag/skor/gate dengan threshold terbaru tanpa olah audio.
    for row in rows.values():
        if row.get("duration_s") is not None or row.get("readable") is not None:
            refresh_derived(row, th)

    print(f"Scan: {scan_dir.relative_to(ROOT)} | {len(files)} WAV | output {out.name}")

    device = _device(a.device)
    if "basic" in stages:
        stage_basic(files, rows, out, a.workers, a.sample_sec, manifest, a.limit, th,
                    a.overwrite)
    if "score" in stages:
        stage_score(files, rows, out, a.sample_sec, a.limit, th, a.overwrite)
    if "events" in stages:
        stage_events(files, rows, out, a.sample_sec, a.limit, th, device, a.overwrite,
                     a.only_flagged)
    if "diar" in stages:
        stage_diar(files, rows, out, a.limit, th, device, a.only_flagged, a.overwrite)
    if "asr" in stages:
        stage_asr(files, rows, out, a.limit, th, device, a.overwrite)

    apply_relative_flags(rows, th)
    save_rows(rows, out)
    write_summary(rows, th)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
