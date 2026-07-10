"""Tests für die zwei Deaktivierungs-Weichen (QS Top-Testlücken):

1. Spotify-Chip-Guard: liegt ein Spotify-Chip auf, während Spotify per Config
   deaktiviert ist (spotify.enabled=false → self._spotify None), darf er NICHT
   in den normalen Kaka-Flow fallen (Auto-Pairing legte sonst eine leere
   Geister-Kaka an). Und beim Abnehmen darf er keine laufende Wiedergabe stoppen.

2. Blau-Hold-Fallback: ist der KI-Modus deaktiviert (assistant.enabled=false),
   verhält sich Blau ≥2s halten wie ein kurzer Druck → normales Voice-PTT.

Kakabox.__init__ braucht Hardware — wie in den anderen Tests via
object.__new__ + gezieltes Attribut-Setzen umgangen.
"""
import main


def _bare_box(config=None) -> "main.Kakabox":
    box = object.__new__(main.Kakabox)
    box.config = config or {}
    box.leds = None
    box._spotify = None
    box._spotify_active = False
    box._spotify_tag_uids = {"53:05:8C:9F:53:00:01"}
    box._active_tag_uid = None
    box._random_mode = False
    box._voice_mode = False
    box._standby = False
    box._speed_mode = False
    box._speed = 1.0
    return box


# ── 1. Spotify-Chip-Guard (deaktiviert) ──────────────────────────────────


def test_disabled_spotify_chip_does_not_enter_kaka_flow(monkeypatch):
    box = _bare_box()

    called = {"kaka": False, "flash": False}
    monkeypatch.setattr(box, "_note_activity", lambda: None)
    # Wenn der Guard NICHT greift, würde _handle_tag weiter unten den Cache-/
    # Backend-Flow anstoßen — wir markieren das über _start_kaka_playlist.
    monkeypatch.setattr(box, "_start_kaka_playlist", lambda *a, **kw: called.__setitem__("kaka", True))
    monkeypatch.setattr(box, "_tag_cache", {}, raising=False)
    box.backend = None

    # Flash-Thread abfangen, damit kein echter Thread/Sleep läuft.
    import threading as _t
    monkeypatch.setattr(_t, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: called.__setitem__("flash", True)})())

    box._handle_tag("53:05:8C:9F:53:00:01")

    assert called["kaka"] is False   # KEIN Kaka-Flow für den Spotify-Chip
    assert called["flash"] is True   # stattdessen der Fehler-Blitz-Thread


def test_disabled_spotify_chip_removal_does_not_stop_playback(monkeypatch):
    """Random läuft, deaktivierter Spotify-Chip wird abgenommen → die
    Random-Wiedergabe darf NICHT gestoppt werden (F1)."""
    box = _bare_box()
    box._random_mode = True

    stopped = {"player": False, "restore": False}

    class _Player:
        def stop(self):
            stopped["player"] = True

    box.player = _Player()
    box._current_playlist = None
    monkeypatch.setattr(box, "_restore_playback_led", lambda: stopped.__setitem__("restore", True))

    box._on_tag_removed("53:05:8C:9F:53:00:01")

    assert stopped["player"] is False   # Wiedergabe unangetastet
    assert stopped["restore"] is True   # nur LED zurückgesetzt
    assert box._random_mode is True     # Random läuft weiter


# ── 2. Blau-Hold-Fallback (KI deaktiviert) ───────────────────────────────


def test_blue_hold_with_assistant_disabled_calls_voice_ptt(monkeypatch):
    box = _bare_box({"assistant": {"enabled": False}})

    called = {"ptt": False, "ki": False}
    monkeypatch.setattr(box, "_on_blue_pressed", lambda: called.__setitem__("ptt", True))
    # Falls der KI-Pfad fälschlich liefe, würde er _abort_prompt_if_playing rufen.
    monkeypatch.setattr(box, "_abort_prompt_if_playing", lambda: called.__setitem__("ki", True) or False)

    box._on_blue_held()

    assert called["ptt"] is True   # Voice-PTT statt KI
    assert called["ki"] is False   # KI-Pfad NICHT betreten


def test_blue_hold_with_assistant_enabled_does_not_shortcut_to_ptt(monkeypatch):
    """Gegenprobe: ist die KI aktiviert, darf der Hold NICHT den PTT-Shortcut
    nehmen, sondern den KI-Pfad betreten."""
    box = _bare_box({"assistant": {"enabled": True}})

    called = {"ptt": False}
    monkeypatch.setattr(box, "_on_blue_pressed", lambda: called.__setitem__("ptt", True))
    # KI-Pfad an der ersten Verzweigung abfangen (True = "Prompt lief, return").
    monkeypatch.setattr(box, "_abort_prompt_if_playing", lambda: True)

    box._on_blue_held()

    assert called["ptt"] is False   # KEIN PTT-Shortcut bei aktiver KI
