"""Metrik kualitas audio untuk dataset (bisa diuji tanpa CLI).

Semua fungsi di sini murni membaca audio; tidak menulis/mengubah file dataset.
Dipakai oleh ``scripts/audio_quality.py``.

Metrik yang dihitung:
- teknis       : sr, kanal, subtype, durasi, DC offset, energi per kanal
- kenyaringan  : LUFS (pyloudnorm), RMS dBFS, peak dBFS
- clipping     : rasio sampel mentok, jumlah run, run terpanjang
- SNR          : speech vs non-speech dari VAD Silero
- gema         : indikator peluruhan energi setelah bicara berhenti (relatif)
- frekuensi    : rolloff 95%, bandwidth 99,5% energi, deteksi pita telepon
- jeda/kecepatan: rasio bicara, jumlah/panjang jeda, onset per detik

Penilaian:
- flags        : catatan tiga tingkat (hard / warn / info)
- skor         : rata-rata berbobot enam komponen (quality_score)
- gate         : accept / review / reject + alasannya
"""

from __future__ import annotations

import math
from pathlib import Path

import librosa
import numpy as np
import pyloudnorm as pyln
import soundfile as sf

SAMPLE_RATE = 16000
CLIP_THRESHOLD = 32767.0 / 32768.0
SILENCE_DBFS = -50.0
FRAME_SEC = 0.025

# Ambang gate QC. Tiga kelompok:
#   hard  : kegagalan wajib -> reject (skor dipaksa 0)
#   warn  : memicu review (tidak bisa diperbaiki otomatis)
#   score : parameter rumus skor, bukan ambang
# Angka diambil dari acuan (BS.1770 untuk LUFS, P.835/DNSMOS, literatur ASR
# SNR 10-15 dB) lalu dilunakkan ke distribusi dataset (lihat
# data/reports/audio_quality.csv). Bisa dioverride lewat --thresholds (JSON).
THRESHOLDS = {
    "hard": {
        "duration_min_s": 45.0,
        "silence_ratio_max": 0.98,
        "speech_ratio_min": 0.15,
        "clip_ratio_max": 1e-3,
        "dc_offset_max": 0.05,
    },
    "warn": {
        "sr_expected": 16000,
        "channels_expected": 1,
        "snr_min_db": 8.0,
        "speech_ratio_warn": 0.45,
        "dnsmos_ovrl_min": 2.8,
        "dnsmos_sig_min": 2.8,
        "dnsmos_bak_min": 2.6,
        "bandwidth_min_hz": 2500.0,
        "music_ratio_max": 0.15,
        "applause_ratio_max": 0.10,
        "overlap_max": 0.10,
        # --- catatan saja: tidak memicu review ---
        "lufs_min": -32.0,
        "lufs_max": -12.0,
        "clip_ratio_warn": 1e-4,
        "clip_longest_warn": 24,
        "dc_offset_warn": 0.02,
        "channel_imbalance_max_db": 20.0,
        "longest_pause_max_s": 12.0,
        "pause_ratio_max": 0.6,
        "onset_rate_min": 1.5,
        "onset_rate_max": 7.0,
        "rt60_percentile": 90.0,
    },
    "score": {
        "speech_floor": 0.20,
        "speech_span": 0.50,
        "snr_floor": 4.0,
        "snr_span": 16.0,
        "dnsmos_floor": 2.2,
        "dnsmos_span": 1.4,
        "lufs_lo": -30.0,
        "lufs_hi": -16.0,
        "lufs_quiet_span": 12.0,
        "lufs_loud_span": 8.0,
        "bandwidth_floor": 2500.0,
        "bandwidth_span": 2500.0,
        "clip_floor": 1e-4,
        "clip_span": 1.9e-3,
        "dc_floor": 0.01,
        "dc_span": 0.04,
        "silence_floor": 0.85,
        "silence_span": 0.10,
        "weights": {
            "speech": 0.25,
            "noise": 0.20,
            "quality": 0.20,
            "level": 0.15,
            "clarity": 0.10,
            "artifacts": 0.10,
        },
    },
    "gate": {"review_below_score": 75},
}

