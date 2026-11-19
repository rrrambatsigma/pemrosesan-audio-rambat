"""Regresi: 21 file sumber harus lengkap dan berbasis bab."""

import pytest

from audio_pipeline.config import SRC_DIR
from audio_pipeline.registry import load
from audio_pipeline.sources import parse_file

LABELS = load()


@pytest.mark.parametrize("slug", sorted(LABELS))
def test_file_lengkap(slug):
    p = SRC_DIR / f"playlist_{slug}.txt"
    assert p.exists(), f"{p.name} tidak ada"
    assert p.stat().st_size > 0, f"{p.name} 0 byte"

    items = parse_file(p, slug)
    assert len(items) >= 20, f"{slug}: hanya {len(items)} item"
    assert {i.kelas for i in items} == {10, 11, 12}
    assert {i.kurikulum for i in items} == {"k13", "kurmerdeka", "ktsp2006"}

    # seksi non-KTSP wajib menyebut bab nyata, bukan "materi kelas N"
    generik = [i.bab for i in items
               if i.kurikulum != "ktsp2006"
               and i.bab.lower().startswith("materi kelas")]
    assert not generik, f"{slug}: bab generik {generik[:3]}"


def test_tidak_ada_file_asing():
    dikenal = {f"playlist_{s}.txt" for s in LABELS}
    ada = {p.name for p in SRC_DIR.glob("playlist_*.txt")}
    assert ada == dikenal, f"file tak dikenal: {sorted(ada - dikenal)}"
