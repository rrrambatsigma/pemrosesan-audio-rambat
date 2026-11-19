"""Konfigurasi path, konstanta, dan skema manifest.

Semua modul lain mengambil path dari sini supaya tidak ada lagi path
hardcoded yang salah (dulu: ``00_SumberData`` padahal foldernya
``sumber_data``).
"""

from __future__ import annotations

import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Path
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]

SRC_DIR = ROOT / "sumber_data"
REGISTRY = SRC_DIR / "registry_labels.csv"
LINKS_TXT = SRC_DIR / "links_yt.txt"
LINKS_DIR = SRC_DIR / "links"

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"                 # klip WAV hasil potong
SOURCE_DIR = DATA_DIR / "source"           # audio sumber utuh (opus)
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
CACHE_DIR = DATA_DIR / "cache"
SEARCH_CACHE = CACHE_DIR / "search.json"
PROBE_DIR = CACHE_DIR / "probe"
TMP_DIR = DATA_DIR / "_tmp"
MANIFEST = DATA_DIR / "manifest.csv"
FAILURES = DATA_DIR / "failures.csv"
REPORTS_DIR = DATA_DIR / "reports"
LOCK_FILE = DATA_DIR / ".scrape.lock"

SOURCE_INVENTORY = REPORTS_DIR / "source_inventory.csv"
SOURCE_PLAYLISTS = REPORTS_DIR / "source_playlists.csv"
SOURCE_COVERAGE = REPORTS_DIR / "source_coverage.csv"
QUALITY_CSV = REPORTS_DIR / "audio_quality.csv"
LINKS_CHECK = REPORTS_DIR / "links_check.csv"

# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
SAMPLE_RATE = 16000
CLIP_SEC = 60
CLIPS_PER_VIDEO = 4        # 1 inti + 3 bonus, tanpa unduh ulang
INTRO_SKIP_S = 90.0        # buang jingle / salam pembuka
OUTRO_SKIP_S = 30.0
MIN_GAP_S = 20.0           # jarak minimum antar jendela dalam satu video
MIN_VIDEO_SEC = 8 * 60
MAX_VIDEO_SEC = 4 * 3600

# --------------------------------------------------------------------------- #
# Target dataset
# --------------------------------------------------------------------------- #
TARGET_PER_LABEL = 100     # 100 x 21 = 2.100 video rencana -> ~2.000 hasil bersih
MAX_CHANNEL_FRAC = 0.20    # maksimum 20% video satu label dari satu channel
MIN_CHANNELS_PER_LABEL = 12
KELAS = (10, 11, 12)
KURIKULUM = ("k13", "kurmerdeka", "ktsp2006")

# proporsi per kurikulum di dalam satu kelas (sisanya dibulatkan)
KUR_WEIGHT = {"k13": 0.42, "kurmerdeka": 0.42, "ktsp2006": 0.16}

CACHE_TTL_H = 24
LOCK_STALE_H = 12

BLOCK_HINTS = ("429", "too many requests", "sign in to confirm",
               "confirm you're not a bot", "http error 403")

# --------------------------------------------------------------------------- #
# Skema manifest: 1 baris = 1 klip
# --------------------------------------------------------------------------- #
FIELDS = [
    # identitas & label
    "clip_id", "label_no", "label", "rumpun", "bab", "kelas", "kurikulum",
    # sumber video
    "video_id", "clip_index", "clip_start_sec", "clip_sec", "status", "error",
    # metadata video
    "title", "channel", "channel_id", "channel_url", "license", "lang",
    "lang_detected", "duration_src_sec",
    # jejak audit
    "url", "video_url", "source_url", "source_kind", "source_query",
    # berkas
    "sr", "channels", "filepath", "bytes", "sha256",
    # hasil tahap hilir (diisi qc.py)
    "qc_gate", "qc_score", "qc_flags", "reviewed",
    # provenans
    "yt_dlp_version", "downloaded_at",
]

FAILURE_FIELDS = ["video_id", "label", "stage", "error", "attempts", "last_at"]

NUMERIC_FIELDS = {
    "label_no", "kelas", "clip_index", "clip_start_sec", "clip_sec",
    "duration_src_sec", "sr", "channels", "bytes", "qc_score",
}


def setup_console() -> None:
    """Paksa stdout/stderr UTF-8 agar karakter non-ASCII aman di Windows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def ensure_dirs() -> None:
    """Buat semua folder kerja (idempoten, aman dipanggil berkali-kali)."""
    for d in (SRC_DIR, LINKS_DIR, RAW_DIR, SOURCE_DIR, TRANSCRIPT_DIR,
              CACHE_DIR, PROBE_DIR, TMP_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