AUDIO_FIELDS = [
    "filepath",
    "audio_id",
    "video_id",
    "category",
    "subtopic",
    "manifest_status",
    "duration_s",
    "sr",
    "channels",
    "subtype",
    "readable",
    "error",
    "sample_mode",
    "analyzed_sec",
    "peak_dbfs",
    "rms_dbfs",
    "lufs",
    "lufs_estimated",
    "clip_ratio",
    "clip_runs",
    "clip_longest",
    "dc_offset",
    "zero_energy",
    "silence_ratio",
    "channel_imbalance_db",
    "mono_cancel_db",
    "snr_db",
    "noise_floor_dbfs",
    "speech_level_dbfs",
    "speech_ratio_vad",
    "pause_count",
    "mean_pause_s",
    "longest_pause_s",
    "pause_ratio",
    "onset_rate_hz",
    "rolloff95_hz",
    "bandwidth_hz",
    "telephone_band",
    "rt60_proxy_s",
    "decay_db_per_s",
    "dnsmos_sig",
    "dnsmos_bak",
    "dnsmos_ovrl",
    "dnsmos_p808",
    "music_ratio",
    "applause_ratio",
    "laughter_ratio",
    "noise_ratio",
    "top_events",
    "diar_speakers",
    "overlap_ratio",
    "asr_wpm",
    "asr_words",
    "asr_lang",
    "gate",
    "gate_reason",
    "quality_score",
    "flags",
]


# --------------------------------------------------------------------------- #
# util
# --------------------------------------------------------------------------- #
def _db(value: float, floor: float = 1e-12) -> float:
    return 20.0 * math.log10(max(float(value), floor))


def _num(value) -> float | None:
    """Angka dari float/int/str CSV; None untuk kosong dan NaN."""
    if value is None or value == "" or value == "None":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out  # NaN -> None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _resample(y: np.ndarray, sr_in: int, sr_out: int = SAMPLE_RATE) -> np.ndarray:
    if sr_in == sr_out:
        return y.astype(np.float32, copy=False)
    from math import gcd

    g = gcd(int(sr_in), int(sr_out))
    from scipy.signal import resample_poly

    return resample_poly(y, sr_out // g, sr_in // g).astype(np.float32)


def _run_stats(mask: np.ndarray, prev_active: bool, prev_len: int):
    """Hitung jumlah & panjang run True dalam satu blok mask.

    ``prev_active``/``prev_len`` meneruskan run dari blok sebelumnya.
    """
    if mask.size == 0:
        return 0, 0, prev_active, prev_len
    idx = np.flatnonzero(mask)
    runs = 0
    longest = 0
    if idx.size == 0:
        return 0, 0, False, 0
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [idx.size - 1]))
    lengths = idx[ends] - idx[starts] + 1
    runs = int(lengths.size)
    longest = int(lengths.max())
    if prev_active and idx[0] == 0:
        # run pertama menyambung blok sebelumnya
        runs -= 1
        longest = max(longest, int(lengths[0]) + prev_len)
    return runs, longest, bool(mask[-1]), int(lengths[-1]) if mask[-1] else 0


def read_audio(path: str | Path, max_sec: float | None = None) -> tuple[np.ndarray, int]:
    """Baca audio mono float32 pada sample rate asli (atau dipotong)."""
    frames = -1
    if max_sec:
        info = sf.info(str(path))
        frames = int(max_sec * info.samplerate)
    y, sr = sf.read(str(path), dtype="float32", always_2d=True, frames=frames)
    return y.mean(axis=1), int(sr)


def extract_sample(
    path: str | Path,
    info: sf._SoundFileInfo,
    sample_sec: float = 120.0,
    windows: tuple[float, float, float] = (0.1, 0.5, 0.9),
) -> tuple[np.ndarray, str]:
    """Ambil sampel deterministik (10%, 50%, 90%) atau seluruh file bila pendek."""
    sr = int(info.samplerate)
    dur = info.frames / sr if sr else 0.0
    if sample_sec <= 0 or dur <= sample_sec:
        y, _ = read_audio(path)
        return _resample(y, sr), "full"

    win = sample_sec / len(windows)
    chunks = []
    with sf.SoundFile(str(path)) as f:
        for frac in windows:
            start = max(0.0, dur * frac - win / 2.0)
            start = min(start, max(0.0, dur - win))
            f.seek(int(start * sr))
            n = int(win * sr)
            block = f.read(n, dtype="float32", always_2d=True)
            if block.size == 0:
                continue
            chunks.append(block.mean(axis=1))
    if not chunks:
        return np.zeros(0, dtype=np.float32), "empty"
    return _resample(np.concatenate(chunks), sr), "sampled"


