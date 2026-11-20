"""Sink Hugging Face Hub: audio hasil scraping langsung di-commit ke HF.

Alur:
    unduh/potong ke ``data/_tmp/hub/<video_id>/`` -> enqueue -> commit
    -> hapus file sementara (kalau ``keep_local=False``).

Commit dibuat **per batch** (default 1 video) dan selalu menyertakan
``manifest.csv`` terbaru, jadi repo HF menyimpan provenans yang sama dengan
laptop. Bila commit gagal, baris manifest ditandai ``hub_status=failed`` dan
file sementara dibiarkan supaya bisa diulang tanpa mengunduh ulang.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from . import manifest as manifest_mod
from .config import (HUB_DEFAULT_NAME, HUB_MANIFEST_NAME, HUB_PREFIX,
                     HUB_RETRIES, HUB_TOKEN_KEYS, TMP_DIR, load_env)


class HubError(RuntimeError):
    """Token/repo/commit ke Hugging Face gagal."""


# --------------------------------------------------------------------------- #
# konfigurasi dari environment / .env
# --------------------------------------------------------------------------- #
def resolve_token() -> str | None:
    load_env()
    for key in HUB_TOKEN_KEYS:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


def resolve_repo() -> str:
    load_env()
    return (os.environ.get("HF_DATASET_REPO") or "").strip()


def default_repo() -> str | None:
    """Tebak repo dari akun token: ``<username>/audio-sma-21-mapel-raw``."""
    token = resolve_token()
    if not token:
        return None
    try:
        from huggingface_hub import HfApi

        name = (HfApi(token=token).whoami() or {}).get("name")
    except Exception:  # noqa: BLE001 - offline / token salah
        return None
    return f"{name}/{HUB_DEFAULT_NAME}" if name else None


def _retryable(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (408, 429, 500, 502, 503, 504):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in (
        "429", "too many", "rate limit", "timeout", "timed out",
        "connection", "temporarily unavailable",
    ))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# sink
# --------------------------------------------------------------------------- #
class HubSink:
    """Kumpulkan berkas lalu commit ke satu repo dataset HF."""

    def __init__(self, repo_id: str, *, token: str | None = None,
                 private: bool = True, prefix: str = HUB_PREFIX,
                 batch: int = 1, keep_local: bool = False,
                 verify: bool = False, retries: int = HUB_RETRIES,
                 api=None, progress=print) -> None:
        if not repo_id or "/" not in repo_id:
            raise HubError(
                "nama repo HF harus berbentuk 'username/nama-repo' "
                f"(dapat: {repo_id!r})"
            )
        self.repo_id = repo_id.strip()
        self.token = (token or resolve_token() or "").strip() or None
        self.private = private
        self.prefix = prefix.strip("/")
        self.batch = max(1, int(batch))
        self.keep_local = keep_local
        self.verify = verify
        self.retries = max(1, int(retries))
        self.api = api
        self.progress = progress

        self._queue: list[tuple[dict, Path]] = []
        self._remote: set[str] | None = None
        self.uploaded = 0
        self.uploaded_bytes = 0
        self.failed = 0

    # -- koneksi ---------------------------------------------------------- #
    def _client(self):
        if self.api is None:
            try:
                from huggingface_hub import HfApi
            except ImportError as e:  # pragma: no cover - env tanpa paket
                raise HubError(
                    "huggingface_hub belum terpasang: "
                    "pip install -r requirements.txt"
                ) from e
            if not self.token:
                raise HubError(
                    "token HF tidak ditemukan. Isi HF_API_KEY (atau HF_TOKEN) "
                    "di file .env atau environment."
                )
            self.api = HfApi(token=self.token)
        return self.api

    def ensure_repo(self) -> str:
        """Buat repo kalau belum ada; kembalikan URL-nya."""
        api = self._client()
        last_error: Exception | None = None
        me: dict = {}
        for attempt in range(1, self.retries + 1):
            try:
                me = api.whoami() or {}
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                if not _retryable(e) or attempt == self.retries:
                    raise HubError(
                        f"token HF ditolak ({type(e).__name__}: {e}). "
                        "Periksa HF_API_KEY di .env."
                    ) from e
                time.sleep(min(60.0, 3.0 * 2 ** (attempt - 1)))
        user = me.get("name") or "?"

        for attempt in range(1, self.retries + 1):
            try:
                api.create_repo(repo_id=self.repo_id, repo_type="dataset",
                                private=self.private, exist_ok=True)
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                if not _retryable(e) or attempt == self.retries:
                    raise HubError(
                        f"tidak bisa menyiapkan repo {self.repo_id} sebagai "
                        f"{user}: {type(e).__name__}: {e}"
                    ) from e
                self.progress(f"[hub] koneksi putus saat membuat repo, "
                              f"ulang ({attempt}/{self.retries}) ...")
                time.sleep(min(60.0, 3.0 * 2 ** (attempt - 1)))

        url = f"https://huggingface.co/datasets/{self.repo_id}"
        self.progress(f"[hub] repo {'privat' if self.private else 'publik'}: {url}")
        return url

    # -- path ------------------------------------------------------------- #
    def remote_path(self, row: dict, local: str | Path) -> str:
        label = row.get("label") or "lain"
        try:
            kelas = int(row.get("kelas") or 0)
        except (TypeError, ValueError):
            kelas = 0
        kur = row.get("kurikulum") or "umum"
        clip_id = row.get("clip_id") or Path(local).stem
        suffix = Path(local).suffix or ".wav"
        return f"{self.prefix}/{label}/{kelas}_{kur}/{clip_id}{suffix}"

    def file_uri(self, path_in_repo: str) -> str:
        return f"hf://datasets/{self.repo_id}/{path_in_repo}"

    def remote_has(self, path_in_repo: str) -> bool:
        if self._remote is None:
            api = self._client()
            try:
                self._remote = set(api.list_repo_files(
                    repo_id=self.repo_id, repo_type="dataset"))
            except Exception:  # noqa: BLE001 - repo kosong / offline
                self._remote = set()
        return path_in_repo in self._remote

    def reconcile(self, rows: dict[str, dict]) -> int:
        """Tandai uploaded bila berkasnya ternyata sudah ada di repo.

        Berguna kalau manifest lokal hilang/tertimpa: ``--hub-verify`` tidak
        akan mengunduh ulang audio yang sebenarnya sudah aman di HF.
        """
        if not self.verify:
            return 0
        fixed = 0
        for row in rows.values():
            if row.get("status") != "ok" or row.get("hub_status") == "uploaded":
                continue
            path = str(row.get("hub_path") or "")
            if not path or not self.remote_has(path):
                continue
            row["hub_status"] = "uploaded"
            row["hub_repo"] = self.repo_id
            row["filepath"] = self.file_uri(path)
            fixed += 1
        if fixed:
            manifest_mod.save(rows)
            self.progress(f"[hub] {fixed} baris dipulihkan dari repo (verify)")
        return fixed

    # -- antrean ---------------------------------------------------------- #
    @property
    def pending(self) -> int:
        return len(self._queue)

    def enqueue(self, row: dict, local: str | Path) -> None:
        local = Path(local)
        row["hub_repo"] = self.repo_id
        row["hub_path"] = self.remote_path(row, local)
        row["hub_status"] = "pending"
        row["hub_at"] = _now()
        row["hub_bytes"] = local.stat().st_size if local.exists() else 0
        self._queue.append((row, local))

    # -- commit ----------------------------------------------------------- #
    def flush(self, rows: dict[str, dict], *, force: bool = False) -> bool:
        """Commit antrean ke HF. True kalau antrean kosong / berhasil."""
        if not self._queue:
            return True
        if not force and len(self._queue) < self.batch:
            return True

        from huggingface_hub import CommitOperationAdd

        queue, self._queue = self._queue, []
        api = self._client()

        # Manifest yang ikut di-commit sudah berstatus uploaded; kalau commit
        # gagal statusnya dikembalikan ke failed (tidak pernah bohong di lokal).
        for row, local in queue:
            row["_filepath_before"] = row.get("filepath") or ""
            row["hub_status"] = "uploaded"
            row["hub_at"] = _now()
            row["hub_bytes"] = local.stat().st_size if local.exists() else 0
            row["filepath"] = self.file_uri(row["hub_path"])

        staging_manifest = TMP_DIR / "manifest.upload.csv"
        manifest_mod.save(rows, staging_manifest)

        operations = [
            CommitOperationAdd(path_in_repo=row["hub_path"],
                               path_or_fileobj=str(local))
            for row, local in queue
        ]
        operations.append(CommitOperationAdd(
            path_in_repo=HUB_MANIFEST_NAME,
            path_or_fileobj=str(staging_manifest),
        ))
        labels = {row.get("label") or "?" for row, _ in queue}
        message = f"raw: {len(queue)} berkas ({', '.join(sorted(labels))})"

        commit = None
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                commit = api.create_commit(
                    repo_id=self.repo_id, repo_type="dataset",
                    operations=operations, commit_message=message,
                )
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                if not _retryable(e) or attempt == self.retries:
                    break
                wait = min(300.0, 5.0 * 2 ** (attempt - 1))
                self.progress(f"[hub] commit ditunda {wait:.0f} dtk "
                              f"({type(e).__name__}) — percobaan "
                              f"{attempt}/{self.retries}")
                time.sleep(wait)

        if commit is None:
            for row, _ in queue:
                row["hub_status"] = "failed"
                row["hub_at"] = _now()
                if row.get("_filepath_before"):
                    row["filepath"] = row["_filepath_before"]
            self.failed += len(queue)
            manifest_mod.save(rows)
            raise HubError(
                f"commit ke {self.repo_id} gagal setelah {self.retries} "
                f"percobaan: {type(last_error).__name__}: {last_error}"
            ) from last_error

        oid = str(getattr(commit, "oid", "") or "")
        freed = 0
        for row, local in queue:
            row["hub_commit"] = oid
            self.uploaded += 1
            self.uploaded_bytes += int(row.get("hub_bytes") or 0)
            if not self.keep_local and local.exists():
                freed += local.stat().st_size
                local.unlink(missing_ok=True)
        manifest_mod.save(rows)
        self.progress(
            f"[hub] commit {oid[:8] or '(ok)'}: +{len(queue)} berkas "
            f"({self.uploaded} total, {self.uploaded_bytes / 1e9:.2f} GB)"
            + (f", lokal dibebaskan {freed / 1e6:.1f} MB" if freed else "")
        )
        return True

    # -- ringkasan -------------------------------------------------------- #
    def counters(self) -> dict:
        return {"hub_uploaded": self.uploaded,
                "hub_upload_bytes": self.uploaded_bytes,
                "hub_failed": self.failed}
