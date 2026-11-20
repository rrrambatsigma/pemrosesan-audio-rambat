"""Manifest v2: satu baris = satu klip, primary key ``clip_id``.

Semua penulisan atomik (tmp -> replace) dan deterministik (urutan tetap,
line ending tetap) supaya menjalankan ulang tidak mengubah byte file.
"""

from __future__ import annotations

import csv
import os
import re
import time
from pathlib import Path

from .config import (FAILURE_FIELDS, FAILURES, FIELDS, MANIFEST, ROOT,
                     SAMPLE_RATE, KURIKULUM)

KUR_ORDER = {k: i for i, k in enumerate(KURIKULUM)}
CLIP_RE = re.compile(
    r"^(?P<label>[a-z0-9_]+)-k(?P<kelas>1[0-2])-(?P<kur>[a-z0-9]+)-(?P<seq>\d{4})$"
)


class ManifestError(RuntimeError):
    """Isi manifest tidak konsisten."""


# --------------------------------------------------------------------------- #
# baca / tulis
# --------------------------------------------------------------------------- #
def load(path: Path | None = None) -> dict[str, dict]:
    path = Path(path or MANIFEST)
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = {}
        for r in csv.DictReader(f):
            cid = (r.get("clip_id") or "").strip()
            if cid:
                rows[cid] = dict(r)
        return rows


def _sort_key(row: dict):
    try:
        no = int(row.get("label_no") or 999)
    except ValueError:
        no = 999
    try:
        kelas = int(row.get("kelas") or 0)
    except ValueError:
        kelas = 0
    return (no, row.get("label") or "", kelas,
            KUR_ORDER.get(row.get("kurikulum") or "", 99),
            row.get("clip_id") or "")


def save(rows: dict[str, dict], path: Path | None = None) -> None:
    path = Path(path or MANIFEST)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore",
                           lineterminator="\n")
        w.writeheader()
        for row in sorted(rows.values(), key=_sort_key):
            w.writerow({k: row.get(k, "") for k in FIELDS})
    for attempt in range(4):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 3:
                raise ManifestError(
                    f"{path.name} terkunci (dibuka Excel?). Tutup lalu jalankan lagi."
                ) from None
            time.sleep(0.5 * (attempt + 1))


def upsert(rows: dict[str, dict], new_rows) -> int:
    """Tambahkan/ganti baris berdasarkan ``clip_id``; kembalikan jumlah berubah."""
    if isinstance(new_rows, dict):
        new_rows = list(new_rows.values())
    changed = 0
    for row in new_rows:
        cid = row.get("clip_id")
        if not cid:
            raise ManifestError("baris tanpa clip_id")
        if rows.get(cid) != row:
            rows[cid] = row
            changed += 1
    return changed


def validate(rows: dict[str, dict]) -> list[str]:
    """Cek konsistensi manifest; kembalikan daftar masalah (tidak melempar)."""
    problems: list[str] = []
    seen_files: dict[str, str] = {}
    for cid, r in rows.items():
        if not CLIP_RE.match(cid):
            problems.append(f"clip_id tidak sesuai pola: {cid}")
        if not r.get("label"):
            problems.append(f"{cid}: label kosong")
        if r.get("kelas") and int(r["kelas"]) not in (10, 11, 12):
            problems.append(f"{cid}: kelas {r['kelas']}")
        if r.get("kurikulum") and r["kurikulum"] not in KURIKULUM:
            problems.append(f"{cid}: kurikulum {r['kurikulum']}")
        fp = r.get("filepath") or ""
        if r.get("status") == "ok":
            if not fp:
                problems.append(f"{cid}: status ok tanpa filepath")
            elif fp in seen_files:
                problems.append(f"{cid}: filepath sama dengan {seen_files[fp]}")
            else:
                seen_files[fp] = cid
            if not r.get("channel_id"):
                problems.append(f"{cid}: channel_id kosong (split tidak aman)")
            try:
                if float(r.get("clip_sec") or 0) <= 0:
                    problems.append(f"{cid}: clip_sec <= 0")
            except ValueError:
                problems.append(f"{cid}: clip_sec bukan angka")
    return problems


