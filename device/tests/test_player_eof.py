"""Tests für die Track-Ende-/Generation-Logik des Players (audio.player._eof_step).

Deckt die Race ab, die das adversariale Review fand: ein direkt nach einem
Prompt (z.B. Titel-Ansage) frisch gestarteter Resume-Track darf NICHT durch die
noch "idle" stehende mpv-Phase des Prompts fälschlich als beendet gewertet
werden. Verifiziert über die ausgelagerte, mpv-freie Schritt-Funktion.
"""
from audio.player import Player, PlaybackState


class FakeMpv:
    def __init__(self):
        self.volume = 80

    def play(self, path):
        pass


def _make_player():
    """Player ohne __init__ (kein echtes mpv, kein EOF-Thread)."""
    p = Player.__new__(Player)
    p._mpv = FakeMpv()
    p._state = PlaybackState()
    p._prompt_active = False
    p._volume_before_prompt = None
    p._on_track_end = None
    p._play_gen = 0
    return p


def test_normal_track_end_fires_callback():
    p = _make_player()
    calls = []
    p._on_track_end = lambda: calls.append(1)
    # Track läuft (gen=1, playing=True).
    p._play_gen = 1
    p._state.playing = True
    prev_idle, gen = True, 0
    # idle=False → playing_gen merkt sich die laufende Generation.
    prev_idle, gen = p._eof_step(False, prev_idle, gen)
    assert (prev_idle, gen) == (False, 1)
    # idle=True bei gleicher Generation → echtes Track-Ende.
    prev_idle, gen = p._eof_step(True, prev_idle, gen)
    assert p._state.playing is False
    assert calls == [1]


def test_resume_after_prompt_not_wiped():
    # Prompt läuft (gen=1) → endet (idle=True), aber der Resume hat bereits gen=2
    # gesetzt, bevor der EOF-Schritt die idle-Phase sieht. Der Resume-Track darf
    # NICHT entwertet werden.
    p = _make_player()
    calls = []
    p._on_track_end = lambda: calls.append(1)
    p._play_gen = 1
    p._state.playing = True
    prev_idle, gen = p._eof_step(False, True, 0)  # Prompt spielt → playing_gen=1
    # Resume-Track startet (play_file erhöht gen):
    p._play_gen = 2
    p._state.playing = True
    # EOF sieht jetzt die (noch) idle-Phase, aber Generation hat sich geändert:
    prev_idle, gen = p._eof_step(True, prev_idle, gen)
    assert p._state.playing is True          # Resume bleibt am Leben
    assert calls == []                        # kein falsches Track-Ende
    # Wenn der Resume-Track später WIRKLICH endet, greift es korrekt:
    prev_idle, gen = p._eof_step(False, prev_idle, gen)  # Resume spielt → playing_gen=2
    prev_idle, gen = p._eof_step(True, prev_idle, gen)   # Resume endet
    assert p._state.playing is False
    assert calls == [1]


def test_prompt_end_restores_volume_and_suppresses_callback():
    # Fängt der EOF-Schritt das Prompt-Ende direkt (gleiche Generation), wird die
    # User-Lautstärke wiederhergestellt und der Musik-Callback unterdrückt.
    p = _make_player()
    calls = []
    p._on_track_end = lambda: calls.append(1)
    p._prompt_active = True
    p._volume_before_prompt = 65
    p._play_gen = 1
    p._state.playing = True
    prev_idle, gen = p._eof_step(False, True, 0)   # Prompt spielt
    prev_idle, gen = p._eof_step(True, prev_idle, gen)  # Prompt endet (gleiche gen)
    assert p._state.playing is False
    assert p._prompt_active is False
    assert p._mpv.volume == 65       # User-Lautstärke zurück
    assert calls == []               # Prompt triggert keine Playlist-Logik


def test_paused_does_not_fire():
    p = _make_player()
    calls = []
    p._on_track_end = lambda: calls.append(1)
    p._play_gen = 1
    p._state.playing = True
    p._state.paused = True
    prev_idle, gen = p._eof_step(False, True, 0)
    prev_idle, gen = p._eof_step(True, prev_idle, gen)
    assert calls == []               # Pause ist kein Track-Ende
