from audio_pipeline.planner import cells_for, kelas_from_title
from audio_pipeline.registry import Label
from audio_pipeline.search import Candidate
from audio_pipeline.planner import select_label


def test_kelas_from_title():
    assert kelas_from_title("Fisika Kelas X - Vektor") == 10
    assert kelas_from_title("FISIKA KELAS 11") == 11
    assert kelas_from_title("Latihan Soal Kelas XII") == 12
    assert kelas_from_title("Physics Grade 10 SMA") == 10
    assert kelas_from_title("Fisika SMA - Vektor") is None
    assert kelas_from_title("Kelas 13 tidak ada") is None


def _c(vid, kelas, title, dur=1200, ch="UC1"):
    return Candidate(video_id=vid, title=title, duration=dur, channel="ch",
                     channel_id=ch, channel_url="", url="", label="fisika",
                     kelas=kelas, kurikulum="k13", bab="x", source_url="",
                     source_kind="search", source_query="")


def test_select_label_drops_wrong_kelas():
    cands = [
        _c("aaaaaaaaaaa", 10, "Fisika Kelas 10 Vektor"),
        _c("bbbbbbbbbbb", 10, "Fisika Kelas 12 Vektor"),   # salah kelas
        _c("ccccccccccc", 10, "Fisika SMA Vektor"),        # tidak menyebut kelas
        _c("ddddddddddd", 10, "Fisika", dur=60),           # terlalu pendek
    ]
    plan = select_label(cands, target=10)
    ids = {c.video_id for c in plan}
    assert "aaaaaaaaaaa" in ids
    assert "bbbbbbbbbbb" not in ids
    assert "ccccccccccc" in ids
    assert "ddddddddddd" not in ids


def test_cells_sum_to_target():
    for target in (100, 143, 144, 210):
        cells = cells_for(target)
        assert sum(cells.values()) == target
        assert all(v >= 0 for v in cells.values())


def test_rollover_mengisi_target_saat_sel_kosong():
    # semua kandidat hanya di satu sel (kelas 10 / k13); sel lain kosong
    cands = [_c(f"vid{i:08d}", 10, "Fisika SMA", ch=f"UC{i % 15}")
             for i in range(120)]
    plan = select_label(cands, target=100)
    assert len(plan) == 100


def test_batas_channel_tetap_dihormati():
    import math
    cands = [_c(f"vid{i:08d}", 10, "Fisika SMA", ch="UC1") for i in range(60)]
    plan = select_label(cands, target=20, max_channel_frac=0.20)
    assert len(plan) <= max(3, math.ceil(20 * 0.20))