# --------------------------------------------------------------------------- #
# teknis + statistik streaming
# --------------------------------------------------------------------------- #
def _channel_stats(path: str | Path, info: sf._SoundFileInfo, max_sec: float = 60.0):
    if int(info.channels) < 2:
        return None, None
    y, sr = sf.read(
        str(path), dtype="float32", always_2d=True,
        frames=int(min(max_sec * info.samplerate, info.frames)),
    )
    if y.shape[1] < 2:
        return None, None
    left, right = y[:, 0], y[:, 1]
    rms_l = float(np.sqrt(np.mean(left ** 2) + 1e-12))
    rms_r = float(np.sqrt(np.mean(right ** 2) + 1e-12))
    imbalance = abs(20.0 * math.log10(max(rms_l, 1e-9) / max(rms_r, 1e-9)))
    mono = 0.5 * (left + right)
    rms_mono = float(np.sqrt(np.mean(mono ** 2) + 1e-12))
    ref = 0.5 * (rms_l + rms_r)
    cancel = 20.0 * math.log10(max(rms_mono, 1e-9) / max(ref, 1e-9))
    return round(imbalance, 2), round(cancel, 2)


def _stats_from_float(y: np.ndarray, sr: int) -> dict:
    total = int(y.size)
    if total == 0:
        return dict(peak_dbfs=None, rms_dbfs=None, clip_ratio=0.0, clip_runs=0,
                    clip_longest=0, dc_offset=0.0, zero_energy=1, silence_ratio=1.0,
                    max_abs=0.0)
    max_abs = float(np.max(np.abs(y)))
    rms = float(np.sqrt(np.mean(y.astype(np.float64) ** 2)))
    dc = float(np.mean(y, dtype=np.float64))
    mask = np.abs(y) >= CLIP_THRESHOLD
    runs, longest, _, _ = _run_stats(mask, False, 0)
    frame_len = max(1, int(FRAME_SEC * sr))
    usable = (total // frame_len) * frame_len
    if usable:
        frames = y[:usable].reshape(-1, frame_len).astype(np.float64)
        frms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-18)
        frms_db = 20.0 * np.log10(frms + 1e-12)
        silence_ratio = float(np.mean(frms_db < SILENCE_DBFS))
    else:
        silence_ratio = 1.0
    return dict(
        peak_dbfs=round(_db(max_abs), 2),
        rms_dbfs=round(_db(rms), 2),
        clip_ratio=round(float(mask.mean()), 8),
        clip_runs=runs,
        clip_longest=longest,
        dc_offset=round(dc, 6),
        zero_energy=int(max_abs == 0.0),
        silence_ratio=round(silence_ratio, 4),
        max_abs=max_abs,
    )


def _stats_streaming(path: str | Path, info: sf._SoundFileInfo, block_frames: int = 1_000_000):
    """Baca int16 per blok untuk file panjang (hemat memori)."""
    block = int(block_frames)
    max_abs = 0.0
    sumsq = 0.0
    dc = 0.0
    total = 0
    clip = 0
    runs = 0
    longest = 0
    prev_active = False
    prev_len = 0
    sil_frames = 0
    frame_total = 0
    frame_len = max(1, int(FRAME_SEC * int(info.samplerate)))
    with sf.SoundFile(str(path)) as f:
        while True:
            raw = f.read(block, dtype="int16", always_2d=True)
            if raw.size == 0:
                break
            if raw.shape[1] > 1:
                x = raw.astype(np.float32).mean(axis=1)
            else:
                x = raw[:, 0].astype(np.float32)
            if x.size == 0:
                continue
            max_abs = max(max_abs, float(np.max(np.abs(x))))
            x64 = x.astype(np.float64)
            sumsq += float(np.dot(x64, x64))
            dc += float(x64.sum())
            total += int(x.size)
            mask = np.abs(x) >= 32767.0
            clip += int(mask.sum())
            r, l, prev_active, prev_len = _run_stats(mask, prev_active, prev_len)
            runs += r
            longest = max(longest, l)
            usable = (x.size // frame_len) * frame_len
            if usable:
                frames = x[:usable].reshape(-1, frame_len).astype(np.float64)
                frms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-9)
                frms_db = 20.0 * np.log10(frms / 32768.0 + 1e-12)
                sil_frames += int(np.sum(frms_db < SILENCE_DBFS))
                frame_total += int(frms_db.size)
    return dict(
        peak_dbfs=round(_db(max_abs / 32768.0), 2) if total else None,
        rms_dbfs=round(10.0 * math.log10(max(sumsq / total, 1e-12) / 32768.0 ** 2), 2) if total else None,
        clip_ratio=round(clip / total, 8) if total else 0.0,
        clip_runs=runs,
        clip_longest=longest,
        dc_offset=round(dc / total / 32768.0, 6) if total else 0.0,
        zero_energy=int(max_abs == 0.0),
        silence_ratio=round(sil_frames / frame_total, 4) if frame_total else 1.0,
        max_abs=max_abs / 32768.0,
    )


