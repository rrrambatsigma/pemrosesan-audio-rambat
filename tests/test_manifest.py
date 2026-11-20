import csv

from audio_pipeline import manifest
from audio_pipeline.config import FIELDS


def _row(cid="fisika-k10-k13-0001", **kw):
    r = {k: "" for k in FIELDS}
    r.update(clip_id=cid, label_no="9", label="fisika", kelas="10",
             kurikulum="k13", video_id="abcdefghijk", clip_index="0",
             clip_start_sec="120", clip_sec="60", status="ok",
             channel_id="UC123", filepath=f"data/raw/fisika/10_k13/{cid}.wav",
             bytes="32000", sha256="a" * 64)
    r.update(kw)
    return r


def test_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "manifest.csv"
    monkeypatch.setattr(manifest, "MANIFEST", p)
    rows = {"fisika-k10-k13-0001": _row()}
    manifest.save(rows)
    assert manifest.load()["fisika-k10-k13-0001"]["label"] == "fisika"
    # urutan kolom sama dengan FIELDS
    with open(p, newline="", encoding="utf-8-sig") as f:
        assert next(csv.reader(f)) == FIELDS


def test_save_is_byte_identical(tmp_path, monkeypatch):
    p = tmp_path / "manifest.csv"
    monkeypatch.setattr(manifest, "MANIFEST", p)
    rows = {"fisika-k10-k13-0001": _row()}
    manifest.save(rows)
    first = p.read_bytes()
    manifest.save(manifest.load())
    assert p.read_bytes() == first


def test_upsert_idempotent():
    rows = {}
    r = _row()
    assert manifest.upsert(rows, [r]) == 1
    assert manifest.upsert(rows, [dict(r)]) == 0
    assert len(rows) == 1


def test_validate_duplicate_filepath():
    rows = {"fisika-k10-k13-0001": _row()}
    rows["fisika-k10-k13-0002"] = _row(
        "fisika-k10-k13-0002",
        filepath=rows["fisika-k10-k13-0001"]["filepath"],
    )
    problems = manifest.validate(rows)
    assert any("filepath sama" in p for p in problems)


def test_next_seq():
    rows = {"fisika-k10-k13-0007": _row("fisika-k10-k13-0007")}
    assert manifest.next_seq(rows, "fisika") == 8
    assert manifest.next_seq(rows, "kimia") == 1


def test_clip_id_format():
    assert manifest.make_clip_id("fisika", 10, "k13", 7) == "fisika-k10-k13-0007"
    assert manifest.CLIP_RE.match("fisika-k10-k13-0007")


def test_done_videos(tmp_path, monkeypatch):
    # path di tmp (bukan path dataset asli) supaya uji tidak bergantung isi repo
    rows = {"fisika-k10-k13-0001": _row(filepath=str(tmp_path / "belum-ada.wav"))}
    assert manifest.done_videos(rows) == set()  # file tidak ada
    p = tmp_path / "x.wav"
    p.write_bytes(b"0" * 10)
    rows["fisika-k10-k13-0001"]["filepath"] = str(p)
    assert manifest.done_videos(rows) == {"abcdefghijk"}


def test_done_videos_per_label_menghitung_video_unik(tmp_path):
    p1 = tmp_path / "a.wav"
    p2 = tmp_path / "b.wav"
    p1.write_bytes(b"0")
    p2.write_bytes(b"0")
    rows = {
        # dua klip dari video yang sama -> tetap dihitung satu
        "fisika-k10-k13-0001": _row(filepath=str(p1)),
        "fisika-k10-k13-0002": _row("fisika-k10-k13-0002", filepath=str(p2)),
        "kimia-k10-k13-0001": _row("kimia-k10-k13-0001", label="kimia",
                                   video_id="zzzzzzzzzzz", filepath=str(p1)),
    }
    assert manifest.done_videos_per_label(rows) == {"fisika": 1, "kimia": 1}
