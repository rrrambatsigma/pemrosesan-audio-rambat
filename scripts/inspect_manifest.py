"""Lihat isi manifest + cek file klipnya (tanpa jaringan).

Contoh:
    python scripts/inspect_manifest.py
    python scripts/inspect_manifest.py --label ppkn --detail 10
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

import _bootstrap  # noqa: F401
import soundfile as sf

from audio_pipeline.config import ROOT, setup_console
from audio_pipeline.manifest import load


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label")
    ap.add_argument("--detail", type=int, default=0, help="tampilkan N baris rinci")
    a = ap.parse_args(argv)

    rows = list(load().values())
    if a.label:
        rows = [r for r in rows if r.get("label") == a.label]
    ok = [r for r in rows if r.get("status") == "ok"]
    bad = [r for r in rows if r.get("status") != "ok"]

    print(f"Total baris: {len(rows)} | ok: {len(ok)} | gagal: {len(bad)}")
    print(f"Video unik : {len({r.get('video_id') for r in ok})}")
    if not ok:
        return 0

    per_label = Counter(r["label"] for r in ok)
    print(f"\n{'label':<24}{'klip':>6}{'video':>7}")
    print("-" * 37)
    for label, n in sorted(per_label.items()):
        vids = len({r["video_id"] for r in ok if r["label"] == label})
        print(f"{label:<24}{n:>6}{vids:>7}")

    missing = [r for r in ok
               if not str(r.get("filepath") or "").startswith("hf://")
               and not (ROOT / (r.get("filepath") or "_")).exists()]
    remote = sum(1 for r in ok
                 if str(r.get("filepath") or "").startswith("hf://"))
    print(f"\nFile hilang: {len(missing)} | di HF: {remote}")

    sample = ok[: a.detail] if a.detail else []
    if sample:
        print()
        for r in sample:
            fp = str(r.get("filepath") or "")
            if fp.startswith("hf://"):
                meta = "di Hugging Face"
            else:
                p = ROOT / fp
                try:
                    info = sf.info(str(p))
                    meta = (f"{info.duration:.1f}s {info.samplerate}Hz "
                            f"{info.channels}ch")
                except Exception:  # noqa: BLE001
                    meta = "TIDAK TERBACA"
            print(f"{r['clip_id']:<28} mulai {r.get('clip_start_sec'):>5}s | "
                  f"{meta:<22} | {r['label']} k{r['kelas']} {r['kurikulum']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
