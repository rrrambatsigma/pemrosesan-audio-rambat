"""Pemuat model untuk pemeriksaan kualitas audio.

Model dimuat *lazy* dan di-cache per proses supaya stage yang tidak dipakai
tidak menambah waktu/RAM.

- Silero VAD  : deteksi bicara (ONNX, sudah bundled di paket silero_vad)
- DNSMOS P.835: skor kualitas 1-5 (paket speechmos, ONNX)
- AST AudioSet: klasifikasi musik/tepuk tangan/tawa (transformers, GPU)
- pyannote    : diarization untuk overlap bicara (opsional, butuh HF token)
"""

from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

SAMPLE_RATE = 16000
AST_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"

GROUP_KEYWORDS = {
    "speech": (
        "speech", "conversation", "narration", "monologue", "talk",
        "male voice", "female voice", "child", "whisper", "babble",
    ),
    "music": (
        "music", "musical", "singing", "song", "melody", "instrument",
        "guitar", "piano", "drum", "orchestra", "choir", "violin", "bass",
        "synthesizer", "hip hop", "rock", "pop", "jazz", "classical",
    ),
    "applause": ("applause", "clapping", "clap"),
    "laughter": ("laughter", "laugh", "giggle", "chuckle"),
    "noise": (
        "noise", "hum", "buzz", "static", "hiss", "wind", "traffic",
        "air conditioning", "crowd", "rustling", "tap", "click",
    ),
    "silence": ("silence",),
}


# --------------------------------------------------------------------------- #
# Silero VAD
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def get_vad_model():
    from silero_vad import load_silero_vad

    return load_silero_vad(onnx=True)


# --------------------------------------------------------------------------- #
# DNSMOS
# --------------------------------------------------------------------------- #
def dnsmos_scores(y: np.ndarray, sr: int = SAMPLE_RATE, max_chunks: int = 3) -> dict:
    """Skor DNSMOS P.835: signal, background, overall (skala 1-5).

    ``speechmos`` memakai hop 1 detik, sehingga 120 detik audio = 112 inferensi.
    Di sini audio dipotong menjadi maksimal ``max_chunks`` segmen 9 detik
    (awal/tengah/akhir), lalu hasilnya dirata-rata.
    """
    from speechmos import dnsmos

    if sr != SAMPLE_RATE:
        raise ValueError("DNSMOS membutuhkan audio 16 kHz")
    x = np.asarray(y, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.size == 0:
        return {}

    need = int(9.01 * SAMPLE_RATE) + 160
    if x.size <= need:
        chunks = [np.pad(x, (0, need - x.size))]
    else:
        n = max(1, int(max_chunks))
        starts = np.linspace(0, x.size - need, n).astype(int)
        chunks = [x[s:s + need] for s in starts]

    results = []
    for chunk in chunks:
        chunk = np.clip(chunk.astype(np.float32), -1.0, 1.0)
        out = dnsmos.run(chunk, sr=SAMPLE_RATE, return_df=False)
        if isinstance(out, list):
            out = out[0]
        if out:
            results.append(out)
    if not results:
        return {}

    keys = ("sig_mos", "bak_mos", "ovrl_mos", "p808_mos")
    mean = {k: float(np.mean([r.get(k, np.nan) for r in results])) for k in keys}
    return {
        "dnsmos_sig": round(mean["sig_mos"], 3),
        "dnsmos_bak": round(mean["bak_mos"], 3),
        "dnsmos_ovrl": round(mean["ovrl_mos"], 3),
        "dnsmos_p808": round(mean["p808_mos"], 3),
    }


# --------------------------------------------------------------------------- #
# AST AudioSet
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=2)
def _load_ast(device: str):
    import torch
    from transformers import ASTFeatureExtractor, ASTForAudioClassification

    extractor = ASTFeatureExtractor.from_pretrained(AST_MODEL)
    model = ASTForAudioClassification.from_pretrained(AST_MODEL)
    model.eval().to(device)
    labels = {int(k): str(v) for k, v in model.config.id2label.items()}
    return extractor, model, labels


def _group_of(label: str) -> str | None:
    low = label.lower()
    for group, words in GROUP_KEYWORDS.items():
        if any(w in low for w in words):
            return group
    return None


