"""
scraper/core.py - engine scraping audio YouTube -> WAV 16 kHz mono (dengan adab).

Dipakai oleh:
    collect_audio.py          (CLI generik, --cats bebas)
    scrape_<bidang>.py        (bidang terkunci, mis. scrape_sainsmath.py)

Format file sumber (00_SumberData/playlist_<bidang>[.txt]):
    ## 1. nama_subtopik                      <- header = subtopik
    https://youtube.com/playlist?list=...    <- playlist
    https://youtu.be/xxxxxxxxxxx             <- video satuan
  Baris tanpa URL = catatan (diabaikan). Dua URL menempel di satu baris tetap terbaca.

Output:
    data/raw/<bidang>/<subtopik>/<bidang>-<subtopik>-<0001>-<video_id>.wav
    data/manifest.csv   (append-only: ID yang sudah diberikan tidak pernah berubah)
"""
import argparse, csv, json, os, random, re, shutil, sys, time
from collections import Counter
from datetime import datetime
from pathlib import Path

# Console Windows bisa memakai cp1252; paksa UTF-8 agar karakter aman.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import yt_dlp
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import download_range_func
except ImportError:
    sys.exit("yt-dlp belum terpasang -> pip install -U yt-dlp")

# core.py ada di <root>/scraper/core.py -> ROOT harus naik satu level.
ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "00_SumberData"
RAW_DIR = ROOT / "data" / "raw"
TMP_DIR = ROOT / "data" / "_tmp"
MANIFEST = ROOT / "data" / "manifest.csv"
CACHE = ROOT / "data" / "cache" / "expand.json"
CACHE_TTL_H = 24
SAMPLE_RATE = 16000
BLOCK_HINTS = ("429", "too many requests", "sign in to confirm")

FIELDS = ["audio_id", "category", "subtopic", "seq", "video_id", "title", "channel",
          "license", "duration_src_sec", "clip_sec", "sr", "url", "source_url",
          "playlist_title", "other_sources", "lang", "status", "error", "filepath",
          "yt_dlp_version", "downloaded_at"]

HEADER = re.compile(r"^\s*#{1,6}\s*(?:\d+\s*[.)]\s*)?(.+?)\s*$")
URL_RE = re.compile(r"https?://.*?(?=https?://|\s|$)")      # memisahkan URL yang menempel
NAME_RE = re.compile(r"^(?P<cat>[a-z0-9_]+)-(?P<sub>[a-z0-9_]+)-(?P<seq>\d{4})-"
                     r"(?P<vid>[A-Za-z0-9_-]{11})\.wav$")

# opsi "sopan" untuk semua panggilan yt-dlp
POLITE = {"sleep_interval_requests": 1, "concurrent_fragment_downloads": 1,
          "retry_sleep_functions": {"http": lambda n: min(60, 2 ** n),
                                    "fragment": lambda n: min(60, 2 ** n),
                                    "extractor": lambda n: min(60, 2 ** n)}}


# ---------------- util ----------------
def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "umum"


def src_key(url):
    """ID playlist/video dari URL; URL sama (beda ?si=) dianggap satu sumber."""
    m = re.search(r"(?:list=|v=|youtu\.be/)([\w-]+)", url)
    return m.group(1) if m else url


def interleave(lists):
    """[[a1,a2],[b1,b2,b3]] -> [a1,b1,a2,b2,b3] (deterministik, sampling seimbang)."""
    lists = [l for l in lists if l]
    out, i = [], 0
    while any(i < len(l) for l in lists):
        out += [l[i] for l in lists if i < len(l)]
        i += 1
    return out


# ---------------- sumber ----------------
def discover():
    """playlist_sainsmath.txt / playlist_soshum (tanpa ekstensi) -> {'sainsmath': Path, ...}"""
    return {slug(f.stem[len("playlist_"):]): f
            for f in sorted(SRC_DIR.glob("playlist_*")) if f.is_file()}


def parse_source(path):
    sub, items = "umum", []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        m = HEADER.match(line)
        if m:
            sub = slug(m.group(1)); continue
        for u in URL_RE.findall(line):
            u = u.strip().rstrip(",;")
            if "youtube.com" in u or "youtu.be" in u:
                items.append((sub, u))
            else:
                print(f"  [!] bukan URL YouTube, dilewati: {u[:60]}")
    return items


