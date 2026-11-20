"""Perencanaan: kandidat -> daftar video yang akan diunduh.

Kuota berlapis supaya dataset tidak miring:

1. per label  : ``target_per_label`` (default dari konfigurasi)
2. per kelas  : dibagi rata (10/11/12)
3. per kurikulum: k13 / kurmerdeka / ktsp2006 sesuai bobot
4. per channel: maksimum ``MAX_CHANNEL_FRAC`` dari kuota label, dipilih
   dengan prinsip "channel paling sedikit dipakai dulu".

Semua urutan deterministik: menjalankan ulang menghasilkan rencana yang sama.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from .config import (KUR_WEIGHT, MAX_CHANNEL_FRAC, MIN_VIDEO_SEC,
                     TARGET_PER_LABEL, KELAS, KURIKULUM)
from .registry import Label
from .search import Candidate


class PlanError(RuntimeError):
    """Tidak ada kandidat yang bisa direncanakan."""


def cells_for(target: int) -> dict[tuple[int, str], int]:
    """Bagi target label ke sel (kelas, kurikulum), deterministik."""
    cells: dict[tuple[int, str], int] = {}
    per_kelas = target // len(KELAS)
    sisa_kelas = target - per_kelas * len(KELAS)
    for i, kelas in enumerate(KELAS):
        n = per_kelas + (1 if i < sisa_kelas else 0)
        k13 = int(round(n * KUR_WEIGHT["k13"]))
        km = int(round(n * KUR_WEIGHT["kurmerdeka"]))
        ktsp = n - k13 - km
        cells[(kelas, "k13")] = k13
        cells[(kelas, "kurmerdeka")] = km
        cells[(kelas, "ktsp2006")] = max(0, ktsp)
    # koreksi pembulatan: total harus sama dengan target
    total = sum(cells.values())
    if total != target:
        for key in sorted(cells):
            if total >= target:
                break
            cells[key] += 1
            total += 1
        for key in sorted(cells, reverse=True):
            if total <= target:
                break
            if cells[key] > 0:
                cells[key] -= 1
                total -= 1
    return cells


def _usable(c: Candidate, min_sec: float) -> bool:
    if c.duration is None or c.duration < min_sec:
        return False
    declared = kelas_from_title(c.title)
    if declared is not None and declared != c.kelas:
        return False
    return True


ROMAN = {"x": 10, "xi": 11, "xii": 12}
KELAS_RE = re.compile(
    r"\b(?:kelas|kls\.?|grade)\s*[-:]?\s*(xii|xi|x|12|11|10)\b", re.IGNORECASE
)


def kelas_from_title(title: str) -> int | None:
    """Kelas yang disebut di judul, kalau ada (``None`` = tidak disebut)."""
    m = KELAS_RE.search(title or "")
    if not m:
        return None
    token = m.group(1).lower()
    return int(token) if token.isdigit() else ROMAN[token]


def select_label(cands: list[Candidate], target: int,
                 max_channel_frac: float = MAX_CHANNEL_FRAC,
                 min_sec: float = MIN_VIDEO_SEC) -> list[Candidate]:
    """Pilih video untuk satu label sesuai kuota sel + batas channel."""
    pool = [c for c in cands if _usable(c, min_sec)]
    if not pool:
        return []

    cells = cells_for(target)
    cap = max(3, int(math.ceil(target * max_channel_frac)))
    chosen: list[Candidate] = []
    per_channel: Counter[str] = Counter()
    used: set[str] = set()

    # urutkan kandidat per sel: relevansi (rank) lalu channel agar beragam
    buckets: dict[tuple[int, str], list[Candidate]] = defaultdict(list)
    for c in pool:
        buckets[(c.kelas, c.kurikulum)].append(c)

    for key in ((k, ku) for k in KELAS for ku in KURIKULUM):
        quota = cells.get(key, 0)
        if quota <= 0:
            continue
        bucket = [c for c in buckets.get(key, []) if c.video_id not in used]
        bucket.sort(key=lambda c: (c.rank, c.channel_id, c.video_id))
        taken = 0
        # dua putaran: utamakan channel yang belum dipakai di label ini
        for strict in (True, False):
            for c in bucket:
                if taken >= quota:
                    break
                if c.video_id in used:
                    continue
                ch = c.channel_id or c.channel
                if per_channel[ch] >= cap:
                    continue
                if strict and per_channel[ch] > 0:
                    continue
                chosen.append(c)
                used.add(c.video_id)
                per_channel[ch] += 1
                taken += 1

    # putaran penutup: kalau ada sel yang kandidatnya kurang, kuota sel lain
    # dipakai untuk mengejar target (tetap hormati batas channel)
    if len(chosen) < target:
        rest = [c for c in pool if c.video_id not in used]
        rest.sort(key=lambda c: (c.rank, c.channel_id, c.video_id))
        for c in rest:
            if len(chosen) >= target:
                break
            ch = c.channel_id or c.channel
            if per_channel[ch] >= cap:
                continue
            chosen.append(c)
            used.add(c.video_id)
            per_channel[ch] += 1
    return chosen


def remaining_targets(labels: dict[str, Label],
                      done_per_label: dict[str, int] | None = None,
                      target_per_label: int = TARGET_PER_LABEL,
                      only: list[str] | None = None) -> dict[str, int]:
    """Sisa kuota tiap label: target dikurangi video yang sudah selesai.

    Inilah yang membuat rencana konvergen ke nol. Tanpa ini, setiap run
    mengejar 100 video *baru* per label selamanya.
    """
    done = done_per_label or {}
    slugs = sorted((s for s in labels if not only or s in only),
                   key=lambda s: labels[s].no)
    return {slug: max(0, target_per_label - int(done.get(slug, 0) or 0))
            for slug in slugs}


def build_plan(cands: list[Candidate], labels: dict[str, Label],
               target_per_label: int,
               only: list[str] | None = None,
               exclude_videos: set[str] | None = None,
               max_channel_frac: float = MAX_CHANNEL_FRAC,
               min_sec: float = MIN_VIDEO_SEC,
               done_per_label: dict[str, int] | None = None
               ) -> dict[str, list[Candidate]]:
    """Rencana per label, sudah membuang video yang selesai/gagal.

    Kuota yang dipakai adalah **sisa** target (lihat ``remaining_targets``),
    bukan target penuh, sehingga menjalankan ulang setelah target terpenuhi
    menghasilkan rencana kosong.
    """
    exclude = exclude_videos or set()
    by_label: dict[str, list[Candidate]] = defaultdict(list)
    for c in cands:
        if c.video_id in exclude:
            continue
        by_label[c.label].append(c)

    targets = remaining_targets(labels, done_per_label, target_per_label, only)
    plan: dict[str, list[Candidate]] = {}
    for slug, want in targets.items():
        plan[slug] = select_label(by_label.get(slug, []), want,
                                  max_channel_frac, min_sec)
    return plan


def flatten(plan: dict[str, list[Candidate]]) -> list[Candidate]:
    """Urut stabil: label -> kelas -> kurikulum -> channel -> video."""
    kur_i = {k: i for i, k in enumerate(KURIKULUM)}
    out: list[Candidate] = []
    for slug in sorted(plan):
        for c in plan[slug]:
            out.append(c)
    out.sort(key=lambda c: (c.label, c.kelas, kur_i.get(c.kurikulum, 99),
                            c.channel_id or c.channel, c.video_id))
    return out


def describe(plan: dict[str, list[Candidate]], labels: dict[str, Label],
             targets: dict[str, int] | None = None) -> str:
    """Ringkasan teks: sisa target vs kandidat vs channel per label."""
    targets = targets or {}
    lines = [f"{'label':<24}{'rencana':>8}{'channel':>9}{'k13':>6}"
             f"{'km':>6}{'ktsp':>6}   catatan"]
    for slug in sorted(plan, key=lambda s: labels[s].no):
        items = plan[slug]
        target = int(targets.get(slug, TARGET_PER_LABEL))
        kur = Counter(c.kurikulum for c in items)
        ch = len({c.channel_id or c.channel for c in items})
        note = ""
        if len(items) < target:
            note = f"KURANG {target - len(items)}"
        lines.append(f"{slug:<24}{len(items):>8}{ch:>9}{kur['k13']:>6}"
                     f"{kur['kurmerdeka']:>6}{kur['ktsp2006']:>6}   {note}")
    return "\n".join(lines)
