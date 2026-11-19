"""Scrape bidang SAINSMATH -> data/raw/sainsmath/..."""

import _bootstrap  # noqa: F401
from audio_pipeline.local import main

if __name__ == "__main__":
    main(default_cats=["sainsmath"])
