# Cara Menjalankan — Scraping Audio 21 Mapel SMA → Hugging Face

Panduan ini untuk siapa pun yang menjalankan proyek ini di laptopnya.
Audio YouTube diunduh sebagai **WAV utuh**, langsung dikirim ke **Hugging Face**,
lalu file sementaranya dihapus. Lokal hanya menyimpan `manifest.csv` yang kecil,
jadi tidak butuh storage besar.

```
YouTube → WAV 16 kHz mono (transit ±26 MB) → commit ke HF → file transit dihapus
```

---

## 1. Yang harus disiapkan

| Kebutuhan | Keterangan |
|---|---|
| Python 3.11 | Disarankan lewat Miniconda (env bernama `audio-ds`) |
| ffmpeg | Wajib ada di `PATH` |
| Akun Hugging Face | Token **Write** + akses tulis ke repo dataset tujuan |
| Internet | Untuk YouTube dan huggingface.co |

Cek cepat di terminal:

```powershell
python --version      # harus 3.11.x
ffmpeg -version
git --version
```

> Jangan pakai Python 3.13 global — `soundfile`/`torch` biasanya belum siap.
> Kalau belum punya Python 3.11, buat env baru (langkah 2).

---

## 2. Instalasi

### Windows (PowerShell)

```powershell
# 1) buat & aktifkan environment (sekali saja)
conda create -n audio-ds python=3.11 -y
conda activate audio-ds

# 2) pasang ffmpeg kalau belum ada
winget install --id Gyan.FFmpeg -e

# 3) masuk ke folder proyek, lalu pasang dependensi
cd E:\pemrosesan-audio-rambat
python -m pip install -r requirements.txt
```

### Linux / macOS

```bash
conda create -n audio-ds python=3.11 -y
conda activate audio-ds
# Ubuntu/Debian: sudo apt install ffmpeg
# macOS:         brew install ffmpeg
python -m pip install -r requirements.txt
```

Kalau malas mengaktifkan conda, panggil Python-nya langsung:

```powershell
$py = "C:\Users\Asus\miniconda3\envs\audio-ds\python.exe"
& $py -m pip install -r requirements.txt
```

---

## 3. Isi `.env`

Salin contohnya, lalu edit:

```powershell
Copy-Item .env.example .env
notepad .env
```

Isinya:

```dotenv
# token dari https://huggingface.co/settings/tokens  (New token → Write)
HF_API_KEY=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# repo tujuan; dibuat otomatis kalau belum ada
HF_DATASET_REPO=username/audio-sma-21-mapel-raw
```

Aturan penting:

- **Jangan pernah mengirim atau meng-commit `.env`.** File ini sudah masuk
  `.gitignore`; isinya token pribadi.
- Kalau repo milik orang lain: minta pemilik menambahkan akunmu sebagai
  **collaborator** (repo → Settings → Collaborators), lalu pakai tokenmu sendiri.
- Kalau ingin repo sendiri: ganti `HF_DATASET_REPO` dengan
  `akunmu/nama-repo`. Repo **private** lebih aman (default).
- Kalau `HF_DATASET_REPO` dikosongkan, kode menebak otomatis:
  `<akun-pemilik-token>/audio-sma-21-mapel-raw`.

---

## 4. Cek environment

```powershell
python scripts/check_env.py
```

Yang harus terlihat:

```
ffmpeg    : OK -> ...
soundfile : OK ...
=== Hugging Face ===
token  : ada
repo   : username/audio-sma-21-mapel-raw
akun   : nama-akunmu
```

Kalau token `DITOLAK`, periksa token di `.env`. Kalau repo `belum ada`, itu
normal — repo dibuat saat scraping pertama.

---

## 5. Uji 1 video (wajib dicoba dulu)

```powershell
python scripts/scrape.py --only fisika --limit-videos 1 --yes
```

Yang terjadi:

1. unduh audio ke `data/_tmp/hub/<video_id>/<video_id>.wav`;
2. commit ke HF: `raw/fisika/10_k13/<clip_id>.wav` + `manifest.csv`;
3. folder transit dihapus.

Contoh akhir:

```
[4/4] mulai: 1 video (1 berkas utuh/video)
[hub] repo privat: https://huggingface.co/datasets/username/audio-sma-21-mapel-raw
[hub] commit 1a2b3c4d: +1 berkas (1 total, 0.04 GB), lokal dibebaskan 38.4 MB
[1/1] [ok] fisika abcdefghijk +1 klip
```

Buka repo di browser — file WAV harus sudah ada.

---

## 6. Jalankan scraping penuh

```powershell
# semua label, maksimal 1.512 video (target dataset)
python scripts/scrape.py --all --max-per-run 1512 --yes

# atau satu label dulu
python scripts/scrape.py --only fisika --yes
```

Selama berjalan:

- audio hanya menempati disk sebentar (±50 MB per video, termasuk file sementara
  yt-dlp), lalu dihapus setelah commit sukses;
- commit dilakukan **tiap video**; kalau kena limit HF, pakai
  `--hub-flush 5` (commit tiap 5 video);
