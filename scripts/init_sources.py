"""Buat registry 21 mapel + 21 file sumber (idempoten).

Menjalankan ulang tidak menimpa file yang sudah ada (kecuali ``--force``),
jadi hasil edit manual kamu aman.

Contoh:
    python scripts/init_sources.py
    python scripts/init_sources.py --check
    python scripts/init_sources.py --force --only fisika kimia
"""

from __future__ import annotations

import argparse
import csv
import sys

import _bootstrap  # noqa: F401
from audio_pipeline.config import REGISTRY, SRC_DIR, ensure_dirs, setup_console

# no, slug, rumpun, kelompok, bahasa, display, alternatif query
META = [
    (1, "pendidikan_agama", "kewargaan_seni", "Umum / Wajib", "id",
     "Pendidikan Agama & Budi Pekerti",
     ["pendidikan agama islam", "budi pekerti", "pendidikan agama kristen"]),
    (2, "ppkn", "kewargaan_seni", "Umum / Wajib", "id",
     "Pendidikan Pancasila / PPKn",
     ["ppkn sma", "pkn sma", "pendidikan pancasila"]),
    (3, "bahasa_indonesia", "bahasa", "Umum / Wajib", "id",
     "Bahasa Indonesia",
     ["bahasa indonesia sma", "bahasa indonesia", "materi bahasa indonesia"]),
    (4, "matematika", "eksakta_teknologi", "Umum / Wajib", "id",
     "Matematika Wajib",
     ["matematika wajib sma", "matematika sma", "matematika"]),
    (5, "sejarah", "sosial_ekonomi", "Umum / Wajib", "id",
     "Sejarah Indonesia & Dunia",
     ["sejarah indonesia sma", "sejarah peminatan", "sejarah dunia sma"]),
    (6, "pjok", "kewargaan_seni", "Umum / Wajib", "id",
     "PJOK / Penjasorkes",
     ["pjok sma", "penjasorkes", "pendidikan jasmani"]),
    (7, "seni_budaya", "kewargaan_seni", "Umum / Wajib", "id",
     "Seni Budaya",
     ["seni budaya sma", "seni rupa sma", "seni musik sma"]),
    (8, "prakarya", "kewargaan_seni", "Umum / Wajib", "id",
     "Prakarya & Kewirausahaan",
     ["prakarya dan kewirausahaan", "pkwu sma", "kewirausahaan sma"]),
    (9, "fisika", "eksakta_teknologi", "IPA / MIPA", "id",
     "Fisika",
     ["fisika sma", "fisika", "fisika dasar"]),
    (10, "kimia", "eksakta_teknologi", "IPA / MIPA", "id",
     "Kimia", ["kimia sma", "kimia", "kimia dasar"]),
    (11, "biologi", "eksakta_teknologi", "IPA / MIPA", "id",
     "Biologi", ["biologi sma", "biologi", "biologi dasar"]),
    (12, "matematika_lanjut", "eksakta_teknologi", "IPA / MIPA", "id",
     "Matematika Tingkat Lanjut",
     ["matematika tingkat lanjut", "matematika peminatan", "matematika lanjut sma"]),
    (13, "ekonomi", "sosial_ekonomi", "IPS", "id",
     "Ekonomi & Akuntansi",
     ["ekonomi sma", "akuntansi sma", "ekonomi"]),
    (14, "geografi", "sosial_ekonomi", "IPS", "id",
     "Geografi", ["geografi sma", "geografi", "geografi peminatan"]),
    (15, "sosiologi", "sosial_ekonomi", "IPS", "id",
     "Sosiologi", ["sosiologi sma", "sosiologi", "sosiologi peminatan"]),
    (16, "informatika", "eksakta_teknologi", "Teknologi", "id",
     "TIK / Informatika",
     ["informatika sma", "tik sma", "informatika"]),
    (17, "antropologi", "sosial_ekonomi", "Bahasa & Budaya", "id",
     "Antropologi",
     ["antropologi sma", "antropologi", "antropologi peminatan"]),
    (18, "sastra_indonesia", "bahasa", "Bahasa & Budaya", "id",
     "Sastra Indonesia",
     ["sastra indonesia", "bahasa indonesia tingkat lanjut", "sastra sma"]),
    (19, "bahasa_inggris", "bahasa", "Umum / Bahasa", "id+en",
     "Bahasa Inggris",
     ["bahasa inggris sma", "bahasa inggris", "english sma"]),
    (20, "bahasa_inggris_lanjut", "bahasa", "Bahasa & Budaya", "id+en",
     "Bahasa Inggris Tingkat Lanjut",
     ["bahasa inggris tingkat lanjut", "english advanced sma", "bahasa inggris peminatan"]),
    (21, "bahasa_jawa", "bahasa", "Muatan Lokal", "id+jw",
     "Bahasa Jawa", ["bahasa jawa sma", "bahasa jawa", "basa jawa"]),
]

