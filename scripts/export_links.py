"""Ekspor links_yt.txt (audit) + tabel cakupan dari manifest.

Contoh:
    python scripts/export_links.py
    python scripts/export_links.py --check
    python scripts/export_links.py --check --deep
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401
from audio_pipeline.config import (LINKS_DIR, LINKS_TXT, REPORTS_DIR,
                                   SOURCE_COVERAGE, setup_console)
from audio_pipeline.export import verify_local, write_coverage, write_links
from audio_pipeline.manifest import load, validate
from audio_pipeline.registry import load as load_registry


def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(LINKS_TXT))
    ap.add_argument("--no-per-label", action="store_true")
    ap.add_argument("--stamp", action="store_true",
                    help="tambahkan waktu pembuatan (membuat file tidak identik)")
    ap.add_argument("--check", action="store_true",
                    help="verifikasi file lokal terhadap manifest")
    ap.add_argument("--deep", action="store_true", help="ikut verifikasi sha256")
    ap.add_argument("--coverage", action="store_true", default=True)
    a = ap.parse_args(argv)

    labels = load_registry()
    rows = load()
    if not rows:
        sys.exit("manifest kosong — belum ada yang diunduh")

    problems = validate(rows)
    if problems:
        print(f"[!] {len(problems)} masalah manifest (10 pertama):")
        for p in problems[:10]:
            print("   -", p)

    path, n = write_links(rows, labels, out=a.out,
                          per_label_dir=None if a.no_per_label else LINKS_DIR,
                          stamp=a.stamp)
    print(f"[ok] {path}  ({n} klip)")

    if a.coverage:
        cov = write_coverage(rows, labels, SOURCE_COVERAGE)
        print(f"[ok] {cov}")

    if a.check:
        res = verify_local(rows, deep=a.deep)
        print(f"\n[check] {res['ok']}/{res['total']} klip valid")
        for key in ("missing", "size_mismatch", "sha_mismatch"):
            if res[key]:
                print(f"  {key}: {len(res[key])} -> {', '.join(res[key][:5])}")
        if res["ok"] != res["total"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
