"""Pipeline lokal: unduh YouTube -> WAV 16 kHz mono + manifest.

Skrip CLI ada di scripts/scrape_<bidang>.py dan scripts/scrape_local.py.
"""

import argparse
import os
import random
import shutil
import sys
import time
from collections import Counter
from datetime import datetime

import yt_dlp

from .config import BLOCK_HINTS, RAW_DIR, ROOT, SAMPLE_RATE, TMP_DIR, setup_console
from .manifest import adopt_existing, load_manifest, save_manifest
from .planner import build_candidates
from .sources import discover, parse_source
from .youtube import download, fetch_info, load_cache, save_cache


def main(argv=None, default_cats=None):
    """Jalankan pipeline lokal. ``default_cats`` dipakai skrip per bidang."""
    setup_console()
    ap = argparse.ArgumentParser(
        description="Unduh audio YouTube -> WAV 16 kHz mono (lokal).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--cats", nargs="+", help="bidang yang dijalankan (default: semua playlist_*)")
    ap.add_argument("--subs", nargs="+", help="batasi ke subtopik tertentu (nama slug)")
    ap.add_argument("--clip-minutes", type=int, default=0, help="ambil N menit pertama (0 = full, default)")
    ap.add_argument("--max-per-playlist", type=int, default=0, help="maks video per playlist (0 = semua)")
    ap.add_argument("--quota-cat", type=int, default=0,
                    help="target total video OK per bidang; diambil bergiliran antar subtopik")
    ap.add_argument("--max-per-run", type=int, default=0, help="maks video diunduh per sesi (0 = semua)")
    ap.add_argument("--min-duration-min", type=float, default=5, help="lewati video lebih pendek (menit)")
    ap.add_argument("--only-cc", action="store_true", help="hanya video berlisensi Creative Commons")
    ap.add_argument("--skip-failed", action="store_true", help="jangan coba ulang yang gagal")
    ap.add_argument("--refresh", action="store_true", help="abaikan cache isi playlist")
    # --- adab ---
    ap.add_argument("--sleep-min", type=float, default=4, help="jeda minimum antar video (detik)")
    ap.add_argument("--sleep-max", type=float, default=10, help="jeda maksimum antar video (detik)")
    ap.add_argument("--batch-size", type=int, default=25, help="istirahat panjang tiap N video")
    ap.add_argument("--batch-pause", type=float, default=90, help="lama istirahat panjang (detik)")
    ap.add_argument("--cooldown", type=float, default=300, help="jeda bila kena 429/bot-check (detik)")
    ap.add_argument("--max-consecutive-fails", type=int, default=5,
                    help="berhenti bila gagal beruntun sebanyak ini")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="lewati konfirmasi")
    a = ap.parse_args(argv)
    if a.sleep_min > a.sleep_max:
        a.sleep_min, a.sleep_max = a.sleep_max, a.sleep_min

    ver = yt_dlp.version.__version__
    has_ff = bool(shutil.which("ffmpeg"))
    print(f"yt-dlp {ver} | ffmpeg {'OK' if has_ff else 'TIDAK ADA'} | python {sys.version.split()[0]}")
    if not has_ff:
        sys.exit("ffmpeg tidak ada di PATH (conda install -c conda-forge ffmpeg)")

    rows = load_manifest()
    adopt_existing(rows)
    cache = load_cache()
    claimed = {vid: r["category"] for vid, r in rows.items()}  # video -> bidang pemilik
    sources = discover()
    if not sources:
        sys.exit(f"Tidak ada file playlist_* di {ROOT / '00_SumberData'}")
    cats = a.cats or default_cats or list(sources)

    # ---- 1) rencana ----
    plan = []  # (cat, sub, seq, audio_id, video)
    for cat in cats:
        if cat not in sources:
            print(f"[!] file sumber '{cat}' tidak ditemukan. Tersedia: {', '.join(sources)}")
            continue
        print(f"\n=== {cat}  ({sources[cat].name}) ===")
        items = parse_source(sources[cat])
        if not items:
            print(f"  (belum ada URL; ukuran file {sources[cat].stat().st_size} byte "
                  f"-> sudah disimpan?)")
            continue
        cands = build_candidates(items, a, cache)
        save_cache(cache)
        ok_n = sum(1 for r in rows.values() if r["category"] == cat and r["status"] == "ok")
        next_seq = 1 + max([int(r["seq"]) for r in rows.values() if r["category"] == cat] or [0])
        todo, n_short = [], 0
        for v in cands:
            owner = claimed.setdefault(v["video_id"], cat)
            if owner != cat:
                print(f"  [dobel lintas-bidang] {v['video_id']} milik '{owner}', dilewati")
                continue
            old = rows.get(v["video_id"])
            if old and old["status"] == "ok" and (ROOT / old["filepath"]).exists():
                continue
            if old and old["status"] == "failed" and a.skip_failed:
                continue
            if v["duration"] and v["duration"] < a.min_duration_min * 60:
                n_short += 1
                continue
            todo.append(v)
        if a.quota_cat:
            todo = todo[: max(0, a.quota_cat - ok_n)]
        start = len(plan)
        for v in todo:
            old = rows.get(v["video_id"])
            if old:  # coba ulang -> ID lama dipakai lagi
                sub, seq = old["subtopic"], int(old["seq"])
            else:
                sub, seq = v["sub"], next_seq
                next_seq += 1
            plan.append((cat, sub, seq, f"{cat}-{sub}-{seq:04d}", v))
        per_sub = Counter(p[1] for p in plan[start:])
        print(f"  sudah OK: {ok_n} | akan diunduh: {len(todo)} | dilewati (terlalu pendek): {n_short}")
        for s, n in per_sub.items():
            print(f"     - {s}: {n}")

    if a.max_per_run:
        plan = plan[: a.max_per_run]
    clip_s = a.clip_minutes * 60
    est_gb = sum(min(p[4]["duration"] or 600, clip_s or 10 ** 9) for p in plan) * SAMPLE_RATE * 2 / 1e9
    avg = (a.sleep_min + a.sleep_max) / 2
    pause_min = (len(plan) * avg + (len(plan) // max(1, a.batch_size)) * a.batch_pause) / 60
    print(f"\nTotal rencana: {len(plan)} video (perkiraan ukuran WAV ~ {est_gb:.1f} GB)")
    print(f"Waktu jeda saja ~ {pause_min:.0f} menit (belum termasuk waktu unduh)")
    if a.dry_run:
        for cat, sub, seq, aid, v in plan[:20]:
            print(f"  [dry] {cat}/{sub}/{aid}-{v['video_id']}.wav | {v['title'][:50]}")
        if len(plan) > 20:
            print(f"  ... +{len(plan) - 20} lainnya")
        return
    if not plan:
        print("Tidak ada yang perlu diunduh. (idempotent: semua sudah lengkap)")
        return
    if not a.yes and input("Lanjut download? [y/N] ").strip().lower() != "y":
        return

    # ---- 2) eksekusi ----
    ok = bad = consec = 0
    try:
        for i, (cat, sub, seq, audio_id, v) in enumerate(plan, 1):
            vid = v["video_id"]
            final = RAW_DIR / cat / sub / f"{audio_id}-{vid}.wav"
            if a.only_cc:
                try:
                    lic = (fetch_info(v["url"]) or {}).get("license") or ""
                except Exception:  # noqa: BLE001
                    lic = ""
                time.sleep(random.uniform(1, 3))
                if "creative commons" not in lic.lower():
                    print(f"[{i}/{len(plan)}] [skip non-CC] {vid}")
                    continue
            row = {
                "audio_id": audio_id,
                "category": cat,
                "subtopic": sub,
                "seq": seq,
                "video_id": vid,
                "title": v["title"],
                "url": v["url"],
                "source_url": v["source_url"],
                "playlist_title": v["playlist_title"],
                "other_sources": v["other_sources"],
                "duration_src_sec": v["duration"] or "",
                "sr": SAMPLE_RATE,
                "lang": "",
                "yt_dlp_version": ver,
                "downloaded_at": datetime.now().isoformat(timespec="seconds"),
            }
            failed_now, blocked = False, False
            try:
                info, wav, tmp = download(v["url"], vid, a.clip_minutes)
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(wav, final)
                shutil.rmtree(tmp, ignore_errors=True)
                dur = info.get("duration") or v["duration"] or ""
                clip = (min(clip_s, dur) if dur else clip_s) if a.clip_minutes else dur
                row.update(
                    status="ok",
                    error="",
                    filepath=final.relative_to(ROOT).as_posix(),
                    title=info.get("title") or v["title"],
                    channel=info.get("channel") or "",
                    license=info.get("license") or "",
                    duration_src_sec=dur,
                    clip_sec=clip,
                )
                ok += 1
                print(f"[{i}/{len(plan)}] [ok]    {final.name}")
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                blocked = any(h in msg.lower() for h in BLOCK_HINTS)
                row.update(status="failed", error=msg[:200], filepath="")
                failed_now = True
                bad += 1
                print(f"[{i}/{len(plan)}] [GAGAL] {vid}: {msg[:90]}")
            rows[vid] = row
            save_manifest(rows)  # simpan tiap video: aman kalau terputus

            if failed_now:
                consec += 1
                if consec >= a.max_consecutive_fails:
                    print(f"\n[STOP] {consec} kegagalan beruntun (kemungkinan rate-limit/blokir). "
                          f"Kemajuan tersimpan; coba lagi beberapa jam kemudian.")
                    break
                wait = a.cooldown if blocked else min(300, 30 * 2 ** (consec - 1))
                print(f"      jeda {wait:.0f} dtk sebelum lanjut"
                      + (" (terdeteksi pembatasan)" if blocked else ""))
                time.sleep(wait)
                continue
            consec = 0
            if i < len(plan):
                time.sleep(random.uniform(a.sleep_min, a.sleep_max))
                if i % a.batch_size == 0:
                    t = a.batch_pause * random.uniform(0.7, 1.3)
                    print(f"      -- istirahat {t:.0f} dtk (tiap {a.batch_size} video) --")
                    time.sleep(t)
    except KeyboardInterrupt:
        print("\nDihentikan manual. Kemajuan tersimpan; jalankan lagi untuk melanjutkan.")

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    save_manifest(rows)
    print(f"\nSelesai sesi ini: {ok} berhasil, {bad} gagal. "
          f"Jalankan: python scripts/audit_dataset.py")


if __name__ == "__main__":
    main()
