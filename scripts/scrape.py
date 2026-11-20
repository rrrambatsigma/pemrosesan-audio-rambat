"""Unduh audio YouTube -> klip WAV 16 kHz mono + manifest (idempoten).

Contoh:
    # lihat rencana tanpa menyentuh jaringan/disk
    python scripts/scrape.py --only fisika --dry-run

    # uji 1 video
    python scripts/scrape.py --only fisika --limit-videos 1 --yes

    # sesi harian
    python scripts/scrape.py --all --max-per-run 180 --yes

    # lanjut besok (aman dijalankan berkali-kali)
    python scripts/scrape.py --all --max-per-run 180 --yes
"""

from __future__ import annotations

import argparse
import sys
import time

import _bootstrap  # noqa: F401
from audio_pipeline import scrape
from audio_pipeline.config import (CLIPS_PER_VIDEO, CLIP_SEC, MAX_CHANNEL_FRAC,
                                   MIN_VIDEO_SEC, SRC_DIR, TARGET_PER_LABEL,
                                   ensure_dirs, setup_console)
from audio_pipeline.manifest import (done_videos, done_videos_per_label, load,
                                     load_failures)
from audio_pipeline.planner import (build_plan, describe, flatten,
                                    remaining_targets)
from audio_pipeline.registry import RegistryError, load as load_registry
from audio_pipeline.search import expand_items
from audio_pipeline.sources import parse_file