# --------------------------------------------------------------------------- #
# query bantu
# --------------------------------------------------------------------------- #
def ok_rows(rows: dict[str, dict]) -> list[dict]:
    return [r for r in rows.values()
            if r.get("status") == "ok" and (ROOT / (r.get("filepath") or "_")).exists()]


def done_videos(rows: dict[str, dict]) -> set[str]:
    """video_id yang sudah punya minimal satu klip siap pakai."""
    return {r["video_id"] for r in ok_rows(rows) if r.get("video_id")}


def done_videos_per_label(rows: dict[str, dict]) -> dict[str, int]:
    """Jumlah video unik siap pakai per label (bukan jumlah baris klip)."""
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for r in ok_rows(rows):
        vid = r.get("video_id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        label = r.get("label") or ""
        counts[label] = counts.get(label, 0) + 1
    return counts


def clips_of_video(rows: dict[str, dict], video_id: str) -> dict[int, dict]:
    out = {}
    for r in rows.values():
        if r.get("video_id") == video_id:
            try:
                out[int(r.get("clip_index") or 0)] = r
            except ValueError:
                continue
    return out


def next_seq(rows: dict[str, dict], label: str) -> int:
    seqs = []
    for cid, r in rows.items():
        if r.get("label") == label:
            m = CLIP_RE.match(cid)
            if m:
                seqs.append(int(m["seq"]))
    return (max(seqs) + 1) if seqs else 1


def make_clip_id(label: str, kelas: int, kurikulum: str, seq: int) -> str:
    return f"{label}-k{int(kelas)}-{kurikulum}-{int(seq):04d}"


def stats(rows: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows.values():
        slug = r.get("label") or "?"
        s = out.setdefault(slug, {"ok": 0, "failed": 0, "videos": set(),
                                  "channels": set(), "sec": 0.0})
        if r.get("status") == "ok":
            s["ok"] += 1
            s["videos"].add(r.get("video_id"))
            s["channels"].add(r.get("channel_id"))
            try:
                s["sec"] += float(r.get("clip_sec") or 0)
            except ValueError:
                pass
        else:
            s["failed"] += 1
    return out


# --------------------------------------------------------------------------- #
# kegagalan tingkat video (video yang tidak menghasilkan klip sama sekali)
# --------------------------------------------------------------------------- #
def load_failures(path: Path | None = None) -> dict[str, dict]:
    path = Path(path or FAILURES)
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["video_id"]: dict(r) for r in csv.DictReader(f)
                if r.get("video_id")}


def save_failures(rows: dict[str, dict], path: Path | None = None) -> None:
    path = Path(path or FAILURES)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FAILURE_FIELDS, extrasaction="ignore",
                           lineterminator="\n")
        w.writeheader()
        for vid in sorted(rows):
            w.writerow({k: rows[vid].get(k, "") for k in FAILURE_FIELDS})
    os.replace(tmp, path)


def record_failure(rows: dict[str, dict], video_id: str, label: str,
                   stage: str, error: str) -> None:
    from datetime import datetime

    old = rows.get(video_id) or {}
    rows[video_id] = {
        "video_id": video_id,
        "label": label,
        "stage": stage,
        "error": (error or "")[:220],
        "attempts": int(old.get("attempts") or 0) + 1,
        "last_at": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# adopsi file lama (manifest hilang tapi WAV ada)
# --------------------------------------------------------------------------- #
def adopt_existing(rows: dict[str, dict], raw_dir: Path) -> int:
    """Daftarkan kembali WAV yang namanya sesuai clip_id tapi tak ada di manifest."""
    n = 0
    raw_dir = Path(raw_dir)
    if not raw_dir.exists():
        return 0
    for p in sorted(raw_dir.rglob("*.wav")):
        cid = p.stem
        m = CLIP_RE.match(cid)
        if not m or cid in rows:
            continue
        rows[cid] = {
            "clip_id": cid,
            "label": m["label"],
            "kelas": m["kelas"],
            "kurikulum": m["kur"],
            "sr": SAMPLE_RATE,
            "status": "ok",
            "error": "adopted_from_disk",
            "filepath": p.relative_to(ROOT).as_posix(),
            "bytes": p.stat().st_size,
            "sha256": "",
        }
        n += 1
    return n
