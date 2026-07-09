"""Tests für voice.assistant — KI-Assistent für Kinder."""
import json
import threading
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


def _legacy_response(payload, status=200):
    """Fake-Response im Legacy-Format (alter Server ohne Streaming):
    Content-Type application/json + volles Ergebnis-JSON. _open_stream()
    erkennt das am Content-Type und fällt auf den klassischen Pfad zurück."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status < 400
    resp.headers = {"Content-Type": "application/json"}
    resp.json = lambda: payload
    resp.text = json.dumps(payload)
    return resp


class _FakeStreamResponse:
    """Fake für eine gestreamte Server-Antwort (Content-Type text/plain):
    liefert den Text in kleinen Chunks, wie ihn requests' iter_content
    liefern würde."""

    def __init__(self, text, status=200, content_type="text/plain; charset=utf-8", chunk_size=7):
        self.status_code = status
        self.ok = status < 400
        self.headers = {"Content-Type": content_type}
        self._text = text
        self._chunk = chunk_size
        self.encoding = None
        self.closed = False

    @property
    def text(self):
        return self._text

    def json(self):
        return json.loads(self._text)

    def iter_content(self, chunk_size=None, decode_unicode=False):
        for i in range(0, len(self._text), self._chunk):
            yield self._text[i:i + self._chunk]

    def close(self):
        self.closed = True


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


def test_assistant_omits_now_playing_when_nothing_plays():
    """Regression: Laravels 'sometimes|array'-Validierung prüft nur, ob der
    Key existiert — ein explizites "now_playing": null zählt als vorhanden
    und scheitert an der array-Regel (422 bei JEDER Anfrage ohne laufendes
    Lied, live beobachtet). Der Key muss bei fehlendem now_playing GANZ
    fehlen, nicht mit null gesendet werden."""
    backend = _FakeBackend()
    response = _FakeLResponse(_FakeBackend()._result)
    backend._session.post.return_value = response
    player = MagicMock()

    asst = VoiceAssistant(backend, player)
    asst.understand("Was ist ein Löwe?")  # kein set_now_playing() aufgerufen

    call_args = backend._session.post.call_args
    assert "now_playing" not in call_args[1]["json"]


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


def test_safety_filter_ignores_zauberwort_mode_entirely():
    """User-Klarstellung (2026-07-09): Zauberwort-Modus hat NICHTS mit
    SafetyFilter/Konversationsarten zu tun — 'bitte' ist nur für Song-Wünsche
    relevant und wird separat in conversation_loop() geprüft (siehe
    test_conversation_loop_zauberwort_*). Witze/Geschichten/Fragen sind vom
    Zauberwort-Modus IMMER unberührt, egal ob 'bitte' gesagt wurde."""
    from voice.assistant import SafetyFilter
    box_config = {"zauberwort_mode_enabled": True}
    assert SafetyFilter.is_safe("Erzähl mir eine Geschichte", box_config)
    assert SafetyFilter.is_safe("Warum können Geister nicht lügen? Weil man durch sie hindurchsieht!", box_config)
    assert SafetyFilter.is_safe("Spiele irgendwas", box_config)


def test_safety_filter_quiet_hours_blocks_scary_topics():
    """quiet_hours_active ist der von main.py AUSGEWERTETE Bool (Nachtmodus
    JETZT) — der alte Zeitplan-Listen-Key aktivierte die Regeln 24/7, sobald
    irgendein Fenster konfiguriert war (Review-Finding)."""
    from voice.assistant import SafetyFilter
    box_config = {"quiet_hours_active": True}
    # Horror-Inhalte blockiert
    assert not SafetyFilter.is_safe("Erzähl mir was Gruseliges", box_config)
    assert not SafetyFilter.is_safe("Monster und Angst", box_config)
    # Normale Fragen erlaubt
    assert SafetyFilter.is_safe("Was ist eine Pflanze?", box_config)


def test_safety_filter_quiet_hours_schedule_alone_does_not_block():
    """Regression: der ROHE Zeitplan (Liste) darf die Nachtmodus-Regeln NICHT
    aktivieren — nur der ausgewertete quiet_hours_active-Bool zählt. Sonst
    blockte die KI Grusel-Themen rund um die Uhr, sobald Eltern irgendein
    Schlaffenster konfiguriert hatten."""
    from voice.assistant import SafetyFilter
    box_config = {"quiet_hours": [{"start": "20:00", "end": "07:00"}]}
    assert SafetyFilter.is_safe("Erzähl mir was Gruseliges", box_config)


def test_safety_filter_word_boundaries():
    """Regression (Review-Finding): rohes Substring-Matching blockierte
    harmlose Antworten — 'arsch' in 'Marsch', 'geist' in 'begeistert'."""
    from voice.assistant import SafetyFilter
    assert SafetyFilter.is_safe("Das ist der Radetzky-Marsch von Strauss", {})
    assert SafetyFilter.is_safe("Ich bin ganz begeistert!", {"quiet_hours_active": True})
    # Wortanfang matcht weiterhin, auch flektiert:
    assert not SafetyFilter.is_safe("Er wollte ihn verprügeln.", {})
    assert not SafetyFilter.is_safe("Gruselige Geschichten!", {"quiet_hours_active": True})


# --------------------------------------------------------------------------
# Conversation Loop — Mock-Recorder + Intent-Routing
# --------------------------------------------------------------------------


class _FakeRecorder:
    """Emuliert MicRecorder.record_until_silence() — liefert NUR die Aufnahme
    (path + speech_seen), KEIN Transkript (das macht ein separates transcribe_fn,
    genau wie beim echten MicRecorder/Recognizer-Duo)."""

    def __init__(self, speech_seen_sequence=None, cancelled_at=None, pre_delay=0.0):
        # Ein Bool pro Aufnahme-Runde. Läuft die Liste aus, liefert jede
        # weitere Runde "keine Sprache" (Stille) — beendet den Loop sicher.
        self.speech_seen_sequence = speech_seen_sequence if speech_seen_sequence is not None else [True]
        # 0-basierter Call-Index, ab dem cancelled=True simuliert wird (harter
        # Stopp, z.B. Blau-Knopf) — None = nie.
        self.cancelled_at = cancelled_at
        # Verzögerung pro Aufnahme (Sekunden) — simuliert, dass eine echte
        # Aufnahme Zeit braucht. Nötig für Tests, in denen der Speaker-Thread
        # VOR dem ersten Lauschfenster-Ergebnis gesprochen haben muss.
        self.pre_delay = pre_delay
        self.call_count = 0
        self.recorded_kwargs = []

    def record_until_silence(self, max_seconds=60.0, silence_seconds=5.0,
                              initial_silence_seconds=5.0, cancel_event=None):
        if self.pre_delay:
            import time as _time
            _time.sleep(self.pre_delay)
        self.recorded_kwargs.append({
            "max_seconds": max_seconds,
            "silence_seconds": silence_seconds,
            "initial_silence_seconds": initial_silence_seconds,
        })
        idx = self.call_count
        self.call_count += 1
        result = MagicMock()
        result.cancelled = self.cancelled_at is not None and idx >= self.cancelled_at
        result.speech_seen = (
            not result.cancelled
            and idx < len(self.speech_seen_sequence)
            and self.speech_seen_sequence[idx]
        )
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


def test_turn_silence_is_responsive_not_full_timeout():
    """Regression: Live-Test zeigte spürbare Verzögerung nach jedem Satz, weil
    die Nachlauf-Stille (Satzende-Erkennung) denselben 5s-Wert nutzte wie der
    'Kind reagiert gar nicht'-Abbruch. TURN_SILENCE_SECONDS muss deutlich
    kürzer sein als SILENCE_TIMEOUT, sonst fühlt sich die KI nach jedem Turn
    wieder träge an statt Siri-artig direkt zu reagieren."""
    from voice.assistant import VoiceAssistant
    assert VoiceAssistant.TURN_SILENCE_SECONDS < VoiceAssistant.SILENCE_TIMEOUT
    assert VoiceAssistant.TURN_SILENCE_SECONDS <= 2.0


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
        "silence_seconds": VoiceAssistant.TURN_SILENCE_SECONDS,
        "initial_silence_seconds": VoiceAssistant.SILENCE_TIMEOUT,
    }


def test_conversation_loop_returns_play_song_intent():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    transcribe_fn = _fake_transcriber(["Spiele 99 Luftballons"])
    backend._session.post.return_value = _legacy_response({
            "status": "ok",
            "intent": "play_song",
            "response": "Hier ist dein Lied!",
            "action": {"song_id": "99lb", "song_title": "99 Luftballons"},
            "confidence": 0.95,
        })

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    result = asst.conversation_loop({}, [])

    assert result is not None
    assert result.get("intent") == "play_song"
    assert result.get("song_title") == "99 Luftballons"


def test_conversation_loop_zauberwort_mode_blocks_song_without_bitte():
    """User-Klarstellung (2026-07-09): Zauberwort-Modus verlangt 'bitte' NUR
    für Songs. Fehlt es im TRANSKRIPT, wird play_song zu answer umgewidmet
    (Erinnerung gesprochen statt Playback) — deterministischer Backstop,
    falls Claude die System-Prompt-Regel trotzdem mal ignoriert."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"
    recorder = _FakeRecorder(speech_seen_sequence=[True, False])
    transcribe_fn = _fake_transcriber(["Spiele 99 Luftballons"])  # KEIN "bitte"
    backend._session.post.return_value = _legacy_response({
            "status": "ok", "intent": "play_song",
            "response": "Klar, hier kommt 99 Luftballons!",
            "action": {"song_id": 12, "song_title": "99 Luftballons"},
            "confidence": 0.95,
        })
    box_config = {"zauberwort_mode_enabled": True}

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker, transcribe_fn=transcribe_fn)
    result = asst.conversation_loop(box_config, [])

    # KEIN play_song-Ergebnis — stattdessen wurde die Erinnerung gesprochen.
    assert result is None
    speaker.synth_to_wav.assert_called_with("Du musst noch 'bitte' sagen, wenn du ein Lied hören möchtest!")


