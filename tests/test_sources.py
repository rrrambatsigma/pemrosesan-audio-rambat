import pytest

from audio_pipeline.sources import SourceError, parse_file

VALID = """\
# ini komentar, bukan bab
## k13 | kelas 10 | Vektor
ytsearch12:fisika kelas 10 vektor
https://www.youtube.com/playlist?list=PLabc123

## kurikulum merdeka | kelas 11 | Fluida
ytsearch5:fisika kelas 11 fluida
"""


def test_parse_valid(tmp_path):
    p = tmp_path / "playlist_fisika.txt"
    p.write_text(VALID, encoding="utf-8")
    items = parse_file(p, "fisika")
    assert len(items) == 3
    assert [i.kurikulum for i in items] == ["k13", "k13", "kurmerdeka"]
    assert [i.kelas for i in items] == [10, 10, 11]
    assert items[0].kind == "search" and items[0].limit == 12
    assert items[1].kind == "url"
    assert all(i.bab for i in items)


def test_comment_is_not_topic(tmp_path):
    p = tmp_path / "playlist_fisika.txt"
    p.write_text("# catatan\n## k13 | kelas 10 | Optik\n# lagi\n"
                 "ytsearch3:fisika optik\n", encoding="utf-8")
    items = parse_file(p, "fisika")
    assert items[0].bab == "Optik"


def test_line_outside_section(tmp_path):
    p = tmp_path / "playlist_fisika.txt"
    p.write_text("ytsearch3:fisika\n", encoding="utf-8")
    with pytest.raises(SourceError, match="di luar seksi"):
        parse_file(p, "fisika")


def test_header_wrong(tmp_path):
    p = tmp_path / "playlist_fisika.txt"
    p.write_text("## Vektor\nytsearch3:fisika\n", encoding="utf-8")
    with pytest.raises(SourceError, match="header"):
        parse_file(p, "fisika")


def test_bad_kurikulum(tmp_path):
    p = tmp_path / "playlist_fisika.txt"
    p.write_text("## k99 | kelas 10 | X\nytsearch3:x\n", encoding="utf-8")
    with pytest.raises(SourceError, match="kurikulum"):
        parse_file(p, "fisika")


def test_bad_kelas(tmp_path):
    p = tmp_path / "playlist_fisika.txt"
    p.write_text("## k13 | kelas 13 | X\nytsearch3:x\n", encoding="utf-8")
    with pytest.raises(SourceError, match="header"):
        parse_file(p, "fisika")
