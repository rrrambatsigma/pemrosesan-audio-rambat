from audio_pipeline.youtube import pick_windows

N = 4
CLIP = 60


def test_deterministic():
    a = pick_windows(1800, N, CLIP, seed=42)
    b = pick_windows(1800, N, CLIP, seed=42)
    assert a == b


def test_differs_by_seed():
    assert pick_windows(1800, N, CLIP, seed=1) != pick_windows(1800, N, CLIP, seed=2)


def test_non_overlap_and_bounds():
    for dur in (900, 1200, 1800, 2700, 3600):
        w = pick_windows(dur, N, CLIP, seed=7)
        assert w, dur
        assert len(w) <= N
        assert w == sorted(w)
        for s in w:
            assert 0 <= s <= dur - CLIP
        for prev, nxt in zip(w, w[1:]):
            assert nxt - prev >= CLIP


def test_short_video_reduces_windows():
    w = pick_windows(300, 4, CLIP, seed=1)
    assert 1 <= len(w) <= 3


def test_too_short_returns_empty():
    assert pick_windows(40, 4, CLIP, seed=1) == []


def test_unknown_duration():
    assert pick_windows(None, 4, CLIP) == []
    assert pick_windows(0, 4, CLIP) == []


def test_skips_intro():
    w = pick_windows(1800, 2, CLIP, seed=3)
    assert w[0] >= 90