def test_conversation_loop_zauberwort_mode_allows_song_with_bitte():
    """Mit 'bitte' im Transkript wird der Song ganz normal gespielt, auch bei
    aktivem Zauberwort-Modus."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    transcribe_fn = _fake_transcriber(["Spiele bitte 99 Luftballons"])
    backend._session.post.return_value = _legacy_response({
            "status": "ok", "intent": "play_song",
            "response": "Klar, hier kommt 99 Luftballons!",
            "action": {"song_id": 12, "song_title": "99 Luftballons"},
            "confidence": 0.95,
        })
    box_config = {"zauberwort_mode_enabled": True}

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    result = asst.conversation_loop(box_config, [])

    assert result == {"intent": "play_song", "song_id": 12, "song_title": "99 Luftballons"}


def test_conversation_loop_zauberwort_mode_does_not_affect_jokes():
    """Kern der User-Klarstellung: Witze/Geschichten/Fragen sind vom
    Zauberwort-Modus KOMPLETT unberührt — funktionieren unabhängig davon ob
    'bitte' gesagt wurde."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"
    recorder = _FakeRecorder(speech_seen_sequence=[True, False])
    transcribe_fn = _fake_transcriber(["Erzähl mir einen Witz"])  # KEIN "bitte"
    joke_text = "Warum können Geister nicht lügen? Weil man durch sie hindurchsieht!"
    backend._session.post.return_value = _legacy_response({"status": "ok", "intent": "joke", "response": joke_text, "confidence": 0.9})
    box_config = {"zauberwort_mode_enabled": True}

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker, transcribe_fn=transcribe_fn)
    asst.conversation_loop(box_config, [])

    # Der ECHTE Witz wurde gesprochen (satzweise), NICHT die Zauberwort-
    # Erinnerung. Der Satz-Splitter trennt Frage und Pointe.
    spoken = " ".join(c.args[0] for c in speaker.synth_to_wav.call_args_list)
    assert "Warum können Geister nicht lügen?" in spoken
    assert "Weil man durch sie hindurchsieht!" in spoken
    assert "bitte" not in spoken.lower()


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


