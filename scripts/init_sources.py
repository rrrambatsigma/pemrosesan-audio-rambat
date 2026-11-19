"""Generator file sumber v2: query berbasis BAB kurikulum (bukan "materi kelas N").

Cara pakai (dari root proyek):
    copy C:\\Users\\Asus\\.opencode\\plan\\init_sources_v2.py scripts\\init_sources.py
    C:\\Users\\Asus\\miniconda3\\envs\\audio-ds\\python.exe scripts\\init_sources.py --force

Yang berubah dibanding versi lama:
- 21 label x 3 kelas x 3-4 bab = 189 seksi bab nyata (Vektor, Fluida, Tembang Macapat, ...)
- 33 query/label (693 total) vs 24/label (504) sebelumnya
- `bab` di manifest jadi topik yang berguna, bukan "materi kelas 10"
- file 0 byte otomatis diisi walau tanpa --force
"""

from __future__ import annotations

import argparse
import csv
import re
import sys

import _bootstrap  # noqa: F401
from audio_pipeline.config import REGISTRY, SRC_DIR, TARGET_PER_LABEL, ensure_dirs, setup_console

META = [
    {
        "no": 1, "slug": "pendidikan_agama", "rumpun": "kewargaan_seni",
        "kelompok": "Umum / Wajib", "bahasa": "id",
        "display": "Pendidikan Agama & Budi Pekerti",
        "q": ["pendidikan agama islam", "budi pekerti", "pendidikan agama"],
        "topics": {
            10: ["Akidah Akhlak", "Al-Qur'an Hadis", "Sejarah Kebudayaan Islam"],
            11: ["Iman kepada Rasul", "Fikih Ibadah", "Akhlak Terpuji"],
            12: ["Iman kepada Hari Akhir", "Hukum Islam", "Peradaban Islam"],
        },
    },
    {
        "no": 2, "slug": "ppkn", "rumpun": "kewargaan_seni",
        "kelompok": "Umum / Wajib", "bahasa": "id",
        "display": "Pendidikan Pancasila / PPKn",
        "q": ["ppkn sma", "pkn sma", "pendidikan pancasila"],
        "ktsp_q": "pkn",
        "topics": {
            10: ["Pancasila dan UUD 1945", "Hak dan Kewajiban Warga Negara",
                 "Bhinneka Tunggal Ika"],
            11: ["Demokrasi dan HAM", "Sistem Hukum Indonesia",
                 "Wawasan Nusantara"],
            12: ["Ketahanan Nasional", "Globalisasi", "Demokrasi Pancasila"],
        },
    },
    {
        "no": 3, "slug": "bahasa_indonesia", "rumpun": "bahasa",
        "kelompok": "Umum / Wajib", "bahasa": "id",
        "display": "Bahasa Indonesia",
        "q": ["bahasa indonesia sma", "bahasa indonesia", "materi bahasa indonesia"],
        "topics": {
            10: ["Teks Laporan Hasil Observasi", "Teks Eksposisi", "Teks Anekdot"],
            11: ["Teks Prosedur", "Teks Eksplanasi", "Teks Ceramah"],
            12: ["Teks Editorial", "Teks Cerita Sejarah", "Kritik dan Esai"],
        },
    },
    {
        "no": 4, "slug": "matematika", "rumpun": "eksakta_teknologi",
        "kelompok": "Umum / Wajib", "bahasa": "id",
        "display": "Matematika Wajib",
        "q": ["matematika wajib sma", "matematika sma", "matematika"],
        "topics": {
            10: ["Vektor", "Fungsi Kuadrat", "Trigonometri Dasar"],
            11: ["Barisan dan Deret", "Limit Fungsi", "Turunan"],
            12: ["Integral", "Peluang", "Program Linear"],
        },
    },
    {
        "no": 5, "slug": "sejarah", "rumpun": "sosial_ekonomi",
        "kelompok": "Umum / Wajib", "bahasa": "id",
        "display": "Sejarah Indonesia & Dunia",
        "q": ["sejarah indonesia sma", "sejarah peminatan", "sejarah sma"],
        "topics": {
            10: ["Konsep Sejarah dan Periodisasi", "Peradaban Dunia Kuno",
                 "Kerajaan Hindu Buddha"],
            11: ["Kolonialisme dan Imperialisme", "Pergerakan Nasional",
                 "Pendudukan Jepang"],
            12: ["Kemerdekaan Indonesia", "Orde Baru", "Reformasi"],
        },
    },
    {
        "no": 6, "slug": "pjok", "rumpun": "kewargaan_seni",
        "kelompok": "Umum / Wajib", "bahasa": "id",
        "display": "PJOK / Penjasorkes",
        "q": ["pjok sma", "penjasorkes", "pendidikan jasmani"],
        "topics": {
            10: ["Sepak Bola dan Bola Voli", "Atletik", "Kebugaran Jasmani"],
            11: ["Bola Basket", "Bulu Tangkis", "Senam Lantai"],
            12: ["Renang", "Pencak Silat", "Pola Hidup Sehat"],
        },
    },
    {
        "no": 7, "slug": "seni_budaya", "rumpun": "kewargaan_seni",
        "kelompok": "Umum / Wajib", "bahasa": "id",
        "display": "Seni Budaya",
        "q": ["seni budaya sma", "seni budaya", "materi seni budaya"],
        "topics": {
            10: ["Seni Rupa Dua Dimensi", "Seni Musik Tradisional", "Seni Tari"],
            11: ["Seni Rupa Tiga Dimensi", "Musik Barat", "Teater"],
            12: ["Pameran Seni Rupa", "Pertunjukan Musik", "Kritik Seni"],
        },
    },
    {
        "no": 8, "slug": "prakarya", "rumpun": "kewargaan_seni",
        "kelompok": "Umum / Wajib", "bahasa": "id",
        "display": "Prakarya & Kewirausahaan",
        "q": ["prakarya dan kewirausahaan", "pkwu sma", "prakarya sma"],
        "topics": {
            10: ["Kerajinan Bahan Lunak", "Budidaya Tanaman", "Pengolahan Makanan"],
            11: ["Kerajinan Bahan Keras", "Budidaya Ikan", "Rekayasa Teknologi"],
            12: ["Wirausaha Kerajinan", "Pengolahan Pangan", "Rekayasa Produk"],
        },
    },
    {
        "no": 9, "slug": "fisika", "rumpun": "eksakta_teknologi",
        "kelompok": "IPA / MIPA", "bahasa": "id",
        "display": "Fisika",
        "q": ["fisika sma", "fisika", "fisika dasar"],
        "topics": {
            10: ["Besaran dan Pengukuran", "Vektor", "Gerak Lurus", "Hukum Newton"],
            11: ["Fluida", "Suhu dan Kalor", "Gelombang", "Termodinamika"],
            12: ["Listrik Statis", "Induksi Elektromagnetik", "Fisika Modern"],
        },
    },
    {
        "no": 10, "slug": "kimia", "rumpun": "eksakta_teknologi",
        "kelompok": "IPA / MIPA", "bahasa": "id",
        "display": "Kimia",
        "q": ["kimia sma", "kimia", "kimia dasar"],
        "topics": {
            10: ["Struktur Atom", "Ikatan Kimia", "Hukum Dasar Kimia", "Larutan"],
            11: ["Asam Basa", "Laju Reaksi", "Kesetimbangan Kimia"],
            12: ["Sifat Koligatif Larutan", "Redoks dan Elektrokimia",
                 "Kimia Organik"],
        },
    },
    {
        "no": 11, "slug": "biologi", "rumpun": "eksakta_teknologi",
        "kelompok": "IPA / MIPA", "bahasa": "id",
        "display": "Biologi",
        "q": ["biologi sma", "biologi", "biologi dasar"],
        "topics": {
            10: ["Keanekaragaman Hayati", "Klasifikasi Makhluk Hidup",
                 "Virus dan Bakteri"],
            11: ["Sistem Organ Manusia", "Jaringan Tumbuhan", "Sistem Imun"],
            12: ["Genetika", "Bioteknologi", "Evolusi"],
        },
    },
    {
        "no": 12, "slug": "matematika_lanjut", "rumpun": "eksakta_teknologi",
        "kelompok": "IPA / MIPA", "bahasa": "id",
        "display": "Matematika Tingkat Lanjut",
        "q": ["matematika tingkat lanjut", "matematika peminatan", "matematika lanjut"],
        "topics": {
            10: ["Fungsi Eksponen dan Logaritma", "Sistem Persamaan",
                 "Polinomial"],
            11: ["Matriks", "Vektor Tingkat Lanjut", "Transformasi Geometri"],
            12: ["Limit Tingkat Lanjut", "Turunan Tingkat Lanjut",
                 "Integral Tingkat Lanjut"],
        },
    },
    {
        "no": 13, "slug": "ekonomi", "rumpun": "sosial_ekonomi",
        "kelompok": "IPS", "bahasa": "id",
        "display": "Ekonomi & Akuntansi",
        "q": ["ekonomi sma", "akuntansi sma", "ekonomi"],
        "topics": {
            10: ["Konsep Ilmu Ekonomi", "Permintaan dan Penawaran", "Pasar"],
            11: ["Pendapatan Nasional", "Inflasi dan Kebijakan Moneter", "APBN"],
            12: ["Akuntansi Dasar", "Jurnal Umum", "Laporan Keuangan"],
        },
    },
    {
        "no": 14, "slug": "geografi", "rumpun": "sosial_ekonomi",
        "kelompok": "IPS", "bahasa": "id",
        "display": "Geografi",
        "q": ["geografi sma", "geografi", "geografi peminatan"],
        "topics": {
            10: ["Peta dan Penginderaan Jauh", "Sejarah Pembentukan Bumi",
                 "Atmosfer"],
            11: ["Biosfer", "Sumber Daya Alam", "Mitigasi Bencana"],
            12: ["Wilayah dan Tata Ruang", "Pasar Bebas",
                 "Kerja Sama Internasional"],
        },
    },
    {
        "no": 15, "slug": "sosiologi", "rumpun": "sosial_ekonomi",
        "kelompok": "IPS", "bahasa": "id",
        "display": "Sosiologi",
        "q": ["sosiologi sma", "sosiologi", "sosiologi peminatan"],
        "topics": {
            10: ["Fungsi Sosiologi", "Interaksi Sosial", "Sosialisasi"],
            11: ["Konflik Sosial", "Stratifikasi Sosial", "Diferensiasi Sosial"],
            12: ["Perubahan Sosial", "Modernisasi", "Ketimpangan Sosial"],
        },
    },
    {
        "no": 16, "slug": "informatika", "rumpun": "eksakta_teknologi",
        "kelompok": "Teknologi", "bahasa": "id",
        "display": "TIK / Informatika",
        "q": ["informatika sma", "tik sma", "informatika"],
        "ktsp_q": "tik",
        "topics": {
            10: ["Berpikir Komputasional", "Algoritma Pemrograman",
                 "Sistem Komputer"],
            11: ["Jaringan Komputer", "Basis Data", "Analisis Data"],
            12: ["Kecerdasan Buatan", "Keamanan Siber", "Proyek Informatika"],
        },
    },
    {
        "no": 17, "slug": "antropologi", "rumpun": "sosial_ekonomi",
        "kelompok": "Bahasa & Budaya", "bahasa": "id",
        "display": "Antropologi",
        "q": ["antropologi sma", "antropologi", "antropologi peminatan"],
        "topics": {
            10: ["Konsep Antropologi", "Kebudayaan", "Etnografi"],
            11: ["Keanekaragaman Budaya", "Sistem Kekerabatan",
                 "Religi dan Kepercayaan"],
            12: ["Perubahan Budaya", "Globalisasi Budaya", "Antropologi Terapan"],
        },
        # antropologi kandidatnya paling langka: tambah bab cadangan
        "extra": {
            10: [("Antropologi Ragawi",
                  ["antropologi ragawi manusia purba",
                   "materi antropologi ragawi kelas 10"]),
                 ("Manusia dan Kebudayaan",
                  ["antropologi manusia dan kebudayaan",
                   "pengantar antropologi budaya"])],
            11: [("Sistem Kekerabatan dan Perkawinan",
                  ["sistem kekerabatan dan perkawinan antropologi",
                   "antropologi kekerabatan"]),
                 ("Sistem Mata Pencaharian",
                  ["sistem mata pencaharian antropologi",
                   "antropologi ekonomi mata pencaharian"])],
            12: [("Antropologi Kesehatan",
                  ["antropologi kesehatan",
                   "antropologi kesehatan masyarakat"]),
                 ("Etnografi Indonesia",
                  ["etnografi masyarakat indonesia",
                   "contoh penelitian etnografi"])],
        },
    },
    {
        "no": 18, "slug": "sastra_indonesia", "rumpun": "bahasa",
        "kelompok": "Bahasa & Budaya", "bahasa": "id",
        "display": "Sastra Indonesia",
        "q": ["sastra indonesia", "bahasa indonesia tingkat lanjut", "sastra sma"],
        "topics": {
            10: ["Puisi", "Cerpen", "Pantun"],
            11: ["Hikayat", "Drama", "Resensi Buku"],
            12: ["Novel", "Kritik Sastra", "Esai Sastra"],
        },
    },
    {
        "no": 19, "slug": "bahasa_inggris", "rumpun": "bahasa",
        "kelompok": "Umum / Bahasa", "bahasa": "id+en",
        "display": "Bahasa Inggris",
        "q": ["bahasa inggris sma", "bahasa inggris", "english sma"],
        "topics": {
            10: ["Narrative Text", "Descriptive Text", "Procedure Text"],
            11: ["Analytical Exposition", "Explanation Text", "Report Text"],
            12: ["News Item", "Discussion Text", "Review Text"],
        },
    },
    {
        "no": 20, "slug": "bahasa_inggris_lanjut", "rumpun": "bahasa",
        "kelompok": "Bahasa & Budaya", "bahasa": "id+en",
        "display": "Bahasa Inggris Tingkat Lanjut",
        "q": ["bahasa inggris tingkat lanjut", "english advanced sma",
              "bahasa inggris peminatan"],
        "topics": {
            10: ["Argumentative Essay", "Short Story Analysis", "Poetry Analysis"],
            11: ["Formal Letter", "Presentation Skills", "Debate"],
            12: ["Literary Analysis", "Research Report", "Academic Writing"],
        },
    },
    {
        "no": 21, "slug": "bahasa_jawa", "rumpun": "bahasa",
        "kelompok": "Muatan Lokal", "bahasa": "id+jw",
        "display": "Bahasa Jawa",
        "q": ["bahasa jawa sma", "bahasa jawa", "basa jawa"],
        "topics": {
            10: ["Tembang Macapat", "Geguritan", "Aksara Jawa"],
            11: ["Wayang", "Serat Wedhatama", "Parikan"],
            12: ["Teks Eksposisi Bahasa Jawa", "Sesorah", "Sastra Jawa Modern"],
        },
    },
]

