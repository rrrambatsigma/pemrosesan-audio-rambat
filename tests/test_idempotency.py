"""Bukti idempotensi: run kedua tidak mengunduh / memotong / menulis apa pun."""

import soundfile as sf
from pathlib import Path

from audio_pipeline import manifest, scrape, youtube
from audio_pipeline.config import FIELDS
from audio_pipeline.registry import Label
from audio_pipeline.search import Candidate

LABELS = {"fisika": Label(9, "fisika", "eksakta_teknologi", "IPA", "id", ("fisika",))}
CAND = Candidate(
    video_id="abcdefghijk", title="Fisika Kelas X", duration=1800,
    channel="Ch", channel_id="UC123", channel_url="", url="", label="fisika",
    kelas=10, kurikulum="k13", bab="Vektor", source_url="ytsearch:fisika",
    source_kind="search", source_query="fisika kelas 10", rank=1,
)


def _setup(tmp_path, monkeypatch):
    conftest_manifest = tmp_path / "manifest.csv"
    raw = tmp_path / "raw"
    monkeypatch.setattr(manifest, "MANIFEST", conftest_manifest)
    monkeypatch.setattr(manifest, "FAILURES", tmp_path / "failures.csv")
    monkeypatch.setattr(scrape, "RAW_DIR", raw)
    return conftest_manifest, raw


def _fake_probe(vid, refresh=False, browser=None):
    return {"video_id": vid, "title": "Fisika Kelas X", "channel": "Ch",
            "channel_id": "UC123", "channel_url": "", "license": "CC-BY",
            "duration": 1800, "yt_dlp_version": "test"}


def _fake_download(vid, browser=None, quality="48"):
    return None


def _fake_windows(duration, n=4, clip_sec=60, intro=0, outro=0, min_gap=0,
                  seed=None):
    return [100, 400, 700, 1000][:n]


def _fake_cut(src, out, start, sec, sample_rate=16000):
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), [0.0] * int(sec * sample_rate), sample_rate,
             subtype="PCM_16")
    return out.stat().st_size, float(sec)


def _patch(monkeypatch):
    calls = {"probe": 0, "download": 0, "cut": 0}

    def probe(vid, refresh=False, browser=None):
        calls["probe"] += 1
        return _fake_probe(vid)

    def download(vid, browser=None, quality="48"):
        calls["download"] += 1
        return None

    def cut(*a, **kw):
        calls["cut"] += 1
        return _fake_cut(*a, **kw)

    monkeypatch.setattr(youtube, "probe", probe)
    monkeypatch.setattr(youtube, "download_source", download)
    monkeypatch.setattr(youtube, "pick_windows", _fake_windows)
    monkeypatch.setattr(youtube, "cut", cut)
    monkeypatch.setattr(youtube, "sha256_file", lambda p: "b" * 64)
    return calls


def test_second_run_does_nothing(tmp_path, monkeypatch):
    mpath, raw = _setup(tmp_path, monkeypatch)
    calls = _patch(monkeypatch)

    stats1 = scrape.run([CAND], LABELS, clip_sec=2, sleep=(0, 0), batch_size=0,
                        progress=lambda *a, **k: None)
    assert stats1["clips_new"] == 4
    assert stats1["videos_failed"] == 0
    assert calls["probe"] == 1 and calls["cut"] == 4

    rows = manifest.load()
    assert len(rows) == 4
    assert len({r["filepath"] for r in rows.values()}) == 4
    first = mpath.read_bytes()

    stats2 = scrape.run([CAND], LABELS, clip_sec=2, sleep=(0, 0), batch_size=0,
                        progress=lambda *a, **k: None)
    assert stats2["videos_skipped"] == 1
    assert stats2["clips_new"] == 0
    assert stats2["videos_failed"] == 0
    # tidak ada panggilan jaringan / potong ulang
    assert calls["probe"] == 1
    assert calls["download"] == 1
    assert calls["cut"] == 4
    assert mpath.read_bytes() == first
    assert len(manifest.load()) == 4


def test_partial_resume_keeps_clip_id(tmp_path, monkeypatch):
    mpath, raw = _setup(tmp_path, monkeypatch)
    _patch(monkeypatch)

    # run pertama dipaksa berhenti setelah 2 video? Simulasi: potong manual 2 klip
    scrape.run([CAND], LABELS, clip_sec=2, sleep=(0, 0), batch_size=0,
               progress=lambda *a, **k: None)
    rows = manifest.load()
    ids = sorted(rows)
    # hapus 2 klip terakhir -> seolah sesi sebelumnya terputus
    for cid in ids[2:]:
        Path(rows[cid]["filepath"]).unlink()
        del rows[cid]
    manifest.save(rows)

    stats = scrape.run([CAND], LABELS, clip_sec=2, sleep=(0, 0), batch_size=0,
                       progress=lambda *a, **k: None)
    assert stats["clips_new"] == 2
    rows2 = manifest.load()
    assert len(rows2) == 4
    assert sorted(rows2) == ids          # clip_id tidak berubah
    for cid in rows2:
        assert rows2[cid]["status"] == "ok"


def test_catatan_gagal_lama_dibersihkan(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch(monkeypatch)
    kwargs = dict(clip_sec=2, sleep=(0, 0), batch_size=0,
                  progress=lambda *a, **k: None)
    scrape.run([CAND], LABELS, **kwargs)

    manifest.save_failures({CAND.video_id: {
        "video_id": CAND.video_id, "label": "fisika", "stage": "process",
        "error": "percobaan lama", "attempts": "1", "last_at": "",
    }})
    assert manifest.load_failures()

    scrape.run([CAND], LABELS, **kwargs)
    assert manifest.load_failures() == {}