# --------------------------------------------------------------------------
# Barge-in — Kind unterbricht die KI während sie noch antwortet
# --------------------------------------------------------------------------


def test_speak_sentences_no_barge_in_ends_with_silence():
    """Ohne Barge-in spielt die Antwort komplett ab; ein volles Stille-Fenster
    NACH dem Wiedergabe-Ende beendet die Konversation (outcome=silence)."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[])  # nur Stille-Fenster
    transcribe_fn = _fake_transcriber([])
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn, volume=50)
    outcome = asst._speak_sentences_interruptible(iter(["Hallo!"]), {})

    assert outcome["outcome"] == "silence"
    assert outcome["transcript"] is None
    assert outcome["full_text"] == "Hallo!"
    speaker.synth_to_wav.assert_called_with("Hallo!")
    player.play_prompt.assert_called_with("/tmp/answer.wav", 50)


def test_speak_sentences_barge_in_stops_playback_and_returns_transcript():
    """Kern des Barge-in-Verhaltens (wie ChatGPT Voice Mode): wird während/
    nach der Antwort Sprache erkannt, stoppt player.stop() die Wiedergabe und
    der transkribierte Text wird als nächster Turn zurückgegeben."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    transcribe_fn = _fake_transcriber(["Warte, ich hab noch was"])
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn, volume=50)
    outcome = asst._speak_sentences_interruptible(iter(["Es war einmal..."]), {})

    assert outcome["outcome"] == "barge_in"
    assert outcome["transcript"] == "Warte, ich hab noch was"
    player.stop.assert_called_once()


