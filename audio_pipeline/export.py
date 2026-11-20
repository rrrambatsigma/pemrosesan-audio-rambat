"""Ekspor jejak audit: ``links_yt.txt`` dan tabel cakupan.

File link **selalu digenerate ulang** dari manifest, sehingga tidak mungkin
menyimpang dari dataset. Tanpa ``stamp=True`` hasilnya byte-identik setiap
kali dijalankan pada manifest yang sama (idempoten).
"""

from __future__ import annotations

import csv
import hashlib
from collections import Counter
from pathlib import Path

from .config import KELAS, KURIKULUM, LINKS_DIR, LINKS_TXT, ROOT, TARGET_PER_LABEL
from .registry import Label
from .youtube import sha256_file


def mmss(sec: float) -> str:
    sec = max(0, int(round(float(sec or 0))))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _clean(text: str, limit: int) -> str:
    return " ".join(str(text or "").replace("|", "/").split())[:limit]


def fingerprint(rows: dict[str, dict]) -> str:
    h = hashlib.sha256()
    for cid in sorted(rows):
        r = rows[cid]
        h.update(f"{cid}:{r.get('sha256','')}:{r.get('status','')}".encode())
    return h.hexdigest()[:16]


def _ok(rows: dict[str, dict]) -> list[dict]:
    return [r for r in rows.values() if r.get("status") == "ok"]


def write_links(rows: dict[str, dict], labels: dict[str, Label],
                out: Path = LINKS_TXT, per_label_dir: Path | None = LINKS_DIR,
                stamp: bool = False) -> tuple[Path, int]:
    """Tulis links_yt.txt global (+ per label). Kembalikan (path, jumlah baris)."""
    ok = _ok(rows)
    ok.sort(key=lambda r: (
        int(r.get("label_no") or 999), r.get("label") or "",
        int(r.get("kelas") or 0),
        KURIKULUM.index(r["kurikulum"]) if r.get("kurikulum") in KURIKULUM else 99,
        r.get("channel") or "", r.get("video_id") or "",
        int(r.get("clip_index") or 0),
    ))

    total_videos = len({r.get("video_id") for r in ok})
    total_sec = sum(float(r.get("clip_sec") or 0) for r in ok)
    lines = [
        "# " + "=" * 74,
        "# Dataset: Klasifikasi Mata Pelajaran SMA dari Audio",
        "# Skrip   : scripts/export_links.py",
        f"# Isi     : {len(ok)} klip | {total_videos} video | "
        f"{total_sec / 3600:.1f} jam | {len(labels)} label",
        f"# Manifest: data/manifest.csv  sha256 {fingerprint(rows)}",
    ]
    if stamp:
        from datetime import datetime
        lines.append(f"# Dibuat  : {datetime.now().isoformat(timespec='seconds')}")
    lines += [
        "# Format  : URL?t=<detik>  # mm:ss-mm:ss | judul | channel | lisensi "
        "| status | qc | sha256(8)",
        "# " + "=" * 74,
        "",
    ]

    by_label: dict[str, list[dict]] = {}
    for r in ok:
        by_label.setdefault(r.get("label") or "?", []).append(r)

    for slug in sorted(by_label, key=lambda s: labels[s].no if s in labels else 999):
        items = by_label[slug]
        meta = labels.get(slug)
        lines.append(f"## [{meta.no if meta else '??'}] {slug}  ·  "
                     f"{meta.rumpun if meta else '?'}  ·  "
                     f"{len(items)} klip / {len({r.get('video_id') for r in items})} video")
        for kelas in KELAS:
            for kur in KURIKULUM:
                part = [r for r in items
                        if int(r.get("kelas") or 0) == kelas
                        and r.get("kurikulum") == kur]
                if not part:
                    continue
                nvid = len({r.get("video_id") for r in part})
                bab = sorted({r.get("bab") or "-" for r in part})
                tag = "   [CAKUPAN RENDAH]" if len(part) < 6 else ""
                lines.append(f"### {kur} | kelas {kelas} | "
                             f"{', '.join(bab)[:70]}   ({len(part)} klip / {nvid} video){tag}")
                for r in part:
                    start = float(r.get("clip_start_sec") or 0)
                    end = start + float(r.get("clip_sec") or 0)
                    score = r.get("qc_score") or "-"
                    lines.append(
                        f"{r.get('url')}  # {mmss(start)}-{mmss(end)} | "
                        f"{_clean(r.get('title'), 80)} | "
                        f"{_clean(r.get('channel'), 36)} | "
                        f"{_clean(r.get('license') or '-', 18)} | "
                        f"{r.get('status')} | qc {score} | "
                        f"{(r.get('sha256') or '')[:8]}"
                    )
        lines.append("")

    lines += ["# " + "=" * 74, "# RINGKASAN CAKUPAN", "# " + "=" * 74]
    for slug in sorted(by_label, key=lambda s: labels[s].no if s in labels else 999):
        items = by_label[slug]
        per_kelas = Counter(int(r.get("kelas") or 0) for r in items)
        per_kur = Counter(r.get("kurikulum") for r in items)
        ch = len({r.get("channel_id") for r in items})
        lines.append(
            f"# {slug:<24} klip {len(items):>4} | video "
            f"{len({r.get('video_id') for r in items}):>4} | channel {ch:>3} | "
            + " ".join(f"k{k}:{per_kelas.get(k, 0)}" for k in KELAS)
            + " | " + " ".join(f"{k}:{per_kur.get(k, 0)}" for k in KURIKULUM)
        )

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(out)

    if per_label_dir:
        per_label_dir = Path(per_label_dir)
        per_label_dir.mkdir(parents=True, exist_ok=True)
        head = lines[:8]
        for slug, items in by_label.items():
            # header + hanya baris label ini
            lab_lines = list(head)
            lab_lines.append("")
            idx = next(i for i, l in enumerate(lines) if f"] {slug}  ·" in l)
            j = next((k for k in range(idx + 1, len(lines))
                      if lines[k].startswith("## [")), len(lines))
            lab_lines += lines[idx:j]
            p = per_label_dir / f"links_{slug}.txt"
            t = p.with_suffix(".txt.tmp")
            t.write_text("\n".join(lab_lines) + "\n", encoding="utf-8")
            t.replace(p)
    return out, len(ok)