def loudness(y: np.ndarray, sr: int) -> float | None:
    if y.size < int(0.5 * sr):
        return None
    try:
        meter = pyln.Meter(sr)
        value = float(meter.integrated_loudness(y.astype(np.float64)))
        if not math.isfinite(value):
            return None
        return round(value, 2)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# VAD, SNR, jeda, onset
# --------------------------------------------------------------------------- #
def _frame_powers(y: np.ndarray, sr: int, frame_sec: float = FRAME_SEC):
    frame_len = max(1, int(frame_sec * sr))
    usable = (len(y) // frame_len) * frame_len
    if usable == 0:
        return np.zeros(0), frame_sec
    frames = y[:usable].reshape(-1, frame_len).astype(np.float64)
    powers = np.mean(frames ** 2, axis=1) + 1e-18
    return powers, frame_len / sr


def vad_stats(y: np.ndarray, sr: int) -> dict:
    """Rasio bicara, jeda, SNR, dan level dari Silero VAD."""
    from .quality_models import get_vad_model

    dur = len(y) / sr if sr else 0.0
    empty = dict(
        speech_ratio_vad=None, pause_count=None, mean_pause_s=None,
        longest_pause_s=None, pause_ratio=None, snr_db=None,
        noise_floor_dbfs=None, speech_level_dbfs=None,
        speech_segments=[], speech_sec=0.0,
    )
    if dur < 1.0:
        return empty

    model = get_vad_model()
    try:
        segments = get_speech_segments(y, sr, model)
    except Exception:
        return empty

    speech_sec = sum(max(0.0, s["end"] - s["start"]) for s in segments)
    speech_ratio = speech_sec / dur if dur else 0.0
    gaps = []
    cursor = 0.0
    for s in segments:
        if s["start"] > cursor:
            gaps.append(s["start"] - cursor)
        cursor = max(cursor, s["end"])
    if cursor < dur:
        gaps.append(dur - cursor)
    gaps = [g for g in gaps if g >= 0.3]
    pause_count = len(gaps)
    mean_pause = float(np.mean(gaps)) if gaps else 0.0
    longest_pause = float(np.max(gaps)) if gaps else 0.0
    pause_ratio = max(0.0, 1.0 - speech_ratio)

    powers, hop = _frame_powers(y, sr)
    snr = noise_floor = speech_level = None
    if powers.size:
        mask = np.zeros(powers.size, dtype=bool)
        for s in segments:
            a = int(s["start"] / hop)
            b = int(math.ceil(s["end"] / hop))
            mask[a:b] = True
        speech_p = powers[mask]
        noise_p = powers[~mask]
        if speech_p.size and noise_p.size >= max(5, int(0.02 * powers.size)):
            snr = round(10.0 * math.log10(max(float(speech_p.mean()), 1e-18) /
                                          max(float(noise_p.mean()), 1e-18)), 2)
        all_db = 10.0 * np.log10(powers + 1e-18)
        noise_floor = round(float(np.percentile(all_db, 10)), 2)
        speech_level = round(float(np.percentile(all_db, 90)), 2)

    onset_rate = None
    if speech_sec >= 5.0:
        try:
            onsets = librosa.onset.onset_detect(
                y=y.astype(np.float32), sr=sr, hop_length=160, backtrack=False
            )
            times = librosa.frames_to_time(onsets, sr=sr, hop_length=160)
            inside = 0
            for t in times:
                if any(s["start"] <= t <= s["end"] for s in segments):
                    inside += 1
            onset_rate = round(inside / speech_sec, 2)
        except Exception:
            onset_rate = None

    return dict(
        speech_ratio_vad=round(speech_ratio, 4),
        pause_count=pause_count,
        mean_pause_s=round(mean_pause, 2),
        longest_pause_s=round(longest_pause, 2),
        pause_ratio=round(pause_ratio, 4),
        snr_db=snr,
        noise_floor_dbfs=noise_floor,
        speech_level_dbfs=speech_level,
        speech_segments=segments,
        speech_sec=speech_sec,
        onset_rate_hz=onset_rate,
    )


def get_speech_segments(y: np.ndarray, sr: int, model=None) -> list[dict]:
    """Segmen bicara (detik) dari Silero VAD."""
    import torch
    from silero_vad import get_speech_timestamps

    if model is None:
        from .quality_models import get_vad_model

        model = get_vad_model()
    if sr != SAMPLE_RATE:
        y = _resample(y, sr)
        sr = SAMPLE_RATE
    if len(y) < 512:
        return []
    tensor = torch.from_numpy(np.ascontiguousarray(y, dtype=np.float32))
    raw = get_speech_timestamps(
        tensor,
        model,
        sampling_rate=sr,
        threshold=0.5,
        min_speech_duration_ms=250,
        min_silence_duration_ms=300,
        speech_pad_ms=100,
        return_seconds=True,
    )
    return [{"start": float(s["start"]), "end": float(s["end"])} for s in raw]


# --------------------------------------------------------------------------- #
# spektrum & gema
# --------------------------------------------------------------------------- #
def spectral_stats(y: np.ndarray, sr: int) -> dict:
    empty = dict(rolloff95_hz=None, bandwidth_hz=None, telephone_band=None)
    if len(y) < 2048:
        return empty
    try:
        n_fft, hop = 1024, 256
        spec = np.abs(librosa.stft(y.astype(np.float32), n_fft=n_fft, hop_length=hop))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
        power = spec ** 2
        mean_power = power.mean(axis=1)
        total = float(mean_power.sum())
        if total <= 0:
            return empty
        cum = np.cumsum(mean_power) / total
        idx = int(np.searchsorted(cum, 0.995))
        idx = min(idx, len(freqs) - 1)
        bandwidth = float(freqs[idx])
        rolloff = librosa.feature.spectral_rolloff(
            y=y.astype(np.float32), sr=sr, n_fft=n_fft, hop_length=hop, roll_percent=0.95
        )
        rolloff95 = float(np.median(rolloff))
        return dict(
            rolloff95_hz=round(rolloff95, 1),
            bandwidth_hz=round(bandwidth, 1),
            telephone_band=int(bandwidth < 4200.0),
        )
    except Exception:
        return empty


def reverb_proxy(y: np.ndarray, sr: int, segments: list[dict]) -> dict:
    """Indikator gema dari peluruhan energi setelah offset bicara.

    Nilainya BUKAN RT60 dalam arti ISO 3382. Estimator buta dari sinyal ucapan
    tidak bisa memisahkan peluruhan suku kata dari gema ruangan. Gunakan hanya
    untuk membandingkan file dalam dataset yang sama; CLI menambahkan flag
    `gema_relatif` berdasarkan persentil 90.
    """
    empty = dict(rt60_proxy_s=None, decay_db_per_s=None)
    if not segments or len(y) < sr:
        return empty
    powers, hop = _frame_powers(y, sr, frame_sec=0.01)
    if powers.size < 10:
        return empty
    db = 10.0 * np.log10(powers + 1e-18)
    span = int(0.30 / hop)
    estimates, slopes = [], []
    for seg in segments[:200]:
        end = int(seg["end"] / hop)
        if end + span >= db.size:
            continue
        tail = db[end:end + span]
        if tail.size < 10:
            continue
        ref = float(np.max(tail[:3]))
        rel = tail - ref
        mask = (rel <= -5.0) & (rel >= -25.0)
        if mask.sum() < 8:
            continue
        t = np.arange(tail.size) * hop
        try:
            slope = float(np.polyfit(t[mask], rel[mask], 1)[0])
        except Exception:
            continue
        if slope >= -0.5:
            continue
        rt60 = 60.0 / abs(slope)
        if 0.05 <= rt60 <= 3.0:
            estimates.append(rt60)
            slopes.append(slope)
    if len(estimates) < 3:
        return empty
    return dict(
        rt60_proxy_s=round(float(np.median(estimates)), 3),
        decay_db_per_s=round(float(np.median(slopes)), 2),
    )


# --------------------------------------------------------------------------- #
# flags & skor
# --------------------------------------------------------------------------- #
def hard_flags(row: dict, th: dict | None = None) -> list[str]:
    """Kegagalan wajib: file tidak layak dipakai, apa pun skornya."""
    th = th or THRESHOLDS
    h = th["hard"]
    f: list[str] = []

    if _num(row.get("readable")) == 0:
        f.append("tidak_terbaca")
    if _num(row.get("zero_energy")) == 1:
        f.append("energi_nol")
    dur = _num(row.get("duration_s"))
    if dur is not None and dur < h["duration_min_s"]:
        f.append("terlalu_pendek")
    sil = _num(row.get("silence_ratio"))
    if sil is not None and sil >= h["silence_ratio_max"]:
        f.append("hampir_hening")
    sp = _num(row.get("speech_ratio_vad"))
    if sp is not None and sp < h["speech_ratio_min"]:
        f.append("tanpa_bicara")
    clip = _num(row.get("clip_ratio"))
    if clip is not None and clip > h["clip_ratio_max"]:
        f.append("clipping_berat")
    dc = _num(row.get("dc_offset"))
    if dc is not None and abs(dc) > h["dc_offset_max"]:
        f.append("dc_offset_berat")
    return f


def warn_flags(row: dict, th: dict | None = None) -> list[str]:
    """Pemicu review: meragukan dan tidak bisa diperbaiki otomatis."""
    th = th or THRESHOLDS
    w = th["warn"]
    f: list[str] = []

    sr = _num(row.get("sr"))
    if w.get("sr_expected") and sr is not None and sr != w["sr_expected"]:
        f.append("sr_bukan_16k")
    ch = _num(row.get("channels"))
    if w.get("channels_expected") and ch is not None and ch != w["channels_expected"]:
        f.append("bukan_mono")
    snr = _num(row.get("snr_db"))
    if snr is not None and snr < w["snr_min_db"]:
        f.append("snr_rendah")
    sp = _num(row.get("speech_ratio_vad"))
    if sp is not None and th["hard"]["speech_ratio_min"] <= sp < w["speech_ratio_warn"]:
        f.append("bicara_sedikit")
    ovrl = _num(row.get("dnsmos_ovrl"))
    if ovrl is not None and ovrl < w["dnsmos_ovrl_min"]:
        f.append("dnsmos_rendah")
    sig = _num(row.get("dnsmos_sig"))
    if sig is not None and sig < w["dnsmos_sig_min"]:
        f.append("dnsmos_signal_rendah")
    bak = _num(row.get("dnsmos_bak"))
    if bak is not None and bak < w["dnsmos_bak_min"]:
        f.append("dnsmos_background_rendah")
    bw = _num(row.get("bandwidth_hz"))
    if bw is not None and bw < w["bandwidth_min_hz"]:
        f.append("bandwidth_sempit")
    music = _num(row.get("music_ratio"))
    if music is not None and music > w["music_ratio_max"]:
        f.append("banyak_musik")
    appl = _num(row.get("applause_ratio"))
    if appl is not None and appl > w["applause_ratio_max"]:
        f.append("tepuk_tangan")
    overlap = _num(row.get("overlap_ratio"))
    if overlap is not None and overlap > w["overlap_max"]:
        f.append("overlap_bicara")
    return f


def info_flags(row: dict, th: dict | None = None) -> list[str]:
    """Catatan saja: tidak memicu review, tapi berguna untuk laporan."""
    th = th or THRESHOLDS
    w = th["warn"]
    f: list[str] = []

    lufs = _num(row.get("lufs"))
    if lufs is not None:
        if lufs < w["lufs_min"]:
            f.append("terlalu_pelan")
        elif lufs > w["lufs_max"]:
            f.append("terlalu_keras")
    clip = _num(row.get("clip_ratio"))
    if clip is not None and clip > w["clip_ratio_warn"]:
        f.append("clipping")
    longest = _num(row.get("clip_longest"))
    if longest is not None and longest > w["clip_longest_warn"]:
        f.append("clipping_panjang")
    dc = _num(row.get("dc_offset"))
    if dc is not None and abs(dc) > w["dc_offset_warn"]:
        f.append("dc_offset")
    imb = _num(row.get("channel_imbalance_db"))
    if imb is not None and imb > w["channel_imbalance_max_db"]:
        f.append("kanal_timpang")
    cancel = _num(row.get("mono_cancel_db"))
    if cancel is not None and cancel < -20.0:
        f.append("mono_cancel")
    pause = _num(row.get("longest_pause_s"))
    if pause is not None and pause > w["longest_pause_max_s"]:
        f.append("jeda_panjang")
    pr = _num(row.get("pause_ratio"))
    if pr is not None and pr > w["pause_ratio_max"]:
        f.append("banyak_hening")
    onset = _num(row.get("onset_rate_hz"))
    if onset is not None and not (w["onset_rate_min"] <= onset <= w["onset_rate_max"]):
        f.append("kecepatan_bicara")
    return f


def compute_flags(row: dict, th: dict | None = None) -> list[str]:
    """Semua catatan: gagal wajib + pemicu review + informasi."""
    return hard_flags(row, th) + warn_flags(row, th) + info_flags(row, th)


def quality_score(row: dict, th: dict | None = None) -> int | None:
    """Skor 0-100: rata-rata berbobot enam komponen yang tersedia.

    Komponen tanpa data dilewati dan bobotnya dinormalisasi ulang, jadi file
    yang belum melewati tahap DNSMOS/AST tidak dihukum. Kegagalan wajib -> 0.
    """
    th = th or THRESHOLDS
    if _num(row.get("readable")) == 0:
        return 0
    if hard_flags(row, th):
        return 0

    cfg = th["score"]
    weights = cfg["weights"]
    parts: list[tuple[float, float]] = []

    def pack(name: str, value: float) -> None:
        parts.append((weights[name], _clamp01(value)))

    sp = _num(row.get("speech_ratio_vad"))
    if sp is not None:
        pack("speech", (sp - cfg["speech_floor"]) / cfg["speech_span"])
    snr = _num(row.get("snr_db"))
    if snr is not None:
        pack("noise", (snr - cfg["snr_floor"]) / cfg["snr_span"])
    ovrl = _num(row.get("dnsmos_ovrl"))
    if ovrl is not None:
        pack("quality", (ovrl - cfg["dnsmos_floor"]) / cfg["dnsmos_span"])
    lufs = _num(row.get("lufs"))
    if lufs is not None:
        if lufs < cfg["lufs_lo"]:
            level = 1.0 - (cfg["lufs_lo"] - lufs) / cfg["lufs_quiet_span"]
        elif lufs > cfg["lufs_hi"]:
            level = 1.0 - (lufs - cfg["lufs_hi"]) / cfg["lufs_loud_span"]
        else:
            level = 1.0
        pack("level", level)
    bw = _num(row.get("bandwidth_hz"))
    if bw is not None:
        pack("clarity", (bw - cfg["bandwidth_floor"]) / cfg["bandwidth_span"])

    artifact = 0.0
    clip = _num(row.get("clip_ratio"))
    if clip is not None:
        artifact = max(artifact, (clip - cfg["clip_floor"]) / cfg["clip_span"])
    dc = _num(row.get("dc_offset"))
    if dc is not None:
        artifact = max(artifact, (abs(dc) - cfg["dc_floor"]) / cfg["dc_span"])
    sil = _num(row.get("silence_ratio"))
    if sil is not None:
        artifact = max(artifact, (sil - cfg["silence_floor"]) / cfg["silence_span"])
    parts.append((weights["artifacts"], 1.0 - _clamp01(artifact)))

    total_w = sum(w for w, _ in parts)
    if total_w <= 0:
        return None
    return int(round(100.0 * sum(w * v for w, v in parts) / total_w))


def evaluate(row: dict, th: dict | None = None) -> dict:
    """Hitung semua derivatif sekaligus: flags, skor, gate, dan alasannya."""
    th = th or THRESHOLDS
    hard = hard_flags(row, th)
    warn = warn_flags(row, th)
    info = info_flags(row, th)
    score = quality_score(row, th)

    if hard:
        gate, reasons = "reject", list(hard)
    else:
        below = score is not None and score < th["gate"]["review_below_score"]
        reasons = (["skor_rendah"] if below else []) + list(warn)
        if score is None:
            reasons.append("tanpa_metrik")
        gate = "review" if reasons else "accept"

    return {
        "flags": ";".join(hard + warn + info),
        "quality_score": score,
        "gate": gate,
        "gate_reason": ";".join(reasons),
    }


def gate_decision(row: dict, th: dict | None = None) -> tuple[str, str]:
    """Keputusan tiga pintu: accept / review / reject beserta alasannya."""
    result = evaluate(row, th)
    return result["gate"], result["gate_reason"]


def merge_thresholds(base: dict | None = None, override: dict | None = None) -> dict:
    """Gabungkan override JSON dengan ambang default.

    Mendukung format baru (``{"warn": {"snr_min_db": 6}}``) dan format lama
    datar (``{"snr_min_db": 6}``) yang otomatis masuk ke grup yang tepat.
    """
    source = base or THRESHOLDS
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in source.items()}
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key].update(value)
        elif key in out["hard"]:
            out["hard"][key] = value
        elif key in out["warn"]:
            out["warn"][key] = value
        elif key in out["score"]:
            out["score"][key] = value
        else:
            out["warn"][key] = value
    return out