def test_speak_sentences_without_recorder_falls_back_to_blocking():
    """Ohne recorder/transcribe_fn (kein Barge-in möglich) wird blockierend
    gesprochen; outcome=done_no_listener → Aufrufer nimmt danach normal den
    nächsten Turn auf."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"

    asst = VoiceAssistant(backend, player, recorder=None, speaker=speaker, volume=50)
    outcome = asst._speak_sentences_interruptible(iter(["Hallo!"]), {})

    assert outcome["outcome"] == "done_no_listener"
    assert outcome["full_text"] == "Hallo!"
    player.wait_until_idle.assert_called_once()


def test_speak_sentences_long_answer_not_cut_by_silent_window_during_playback():
    """Regression gegen den Vorgänger-Bug: eine Antwort, die LÄNGER läuft als
    das 5s-Stille-Fenster, wurde nach dem ersten sprachlosen Fenster per
    player.stop() mitten im Satz abgeschnitten (wenn das Mikro die Box-Stimme
    nicht hört). Jetzt beendet nur ein Fenster, das NACH dem Wiedergabe-Ende
    begann, die Konversation — Fenster, die die Wiedergabe überlappen, lauschen
    einfach weiter."""
    import time as _time
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    # Wiedergabe "dauert": wait_until_idle blockiert kurz, damit die ersten
    # Lauschfenster garantiert WÄHREND der Wiedergabe starten.
    player.wait_until_idle.side_effect = lambda timeout=30.0: _time.sleep(0.15)
    recorder = _FakeRecorder(speech_seen_sequence=[])
    transcribe_fn = _fake_transcriber([])
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn, volume=50)
    outcome = asst._speak_sentences_interruptible(
        iter(["Satz eins.", "Satz zwei.", "Satz drei."]), {},
    )

    assert outcome["outcome"] == "silence"
    # ALLE Sätze wurden gespielt — nichts wurde vom Stille-Fenster abgeschnitten.
    assert outcome["full_text"] == "Satz eins. Satz zwei. Satz drei."
    assert player.play_prompt.call_count == 3


def test_speak_sentences_safety_blocks_single_sentence():
    """Streaming-Safety: jeder Satz wird EINZELN geprüft — ein unsicherer Satz
    wird durch den Blocktext ersetzt und der Rest der Antwort verworfen."""
    from voice.assistant import BLOCKED_TEXT, VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[])
    transcribe_fn = _fake_transcriber([])
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn, volume=50)
    outcome = asst._speak_sentences_interruptible(
        iter(["Erster Satz ist ok.", "Das ist verdammt schlimm.", "Kommt nie an."]),
        {},
    )

    assert outcome["outcome"] == "silence"
    spoken = [c.args[0] for c in speaker.synth_to_wav.call_args_list]
    assert spoken == ["Erster Satz ist ok.", BLOCKED_TEXT]


def test_conversation_loop_barge_in_skips_new_recording_for_next_turn():
    """Barge-in während der Antwort → der aufgenommene Text wird DIREKT als
    nächster Turn verarbeitet, OHNE eine zusätzliche Aufnahme zu starten."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"
    # [0] erster Turn (Sprache), [1] Barge-in während der ersten Antwort
    recorder = _FakeRecorder(speech_seen_sequence=[True, True])
    transcribe_fn = _fake_transcriber(["Erzähl mir was", "Stopp, spiel Musik"])

    responses = [
        {"status": "ok", "intent": "story", "response": "Es war einmal...", "confidence": 0.9},
        {"status": "ok", "intent": "play_song", "response": "Klar!",
         "action": {"song_id": 5, "song_title": "Testlied"}, "confidence": 0.95},
    ]
    backend._session.post.side_effect = [
        _legacy_response(responses[0]),
        _legacy_response(responses[1]),
    ]

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn, volume=50)
    result = asst.conversation_loop({}, [])

    assert result == {"intent": "play_song", "song_id": 5, "song_title": "Testlied"}
    # Nur 2 Recorder-Calls insgesamt: initiale Aufnahme + Barge-in-Erkennung
    # während der ersten Antwort. KEIN dritter Call für den zweiten Turn —
    # der kam ja schon aus dem Barge-in.
    assert recorder.call_count == 2
    assert backend._session.post.call_count == 2


def test_looks_like_self_echo_detects_matching_text():
    """Regression (Live-Test 2026-07-09): ohne Echo-Unterdrückung hörte das
    Mikro die eigene TTS-Stimme der Box und löste eine Rückkopplungsschleife
    aus (Box sagt 'Davon kann ich dir nicht erzählen', hört sich selbst,
    verarbeitet das erneut, blockiert erneut, hört sich wieder, ...)."""
    from voice.assistant import _looks_like_self_echo
    assert _looks_like_self_echo(
        "Davon kann ich dir nicht erzählen.",
        "Davon kann ich dir nicht erzählen.",
    )
    assert _looks_like_self_echo(
        "Davon kann ich dir nicht erzählen.",
        "davon kann ich dir nicht erzählen",  # leichte ASR-Variation
    )


def test_looks_like_self_echo_ignores_unrelated_text():
    from voice.assistant import _looks_like_self_echo
    assert not _looks_like_self_echo(
        "Davon kann ich dir nicht erzählen.",
        "Spiele mir 99 Luftballons",
    )


def test_strip_emoji_removes_common_emoji():
    """Regression (Live-Test 2026-07-09): Claude nutzt Emoji in Antworten,
    Piper (TTS) kann die nicht sprechen und verbalisiert sie stattdessen als
    Beschreibung ("😄" → "Gesicht mit lachenden Augen") — das Mikro hört diese
    Beschreibung (kein Echo-Cancelling) und verwechselt sie mit Kind-Eingabe,
    weil sie zu weit vom Quelltext abweicht um vom Selbst-Echo-Filter erkannt
    zu werden. Also VOR der Synthese entfernen."""
    from voice.assistant import _strip_emoji
    assert _strip_emoji("Klar, hier kommt dein Lied! 😄") == "Klar, hier kommt dein Lied! "
    assert _strip_emoji("Toll gemacht! 🎉🎈") == "Toll gemacht! "
    assert _strip_emoji("Ganz normaler Text ohne Emoji.") == "Ganz normaler Text ohne Emoji."


