"""Tests für die VAD-Fixes in voice/recorder.py (ASR-Plan 1.3/1.5a).

arecord wird durch einen Fake-Prozess ersetzt, der synthetische S16_LE-Chunks
liefert — so lässt sich das VAD-Verhalten deterministisch prüfen: DC-Abzug,
Einschalt-Transient, adaptive Schwellen, speech_seen.
"""
import math
import struct
import wave

import pytest

from voice import recorder as rec_mod
from voice.recorder import (
    LEGACY_SILENCE_RMS,
    LEGACY_SPEECH_RMS,
    MicRecorder,
    MIN_SILENCE_RMS,
    MIN_SPEECH_RMS,
    RecordingResult,
    adaptive_thresholds,
    chunk_rms,
)

SAMPLE_RATE = 16000
CHUNK_FRAMES = SAMPLE_RATE // 10  # 100 ms wie in der Implementierung


def _chunk(amplitude: float, dc_offset: float = 0.0, freq: float = 300.0) -> bytes:
    """100-ms-Chunk: Sinus mit Amplitude + DC-Offset (INMP441-typisch)."""
    samples = [
        int(max(-32768, min(32767,
            dc_offset + amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)
        )))
        for i in range(CHUNK_FRAMES)
    ]
    return struct.pack(f"<{CHUNK_FRAMES}h", *samples)


class _FakeProc:
    """Ersetzt das arecord-subprocess: liefert vorgegebene Chunks, dann EOF."""

    def __init__(self, chunks):
        self._data = b"".join(chunks)
        self._pos = 0
        self.stdout = self
        self.stderr = None

    def read(self, n):
        out = self._data[self._pos:self._pos + n]
        self._pos += n
        return out

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def _record(monkeypatch, tmp_path, chunks, **kwargs):
    monkeypatch.setattr(
        rec_mod.subprocess, "Popen", lambda *a, **k: _FakeProc(chunks)
    )
    r = MicRecorder()
    return r.record_until_silence(
        max_seconds=5.0, silence_seconds=0.3, initial_silence_seconds=1.0,
        output_path=tmp_path / "out.wav", **kwargs,
    )


# --- Pure Helfer ---------------------------------------------------------------

def test_chunk_rms_removes_dc_offset():
    # Reiner DC-Offset (kein Signal) → RMS ~0 statt ~5000.
    silent_with_dc = _chunk(amplitude=0, dc_offset=5000)
    assert chunk_rms(silent_with_dc, CHUNK_FRAMES) < 1.0


def test_adaptive_thresholds_scale_with_floor():
    speech, silence = adaptive_thresholds(20.0)
    assert speech == pytest.approx(80.0)   # floor×4
    assert silence == pytest.approx(40.0)  # floor×2
    assert silence < speech


def test_adaptive_thresholds_clamped_to_legacy_maximum():
    # Lauter Raum: nie tauber als die alten C920-Fixwerte.
    speech, silence = adaptive_thresholds(500.0)
    assert speech == LEGACY_SPEECH_RMS
    assert silence == LEGACY_SILENCE_RMS


def test_adaptive_thresholds_have_sane_minimum():
    speech, silence = adaptive_thresholds(0.0)
    assert speech == MIN_SPEECH_RMS
    assert silence == MIN_SILENCE_RMS


# --- Aufnahme-Verhalten ----------------------------------------------------------

def test_quiet_child_voice_is_detected(monkeypatch, tmp_path):
    """Der Kern des 1.3-Fixes: RMS ~120 lag früher UNTER der Speech-Schwelle
    (180) — leise Kinderstimmen wurden nie als Sprache erkannt. Mit adaptiven
    Schwellen (Floor ~10 → speech≥45) triggert sie jetzt."""
    chunks = (
        [_chunk(amplitude=14, dc_offset=200)] * 3      # Settle: leises Grundrauschen
        + [_chunk(amplitude=170, dc_offset=200)] * 5   # leise Stimme, RMS ~120
        + [_chunk(amplitude=5, dc_offset=200)] * 6     # Stille danach
    )
    result = _record(monkeypatch, tmp_path, chunks)
    assert isinstance(result, RecordingResult)
    assert result.speech_seen is True


