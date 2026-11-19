# Dataset Audio 21 Mata Pelajaran SMA

Pipeline akuisisi: YouTube → audio sumber → klip 60 detik → manifest → jejak audit `links_yt.txt`.

Target: **3.003 video** (143 × 21 label), masing-masing 1 klip inti + 3 klip bonus,
tersebar di kelas 10–12 dan kurikulum K13 / Kurikulum Merdeka / KTSP 2006.

## Environment

Semua dependensi ada di env conda **`audio-ds`** (Python 3.11). Jangan pakai
`python` global 3.13 (tidak punya `soundfile`/`torch`).

```powershell
# cara 1: panggil langsung
C:\Users\Asus\miniconda3\envs\audio-ds\python.exe scripts\check_env.py

# cara 2: aktifkan dulu
conda activate audio-ds
python scripts/check_env.py
```

Pasang dependensi:

```powershell
C:\Users\Asus\miniconda3\envs\audio-ds\python.exe -m pip install -r requirements.txt
```

## Alur

```
sumber_data/registry_labels.csv        21 label + rumpun
sumber_data/playlist_<label>.txt       seksi per kurikulum|kelas|bab + ytsearch
        │
        ▼  scripts/init_sources.py      (buat/regenerasi sumber, idempoten)
        ▼  scripts/sources_audit.py     (cek kecukupan sebelum unduh)
        ▼  scripts/scrape.py            (unduh → potong → manifest)
        ▼  scripts/qc.py                (kualitas audio)
        ▼  scripts/export_links.py      (links_yt.txt + cakupan)
```

## Perintah harian

```powershell
$py = "C:\Users\Asus\miniconda3\envs\audio-ds\python.exe"

& $py scripts/check_env.py
& $py scripts/init_sources.py --check          # 21 file sumber lengkap?
& $py scripts/verify_sources.py                # isi & bab tiap file
& $py scripts/sources_audit.py                 # kandidat cukup per label?
& $py scripts/scrape.py --only fisika --limit-videos 1 --yes   # uji 1 video
& $py scripts/scrape.py --all --max-per-run 180 --yes          # sesi harian
& $py scripts/qc.py --stages basic --workers 4
& $py scripts/export_links.py --check
```

Bila kena `sign in to confirm you're not a bot`, ulangi dengan cookies browser:

```powershell
& $py scripts/scrape.py --all --max-per-run 180 --cookies-from-browser chrome --yes
```

## Jaminan idempoten

Menjalankan perintah yang sama berulang kali **tidak**:

- mengunduh ulang video yang audionya sudah ada (`data/source/<id>.opus`);
- memotong ulang klip yang sudah lengkap (`data/raw/.../*.wav`);
- menduplikasi baris manifest (primary key `clip_id`);
- mengubah byte `manifest.csv` dan `links_yt.txt` bila tidak ada data baru;
- mengulang video yang sudah tercatat gagal (kecuali `--retry-failed`).

Batas yang dijamin oleh kode:

| Lapisan | Penjaga |
|---|---|
| Rencana | urutan deterministik + seed dari `video_id` |
| Unduh | berkas sumber dicek sebelum jaringan |
| Potong | `clip_id` dipakai ulang, bukan dibuat baru |
| Tulis | atomic replace (`*.tmp` → `os.replace`) |
| Proses | lock file `data/.scrape.lock` |
| QC | baris per `filepath` di `audio_quality.csv` |

## Struktur

```
audio_pipeline/     library akuisisi (config, registry, sources, search,
                    youtube, planner, manifest, scrape, export, quality)
scripts/            CLI tipis + _legacy/ (skrip lama, tidak dipakai)
tests/              pytest (25 tes, termasuk bukti idempoten)
sumber_data/        registry + 21 file sumber + links_yt.txt
data/raw/           klip WAV 16 kHz mono
data/source/        audio sumber utuh (opus) untuk potong ulang
data/manifest.csv   1 baris = 1 klip
data/reports/       audit sumber, cakupan, QC, link check
```

## Format file sumber

Setiap file berisi seksi per **kurikulum | kelas | bab**, sehingga query
pencarian spesifik ke bab dan kolom `bab` di manifest berisi topik nyata
(bukan "materi kelas 10").

```
# label: fisika | no: 9 | rumpun: eksakta_teknologi

## k13 | kelas 10 | Vektor
ytsearch12:fisika sma kelas 10 Vektor
ytsearch12:pembahasan soal Vektor fisika sma kelas 10
https://www.youtube.com/playlist?list=PLxxxx

## kurmerdeka | kelas 10 | Vektor
ytsearch12:fisika kelas 10 kurikulum merdeka Vektor
```

- `##` = header seksi (kurikulum wajib dikenal: `k13`, `kurmerdeka`, `ktsp2006`)
- `#` = komentar
- `ytsearchN:` = pencarian, N hasil
- URL YouTube = playlist/video manual
- baris di luar seksi = error (bukan diam-diam jadi `umum`)

## Uji

```powershell
& $py -m pytest
& $py scripts/smoke_test.py
```

## Catatan etika

Audio hanya disimpan lokal untuk penelitian; yang dipublikasikan adalah
**manifest + link + model**, bukan berkas audio. Hormati ToS YouTube: hanya
video publik, jeda sopan (default 4–10 detik/video, istirahat tiap 25 video).
