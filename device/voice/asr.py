"""Offline-ASR-Wrapper mit umschaltbarem Backend.

Zwei Backends hinter derselben API:

- **Vosk** (``backend="vosk"``): Kaldi-basiert, ~50 MB Klein-Modell, ~1–2 s
  auf Pi 5. Schnell, aber bei Kinderstimmen + Eigennamen oft schwach.
- **Whisper** (``backend="whisper"``): whisper.cpp via ``pywhispercpp``,
  ~140 MB für ``ggml-base.bin``. Deutlich robuster bei Nuscheln/Akzent,
  Latenz auf Pi 5 ~2–3 s. Multilingual, ``language="de"`` setzen wir explizit.

Beide laden lazy: Modell zieht erst beim ersten ``transcribe_wav`` in den RAM.
Fehlt das Paket oder das Modell, fliegt ``VoiceUnavailable`` — die Box läuft
unabhängig davon weiter.

Auswahl via ``Recognizer(backend="whisper")`` oder ``build_recognizer(config)``.
"""
from __future__ import annotations

import json
import logging
import wave
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("kakabox.voice")

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_VOSK_MODEL_DIR = Path("/usr/share/kakabox/voice/vosk-model-small-de-0.15")
DEFAULT_WHISPER_MODEL = Path("/usr/share/kakabox/voice/ggml-base.bin")


class VoiceUnavailable(RuntimeError):
    """ASR kann nicht laufen — Modell fehlt, Paket fehlt, oder WAV-Format falsch."""


