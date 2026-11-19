"""Audit sumber sebelum mengunduh: apakah 21 label bisa mencapai target?

Tidak mengunduh audio, hanya metadata (dan memakai cache). Hasil:
    data/reports/source_inventory.csv   satu baris per kandidat video
    data/reports/source_playlists.csv   satu baris per item sumber
    data/reports/source_coverage.csv    matriks label x kelas x kurikulum

Contoh:
    python scripts/sources_audit.py --target-per-label 100
    python scripts/sources_audit.py --only fisika kimia --limit-queries 4
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict

import _bootstrap  # noqa: F401
from audio_pipeline.config import (MAX_CHANNEL_FRAC, REPORTS_DIR, SOURCE_COVERAGE,
                                   SOURCE_INVENTORY, SOURCE_PLAYLISTS,
                                   TARGET_PER_LABEL, ensure_dirs, setup_console)
from audio_pipeline.planner import cells_for, select_label
from audio_pipeline.registry import discover_files, load
from audio_pipeline.search import expand, expand_items, load_cache, save_cache
from audio_pipeline.sources import parse_file, parse_many


def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", help="label tertentu")
    ap.add_argument("--target-per-label", type=int, default=TARGET_PER_LABEL)
    ap.add_argument("--min-duration-min", type=float, default=8.0)
    ap.add_argument("--min-channels", type=int, default=12)
    ap.add_argument("--limit-queries", type=int, default=0,
                    help="batasi jumlah query (uji cepat)")
    ap.add_argument("--refresh", action="store_true", help="abaikan cache 24 jam")
    ap.add_argument("--browser", help="cookies dari browser (chrome/firefox/edge)")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)

    ensure_dirs()
    labels = load()
    files = discover_files(labels)
    if a.only:
        missing = [s for s in a.only if s not in files]
        if missing:
            sys.exit("file sumber belum ada untuk: " + ", ".join(missing)
                     + "\njalankan: python scripts/init_sources.py")
        files = {s: files[s] for s in a.only}

    items = []
    for slug in sorted(files, key=lambda s: labels[s].no):
        items.extend(parse_file(files[slug], slug))
    print(f"[i] {len(items)} item sumber dari {len(files)} label "
          f"({len({i.kind for i in items})} jenis)")

    per_item: dict[str, int] = Counter()
    cache = load_cache()
    expanded: list = []
    counts: dict[str, int] = {}
    for item in items:
        done = counts.get(item.label, 0)
        if a.limit_queries and done >= a.limit_queries:
            continue
        counts[item.label] = done + 1
        got = expand(item, cache, refresh=a.refresh, browser=a.browser)
        per_item[f"{item.label}|{item.kelas}|{item.kurikulum}|{item.bab}|"
                 f"{item.kind}|{item.value}"] = len(got)
        expanded.extend(got)
    save_cache(cache)

    # dedup global: video yang lebih relevan (rank kecil) menang
    best: dict[str, object] = {}
    for c in expanded:
        old = best.get(c.video_id)
        if old is None or c.rank < old.rank:
            best[c.video_id] = c
    cands = list(best.values())
    print(f"[i] {len(expanded)} hasil -> {len(cands)} video unik")

    min_sec = a.min_duration_min * 60
    usable = [c for c in cands if (c.duration or 0) >= min_sec]
    print(f"[i] layak (>= {a.min_duration_min:.0f} menit): {len(usable)}")

    # ---------------- ringkasan per label ----------------
    by_label: dict[str, list] = defaultdict(list)
    for c in usable:
        by_label[c.label].append(c)
    cells = cells_for(a.target_per_label)
    cap = max(3, int(round(a.target_per_label * MAX_CHANNEL_FRAC)))

    print("\n" + "=" * 96)
    print(f"TARGET {a.target_per_label}/label | batas {cap} video/channel | "
          f"min {a.min_channels} channel/label")
    print("=" * 96)
    hdr = (f"{'no':>3} {'label':<24}{'layak':>7}{'rencana':>8}{'channel':>8}"
           f"{'k13':>6}{'km':>6}{'ktsp':>6}  status")
    print(hdr)
    print("-" * len(hdr))

    gaps: list[str] = []
    gap_rows: list[dict] = []
    for slug in sorted(files, key=lambda s: labels[s].no):
        meta = labels[slug]
        part = by_label.get(slug, [])
        ch = len({c.channel_id or c.channel for c in part})
        kur = Counter(c.kurikulum for c in part)
        planned = len(select_label(part, a.target_per_label,
                                   MAX_CHANNEL_FRAC, min_sec))
        status = []
        if planned < a.target_per_label:
            status.append(f"KURANG {a.target_per_label - planned} video")
        if ch < a.min_channels:
            status.append(f"channel {ch}<{a.min_channels}")
        print(f"{meta.no:>3} {slug:<24}{len(part):>7}{planned:>8}{ch:>8}"
              f"{kur['k13']:>6}{kur['kurmerdeka']:>6}{kur['ktsp2006']:>6}  "
              + ("; ".join(status) if status else "siap"))
        if status:
            gaps.append(f"{slug}: " + "; ".join(status))
            gap_rows.append({
                "label_no": meta.no,
                "label": slug,
                "kandidat_layak": len(part),
                "bisa_dipilih": planned,
                "channel": ch,
                "kurang_video": max(0, a.target_per_label - planned),
                "kurang_channel": max(0, a.min_channels - ch),
                "sel_kurang": ";".join(
                    f"k{kelas}/{kur}={sum(1 for c in part if c.kelas == kelas and c.kurikulum == kur)}"
                    f"/{cells.get((kelas, kur), 0)}"
                    for kelas in (10, 11, 12)
                    for kur in ("k13", "kurmerdeka", "ktsp2006")
                    if sum(1 for c in part
                           if c.kelas == kelas and c.kurikulum == kur)
                    < cells.get((kelas, kur), 0)
                ),
            })

        over = Counter(c.channel_id or c.channel for c in part)
        for channel, n_ch in over.items():
            if n_ch > cap:
                print(f"      [!] channel {channel[:40]} punya {n_ch} video "
                      f"(batas {cap}) — akan dipotong planner")

    # ---------------- tulis CSV ----------------
    if not a.no_write:
        inv = []
        for c in sorted(usable, key=lambda c: (c.label, c.kelas, c.kurikulum,
                                               c.channel_id, c.video_id)):
            inv.append({
                "label_no": labels[c.label].no, "label": c.label,
                "kelas": c.kelas, "kurikulum": c.kurikulum, "bab": c.bab,
                "video_id": c.video_id, "title": c.title,
                "duration_sec": c.duration, "channel": c.channel,
                "channel_id": c.channel_id, "rank": c.rank,
                "source_kind": c.source_kind, "source_query": c.source_query,
                "source_url": c.source_url,
            })
        _write(SOURCE_INVENTORY, inv)

        src_rows = []
        for item in items:
            key = (f"{item.label}|{item.kelas}|{item.kurikulum}|{item.bab}|"
                   f"{item.kind}|{item.value}")
            src_rows.append({
                "label": item.label, "kelas": item.kelas,
                "kurikulum": item.kurikulum, "bab": item.bab,
                "kind": item.kind, "value": item.value,
                "hasil": per_item.get(key, 0),
            })
        _write(SOURCE_PLAYLISTS, src_rows)

        cov = []
        for slug in sorted(files, key=lambda s: labels[s].no):
            meta = labels[slug]
            for kelas in (10, 11, 12):
                for kur in ("k13", "kurmerdeka", "ktsp2006"):
                    part = [c for c in by_label.get(slug, [])
                            if c.kelas == kelas and c.kurikulum == kur]
                    cov.append({
                        "label_no": meta.no, "label": slug, "rumpun": meta.rumpun,
                        "kelas": kelas, "kurikulum": kur,
                        "kandidat": len(part),
                        "channel": len({c.channel_id or c.channel for c in part}),
                        "target_sel": cells.get((kelas, kur), 0),
                        "gap": cells.get((kelas, kur), 0) - len(part),
                    })
        _write(SOURCE_COVERAGE, cov)
        if gap_rows:
            _write(REPORTS_DIR / "source_gaps.csv", gap_rows)
            print(f"[i] kekurangan : {(REPORTS_DIR / 'source_gaps.csv').name}")
        print(f"\n[i] laporan: {SOURCE_INVENTORY.name}, "
              f"{SOURCE_PLAYLISTS.name}, {SOURCE_COVERAGE.name}")

    if gaps:
        print("\n" + "=" * 96)
        print("PERLU DITAMBAH DI sumber_data/playlist_<label>.txt")
        print("=" * 96)
        for g in gaps:
            print("  -", g)
        print("\nSaran: tambah 'ytsearch15:<mapel> kelas <n> ...' pada seksi "
              "yang kosong, atau tambah playlist guru/bimbel yang relevan.")
    else:
        print("\n[ok] semua label memenuhi target minimum.")


def _write(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


if __name__ == "__main__":
    main()
