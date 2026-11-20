"""Lock: PID mati = boleh mengambil alih, PID hidup = ditolak."""

import os

import pytest

from audio_pipeline import scrape


def test_lock_pid_mati_langsung_diambil_alih(tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "ensure_dirs", lambda: None)
    lock = tmp_path / ".scrape.lock"
    # PID mustahil ada, umur lock masih baru
    lock.write_text("999999 2026-01-01T00:00:00", encoding="utf-8")

    scrape.acquire_lock(lock)

    assert lock.exists()
    assert lock.read_text(encoding="utf-8").startswith(str(os.getpid()))

    scrape.release_lock(lock)
    assert not lock.exists()


def test_lock_pid_hidup_ditolak(tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "ensure_dirs", lambda: None)
    lock = tmp_path / ".scrape.lock"
    lock.write_text(f"{os.getpid()} 2026-01-01T00:00:00", encoding="utf-8")

    with pytest.raises(scrape.LockBusy):
        scrape.acquire_lock(lock)
