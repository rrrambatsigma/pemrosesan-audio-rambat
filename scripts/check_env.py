"""Cek environment untuk pipeline scraping audio.

Jalankan dengan env yang benar (bukan Python global 3.13):

    C:\\Users\\Asus\\miniconda3\\envs\\audio-ds\\python.exe scripts\\check_env.py

atau setelah `conda activate audio-ds`:
    python scripts/check_env.py
"""

from __future__ import annotations

import importlib
import platform
import shutil
import subprocess
import sys

import _bootstrap  # noqa: F401
from audio_pipeline.config import MANIFEST, ROOT, SRC_DIR, ensure_dirs


def run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def section(title):
    print(f"\n=== {title} ===")


def main() -> int:
    section("Sistem")
    print("Proyek  :", ROOT)
    print("OS      :", platform.platform())
    print("Python  :", sys.version.split()[0], "->", sys.executable)
    if sys.version_info >= (3, 12):
        print("  [!] paket ML/audio biasanya belum lengkap di 3.12+; "
              "disarankan env 3.11 (audio-ds)")
    total, used, free = shutil.disk_usage(ROOT)
    print(f"Disk    : {free / 1e9:.0f} GB bebas di drive proyek "
          f"(butuh >= 60 GB)")

    section("Tool command-line")
    ok = True
    for tool in ("ffmpeg", "ffprobe", "git"):
        path = shutil.which(tool)
        print(f"{tool:10}: {'OK -> ' + path if path else 'TIDAK ADA'}")
        ok &= bool(path)

    section("GPU")
    if shutil.which("nvidia-smi"):
        print(run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                   "--format=csv,noheader"]))
    else:
        print("nvidia-smi tidak ada (tidak wajib untuk scraping)")

    section("Paket Python")
    need = ["yt_dlp", "soundfile", "numpy", "librosa", "pyloudnorm",
            "huggingface_hub"]
    optional = ["torch", "transformers", "pandas", "pytest"]
    for name in need + optional:
        try:
            mod = importlib.import_module(name)
            ver = getattr(mod, "version", None)
            ver = getattr(ver, "__version__", None) or getattr(mod, "__version__", "")
            print(f"{name:14}: OK {ver}")
        except Exception:  # noqa: BLE001
            flag = "WAJIB" if name in need else "opsional"
            print(f"{name:14}: TIDAK ADA ({flag})")
            if name in need:
                ok = False

    section("Hugging Face")
    from audio_pipeline import hub as hub_mod

    token = hub_mod.resolve_token()
    repo = hub_mod.resolve_repo()
    print(f"token  : {'ada' if token else 'TIDAK ADA (isi HF_API_KEY di .env)'}")
    print(f"repo   : {repo or 'belum diisi (HF_DATASET_REPO)'}")
    if token:
        try:
            from huggingface_hub import HfApi

            api = HfApi(token=token)
            me = api.whoami()
            print(f"akun   : {me.get('name')}")
            if repo:
                try:
                    api.repo_info(repo, repo_type="dataset")
                    print("status : repo sudah ada")
                except Exception:  # noqa: BLE001
                    print("status : repo belum ada (dibuat otomatis saat scrape)")
        except Exception as e:  # noqa: BLE001
            print(f"token  : DITOLAK ({type(e).__name__}: {e})")
            ok = False

    section("Status proyek")
    ensure_dirs()
    n_src = len(list(SRC_DIR.glob("playlist_*.txt")))
    print(f"file sumber : {n_src} (seharusnya 21)")
    print(f"manifest    : {'ada' if MANIFEST.exists() else 'belum ada'}")

    print("\n" + ("[ok] siap." if ok else "[x] ada yang harus dipasang dulu."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
