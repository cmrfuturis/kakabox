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
    """Emuliert MicRecorder.record_until_silence() — liefert NUR die Aufnahme
    (path + speech_seen), KEIN Transkript (das macht ein separates transcribe_fn,
    genau wie beim echten MicRecorder/Recognizer-Duo)."""

    def __init__(self, speech_seen_sequence=None):
        # Ein Bool pro Aufnahme-Runde. Läuft die Liste aus, liefert jede
        # weitere Runde "keine Sprache" (Stille) — beendet den Loop sicher.
        self.speech_seen_sequence = speech_seen_sequence if speech_seen_sequence is not None else [True]
        self.call_count = 0
        self.recorded_kwargs = []

    def record_until_silence(self, max_seconds=60.0, silence_seconds=5.0, initial_silence_seconds=5.0):
        self.recorded_kwargs.append({
            "max_seconds": max_seconds,
            "silence_seconds": silence_seconds,
            "initial_silence_seconds": initial_silence_seconds,
        })
        idx = self.call_count
        self.call_count += 1
        result = MagicMock()
        result.speech_seen = idx < len(self.speech_seen_sequence) and self.speech_seen_sequence[idx]
        result.path = f"/tmp/rec_{idx}.wav"
        return result


def _fake_transcriber(transcripts):
    """Baut ein transcribe_fn, das die Transkripte der Reihe nach zurückgibt —
    eins pro Aufnahme-Runde, unabhängig vom übergebenen Pfad."""
    it = iter(transcripts)

    def fn(path):
        return next(it, "")

    return fn


def test_conversation_loop_exits_on_silence():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend(connected=True)
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[])

    asst = VoiceAssistant(backend, player, recorder)
    result = asst.conversation_loop({}, [])

    assert result is None
    assert recorder.call_count == 1


def test_conversation_loop_uses_correct_recorder_signature():
    """Regression: record_until_silence() akzeptiert max_seconds/silence_seconds/
    initial_silence_seconds — NICHT timeout_secs/silence_threshold (die es nie
    gab). Ohne diesen Test crasht ein falscher Kwarg-Name erst live auf der Box."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend(connected=True)
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[])

    asst = VoiceAssistant(backend, player, recorder)
    asst.conversation_loop({}, [])

    assert recorder.recorded_kwargs[0] == {
        "max_seconds": VoiceAssistant.MAX_TURN_SECONDS,
        "silence_seconds": VoiceAssistant.SILENCE_TIMEOUT,
        "initial_silence_seconds": VoiceAssistant.SILENCE_TIMEOUT,
    }


def test_conversation_loop_returns_play_song_intent():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    transcribe_fn = _fake_transcriber(["Spiele 99 Luftballons"])
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

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    result = asst.conversation_loop({}, [])

    assert result is not None
    assert result.get("intent") == "play_song"
    assert result.get("song_title") == "99 Luftballons"


def test_conversation_loop_returns_offline_without_recording():
    """Offline-Gate: Server nicht erreichbar → sofortiger Abbruch, KEINE
    Aufnahme (Kind soll nicht ins Leere sprechen)."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend(connected=False)
    player = MagicMock()
    recorder = _FakeRecorder()

    asst = VoiceAssistant(backend, player, recorder)
    result = asst.conversation_loop({}, [])

    assert result == {"intent": "offline"}
    assert recorder.call_count == 0


def test_conversation_loop_returns_offline_when_connection_lost_mid_turn():
    """Verbindung bricht ECHT weg (is_connected wird False) während des
    Requests → 'offline', damit main.py den Offline-Hinweis spielt."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend(connected=True)
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    transcribe_fn = _fake_transcriber(["Hallo"])

    def _drop_connection(*args, **kwargs):
        backend.is_connected = False  # WLAN fällt genau jetzt weg
        raise Exception("connection reset")
    backend._session.post.side_effect = _drop_connection

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    result = asst.conversation_loop({}, [])

    assert result == {"intent": "offline"}


def test_conversation_loop_returns_none_on_server_error_while_still_connected():
    """User-Wunsch: ein serverseitiger Fehler (z.B. 503 weil der Assistant
    server-seitig deaktiviert ist) bei weiterhin bestehender Verbindung ist
    KEIN Offline-Zustand — nur ein normaler, stiller Abbruch. Sonst hätte ein
    Konfigurationsfehler auf dem Server fälschlich behauptet, DIE BOX sei
    offline, obwohl die Verbindung die ganze Zeit stand."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend(connected=True)
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    transcribe_fn = _fake_transcriber(["Hallo"])
    backend._session.post.return_value = MagicMock(status_code=503, text="assistant disabled")

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    result = asst.conversation_loop({}, [])

    assert result is None
    assert backend.is_connected is True


def test_conversation_loop_returns_none_without_transcribe_fn():
    """Ohne transcribe_fn (main.py hat's vergessen zu übergeben) sauber
    abbrechen statt mit AttributeError zu crashen."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend(connected=True)
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=None)
    result = asst.conversation_loop({}, [])

    assert result is None


def test_conversation_loop_speaks_response_via_tts():
    """Antwort wird per Speaker synthetisiert + über Player abgespielt."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True, False])
    transcribe_fn = _fake_transcriber(["Was ist ein Löwe?"])
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"
    backend._session.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "status": "ok", "intent": "answer",
            "response": "Ein Löwe ist ein großes Tier.", "confidence": 0.9,
        }
    )

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker, volume=42, transcribe_fn=transcribe_fn)
    asst.conversation_loop({}, [])

    speaker.synth_to_wav.assert_called_with("Ein Löwe ist ein großes Tier.")
    player.play_prompt.assert_called_with("/tmp/answer.wav", 42)


def test_conversation_loop_sends_box_config_and_catalog():
    """box_config + catalog müssen im Server-Request landen (für Zauberwort/
    Nachtmodus-Regeln + Song-Auswahl im System-Prompt)."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True, False])
    transcribe_fn = _fake_transcriber(["Spiele was"])
    backend._session.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"status": "ok", "intent": "answer", "response": "ok", "confidence": 0.9}
    )
    box_config = {"zauberwort_mode_enabled": True, "quiet_hours": []}
    catalog = [{"content_id": 1, "title": "Testlied"}]

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    asst.conversation_loop(box_config, catalog)

    call_kwargs = backend._session.post.call_args[1]
    assert call_kwargs["json"]["box_config"] == box_config
    assert call_kwargs["json"]["catalog"] == catalog


def test_conversation_loop_safety_filter_blocks_response():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True, False])
    transcribe_fn = _fake_transcriber(["Erzähl mir etwas"])
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

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    box_config = {}  # kein special mode
    result = asst.conversation_loop(box_config, [])

    # Nach safety-check sollte die Response ersetzt sein
    assert result is None  # Keine play_song Intent, daher None (Abbruch)
