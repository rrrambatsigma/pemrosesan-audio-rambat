"""Orkestrator akuisisi: kandidat -> audio sumber -> klip WAV -> manifest.

Sifat **idempoten**: menjalankan ulang perintah yang sama tidak mengunduh,
memotong, atau menulis ulang apa pun yang sudah selesai.

Aturan yang menjaminnya:
1. Rencana deterministic (urutan tetap, seed dari ``video_id``).
2. Video yang sudah punya klip lengkap dilewati **tanpa** menyentuh jaringan.
3. Audio sumber di ``data/source/<id>.opus`` dipakai ulang bila ada.
4. ``clip_id`` diturunkan dari (label, kelas, kurikulum, seq) dan dipakai
   sebagai primary key; baris lama dipakai ulang, bukan diduplikasi.
5. Manifest/failures hanya ditulis kalau ada perubahan.
6. Satu proses saja boleh jalan (lock file).
"""

from __future__ import annotations

import os
import random
import shutil
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import youtube
from .config import (CLIPS_PER_VIDEO, CLIP_SEC, INTRO_SKIP_S, LOCK_FILE,
                     LOCK_STALE_H, MIN_VIDEO_SEC, OUTRO_SKIP_S, RAW_DIR,
                     SAMPLE_RATE, TMP_DIR, ensure_dirs)
from .manifest import (clips_of_video, load, load_failures, make_clip_id,
                       next_seq, record_failure, save, save_failures)
from .registry import Label
from .search import Candidate


class LockBusy(RuntimeError):
    """Ada proses scrape lain yang masih berjalan."""


# --------------------------------------------------------------------------- #
# lock (satu penulis)
# --------------------------------------------------------------------------- #
PID_STILL_ACTIVE = 259  # GetExitCodeProcess: proses belum selesai


