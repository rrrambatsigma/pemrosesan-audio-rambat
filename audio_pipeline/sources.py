"""Parser file sumber ``sumber_data/playlist_<label>.txt``.

Format yang diterima::

    # komentar bebas (satu pagar)
    ## k13 | kelas 10 | Vektor
    ytsearch20:fisika kelas 10 kurikulum 2013 vektor
    ytsearch15:fisika kelas x vektor pembahasan soal
    https://www.youtube.com/playlist?list=PLxxxx

Aturan:
- ``##`` = header seksi (wajib: kurikulum, kelas, bab).
- ``#``  = komentar; TIDAK boleh jadi nama bab (bug lama).
- ``ytsearchN:`` = pencarian YouTube, N hasil.
- URL YouTube lain = playlist / video tunggal.
- Baris di luar seksi = error, bukan diam-diam masuk ``umum``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import SRC_DIR
from .registry import RegistryError, check_kelas, check_kurikulum

SECTION_RE = re.compile(
    r"^##\s*(?P<kur>[^|]+?)\s*\|\s*kelas\s*(?P<kelas>1[0-2])\s*\|\s*(?P<bab>.+?)\s*$",
    re.IGNORECASE,
)
SEARCH_RE = re.compile(r"^ytsearch(?P<n>\d{1,3}):(?P<q>.+)$", re.IGNORECASE)
YOUTUBE_RE = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE)


class SourceError(ValueError):
    """Isi file sumber tidak sesuai format."""


@dataclass(frozen=True)
class SourceItem:
    label: str
    kurikulum: str
    kelas: int
    bab: str
    kind: str          # "search" | "url"
    value: str         # query atau URL
    limit: int
    line: int

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.value.strip().lower()}"


def parse_file(path: Path, label: str) -> list[SourceItem]:
    """Baca satu file sumber; kumpulkan semua error lalu gagalkan sekaligus."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    items: list[SourceItem] = []
    errors: list[str] = []
    section: tuple[str, int, str] | None = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("##"):
            m = SECTION_RE.match(line)
            if not m:
                errors.append(
                    f"{path.name}:{lineno}: header harus "
                    f"'## <kurikulum> | kelas <10-12> | <bab>', dapat: {line[:70]}"
                )
                section = None
                continue
            try:
                kur = check_kurikulum(m["kur"])
                kelas = check_kelas(m["kelas"])
            except RegistryError as e:
                errors.append(f"{path.name}:{lineno}: {e}")
                section = None
                continue
            section = (kur, kelas, m["bab"].strip())
            continue
        if line.startswith("#"):
            continue  # komentar

        if section is None:
            errors.append(
                f"{path.name}:{lineno}: baris di luar seksi "
                f"(taruh '## kurikulum | kelas N | bab' di atasnya)"
            )
            continue

        kur, kelas, bab = section
        m = SEARCH_RE.match(line)
        if m:
            n = int(m["n"])
            if not 1 <= n <= 50:
                errors.append(f"{path.name}:{lineno}: ytsearch{N} di luar 1-50")
                continue
            items.append(SourceItem(label, kur, kelas, bab, "search",
                                    m["q"].strip(), n, lineno))
            continue
        if YOUTUBE_RE.match(line):
            url = line.split()[0].rstrip(",;")
            items.append(SourceItem(label, kur, kelas, bab, "url", url, 1, lineno))
            continue
        errors.append(
            f"{path.name}:{lineno}: bukan 'ytsearchN:' atau URL YouTube: {line[:60]}"
        )

    if errors:
        raise SourceError(
            f"{len(errors)} masalah di {path.name}:\n  " + "\n  ".join(errors[:15])
            + ("\n  ..." if len(errors) > 15 else "")
        )
    return items


def parse_label(label: str, labels=None) -> list[SourceItem]:
    from .registry import discover_files

    files = discover_files(labels)
    if label not in files:
        raise SourceError(
            f"sumber untuk label '{label}' tidak ada "
            f"(harus: {SRC_DIR / f'playlist_{label}.txt'})"
        )
    return parse_file(files[label], label)


def parse_many(labels=None, only: list[str] | None = None) -> list[SourceItem]:
    """Baca semua file sumber (atau subset label), urut nomor label."""
    from .registry import discover_files, load

    labels = labels or load()
    files = discover_files(labels)
    chosen = sorted(
        (l for l in files if not only or l in only),
        key=lambda s: labels[s].no,
    )
    missing = [l for l in (only or []) if l not in files]
    if missing:
        raise SourceError("file sumber tidak ada untuk: " + ", ".join(missing))
    out: list[SourceItem] = []
    for slug in chosen:
        out.extend(parse_file(files[slug], slug))
    return out
