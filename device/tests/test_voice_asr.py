"""Tests fuer voice.asr — Dispatcher- und Config-Mapping-Logik.

Die eigentliche Transkription (Vosk/Whisper) braucht Modelle + Hardware und
wird hier NICHT getestet. Stattdessen verifizieren wir:
  - Recognizer(backend=...) dispatched auf die richtige Backend-Klasse
  - build_recognizer(voice_config) liest backend + nested config korrekt
  - Pfad-Strings aus JSON werden zu Path-Objekten konvertiert
  - Whisper-Grammar-Prompt wird korrekt gebaut und bei Bedarf gekuerzt
  - WAV-Format-Check liefert klare Fehler bei falschem Format
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from voice.asr import (
    DEFAULT_VOSK_MODEL_DIR,
    DEFAULT_WHISPER_MODEL,
    Recognizer,
    VoiceUnavailable,
    VoskRecognizer,
    WhisperRecognizer,
    _check_wav_format,
    build_recognizer,
)


# -----------------------------------------------------------------------------
# Dispatcher
# -----------------------------------------------------------------------------

def test_recognizer_defaults_to_vosk():
    rec = Recognizer()
    assert rec.backend == "vosk"
    assert isinstance(rec._impl, VoskRecognizer)


def test_recognizer_whisper_backend():
    rec = Recognizer(backend="whisper")
    assert rec.backend == "whisper"
    assert isinstance(rec._impl, WhisperRecognizer)


def test_recognizer_unknown_backend_raises():
    with pytest.raises(VoiceUnavailable, match="Unbekanntes ASR-Backend"):
        Recognizer(backend="kaldi-pro-max")


def test_recognizer_passes_kwargs_to_vosk(tmp_path):
    rec = Recognizer(backend="vosk", model_dir=tmp_path / "vosk-model")
    assert rec._impl._model_dir == tmp_path / "vosk-model"


def test_recognizer_passes_kwargs_to_whisper(tmp_path):
    rec = Recognizer(
        backend="whisper",
        model_path=tmp_path / "ggml-tiny.bin",
        language="en",
        n_threads=2,
    )
    assert rec._impl._model_path == tmp_path / "ggml-tiny.bin"
    assert rec._impl._language == "en"
    assert rec._impl._n_threads == 2


# -----------------------------------------------------------------------------
# build_recognizer (config -> Recognizer)
# -----------------------------------------------------------------------------

def test_build_recognizer_no_config_defaults_to_vosk():
    rec = build_recognizer(None)
    assert rec.backend == "vosk"
    assert rec._impl._model_dir == DEFAULT_VOSK_MODEL_DIR


def test_build_recognizer_empty_config_defaults_to_vosk():
    rec = build_recognizer({})
    assert rec.backend == "vosk"


def test_build_recognizer_picks_whisper():
    rec = build_recognizer({"backend": "whisper"})
    assert rec.backend == "whisper"
    # Wenn keine whisper.model_path angegeben → Default
    assert rec._impl._model_path == DEFAULT_WHISPER_MODEL


def test_build_recognizer_converts_string_paths_to_path_objects():
    rec = build_recognizer({
        "backend": "whisper",
        "whisper": {
            "model_path": "/custom/path/ggml-base.bin",
            "language": "de",
            "n_threads": 8,
        },
    })
    assert rec._impl._model_path == Path("/custom/path/ggml-base.bin")
    assert isinstance(rec._impl._model_path, Path)
    assert rec._impl._language == "de"
    assert rec._impl._n_threads == 8


def test_build_recognizer_vosk_with_custom_dir():
    rec = build_recognizer({
        "backend": "vosk",
        "vosk": {"model_dir": "/opt/vosk/de-large"},
    })
    assert rec.backend == "vosk"
    assert rec._impl._model_dir == Path("/opt/vosk/de-large")


def test_build_recognizer_ignores_other_backend_block():
    # backend="vosk" → "whisper"-Block darf nicht in Vosk-Konstruktor leaken
    rec = build_recognizer({
        "backend": "vosk",
        "vosk": {"model_dir": "/opt/vosk"},
        "whisper": {"model_path": "/wrong/place.bin"},
    })
    assert rec.backend == "vosk"
    assert rec._impl._model_dir == Path("/opt/vosk")


# -----------------------------------------------------------------------------
# WhisperRecognizer._grammar_to_prompt
# -----------------------------------------------------------------------------

def test_whisper_grammar_prompt_format():
    rec = WhisperRecognizer()
    prompt = rec._grammar_to_prompt(["Bambi", "Bibi Blocksberg", "Dschungelbuch"])
    assert prompt.startswith("Mögliche Titel: ")
    assert "Bambi" in prompt
    assert "Bibi Blocksberg" in prompt
    assert prompt.endswith(".")


def test_whisper_grammar_prompt_truncates_at_word_boundary():
    rec = WhisperRecognizer()
    # 200 Titel à ~10 Zeichen → ueber dem 600-Zeichen-Limit
    big = [f"Title{i:04d}xxx" for i in range(200)]
    prompt = rec._grammar_to_prompt(big)
    # Truncation bei letztem Komma → kein abgeschnittener Title-Name am Ende
    body = prompt.removeprefix("Mögliche Titel: ").removesuffix(".")
    assert len(body) <= rec._INITIAL_PROMPT_MAX_CHARS
    titles = [t.strip() for t in body.split(",")]
    for t in titles:
        # Jeder Title muss ein voller "Titel####xxx" sein, kein angeschnittener
        assert t.startswith("Title") and t.endswith("xxx"), t


# -----------------------------------------------------------------------------
# _check_wav_format
# -----------------------------------------------------------------------------

def _write_wav(path: Path, *, channels: int, sampwidth: int, framerate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(b"\x00" * 100)


def test_check_wav_format_accepts_mono_16bit_16k(tmp_path):
    wav = tmp_path / "ok.wav"
    _write_wav(wav, channels=1, sampwidth=2, framerate=16000)
    _check_wav_format(wav, expected_rate=16000)  # darf nicht werfen


def test_check_wav_format_rejects_stereo(tmp_path):
    wav = tmp_path / "stereo.wav"
    _write_wav(wav, channels=2, sampwidth=2, framerate=16000)
    with pytest.raises(VoiceUnavailable, match="mono"):
        _check_wav_format(wav, expected_rate=16000)


def test_check_wav_format_rejects_wrong_samplerate(tmp_path):
    wav = tmp_path / "44k.wav"
    _write_wav(wav, channels=1, sampwidth=2, framerate=44100)
    with pytest.raises(VoiceUnavailable, match="16000 Hz"):
        _check_wav_format(wav, expected_rate=16000)


def test_check_wav_format_rejects_8bit(tmp_path):
    wav = tmp_path / "8bit.wav"
    _write_wav(wav, channels=1, sampwidth=1, framerate=16000)
    with pytest.raises(VoiceUnavailable, match="16-bit"):
        _check_wav_format(wav, expected_rate=16000)


# -----------------------------------------------------------------------------
# warmup() — Dispatcher delegiert, Backends werfen ohne Modell
# -----------------------------------------------------------------------------

def test_dispatcher_warmup_delegates_to_impl():
    rec = Recognizer(backend="whisper")
    called = {"n": 0}
    rec._impl.warmup = lambda: called.__setitem__("n", called["n"] + 1)
    rec.warmup()
    assert called["n"] == 1


def test_vosk_warmup_raises_when_model_dir_missing(tmp_path):
    rec = VoskRecognizer(model_dir=tmp_path / "does-not-exist")
    with pytest.raises(VoiceUnavailable, match="Vosk-Modell nicht gefunden"):
        rec.warmup()


def test_whisper_warmup_raises_voice_unavailable_when_unconfigured(tmp_path):
    # Wenn pywhispercpp installiert ist, kriegen wir "Modell nicht gefunden";
    # ohne Paket "Paket fehlt". Beide sind valide VoiceUnavailable-Pfade —
    # für den Warmup-Caller (siehe main._warmup_recognizer) zählt nur, dass
    # die Exception sauber als VoiceUnavailable rauskommt und nicht z.B.
    # als FileNotFoundError oder ImportError den Daemon-Thread crashed.
    rec = WhisperRecognizer(model_path=tmp_path / "ggml-nope.bin")
    with pytest.raises(VoiceUnavailable):
        rec.warmup()