def _check_wav_format(wav_path: Path, expected_rate: int) -> None:
    with wave.open(str(wav_path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise VoiceUnavailable(
                f"WAV muss mono 16-bit sein: {wav_path} hat "
                f"{wf.getnchannels()} Kanäle / {wf.getsampwidth() * 8}-bit."
            )
        if wf.getframerate() != expected_rate:
            raise VoiceUnavailable(
                f"WAV muss {expected_rate} Hz sein: {wav_path} hat "
                f"{wf.getframerate()} Hz."
            )


class VoskRecognizer:
    """Lazy-loading Vosk-Backend. Modell lädt erst beim ersten ``transcribe_wav``."""

    def __init__(
        self,
        model_dir: Path = DEFAULT_VOSK_MODEL_DIR,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self._model_dir = Path(model_dir)
        self._sample_rate = sample_rate
        self._model = None  # type: ignore

    def warmup(self) -> None:
        """Lädt das Vosk-Modell in den RAM ohne KaldiRecognizer zu bauen.

        Idempotent — zweimal aufzurufen ist billig (no-op nach erstem Load).
        Wirft VoiceUnavailable wie ``transcribe_wav``, wenn Paket oder Modell
        fehlen — der Aufrufer muss das fangen.
        """
        try:
            from vosk import Model  # type: ignore
        except ImportError as e:
            raise VoiceUnavailable(
                "vosk-Paket fehlt. Installation: "
                ".venv/bin/pip install -r device/requirements-voice.txt"
            ) from e

        if not self._model_dir.is_dir():
            raise VoiceUnavailable(
                f"Vosk-Modell nicht gefunden unter {self._model_dir}. "
                "Siehe device/voice/README.md für Download-Anleitung."
            )
        if self._model is None:
            logger.info("Lade Vosk-Modell aus %s …", self._model_dir)
            self._model = Model(str(self._model_dir))

    def _build_kaldi_recognizer(self, grammar: Sequence[str] | None):
        try:
            from vosk import KaldiRecognizer  # type: ignore
        except ImportError as e:
            raise VoiceUnavailable(
                "vosk-Paket fehlt. Installation: "
                ".venv/bin/pip install -r device/requirements-voice.txt"
            ) from e

        self.warmup()

        if grammar:
            # "[unk]" als Fallback für Wörter außerhalb des Catalogs — sonst
            # erfindet der Decoder gerne Catalog-Phrasen, auch wenn der User
            # was völlig anderes sagt.
            payload = json.dumps(list(grammar) + ["[unk]"], ensure_ascii=False)
            return KaldiRecognizer(self._model, self._sample_rate, payload)
        return KaldiRecognizer(self._model, self._sample_rate)

    def transcribe_wav(
        self,
        wav_path: Path,
        grammar: Sequence[str] | None = None,
    ) -> str:
        rec = self._build_kaldi_recognizer(grammar)
        _check_wav_format(Path(wav_path), self._sample_rate)
        with wave.open(str(wav_path), "rb") as wf:
            chunks: list[str] = []
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    chunks.append(json.loads(rec.Result()).get("text", ""))
            chunks.append(json.loads(rec.FinalResult()).get("text", ""))
        return " ".join(c for c in chunks if c).strip()


class WhisperRecognizer:
    """Lazy-loading whisper.cpp-Backend via ``pywhispercpp``.

    Grammar wird zu einem ``initial_prompt`` — biased den Decoder Richtung
    Catalog-Namen, ohne hartes Vokabular zu erzwingen (Whisper-Grammar ist
    experimentell und fummelig). Bei zu langem Catalog wird der Prompt
    abgeschnitten (Whisper-Token-Limit für initial_prompt: ~224 Tokens).
    """

    _INITIAL_PROMPT_MAX_CHARS = 600  # grobe Daumenregel für ~200 Tokens DE

    def __init__(
        self,
        model_path: Path = DEFAULT_WHISPER_MODEL,
        language: str = "de",
        n_threads: int = 4,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self._model_path = Path(model_path)
        self._language = language
        self._n_threads = n_threads
        self._sample_rate = sample_rate
        self._model = None  # type: ignore

    def warmup(self) -> None:
        """Lädt das Whisper-Modell in den RAM. Idempotent."""
        self._load_model()

    def _load_model(self):
        try:
            from pywhispercpp.model import Model  # type: ignore
        except ImportError as e:
            raise VoiceUnavailable(
                "pywhispercpp-Paket fehlt. Installation: "
                ".venv/bin/pip install -r device/requirements-voice.txt"
            ) from e

        if not self._model_path.is_file():
            raise VoiceUnavailable(
                f"Whisper-Modell nicht gefunden unter {self._model_path}. "
                "Siehe device/voice/README.md für Download-Anleitung."
            )
        if self._model is None:
            logger.info("Lade Whisper-Modell aus %s …", self._model_path)
            self._model = Model(
                str(self._model_path),
                n_threads=self._n_threads,
                print_realtime=False,
                print_progress=False,
            )
        return self._model

    def _grammar_to_prompt(self, grammar: Sequence[str]) -> str:
        joined = ", ".join(grammar)
        if len(joined) > self._INITIAL_PROMPT_MAX_CHARS:
            joined = joined[: self._INITIAL_PROMPT_MAX_CHARS].rsplit(",", 1)[0]
        return f"Mögliche Titel: {joined}."

    def transcribe_wav(
        self,
        wav_path: Path,
        grammar: Sequence[str] | None = None,
    ) -> str:
        model = self._load_model()
        _check_wav_format(Path(wav_path), self._sample_rate)
        kwargs = {"language": self._language, "translate": False}
        if grammar:
            kwargs["initial_prompt"] = self._grammar_to_prompt(grammar)
        segments = model.transcribe(str(wav_path), **kwargs)
        return " ".join(seg.text.strip() for seg in segments if seg.text).strip()


class Recognizer:
    """Dispatcher — wählt das Backend bei Konstruktion.

    ``Recognizer()`` ohne Argumente bleibt rückwärtskompatibel und nutzt Vosk.
    Für Whisper: ``Recognizer(backend="whisper")`` oder ``build_recognizer(...)``.
    """

    def __init__(self, backend: str = "vosk", **kwargs) -> None:
        if backend == "vosk":
            self._impl: VoskRecognizer | WhisperRecognizer = VoskRecognizer(**kwargs)
        elif backend == "whisper":
            self._impl = WhisperRecognizer(**kwargs)
        else:
            raise VoiceUnavailable(
                f"Unbekanntes ASR-Backend '{backend}'. Erlaubt: 'vosk', 'whisper'."
            )
        self.backend = backend

    def transcribe_wav(
        self,
        wav_path: Path,
        grammar: Sequence[str] | None = None,
    ) -> str:
        return self._impl.transcribe_wav(wav_path, grammar=grammar)

    def warmup(self) -> None:
        """Lädt das Modell vorab in den RAM (typisch 1–3 s je nach Backend).

        Nützlich beim Service-Start in einem Daemon-Thread: die erste Push-to-
        Talk-Session muss dann nicht mehr auf den Modell-Load warten. Wirft
        ``VoiceUnavailable`` wenn Paket/Modell fehlen.
        """
        self._impl.warmup()


def build_recognizer(voice_config: dict | None) -> Recognizer:
    """Baut einen Recognizer aus dem ``voice``-Block der config.json.

    Beispiel-Config::

        "voice": {
          "backend": "whisper",
          "whisper": { "model_path": "/usr/share/kakabox/voice/ggml-base.bin",
                       "language": "de", "n_threads": 4 },
          "vosk":    { "model_dir":  "/usr/share/kakabox/voice/vosk-model-small-de-0.15" }
        }

    Ohne Voice-Block → Default-Vosk (rückwärtskompatibel).
    """
    cfg = voice_config or {}
    backend = cfg.get("backend", "vosk")
    backend_cfg = dict(cfg.get(backend, {}))
    # Path-Strings aus JSON in Path-Objekte konvertieren, falls vorhanden.
    for key in ("model_dir", "model_path"):
        if key in backend_cfg:
            backend_cfg[key] = Path(backend_cfg[key])
    return Recognizer(backend=backend, **backend_cfg)
