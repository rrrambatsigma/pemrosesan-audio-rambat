"""Pipeline dataset audio 21 mata pelajaran SMA.

Modul akuisisi (fokus saat ini):

- :mod:`config`    path & konstanta
- :mod:`registry`  21 label, validasi
- :mod:`sources`   parser ``sumber_data/playlist_*.txt``
- :mod:`search`    ekspansi ``ytsearch`` / playlist
- :mod:`youtube`   probe, unduh audio sumber, potong klip
- :mod:`planner`   kuota label/kelas/kurikulum/channel
- :mod:`manifest`  manifest.csv (1 baris = 1 klip)
- :mod:`scrape`    orkestrator idempoten
- :mod:`export`    links_yt.txt + cakupan
- :mod:`quality`   metrik kualitas audio
"""

__all__ = [
    "config", "registry", "sources", "search", "youtube", "planner",
    "manifest", "scrape", "export", "quality",
]
