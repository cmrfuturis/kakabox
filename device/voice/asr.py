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
import threading
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
        # Schützt den Lazy-Load: Boot-Warmup-Thread und erster Push-to-Talk
        # könnten sonst beide gleichzeitig ein volles Modell laden (siehe
        # tts.py TitleSpeaker._load_lock für dasselbe Muster).
        self._load_lock = threading.Lock()

    def warmup(self) -> None:
        """Lädt das Vosk-Modell in den RAM ohne KaldiRecognizer zu bauen.

        Idempotent — zweimal aufzurufen ist billig (no-op nach erstem Load).
        Wirft VoiceUnavailable wie ``transcribe_wav``, wenn Paket oder Modell
        fehlen — der Aufrufer muss das fangen.
        """
        # Double-checked: erst lockfrei (häufiger Fall: Modell schon geladen).
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
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

    Decoding-Tuning (ASR-Plan 2026-07-07, Stufe 1.1): whisper.cpp rechnet
    per Default IMMER das volle 30-s-Encoder-Fenster — auch für ein 2-s-
    Kommando. ``dynamic_audio_ctx`` begrenzt den Encoder-Kontext auf die
    tatsächliche Audiodauer (~50 Frames/s + Puffer): live gemessen 6,7×
    schneller (tiny, 3,1-s-Kommando: 5,2 s → 0,78 s). Der Gewinn macht
    Beam-Search (``beam_size``, robuster bei schwierigen Sprechern —
    Kinderstimmen!) überhaupt erst bezahlbar. ``single_segment`` verhindert
    Segment-Splitting bei kurzen Kommandos.

    Verifizierte pywhispercpp-1.4.1-Gotchas:
    - ``beam_search`` wirkt NUR mit ``params_sampling_strategy=1`` im
      Konstruktor (sonst stillschweigend ignoriert, Output = greedy).
    - Das beam_search-Dict muss vollständig sein (``patience`` mitgeben).
    - Transcribe-Parameter persistieren zwischen Aufrufen auf dem geteilten
      Params-Struct → ``audio_ctx`` muss bei JEDEM Aufruf gesetzt werden.
    - ``suppress_non_speech_tokens`` fehlt im gebündelten Binding
      (AttributeError) — nicht setzen.
    """

    _INITIAL_PROMPT_MAX_CHARS = 600  # grobe Daumenregel für ~200 Tokens DE
    # Whisper-Encoder: 1500 Frames = 30 s → 50 Frames/s. +32 Puffer fürs
    # Fenster-Ende; min 256 (unterhalb wird der Decoder instabil, empirisch
    # aus der whisper.cpp-Community), max 1500 (volles Fenster).
    _AUDIO_CTX_FRAMES_PER_S = 50
    _AUDIO_CTX_MARGIN = 32
    _AUDIO_CTX_MIN = 256
    _AUDIO_CTX_MAX = 1500

    def __init__(
        self,
        model_path: Path = DEFAULT_WHISPER_MODEL,
        language: str = "de",
        n_threads: int = 4,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        beam_size: int = 5,
        dynamic_audio_ctx: bool = True,
        single_segment: bool = True,
        min_segment_probability: float = 0.4,
    ) -> None:
        self._model_path = Path(model_path)
        self._language = language
        self._n_threads = n_threads
        self._sample_rate = sample_rate
        # beam_size=0 → Greedy wie früher (Kill-Switch via config.json:
        # voice.whisper.beam_size). dynamic_audio_ctx=False → volles Fenster.
        self._beam_size = int(beam_size)
        self._dynamic_audio_ctx = bool(dynamic_audio_ctx)
        self._single_segment = bool(single_segment)
        # Segmente unterhalb dieser Konfidenz (geometrisches Mittel der
        # Token-Wahrscheinlichkeiten) werden verworfen — Whisper halluziniert
        # auf Rauschen/Stille gern plausible Sätze mit niedriger Konfidenz.
        # 0.0 = Filter aus.
        self._min_segment_probability = float(min_segment_probability)
        self._model = None  # type: ignore
        # Schützt den Lazy-Load: Boot-Warmup-Thread und erster Push-to-Talk
        # könnten sonst beide gleichzeitig ein volles Modell laden (siehe
        # tts.py TitleSpeaker._load_lock für dasselbe Muster).
        self._load_lock = threading.Lock()

    def _audio_ctx_for(self, wav_path: Path) -> int:
        """Encoder-Kontext passend zur tatsächlichen WAV-Dauer."""
        try:
            with wave.open(str(wav_path), "rb") as wf:
                duration = wf.getnframes() / float(wf.getframerate() or 1)
        except Exception:
            return self._AUDIO_CTX_MAX  # im Zweifel volles Fenster
        frames = int(duration * self._AUDIO_CTX_FRAMES_PER_S) + self._AUDIO_CTX_MARGIN
        return max(self._AUDIO_CTX_MIN, min(frames, self._AUDIO_CTX_MAX))

    def warmup(self) -> None:
        """Lädt das Whisper-Modell in den RAM. Idempotent."""
        self._load_model()

    def _load_model(self):
        # Double-checked: erst lockfrei (häufiger Fall: Modell schon geladen).
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
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
            logger.info("Lade Whisper-Modell aus %s …", self._model_path)
            self._model = Model(
                str(self._model_path),
                n_threads=self._n_threads,
                # 1 = BEAM_SEARCH-Strategie. Ohne das wird ein spaeteres
                # beam_search-Dict stillschweigend ignoriert (verifiziert).
                params_sampling_strategy=1 if self._beam_size > 0 else 0,
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
        wav_path = Path(wav_path)
        _check_wav_format(wav_path, self._sample_rate)
        kwargs = {
            "language": self._language,
            "translate": False,
            "single_segment": self._single_segment,
            # Params persistieren zwischen Aufrufen → audio_ctx bei JEDEM
            # Aufruf explizit setzen (1500 = volles Fenster als Reset).
            "audio_ctx": (
                self._audio_ctx_for(wav_path)
                if self._dynamic_audio_ctx else self._AUDIO_CTX_MAX
            ),
            # Konfidenz pro Segment mitrechnen (geometrisches Mittel der
            # Token-Wahrscheinlichkeiten) — Basis fuer den Halluzinations-Filter.
            "extract_probability": self._min_segment_probability > 0.0,
        }
        if self._beam_size > 0:
            # Dict muss vollstaendig sein — patience weglassen = KeyError.
            kwargs["beam_search"] = {"beam_size": self._beam_size, "patience": -1.0}
        if grammar:
            kwargs["initial_prompt"] = self._grammar_to_prompt(grammar)
        segments = model.transcribe(str(wav_path), **kwargs)

        all_text: list[str] = []
        kept: list[str] = []
        dropped_low_conf = False
        for seg in segments:
            if not seg.text:
                continue
            all_text.append(seg.text.strip())
            prob = getattr(seg, "probability", None)
            # NaN/None = Konfidenz nicht berechnet → Segment behalten.
            if (
                self._min_segment_probability > 0.0
                and prob is not None
                and prob == prob  # not NaN
                and prob < self._min_segment_probability
            ):
                logger.info(
                    "Whisper-Segment niedrig-konfident (%.2f < %.2f): «%s»",
                    prob, self._min_segment_probability, seg.text.strip(),
                )
                dropped_low_conf = True
                continue
            kept.append(seg.text.strip())

        result = " ".join(kept).strip()
        if result:
            return result
        # Alle Segmente rausgefiltert, aber es GAB welche: die VAD hat vorher
        # Sprache gesehen (der Aufrufer gatet auf speech_seen), also lieber die
        # niedrig-konfidente Transkription zurückgeben als NICHTS — sonst würde
        # genau die leise/undeutliche Kinderstimme, die der Umbau erfassen soll,
        # stummgeschaltet. Der Fuzzy-Match-Threshold (intent.py) ist die zweite
        # Verteidigungslinie gegen Müll; die reine Halluzination auf Stille wird
        # bereits vom speech_seen-Gate abgefangen.
        if dropped_low_conf and all_text:
            logger.info("Alle Segmente niedrig-konfident — nutze Rohtranskript als Fallback.")
            return " ".join(all_text).strip()
        return result


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