def test_speak_strips_emoji_before_synthesis():
    """_speak() darf Emoji nie an die TTS weiterreichen."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"

    asst = VoiceAssistant(backend, player, speaker=speaker, volume=50)
    asst._speak("Klar, hier kommt dein Lied! 😄🎉")

    speaker.synth_to_wav.assert_called_once_with("Klar, hier kommt dein Lied!")


def test_speak_sentences_strips_emoji_before_synthesis():
    """Gleicher Emoji-Fix für den unterbrechbaren (Streaming-)Antwortpfad."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[])
    transcribe_fn = _fake_transcriber([])
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn, volume=50)
    asst._speak_sentences_interruptible(iter(["Toll gemacht! 🎉"]), {})

    speaker.synth_to_wav.assert_called_once_with("Toll gemacht!")


def test_speak_sentences_filters_self_echo():
    """Erkennt der Lauscher einen Text, der fast identisch mit dem bisher
    Gesprochenen ist, wird das NICHT als Barge-in gewertet (Rückkopplungs-
    schutz) — es wird weitergelauscht, bis echte Stille kommt."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    # Fenster 1: "Sprache" (das Selbst-Echo), danach nur noch Stille.
    # pre_delay stellt sicher, dass der Speaker-Thread den Satz VOR dem ersten
    # Fenster-Ergebnis gesprochen hat (sonst wäre spoken_parts noch leer und
    # der Echo-Vergleich hätte nichts zum Vergleichen).
    recorder = _FakeRecorder(speech_seen_sequence=[True], pre_delay=0.3)
    transcribe_fn = _fake_transcriber(["Davon kann ich dir nicht erzählen."])
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn, volume=50)
    outcome = asst._speak_sentences_interruptible(
        iter(["Davon kann ich dir nicht erzählen."]), {},
    )

    # Kein Barge-in — das Echo wurde ignoriert, danach beendete Stille den Turn.
    assert outcome["outcome"] == "silence"
    assert outcome["transcript"] is None


# --------------------------------------------------------------------------
# Harter Stopp — Blau-Knopf während KI-Modus bricht sofort ab
# --------------------------------------------------------------------------


def test_conversation_loop_hard_stop_via_cancel_event():
    """Blau-Knopf während des KI-Modus (cancel_event) bricht die laufende
    Aufnahme sofort ab und beendet die Konversation."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(cancelled_at=0)  # sofort beim ersten Call
    cancel_event = threading.Event()
    cancel_event.set()

    asst = VoiceAssistant(backend, player, recorder)
    result = asst.conversation_loop({}, [], cancel_event=cancel_event)

    assert result is None
    assert recorder.call_count == 1


def test_conversation_loop_hard_stop_during_speaking_stops_playback():
    """Harter Stopp WÄHREND die KI gerade antwortet (Barge-in-Lauschphase)
    bricht ebenfalls sofort ab und stoppt die laufende Wiedergabe."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"
    # [0] normale erste Aufnahme, [1] während der Antwort hart abgebrochen
    recorder = _FakeRecorder(speech_seen_sequence=[True], cancelled_at=1)
    transcribe_fn = _fake_transcriber(["Erzähl mir was"])
    backend._session.post.return_value = _legacy_response({"status": "ok", "intent": "story", "response": "Es war einmal...", "confidence": 0.9})

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker, transcribe_fn=transcribe_fn)
    result = asst.conversation_loop({}, [], cancel_event=threading.Event())

    assert result is None
    player.stop.assert_called_once()


def test_conversation_loop_speaks_response_via_tts():
    """Antwort wird per Speaker synthetisiert + über Player abgespielt."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True, False])
    transcribe_fn = _fake_transcriber(["Was ist ein Löwe?"])
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"
    backend._session.post.return_value = _legacy_response({
            "status": "ok", "intent": "answer",
            "response": "Ein Löwe ist ein großes Tier.", "confidence": 0.9,
        })

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
    backend._session.post.return_value = _legacy_response({"status": "ok", "intent": "answer", "response": "ok", "confidence": 0.9})
    box_config = {"zauberwort_mode_enabled": True, "quiet_hours": []}
    catalog = [{"content_id": 1, "title": "Testlied"}]

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    asst.conversation_loop(box_config, catalog)

    call_kwargs = backend._session.post.call_args[1]
    assert call_kwargs["json"]["box_config"] == box_config
    assert call_kwargs["json"]["catalog"] == catalog


