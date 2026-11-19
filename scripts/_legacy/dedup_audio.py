"""Deteksi duplikat audio (isi sama, unggahan berbeda).

Tiga lapis, dari yang paling pasti:
  1. hash PCM identik (sampel 3x20 dtk + seluruh file)
  2. kemiripan judul (rapidfuzz) + durasi berdekatan
  3. kemiripan akustik (chroma CQT) untuk kandidat, sebagai verifikasi

Keluaran:
  data/reports/duplicates.csv     satu baris per anggota grup duplikat
  data/reports/duplicate_map.csv  pemetaan video_id -> grup + wakil

Contoh:
    python scripts/dedup_audio.py --limit 40      # uji cepat
    python scripts/dedup_audio.py --workers 6
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import soundfile as sf

from audio_pipeline.config import REPORTS_DIR, ROOT, setup_console

MANIFEST = ROOT / "data" / "manifest.csv"
QC = REPORTS_DIR / "audio_quality.csv"
DUPS_OUT = REPORTS_DIR / "duplicates.csv"
MAP_OUT = REPORTS_DIR / "duplicate_map.csv"

TITLE_RE = re.compile(r"[^a-z0-9 ]+")


def norm_title(text: str) -> str:
    return " ".join(TITLE_RE.sub(" ", (text or "").lower()).split())


def hash_samples(path: str, sec: int = 20, fracs=(0.25, 0.5, 0.75)) -> str:
    """Hash potongan PCM di tiga posisi; tahan terhadap header berbeda."""
    with sf.SoundFile(path) as f:
        sr, frames = f.samplerate, f.frames
        h = hashlib.md5()
        h.update(f"{round(frames / sr, 2)}".encode())
        for frac in fracs:
            start = int(max(0, min(frames - sec * sr, frames * frac - sec * sr / 2)))
            f.seek(start)
            block = f.read(int(sec * sr), dtype="int16", always_2d=True)
            if block.size:
                h.update(block[:, 0].tobytes() if block.shape[1] == 1
                         else block.mean(axis=1).astype(np.int16).tobytes())
    return h.hexdigest()


def hash_full(path: str, block_frames: int = 1_000_000) -> str:
    """Hash seluruh PCM; hanya untuk file dalam kandidat."""
    h = hashlib.md5()
    with sf.SoundFile(path) as f:
        while True:
            b = f.read(block_frames, dtype="int16", always_2d=True)
            if b.size == 0:
                break
            h.update(b[:, 0].tobytes() if b.shape[1] == 1
                     else b.mean(axis=1).astype(np.int16).tobytes())
    return h.hexdigest()


def chroma_signature(path: str, sec: float = 30.0) -> np.ndarray:
    """Chroma CQT dari potongan tengah (12 x frame), pada sample rate asli."""
    import librosa

    with sf.SoundFile(path) as f:
        file_sr, frames = f.samplerate, f.frames
        dur = frames / file_sr
        start = max(0.0, dur / 2 - sec / 2)
        f.seek(int(start * file_sr))
        y = f.read(int(min(sec, dur) * file_sr), dtype="float32", always_2d=True)
    y = y.mean(axis=1)
    # CQT butuh fmax < Nyquist; pakai sample rate asli (16 kHz) agar aman.
    return librosa.feature.chroma_cqt(y=y, sr=file_sr, hop_length=1024).astype(np.float32)


def process_one(path: str) -> dict:
    """Sampel hash + signature akustik. Hash penuh menyusul untuk kandidat."""
    try:
        sig = chroma_signature(path)
        blocks = np.array_split(sig, 3, axis=1)
        feat = np.concatenate([b.mean(axis=1) for b in blocks])
        norm = float(np.linalg.norm(feat)) or 1.0
        return {
            "path": path,
            "sample_hash": hash_samples(path),
            "chroma": sig,
            "feat": (feat / norm),
            "error": "",
        }
    except Exception as e:  # noqa: BLE001
        return {"path": path, "sample_hash": "", "chroma": None,
                "feat": None, "error": f"{type(e).__name__}: {e}"}


def seq_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Kosinus rata-rata antar frame pada panjang minimum (tanpa lag)."""
    n = min(a.shape[1], b.shape[1])
    if n < 10:
        return 0.0
    a = a[:, :n]
    b = b[:, :n]
    num = float(np.sum(a * b))
    den = float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return num / den