def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def fetch_entries(url):
    """Baca isi playlist/video dari YouTube (ringan: hanya metadata, tanpa download)."""
    single = ("watch?v=" in url) or ("youtu.be/" in url)
    opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True,
            "skip_download": True, "ignoreerrors": True, "noplaylist": single, **POLITE}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"    [!] gagal baca sumber: {str(e)[:80]}"); return []
    if not info:
        return []
    ptitle, ents = (info.get("title", ""), [e for e in info["entries"] if e]) \
        if "entries" in info else ("", [info])
    out = []
    for e in ents:
        vid = e.get("id") or ""
        if len(vid) != 11 or (e.get("title") or "") in ("[Private video]", "[Deleted video]"):
            continue                                   # nested playlist / video tak tersedia
        out.append({"video_id": vid, "title": e.get("title") or "", "duration": e.get("duration"),
                    "playlist_title": ptitle, "url": f"https://www.youtube.com/watch?v={vid}"})
    return out


def expand(url, a, cache):
    key = src_key(url)
    hit = cache.get(key)
    if hit and not a.refresh and time.time() - hit["t"] < CACHE_TTL_H * 3600:
        out, src = hit["entries"], "cache"
    else:
        out, src = fetch_entries(url), "web"
        if out:
            cache[key] = {"t": time.time(), "entries": out}
        time.sleep(random.uniform(1.5, 4.0))           # adab: jeda antar-permintaan
    if a.max_per_playlist:
        out = out[:a.max_per_playlist]
    return [dict(e) for e in out], src                 # salinan, agar cache tidak ikut berubah


# ---------------- manifest ----------------
def load_manifest():
    if not MANIFEST.exists():
        return {}
    with open(MANIFEST, newline="", encoding="utf-8-sig") as f:
        return {r["video_id"]: r for r in csv.DictReader(f)}


def save_manifest(rows):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (r["category"], int(r["seq"]))):
            w.writerow({k: r.get(k, "") for k in FIELDS})
    try:
        os.replace(tmp, MANIFEST)
    except PermissionError:
        print("  [!] manifest.csv sedang dibuka (Excel?). Data aman di memori, disimpan lagi di langkah berikutnya.")


def adopt_existing(rows):
    """Jika manifest hilang tapi file WAV masih ada, daftarkan kembali (ID tetap sama)."""
    n = 0
    for p in RAW_DIR.rglob("*.wav"):
        m = NAME_RE.match(p.name)
        if m and m["vid"] not in rows:
            rows[m["vid"]] = {"audio_id": f'{m["cat"]}-{m["sub"]}-{m["seq"]}', "category": m["cat"],
                              "subtopic": m["sub"], "seq": int(m["seq"]), "video_id": m["vid"],
                              "url": f'https://www.youtube.com/watch?v={m["vid"]}',
                              "status": "ok", "error": "adopted_from_disk",
                              "filepath": p.relative_to(ROOT).as_posix()}
            n += 1
    if n:
        print(f"[i] {n} file WAV lama didaftarkan ulang ke manifest")


# ---------------- download ----------------
def fetch_info(url):
    with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                    "noplaylist": True, **POLITE}) as ydl:
        return ydl.extract_info(url, download=False)


