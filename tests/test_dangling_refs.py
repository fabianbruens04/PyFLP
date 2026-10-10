from __future__ import annotations

import pathlib

import pyflp
from pyflp.arrangement import ChannelPLItem, PatternPLItem

ASSET = pathlib.Path(__file__).parent / "assets" / "FL 20.8.4.flp"


def test_tracks_keep_clips_whose_models_are_missing():
    arrangement = next(iter(pyflp.parse(ASSET).arrangements))
    before = [it for tr in arrangement.tracks for it in tr]
    assert any(isinstance(it, PatternPLItem) for it in before)
    arrangement._kw["patterns"] = []
    arrangement._kw["channels"] = []
    after = [it for tr in arrangement.tracks for it in tr]
    assert [(it.position, it.length) for it in after] == [(it.position, it.length) for it in before]
    assert all(it.pattern is None for it in after if isinstance(it, PatternPLItem))
    assert all(it.channel is None for it in after if isinstance(it, ChannelPLItem))