def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="batasi jumlah file (uji cepat)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--title-cutoff", type=float, default=90.0)
    ap.add_argument("--duration-tol", type=float, default=0.03)
    ap.add_argument("--acoustic-cutoff", type=float, default=0.98)
    ap.add_argument("--out", default=str(DUPS_OUT))
    ap.add_argument("--map", default=str(MAP_OUT))
    a = ap.parse_args(argv)

    with open(MANIFEST, newline="", encoding="utf-8-sig") as f:
        manifest = [r for r in csv.DictReader(f) if r.get("status") == "ok"]
    with open(QC, newline="", encoding="utf-8-sig") as f:
        qc = {r["video_id"]: r for r in csv.DictReader(f) if r.get("video_id")}

    rows = []
    for m in manifest:
        vid = m.get("video_id")
        q = qc.get(vid)
        if not q or not q.get("filepath"):
            continue
        path = ROOT / q["filepath"]
        if not path.exists():
            continue
        rows.append({
            "video_id": vid,
            "audio_id": q.get("audio_id") or m.get("audio_id", ""),
            "title": m.get("title", ""),
            "channel": m.get("channel", ""),
            "duration": float(q.get("duration_s") or 0),
            "score": float(q.get("quality_score") or 0),
            "dnsmos": float(q.get("dnsmos_ovrl") or 0),
            "path": str(path),
            "seq": int(m.get("seq") or 0),
        })
    if a.limit:
        rows = rows[: a.limit]
    print(f"[dedup] {len(rows)} file | workers {a.workers}")

    # ---- 1) signature akustik + hash sampel ----
    started = time.time()
    results = {}
    with ProcessPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(process_one, r["path"]): r for r in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            r = futs[fut]
            res = fut.result()
            results[r["video_id"]] = res
            if i % 100 == 0 or i == len(rows):
                rate = i / max(1e-9, time.time() - started)
                print(f"  signature {i}/{len(rows)} ({rate:.1f} file/dtk, "
                      f"ETA {(len(rows) - i) / max(rate, 1e-9) / 60:.1f} mnt)")
    errs = [v for v in results.values() if v["error"]]
    print(f"[dedup] signature selesai: {len(results) - len(errs)} ok, {len(errs)} gagal")

    # ---- 2) kandidat: hash sampel, judul, akustik ----
    pairs = {}

    def add_pair(i, j, method, sim=1.0):
        if i == j:
            return
        key = tuple(sorted((i, j)))
        old = pairs.get(key)
        if old is None or sim > old[1]:
            pairs[key] = (method, sim)

    hash_groups = {}
    for r in rows:
        res = results.get(r["video_id"]) or {}
        if res.get("sample_hash"):
            key = (res["sample_hash"], round(r["duration"], 1))
            hash_groups.setdefault(key, []).append(r["video_id"])
    for vids in hash_groups.values():
        for i in range(len(vids)):
            for j in range(i + 1, len(vids)):
                add_pair(vids[i], vids[j], "hash", 1.0)

    from rapidfuzz import fuzz, process

    titles = [norm_title(r["title"]) for r in rows]
    if any(titles):
        sim = process.cdist(titles, titles, scorer=fuzz.token_set_ratio,
                            score_cutoff=a.title_cutoff, workers=-1)
        for i, j in zip(*np.where(sim >= a.title_cutoff)):
            if i >= j:
                continue
            di, dj = rows[i]["duration"], rows[j]["duration"]
            if not di or not dj:
                continue
            if abs(di - dj) / max(di, dj) <= a.duration_tol:
                add_pair(rows[i]["video_id"], rows[j]["video_id"], "judul",
                         float(sim[i, j]) / 100)

    feats = [results.get(r["video_id"], {}).get("feat") for r in rows]
    valid = [(i, f) for i, f in enumerate(feats) if f is not None]
    if len(valid) > 1:
        idx = [i for i, _ in valid]
        mat = np.stack([f for _, f in valid])
        sim = mat @ mat.T
        tri = np.triu(sim >= a.acoustic_cutoff, 1)
        for oi, oj in zip(*np.where(tri)):
            ri, rj = rows[idx[oi]], rows[idx[oj]]
            if not ri["duration"] or not rj["duration"]:
                continue
            if abs(ri["duration"] - rj["duration"]) / max(ri["duration"], rj["duration"]) > 0.05:
                continue
            add_pair(ri["video_id"], rj["video_id"], "akustik", float(sim[oi, oj]))

    # ---- 3) verifikasi kandidat + hash penuh ----
    by_id = {r["video_id"]: r for r in rows}
    confirmed = []
    for (vi, vj), (method, sim) in pairs.items():
        ri, rj = by_id[vi], by_id[vj]
        if method == "hash":
            confirmed.append((vi, vj, "hash", 1.0))
            continue
        ci = results[vi]["chroma"]
        cj = results[vj]["chroma"]
        if ci is None or cj is None:
            continue
        seq = seq_similarity(ci, cj)
        if method == "judul":
            confirmed.append((vi, vj, "judul+akustik", round(seq, 3)))
        elif seq >= 0.90:
            confirmed.append((vi, vj, "akustik", round(seq, 3)))

    # hash penuh untuk kandidat yang belum pasti (memastikan bukan kebetulan)
    verify_ids = {v for pair in confirmed if pair[2] != "hash" for v in pair[:2]}
    print(f"[dedup] verifikasi hash penuh: {len(verify_ids)} file")
    full = {}
    for k, vid in enumerate(sorted(verify_ids), 1):
        try:
            full[vid] = hash_full(by_id[vid]["path"])
        except Exception:  # noqa: BLE001
            full[vid] = ""
        if k % 25 == 0:
            print(f"  hash {k}/{len(verify_ids)}")
    confirmed = [p for p in confirmed
                 if p[2] == "hash"
                 or (full.get(p[0]) and full.get(p[0]) == full.get(p[1]))
                 or (p[2] == "judul+akustik" and p[3] >= 0.97)
                 or (p[2] == "akustik" and p[3] >= 0.95)]

    # ---- 4) kelompokkan (union-find) ----
    parent = {v: v for v in by_id}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for vi, vj, method, sim in confirmed:
        union(vi, vj)

    groups = {}
    for vid in by_id:
        groups.setdefault(find(vid), []).append(vid)
    groups = {g: v for g, v in groups.items() if len(v) > 1}

    # ---- 5) pilih wakil & tulis laporan ----
    method_of = {}
    for vi, vj, method, sim in confirmed:
        for v in (vi, vj):
            method_of.setdefault(v, method)

    out_rows, map_rows = [], []
    for gid, vids in enumerate(sorted(groups.values(), key=len, reverse=True), 1):
        members = sorted((by_id[v] for v in vids),
                         key=lambda r: (-r["score"], -r["dnsmos"],
                                        -r["duration"], r["seq"]))
        keep = members[0]
        methods = sorted({method_of.get(v, "?") for v in vids})
        for m in members:
            peran = "wakil" if m["video_id"] == keep["video_id"] else "duplikat"
            out_rows.append({
                "grup": gid,
                "peran": peran,
                "video_id": m["video_id"],
                "audio_id": m["audio_id"],
                "judul": m["title"],
                "channel": m["channel"],
                "durasi_menit": round(m["duration"] / 60, 1),
                "skor": int(m["score"]),
                "dnsmos": round(m["dnsmos"], 2),
                "metode": ";".join(methods),
                "wakil_dari": "" if peran == "wakil" else keep["video_id"],
            })
            map_rows.append({
                "video_id": m["video_id"],
                "duplikat_group": gid,
                "duplikat_dari": "" if peran == "wakil" else keep["video_id"],
                "metode_duplikat": ";".join(methods),
                "peran": peran,
            })

    out = Path(a.out)
    map_out = Path(a.map)
    if not out.is_absolute():
        out = ROOT / out
    if not map_out.is_absolute():
        map_out = ROOT / map_out
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()) if out_rows else
                           ["grup", "peran", "video_id", "audio_id", "judul", "channel",
                            "durasi_menit", "skor", "dnsmos", "metode", "wakil_dari"])
        w.writeheader()
        w.writerows(out_rows)
    with open(map_out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["video_id", "duplikat_group", "duplikat_dari",
                                          "metode_duplikat", "peran"])
        w.writeheader()
        w.writerows(map_rows)

    n_dup = sum(1 for r in out_rows if r["peran"] == "duplikat")
    print(f"\n[dedup] grup duplikat: {len(groups)} | file duplikat: {n_dup} "
          f"| file unik tersisa: {len(rows) - n_dup}")
    print(f"  laporan: {out.relative_to(ROOT)}")
    print(f"  peta   : {map_out.relative_to(ROOT)}")
    for gid, vids in enumerate(sorted(groups.values(), key=len, reverse=True)[:5], 1):
        members = sorted((by_id[v] for v in vids), key=lambda r: -r["score"])
        print(f"  grup {gid} ({len(vids)}): " + " | ".join(
            f'{m["video_id"]} {m["duration"] / 60:.0f}mnt skor {int(m["score"])}' for m in members[:4]))


if __name__ == "__main__":
    main()