HEADER = """\
# label: {slug} | no: {no} | rumpun: {rumpun}
# mapel: {display}
# aturan: '## <kurikulum> | kelas <10-12> | <bab>' lalu 'ytsearchN:query' atau URL
# silakan tambah playlist/video manual di bawah seksi yang sesuai
"""


def render_file(no: int, slug: str, rumpun: str, display: str,
                queries: list[str]) -> str:
    lines = [HEADER.format(slug=slug, no=no, rumpun=rumpun, display=display)]
    for i, kelas in enumerate((10, 11, 12)):
        q0 = queries[0]
        q1 = queries[1 % len(queries)]
        q2 = queries[2 % len(queries)]
        lines += [
            f"## k13 | kelas {kelas} | materi kelas {kelas}",
            f"ytsearch18:{q0} kelas {kelas} kurikulum 2013",
            f"ytsearch18:{q0} kelas {kelas} k13 pembahasan soal",
            f"ytsearch15:materi {q0} kelas {kelas} semester 1",
            "",
            f"## kurmerdeka | kelas {kelas} | materi kelas {kelas}",
            f"ytsearch18:{q1} kelas {kelas} kurikulum merdeka",
            f"ytsearch18:{q1} kelas {kelas} merdeka belajar",
            f"ytsearch15:materi {q1} kelas {kelas} semester 2",
            "",
            f"## ktsp2006 | kelas {kelas} | materi kelas {kelas}",
            f"ytsearch12:{q2} kelas {kelas} ktsp",
            f"ytsearch12:{q2} kelas {kelas} ktsp 2006",
            "",
        ]
    return "\n".join(lines)


def main(argv=None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Buat registry + 21 file sumber.")
    ap.add_argument("--force", action="store_true", help="timpa file yang ada")
    ap.add_argument("--only", nargs="+", help="batasi ke label tertentu")
    ap.add_argument("--check", action="store_true",
                    help="hanya periksa kelengkapan, tidak menulis")
    a = ap.parse_args(argv)

    ensure_dirs()
    rows = [m for m in META if not a.only or m[1] in a.only]
    if a.only:
        unknown = set(a.only) - {m[1] for m in META}
        if unknown:
            sys.exit(f"label tidak dikenal: {', '.join(sorted(unknown))}")

    missing_reg = not REGISTRY.exists()
    if a.check:
        problems = []
        if missing_reg:
            problems.append(f"registry belum ada: {REGISTRY}")
        for m in META:
            p = SRC_DIR / f"playlist_{m[1]}.txt"
            if not p.exists():
                problems.append(f"sumber belum ada: {p.name}")
        for p in problems:
            print("[x]", p)
        print("OK" if not problems else f"{len(problems)} masalah")
        return

    if missing_reg or a.force:
        tmp = REGISTRY.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["no", "label", "rumpun", "kelompok_tabel", "bahasa", "query"])
            for no, slug, rumpun, kelompok, bahasa, _display, q in META:
                w.writerow([no, slug, rumpun, kelompok, bahasa, ";".join(q)])
        tmp.replace(REGISTRY)
        print(f"[ok] registry ditulis: {REGISTRY.name} (21 label)")
    else:
        print(f"[=] registry sudah ada, tidak ditimpa: {REGISTRY.name}")

    n_new = n_skip = 0
    for no, slug, rumpun, _kelompok, _bahasa, display, queries in rows:
        p = SRC_DIR / f"playlist_{slug}.txt"
        if p.exists() and not a.force:
            n_skip += 1
            continue
        body = render_file(no, slug, rumpun, display, queries)
        tmp = p.with_suffix(".txt.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(p)
        n_new += 1

    total = sum(1 for _ in SRC_DIR.glob("playlist_*.txt"))
    print(f"[ok] file sumber: {n_new} dibuat, {n_skip} dilewati, {total} total")
    print("Langkah berikut: python scripts/sources_audit.py --target-per-label 143")


if __name__ == "__main__":
    main()
