"""Tes sink Hugging Face: commit batch, idempoten, dan kegagalan."""

from types import SimpleNamespace

import pytest

from audio_pipeline import hub, manifest, scrape
from audio_pipeline.hub import HubError, HubSink


class FakeApi:
    def __init__(self, fail_times: int = 0):
        self.commits: list[list[str]] = []
        self.repo_kw: dict | None = None
        self.fail_times = fail_times

    def whoami(self):
        return {"name": "tester"}

    def create_repo(self, **kw):
        self.repo_kw = kw
        return SimpleNamespace(repo_id=kw["repo_id"])

    def create_commit(self, *, repo_id, repo_type, operations, commit_message):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("403 Forbidden")
        self.commits.append([op.path_in_repo for op in operations])
        return SimpleNamespace(oid="c" * 40)


def _row(cid: str = "fisika-k10-k13-0001", video_id: str = "abcdefghijk") -> dict:
    return {
        "clip_id": cid, "label": "fisika", "kelas": "10", "kurikulum": "k13",
        "video_id": video_id, "status": "ok", "filepath": "",
        "bytes": "", "sha256": "",
    }


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "TMP_DIR", tmp_path)
    monkeypatch.setattr(manifest, "MANIFEST", tmp_path / "manifest.csv")
    monkeypatch.setattr(manifest, "FAILURES", tmp_path / "failures.csv")


def _wav(tmp_path, name: str = "a.wav") -> "object":
    p = tmp_path / name
    p.write_bytes(b"RIFF" + b"x" * 4096)
    return p


def test_commit_satu_video_lalu_hapus_lokal(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    api = FakeApi()
    sink = HubSink("tester/dataset", api=api, keep_local=False, batch=1)
    row = _row()
    rows = {row["clip_id"]: row}
    src = _wav(tmp_path)

    sink.enqueue(row, src)
    assert row["hub_status"] == "pending"
    sink.flush(rows, force=True)

    assert row["hub_status"] == "uploaded"
    assert row["filepath"].startswith("hf://datasets/tester/dataset/raw/fisika/10_k13/")
    assert row["hub_commit"] == "c" * 40
    assert not src.exists()               # lokal dibebaskan setelah commit
    assert api.commits == [[
        "raw/fisika/10_k13/fisika-k10-k13-0001.wav", "manifest.csv",
    ]]
    assert sink.uploaded == 1


def test_batch_menunggu_sampai_n_berkas(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    api = FakeApi()
    sink = HubSink("tester/dataset", api=api, batch=2)
    rows = {}
    row = _row("fisika-k10-k13-0001")
    rows[row["clip_id"]] = row
    sink.enqueue(row, _wav(tmp_path, "1.wav"))
    sink.flush(rows)
    assert api.commits == []              # baru 1 berkas, belum mencapai batch

    row2 = _row("fisika-k10-k13-0002")
    rows[row2["clip_id"]] = row2
    sink.enqueue(row2, _wav(tmp_path, "2.wav"))
    sink.flush(rows)
    assert len(api.commits) == 1
    assert len(api.commits[0]) == 3       # 2 audio + manifest


def test_gagal_commit_menandai_failed_dan_menyimpan_berkas(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    api = FakeApi(fail_times=1)
    sink = HubSink("tester/dataset", api=api, batch=1, retries=1)
    row = _row()
    rows = {row["clip_id"]: row}
    src = _wav(tmp_path)

    sink.enqueue(row, src)
    with pytest.raises(HubError):
        sink.flush(rows, force=True)

    assert row["hub_status"] == "failed"
    assert src.exists()                   # bisa diulang tanpa unduh ulang
    assert sink.failed == 1


def test_baris_uploaded_dihitung_selesai_tanpa_file_lokal():
    row = _row()
    row.update(hub_status="uploaded",
               filepath="hf://datasets/tester/dataset/raw/fisika/10_k13/x.wav")
    rows = {row["clip_id"]: row}

    assert scrape._artifact_ok(row, hub_enabled=True)
    assert scrape._video_complete(rows, row["video_id"], 1, hub_enabled=True)
    assert [r["clip_id"] for r in manifest.ok_rows(rows)] == [row["clip_id"]]


def test_token_wajib_untuk_commit(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(hub, "resolve_token", lambda: None)
    with pytest.raises(HubError, match="token HF"):
        HubSink("tester/dataset")._client()


def test_scrape_raw_langsung_ke_hub_tanpa_file_lokal(tmp_path, monkeypatch):
    """Alur penuh: unduh WAV -> commit -> hapus transit -> run kedua skip."""
    from pathlib import Path

    from audio_pipeline import youtube
    from audio_pipeline.registry import Label
    from audio_pipeline.search import Candidate

    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(scrape, "HUB_STAGING", tmp_path / "hub")
    monkeypatch.setattr(scrape, "TMP_DIR", tmp_path)

    label = Label(9, "fisika", "eksakta_teknologi", "IPA", "id", ("fisika",))
    labels = {"fisika": label}
    cand = Candidate(
        video_id="abcdefghijk", title="Fisika", duration=1200, channel="C",
        channel_id="UC1", channel_url="", url="", label="fisika", kelas=10,
        kurikulum="k13", bab="Vektor", source_url="ytsearch:x",
        source_kind="search", source_query="x", rank=1,
    )
    calls = {"download": 0}

    def fake_download(vid, browser=None, quality="48", raw=False, to_dir=None,
                      wav_sr=None, wav_mono=True):
        calls["download"] += 1
        base = Path(to_dir) if to_dir else tmp_path / "src"
        base.mkdir(parents=True, exist_ok=True)
        p = base / f"{vid}.wav"
        p.write_bytes(b"RIFF" + b"x" * 8192)
        return p

    monkeypatch.setattr(youtube, "probe", lambda vid, refresh=False, browser=None: {
        "video_id": vid, "title": "T", "channel": "C", "channel_id": "UC1",
        "channel_url": "", "license": "", "duration": 1200,
        "yt_dlp_version": "test"})
    monkeypatch.setattr(youtube, "download_source", fake_download)
    monkeypatch.setattr(youtube, "sha256_file", lambda p: "e" * 64)
    monkeypatch.setattr(youtube, "audio_info", lambda p: (16000, 1))

    api = FakeApi()
    sink = HubSink("tester/dataset", api=api, keep_local=False, batch=1)
    kwargs = dict(raw_only=True, hub=sink, sleep=(0, 0), batch_size=0,
                  progress=lambda *a, **k: None)

    stats = scrape.run([cand], labels, **kwargs)
    assert stats["videos_ok"] == 1
    assert stats["hub_uploaded"] == 1
    assert not any(p.is_file() for p in (tmp_path / "hub").rglob("*"))

    rows = manifest.load()
    row = next(iter(rows.values()))
    assert row["hub_status"] == "uploaded"
    assert row["filepath"].startswith("hf://datasets/tester/dataset/raw/")
    assert row["hub_commit"] == "c" * 40

    stats2 = scrape.run([cand], labels, **kwargs)
    assert stats2["videos_skipped"] == 1
    assert calls["download"] == 1          # tidak unduh ulang
