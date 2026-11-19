"""Registry 21 mata pelajaran SMA.

Sumber kebenaran label ada di ``sumber_data/registry_labels.csv``. Semua
validasi (label dikenal, kelas 10-12, kurikulum dikenal) lewat modul ini
supaya tidak ada label siluman yang masuk manifest.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .config import KELAS, KURIKULUM, REGISTRY, SRC_DIR

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class RegistryError(ValueError):
    """Registry tidak lengkap / tidak konsisten."""


@dataclass(frozen=True)
class Label:
    no: int
    label: str
    rumpun: str
    kelompok: str
    bahasa: str
    query: tuple[str, ...]

    @property
    def queries(self) -> tuple[str, ...]:
        return self.query or (self.label.replace("_", " "),)


def load(path: Path | None = None) -> dict[str, Label]:
    """Muat registry -> {slug: Label}. Gagal keras kalau ada yang salah."""
    path = Path(path or REGISTRY)
    if not path.exists():
        raise RegistryError(
            f"registry tidak ada: {path}\n"
            f"jalankan: python scripts/init_sources.py"
        )
    out: dict[str, Label] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), 2):
            slug = (row.get("label") or "").strip()
            if not slug:
                raise RegistryError(f"{path.name}:{i}: kolom 'label' kosong")
            if not SLUG_RE.match(slug):
                raise RegistryError(f"{path.name}:{i}: slug '{slug}' tidak valid")
            if slug in out:
                raise RegistryError(f"{path.name}:{i}: label ganda '{slug}'")
            try:
                no = int(row["no"])
            except (KeyError, TypeError, ValueError) as e:
                raise RegistryError(f"{path.name}:{i}: kolom 'no' salah") from e
            queries = tuple(
                q.strip() for q in (row.get("query") or "").split(";") if q.strip()
            )
            out[slug] = Label(
                no=no,
                label=slug,
                rumpun=(row.get("rumpun") or "").strip(),
                kelompok=(row.get("kelompok_tabel") or "").strip(),
                bahasa=(row.get("bahasa") or "id").strip(),
                query=queries,
            )
    if not out:
        raise RegistryError(f"{path.name}: kosong")
    no_set = sorted(l.no for l in out.values())
    if no_set != list(range(1, len(no_set) + 1)):
        raise RegistryError(f"{path.name}: nomor label harus 1..{len(no_set)}, "
                            f"ditemukan {no_set}")
    return out


def by_no(labels: dict[str, Label] | None = None) -> dict[int, Label]:
    labels = labels or load()
    return {l.no: l for l in labels.values()}


def check_label(slug: str, labels: dict[str, Label] | None = None) -> Label:
    labels = labels or load()
    if slug not in labels:
        raise RegistryError(
            f"label '{slug}' tidak ada di registry. Tersedia: "
            + ", ".join(sorted(labels))
        )
    return labels[slug]


def check_kelas(value) -> int:
    try:
        k = int(value)
    except (TypeError, ValueError):
        raise RegistryError(f"kelas harus 10/11/12, bukan {value!r}") from None
    if k not in KELAS:
        raise RegistryError(f"kelas harus 10/11/12, bukan {k}")
    return k


KUR_ALIASES = {
    "k13": "k13", "kurikulum2013": "k13", "kurikulum_2013": "k13",
    "kurtilas": "k13", "2013": "k13",
    "kurmerdeka": "kurmerdeka", "kurikulum_merdeka": "kurmerdeka",
    "kurikulummerdeka": "kurmerdeka", "merdeka": "kurmerdeka",
    "ktsp": "ktsp2006", "ktsp2006": "ktsp2006", "ktsp_2006": "ktsp2006",
    "2006": "ktsp2006",
}


def check_kurikulum(value: str) -> str:
    key = re.sub(r"[\s\-]+", "_", (value or "").strip().lower())
    key = key.replace("_", "")
    # coba alias apa adanya dulu
    for alias, canon in KUR_ALIASES.items():
        if key == alias.replace("_", ""):
            return canon
    raise RegistryError(
        f"kurikulum '{value}' tidak dikenal (pakai: {', '.join(KURIKULUM)})"
    )


def discover_files(labels: dict[str, Label] | None = None) -> dict[str, Path]:
    """Peta label -> file sumber; file asing / label tak dikenal dilaporkan."""
    labels = labels or load()
    found: dict[str, Path] = {}
    unknown: list[str] = []
    for p in sorted(SRC_DIR.glob("playlist_*.txt")):
        slug = p.stem[len("playlist_"):]
        if slug in labels:
            found[slug] = p
        else:
            unknown.append(p.name)
    if unknown:
        print(f"[!] {len(unknown)} file sumber diabaikan (label tidak ada di "
              f"registry): {', '.join(unknown[:5])}"
              + (" ..." if len(unknown) > 5 else ""))
    return found
