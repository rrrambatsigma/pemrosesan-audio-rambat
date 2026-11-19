"""Uji sintetis untuk metrik audio_pipeline.quality.

Jalankan sebelum memproses dataset:
    python scripts/quality_selftest.py
    python scripts/quality_selftest.py --with-models   # ikut uji DNSMOS

Tidak menyentuh data/raw; semua file uji dibuat di folder sementara.
"""

from __future__ import annotations

import argparse
import math
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import soundfile as sf

from audio_pipeline.quality import (
    THRESHOLDS,
    analyze_basic,
    compute_flags,
    evaluate,
    merge_thresholds,
    quality_score,
)

SR = 16000


def tone(freq=440.0, sec=5.0, amp=0.1):
    t = np.arange(int(sec * SR)) / SR
    return (amp * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def write(path: Path, y: np.ndarray, channels: int = 1):
    if channels == 2 and y.ndim == 1:
        y = np.stack([y, np.zeros_like(y)], axis=1)
    sf.write(str(path), y, SR, subtype="PCM_16")
    return path


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(condition)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-models", action="store_true")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="quality_selftest_"))
    ok = True

    # 1. sine bersih
    p = write(tmp / "sine.wav", tone())
    row = analyze_basic(p)
    ok &= check("sine: terbaca", row["readable"] == 1, str(row.get("error")))
    ok &= check("sine: sr 16k mono", row["sr"] == 16000 and row["channels"] == 1)
    ok &= check("sine: durasi", abs((row["duration_s"] or 0) - 5.0) < 0.05,
                str(row["duration_s"]))
    ok &= check("sine: peak ~ -20 dBFS", -21 <= (row["peak_dbfs"] or -99) <= -19,
                str(row["peak_dbfs"]))
    ok &= check("sine: rms ~ -23 dBFS", -24 <= (row["rms_dbfs"] or -99) <= -22,
                str(row["rms_dbfs"]))
    ok &= check("sine: tanpa clipping", (row["clip_ratio"] or 0) == 0)
    ok &= check("sine: LUFS wajar", row["lufs"] is not None and -35 < row["lufs"] < -10,
                str(row["lufs"]))

    # 2. clipping
    p = write(tmp / "clip.wav", np.sign(tone(freq=200, sec=2.0, amp=1.0)))
    row = analyze_basic(p)
    ok &= check("clip: rasio tinggi", (row["clip_ratio"] or 0) > 0.9, str(row["clip_ratio"]))
    ok &= check("clip: run panjang", (row["clip_longest"] or 0) > 100, str(row["clip_longest"]))
    ok &= check("clip: flag muncul", "clipping" in (row["flags"] or ""), row["flags"])

    # 3. hening
    p = write(tmp / "silence.wav", np.zeros(SR * 3, dtype=np.float32))
    row = analyze_basic(p)
    ok &= check("silence: energi nol", row["zero_energy"] == 1)
    ok &= check("silence: flag energi_nol", "energi_nol" in (row["flags"] or ""), row["flags"])
    ok &= check("silence: LUFS None", row["lufs"] is None, str(row["lufs"]))

    # 4. lowpass 3.4 kHz (mirip telepon)
    rng = np.random.default_rng(0)
    noise = (0.1 * rng.standard_normal(SR * 5)).astype(np.float32)
    from scipy.signal import butter, sosfiltfilt

    sos = butter(8, 3400, btype="low", fs=SR, output="sos")
    p = write(tmp / "lowpass.wav", sosfiltfilt(sos, noise))
    row = analyze_basic(p)
    ok &= check("lowpass: bandwidth < 4.2 kHz", (row["bandwidth_hz"] or 9999) < 4200,
                str(row["bandwidth_hz"]))
    ok &= check("lowpass: flag pita telepon", row["telephone_band"] == 1)

    # 5. stereo kanan kosong (imbalance) vs stereo terbalik fasa (cancel)
    p = write(tmp / "stereo_silent.wav", tone(amp=0.5), channels=2)
    row = analyze_basic(p)
    ok &= check("stereo: imbalance > 20 dB", (row["channel_imbalance_db"] or 0) > 20,
                str(row["channel_imbalance_db"]))
    ok &= check("stereo: mono cancel ~0 dB", abs(row["mono_cancel_db"] or 0) < 1.0,
                str(row["mono_cancel_db"]))
    ok &= check("stereo: flag bukan_mono", "bukan_mono" in (row["flags"] or ""), row["flags"])
    ok &= check("stereo: flag kanal_timpang", "kanal_timpang" in (row["flags"] or ""),
                row["flags"])

    inv = tone(amp=0.5)
    p = write(tmp / "stereo_invert.wav", np.stack([inv, -inv], axis=1))
    row = analyze_basic(p)
    ok &= check("stereo terbalik: mono cancel < -40 dB",
                (row["mono_cancel_db"] or 0) < -40, str(row["mono_cancel_db"]))
    ok &= check("stereo terbalik: flag mono_cancel",
                "mono_cancel" in (row["flags"] or ""), row["flags"])

    # 6. skor & flag
    good = dict(readable=1, dnsmos_ovrl=4.5, snr_db=30, bandwidth_hz=7500,
                rt60_proxy_s=0.4, lufs=-20, speech_ratio_vad=0.8, clip_ratio=0.0)
    bad = dict(readable=1, dnsmos_ovrl=2.0, snr_db=8, bandwidth_hz=3500,
               rt60_proxy_s=1.2, lufs=-40, speech_ratio_vad=0.3, clip_ratio=0.002)
    sg, sb = quality_score(good), quality_score(bad)
    ok &= check("skor: bagus > buruk", sg > sb, f"{sg} > {sb}")
    ok &= check("skor: rentang 0-100", 0 <= sb <= 100 and 0 <= sg <= 100)
    flags = compute_flags(dict(readable=1, duration_s=600, snr_db=5))
    ok &= check("flag: snr_rendah", "snr_rendah" in flags, ";".join(flags))

    # 7. gate accept / review / reject
    clean = dict(readable=1, duration_s=600, speech_ratio_vad=0.85, snr_db=25,
                 dnsmos_ovrl=3.6, bandwidth_hz=6500, lufs=-21.0, clip_ratio=0.0,
                 dc_offset=0.0, silence_ratio=0.1)
    res = evaluate(clean)
    ok &= check("gate: bersih -> accept", res["gate"] == "accept",
                f'{res["gate"]} skor={res["quality_score"]}')
    res = evaluate({**clean, "snr_db": 5})
    ok &= check("gate: SNR 5 dB -> review", res["gate"] == "review",
                f'{res["gate"]} {res["gate_reason"]}')
    res = evaluate({**clean, "speech_ratio_vad": 0.1})
    ok &= check("gate: tanpa bicara -> reject", res["gate"] == "reject",
                f'{res["gate"]} {res["gate_reason"]}')
    res = evaluate({**clean, "zero_energy": 1, "silence_ratio": 1.0,
                    "lufs": None, "speech_ratio_vad": 0.0})
    ok &= check("gate: hening -> reject & skor 0",
                res["gate"] == "reject" and res["quality_score"] == 0,
                f'{res["gate"]} skor={res["quality_score"]}')
    res = evaluate({**clean, "clip_ratio": 0.01})
    ok &= check("gate: clipping berat -> reject", res["gate"] == "reject",
                res["gate_reason"])
    res = evaluate({**clean, "bandwidth_hz": 2200})
    ok &= check("gate: pita sempit -> review", res["gate"] == "review",
                res["gate_reason"])
    res = evaluate({**clean, "lufs": -45.0})
    ok &= check("gate: pelan tapi sehat -> accept", res["gate"] == "accept",
                f'skor={res["quality_score"]}')
    ok &= check("gate: skor turun saat SNR turun",
                quality_score(clean) > quality_score({**clean, "snr_db": 9}))

    # 8. override ambang
    th2 = merge_thresholds(THRESHOLDS, {"snr_min_db": 6})
    ok &= check("thresholds: override datar -> warn", th2["warn"]["snr_min_db"] == 6)
    th3 = merge_thresholds(THRESHOLDS, {"gate": {"review_below_score": 70}})
    ok &= check("thresholds: override bersarang",
                th3["gate"]["review_below_score"] == 70)
    ok &= check("thresholds: default tidak berubah",
                THRESHOLDS["warn"]["snr_min_db"] == 8.0)

    # 9. DNSMOS opsional
    if args.with_models:
        from audio_pipeline.quality_models import dnsmos_scores

        scores = dnsmos_scores(tone(freq=300, sec=10.0, amp=0.2))
        keys = {"dnsmos_sig", "dnsmos_bak", "dnsmos_ovrl", "dnsmos_p808"}
        ok &= check("dnsmos: kunci lengkap", keys <= set(scores), str(scores))
        ok &= check("dnsmos: rentang 1-5",
                    all(1.0 <= v <= 5.0 for v in scores.values()), str(scores))

    print("\nHASIL:", "SEMUA LULUS" if ok else "ADA YANG GAGAL")
    print(f"Folder uji: {tmp}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
