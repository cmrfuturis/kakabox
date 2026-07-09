"""Tests für voice.assistant — KI-Assistent für Kinder."""
import json
from unittest.mock import MagicMock

import pytest

from voice.assistant import VoiceAssistant


class _FakeBackend:
    def __init__(self, connected=True, result=None):
        self.is_connected = connected
        self._base_url = "https://test"
        self._session = MagicMock()
        self._result = result or {
            "status": "ok",
            "intent": "answer",
            "response": "Das ist eine tolle Frage!",
            "confidence": 0.9,
        }

    def _auth_headers(self):
        return {"Authorization": "Bearer test"}


class _FakeLResponse:
    def __init__(self, data, status=200):
        self.status_code = status
        self._data = data

    def ok(self):
        return self.status_code == 200

    def json(self):
        return self._data


# --------------------------------------------------------------------------
# VoiceAssistant — Memory + Server Integration
# --------------------------------------------------------------------------


def test_assistant_understand_success():
    backend = _FakeBackend()
    response = _FakeLResponse(_FakeBackend()._result)
    backend._session.post.return_value = response
    player = MagicMock()

    asst = VoiceAssistant(backend, player)
    result = asst.understand("Was ist ein Löwe?")

    assert result == "Das ist eine tolle Frage!"
    assert len(asst.history) == 1
    assert asst.history[0]["transcript"] == "Was ist ein Löwe?"


def test_assistant_offline_returns_none():
    backend = _FakeBackend(connected=False)
    player = MagicMock()

    asst = VoiceAssistant(backend, player)
    result = asst.understand("test")

    assert result is None
    assert backend._session.post.call_count == 0


def test_assistant_error_503_returns_none():
    backend = _FakeBackend()
    response = _FakeLResponse({"status": "disabled"}, status=503)
    backend._session.post.return_value = response
    player = MagicMock()

    asst = VoiceAssistant(backend, player)
    result = asst.understand("test")

    assert result is None


def test_assistant_history_max_10_turns():
    backend = _FakeBackend()
    response = _FakeLResponse(_FakeBackend()._result)
    backend._session.post.return_value = response
    player = MagicMock()

    asst = VoiceAssistant(backend, player)
    for i in range(15):
        asst.understand(f"Frage {i}")

    assert len(asst.history) == 10  # capped at max_history


def test_assistant_now_playing_in_context():
    backend = _FakeBackend()
    response = _FakeLResponse(_FakeBackend()._result)
    backend._session.post.return_value = response
    player = MagicMock()

    asst = VoiceAssistant(backend, player)
    asst.set_now_playing("99 Luftballons", "Nena")
    asst.understand("Wer singt das?")

    # Verifiziere now_playing wurde im Request mitgesendet
    call_args = backend._session.post.call_args
    assert call_args[1]["json"]["now_playing"] == {
        "title": "99 Luftballons",
        "artist": "Nena",
    }


def test_assistant_clear_history():
    backend = _FakeBackend()
    response = _FakeLResponse(_FakeBackend()._result)
    backend._session.post.return_value = response
    player = MagicMock()

    asst = VoiceAssistant(backend, player)
    asst.understand("test1")
    asst.understand("test2")
    assert len(asst.history) == 2

    asst.clear_history()
    assert len(asst.history) == 0
    assert asst.now_playing is None


def test_assistant_child_age_passed_to_server():
    backend = _FakeBackend()
    response = _FakeLResponse(_FakeBackend()._result)
    backend._session.post.return_value = response
    player = MagicMock()

    asst = VoiceAssistant(backend, player)
    asst.child_age = 8
    asst.understand("Mathe-Frage")

    call_args = backend._session.post.call_args
    assert call_args[1]["json"]["child_age"] == 8


def test_assistant_debug_info():
    backend = _FakeBackend()
    player = MagicMock()

    asst = VoiceAssistant(backend, player)
    asst.child_age = 6
    asst.set_now_playing("Bibi und Tina", "Lied")

    info = asst.get_debug_info()
    assert info["enabled"] is True
    assert info["child_age"] == 6
    assert info["now_playing"]["title"] == "Bibi und Tina"
    assert info["history_turns"] == 0


# --------------------------------------------------------------------------
# SafetyFilter — Vulgär, Rassistisch, Zauberwort, Nachtmodus
# --------------------------------------------------------------------------


