"""Tests für den Zauberwort-Re-Listen-Flow (Z3/Z4): _await_zauberwort.

Die volle _run_voice_activation ist I/O-lastig (Mic/ASR/Player/LEDs); hier
testen wir die isolierte, riskante Entscheidungslogik der zweiten Aufnahme.
"""
import types
from pathlib import Path

import main
from voice.recorder import RecordingResult


def _bare_box():
    return object.__new__(main.Kakabox)


class _FakeRecorder:
    def __init__(self, *, raises=None, speech_seen=True):
        self._raises = raises
        self._speech_seen = speech_seen
        self.kwargs = None

    def record_until_silence(self, **kwargs):
        self.kwargs = kwargs
        if self._raises:
            raise self._raises
        return RecordingResult(
            path=Path("/tmp/zauberwort.wav"),
            speech_seen=self._speech_seen,
            duration_seconds=1.0,
        )


def _wire(box, transcript=None, recorder=None):
    box.leds = None
    box._mic_recorder = recorder or _FakeRecorder()
    # Primärer Zauberwort-Pfad: schneller Vosk-Keyword-Erkenner (nimmt grammar).
    box._magic_word_recognizer = types.SimpleNamespace(
        transcribe_wav=lambda w, grammar=None: transcript
    )
    # Whisper-Fallback (ohne grammar) — greift nur, wenn _magic_word_recognizer None ist.
    box._recognizer = types.SimpleNamespace(transcribe_wav=lambda w: transcript)
    box._play_prompt = lambda *a, **k: None
    box.player = types.SimpleNamespace(wait_until_idle=lambda **k: None)
    return box


def test_await_zauberwort_true_when_bitte_said():
    box = _wire(_bare_box(), transcript="ja bitte")
    assert box._await_zauberwort() is True


def test_await_zauberwort_true_bitte_anywhere():
    # "bitte" am Ende muss auch zählen (positionsunabhängig).
    box = _wire(_bare_box(), transcript="mach das bitte")
    assert box._await_zauberwort() is True


def test_await_zauberwort_false_without_bitte():
    box = _wire(_bare_box(), transcript="nein danke")
    assert box._await_zauberwort() is False


def test_await_zauberwort_uses_short_followup_silence():
    # Z4: Follow-up-Aufnahme nutzt die kurze Nachlauf-Stille (sofort-Start
    # nach "bitte") und behält das 7s-Cap / 3s-Initial-Silence.
    rec = _FakeRecorder()
    box = _wire(_bare_box(), transcript="bitte", recorder=rec)
    box._await_zauberwort()
    assert rec.kwargs["silence_seconds"] == main.VOICE_ZAUBERWORT_SILENCE_SECONDS
    assert rec.kwargs["max_seconds"] == main.VOICE_MAX_SECONDS
    assert rec.kwargs["initial_silence_seconds"] == main.VOICE_INITIAL_SILENCE_SECONDS


def test_await_zauberwort_false_on_recorder_error():
    rec = _FakeRecorder(raises=main.RecorderError("kein Mic"))
    box = _wire(_bare_box(), transcript="bitte", recorder=rec)
    assert box._await_zauberwort() is False


def test_await_zauberwort_fallback_to_whisper_without_vosk():
    # Ohne Vosk-Keyword-Erkenner fällt die Prüfung auf den Haupt-Recognizer
    # (Whisper) zurück — das Gate funktioniert auch ohne Vosk.
    box = _wire(_bare_box(), transcript="ja bitte")
    box._magic_word_recognizer = None
    assert box._await_zauberwort() is True


def test_await_zauberwort_false_without_speech():
    # VAD sah keine Sprache → gar nicht erst transkribieren (ASR-Plan 1.5a):
    # Whisper halluziniert auf Stille sonst gern Text, der "bitte" enthalten
    # könnte. Der Recognizer darf hier NIE aufgerufen werden.
    def _boom(*a, **k):
        raise AssertionError("ASR darf bei speech_seen=False nicht laufen")

    box = _wire(_bare_box(), recorder=_FakeRecorder(speech_seen=False))
    box._magic_word_recognizer = types.SimpleNamespace(transcribe_wav=_boom)
    box._recognizer = types.SimpleNamespace(transcribe_wav=_boom)
    assert box._await_zauberwort() is False


def test_asr_returns_raw_text_when_all_segments_low_confidence():
    # Regression (Review 2026-07-07): der Konfidenz-Filter darf ein leise/
    # undeutlich gesprochenes (aber real vorhandenes) Transkript nicht komplett
    # leeren — sonst wird genau die Kinderstimme stummgeschaltet, die der Umbau
    # erfassen soll. Fallback: Rohtext, wenn ALLE Segmente rausgefiltert würden.
    import types as _t
    from voice.asr import WhisperRecognizer

    r = WhisperRecognizer.__new__(WhisperRecognizer)
    r._min_segment_probability = 0.4
    r._single_segment = True
    r._dynamic_audio_ctx = False
    r._beam_size = 5
    r._sample_rate = 16000
    r._AUDIO_CTX_MAX = 1500
    r._language = "de"
    seg = _t.SimpleNamespace(text="spiele superkind", probability=0.25)  # unter 0.4
    r._model = _t.SimpleNamespace(transcribe=lambda *a, **k: [seg])
    r._load_model = lambda: r._model
    r._audio_ctx_for = lambda p: 256
    # _check_wav_format umgehen (keine echte WAV):
    import voice.asr as _asr
    _orig = _asr._check_wav_format
    _asr._check_wav_format = lambda *a, **k: None
    try:
        out = r.transcribe_wav("/tmp/dummy.wav")
    finally:
        _asr._check_wav_format = _orig
    assert out == "spiele superkind"  # Rohtext-Fallback, nicht ""