def classify_events(
    y: np.ndarray,
    sr: int = SAMPLE_RATE,
    device: str = "cpu",
    window_sec: float = 10.0,
    max_windows: int = 4,
) -> dict:
    """Rasio musik/tepuk tangan/tawa dari AST pada beberapa window."""
    import torch

    if y.size == 0:
        return {}
    if sr != SAMPLE_RATE:
        from scipy.signal import resample_poly
        from math import gcd

        g = gcd(int(sr), SAMPLE_RATE)
        y = resample_poly(y, SAMPLE_RATE // g, sr // g).astype(np.float32)
    extractor, model, labels = _load_ast(device)

    win = int(window_sec * SAMPLE_RATE)
    if y.size <= win:
        windows = [np.pad(y, (0, max(0, win - y.size)))]
    else:
        n = min(max_windows, int(np.ceil(y.size / win)))
        step = max(win, (y.size - win) // max(1, n - 1)) if n > 1 else win
        windows = []
        for i in range(n):
            start = min(i * step, y.size - win)
            windows.append(y[start:start + win])
        if not windows:
            windows = [np.pad(y, (0, max(0, win - y.size)))]

    feats = extractor(
        [w.astype(np.float32) for w in windows],
        sampling_rate=SAMPLE_RATE,
        return_tensors="pt",
    )
    input_values = feats["input_values"].to(device)

    probs_sum = None
    use_amp = device.startswith("cuda")
    with torch.inference_mode():
        for start in range(0, input_values.size(0), 4):
            batch = input_values[start:start + 4]
            if use_amp:
                with torch.autocast("cuda", dtype=torch.float16):
                    logits = model(batch).logits.float()
            else:
                logits = model(batch).logits.float()
            probs = torch.softmax(logits, dim=-1).cpu().numpy()
            if probs_sum is None:
                probs_sum = probs.sum(axis=0)
            else:
                probs_sum += probs.sum(axis=0)

    n_windows = max(1, input_values.size(0))
    mean_probs = probs_sum / n_windows
    groups = {g: 0.0 for g in GROUP_KEYWORDS}
    for idx, prob in enumerate(mean_probs):
        group = _group_of(labels.get(idx, ""))
        if group:
            groups[group] += float(prob)

    top_idx = np.argsort(mean_probs)[-3:][::-1]
    top = "; ".join(
        f"{labels.get(int(i), i)[:40]} {mean_probs[int(i)]:.2f}" for i in top_idx
    )

    return {
        "music_ratio": round(min(1.0, groups["music"]), 4),
        "applause_ratio": round(min(1.0, groups["applause"]), 4),
        "laughter_ratio": round(min(1.0, groups["laughter"]), 4),
        "noise_ratio": round(min(1.0, groups["noise"]), 4),
        "top_events": top,
    }


# --------------------------------------------------------------------------- #
# pyannote diarization (opsional)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _load_diarization(device: str):
    from pyannote.audio import Pipeline

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", use_auth_token=token
    )
    import torch

    pipeline.to(torch.device(device))
    return pipeline


def diarization_stats(y: np.ndarray, sr: int = SAMPLE_RATE, device: str = "cpu") -> dict:
    """Jumlah pembicara dan rasio overlap bicara (butuh pyannote + HF token)."""
    import torch

    try:
        pipeline = _load_diarization(device)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "pyannote belum siap. Install `pyannote.audio`, login HF, dan terima "
            f"terms pyannote/speaker-diarization-3.1. Detail: {type(e).__name__}: {e}"
        ) from e

    import librosa

    if sr != SAMPLE_RATE:
        y = librosa.resample(y, orig_sr=sr, target_sr=SAMPLE_RATE)
        sr = SAMPLE_RATE
    waveform = torch.from_numpy(np.ascontiguousarray(y, dtype=np.float32)).unsqueeze(0)
    annotation = pipeline({"waveform": waveform, "sample_rate": sr})
    try:
        speakers = len(annotation.labels())
        support = annotation.get_timeline().support()
        speech_sec = float(support.duration())
        overlap = annotation.get_overlap()
        overlap_sec = float(overlap.duration())
        ratio = overlap_sec / speech_sec if speech_sec > 0 else 0.0
    except Exception:
        return {"diar_speakers": None, "overlap_ratio": None}
    return {
        "diar_speakers": speakers,
        "overlap_ratio": round(min(1.0, ratio), 4),
    }