def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(
        description="Unduh audio YouTube -> klip WAV (idempoten).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--only", nargs="+", help="label tertentu")
    who.add_argument("--all", action="store_true", help="semua 21 label (default)")

    ap.add_argument("--target-per-label", type=int, default=TARGET_PER_LABEL)
    ap.add_argument("--clips-per-video", type=int, default=CLIPS_PER_VIDEO)
    ap.add_argument("--clip-seconds", type=float, default=CLIP_SEC)
    ap.add_argument("--raw-only", action="store_true",
                    help="simpan audio utuh per video, tanpa segmentasi/klip")
    ap.add_argument("--min-video-sec", type=float, default=MIN_VIDEO_SEC)
    ap.add_argument("--max-channel-frac", type=float, default=MAX_CHANNEL_FRAC)
    ap.add_argument("--max-per-run", type=int, default=0,
                    help="maksimum VIDEO diproses sesi ini (0 = semua)")
    ap.add_argument("--limit-videos", type=int, default=0,
                    help="batasi rencana total (untuk uji cepat)")
    ap.add_argument("--limit-queries", type=int, default=0,
                    help="batasi jumlah query pencarian saat menyusun rencana")

    ap.add_argument("--cookies-from-browser", dest="browser",
                    choices=["chrome", "chromium", "firefox", "edge", "brave",
                             "opera", "safari", "vivaldi"],
                    help="pakai cookies browser (mengatasi sign-in/bot check)")
    ap.add_argument("--only-cc", action="store_true",
                    help="hanya video berlisensi Creative Commons")
    ap.add_argument("--drop-source", action="store_true",
                    help="hapus audio sumber setelah dipotong (hemat disk)")
    ap.add_argument("--refresh-search", action="store_true")
    ap.add_argument("--refresh-probe", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--verify-sha", action="store_true",
                    help="verifikasi sha256 file lama (lebih lambat)")

    ap.add_argument("--sleep-min", type=float, default=4.0)
    ap.add_argument("--sleep-max", type=float, default=10.0)
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--batch-pause", type=float, default=90.0)
    ap.add_argument("--cooldown", type=float, default=300.0)
    ap.add_argument("--max-consecutive-fails", type=int, default=5)

    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="tanpa konfirmasi")
    a = ap.parse_args(argv)

    if a.sleep_min > a.sleep_max:
        a.sleep_min, a.sleep_max = a.sleep_max, a.sleep_min
    if a.raw_only:
        a.clips_per_video = 1
    if a.clips_per_video < 1:
        sys.exit("--clips-per-video minimal 1")
    if a.clip_seconds < 10:
        sys.exit("--clip-seconds minimal 10")

    ensure_dirs()
    try:
        labels = load_registry()
    except RegistryError as e:
        sys.exit(f"{e}\njalankan: python scripts/init_sources.py")

    only = None if a.all else (a.only or list(labels))
    unknown = [s for s in (only or []) if s not in labels]
    if unknown:
        sys.exit("label tidak dikenal: " + ", ".join(unknown))

    # 1) sumber -> kandidat
    print(f"[1/4] membaca sumber untuk {len(only or labels)} label ...")
    items = []
    for slug in sorted(only or labels, key=lambda s: labels[s].no):
        p = SRC_DIR / f"playlist_{slug}.txt"
        if not p.exists():
            sys.exit(f"file sumber belum ada: {p.name}\n"
                     f"jalankan: python scripts/init_sources.py")
        items.extend(parse_file(p, slug))
    print(f"      {len(items)} item sumber")

    cands, _cache = expand_items(items, refresh=a.refresh_search,
                                 browser=a.browser,
                                 limit_queries=a.limit_queries)
    print(f"      {len(cands)} video kandidat unik")

    # 2) rencana, membuang yang sudah selesai / gagal
    rows = load()
    done = done_videos(rows)
    done_per_label = done_videos_per_label(rows)
    failures = load_failures()
    blocked = set() if a.retry_failed else set(failures)
    exclude = done | blocked

    plan = build_plan(cands, labels, a.target_per_label, only=only,
                      exclude_videos=exclude,
                      max_channel_frac=a.max_channel_frac,
                      min_sec=a.min_video_sec,
                      done_per_label=done_per_label)
    targets = remaining_targets(labels, done_per_label,
                                a.target_per_label, only)
    ordered = flatten(plan)
    if a.limit_videos:
        ordered = ordered[: a.limit_videos]

    print(f"[2/4] selesai sekarang: {len(done)} video | gagal: {len(failures)} "
          f"| rencana baru: {len(ordered)} video")
    print(describe(plan, labels, targets))
    if not ordered:
        print("\n[ok] tidak ada yang perlu diunduh (idempoten).")
        return

    if a.raw_only:
        print(f"\n[3/4] mode raw: {len(ordered)} berkas audio utuh "
              f"(±{len(ordered) * 10 / 1000:.1f} GB opus) | "
              f"unduhan ±{len(ordered) * 10 / 1000:.1f} GB")
    else:
        est_clips = len(ordered) * a.clips_per_video
        print(f"\n[3/4] perkiraan: {est_clips} klip "
              f"({est_clips * a.clip_seconds / 3600:.1f} jam, "
              f"{est_clips * a.clip_seconds * 32000 / 1e9:.1f} GB WAV) | "
              f"unduhan ±{len(ordered) * 8 / 1000:.1f} GB")
    if a.dry_run:
        print("\n[dry-run] 10 video pertama:")
        for c in ordered[:10]:
            print(f"  {c.label:<22} {c.kelas} {c.kurikulum:<11} "
                  f"{c.video_id}  {(c.title or '')[:52]}")
        print("\nTidak ada file yang diubah (dry-run).")
        return
    if not a.yes and input("\nLanjut unduh? [y/N] ").strip().lower() != "y":
        return

    # 3) eksekusi
    started = time.time()
    try:
        scrape.acquire_lock()
    except scrape.LockBusy as e:
        sys.exit(str(e))

    try:
        mode = "1 berkas utuh/video" if a.raw_only else (
            f"{a.clips_per_video} klip x {a.clip_seconds:.0f} dtk")
        print(f"\n[4/4] mulai: {len(ordered)} video ({mode})")
        stats = scrape.run(
            ordered, labels,
            clips_per_video=a.clips_per_video,
            clip_sec=a.clip_seconds,
            browser=a.browser,
            max_per_run=a.max_per_run,
            only_cc=a.only_cc,
            keep_source=not a.drop_source,
            retry_failed=a.retry_failed,
            refresh_probe=a.refresh_probe,
            verify_sha=a.verify_sha,
            sleep=(a.sleep_min, a.sleep_max),
            batch_size=a.batch_size,
            batch_pause=a.batch_pause,
            cooldown=a.cooldown,
            max_consecutive_fails=a.max_consecutive_fails,
            raw_only=a.raw_only,
        )
    finally:
        scrape.release_lock()

    mins = (time.time() - started) / 60
    print("\n" + "=" * 60)
    print(f"Sesi selesai dalam {mins:.1f} menit")
    print(f"  video diproses : {stats.get('videos_done', 0)}")
    print(f"  video ok       : {stats.get('videos_ok', 0)}")
    print(f"  video dilewati : {stats.get('videos_skipped', 0)}")
    print(f"  video gagal    : {stats.get('videos_failed', 0)}")
    print(f"  klip baru      : {stats.get('clips_new', 0)}")
    print(f"  klip dipakai ulang: {stats.get('clips_reused', 0)}")
    print("\nJalankan lagi perintah yang sama untuk melanjutkan.")
    print("Audit link : python scripts/export_links.py")


if __name__ == "__main__":
    main()
