"""Audit dataset lokal (manifest + file WAV).

Contoh:
    python scripts/audit_dataset.py
    python scripts/audit_dataset.py --target 300
    python scripts/audit_dataset.py --cat teknik --list
    python scripts/audit_dataset.py --export
"""

import argparse
import csv
from collections import Counter, defaultdict

import soundfile as sf

from .config import (
    AUDIT_REPORT,
    DATASET_INDEX,
    MANIFEST,
    RAW_DIR,
    REPORTS_DIR,
    ROOT,
    setup_console,
)

TARGET_SR, MIN_SEC, MAX_SEC = 16000, 60, 3600
INFO_FLAGS = {"lang_kosong", "lisensi_non_cc"}  # dilaporkan terpisah, bukan "bermasalah"


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cat")
    ap.add_argument("--sub")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--target", type=int, default=0, help="target jumlah OK per bidang")
    ap.add_argument("--export", action="store_true", help="tulis data/reports/dataset_index.csv")
    a = ap.parse_args(argv)

    with open(MANIFEST, newline="", encoding="utf-8-sig") as f:
        all_rows = list(csv.DictReader(f))
    titles = Counter(r["title"].strip().lower() for r in all_rows if r["title"])
    rows = [r for r in all_rows
            if (not a.cat or r["category"] == a.cat)
            and (not a.sub or r["subtopic"] == a.sub)]

    report, stats, known = [], defaultdict(lambda: {"ok": 0, "bad": 0, "sec": 0.0}), set()
    for r in sorted(rows, key=lambda r: (r["category"], int(r["seq"]))):
        key = (r["category"], r["subtopic"])
        flags, dur, sr, ch = [], 0.0, "", ""
        if r["status"] != "ok":
            flags.append(f"status={r['status']}")
        else:
            p = ROOT / r["filepath"]
            known.add(p.resolve())
            if not p.exists():
                flags.append("FILE_HILANG")
            else:
                info = sf.info(p)
                dur, sr, ch = info.duration, info.samplerate, info.channels
                if sr != TARGET_SR:
                    flags.append(f"sr={sr}")
                if ch != 1:
                    flags.append(f"channels={ch}")
                if dur < MIN_SEC:
                    flags.append("terlalu_pendek")
                if dur > MAX_SEC:
                    flags.append("terlalu_panjang")
                clip = float(r["clip_sec"]) if r.get("clip_sec") else 0
                if clip and dur < 0.9 * clip:
                    flags.append("klip_terpotong")
                if p.name != f"{r['audio_id']}-{r['video_id']}.wav":
                    flags.append("nama_beda_manifest")
            if titles[r["title"].strip().lower()] > 1 and r["title"]:
                flags.append("judul_kembar")
            if not r.get("lang"):
                flags.append("lang_kosong")
            if r.get("license") and "creative commons" not in r["license"].lower():
                flags.append("lisensi_non_cc")
        bad = [x for x in flags if x not in INFO_FLAGS]
        s = stats[key]
        s["ok" if r["status"] == "ok" and not bad else "bad"] += 1
        s["sec"] += dur
        report.append({
            "audio_id": r["audio_id"],
            "category": r["category"],
            "subtopic": r["subtopic"],
            "seq": r["seq"],
            "video_id": r["video_id"],
            "title": r["title"],
            "license": r.get("license", ""),
            "status": r["status"],
            "duration_sec": round(dur, 1),
            "sr": sr,
            "channels": ch,
            "filepath": r.get("filepath", ""),
            "url": r.get("url", ""),
            "flags": ";".join(flags),
        })
        if a.list:
            print(f"{r['audio_id']:<42}{dur / 60:6.1f} mnt  {r['title'][:60]}")

    print("\n=== PER BIDANG / SUBTOPIK ===")
    print(f"{'bidang':<14}{'subtopik':<28}{'ok':>5}{'bermasalah':>12}{'jam':>8}")
    tot = defaultdict(lambda: [0, 0.0])
    for (cat, sub), s in sorted(stats.items()):
        print(f"{cat:<14}{sub:<28}{s['ok']:>5}{s['bad']:>12}{s['sec'] / 3600:>8.2f}")
        tot[cat][0] += s["ok"]
        tot[cat][1] += s["sec"]
    print("\n=== TOTAL PER BIDANG ===")
    for cat, (n, sec) in sorted(tot.items()):
        extra = f" | kurang {max(0, a.target - n)} dari target {a.target}" if a.target else ""
        print(f"{cat:<14}{n:>5} video OK | {sec / 3600:6.2f} jam{extra}")

    hard = [r for r in report if any(x not in INFO_FLAGS for x in r["flags"].split(";") if x)]
    n_lic = sum("lisensi_non_cc" in r["flags"] for r in report)
    n_lang = sum("lang_kosong" in r["flags"] for r in report)
    print(f"\nBermasalah: {len(hard)} | non-CC: {n_lic} | lang belum diisi: {n_lang}")
    for r in hard[:15]:
        print(f"  {r['audio_id']}: {r['flags']}")

    if not a.cat and not a.sub:
        orphans = [p for p in RAW_DIR.rglob("*.wav") if p.resolve() not in known]
        if orphans:
            print(f"\nFile yatim (ada di folder, tidak ada di manifest): {len(orphans)}")
            for p in orphans[:10]:
                print("  ", p.relative_to(ROOT))

    if report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_REPORT, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(report[0].keys()))
            w.writeheader()
            w.writerows(report)
        print(f"\nLaporan lengkap: {AUDIT_REPORT.relative_to(ROOT)}")
        if a.export:
            with open(DATASET_INDEX, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(report[0].keys()))
                w.writeheader()
                w.writerows(report)
            print(f"Indeks audit   : {DATASET_INDEX.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