def write_coverage(rows: dict[str, dict], labels: dict[str, Label],
                   out: Path) -> Path:
    """CSV cakupan per (label, kelas, kurikulum) + status target."""
    ok = _ok(rows)
    counts = Counter(
        (r.get("label"), int(r.get("kelas") or 0), r.get("kurikulum")) for r in ok
    )
    per_label = Counter(r.get("label") for r in ok)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["label_no", "label", "rumpun", "kelas", "kurikulum",
                    "klip", "video", "channel", "jam", "target_label",
                    "cakupan_label_pct"])
        for slug in sorted(labels, key=lambda s: labels[s].no):
            meta = labels[slug]
            for kelas in KELAS:
                for kur in KURIKULUM:
                    part = [r for r in ok
                            if r.get("label") == slug
                            and int(r.get("kelas") or 0) == kelas
                            and r.get("kurikulum") == kur]
                    w.writerow([
                        meta.no, slug, meta.rumpun, kelas, kur, len(part),
                        len({r.get("video_id") for r in part}),
                        len({r.get("channel_id") for r in part}),
                        round(sum(float(r.get("clip_sec") or 0) for r in part) / 3600, 2),
                        per_label[slug],
                        round(100 * per_label[slug] / max(1, TARGET_PER_LABEL), 1),
                    ])
    tmp.replace(out)
    return out


def verify_local(rows: dict[str, dict], deep: bool = False) -> dict:
    """Cek integritas file lokal terhadap manifest (tanpa jaringan)."""
    res = {"total": 0, "ok": 0, "remote": 0, "missing": [],
           "size_mismatch": [], "sha_mismatch": []}
    for r in _ok(rows):
        res["total"] += 1
        fp = str(r.get("filepath") or "")
        if fp.startswith("hf://"):
            res["remote"] += 1          # sudah pindah ke Hugging Face
            res["ok"] += 1
            continue
        p = ROOT / fp
        if not p.exists():
            res["missing"].append(r.get("clip_id"))
            continue
        if r.get("bytes") and p.stat().st_size != int(r["bytes"]):
            res["size_mismatch"].append(r.get("clip_id"))
            continue
        if deep and r.get("sha256") and sha256_file(p) != r["sha256"]:
            res["sha_mismatch"].append(r.get("clip_id"))
            continue
        res["ok"] += 1
    return res
