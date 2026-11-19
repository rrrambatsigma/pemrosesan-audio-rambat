"""Audit ketersediaan sumber sebelum download.

Output:
    data/reports/source_inventory.csv  (per video)
    data/reports/source_playlists.csv  (per playlist/video)
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict

from .config import REPORTS_DIR, SOURCE_INVENTORY, SOURCE_PLAYLISTS, setup_console
from .manifest import load_manifest
from .planner import interleave
from .sources import discover, parse_source_with_subs, src_key
from .youtube import expand, load_cache, save_cache


def is_single(url):
    return ("watch?v=" in url) or ("youtu.be/" in url)


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser(
        description="Hitung video yang sudah ditemukan di file sumber, sebelum download.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--cats", nargs="+")
    ap.add_argument("--subs", nargs="+")
    ap.add_argument("--target", type=int, default=300, help="target video layak per bidang")
    ap.add_argument("--min-duration-min", type=float, default=5)
    ap.add_argument("--clip-minutes", type=int, default=10)
    ap.add_argument("--max-per-playlist", type=int, default=0, help="simulasi batas per playlist")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    clip_s = a.clip_minutes * 60

    sources = discover()
    if not sources:
        sys.exit("Tidak ada file playlist_* di 00_SumberData")
    cats = a.cats or list(sources)
    cache, manifest = load_cache(), load_manifest()

    S = defaultdict(
        lambda: dict(pl=0, vid=0, n=0, usable=0, short=0, unk=0,
                    h_clip=0.0, done=0, dup_x=0)
    )
    subs_of, inv, src_rows, claimed = {}, [], [], {}

    for cat in cats:
        if cat not in sources:
            print(f"[!] file sumber '{cat}' tidak ditemukan. Tersedia: {', '.join(sources)}")
            continue
        print(f"\n=== membaca {cat} ({sources[cat].name}) ===")
        subs_all, items = parse_source_with_subs(sources[cat])
        subs_of[cat] = [s for s in subs_all if not a.subs or s in a.subs]
        for s in subs_of[cat]:
            S[(cat, s)]  # subtopik kosong pun tercatat

        by_sub, uniq_src = {}, {}
        for sub, url in items:
            if a.subs and sub not in a.subs:
                continue
            key = (sub, src_key(url))
            if key in uniq_src:
                print(f"  [dobel] sumber sama di '{sub}', dilewati")
                continue
            uniq_src[key] = url
            S[(cat, sub)]["vid" if is_single(url) else "pl"] += 1
            vs, src = expand(url, a, cache)
            print(f"  {sub:<28} {url[:58]:<58} -> {len(vs)} video [{src}]")
            for v in vs:
                v["sub"], v["source_url"] = sub, url
            by_sub.setdefault(sub, []).append(vs)
        save_cache(cache)

        ordered = interleave([interleave(l) for l in by_sub.values()])
        first, also = {}, {}
        for v in ordered:
            if v["video_id"] in first:
                also.setdefault(v["video_id"], set()).add(v["sub"])
            else:
                first[v["video_id"]] = v
        contrib = Counter(v["source_url"] for v in first.values())
        for (sub, _), url in uniq_src.items():
            src_rows.append({
                "category": cat,
                "subtopic": sub,
                "source_url": url,
                "jenis": "video" if is_single(url) else "playlist",
                "video_unik": contrib.get(url, 0),
            })

        for vid, v in first.items():
            st = S[(cat, v["sub"])]
            if claimed.setdefault(vid, cat) != cat:  # sudah dimiliki bidang lain
                st["dup_x"] += 1
                continue
            d = v["duration"]
            usable = not (d is not None and d < a.min_duration_min * 60)
            st["n"] += 1
            if d is None:
                st["unk"] += 1
            if usable:
                st["usable"] += 1
                st["h_clip"] += (
                    (min(d, clip_s) if d else clip_s) if a.clip_minutes else (d or 0)
                ) / 3600
            else:
                st["short"] += 1
            m = manifest.get(vid)
            done = bool(m and m.get("status") == "ok")
            st["done"] += done
            inv.append({
                "category": cat,
                "subtopic": v["sub"],
                "video_id": vid,
                "title": v["title"],
                "duration_sec": d if d is not None else "",
                "layak": "ya" if usable else "terlalu_pendek",
                "playlist_title": v["playlist_title"],
                "source_url": v["source_url"],
                "juga_ada_di": ";".join(sorted(also.get(vid, set()) - {v["sub"]})),
                "sudah_diunduh": "ya" if done else "",
            })

    # ---------------- laporan ----------------
    print("\n" + "=" * 112)
    print(f"RINGKASAN PER BIDANG / SUBTOPIK  (layak = durasi >= {a.min_duration_min:.0f} menit; "
          f"jam_klip = jam audio setelah dipotong {a.clip_minutes} menit)")
    print("=" * 112)
    hdr = (f"{'bidang':<14}{'subtopik':<30}{'sumber':>9}{'unik':>6}{'layak':>7}{'pendek':>8}"
           f"{'?dur':>6}{'jam_klip':>10}{'unduh':>7}  catatan")
    print(hdr)
    print("-" * len(hdr))
    for cat in cats:
        if cat not in subs_of:
            continue
        subs = subs_of[cat]
        fair = a.target / max(1, len(subs))
        for sub in subs:
            s = S[(cat, sub)]
            note = ("KOSONG - belum ada link" if s["n"] == 0
                    else f"di bawah jatah adil ({fair:.0f})" if s["usable"] < fair else "")
            print(f"{cat:<14}{sub:<30}{str(s['pl']) + 'pl/' + str(s['vid']) + 'v':>9}{s['n']:>6}"
                  f"{s['usable']:>7}{s['short']:>8}{s['unk']:>6}{s['h_clip']:>10.1f}{s['done']:>7}  {note}")
        print()

    print("=" * 60)
    print("TOTAL PER BIDANG")
    print("=" * 60)
    grand = 0
    for cat in cats:
        if cat not in subs_of:
            continue
        rows = [S[k] for k in S if k[0] == cat]
        usable = sum(r["usable"] for r in rows)
        grand += usable
        dup = sum(r["dup_x"] for r in rows)
        gap = f"kurang {a.target - usable}" if usable < a.target else "cukup"
        print(f"{cat:<14} unik {sum(r['n'] for r in rows):>5} | layak {usable:>5} | target {a.target} -> {gap:<11}"
              f"| jam_klip {sum(r['h_clip'] for r in rows):>6.1f} | sudah diunduh {sum(r['done'] for r in rows):>4}"
              + (f" | dobel lintas-bidang {dup}" if dup else ""))
    print(f"{'SEMUA':<14} layak {grand}")

    dead = [r for r in src_rows if r["video_unik"] == 0]
    if dead:
        print(f"\nSumber yang menyumbang 0 video ({len(dead)}) - mati/privat, gagal dibaca, atau semuanya duplikat:")
        for r in dead:
            print(f"  {r['category']}/{r['subtopic']}: {r['source_url'][:90]}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    for path, rows in ((SOURCE_INVENTORY, inv), (SOURCE_PLAYLISTS, src_rows)):
        if rows:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
    print(f"\nDetail per video : {SOURCE_INVENTORY.relative_to(REPORTS_DIR.parent.parent)}")
    print(f"Detail per sumber: {SOURCE_PLAYLISTS.relative_to(REPORTS_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