def test_conversation_loop_omits_now_playing_when_nothing_plays():
    """Gleiche Regression wie test_assistant_omits_now_playing_when_nothing_plays,
    aber für ask()/conversation_loop() statt understand()."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True, False])
    transcribe_fn = _fake_transcriber(["Hallo"])
    backend._session.post.return_value = _legacy_response({"status": "ok", "intent": "answer", "response": "ok", "confidence": 0.9})

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    asst.conversation_loop({}, [])

    call_kwargs = backend._session.post.call_args[1]
    assert "now_playing" not in call_kwargs["json"]


def test_conversation_loop_safety_filter_blocks_response():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True, False])
    transcribe_fn = _fake_transcriber(["Erzähl mir etwas"])
    # Claude antwortet mit vulgarem Inhalt (hypothetisch — sollte nicht vorkommen,
    # aber Test deckt es ab)
    backend._session.post.return_value = _legacy_response({
            "status": "ok",
            "intent": "story",
            "response": "Das ist verdammt gruselig!",
            "confidence": 0.9,
        })

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    box_config = {}  # kein special mode
    result = asst.conversation_loop(box_config, [])

    # Nach safety-check sollte die Response ersetzt sein
    assert result is None  # Keine play_song Intent, daher None (Abbruch)


# --------------------------------------------------------------------------
# Streaming — Satz-Splitter, Meta-Parsing, _open_stream, End-to-End
# --------------------------------------------------------------------------


def test_iter_sentences_splits_on_sentence_boundaries():
    from voice.assistant import _iter_sentences
    chunks = iter(["Hallo! Wie geht es ", "dir? Mir geht es gut."])
    assert list(_iter_sentences(chunks)) == [
        "Hallo!", "Wie geht es dir?", "Mir geht es gut.",
    ]


def test_iter_sentences_does_not_split_abbreviations():
    """Nach "z.B." folgt ein Kleinbuchstabe → keine Satzgrenze."""
    from voice.assistant import _iter_sentences
    result = list(_iter_sentences(iter(["Tiere wie z.B. der Löwe sind stark."])))
    assert result == ["Tiere wie z.B. der Löwe sind stark."]


def test_iter_sentences_splits_on_newlines():
    from voice.assistant import _iter_sentences
    result = list(_iter_sentences(iter(["Zeile eins\nZeile zwei\n\nZeile drei"])))
    assert result == ["Zeile eins", "Zeile zwei", "Zeile drei"]


def test_iter_sentences_flushes_tail_without_terminator():
    from voice.assistant import _iter_sentences
    result = list(_iter_sentences(iter(["Erster Satz. Und dann ein Rest ohne Punkt"])))
    assert result == ["Erster Satz.", "Und dann ein Rest ohne Punkt"]


def test_iter_sentences_handles_tiny_chunks():
    """Token-für-Token-Stückelung (wie echte LLM-Deltas) ändert nichts am Ergebnis."""
    from voice.assistant import _iter_sentences
    text = "Kurz. Und knapp! Fertig?"
    chunks = iter(list(text))  # 1 Zeichen pro Chunk
    assert list(_iter_sentences(chunks)) == ["Kurz.", "Und knapp!", "Fertig?"]


def test_iter_sentences_bounds_very_long_sentence():
    """Ohne Satzende wird spätestens nach _SENTENCE_MAX_BUFFER am letzten
    Leerzeichen getrennt — die Wiedergabe wartet nie unbegrenzt."""
    from voice.assistant import _SENTENCE_MAX_BUFFER, _iter_sentences
    long_text = "wort " * 200  # 1000 Zeichen, kein Satzende
    parts = list(_iter_sentences(iter([long_text])))
    assert len(parts) >= 2
    assert all(len(p) <= _SENTENCE_MAX_BUFFER + 10 for p in parts)
    assert " ".join(parts).split() == long_text.split()  # nichts verloren


def test_split_meta_and_text_parses_meta_line():
    from voice.assistant import _split_meta_and_text
    meta, text = _split_meta_and_text(iter(
        ['{"intent":"answer","confidence":0.9}\nEin Löwe ', "ist stark."]
    ))
    assert meta == {"intent": "answer", "confidence": 0.9}
    assert "".join(text) == "Ein Löwe ist stark."


def test_split_meta_and_text_skips_markdown_fences():
    from voice.assistant import _split_meta_and_text
    meta, text = _split_meta_and_text(iter(
        ['```json\n{"intent":"joke"}\nWarum? Darum!']
    ))
    assert meta == {"intent": "joke"}
    assert "".join(text) == "Warum? Darum!"


def test_split_meta_and_text_without_meta_keeps_text():
    """Erste Zeile ist kein JSON → keine Meta, aber die Zeile geht NICHT verloren."""
    from voice.assistant import _split_meta_and_text
    meta, text = _split_meta_and_text(iter(["Hallo Kind!\nWie geht's?"]))
    assert meta is None
    assert "".join(text) == "Hallo Kind!\nWie geht's?"


def test_split_meta_and_text_meta_split_across_chunks():
    from voice.assistant import _split_meta_and_text
    meta, text = _split_meta_and_text(iter(
        ['{"intent":"pl', 'ay_song","action":{"song_id":5}}', "\nKlar!"]
    ))
    assert meta["intent"] == "play_song"
    assert meta["action"]["song_id"] == 5
    assert "".join(text) == "Klar!"


def test_open_stream_returns_stream_tuple():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    stream = _FakeStreamResponse('{"intent":"answer","confidence":0.9}\nHallo du!')
    backend._session.post.return_value = stream
    asst = VoiceAssistant(backend, MagicMock())

    kind, meta, text_chunks, response = asst._open_stream("Hallo", {}, [])

    assert kind == "stream"
    assert meta["intent"] == "answer"
    assert "".join(text_chunks) == "Hallo du!"
    assert response is stream
    # Request war ein Streaming-Request mit stream-Flag im Payload
    call_kwargs = backend._session.post.call_args[1]
    assert call_kwargs["stream"] is True
    assert call_kwargs["json"]["stream"] is True


def test_open_stream_detects_legacy_json_server():
    """Alter Server ohne Streaming antwortet mit application/json →
    Legacy-Pfad, kein Streaming-Parsing."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    payload = {"status": "ok", "intent": "answer", "response": "Hi!", "confidence": 0.9}
    backend._session.post.return_value = _legacy_response(payload)
    asst = VoiceAssistant(backend, MagicMock())

    kind, result, text_chunks, response = asst._open_stream("Hallo", {}, [])

    assert kind == "legacy"
    assert result == payload
    assert text_chunks is None
    assert response is None


