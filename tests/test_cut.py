"""Uji potong klip dengan ffmpeg sungguhan (bukan mock)."""

import shutil

import numpy as np
import pytest
import soundfile as sf

from audio_pipeline.youtube import cut

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"),
                                reason="butuh ffmpeg di PATH")


def _tone(tmp_path, seconds=3.0, sr=16000):
    t = np.arange(int(seconds * sr)) / sr
    y = (0.05 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    p = tmp_path / "src.wav"
    sf.write(p, y, sr, subtype="PCM_16")
    return p


def test_cut_menghasilkan_wav_valid(tmp_path):
    src = _tone(tmp_path)
    out = tmp_path / "out" / "fisika-k10-k13-0001.wav"
    size, dur = cut(src, out, 0.5, 1.0)
    assert out.exists()
    assert size > 1000
    info = sf.info(out)
    assert info.samplerate == 16000
    assert info.channels == 1
    assert abs(info.duration - 1.0) < 0.1
    assert abs(dur - info.duration) < 0.01


def test_tidak_meninggalkan_file_part(tmp_path):
    src = _tone(tmp_path)
    out = tmp_path / "out" / "klip.wav"
    cut(src, out, 0.0, 0.5)
    assert not list(out.parent.glob("*.part.wav"))
    assert not list(out.parent.glob("*.tmp"))


def test_start_melebihi_durasi_tidak_membuat_file_rusak(tmp_path):
    src = _tone(tmp_path, seconds=2.0)
    out = tmp_path / "out" / "kosong.wav"
    with pytest.raises(Exception):
        cut(src, out, 5.0, 1.0)
    assert not out.exists()
