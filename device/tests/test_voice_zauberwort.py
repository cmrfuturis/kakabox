"""Tests für den Zauberwort-Re-Listen-Flow (Z3/Z4): _await_zauberwort.

Die volle _run_voice_activation ist I/O-lastig (Mic/ASR/Player/LEDs); hier
testen wir die isolierte, riskante Entscheidungslogik der zweiten Aufnahme.
"""
import types

import main


def _bare_box():
    return object.__new__(main.Kakabox)


class _FakeRecorder:
    def __init__(self, *, raises=None):
        self._raises = raises
        self.kwargs = None

    def record_until_silence(self, **kwargs):
        self.kwargs = kwargs
        if self._raises:
            raise self._raises
        return "/tmp/zauberwort.wav"


def _wire(box, transcript=None, recorder=None):
    box.leds = None
    box._mic_recorder = recorder or _FakeRecorder()
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
