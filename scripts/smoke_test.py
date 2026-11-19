"""Uji cepat tanpa jaringan: registry, sumber, jendela, manifest, export.

Contoh:
    python scripts/smoke_test.py
    python scripts/smoke_test.py --verbose
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401
from audio_pipeline import export, manifest
from audio_pipeline.config import LINKS_TXT, MANIFEST, SRC_DIR, setup_console
from audio_pipeline.planner import build_plan
from audio_pipeline.registry import discover_files, load
from audio_pipeline.sources import parse_file
from audio_pipeline.youtube import pick_windows

OK, FAIL = "[ok]", "[x]"


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="Smoke test offline.")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)
    fails = 0

    def check(name, cond, detail=""):
        nonlocal fails
        print(f"{OK if cond else FAIL} {name}" + (f"  {detail}" if detail else ""))
        fails += 0 if cond else 1

    # 1. modul
    for mod in ("config", "registry", "sources", "search", "youtube", "planner",
                "manifest", "scrape", "export"):
        try:
            importlib.import_module(f"audio_pipeline.{mod}")
            check(f"import audio_pipeline.{mod}", True)
        except Exception as e:  # noqa: BLE001
            check(f"import audio_pipeline.{mod}", False, str(e)[:80])

    # 2. registry
    try:
        labels = load()
        check("registry 21 label", len(labels) == 21, f"{len(labels)} label")
        check("nomor label 1..21", sorted(l.no for l in labels.values())
              == list(range(1, 22)))
    except Exception as e:  # noqa: BLE001
        check("registry 21 label", False, str(e)[:100])
        return 1

    # 3. sumber
    files = discover_files(labels)
    check("21 file sumber", len(files) == 21, f"{len(files)} file")
    total_items = 0
    for slug, path in files.items():
        try:
            items = parse_file(path, slug)
            total_items += len(items)
            if a.verbose:
                print(f"     {slug:<24} {len(items):>3} item")
        except Exception as e:  # noqa: BLE001
            check(f"parse {path.name}", False, str(e)[:100])
    check("semua file sumber bisa diparsing", total_items > 0,
          f"{total_items} item")

    # 4. jendela
    w = pick_windows(1800, 4, 60, seed=7)
    check("pick_windows 4 jendela", len(w) == 4, str(w))
    check("jendela tidak tumpang tindih",
          all(b - a >= 60 for a, b in zip(w, w[1:])))
    check("jendela deterministik", w == pick_windows(1800, 4, 60, seed=7))
    check("intro dilewati", w[0] >= 90, f"mulai {w[0]} s")

    # 5. manifest roundtrip
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "manifest.csv"
        old = manifest.MANIFEST
        manifest.MANIFEST = p
        try:
            rows = {"fisika-k10-k13-0001": {
                "clip_id": "fisika-k10-k13-0001", "label_no": "9",
                "label": "fisika", "kelas": "10", "kurikulum": "k13",
                "video_id": "abcdefghijk", "clip_index": "0",
                "clip_sec": "60", "status": "ok",
                "channel_id": "UC1", "filepath": "x.wav", "bytes": "1",
            }}
            manifest.save(rows)
            a1 = p.read_bytes()
            manifest.save(manifest.load())
            check("manifest roundtrip byte-identik", p.read_bytes() == a1)
        finally:
            manifest.MANIFEST = old

    # 6. rencana dari sumber (tanpa jaringan, pakai kandidat palsu)
    from audio_pipeline.search import Candidate
    kur = ("k13", "kurmerdeka", "ktsp2006")
    fake = []
    for i in range(240):
        k = 10 + i % 3
        fake.append(Candidate(
            video_id=f"vid{i:08d}", title=f"Fisika kelas {k} materi {i}",
            duration=1200, channel="ch", channel_id=f"UC{i % 13}",
            channel_url="", url="", label="fisika", kelas=k,
            kurikulum=kur[(i // 3) % 3], bab="materi",
            source_url="ytsearch:x", source_kind="search", source_query="q"))
    plan = build_plan(fake, labels, 143, only=["fisika"])
    check("planner mengisi target", len(plan["fisika"]) >= 120,
          f"{len(plan['fisika'])} video")

    # 7. export deterministik
    with tempfile.TemporaryDirectory() as td:
        rows = {r["clip_id"]: r for r in []}  # kosong
        out1 = Path(td) / "links.txt"
        export.write_links({}, labels, out=out1, per_label_dir=None)
        b1 = out1.read_bytes()
        export.write_links({}, labels, out=out1, per_label_dir=None)
        check("links_yt.txt idempoten", out1.read_bytes() == b1)

    print("\n" + ("SEMUA OK" if not fails else f"{fails} GAGAL"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