def test_quiet_child_voice_was_missed_with_legacy_thresholds(monkeypatch, tmp_path):
    """Gegenprobe: dieselbe leise Stimme mit den alten Fixwerten → verpasst.
    (Dokumentiert, WARUM der Fix nötig war — schlägt der Test fehl, hat
    jemand die Legacy-Schwellen verändert und dieser Kontrast stimmt nicht
    mehr.)"""
    chunks = (
        [_chunk(amplitude=14, dc_offset=200)] * 3
        + [_chunk(amplitude=170, dc_offset=200)] * 5
        + [_chunk(amplitude=5, dc_offset=200)] * 6
    )
    result = _record(
        monkeypatch, tmp_path, chunks,
        speech_rms=LEGACY_SPEECH_RMS, silence_rms=LEGACY_SILENCE_RMS,
    )
    assert result.speech_seen is False


def test_startup_transient_does_not_count_as_speech(monkeypatch, tmp_path):
    """Der INMP441-Einschalt-Knall (Chunk 0, RMS >1000) setzte früher bei
    JEDER Aufnahme speech_seen — reines Rauschen ging als Sprache an die ASR.
    Die Settle-Phase nimmt ihn jetzt aus der Detection."""
    chunks = (
        [_chunk(amplitude=8000, dc_offset=3000)]      # Einschalt-Transient
        + [_chunk(amplitude=10, dc_offset=200)] * 12  # danach nur Stille
    )
    result = _record(monkeypatch, tmp_path, chunks)
    assert result.speech_seen is False


def test_immediate_speech_word_start_is_preserved(monkeypatch, tmp_path):
    """Wenn ein Kind SOFORT (in der Settle-Phase) lospricht, muss der
    Wortanfang erhalten bleiben — die Settle-Chunks werden zwar nicht für die
    Detection genutzt, aber IN den WAV-Puffer geschrieben. (Regression-Guard:
    ein früher Entwurf verwarf die Settle-Chunks ganz und schnitt 'spiele' zu
    'iele' ab.)"""
    chunks = (
        [_chunk(amplitude=400, dc_offset=100)] * 3     # Settle, aber schon Sprache!
        + [_chunk(amplitude=400, dc_offset=100)] * 3   # weiter Sprache
        + [_chunk(amplitude=3, dc_offset=100)] * 5     # Stille
    )
    result = _record(monkeypatch, tmp_path, chunks)
    with wave.open(str(result.path)) as w:
        written_s = w.getnframes() / w.getframerate()
    # Alle 6 Sprach-Chunks (0,6s) müssen in der Datei sein — inkl. der 3
    # Settle-Chunks mit dem Wortanfang.
    assert written_s >= 0.6 - 1e-6


def test_silence_only_recording_reports_no_speech(monkeypatch, tmp_path):
    chunks = [_chunk(amplitude=6, dc_offset=150)] * 14
    result = _record(monkeypatch, tmp_path, chunks)
    assert result.speech_seen is False
    assert result.duration_seconds > 0


def test_recording_stops_after_trailing_silence(monkeypatch, tmp_path):
    """Nach Sprache + genug Stille wird abgebrochen (nicht bis max_seconds
    weitergelesen)."""
    chunks = (
        [_chunk(amplitude=10, dc_offset=100)] * 3     # Settle
        + [_chunk(amplitude=400, dc_offset=100)] * 4  # Sprache
        + [_chunk(amplitude=4, dc_offset=100)] * 30   # lange Stille
    )
    result = _record(monkeypatch, tmp_path, chunks)
    assert result.speech_seen is True
    # 4 Sprach-Chunks + 3 Stille-Chunks (silence_seconds=0.3) ≈ 0,7s — weit
    # unter den verfügbaren 3,7s.
    assert result.duration_seconds <= 1.0