- **Ctrl+C kapan saja aman** — kemajuan tersimpan di manifest.

### Melanjutkan besok

Jalankan perintah yang sama lagi. Video yang sudah `uploaded` dilewati tanpa
mengunduh ulang:

```powershell
python scripts/scrape.py --all --max-per-run 1512 --yes
```

### Kalau kena "sign in to confirm you're not a bot"

```powershell
python scripts/scrape.py --all --max-per-run 180 --cookies-from-browser chrome --yes
```

Ganti `chrome` dengan `edge`/`firefox`/`brave` sesuai browser yang login YouTube.

---

## 7. Hasil di Hugging Face

```
<repo>/
├── manifest.csv                          # 1 baris = 1 audio + status
└── raw/
    └── <label>/
        └── <kelas>_<kurikulum>/
            └── <clip_id>.wav
```

Contoh: `raw/fisika/10_k13/fisika-k10-k13-0001.wav`.

Ukuran: WAV 16 kHz mono ≈ **1,9 MB/menit** (±26 MB per video 13 menit).
Target 1.512 video ≈ 520 jam ≈ **60 GB**.

Ingin kualitas asli (44,1 kHz stereo)? Tambahkan `--wav-original` — tapi
ukurannya ±5,5× lebih besar.

---

## 8. Perintah lain yang berguna

```powershell
# lihat rencana tanpa mengunduh / mengubah apa pun
python scripts/scrape.py --all --dry-run

# simpan juga audio di lokal (default: dihapus setelah commit)
python scripts/scrape.py --all --keep-local --yes

# matikan HF, simpan semuanya di data/source
python scripts/scrape.py --all --no-hub --yes

# cek isi manifest
python scripts/inspect_manifest.py --detail 5

# audit link YouTube
python scripts/export_links.py --check

# QC audio (butuh file lokal)
python scripts/qc.py --stages basic --workers 4
```

QC membaca file di disk. Karena audio langsung dihapus setelah upload, pilih
salah satu:

- jalankan scraping dengan `--keep-local`, QC, lalu
  `python scripts/hf_push.py --delete-local --yes`; atau
- QC menyusul dengan mengunduh balik dari HF.

---

## 9. Khusus pemilik data lama (backfill)

Lewati bagian ini kalau mulai dari nol.

```powershell
# lihat status: berapa yang sudah/belum di HF
python scripts/hf_push.py --check

# lihat rencana tanpa commit
python scripts/hf_push.py --dry-run --limit 10

# upload semua: opus lama ditranscode ke WAV 16 kHz mono lalu dikirim
python scripts/hf_push.py --batch 20 --yes

# setelah yakin semua aman di HF, bebaskan disk
python scripts/hf_push.py --batch 20 --delete-local --yes
```

`--delete-local` menghapus file asli secara permanen. Jalankan hanya setelah
`--check` menunjukkan `belum: 0`.

---

## 10. Kalau ada masalah

| Gejala | Solusi |
|---|---|
| `ffmpeg tidak ada di PATH` | Pasang ffmpeg, buka terminal baru, cek `ffmpeg -version` |
| `ModuleNotFoundError: soundfile` | Environment salah. Pakai Python env `audio-ds` (3.11) |
| `token HF tidak ditemukan` | Isi `HF_API_KEY` di `.env` atau environment |
| `token HF ditolak` | Token salah/kadaluarsa; buat token baru (Write) |
| `403` saat membuat repo | Minta akses collaborator, atau ganti `HF_DATASET_REPO` ke repo sendiri |
| `429` / `too many requests` | Tambahkan `--hub-flush 5`, atau ulangi beberapa saat lagi |
| Koneksi HF terputus | Ulangi perintah yang sama; file yang gagal disimpan untuk diulang |
| `sign in to confirm you're not a bot` | Pakai `--cookies-from-browser chrome` |
| Disk penuh | Hapus `data/_tmp/hub/*` yang tersisa dari upload gagal, atau jalankan `hf_push.py --delete-local` |
| Repo tidak muncul di HF | Pastikan `HF_DATASET_REPO` benar dan akun punya hak tulis |

Kalau upload gagal 3× beruntun, script berhenti sendiri dan **tidak** menghapus
audio sementara — perbaiki token/jaringan lalu jalankan ulang.

---

## 11. Struktur proyek (singkat)

```
audio_pipeline/     library: config, youtube, scrape, hub, manifest, planner, ...
scripts/            CLI: scrape.py, hf_push.py, check_env.py, qc.py, ...
sumber_data/        21 file sumber + registry label
data/_tmp/hub/      transit WAV (dibersihkan otomatis)
data/manifest.csv   catatan semua audio + status hub
tests/              pytest
```

---

## 12. Etika & legal

Audio YouTube berhak cipta kecuali berlisensi Creative Commons.

- default repo **private** — jangan `--hub-public` untuk audio berhak cipta;
- untuk repo publik gunakan `--only-cc`;
- sediakan jalur takedown: hapus file dari HF bila diminta pemiliknya;
- hormati ToS YouTube: hanya video publik, jeda sopan (default 4–10 detik/video).
