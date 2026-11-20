"""Watchdog pencarian: query yang menggantung harus dibatalkan, bukan membekukan sesi."""

import time

import pytest

from audio_pipeline import search


class _SlowYDL:
    def __init__(self, opts=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        time.sleep(1.0)
        return {"entries": []}


class _FastYDL(_SlowYDL):
    def extract_info(self, url, download=False):
        return {"entries": []}


def test_query_menggantung_dibatalkan(monkeypatch):
    monkeypatch.setattr(search, "YoutubeDL", _SlowYDL)
    with pytest.raises(search.ExtractTimeout):
        search._extract_info("ytsearch1:fisika", None, timeout=0.15)


def test_query_cepat_normal(monkeypatch):
    monkeypatch.setattr(search, "YoutubeDL", _FastYDL)
    info = search._extract_info("ytsearch1:fisika", None, timeout=1.0)
    assert info == {"entries": []}