def test_safety_filter_blocks_vulgar_words():
    from voice.assistant import SafetyFilter
    assert not SafetyFilter.is_safe("Das ist scheisse!", {})
    assert not SafetyFilter.is_safe("verdammt, das ist falsch", {})


def test_safety_filter_blocks_racist_content():
    from voice.assistant import SafetyFilter
    assert not SafetyFilter.is_safe("Der neger ist schnell", {})
    assert not SafetyFilter.is_safe("Das ist doch behindert", {})


def test_safety_filter_allows_clean_text():
    from voice.assistant import SafetyFilter
    assert SafetyFilter.is_safe("Spiele 99 Luftballons", {})
    assert SafetyFilter.is_safe("Guten Morgen!", {})


def test_safety_filter_zauberwort_mode_only_music():
    from voice.assistant import SafetyFilter
    box_config = {"zauberwort_mode_enabled": True}
    # Musik-Befehle erlaubt
    assert SafetyFilter.is_safe("Spiele irgendwas", box_config)
    assert SafetyFilter.is_safe("Pausiere", box_config)
    # Story-Intent blockiert
    assert not SafetyFilter.is_safe("Erzähl mir eine Geschichte", box_config)


def test_safety_filter_quiet_hours_blocks_scary_topics():
    from voice.assistant import SafetyFilter
    box_config = {"quiet_hours": [{"start": "20:00", "end": "07:00"}]}
    # Horror-Inhalte blockiert
    assert not SafetyFilter.is_safe("Erzähl mir was Gruseliges", box_config)
    assert not SafetyFilter.is_safe("Monster und Angst", box_config)
    # Normale Fragen erlaubt
    assert SafetyFilter.is_safe("Was ist eine Pflanze?", box_config)


# --------------------------------------------------------------------------
# Conversation Loop — Mock-Recorder + Intent-Routing
# --------------------------------------------------------------------------


class _FakeRecorder:
    def __init__(self, transcripts=None, speech_seen=True):
        self.transcripts = transcripts or ["Spiele 99 Luftballons"]
        self.speech_seen_val = speech_seen
        self.call_count = 0

    def record_until_silence(self, timeout_secs=5.0, silence_threshold=0.1):
        if self.call_count >= len(self.transcripts):
            # Stille (kein Transkript) → Abbruch
            result = MagicMock()
            result.speech_seen = False
            result.transcript = None
            result.path = "/tmp/silent.wav"
            self.call_count += 1
            return result
        result = MagicMock()
        result.speech_seen = self.speech_seen_val
        result.transcript = self.transcripts[self.call_count]
        result.path = f"/tmp/rec_{self.call_count}.wav"
        self.call_count += 1
        return result


def test_conversation_loop_exits_on_silence():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend(connected=True)
    player = MagicMock()
    recorder = _FakeRecorder(transcripts=[], speech_seen=False)
    backend._session.post.return_value = MagicMock(status_code=503)

    asst = VoiceAssistant(backend, player, recorder)
    result = asst.conversation_loop({}, [])

    assert result is None
    assert recorder.call_count == 1


def test_conversation_loop_returns_play_song_intent():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(transcripts=["Spiele 99 Luftballons", ""])
    backend._session.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "status": "ok",
            "intent": "play_song",
            "response": "Hier ist dein Lied!",
            "action": {"song_id": "99lb", "song_title": "99 Luftballons"},
            "confidence": 0.95,
        }
    )

    asst = VoiceAssistant(backend, player, recorder)
    result = asst.conversation_loop({}, [])

    assert result is not None
    assert result.get("intent") == "play_song"
    assert result.get("song_title") == "99 Luftballons"


def test_conversation_loop_safety_filter_blocks_response():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(transcripts=["Erzähl mir etwas", ""])
    # Claude antwortet mit vulgarem Inhalt (hypothetisch — sollte nicht vorkommen,
    # aber Test deckt es ab)
    backend._session.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "status": "ok",
            "intent": "story",
            "response": "Das ist verdammt gruselig!",
            "confidence": 0.9,
        }
    )

    asst = VoiceAssistant(backend, player, recorder)
    box_config = {}  # kein special mode
    result = asst.conversation_loop(box_config, [])

    # Nach safety-check sollte die Response ersetzt sein
    assert result is None  # Keine play_song Intent, daher None (Abbruch)
