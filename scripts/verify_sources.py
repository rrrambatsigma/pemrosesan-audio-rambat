"""Verifikasi isi 21 file sumber tanpa jaringan.

Memeriksa:
- file ada dan tidak 0 byte
- semua seksi bisa diparsing (kurikulum/kelas/bab valid)
- setiap label punya bab nyata (bukan "materi kelas N")
- jumlah query dan sebaran kurikulum/kelas

Contoh:
    python scripts/verify_sources.py
    python scripts/verify_sources.py --min-queries 20
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

import _bootstrap  # noqa: F401
from audio_pipeline.config import SRC_DIR, setup_console
from audio_pipeline.registry import load
from audio_pipeline.sources import SourceError, parse_file


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-queries", type=int, default=20,
                    help="minimum item per label (default 20)")
    ap.add_argument("--min-bab", type=int, default=3,
                    help="minimum bab unik per label (default 3)")
    a = ap.parse_args(argv)

    labels = load()
    problems: list[str] = []
    rows: list[tuple] = []
    kur_total: Counter = Counter()
    kelas_total: Counter = Counter()
    grand = 0

    for slug in sorted(labels, key=lambda s: labels[s].no):
        p = SRC_DIR / f"playlist_{slug}.txt"
        if not p.exists():
            problems.append(f"{slug}: file tidak ada")
            continue
        if p.stat().st_size == 0:
            problems.append(f"{slug}: file 0 byte")
            continue
        try:
            items = parse_file(p, slug)
        except SourceError as e:
            problems.append(f"{slug}: {str(e)[:160]}")
            continue

        babs = {i.bab for i in items}
        queries = [i for i in items if i.kind == "search"]
        urls = [i for i in items if i.kind == "url"]
        kur = Counter(i.kurikulum for i in items)
        kelas = Counter(i.kelas for i in items)
        kur_total.update(kur)
        kelas_total.update(kelas)
        grand += len(items)

        generic = [i.bab for i in items
                   if i.kurikulum != "ktsp2006"
                   and i.bab.lower().startswith("materi kelas")]
        if len(items) < a.min_queries:
            problems.append(f"{slug}: hanya {len(items)} item (<{a.min_queries})")
        if len(babs) < a.min_bab:
            problems.append(f"{slug}: hanya {len(babs)} bab unik (<{a.min_bab})")
        missing_kur = [k for k in ("k13", "kurmerdeka", "ktsp2006") if kur[k] == 0]
        if missing_kur:
            problems.append(f"{slug}: kurikulum kosong {missing_kur}")

        rows.append((labels[slug].no, slug, len(items), len(queries), len(urls),
                     len(babs), len(generic), dict(kur), dict(kelas),
                     p.stat().st_size))

    print(f"{'no':>3} {'label':<24}{'item':>6}{'query':>6}{'url':>5}"
          f"{'bab':>5}{'gen':>5}  sebaran kurikulum")
    print("-" * 104)
    for no, slug, n, q, u, nb, ng, kur, kelas, size in rows:
        dist = " ".join(f"{k}:{v}" for k, v in sorted(kur.items()))
        flag = "  <-- generik" if ng else ""
        print(f"{no:>3} {slug:<24}{n:>6}{q:>6}{u:>5}{nb:>5}{ng:>5}  {dist}{flag}")

    print("-" * 104)
    print(f"TOTAL item: {grand} | per kurikulum: "
          + " ".join(f"{k}:{v}" for k, v in sorted(kur_total.items()))
          + " | per kelas: "
          + " ".join(f"{k}:{v}" for k, v in sorted(kelas_total.items())))

    if problems:
        print(f"\n[!] {len(problems)} masalah:")
        for p in problems:
            print("   -", p)
        return 1
    print("\n[ok] 21 file sumber lengkap, terbaca, dan berbasis bab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