def download(url, vid, clip_min):
    tmp = TMP_DIR / vid                      # unduh ke folder sementara dulu, baru dipindah (atomik)
    shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
    opts = {"format": "bestaudio/best", "outtmpl": str(tmp / "%(id)s.%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True, "retries": 3, **POLITE,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
            "postprocessor_args": {"extractaudio": ["-ar", str(SAMPLE_RATE), "-ac", "1"]}}
    if clip_min > 0:
        opts["download_ranges"] = download_range_func(None, [(0, clip_min * 60)])
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    wavs = sorted(tmp.glob("*.wav"))
    if not wavs:
        raise RuntimeError("file wav tidak terbentuk")
    return info, wavs[0], tmp


# ---------------- perencanaan ----------------
def build_candidates(items, a, cache):
    by_sub, seen_src = {}, set()
    for sub, url in items:
        if a.subs and sub not in a.subs:
            continue
        key = (sub, src_key(url))
        if key in seen_src:
            print(f"  [dobel] sumber sama di '{sub}', dilewati"); continue
        seen_src.add(key)
        vs, src = expand(url, a, cache)
        print(f"  {sub:<28} {url[:58]:<58} -> {len(vs)} video [{src}]")
        for v in vs:
            v["sub"], v["source_url"] = sub, url
        by_sub.setdefault(sub, []).append(vs)
    ordered = interleave([interleave(lists) for lists in by_sub.values()])
    first, also = {}, {}
    for v in ordered:
        if v["video_id"] in first:
            also.setdefault(v["video_id"], set()).add(v["sub"])
        else:
            first[v["video_id"]] = v
    for vid, v in first.items():
        v["other_sources"] = ";".join(sorted(also.get(vid, set()) - {v["sub"]}))
    return list(first.values())


def main(argv=None, default_cats=None):
    """Jalankan engine. ``default_cats`` dipakai oleh skrip per bidang."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cats", nargs="+", help="bidang yang dijalankan (default: semua playlist_*)")
    ap.add_argument("--subs", nargs="+", help="batasi ke subtopik tertentu (nama slug)")
    ap.add_argument("--clip-minutes", type=int, default=0, help="ambil N menit pertama (0 = full, default)")
    ap.add_argument("--max-per-playlist", type=int, default=0, help="maks video per playlist (0 = semua)")
    ap.add_argument("--quota-cat", type=int, default=0,
                    help="target total video OK per bidang; diambil bergiliran antar subtopik")
    ap.add_argument("--max-per-run", type=int, default=0, help="maks video diunduh per sesi (0 = semua)")
    ap.add_argument("--min-duration-min", type=float, default=5, help="lewati video lebih pendek (menit)")
    ap.add_argument("--only-cc", action="store_true", help="hanya video berlisensi Creative Commons")
    ap.add_argument("--skip-failed", action="store_true", help="jangan coba ulang yang gagal")
    ap.add_argument("--refresh", action="store_true", help="abaikan cache isi playlist")
    # --- adab ---
    ap.add_argument("--sleep-min", type=float, default=4, help="jeda minimum antar video (detik)")
    ap.add_argument("--sleep-max", type=float, default=10, help="jeda maksimum antar video (detik)")
    ap.add_argument("--batch-size", type=int, default=25, help="istirahat panjang tiap N video")
    ap.add_argument("--batch-pause", type=float, default=90, help="lama istirahat panjang (detik)")
    ap.add_argument("--cooldown", type=float, default=300, help="jeda bila kena 429/bot-check (detik)")
    ap.add_argument("--max-consecutive-fails", type=int, default=5,
                    help="berhenti bila gagal beruntun sebanyak ini")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="lewati konfirmasi")
    a = ap.parse_args(argv)
    if a.sleep_min > a.sleep_max:
        a.sleep_min, a.sleep_max = a.sleep_max, a.sleep_min

    ver = yt_dlp.version.__version__
    has_ff = bool(shutil.which("ffmpeg"))
    print(f"yt-dlp {ver} | ffmpeg {'OK' if has_ff else 'TIDAK ADA'} | python {sys.version.split()[0]}")
    if not has_ff:
        sys.exit("ffmpeg tidak ada di PATH (conda install -c conda-forge ffmpeg)")

    rows = load_manifest()
    adopt_existing(rows)
    cache = load_cache()
    claimed = {vid: r["category"] for vid, r in rows.items()}     # video -> bidang pemilik
    sources = discover()
    if not sources:
        sys.exit(f"Tidak ada file playlist_* di {SRC_DIR}")
    cats = a.cats or default_cats or list(sources)

    # ---- 1) rencana ----
    plan = []                                   # (cat, sub, seq, audio_id, video)
    for cat in cats:
        if cat not in sources:
            print(f"[!] file sumber '{cat}' tidak ditemukan. Tersedia: {', '.join(sources)}"); continue
        print(f"\n=== {cat}  ({sources[cat].name}) ===")
        items = parse_source(sources[cat])
        if not items:
            print(f"  (belum ada URL; ukuran file {sources[cat].stat().st_size} byte "
                  f"-> sudah disimpan?)"); continue
        cands = build_candidates(items, a, cache)
        save_cache(cache)
        ok_n = sum(1 for r in rows.values() if r["category"] == cat and r["status"] == "ok")
        next_seq = 1 + max([int(r["seq"]) for r in rows.values() if r["category"] == cat] or [0])
        todo, n_short = [], 0
        for v in cands:
            owner = claimed.setdefault(v["video_id"], cat)
            if owner != cat:
                print(f"  [dobel lintas-bidang] {v['video_id']} milik '{owner}', dilewati"); continue
            old = rows.get(v["video_id"])
            if old and old["status"] == "ok" and (ROOT / old["filepath"]).exists():
                continue
            if old and old["status"] == "failed" and a.skip_failed:
                continue
            if v["duration"] and v["duration"] < a.min_duration_min * 60:
                n_short += 1; continue
            todo.append(v)
        if a.quota_cat:
            todo = todo[:max(0, a.quota_cat - ok_n)]
        start = len(plan)
        for v in todo:
            old = rows.get(v["video_id"])
            if old:                              # coba ulang -> ID lama dipakai lagi
                sub, seq = old["subtopic"], int(old["seq"])
            else:
                sub, seq = v["sub"], next_seq; next_seq += 1
            plan.append((cat, sub, seq, f"{cat}-{sub}-{seq:04d}", v))
        per_sub = Counter(p[1] for p in plan[start:])
        print(f"  sudah OK: {ok_n} | akan diunduh: {len(todo)} | dilewati (terlalu pendek): {n_short}")
        for s, n in per_sub.items():
            print(f"     - {s}: {n}")

    if a.max_per_run:
        plan = plan[:a.max_per_run]
    clip_s = a.clip_minutes * 60
    est_gb = sum(min(p[4]["duration"] or 600, clip_s or 10**9) for p in plan) * SAMPLE_RATE * 2 / 1e9
    avg = (a.sleep_min + a.sleep_max) / 2
    pause_min = (len(plan) * avg + (len(plan) // max(1, a.batch_size)) * a.batch_pause) / 60
    print(f"\nTotal rencana: {len(plan)} video (perkiraan ukuran WAV ~ {est_gb:.1f} GB)")
    print(f"Waktu jeda saja ~ {pause_min:.0f} menit (belum termasuk waktu unduh)")
    if a.dry_run:
        for cat, sub, seq, aid, v in plan[:20]:
            print(f"  [dry] {cat}/{sub}/{aid}-{v['video_id']}.wav | {v['title'][:50]}")
        if len(plan) > 20:
            print(f"  ... +{len(plan) - 20} lainnya")
        return
    if not plan:
        print("Tidak ada yang perlu diunduh. (idempotent: semua sudah lengkap)"); return
    if not a.yes and input("Lanjut download? [y/N] ").strip().lower() != "y":
        return

    # ---- 2) eksekusi ----
    ok = bad = consec = 0
    try:
        for i, (cat, sub, seq, audio_id, v) in enumerate(plan, 1):
            vid = v["video_id"]
            final = RAW_DIR / cat / sub / f"{audio_id}-{vid}.wav"
            if a.only_cc:
                try:
                    lic = (fetch_info(v["url"]) or {}).get("license") or ""
                except Exception:
                    lic = ""
                time.sleep(random.uniform(1, 3))
                if "creative commons" not in lic.lower():
                    print(f"[{i}/{len(plan)}] [skip non-CC] {vid}"); continue
            row = {"audio_id": audio_id, "category": cat, "subtopic": sub, "seq": seq,
                   "video_id": vid, "title": v["title"], "url": v["url"],
                   "source_url": v["source_url"], "playlist_title": v["playlist_title"],
                   "other_sources": v["other_sources"], "duration_src_sec": v["duration"] or "",
                   "sr": SAMPLE_RATE, "lang": "", "yt_dlp_version": ver,
                   "downloaded_at": datetime.now().isoformat(timespec="seconds")}
            failed_now, blocked = False, False
            try:
                info, wav, tmp = download(v["url"], vid, a.clip_minutes)
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(wav, final)
                shutil.rmtree(tmp, ignore_errors=True)
                dur = info.get("duration") or v["duration"] or ""
                clip = (min(clip_s, dur) if dur else clip_s) if a.clip_minutes else dur
                row.update(status="ok", error="", filepath=final.relative_to(ROOT).as_posix(),
                           title=info.get("title") or v["title"], channel=info.get("channel") or "",
                           license=info.get("license") or "", duration_src_sec=dur, clip_sec=clip)
                ok += 1
                print(f"[{i}/{len(plan)}] [ok]    {final.name}")
            except Exception as e:
                msg = str(e)
                blocked = any(h in msg.lower() for h in BLOCK_HINTS)
                row.update(status="failed", error=msg[:200], filepath="")
                failed_now = True; bad += 1
                print(f"[{i}/{len(plan)}] [GAGAL] {vid}: {msg[:90]}")
            rows[vid] = row
            save_manifest(rows)                   # simpan tiap video: aman kalau terputus

            if failed_now:
                consec += 1
                if consec >= a.max_consecutive_fails:
                    print(f"\n[STOP] {consec} kegagalan beruntun (kemungkinan rate-limit/blokir). "
                          f"Kemajuan tersimpan; coba lagi beberapa jam kemudian.")
                    break
                wait = a.cooldown if blocked else min(300, 30 * 2 ** (consec - 1))
                print(f"      jeda {wait:.0f} dtk sebelum lanjut" + (" (terdeteksi pembatasan)" if blocked else ""))
                time.sleep(wait)
                continue
            consec = 0
            if i < len(plan):
                time.sleep(random.uniform(a.sleep_min, a.sleep_max))
                if i % a.batch_size == 0:
                    t = a.batch_pause * random.uniform(0.7, 1.3)
                    print(f"      -- istirahat {t:.0f} dtk (tiap {a.batch_size} video) --")
                    time.sleep(t)
    except KeyboardInterrupt:
        print("\nDihentikan manual. Kemajuan tersimpan; jalankan lagi untuk melanjutkan.")

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    save_manifest(rows)
    print(f"\nSelesai sesi ini: {ok} berhasil, {bad} gagal. Jalankan: python audit_dataset.py")


if __name__ == "__main__":
    main()
