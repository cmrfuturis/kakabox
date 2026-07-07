"""Tests für die Upload-ENTSCHEIDUNG in Kakabox._save_voice_sample.

test_backend.py testet den HTTP-Aufruf isoliert; hier geht es um die
Orchestrierung: WANN wird hochgeladen (Config-Tor + Sprache-Tor) und mit
welchen Metadaten (matched_content_id nur bei echtem Einzeltitel-Treffer).
Kinder-Stimmdaten — die Tore sind datenschutzrelevant.
"""
import types
import wave
from pathlib import Path

import main
from voice.intent import Candidate, PlayCommand
from voice.recorder import RecordingResult
from voice.router import RouteResult


def _wav(tmp_path: Path) -> Path:
    p = tmp_path / "ptt.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return p


def _bare_box(config):
    box = object.__new__(main.Kakabox)
    box.config = config
    return box


class _FakeBackend:
    def __init__(self, connected=True):
        self.is_connected = connected
        self.calls = []

    def upload_voice_command(self, path, meta):
        self.calls.append((path, meta))
        return True


def _run_synchronously(monkeypatch):
    """threading.Thread(...).start() sofort im Aufrufer-Thread ausführen, damit
    der Upload deterministisch VOR dem Assert passiert."""
    class _SyncThread:
        def __init__(self, target=None, args=(), **kw):
            self._t, self._a = target, args

        def start(self):
            if self._t:
                self._t(*self._a)
    monkeypatch.setattr(main.threading, "Thread", _SyncThread)


def _play_route(kind="track", content_ids=(42,)):
    cand = Candidate(id="42", name="Superkind", kind=kind, content_ids=content_ids)
    cmd = PlayCommand(target=cand, score=0.9, raw_text="spiele superkind", query="superkind", margin=0.3)
    return RouteResult(action="play", command=cmd)


def _rec(tmp_path, speech_seen=True):
    return RecordingResult(path=_wav(tmp_path), speech_seen=speech_seen, duration_seconds=2.5)


def test_no_upload_when_flag_off(tmp_path, monkeypatch):
    _run_synchronously(monkeypatch)
    box = _bare_box({"voice": {"upload_commands": False, "keep_samples": False}})
    box.backend = _FakeBackend()
    box._save_voice_sample(_rec(tmp_path), "spiele superkind", _play_route())
    assert box.backend.calls == []


def test_no_upload_without_speech(tmp_path, monkeypatch):
    # Privacy-Tor: versehentlicher Knopfdruck ohne Sprache → kein Upload.
    _run_synchronously(monkeypatch)
    box = _bare_box({"voice": {"upload_commands": True, "keep_samples": False}})
    box.backend = _FakeBackend()
    box._save_voice_sample(_rec(tmp_path, speech_seen=False), None, RouteResult(action="no_speech"))
    assert box.backend.calls == []


def test_no_upload_when_offline(tmp_path, monkeypatch):
    _run_synchronously(monkeypatch)
    box = _bare_box({"voice": {"upload_commands": True, "keep_samples": False}})
    box.backend = _FakeBackend(connected=False)
    box._save_voice_sample(_rec(tmp_path), "spiele superkind", _play_route())
    assert box.backend.calls == []


def test_upload_sends_track_content_id(tmp_path, monkeypatch):
    _run_synchronously(monkeypatch)
    box = _bare_box({"voice": {"upload_commands": True, "keep_samples": False}})
    box.backend = _FakeBackend()
    box._save_voice_sample(_rec(tmp_path), "spiele superkind", _play_route())
    assert len(box.backend.calls) == 1
    _, meta = box.backend.calls[0]
    assert meta["transcript"] == "spiele superkind"
    assert meta["action"] == "play"
    assert meta["matched_name"] == "Superkind"
    assert meta["matched_content_id"] == 42   # Einzeltitel → content_id gesetzt


def test_upload_no_content_id_for_artist(tmp_path, monkeypatch):
    # Artist/Genre sind Sammlungen — kein einzelnes "erkanntes Lied".
    _run_synchronously(monkeypatch)
    box = _bare_box({"voice": {"upload_commands": True, "keep_samples": False}})
    box.backend = _FakeBackend()
    box._save_voice_sample(_rec(tmp_path), "spiele dikka", _play_route(kind="artist", content_ids=(1, 2, 3)))
    _, meta = box.backend.calls[0]
    assert meta["matched_content_id"] is None
    assert meta["matched_kind"] == "artist"


def test_upload_no_match_has_no_matched_fields(tmp_path, monkeypatch):
    _run_synchronously(monkeypatch)
    box = _bare_box({"voice": {"upload_commands": True, "keep_samples": False}})
    box.backend = _FakeBackend()
    box._save_voice_sample(_rec(tmp_path), "blabla", RouteResult(action="no_match"))
    assert len(box.backend.calls) == 1
    _, meta = box.backend.calls[0]
    assert meta["action"] == "no_match"
    assert meta["matched_name"] is None
    assert meta["matched_content_id"] is None
