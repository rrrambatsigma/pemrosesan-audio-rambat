"""Akses YouTube: probe metadata, unduh audio sumber, potong jendela klip.

Prinsip:
- satu video diunduh **sekali** (``data/source/<id>.opus``), lalu dipotong
  menjadi beberapa jendela 60 detik tanpa tambahan bandwidth;
- jendela dipilih deterministik dari ``video_id`` (hasil sama saat resume);
- intro/outro dibuang, karena 60 detik pertama hampir selalu jingle/salam.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
import time
from pathlib import Path

import soundfile as sf
import yt_dlp
from yt_dlp import YoutubeDL

from .config import (INTRO_SKIP_S, MIN_GAP_S, OUTRO_SKIP_S, PROBE_DIR,
                     SAMPLE_RATE, SOURCE_DIR, TMP_DIR)
from .search import POLITE


class YoutubeError(RuntimeError):
    """Probe / unduh gagal (privat, dibatasi, atau tidak ada)."""


# --------------------------------------------------------------------------- #
# util
# --------------------------------------------------------------------------- #
def sha256_file(path: str | Path, block: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(block)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def audio_duration(path: str | Path) -> float:
    info = sf.info(str(path))
    return info.frames / info.samplerate if info.samplerate else 0.0


def audio_info(path: str | Path) -> tuple[int, int]:
    """(sample_rate, channels) dari berkas audio."""
    info = sf.info(str(path))
    return int(info.samplerate or 0), int(info.channels or 0)


def _opts(browser: str | None, extra: dict | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": False,
        **POLITE,
    }
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    if extra:
        opts.update(extra)
    return opts


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def _probe_path(video_id: str) -> Path:
    return PROBE_DIR / f"{video_id}.json"


def probe(video_id: str, *, refresh: bool = False,
          browser: str | None = None) -> dict:
    """Metadata satu video (di-cache permanen; tidak diulang tiap run)."""
    path = _probe_path(video_id)
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with YoutubeDL(_opts(browser)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        raise YoutubeError(f"probe: {type(e).__name__}: {e}"[:220]) from e
    if not info:
        raise YoutubeError("probe: metadata kosong")

    data = {
        "video_id": video_id,
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "channel_id": info.get("channel_id") or "",
        "channel_url": info.get("channel_url") or "",
        "license": info.get("license") or "",
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date") or "",
        "lang": info.get("language") or "",
        "webpage_url": info.get("webpage_url") or url,
        "yt_dlp_version": yt_dlp.version.__version__,
    }
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return data


# --------------------------------------------------------------------------- #
# unduh audio sumber
# --------------------------------------------------------------------------- #
def source_path(video_id: str, ext: str = "opus") -> Path:
    return SOURCE_DIR / f"{video_id}.{ext}"


def have_source(video_id: str, min_bytes: int = 20_000,
                ext: str = "opus") -> bool:
    p = source_path(video_id, ext)
    return p.exists() and p.stat().st_size >= min_bytes


def _has_audio(path: Path, min_bytes: int = 20_000) -> bool:
    return path.exists() and path.stat().st_size >= min_bytes


def to_wav(src: Path, dst: Path, *, sr: int | None = 16000,
            mono: bool = True) -> None:
    """Konversi cadangan kalau postprocessor tidak menghasilkan WAV."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel",
           "error", "-nostdin", "-i", str(src), "-vn"]
    if sr:
        cmd += ["-ar", str(int(sr))]
    if mono:
        cmd += ["-ac", "1"]
    cmd += ["-c:a", "pcm_s16le", "-f", "wav", "-y", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not _has_audio(dst, 1000):
        dst.unlink(missing_ok=True)
        raise YoutubeError(f"ffmpeg konversi WAV gagal: {(r.stderr or '')[:180]}")


def download_source(video_id: str, *, browser: str | None = None,
                    quality: str = "48", raw: bool = False,
                    to_dir: str | Path | None = None,
                    wav_sr: int | None = 16000,
                    wav_mono: bool = True) -> Path:
    """Unduh audio penuh sekali, atomik, idempoten.

    ``raw=True`` menghasilkan **WAV utuh** (default 16 kHz mono PCM16) â€”
    dipakai mode raw yang langsung dikirim ke Hugging Face.
    ``to_dir`` mengarahkan hasil ke folder transit (mis. ``data/_tmp/hub``).
    """
    ext = "wav" if raw else "opus"
    base = Path(to_dir) if to_dir else SOURCE_DIR
    final = base / f"{video_id}.{ext}"
    if _has_audio(final):
        return final
    if not shutil.which("ffmpeg"):
        raise YoutubeError("ffmpeg tidak ada di PATH")

    base.mkdir(parents=True, exist_ok=True)
    tmp = TMP_DIR / "yt" / f"{video_id}_{ext}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    if raw:
        codec, args = "wav", []
        if wav_sr:
            args += ["-ar", str(int(wav_sr))]
        if wav_mono:
            args += ["-ac", "1"]
    else:
        codec, args = "opus", ["-ar", "16000", "-ac", "1"]

    opts = _opts(browser, {
        "skip_download": False,
        "format": "bestaudio/best",
        "outtmpl": str(tmp / "%(id)s.%(ext)s"),
        "retries": 3,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": None if raw else quality,
        }],
    })
    if args:
        opts["postprocessor_args"] = {"extractaudio": args}
    try:
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}",
                             download=True)
        made = sorted(tmp.glob(f"*.{ext}"))
        if not made:                       # cadangan: konversi manual
            others = [p for p in tmp.iterdir()
                      if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
            if not others:
                raise YoutubeError("yt-dlp tidak menghasilkan berkas audio")
            made = [max(others, key=lambda p: p.stat().st_size)]
            if raw:
                to_wav(made[0], tmp / f"{video_id}.wav",
                        sr=wav_sr, mono=wav_mono)
                made = [tmp / f"{video_id}.wav"]
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(made[0]), str(final))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not _has_audio(final):
        raise YoutubeError("file hasil unduhan terlalu kecil / kosong")
    return final


