"""Vosk-basierter Offline-ASR-Wrapper.

Vosk wurde gewählt weil:
  - vollständig offline, embedded-tauglich (~50 MB Klein-Modell)
  - schnell auf Pi 5 (~1–2 s für 3-Sekunden-Utterances)
  - Grammar-Modus: bekannte Phrasen-Menge einschränkbar — Genauigkeit und
    Speed steigen deutlich, weil der Decoder nicht das ganze deutsche
    Sprachmodell durchforstet.

Vosk ist optionale Dependency. Fehlt das Paket oder das Modell, wirft die
Klasse ``VoiceUnavailable``. Der Rest der Box läuft davon unabhängig.
"""
from __future__ import annotations

import json
import logging
import wave
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("kakabox.voice")

DEFAULT_MODEL_DIR = Path("/usr/share/kakabox/voice/vosk-model-small-de-0.15")
DEFAULT_SAMPLE_RATE = 16000

try:
    from vosk import KaldiRecognizer, Model  # type: ignore
    _VOSK_AVAILABLE = True
except ImportError:
    _VOSK_AVAILABLE = False


class VoiceUnavailable(RuntimeError):
    """ASR kann nicht laufen — Modell fehlt, Paket fehlt, oder WAV-Format falsch."""


class Recognizer:
    """Lazy-loading Vosk-Wrapper. Modell lädt erst beim ersten ``transcribe``."""

    def __init__(
        self,
        model_dir: Path = DEFAULT_MODEL_DIR,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self._model_dir = Path(model_dir)
        self._sample_rate = sample_rate
        self._model = None  # type: ignore

    def _build_recognizer(self, grammar: Sequence[str] | None):
        if not _VOSK_AVAILABLE:
            raise VoiceUnavailable(
                "vosk-Paket fehlt. Installation: "
                ".venv/bin/pip install -r device/requirements-voice.txt"
            )
        if not self._model_dir.is_dir():
            raise VoiceUnavailable(
                f"Vosk-Modell nicht gefunden unter {self._model_dir}. "
                "Siehe device/voice/README.md für Download-Anleitung."
            )
        if self._model is None:
            logger.info("Lade Vosk-Modell aus %s …", self._model_dir)
            self._model = Model(str(self._model_dir))

        if grammar:
            # Vosk braucht JSON-encodede Liste. "[unk]" als Fallback für
            # Wörter, die NICHT im Catalog sind — sonst erfindet der Decoder
            # gerne mal Phrasen aus dem Catalog, auch wenn der User was
            # völlig anderes sagt.
            payload = json.dumps(list(grammar) + ["[unk]"], ensure_ascii=False)
            return KaldiRecognizer(self._model, self._sample_rate, payload)
        return KaldiRecognizer(self._model, self._sample_rate)

    def transcribe_wav(
        self,
        wav_path: Path,
        grammar: Sequence[str] | None = None,
    ) -> str:
        """Transkribiert eine WAV-Datei. Erwartet 16 kHz mono 16-bit PCM."""
        rec = self._build_recognizer(grammar)
        with wave.open(str(wav_path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise VoiceUnavailable(
                    f"WAV muss mono 16-bit sein: {wav_path} hat "
                    f"{wf.getnchannels()} Kanäle / {wf.getsampwidth() * 8}-bit."
                )
            if wf.getframerate() != self._sample_rate:
                raise VoiceUnavailable(
                    f"WAV muss {self._sample_rate} Hz sein: {wav_path} hat "
                    f"{wf.getframerate()} Hz."
                )
            chunks: list[str] = []
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    chunks.append(json.loads(rec.Result()).get("text", ""))
            chunks.append(json.loads(rec.FinalResult()).get("text", ""))
        return " ".join(c for c in chunks if c).strip()
