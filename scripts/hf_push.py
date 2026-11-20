"""Push audio yang SUDAH ada di lokal ke Hugging Face (backfill / cek).

Dipakai untuk 396 audio lama (mode raw opus) supaya ikut pindah ke HF sebagai
WAV, atau untuk mengulang upload yang gagal. Scraping baru tidak butuh script
ini â€” ``scrape.py`` sudah langsung meng-commit tiap video.

Contoh:
    # lihat rencana tanpa menyentuh apa pun
    python scripts/hf_push.py --dry-run

    # upload semua yang belum ada di HF (transcode opus -> WAV 16k mono)
    python scripts/hf_push.py --batch 20 --yes

    # cek kesesuaian manifest vs repo HF
    python scripts/hf_push.py --check

    # upload lalu hapus file opus lokal (hemat 6,7 GB)
    python scripts/hf_push.py --batch 20 --delete-local --yes
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path

import _bootstrap  # noqa: F401
from audio_pipeline import hub as hub_mod
from audio_pipeline import manifest, youtube
from audio_pipeline.config import HUB_STAGING, ROOT, setup_console


def _local_path(row: dict) -> Path | None:
    fp = str(row.get("filepath") or "")
    if not fp or fp.startswith("hf://"):
        return None
    p = Path(fp)
    if not p.is_absolute():
        p = ROOT / p
    return p if p.exists() else None


def _prepare(row: dict, local: Path, *, keep_format: bool) -> Path:
    """Kembalikan berkas yang akan di-upload (transcode opus -> WAV)."""
    if local.suffix.lower() == ".wav" or keep_format:
        return local
    out = HUB_STAGING / "push" / f"{row.get('clip_id') or local.stem}.wav"
    if not (out.exists() and out.stat().st_size > 20_000):
        youtube.to_wav(local, out, sr=16000, mono=True)
    return out


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(
        description="Upload audio lokal ke dataset Hugging Face.",
    )
    ap.add_argument("--hub-repo", default=hub_mod.resolve_repo() or None,
                    metavar="USER/REPO",
                    help="repo dataset HF (default: HF_DATASET_REPO)")
    ap.add_argument("--hub-public", action="store_true",
                    help="repo publik (default privat)")
    ap.add_argument("--batch", type=int, default=20,
                    help="berkas per commit (default 20)")
    ap.add_argument("--limit", type=int, default=0, help="batasi jumlah berkas")
    ap.add_argument("--only", nargs="+", help="label tertentu")
    ap.add_argument("--check", action="store_true",
                    help="hanya bandingkan manifest dengan isi repo HF")
    ap.add_argument("--dry-run", action="store_true",
                    help="tampilkan tujuan upload tanpa commit")
    ap.add_argument("--force", action="store_true",
                    help="upload ulang walau hub_status sudah uploaded")
    ap.add_argument("--keep-format", action="store_true",
                    help="upload apa adanya (jangan transcode ke WAV)")
    ap.add_argument("--delete-local", action="store_true",
                    help="hapus berkas lokal setelah commit sukses")
    ap.add_argument("--yes", action="store_true", help="tanpa konfirmasi")
    a = ap.parse_args(argv)

    repo = (a.hub_repo or "").strip() or (hub_mod.default_repo() or "")
    if not repo and not a.dry_run:
        sys.exit("repo HF belum diisi: --hub-repo user/nama atau HF_DATASET_REPO")
    if not repo:
        repo = "username/nama-repo"       # dry-run: hanya untuk menampilkan path

    rows = manifest.load()
    todo = [r for r in rows.values() if r.get("status") == "ok"]
    if a.only:
        todo = [r for r in todo if r.get("label") in set(a.only)]
    todo.sort(key=lambda r: (int(r.get("label_no") or 999), r.get("clip_id") or ""))

    try:
        sink = hub_mod.HubSink(repo, private=not a.hub_public,
                               batch=a.batch, keep_local=True,
                               progress=print)
    except hub_mod.HubError as e:
        sys.exit(str(e))

    # ------------------------------------------------------------------ #
    # cek
    # ------------------------------------------------------------------ #
    if a.check:
        try:
            remote = set(sink._client().list_repo_files(  # noqa: SLF001
                repo_id=sink.repo_id, repo_type="dataset"))
        except Exception as e:  # noqa: BLE001
            if "404" in str(e) or "NotFound" in type(e).__name__:
                remote = set()
            else:
                sys.exit(f"[hub] gagal membaca repo: {e}")
        per_label = Counter()
        missing, absent_local = [], []
        for r in todo:
            local = _local_path(r)
            if r.get("hub_path"):
                path = r["hub_path"]
            elif local is not None:
                target = local if (a.keep_format or
                                   local.suffix.lower() == ".wav") \
                    else local.with_suffix(".wav")
                path = sink.remote_path(r, target)
            else:
                path = sink.remote_path(r, Path("x.wav"))
            state = "uploaded" if path in remote else "belum"
            per_label[(r.get("label") or "?", state)] += 1
            if state == "belum":
                missing.append(r.get("clip_id"))
            if not local and state != "uploaded":
                absent_local.append(r.get("clip_id"))
        print(f"Repo : {sink.repo_id} ({len(remote)} berkas)")
        print(f"Baris: {len(todo)} | belum di HF: {len(missing)}")
        labels = sorted({k[0] for k in per_label})
        for label in labels:
            up = per_label[(label, "uploaded")]
            no = per_label[(label, "belum")]
            print(f"  {label:<26} uploaded {up:>4} | belum {no:>4}")
        if absent_local:
            print(f"[!] {len(absent_local)} baris belum di HF dan file lokalnya "
                  f"tidak ada (perlu scrape ulang): "
                  f"{', '.join(absent_local[:5])} ...")
        return 0 if not missing else 1

    # ------------------------------------------------------------------ #
    # rencana
    # ------------------------------------------------------------------ #
    plan = []
    skipped = no_local = 0
    for r in todo:
        if r.get("hub_status") == "uploaded" and not a.force:
            skipped += 1
            continue
        local = _local_path(r)
        if local is None:
            no_local += 1
            continue
        plan.append((r, local))
    if a.limit:
        plan = plan[: a.limit]

    total_bytes = sum(p.stat().st_size for _, p in plan)
    print(f"Repo        : {sink.repo_id} "
          f"({'publik' if a.hub_public else 'privat'})")
    print(f"Sudah di HF : {skipped} (dilewati)")
    print(f"Tanpa lokal : {no_local} (tidak bisa diupload)")
    print(f"Akan upload : {len(plan)} berkas, Â±{total_bytes / 1e9:.2f} GB sumber")

    if a.dry_run:
        for r, local in plan[:12]:
            target = local if (a.keep_format or local.suffix.lower() == ".wav") \
                else local.with_suffix(".wav")
            print(f"  {r['clip_id']:<28} {local.name:<22} -> "
                  f"{sink.remote_path(r, target)}")
        print("\n[dry-run] tidak ada commit.")
        return 0
    if not plan:
        print("\n[ok] tidak ada yang perlu diupload.")
        return 0
    if not a.yes and input("\nLanjut upload? [y/N] ").strip().lower() != "y":
        return 0

    try:
        sink.ensure_repo()
    except hub_mod.HubError as e:
        sys.exit(str(e))

    # ------------------------------------------------------------------ #
    # upload
    # ------------------------------------------------------------------ #
    uploaded = freed = failed = 0
    batch: list[tuple[dict, Path, Path]] = []   # row, original, target

    def commit_batch() -> None:
        nonlocal uploaded, freed, failed
        if not batch:
            return
        sink.flush(rows, force=True)
        for row, original, target in batch:
            if row.get("hub_status") == "uploaded":
                uploaded += 1
                if target != original:
                    freed += target.stat().st_size if target.exists() else 0
                    target.unlink(missing_ok=True)
                if a.delete_local and original.exists():
                    freed += original.stat().st_size
                    original.unlink(missing_ok=True)
            else:
                failed += 1
        batch.clear()

    for n, (row, local) in enumerate(plan, 1):
        try:
            target = _prepare(row, local, keep_format=a.keep_format)
        except youtube.YoutubeError as e:
            failed += 1
            print(f"[{n}/{len(plan)}] [GAGAL TRANSCODE] {row.get('clip_id')}: {e}")
            continue
        row["bytes"] = target.stat().st_size
        row["sha256"] = youtube.sha256_file(target)
        if target.suffix.lower() == ".wav":
            try:
                sr, ch = youtube.audio_info(target)
                row["sr"], row["channels"] = sr, ch
            except Exception:  # noqa: BLE001
                pass
        sink.enqueue(row, target)
        batch.append((row, local, target))
        if len(batch) >= a.batch:
            try:
                commit_batch()
            except hub_mod.HubError as e:
                batch.clear()
                print(f"[hub] {e}")
                if input("Lanjut? [y/N] ").strip().lower() != "y":
                    break
        if n % 25 == 0:
            print(f"[{n}/{len(plan)}] {uploaded} uploaded, {failed} gagal, "
                  f"{freed / 1e9:.2f} GB lokal dibebaskan")

    try:
        commit_batch()
    except hub_mod.HubError as e:
        print(f"[hub] commit terakhir gagal: {e}")

    manifest.save(rows)
    shutil.rmtree(HUB_STAGING / "push", ignore_errors=True)
    print("\n" + "=" * 60)
    print(f"Uploaded : {uploaded}")
    print(f"Gagal    : {failed}")
    print(f"Dibebaskan: {freed / 1e9:.2f} GB")
    print(f"Repo     : https://huggingface.co/datasets/{sink.repo_id}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