def test_open_stream_returns_none_on_503():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    backend._session.post.return_value = _FakeStreamResponse("disabled", status=503)
    asst = VoiceAssistant(backend, MagicMock())

    assert asst._open_stream("Hallo", {}, []) is None


def test_open_stream_without_meta_treats_all_as_answer():
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    backend._session.post.return_value = _FakeStreamResponse("Einfach nur Text ohne Meta.")
    asst = VoiceAssistant(backend, MagicMock())

    kind, meta, text_chunks, _ = asst._open_stream("Hallo", {}, [])

    assert kind == "stream"
    assert meta["intent"] == "answer"
    assert "".join(text_chunks) == "Einfach nur Text ohne Meta."


def test_conversation_loop_streaming_speaks_sentences_and_ends_on_silence():
    """End-to-End Streaming: Meta answer + zwei Sätze → beide werden einzeln
    synthetisiert/gespielt, History enthält den vollen Text, Stille beendet."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"
    recorder = _FakeRecorder(speech_seen_sequence=[True])  # nur der initiale Turn
    transcribe_fn = _fake_transcriber(["Was ist ein Löwe?"])
    backend._session.post.return_value = _FakeStreamResponse(
        '{"intent":"answer","confidence":0.9}\n'
        "Ein Löwe ist ein großes Tier. Er hat eine Mähne!"
    )

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn, volume=42)
    result = asst.conversation_loop({}, [])

    assert result is None  # Stille nach der Antwort beendet die Konversation
    spoken = [c.args[0] for c in speaker.synth_to_wav.call_args_list]
    assert spoken == ["Ein Löwe ist ein großes Tier.", "Er hat eine Mähne!"]
    assert asst.history[-1]["response"] == "Ein Löwe ist ein großes Tier. Er hat eine Mähne!"


def test_conversation_loop_streaming_play_song_closes_stream():
    """Streaming play_song: Meta-Zeile reicht — Rest der Antwort wird
    verworfen (main.py übernimmt Bestätigung + Wiedergabe)."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    transcribe_fn = _fake_transcriber(["Spiele bitte 99 Luftballons"])
    stream = _FakeStreamResponse(
        '{"intent":"play_song","action":{"song_id":12,"song_title":"99 Luftballons"},"confidence":0.95}\n'
        "Klar, hier kommt 99 Luftballons!"
    )
    backend._session.post.return_value = stream

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    result = asst.conversation_loop({"zauberwort_mode_enabled": True}, [])

    assert result == {"intent": "play_song", "song_id": 12, "song_title": "99 Luftballons"}
    assert stream.closed is True


def test_conversation_loop_streaming_zauberwort_blocks_song_without_bitte():
    """Streaming + Zauberwort: play_song-Meta ohne 'bitte' im Transkript →
    Stream wird verworfen, stattdessen die Erinnerung gesprochen."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    transcribe_fn = _fake_transcriber(["Spiele 99 Luftballons"])  # KEIN bitte
    stream = _FakeStreamResponse(
        '{"intent":"play_song","action":{"song_id":12,"song_title":"99 Luftballons"},"confidence":0.95}\n'
        "Klar!"
    )
    backend._session.post.return_value = stream

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn)
    result = asst.conversation_loop({"zauberwort_mode_enabled": True}, [])

    assert result is None
    assert stream.closed is True
    spoken = [c.args[0] for c in speaker.synth_to_wav.call_args_list]
    assert any("bitte" in s for s in spoken)


def test_conversation_loop_streaming_barge_in_uses_next_transcript():
    """Barge-in während einer gestreamten Antwort → erkannter Text wird als
    nächster Turn verarbeitet (zweiter Request), Stream geschlossen."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    speaker = MagicMock()
    speaker.synth_to_wav.return_value = "/tmp/answer.wav"
    # [0] initialer Turn (Sprache), [1] Barge-in während der Antwort
    recorder = _FakeRecorder(speech_seen_sequence=[True, True])
    transcribe_fn = _fake_transcriber(["Erzähl mir was", "Spiel bitte Musik"])
    stream1 = _FakeStreamResponse(
        '{"intent":"story","confidence":0.9}\nEs war einmal ein langes Märchen.'
    )
    stream2 = _FakeStreamResponse(
        '{"intent":"play_song","action":{"song_id":7,"song_title":"Musik"},"confidence":0.9}\nOkay!'
    )
    backend._session.post.side_effect = [stream1, stream2]

    asst = VoiceAssistant(backend, player, recorder, speaker=speaker,
                           transcribe_fn=transcribe_fn)
    result = asst.conversation_loop({}, [])

    assert result == {"intent": "play_song", "song_id": 7, "song_title": "Musik"}
    assert stream1.closed is True
    assert backend._session.post.call_count == 2