# --------------------------------------------------------------------------- #
# analisis dasar per file (dipakai worker paralel)
# --------------------------------------------------------------------------- #
def analyze_basic(path: str | Path, sample_sec: float = 120.0) -> dict:
    """Hitung metrik teknis + sinyal + VAD untuk satu file."""
    path = str(path)
    row = {k: None for k in AUDIO_FIELDS}
    row["filepath"] = path
    try:
        info = sf.info(path)
    except Exception as e:  # noqa: BLE001
        row.update(
            readable=0,
            error=f"{type(e).__name__}: {e}"[:200],
            flags="tidak_terbaca",
            gate="reject",
            gate_reason="tidak_terbaca",
            quality_score=0,
        )
        return row

    row.update(
        duration_s=round(info.frames / info.samplerate, 2) if info.samplerate else 0.0,
        sr=int(info.samplerate),
        channels=int(info.channels),
        subtype=info.subtype,
        readable=1,
        error="",
    )
    try:
        dur = info.frames / info.samplerate if info.samplerate else 0.0
        sample, mode = extract_sample(path, info, sample_sec=sample_sec)
        row["sample_mode"] = mode
        row["analyzed_sec"] = round(len(sample) / SAMPLE_RATE, 2) if len(sample) else 0.0

        if dur <= 900.0:
            full, sr = read_audio(path)
            row.update(_stats_from_float(full, sr))
            full16 = _resample(full, sr)
            lufs = loudness(full16, SAMPLE_RATE)
            row["lufs"] = lufs
            row["lufs_estimated"] = 0
        else:
            row.update(_stats_streaming(path, info))
            lufs = loudness(sample, SAMPLE_RATE) if len(sample) else None
            row["lufs"] = lufs
            row["lufs_estimated"] = 1

        imb, cancel = _channel_stats(path, info)
        row["channel_imbalance_db"] = imb
        row["mono_cancel_db"] = cancel

        if len(sample) >= 1600:
            vad = vad_stats(sample, SAMPLE_RATE)
            segments = vad.pop("speech_segments", [])
            vad.pop("speech_sec", None)
            row.update(vad)
            row.update(spectral_stats(sample, SAMPLE_RATE))
            row.update(reverb_proxy(sample, SAMPLE_RATE, segments))
    except Exception as e:  # noqa: BLE001
        row["error"] = f"{type(e).__name__}: {e}"[:200]

    row.update(evaluate(row))
    return row


def _init_worker():
    """Initializer ProcessPool: batasi thread & siapkan VAD per worker."""
    try:
        import torch

        torch.set_num_threads(1)
    except Exception:
        pass
    try:
        from .quality_models import get_vad_model

        get_vad_model()
    except Exception:
        pass