# --------------------------------------------------------------------------- #
# pemilihan jendela
# --------------------------------------------------------------------------- #
def _seed(video_id: str) -> int:
    return int(hashlib.md5(video_id.encode()).hexdigest()[:8], 16)


def pick_windows(duration: float, n: int = 4, clip_sec: float = 60.0,
                 intro: float = INTRO_SKIP_S, outro: float = OUTRO_SKIP_S,
                 min_gap: float = MIN_GAP_S, seed: int | None = None) -> list[int]:
    """Pilih ``n`` titik mulai (detik) yang tersebar, tidak tumpang tindih.

    Deterministik untuk ``seed`` yang sama. Video pendek otomatis
    mendapatkan jendela lebih sedikit (atau kosong bila tidak layak).
    """
    dur = float(duration or 0)
    if dur <= 0 or clip_sec <= 0:
        return []

    # video pendek: jangan buang intro 90 detik (bisa menghabiskan durasi)
    if dur < 15 * 60:
        lo = min(intro, dur * 0.05)
        hi = dur - min(outro, dur * 0.03)
    else:
        lo = min(intro, max(0.0, dur - clip_sec))
        hi = dur - min(outro, max(0.0, dur - clip_sec))

    span = hi - lo
    if span < clip_sec:                      # jangan sampai kehilangan video
        lo, hi, span = 0.0, dur, dur
    if span < clip_sec * 0.8:
        return []

    fit = int((span + min_gap) // (clip_sec + min_gap))
    n = max(1, min(int(n), fit))
    rng = random.Random(0 if seed is None else seed)

    slack = max(0.0, span - (n * clip_sec + (n - 1) * min_gap))
    slot = slack / n
    starts: list[int] = []
    cursor = lo
    for i in range(n):
        start = cursor + rng.uniform(0.0, slot)
        start = min(start, hi - clip_sec)
        if starts and start < starts[-1] + clip_sec + min_gap:
            start = starts[-1] + clip_sec + min_gap
        start = min(start, dur - clip_sec)
        starts.append(int(round(start)))
        cursor = start + clip_sec + min_gap

    # buang duplikat / yang mundur (pembulatan)
    clean: list[int] = []
    for s in starts:
        s = max(0, min(s, int(dur - clip_sec)))
        if clean and s < clean[-1] + clip_sec + min_gap:
            continue
        clean.append(s)
    return clean


# --------------------------------------------------------------------------- #
# potong klip
# --------------------------------------------------------------------------- #
def cut(src: str | Path, out: str | Path, start: float, sec: float,
        sample_rate: int = SAMPLE_RATE) -> tuple[int, float]:
    """Potong ``[start, start+sec]`` dari audio sumber -> WAV mono 16 kHz.

    Mengembalikan ``(bytes, durasi_hasil)``. Penulisan atomik: file jadi
    hanya muncul kalau ffmpeg sukses.
    """
    src, out = Path(src), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # nama sementara harus tetap berakhiran .wav supaya ffmpeg tahu formatnya
    tmp = out.with_name(out.stem + ".part.wav")
    tmp.unlink(missing_ok=True)
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-nostdin", "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(src),
        "-t", f"{float(sec):.3f}", "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", "-f", "wav", "-y", str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        raise YoutubeError(f"ffmpeg gagal: {(r.stderr or '')[:180]}")
    tmp.replace(out)
    return out.stat().st_size, audio_duration(out)


def wait(browser: str | None = None) -> None:  # pragma: no cover - jeda sopan
    time.sleep(random.uniform(1.0, 3.0))
