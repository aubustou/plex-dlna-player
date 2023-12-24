from __future__ import annotations
from datetime import timedelta

from plexdlnaserver.utils import parse_timedelta


def test_parse_timedelta():
    hours = 12
    minutes = 34
    seconds = 56
    microseconds = 789
    assert parse_timedelta(f"{hours}:{minutes}:{seconds}.{microseconds}") == timedelta(hours=hours,minutes=minutes, seconds=seconds, microseconds=microseconds)
    assert parse_timedelta(f"{hours}:{minutes}:{seconds}") == timedelta(hours=hours,minutes=minutes, seconds=seconds)
