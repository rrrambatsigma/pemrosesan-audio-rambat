"""Susun `data/reports/dataset_inti.csv` dari manifest + hasil QC audio.

Aturan keputusan (berhenti di aturan pertama yang cocok):
  1. tidak ada file / manifest failed        -> "ganti"  (perlu unduh pengganti)
  2. gate == reject (hening/tanpa bicara)    -> "keluar"
  3. terdaftar sebagai duplikat             -> "arsip"  (wakil tetap dipakai)
  4. selain itu                              -> "inti"

Kualitas TIDAK dipakai untuk mengeluarkan file; hanya menjadi tag:
  musik (music_ratio > --music-max), pita_sempit (bandwidth < --bandwidth-min),
  dnsmos_rendah (ovrl < --dnsmos-min), snr_rendah (snr < --snr-min),
  uji_dengar_buruk (kolom `layak` == 0 pada CSV uji dengar).

Contoh:
    python scripts/build_dataset.py
    python scripts/build_dataset.py --lang id --out data/reports/dataset_inti_id.csv
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import Counter
from pathlib import Path

import _bootstrap  # noqa: F401

from audio_pipeline.config import REPORTS_DIR, ROOT, setup_console

MANIFEST = ROOT / "data" / "manifest.csv"
QC = REPORTS_DIR / "audio_quality.csv"
LISTEN = REPORTS_DIR / "dnsmos_uji_dengar.csv"
DUPMAP = REPORTS_DIR / "duplicate_map.csv"
OUT = REPORTS_DIR / "dataset_inti.csv"

FIELDS = [
    "video_id", "audio_id", "kategori", "subtopik", "channel", "durasi_menit",
    "gate", "keputusan", "alasan", "tag", "duplikat_group", "duplikat_dari",
    "perlu_unduh_ulang",
    "dnsmos_ovrl", "dnsmos_bak", "dnsmos_sig", "snr_db", "lufs",
    "bandwidth_hz", "speech_ratio_vad", "music_ratio", "noise_ratio",
    "flags", "layak_manusia", "lang", "catatan",
]


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--qc", default=str(QC))
    ap.add_argument("--listen", default=str(LISTEN))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--lang", default="en", help="nilai kolom lang untuk file yang ada")
    ap.add_argument("--music-max", type=float, default=0.25)
    ap.add_argument("--bandwidth-min", type=float, default=4000.0)
    ap.add_argument("--dnsmos-min", type=float, default=2.8)
    ap.add_argument("--snr-min", type=float, default=8.0)
    a = ap.parse_args(argv)

    with open(a.qc, newline="", encoding="utf-8-sig") as f:
        qc = {r["video_id"]: r for r in csv.DictReader(f) if r.get("video_id")}
    with open(a.manifest, newline="", encoding="utf-8-sig") as f:
        manifest = list(csv.DictReader(f))
    listen = {}
    if os.path.exists(a.listen):
        with open(a.listen, newline="", encoding="utf-8-sig") as f:
            listen = {r["audio_id"]: r for r in csv.DictReader(f) if r.get("audio_id")}
    dupmap = {}
    if os.path.exists(DUPMAP):
        with open(DUPMAP, newline="", encoding="utf-8-sig") as f:
            dupmap = {r["video_id"]: r for r in csv.DictReader(f) if r.get("video_id")}

    rows = []
    for m in manifest:
        vid = m.get("video_id", "")
        q = qc.get(vid) or {}
        aid = q.get("audio_id") or m.get("audio_id", "")
        path = q.get("filepath") or m.get("filepath", "")
        exists = bool(path) and (ROOT / path).exists()

        tags = []
        if q:
            music = num(q.get("music_ratio"))
            if music is not None and music > a.music_max:
                tags.append("musik")
            bw = num(q.get("bandwidth_hz"))
            if bw is not None and bw < a.bandwidth_min:
                tags.append("pita_sempit")
            ovrl = num(q.get("dnsmos_ovrl"))
            if ovrl is not None and ovrl < a.dnsmos_min:
                tags.append("dnsmos_rendah")
            snr = num(q.get("snr_db"))
            if snr is not None and snr < a.snr_min:
                tags.append("snr_rendah")
            if str(listen.get(aid, {}).get("layak", "")) == "0":
                tags.append("uji_dengar_buruk")

        if m.get("status") != "ok" or not exists:
            keputusan = "ganti"
            alasan = "tidak_ada_file; " + (m.get("error", "") or "")[:60]
            redownload = 1
        elif q.get("gate") == "reject":
            keputusan = "keluar"
            alasan = q.get("gate_reason", "")
            redownload = int(any(x in alasan for x in ("energi_nol", "hampir_hening")))
        else:
            dup = dupmap.get(vid) or {}
            if dup.get("duplikat_dari"):
                keputusan = "arsip"
                alasan = "duplikat_dari:" + dup["duplikat_dari"]
                redownload = 0
            else:
                keputusan = "inti"
                alasan = "lolos_gate"
                redownload = 0

        dur = num(q.get("duration_s")) or num(m.get("duration_src_sec"))
        rows.append({
            "video_id": vid,
            "audio_id": aid,
            "kategori": m.get("category", ""),
            "subtopik": m.get("subtopic", ""),
            "channel": m.get("channel", ""),
            "durasi_menit": round(dur / 60, 1) if dur else "",
            "gate": q.get("gate", ""),
            "keputusan": keputusan,
            "alasan": alasan,
            "tag": ";".join(tags),
            "duplikat_group": (dupmap.get(vid) or {}).get("duplikat_group", ""),
            "duplikat_dari": (dupmap.get(vid) or {}).get("duplikat_dari", ""),
            "perlu_unduh_ulang": redownload,
            "dnsmos_ovrl": q.get("dnsmos_ovrl", ""),
            "dnsmos_bak": q.get("dnsmos_bak", ""),
            "dnsmos_sig": q.get("dnsmos_sig", ""),
            "snr_db": q.get("snr_db", ""),
            "lufs": q.get("lufs", ""),
            "bandwidth_hz": q.get("bandwidth_hz", ""),
            "speech_ratio_vad": q.get("speech_ratio_vad", ""),
            "music_ratio": q.get("music_ratio", ""),
            "noise_ratio": q.get("noise_ratio", ""),
            "flags": q.get("flags", ""),
            "layak_manusia": listen.get(aid, {}).get("layak", ""),
            "lang": a.lang if exists else "",
            "catatan": "",
        })

    rows.sort(key=lambda r: (r["kategori"], r["subtopik"], r["audio_id"]))
    out = Path(a.out)
    if not out.is_absolute():
        out = ROOT / out
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    kep = Counter(r["keputusan"] for r in rows)
    tags = Counter(t for r in rows if r["keputusan"] == "inti"
                   for t in r["tag"].split(";") if t)
    print(f"[dataset] {len(rows)} baris -> {out.relative_to(ROOT)}")
    print(f"  keputusan: {dict(kep)}")
    print(f"  tag inti : {dict(tags)}")
    print(f"  perlu unduh ulang: {sum(r['perlu_unduh_ulang'] for r in rows)}")


if __name__ == "__main__":
    main()