HEADER = """\
# label: {slug} | no: {no} | rumpun: {rumpun}
# mapel: {display}
# aturan: '## <kurikulum> | kelas <10-12> | <bab>' lalu 'ytsearchN:query' atau URL
# kurikulum yang dikenal: k13, kurmerdeka, ktsp2006
# silakan tambah playlist/video manual di bawah seksi yang sesuai
"""


def render_file(meta: dict) -> str:
    lines = [HEADER.format(**meta)]
    for kelas in (10, 11, 12):
        q0 = meta["q"][0]
        q1 = meta["q"][1 % len(meta["q"])]
        q2 = meta["q"][2 % len(meta["q"])]
        ktsp_q = meta.get("ktsp_q", q2)
        for topic in meta["topics"][kelas]:
            lines += [
                f"## k13 | kelas {kelas} | {topic}",
                f"ytsearch12:{q0} kelas {kelas} {topic}",
                f"ytsearch12:pembahasan soal {topic} {q0} kelas {kelas}",
                "",
                f"## kurmerdeka | kelas {kelas} | {topic}",
                f"ytsearch12:{q1} kelas {kelas} kurikulum merdeka {topic}",
                "",
            ]
        # bab cadangan untuk label yang kandidatnya langka
        for bab, queries in meta.get("extra", {}).get(kelas, []):
            lines.append(f"## k13 | kelas {kelas} | {bab}")
            for q in queries:
                qq = q if re.search(r"kelas\s*(10|11|12)", q.lower()) \
                    else f"{q} kelas {kelas}"
                lines.append(f"ytsearch12:{qq}")
            lines.append("")
        lines += [
            f"## ktsp2006 | kelas {kelas} | materi kelas {kelas}",
            f"ytsearch12:{ktsp_q} kelas {kelas} ktsp",
            f"ytsearch12:{ktsp_q} kelas {kelas} ktsp 2006",
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
    rows = [m for m in META if not a.only or m["slug"] in a.only]
    if a.only:
        unknown = set(a.only) - {m["slug"] for m in META}
        if unknown:
            sys.exit(f"label tidak dikenal: {', '.join(sorted(unknown))}")

    missing_reg = not REGISTRY.exists()
    if a.check:
        problems = []
        if missing_reg:
            problems.append(f"registry belum ada: {REGISTRY}")
        for m in META:
            p = SRC_DIR / f"playlist_{m['slug']}.txt"
            if not p.exists():
                problems.append(f"sumber belum ada: {p.name}")
            elif p.stat().st_size == 0:
                problems.append(f"sumber kosong (0 byte): {p.name}")
        for p in problems:
            print("[x]", p)
        print("OK" if not problems else f"{len(problems)} masalah")
        return

    if missing_reg or a.force:
        tmp = REGISTRY.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["no", "label", "rumpun", "kelompok_tabel", "bahasa", "query"])
            for m in META:
                w.writerow([m["no"], m["slug"], m["rumpun"], m["kelompok"],
                            m["bahasa"], ";".join(m["q"])])
        tmp.replace(REGISTRY)
        print(f"[ok] registry ditulis: {REGISTRY.name} (21 label)")
    else:
        print(f"[=] registry sudah ada, tidak ditimpa: {REGISTRY.name}")

    n_new = n_skip = 0
    for m in rows:
        p = SRC_DIR / f"playlist_{m['slug']}.txt"
        empty = p.exists() and p.stat().st_size == 0
        if p.exists() and not a.force and not empty:
            n_skip += 1
            continue
        body = render_file(m)
        tmp = p.with_suffix(".txt.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(p)
        n_new += 1
        if empty:
            print(f"[ok] {p.name} diisi ulang (sebelumnya 0 byte)")

    total = sum(1 for _ in SRC_DIR.glob("playlist_*.txt"))
    n_query = sum(
        1 for p in SRC_DIR.glob("playlist_*.txt")
        for line in p.read_text(encoding="utf-8-sig").splitlines()
        if line.strip().startswith("ytsearch")
    )
    print(f"[ok] file sumber: {n_new} dibuat/diisi, {n_skip} dilewati, "
          f"{total} total, {n_query} query")
    print(f"Langkah berikut: python scripts/sources_audit.py "
          f"--target-per-label {TARGET_PER_LABEL}")


if __name__ == "__main__":
    main()