def _pid_alive(pid: int) -> bool:
    """True kalau PID masih hidup (tanpa dependensi tambahan)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == PID_STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def acquire_lock(path: Path = LOCK_FILE, stale_hours: float = LOCK_STALE_H) -> None:
    ensure_dirs()
    if path.exists():
        age_h = (time.time() - path.stat().st_mtime) / 3600
        pid = _lock_pid(path)
        if pid == 0 and age_h * 3600 < 60:
            raise LockBusy(
                f"{path.name} baru dibuat (isi belum lengkap) — coba lagi sebentar."
            )
        alive = _pid_alive(pid)
        if alive and age_h < stale_hours:
            raise LockBusy(
                f"{path.name} masih dipegang PID {pid} (umur {age_h:.1f} jam) — "
                f"proses lain masih berjalan. Tunggu atau hentikan proses itu."
            )
        why = (f"PID {pid} sudah tidak aktif" if pid and not alive
               else f"umur {age_h:.1f} jam")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            raise LockBusy(f"tidak bisa mengambil alih {path.name}: {e}") from e
        print(f"[i] lock basi ({why}) diambil alih")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}"
                 .encode())
    finally:
        os.close(fd)


def touch_lock(path: Path = LOCK_FILE) -> None:
    """Heartbeat: segarkan mtime lock selama sesi berjalan."""
    try:
        if _lock_pid(path) != os.getpid():
            return
        os.utime(path, None)
    except OSError:
        pass


def release_lock(path: Path = LOCK_FILE) -> None:
    path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# berkas klip
# --------------------------------------------------------------------------- #
def clip_path(label: str, kelas: int, kur: str, clip_id: str) -> Path:
    return RAW_DIR / label / f"{int(kelas)}_{kur}" / f"{clip_id}.wav"


def _file_ok(row: dict | None) -> bool:
    """Cek murah: file ada dan ukurannya sama dengan catatan manifest."""
    if not row or row.get("status") != "ok":
        return False
    p = Path(row.get("filepath") or "")
    if not p.is_absolute():
        from .config import ROOT
        p = ROOT / p
    if not p.exists():
        return False
    try:
        expected = int(row.get("bytes") or 0)
    except ValueError:
        expected = 0
    return not expected or p.stat().st_size == expected


def _video_complete(rows: dict[str, dict], video_id: str, n_clips: int) -> bool:
    have = clips_of_video(rows, video_id)
    if len(have) < n_clips:
        return False
    return all(_file_ok(have.get(i)) for i in range(n_clips))


# --------------------------------------------------------------------------- #
# satu video
# --------------------------------------------------------------------------- #
def process_video(cand: Candidate, meta: Label, rows: dict[str, dict],
                  seq_state: dict[str, int], *, clips_per_video: int,
                  clip_sec: float, browser: str | None,
                  only_cc: bool = False, keep_source: bool = True,
                  refresh_probe: bool = False,
                  verify_sha: bool = False,
                  raw_only: bool = False) -> dict:
    """Unduh/potong satu video. Mengembalikan statistik kecil."""
    vid = cand.video_id
    stat = {"video": vid, "skipped": False, "new_clips": 0,
            "reused_clips": 0, "failed": False, "error": ""}

    expected = 1 if raw_only else clips_per_video
    if _video_complete(rows, vid, expected):
        stat["skipped"] = True
        return stat

    try:
        info = youtube.probe(vid, refresh=refresh_probe, browser=browser)
    except youtube.YoutubeError as e:
        stat.update(failed=True, error=str(e))
        return stat

    if only_cc and "creative commons" not in (info.get("license") or "").lower():
        stat.update(failed=True, error="bukan CC")
        return stat

    duration = info.get("duration") or cand.duration or 0
    if duration and duration < MIN_VIDEO_SEC:
        stat.update(failed=True, error=f"terlalu pendek ({duration} s)")
        return stat

    existing = clips_of_video(rows, vid)

    if raw_only:
        # satu berkas audio utuh per video, tanpa segmentasi
        old = existing.get(0)
        if old and _file_ok(old):
            stat["reused_clips"] = 1
            return stat
        try:
            src = youtube.download_source(vid, browser=browser, raw=True)
        except youtube.YoutubeError as e:
            stat.update(failed=True, error=str(e))
            return stat
        if old and old.get("clip_id"):
            clip_id = old["clip_id"]
        else:
            seq = seq_state.get(cand.label, 1)
            seq_state[cand.label] = seq + 1
            clip_id = make_clip_id(cand.label, cand.kelas, cand.kurikulum, seq)
        row = _row(cand, meta, info, clip_id, 0, 0, duration,
                   status="ok", error="", filepath=src.as_posix(),
                   size=src.stat().st_size, sha=youtube.sha256_file(src))
        row["url"] = row["video_url"]          # utuh, bukan deep link ?t=
        row["clip_sec"] = round(float(duration or 0), 2)
        row["sr"] = ""                          # sample rate asli (opus)
        row["channels"] = ""
        rows[clip_id] = row
        stat["new_clips"] = 1
        return stat

    windows = youtube.pick_windows(duration, n=clips_per_video, clip_sec=clip_sec,
                                   intro=INTRO_SKIP_S, outro=OUTRO_SKIP_S,
                                   seed=vid)
    if not windows:
        stat.update(failed=True, error="tidak ada jendela layak")
        return stat

    try:
        src = youtube.download_source(vid, browser=browser)
    except youtube.YoutubeError as e:
        stat.update(failed=True, error=str(e))
        return stat

    for i, start in enumerate(windows):
        old = existing.get(i)
        if old and _file_ok(old) and not verify_sha:
            stat["reused_clips"] += 1
            continue
        if old and _file_ok(old) and verify_sha:
            p = Path(old["filepath"])
            if not p.is_absolute():
                from .config import ROOT
                p = ROOT / p
            if youtube.sha256_file(p) == old.get("sha256"):
                stat["reused_clips"] += 1
                continue

        if old and old.get("clip_id"):
            clip_id = old["clip_id"]
        else:
            seq = seq_state.get(cand.label, 1)
            seq_state[cand.label] = seq + 1
            clip_id = make_clip_id(cand.label, cand.kelas, cand.kurikulum, seq)

        out = clip_path(cand.label, cand.kelas, cand.kurikulum, clip_id)
        try:
            size, real_sec = youtube.cut(src, out, start, clip_sec)
        except youtube.YoutubeError as e:
            rows[clip_id] = _row(cand, meta, info, clip_id, i, start, clip_sec,
                                 status="failed", error=str(e)[:200],
                                 filepath="", size=0, sha="")
            stat.update(failed=True, error=str(e))
            break

        rows[clip_id] = _row(
            cand, meta, info, clip_id, i, start, real_sec,
            status="ok", error="", filepath=out.as_posix(),
            size=size, sha=youtube.sha256_file(out),
        )
        stat["new_clips"] += 1

    if not keep_source:
        youtube.source_path(vid).unlink(missing_ok=True)

    if stat["new_clips"] == 0 and stat["reused_clips"] < len(windows):
        stat.update(failed=True, error=stat["error"] or "tidak ada klip baru")
    return stat


def _row(cand: Candidate, meta: Label, info: dict, clip_id: str, index: int,
         start: float, clip_sec: float, *, status: str, error: str,
         filepath: str, size: int, sha: str) -> dict:
    from .config import ROOT

    fp = ""
    if filepath:
        try:
            fp = Path(filepath).relative_to(ROOT).as_posix()
        except ValueError:
            fp = filepath
    return {
        "clip_id": clip_id,
        "label_no": meta.no,
        "label": cand.label,
        "rumpun": meta.rumpun,
        "bab": cand.bab,
        "kelas": cand.kelas,
        "kurikulum": cand.kurikulum,
        "video_id": cand.video_id,
        "clip_index": index,
        "clip_start_sec": int(round(start)),
        "clip_sec": round(float(clip_sec), 2),
        "status": status,
        "error": error,
        "title": (info.get("title") or cand.title or ""),
        "channel": (info.get("channel") or cand.channel or ""),
        "channel_id": (info.get("channel_id") or cand.channel_id or ""),
        "channel_url": (info.get("channel_url") or cand.channel_url or ""),
        "license": info.get("license") or "",
        "lang": meta.bahasa,
        "lang_detected": "",
        "duration_src_sec": info.get("duration") or "",
        "url": f"https://youtu.be/{cand.video_id}?t={int(round(start))}",
        "video_url": f"https://www.youtube.com/watch?v={cand.video_id}",
        "source_url": cand.source_url,
        "source_kind": cand.source_kind,
        "source_query": cand.source_query,
        "sr": SAMPLE_RATE,
        "channels": 1,
        "filepath": fp,
        "bytes": size,
        "sha256": sha,
        "qc_gate": "",
        "qc_score": "",
        "qc_flags": "",
        "reviewed": "",
        "yt_dlp_version": info.get("yt_dlp_version") or "",
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# sesi
# --------------------------------------------------------------------------- #
def run(plan: list[Candidate], labels: dict[str, Label], *,
        clips_per_video: int = CLIPS_PER_VIDEO, clip_sec: float = CLIP_SEC,
        browser: str | None = None, max_per_run: int = 0,
        only_cc: bool = False, keep_source: bool = True,
        retry_failed: bool = False, refresh_probe: bool = False,
        verify_sha: bool = False, sleep: tuple[float, float] = (4.0, 10.0),
        batch_size: int = 25, batch_pause: float = 90.0,
        cooldown: float = 300.0, max_consecutive_fails: int = 5,
        progress=print, raw_only: bool = False) -> dict:
    """Jalankan satu sesi. Aman dihentikan kapan saja (Ctrl+C)."""
    ensure_dirs()
    rows = load()
    failures = load_failures()
    # bersihkan catatan gagal untuk video yang ternyata sudah selesai
    done_now = {r["video_id"] for r in rows.values()
                if r.get("status") == "ok"}
    stale = [vid for vid in failures if vid in done_now]
    for vid in stale:
        failures.pop(vid, None)
    if stale:
        save_failures(failures)
        progress(f"[i] {len(stale)} catatan gagal lama dibersihkan")
    if not retry_failed:
        blocked = set(failures)
    else:
        blocked = set()

    seq_state: dict[str, int] = {slug: next_seq(rows, slug) for slug in labels}
    stats = Counter()
    stats["videos_planned"] = len(plan)
    consec = 0

    for n, cand in enumerate(plan, 1):
        touch_lock()
        if max_per_run and stats["videos_done"] >= max_per_run:
            progress(f"[i] batas --max-per-run {max_per_run} tercapai")
            break
        if cand.video_id in blocked:
            stats["videos_failed_before"] += 1
            continue

        try:
            st = process_video(
                cand, labels[cand.label], rows, seq_state,
                clips_per_video=clips_per_video, clip_sec=clip_sec,
                browser=browser, only_cc=only_cc, keep_source=keep_source,
                refresh_probe=refresh_probe, verify_sha=verify_sha,
                raw_only=raw_only,
            )
        except KeyboardInterrupt:
            progress("\n[STOP] dihentikan manual; kemajuan disimpan.")
            break
        except Exception as e:  # noqa: BLE001
            st = {"video": cand.video_id, "failed": True,
                  "error": f"{type(e).__name__}: {e}", "new_clips": 0,
                  "reused_clips": 0, "skipped": False}

        stats["videos_done"] += 1
        if st["failed"]:
            stats["videos_failed"] += 1
            record_failure(failures, cand.video_id, cand.label, "process",
                           st["error"])
            save_failures(failures)
            progress(f"[{n}/{len(plan)}] [GAGAL] {cand.label:<20} "
                     f"{cand.video_id}  {st['error'][:80]}")
            consec += 1
            if consec >= max_consecutive_fails:
                progress(f"[STOP] {consec} gagal beruntun — kemungkinan "
                         f"pemblokiran. Coba lagi beberapa jam lagi.")
                break
            blocked_hit = any(h in st["error"].lower() for h in
                              ("429", "too many", "sign in", "bot", "403"))
            time.sleep(cooldown if blocked_hit else min(300, 30 * 2 ** (consec - 1)))
            continue

        consec = 0
        stats["videos_ok"] += 1
        if failures.pop(cand.video_id, None) is not None:
            save_failures(failures)
        stats["clips_new"] += st["new_clips"]
        stats["clips_reused"] += st["reused_clips"]
        if st["skipped"]:
            stats["videos_skipped"] += 1
            continue

        save(rows)
        if st["new_clips"]:
            progress(f"[{n}/{len(plan)}] [ok] {cand.label:<20} {cand.video_id} "
                     f"+{st['new_clips']} klip "
                     f"({stats['clips_new']}/{stats['videos_planned'] * clips_per_video})")
        if n < len(plan):
            time.sleep(random.uniform(*sleep))
            if batch_size and n % batch_size == 0:
                pause = batch_pause * random.uniform(0.7, 1.3)
                progress(f"      -- istirahat {pause:.0f} dtk --")
                time.sleep(pause)

    save(rows)
    save_failures(failures)
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    keys = ("videos_planned", "videos_done", "videos_ok", "videos_skipped",
            "videos_failed", "videos_failed_before", "clips_new", "clips_reused")
    return {k: int(stats.get(k, 0)) for k in keys}