# --------------------------------------------------------------------------
# Review-Fixes 2026-07-10 — Cancel-Fenster, Zauberwort-Vorturn, History-Guard
# --------------------------------------------------------------------------


def test_conversation_loop_hard_stop_during_think_window_discards_result():
    """Review-Finding: Blau-Druck WÄHREND die Box denkt (nach der Aufnahme,
    vor/während des Claude-Calls) wurde verschluckt — bei play_song startete
    trotz Stopp die Musik. Jetzt wird das Event nach der ASR geprüft."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    cancel_event = threading.Event()

    def transcribe_and_cancel(path):
        # Kind drückt Blau GENAU während der Transkription (Denk-Fenster).
        cancel_event.set()
        return "Spiele bitte 99 Luftballons"

    backend._session.post.return_value = _legacy_response({
        "status": "ok", "intent": "play_song", "response": "Klar!",
        "action": {"song_id": 12, "song_title": "99 Luftballons"}, "confidence": 0.95,
    })

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_and_cancel)
    result = asst.conversation_loop({}, [], cancel_event=cancel_event)

    assert result is None  # KEIN play_song-Ergebnis trotz Claude-Antwort


def test_conversation_loop_zauberwort_accepts_bitte_from_previous_turn():
    """Review-Finding: mehrstufiger Song-Wunsch — 'bitte' im Vorturn zählt.
    Turn 1: 'Kannst du bitte ein Lied spielen?' → Rückfrage. Turn 2: 'Das
    rote Pferd' (ohne bitte) → play_song darf NICHT geblockt werden."""
    from voice.assistant import VoiceAssistant
    backend = _FakeBackend()
    player = MagicMock()
    recorder = _FakeRecorder(speech_seen_sequence=[True])
    transcribe_fn = _fake_transcriber(["Das rote Pferd"])  # KEIN bitte in DIESEM Turn
    backend._session.post.return_value = _legacy_response({
        "status": "ok", "intent": "play_song", "response": "Hier kommt es!",
        "action": {"song_id": 7, "song_title": "Das rote Pferd"}, "confidence": 0.9,
    })

    asst = VoiceAssistant(backend, player, recorder, transcribe_fn=transcribe_fn)
    # Vorturn mit "bitte" in der History (wie nach einer Rückfrage der Box)
    asst._add_to_history("Kannst du bitte ein Lied spielen?", "Welches denn?")
    result = asst.conversation_loop({"zauberwort_mode_enabled": True}, [])

    assert result == {"intent": "play_song", "song_id": 7, "song_title": "Das rote Pferd"}


def test_self_echo_does_not_swallow_short_real_answers():
    """Review-Finding: 'Bitte' als echte Kind-Antwort war Substring der
    Zauberwort-Erinnerung und wurde als Selbst-Echo verworfen."""
    from voice.assistant import _looks_like_self_echo
    reminder = "Du musst noch 'bitte' sagen, wenn du ein Lied hören möchtest!"
    assert not _looks_like_self_echo(reminder, "Bitte")
    assert not _looks_like_self_echo(reminder, "Ja")
    # Echte lange Echo-Fragmente werden weiterhin erkannt:
    assert _looks_like_self_echo(reminder, "Du musst noch 'bitte' sagen")


def test_add_to_history_skips_empty_entries():
    """Review-Finding: leere assistant-Inhalte in der History lassen die
    Anthropic-API alle Folge-Turns mit 400 ablehnen — leere Turns werden
    deshalb gar nicht erst gespeichert."""
    backend = _FakeBackend()
    asst = VoiceAssistant(backend, MagicMock())

    asst._add_to_history("Hallo", "")       # leere Antwort (Barge-in vor Satz 1)
    asst._add_to_history("", "Antwort")     # leeres Transkript
    asst._add_to_history("  ", "  ")        # nur Whitespace
    assert asst.history == []

    asst._add_to_history("Hallo", "Hi!")
    assert len(asst.history) == 1


def test_open_stream_truncates_overlong_transcript():
    """Review-Finding: Server validiert transcript max:500 — ein langer
    60s-Redeturn führte zu 422 und Konversationsabbruch."""
    backend = _FakeBackend()
    backend._session.post.return_value = _legacy_response({
        "status": "ok", "intent": "answer", "response": "ok", "confidence": 0.9,
    })
    asst = VoiceAssistant(backend, MagicMock())

    asst._open_stream("wort " * 200, {}, [])  # 1000 Zeichen

    sent = backend._session.post.call_args[1]["json"]["transcript"]
    assert len(sent) <= 500
