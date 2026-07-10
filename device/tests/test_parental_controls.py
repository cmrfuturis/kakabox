"""Tests für die geräteseitige Durchsetzung von Ruhezeiten/Kategoriesperren
(H5-Fix, QS-Audit 2026-07-07). _start_kaka_playlist ist der zentrale
Funnel für NFC-Tag-Placement, Resume und Voice-Kaka-Matches.

Kakabox.__init__ braucht Hardware — wir umgehen es wie in test_commands.py
mit object.__new__ und setzen nur die Attribute, die der jeweilige Pfad
anfasst.
"""
from datetime import datetime

import main


def _bare_box(config=None) -> "main.Kakabox":
    box = object.__new__(main.Kakabox)
    box.config = config or {}
    box.leds = None  # kein Hardware-Flash in Tests noetig
    # Speed-Mode-Defaults wie in __init__ — _start_kaka_playlist ruft jetzt
    # _reset_speed_if_active() (F6-Fix), das diese Attribute liest.
    box._speed_mode = False
    box._speed = 1.0
    return box


def test_is_category_blocked_true_when_in_list():
    box = _bare_box({"blocked_category_ids": [3, 7]})
    assert box._is_category_blocked(7) is True
    assert box._is_category_blocked(3) is True


def test_is_category_blocked_false_when_not_in_list():
    box = _bare_box({"blocked_category_ids": [3, 7]})
    assert box._is_category_blocked(1) is False


def test_is_category_blocked_false_when_category_id_is_none():
    # Inhalte ohne Kategorie werden nie gesperrt, egal was blockiert ist.
    box = _bare_box({"blocked_category_ids": [3, 7]})
    assert box._is_category_blocked(None) is False


def test_is_category_blocked_false_when_no_rule_configured():
    box = _bare_box({})
    assert box._is_category_blocked(5) is False


def test_quiet_hours_now_reflects_config(monkeypatch):
    box = _bare_box({"quiet_hours": [
        {"start_time": "20:00", "end_time": "07:00", "days": ["mo", "di", "mi", "do", "fr", "sa", "so"]},
    ]})

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 6, 21, 0)  # Montag 21:00 — in der Ruhezeit

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    assert box._quiet_hours_now() is True


def test_start_kaka_playlist_silently_rejects_during_quiet_hours(monkeypatch):
    """Kernversprechen des H5-Fixes: während der Ruhezeit wird NICHTS gestartet
    — kein Sound, keine Playlist, kein Player-Aufruf."""
    box = _bare_box({"quiet_hours": [
        {"start_time": "00:00", "end_time": "23:59", "days": ["mo", "di", "mi", "do", "fr", "sa", "so"]},
    ]})
    box._current_playlist = None
    playlist_started = []
    monkeypatch.setattr(main, "Playlist", lambda *a, **kw: playlist_started.append((a, kw)))

    box._start_kaka_playlist("UID1", {
        "id": 1, "name": "Testfigur",
        "contents": [{"id": 1, "title": "Lied", "playable": True, "category_id": None}],
    })

    assert playlist_started == []
    assert box._current_playlist is None


def test_start_kaka_playlist_silently_rejects_when_all_content_blocked(monkeypatch):
    box = _bare_box({"blocked_category_ids": [9]})
    box._current_playlist = None
    playlist_started = []
    monkeypatch.setattr(main, "Playlist", lambda *a, **kw: playlist_started.append((a, kw)))

    box._start_kaka_playlist("UID2", {
        "id": 2, "name": "Gruselfigur",
        "contents": [
            {"id": 10, "title": "Gruselied 1", "playable": True, "category_id": 9},
            {"id": 11, "title": "Gruselied 2", "playable": True, "category_id": 9},
        ],
    })

    assert playlist_started == []
    assert box._current_playlist is None


def test_start_kaka_playlist_proceeds_when_no_rules_configured(monkeypatch):
    """Ohne quiet_hours/blocked_category_ids darf sich am bestehenden Verhalten
    nichts ändern — Playlist wird ganz normal konstruiert."""
    box = _bare_box({})
    box._current_playlist = None
    box._playlist_lock = __import__("threading").Lock()
    box._active_tag_uid = None
    box._random_mode = False
    box._voice_mode = False
    box._voice_pending_tag_uid = None
    box._voice_last_target = None
    box._last_kaka_memory = None
    box.audio_cache = object()
    box.backend = None

    class FakePlayer:
        def play_file(self, *a, **kw):
            pass

        def stop(self):
            pass

        def current_position_seconds(self):
            return 0.0

        def seek_to(self, pos):
            pass

    box.player = FakePlayer()

    monkeypatch.setattr(box, "_compute_resume", lambda uid: (0, 0.0))
    monkeypatch.setattr(box, "_playback_session_callbacks", lambda **kw: (None, None))
    monkeypatch.setattr(box, "_trigger_background_sync", lambda reason: None)

    built = {}

    class FakePlaylist:
        def __init__(self, contents, **kw):
            built["contents"] = contents

        def start(self, start_index=0, start_position=0.0):
            built["started"] = True
            return True

        current_index = 0
        length = 1

    monkeypatch.setattr(main, "Playlist", FakePlaylist)

    box._start_kaka_playlist("UID3", {
        "id": 3, "name": "Normalfigur",
        "contents": [{"id": 20, "title": "Normales Lied", "playable": True, "category_id": 1}],
    }, trigger_sync=False)

    assert built.get("started") is True
    assert len(built["contents"]) == 1
    assert built["contents"][0].content_id == 20
