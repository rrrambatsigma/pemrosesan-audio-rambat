"""Ekspansi sumber: ``ytsearchN:`` dan URL YouTube -> daftar kandidat video.

Hanya metadata (``extract_flat``), tidak mengunduh audio. Setiap kandidat
membawa ``source_query`` supaya asal-usulnya bisa diaudit di manifest dan
``links_yt.txt``.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from yt_dlp import YoutubeDL

from .config import CACHE_TTL_H, SEARCH_CACHE, TMP_DIR
from .sources import SourceItem

POLITE = {
    "sleep_interval_requests": 1,
    "concurrent_fragment_downloads": 1,
    "retry_sleep_functions": {
        "http": lambda n: min(60, 2 ** n),
        "fragment": lambda n: min(60, 2 ** n),
        "extractor": lambda n: min(60, 2 ** n),
    },
}


@dataclass
class Candidate:
    video_id: str
    title: str
    duration: int | None
    channel: str
    channel_id: str
    channel_url: str
    url: str
    label: str
    kelas: int
    kurikulum: str
    bab: str
    source_url: str        # URL playlist/video, atau "ytsearch:N"
    source_kind: str       # "search" | "url"
    source_query: str      # query asli (kosong kalau URL manual)
    playlist_title: str = ""
    rank: int = 0          # urutan kemunculan (relevansi pencarian)

    @property
    def key(self) -> tuple[str, str]:
        return (self.video_id, self.label)


def load_cache(path: Path | None = None) -> dict:
    path = Path(path or SEARCH_CACHE)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_cache(cache: dict, path: Path | None = None) -> None:
    path = Path(path or SEARCH_CACHE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(path)


def _cache_key(item: SourceItem) -> str:
    if item.kind == "search":
        return f"search:{item.limit}:{item.value.strip().lower()}"
    return f"url:{item.value.strip().lower()}"


def _opts(browser: str | None, extra: dict | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "noplaylist": False,
        # jangan pernah menggantung: satu query maksimal ±20 detik per percobaan
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 1,
        **POLITE,
    }
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    if extra:
        opts.update(extra)
    return opts


def _to_candidates(info: dict, item: SourceItem, source_url: str,
                   kind: str) -> list[Candidate]:
    entries = info.get("entries") or [info]
    ptitle = info.get("title", "") if "entries" in info else ""
    out: list[Candidate] = []
    for rank, e in enumerate(entries):
        if not e:
            continue
        vid = e.get("id") or ""
        title = e.get("title") or ""
        if len(vid) != 11 or title in ("[Private video]", "[Deleted video]"):
            continue
        out.append(
            Candidate(
                video_id=vid,
                title=title,
                duration=e.get("duration"),
                channel=e.get("channel") or e.get("uploader") or "",
                channel_id=e.get("channel_id") or e.get("uploader_id") or "",
                channel_url=e.get("channel_url") or "",
                url=f"https://www.youtube.com/watch?v={vid}",
                label=item.label,
                kelas=item.kelas,
                kurikulum=item.kurikulum,
                bab=item.bab,
                source_url=source_url,
                source_kind=kind,
                source_query=item.value if item.kind == "search" else "",
                playlist_title=ptitle,
                rank=rank,
            )
        )
    return out


def expand(item: SourceItem, cache: dict, *, refresh: bool = False,
           browser: str | None = None, polite: bool = True) -> list[Candidate]:
    """Ambil kandidat dari satu item sumber (memakai cache bila masih segar)."""
    key = _cache_key(item)
    hit = cache.get(key)
    if hit and not refresh and time.time() - hit.get("t", 0) < CACHE_TTL_H * 3600:
        return [Candidate(**c) for c in hit.get("items", [])]

    if item.kind == "search":
        url = f"ytsearch{item.limit}:{item.value}"
        source_url = f"ytsearch:{item.value}"
        kind = "search"
    else:
        url = item.value
        source_url = item.value
        kind = "url"

    try:
        with YoutubeDL(_opts(browser)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        print(f"    [!] gagal baca '{source_url[:70]}': {str(e)[:90]}")
        return []
    if not info:
        return []

    cands = _to_candidates(info, item, source_url, kind)
    cache[key] = {"t": time.time(), "items": [asdict(c) for c in cands]}
    if polite:
        time.sleep(random.uniform(1.5, 4.0))
    return cands


def expand_items(items: list[SourceItem], *, refresh: bool = False,
                 browser: str | None = None, limit_queries: int = 0,
                 verbose: bool = True) -> tuple[list[Candidate], dict]:
    """Ekspansi banyak item sekaligus; cache disimpan sekali di akhir."""
    cache = load_cache()
    best: dict[str, Candidate] = {}
    counts: dict[str, int] = {}
    for item in items:
        done = counts.get(item.label, 0)
        if limit_queries and done >= limit_queries:
            continue
        counts[item.label] = done + 1
        got = expand(item, cache, refresh=refresh, browser=browser)
        new = 0
        for c in got:
            old = best.get(c.video_id)
            if old is None:
                best[c.video_id] = c
                new += 1
            elif c.rank < old.rank:      # lebih relevan di query label lain
                best[c.video_id] = c
                new += 1
        if verbose:
            print(f"  {item.label:<22} {item.kelas} {item.kurikulum:<11} "
                  f"{item.bab[:26]:<26} {item.value[:44]:<44} -> "
                  f"{len(got):>3} hasil, {new:>3} baru/relevan")
        if sum(counts.values()) % 25 == 0:
            save_cache(cache)
    save_cache(cache)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return list(best.values()), cache
