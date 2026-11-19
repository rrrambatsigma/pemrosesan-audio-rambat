"""CLI generik: unduh audio YouTube -> WAV 16 kHz mono (lokal).

Contoh:
    python scripts/scrape_local.py --cats sainsmath teknik --dry-run
    python scripts/scrape_local.py --cats kesehatan --yes --max-per-run 20
"""

import _bootstrap  # noqa: F401
from audio_pipeline.local import main

if __name__ == "__main__":
    main()
