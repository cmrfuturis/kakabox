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
