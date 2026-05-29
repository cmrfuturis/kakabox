"""Offline-TTS für die Titel-Ansage ("Wie heißt dieses Lied?").

Piper (neuronale deutsche Stimme ``de_DE-thorsten-medium``) als Primär-Backend,
espeak-ng als Fallback — beides offline. Lazy-Load wie bei der ASR (asr.py):
das Modell zieht erst beim ersten Synth in den RAM (oder per ``warmup`` beim
Boot). Fehlt das Piper-Paket oder das Modell, wird auf espeak-ng zurückgefallen;
fehlt auch das, liefert ``synth_to_wav`` ``None`` (der Aufrufer spielt dann einen
festen "weiß ich gerade nicht"-Prompt). So bricht ein TTS-Problem nie den Flow.

Persistent-Cache: Piper-WAVs werden unter ``cache_dir/<sha1(titel)>.wav`` abgelegt
— die Titel-Menge ist durch den Katalog beschränkt, eine zweite Ansage desselben
Titels ist damit instant. Der espeak-Fallback wird BEWUSST nicht in den Cache
geschrieben (sonst würde eine vorübergehend roboterhafte Notlösung den Cache
dauerhaft vergiften); er landet in /tmp und gilt nur für den einen Aufruf.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger("kakabox.tts")

DEFAULT_MODEL_PATH = Path("/usr/share/kakabox/tts/de_DE-thorsten-medium.onnx")
DEFAULT_CACHE_DIR = Path("/var/lib/kakabox/tts-cache")


class TtsUnavailable(RuntimeError):
    """Piper kann nicht laufen — Paket fehlt oder Modell fehlt."""


def _cache_key(text: str) -> str:
    """Stabiler Schlüssel über den normalisierten Titel (case-/whitespace-insensitiv)."""
    norm = " ".join(text.lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


class TitleSpeaker:
    """Synthetisiert kurze Texte (Song-Titel) zu WAV. Piper primär, espeak Fallback."""

    def __init__(
        self,
        model_path: Path = DEFAULT_MODEL_PATH,
        cache_dir: Path = DEFAULT_CACHE_DIR,
    ) -> None:
        self._model_path = Path(model_path)
        self._cache_dir = Path(cache_dir)
        self._voice = None  # type: ignore  # lazy PiperVoice
        # Pfad, AUS DEM ``_voice`` geladen wurde — wird atomar mit _voice gesetzt
        # und mitgeliefert, damit der Cache-Dateiname IMMER zur tatsächlich
        # synthetisierten Stimme passt (auch wenn parallel set_model umschaltet).
        self._voice_model_path: Optional[Path] = None
        # Wenn Paket/Modell dauerhaft fehlen, merken wir uns das und sparen den
        # wiederholten Import-/Datei-Check (transiente Synth-Fehler zählen NICHT).
        self._piper_failed = False
        # Schützt den Lazy-Load: Boot-Warmup-Thread und erster Push-to-Talk
        # könnten sonst das Modell parallel laden.
        self._load_lock = threading.Lock()

    def set_model(self, model_path: Path) -> None:
        """Wechselt die Stimme (z.B. männlich↔weiblich vom Backend).

        Setzt das geladene Modell zurück; der nächste ``warmup``/``synth_to_wav``
        lädt das neue. Der Cache ist pro Modell getrennt (Dateiname enthält den
        Modell-Stem), eine alte Ansage kommt also nie in der falschen Stimme.
        """
        model_path = Path(model_path)
        with self._load_lock:
            if model_path == self._model_path:
                return
            self._model_path = model_path
            self._voice = None
            self._piper_failed = False
        logger.info("TTS-Stimme gewechselt auf %s", model_path.stem)

    def _cache_path(self, text: str) -> Path:
        """Cache-Datei pro (Stimme, Titel) — Modell-Stem im Namen trennt die
        Stimmen, damit ein Stimmwechsel nie eine alte WAV der anderen liefert."""
        return self._cache_dir / f"{self._model_path.stem}_{_cache_key(text)}.wav"

    def warmup(self) -> None:
        """Lädt das Piper-Modell vorab in den RAM (idempotent). Wirft nicht —
        loggt nur, damit der Boot bei fehlendem Modell nicht hängt."""
        try:
            self._load_piper()
        except TtsUnavailable as e:
            logger.info("TTS-Warmup übersprungen: %s", e)
        except Exception:
            logger.exception("TTS-Warmup unerwartet fehlgeschlagen")

    def _load_piper(self):
        """Lädt (lazy) das Piper-Modell und gibt ``(voice, model_path)`` zurück —
        beide aus DERSELBEN Generation, damit der Aufrufer die Cache-Datei nach
        dem tatsächlich geladenen Modell benennt (verhindert Cache-Poisoning bei
        parallelem Stimmwechsel)."""
        # Double-checked: erst lockfrei (häufiger Fall: Modell schon geladen).
        # _voice_model_path wird mit _voice gekoppelt (set_model nullt _voice bei
        # Pfadwechsel), das Paar ist also konsistent.
        if self._voice is not None:
            return self._voice, self._voice_model_path
        with self._load_lock:
            if self._voice is not None:
                return self._voice, self._voice_model_path
            if self._piper_failed:
                raise TtsUnavailable("Piper bereits als nicht verfügbar markiert.")
            try:
                from piper import PiperVoice  # type: ignore
            except ImportError as e:
                self._piper_failed = True
                raise TtsUnavailable(
                    "piper-tts-Paket fehlt. Installation: "
                    ".venv/bin/pip install -r device/requirements-voice.txt"
                ) from e
            if not self._model_path.is_file():
                self._piper_failed = True
                raise TtsUnavailable(
                    f"Piper-Modell nicht gefunden unter {self._model_path}. "
                    "Siehe device/voice/README.md."
                )
            # PiperVoice.load liest zwingend auch die Sidecar-Config
            # <modell>.onnx.json. Fehlt sie (z.B. abgebrochener Download), wäre
            # das ein dauerhafter Fehler — als TtsUnavailable behandeln, damit
            # _piper_failed greift und nicht jede Ansage neu (vergeblich) lädt.
            config_path = Path(f"{self._model_path}.json")
            if not config_path.is_file():
                self._piper_failed = True
                raise TtsUnavailable(
                    f"Piper-Config nicht gefunden unter {config_path}. "
                    "Siehe device/voice/README.md."
                )
            logger.info("Lade Piper-Modell %s …", self._model_path)
            try:
                loaded = PiperVoice.load(str(self._model_path))
            except Exception as e:
                # Korruptes Modell/Config → dauerhaft als nicht verfügbar merken,
                # damit folgende Ansagen sofort auf espeak fallen statt erneut
                # teuer/laut zu scheitern.
                self._piper_failed = True
                raise TtsUnavailable(f"Piper-Modell konnte nicht geladen werden: {e}") from e
            # _voice_model_path VOR _voice setzen: ein lockfreier Leser, der _voice
            # ≠ None sieht, liest dann garantiert den passenden Pfad.
            self._voice_model_path = self._model_path
            self._voice = loaded
            return self._voice, self._voice_model_path

    def synth_to_wav(self, text: str) -> Optional[Path]:
        """Text → Pfad einer WAV-Datei. ``None``, wenn weder Piper noch espeak gehen.

        Reihenfolge: Cache-Hit → Piper (+ Cache schreiben) → espeak (/tmp, kein
        Cache) → None.
        """
        text = (text or "").strip()
        if not text:
            return None

        # Schneller Hit-Check mit der aktuellen Stimme (ohne Modell-Load).
        if self._cache_path(text).is_file():
            return self._cache_path(text)

        # 1) Piper (gecacht). Der SCHREIB-Pfad wird aus dem TATSÄCHLICH geladenen
        # Modell abgeleitet (model_path aus _load_piper), nicht aus self._model_path
        # — sonst könnte ein paralleler set_model die neue Stimme unter dem alten
        # Dateinamen ablegen (Cache-Poisoning).
        try:
            voice, model_path = self._load_piper()
            cache_path = self._cache_dir / f"{Path(model_path).stem}_{_cache_key(text)}.wav"
            if cache_path.is_file():
                return cache_path
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp.wav")
            try:
                with wave.open(str(tmp), "wb") as w:
                    voice.synthesize_wav(text, w)
                tmp.replace(cache_path)  # atomar: nie eine halbe WAV im Cache
            finally:
                # Bei Erfolg ist tmp schon weggerenamt (no-op); bei Abbruch
                # mitten in der Synthese die halbe Datei aufräumen.
                tmp.unlink(missing_ok=True)
            return cache_path
        except TtsUnavailable as e:
            logger.info("Piper nicht verfügbar (%s) → espeak-Fallback.", e)
        except Exception:
            logger.exception("Piper-Synthese fehlgeschlagen → espeak-Fallback.")

        # 2) espeak-ng (ephemer, NICHT in den Cache)
        return self._espeak_to_tmp(text)

    def _espeak_to_tmp(self, text: str) -> Optional[Path]:
        out = Path(tempfile.gettempdir()) / f"kakabox_tts_{_cache_key(text)}.wav"
        try:
            subprocess.run(
                ["espeak-ng", "-v", "de", "-w", str(out), text],
                check=True, capture_output=True, timeout=10,
            )
            return out if out.is_file() else None
        except (FileNotFoundError, subprocess.SubprocessError) as e:
            logger.warning("espeak-ng-Fallback fehlgeschlagen: %s", e)
            return None
