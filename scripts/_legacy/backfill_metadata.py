"""Lengkapi metadata manifest (title, channel, license) tanpa mengunduh audio.

Hanya memakai yt-dlp untuk membaca halaman video, dengan jeda sopan supaya
tidak memicu rate-limit. Aman dihentikan: manifest disimpan tiap 10 video.

Contoh:
    python scripts/backfill_metadata.py --limit 5
    python scripts/backfill_metadata.py
    python scripts/backfill_metadata.py --all      # termasuk yang sudah lengkap
"""

from __future__ import annotations

import argparse
import random
import sys
import time

import _bootstrap  # noqa: F401

from audio_pipeline.config import setup_console
from audio_pipeline.manifest import load_manifest, save_manifest
from audio_pipeline.youtube import fetch_info


def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(
        description="Lengkapi metadata manifest tanpa mengunduh audio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=0, help="maks video per sesi (0 = semua)")
    ap.add_argument("--all", action="store_true", help="proses juga baris yang sudah lengkap")
    ap.add_argument("--sleep-min", type=float, default=1.5)
    ap.add_argument("--sleep-max", type=float, default=3.0)
    ap.add_argument("--retries", type=int, default=2, help="ulang saat gagal (mis. 403/429)")
    ap.add_argument("--max-fails", type=int, default=8, help="berhenti bila gagal beruntun")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.sleep_min > a.sleep_max:
        a.sleep_min, a.sleep_max = a.sleep_max, a.sleep_min

    rows = load_manifest()
    if not rows:
        sys.exit("manifest.csv kosong")

    todo = [
        vid for vid, r in rows.items()
        if r.get("status") == "ok"
        and (a.all or not (r.get("title", "").strip() and r.get("channel", "").strip()))
    ]
    todo.sort(key=lambda v: (rows[v].get("category", ""), int(rows[v].get("seq") or 0)))
    if a.limit:
        todo = todo[: a.limit]

    print(f"[backfill] {len(todo)} video | jeda {a.sleep_min}-{a.sleep_max} dtk")
    if a.dry_run:
        for vid in todo[:20]:
            r = rows[vid]
            print(f"  [dry] {r.get('audio_id', vid):<42} {vid}")
        if len(todo) > 20:
            print(f"  ... +{len(todo) - 20} lainnya")
        return
    if not todo:
        print("[backfill] semua metadata sudah lengkap.")
        return

    started = time.time()
    done = ok = fail = consec = 0
    try:
        for i, vid in enumerate(todo, 1):
            r = rows[vid]
            url = r.get("url") or f"https://www.youtube.com/watch?v={vid}"
            info = None
            for attempt in range(a.retries + 1):
                try:
                    info = fetch_info(url)
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt >= a.retries:
                        fail += 1
                        consec += 1
                        print(f"  [GAGAL] {vid}: {str(e)[:80]}")
                        break
                    time.sleep(20 * (attempt + 1))

            if info:
                r["title"] = info.get("title") or r.get("title", "")
                r["channel"] = info.get("channel") or r.get("channel", "")
                r["license"] = info.get("license") or r.get("license", "")
                if not r.get("duration_src_sec") and info.get("duration"):
                    r["duration_src_sec"] = info["duration"]
                ok += 1
                consec = 0
            done += 1

            if done % 10 == 0:
                save_manifest(rows)
                rate = done / max(1e-9, time.time() - started)
                eta = (len(todo) - done) / max(rate, 1e-9) / 60
                print(f"  {done}/{len(todo)}  ok={ok} gagal={fail}  "
                      f"({rate:.2f} video/dtk, ETA {eta:.1f} mnt)")

            if consec >= a.max_fails:
                print(f"\n[STOP] {consec} kegagalan beruntun (kemungkinan rate-limit). "
                      f"Kemajuan tersimpan; coba lagi beberapa jam kemudian.")
                break
            if i < len(todo):
                time.sleep(random.uniform(a.sleep_min, a.sleep_max))
    except KeyboardInterrupt:
        print("\nDihentikan manual. Kemajuan tersimpan; jalankan lagi untuk melanjutkan.")

    save_manifest(rows)
    print(f"\n[backfill] selesai: {ok} terisi, {fail} gagal, dari {done} video. "
          f"Durasi {time.time() - started:.0f} dtk.")


if __name__ == "__main__":
    main()
