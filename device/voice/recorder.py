"""Push-to-Talk-Aufnahme via ``arecord``.

Vosk erwartet 16 kHz mono 16-bit PCM — exakt das, was ``arecord -f S16_LE
-r 16000 -c 1 -t raw`` liefert. Wir umgehen also bewusst sounddevice/pyaudio
und nutzen das ALSA-Cli-Tool, das auf jedem Pi schon installiert ist.

VAD-light: ``record_until_silence`` streamt vom Mic und bricht ab, sobald
nach erster erkannter Sprache eine zusammenhängende Stille-Phase erreicht
ist — natürlicher als hartes Sekunden-Cap und schneller, wenn jemand
nur "spiele Bambi" sagt. Hartes ``max_seconds``-Cap verhindert Endlosläufe,
wenn der User gar nicht spricht (oder weiter ins Mic murmelt).

Kinderstimmen-Fixes (ASR-Plan 2026-07-07, Stufe 1.3):

- **DC-Abzug pro Chunk:** Das INMP441 liefert einen DC-Offset, der die
  RMS-Werte künstlich hebt (Idle-Floor gemessen 10–7800 statt ~5–40).
  RMS wird deshalb über mean-zentrierte Samples berechnet.
- **Einschalt-Transient:** Die ersten ~300 ms nach arecord-Start enthalten
  einen DC-Einschwing-Knall (Chunk 0: RMS bis 7800 live gemessen), der
  früher bei JEDER Aufnahme ``speech_seen`` setzte — reines Rauschen ging
  als "Sprache" an die ASR. Diese Settle-Phase wird jetzt weder für die
  Speech-Detection genutzt noch in den Puffer übernommen (echte Sprache
  ist dort nicht drin — das Mic schwingt erst ein).
- **Adaptive Schwellen:** Die alten Fixwerte (speech≥180 / silence<80)
  stammen von der Logitech C920 mit eingebautem AGC. Am gain-losen INMP441
  liegt normale Sprechlautstärke rechnerisch UNTER der alten Silence-
  Schwelle — leise Kinderstimmen trafen nie die Speech-Schwelle. Jetzt
  wird der Noise-Floor in der Settle-Phase geschätzt und die Schwellen
  daraus abgeleitet (speech = floor×4, silence = floor×2, geklemmt so,
  dass sie nie ÜBER den alten C920-Werten liegen — die alten Werte sind
  das konservative Maximum, nicht mehr das Minimum).
- **Rückwirkender ASR-Gate (``speech_present``):** Die Streaming-Schwellen
  oben steuern nur noch den vorzeitigen Abbruch nach Stille. Ob überhaupt
  Sprache da war (und damit ASR läuft), entscheidet ``speech_seen`` jetzt
  RÜCKWIRKEND über den ganzen Puffer — robust gegen die Floor-Vergiftung, wenn
  jemand sofort lospricht (Details + Kalibrierung s. ``speech_present`` unten).

Threading: ``record_until_silence`` blockt im Aufrufer-Thread. Die Voice-
Aktivierung im Main-Loop läuft sowieso in einem Hintergrund-Thread (Knopf-
Callback), also unproblematisch.
"""
from __future__ import annotations

import logging
import math
import struct
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# INMP441 MEMS-Mic über Google Voice HAT Overlay. Selbe Karte wie der
# MAX98357A-Speaker (sndrpigooglevoi) — Capture-Side liefert mono I²S-Daten.
# CARD-Name statt Karten-Nummer macht's stabil gegen USB-Reordering bei Reboot.
DEFAULT_DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"
DEFAULT_SAMPLE_RATE = 16000

# Obergrenzen für die adaptiven Schwellen (16-bit RMS, max 32767): die alten,
# mit der C920 kalibrierten Fixwerte. Adaptiv darf nur EMPFINDLICHER werden
# (Schwellen senken), nie tauber — so bleibt das Verhalten bei lautem
# Umgebungsrauschen höchstens so streng wie bisher.
LEGACY_SPEECH_RMS = 180.0
LEGACY_SILENCE_RMS = 80.0
# Untergrenzen: unterhalb davon wäre die Detection reines Rauschen-Raten.
# BEWUSST niedrig gehalten — eine leise Kinderstimme liegt DC-bereinigt oft
# bei RMS ~30–40; eine zu hohe Untergrenze würde genau die verpassen, die der
# Umbau erfassen soll. Provisorische Werte: mit dem Stufe-0-Testset (echte
# Kinderaufnahmen) final kalibrieren.
MIN_SPEECH_RMS = 28.0
MIN_SILENCE_RMS = 12.0
# Faktoren über dem gemessenen Noise-Floor (ASR-Plan: speech = floor×3–4,
# silence = floor×1,5–2).
SPEECH_FLOOR_FACTOR = 4.0
SILENCE_FLOOR_FACTOR = 2.0
# Einschwing-/Kalibrierphase am Aufnahmebeginn (Transient + Floor-Schätzung).
DEFAULT_SETTLE_SECONDS = 0.3

# Rückwärtskompatible Aliase (Tests/ältere Aufrufer referenzieren die Namen).
DEFAULT_SPEECH_RMS = LEGACY_SPEECH_RMS
DEFAULT_SILENCE_RMS = LEGACY_SILENCE_RMS

# Robuste, RÜCKWIRKENDE Sprach-Erkennung (``speech_present``, s.u.): der
# eigentliche ASR-Gate. Entscheidet NACH der Aufnahme über den gesamten Puffer,
# ob überhaupt Sprache drin war — unabhängig von den Streaming-Schwellen oben,
# die nur noch den vorzeitigen Abbruch nach Stille steuern.
#
# WARUM: Die Streaming-Floor-Schätzung aus der 0,3s-Settle-Phase wird VERGIFTET,
# sobald jemand sofort lospricht — der laute Wortanfang gilt dann als
# "Grundrauschen", die Speech-Schwelle schnellt auf den 180-Deckel und echte
# Sprache darunter wird NIE erkannt. Live an INMP441-Aufnahmen reproduziert:
# klare Kommandos ("Der Zug hat keine Bremse") gingen reihenweise als "keine
# Sprache" verloren, weil normale Sprechlautstärke am gain-losen Mic in
# 100-ms-Fenstern nur ~110–180 RMS erreicht und die vergiftete Schwelle nie fiel.
#
# Der robuste Detektor schätzt den Floor als MINIMUM der GANZEN Aufnahme (der
# leiseste Chunk = echter Ruhepegel, nicht das vergiftete Settle-Fenster) und
# verlangt mind. ``SPEECH_DETECT_MIN_CHUNKS`` Chunks über
# ``max(Floor×Faktor, Abs-Minimum)``. Min statt Perzentil, weil ein kurzes,
# sprachdichtes Kommando kaum Stille-Chunks hat — ein Perzentil würde dann IN
# der Sprache landen und den Floor hochtreiben. Die Chunk-ZAHL-Schwelle macht
# die Erkennung spike-robust (ein Knopfklick/Einschalt-Transient sind 1–2 laute
# Chunks, echte Sprache viele); das niedrige Abs-Minimum (55 statt 180) fängt
# leise Kinderstimmen und deckelt zugleich einen zu niedrigen Floor (Dropout).
# Rest-Fehlgeräusche (z.B. Motorbrummen) fängt die nachgelagerte Halluzinations-/
# Routing-Schwelle (route_transcript → no_match). Provisorische Werte: an 35
# echten Aufnahmen kalibriert (0 echte Kommandos verpasst, nur reines
# [Motor]-Rauschen ließ der Gate durch → downstream no_match).
SPEECH_DETECT_FLOOR_FACTOR = 2.5
SPEECH_DETECT_ABS_MIN = 55.0
SPEECH_DETECT_MIN_CHUNKS = 4


class RecorderError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordingResult:
    """Ergebnis einer Push-to-Talk-Aufnahme.

    ``path`` verhält sich wie bisher (WAV-Datei); ``speech_seen`` sagt, ob
    die VAD überhaupt Sprache erkannt hat — Aufrufer sollen bei False gar
    nicht erst transkribieren (Whisper halluziniert auf Stille/Rauschen
    gern plausible deutsche Sätze, die dann fälschlich ein Lied starten).
    ``__fspath__`` macht das Objekt path-like, damit bestehende Aufrufer
    (``transcribe_wav(result)``) ohne Umbau funktionieren.
    """
    path: Path
    speech_seen: bool
    duration_seconds: float

    def __fspath__(self) -> str:
        return str(self.path)


def chunk_rms(data: bytes, frames: int) -> float:
    """RMS eines S16_LE-Chunks über mean-zentrierte Samples (DC-Abzug).

    Pure Funktion — vom Eval-Harness und den Tests direkt nutzbar.
    """
    samples = struct.unpack(f"<{frames}h", data)
    mean = sum(samples) / frames
    return math.sqrt(sum((s - mean) ** 2 for s in samples) / frames)


def adaptive_thresholds(noise_floor: float) -> tuple[float, float]:
    """Leitet (speech_rms, silence_rms) aus dem gemessenen Noise-Floor ab.

    Geklemmt zwischen MIN_* (nie empfindlicher als sinnvoll) und den alten
    C920-Fixwerten (nie tauber als bisher). Garantiert silence < speech.

    Nur noch für den Streaming-Abbruch nach Stille genutzt — der ASR-Gate
    (``speech_seen``) kommt aus ``speech_present`` (s. Modul-Doku).
    """
    speech = min(max(noise_floor * SPEECH_FLOOR_FACTOR, MIN_SPEECH_RMS), LEGACY_SPEECH_RMS)
    silence = min(max(noise_floor * SILENCE_FLOOR_FACTOR, MIN_SILENCE_RMS), LEGACY_SILENCE_RMS)
    if silence >= speech:
        silence = speech / 2.0
    return speech, silence


def speech_present(rms_values: list[float]) -> bool:
    """True, wenn der fertige Aufnahme-Puffer echte Sprache enthält (ASR-Gate).

    Rückwirkend über ALLE Chunk-RMS-Werte — robust gegen die Floor-Vergiftung
    der Streaming-Erkennung (s. Modul-Doku): Floor = MINIMUM (leisester Chunk =
    echter Ruhepegel, auch wenn sofort gesprochen wurde und selbst bei einem
    sprachdichten Kurzkommando ohne Stille-Pause), Sprache = mind.
    ``SPEECH_DETECT_MIN_CHUNKS`` Chunks über ``max(Floor×Faktor, Abs-Minimum)``.
    Die Chunk-Zahl-Schwelle macht die Erkennung spike-robust: ein Einschalt-
    Transient oder Knopfklick (1–2 laute Chunks) triggert nicht.

    Pure Funktion — vom Eval-Harness und den Tests direkt nutzbar.
    """
    if len(rms_values) < SPEECH_DETECT_MIN_CHUNKS:
        return False
    floor = min(rms_values)
    level = max(floor * SPEECH_DETECT_FLOOR_FACTOR, SPEECH_DETECT_ABS_MIN)
    return sum(1 for r in rms_values if r >= level) >= SPEECH_DETECT_MIN_CHUNKS


class MicRecorder:
    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate

    def record_until_silence(
        self,
        max_seconds: float = 5.0,
        silence_seconds: float = 1.0,
        initial_silence_seconds: float = 3.0,
        speech_rms: Optional[float] = None,
        silence_rms: Optional[float] = None,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        output_path: Path | str = "/tmp/kakabox_ptt.wav",
    ) -> RecordingResult:
        """Nimmt auf, bis ``silence_seconds`` zusammenhängende Stille NACH erster
        Sprache erreicht ist — spätestens nach ``max_seconds`` hart abgebrochen.

        ``initial_silence_seconds``: wenn nach so langer Aufnahme noch keine
        Sprache erkannt wurde, wird abgebrochen (verhindert lange Wartezeit
        bei versehentlichem Knopfdruck).

        ``speech_rms``/``silence_rms``: explizite Schwellen-Overrides (Tests,
        Sonderfälle). Default ``None`` → adaptive Schwellen aus dem in der
        Settle-Phase gemessenen Noise-Floor (siehe Modul-Doku).

        Schreibt eine 16 kHz mono S16_LE-WAV (ohne die Settle-Phase). Wirft
        ``RecorderError`` wenn arecord nicht startet oder keine Frames liefert.
        """
        chunk_ms = 100
        chunk_frames = int(self.sample_rate * chunk_ms / 1000)
        chunk_bytes = chunk_frames * 2  # S16_LE = 2 byte pro Sample
        settle_chunks = max(1, int(settle_seconds * 1000 / chunk_ms))
        # Die Zeit-Budgets zählen AB Ende der Settle-Phase, damit sich das
        # nutzbare Aufnahmefenster durch den Fix nicht verkürzt.
        max_chunks = settle_chunks + max(1, int(max_seconds * 1000 / chunk_ms))
        silence_chunks_target = max(1, int(silence_seconds * 1000 / chunk_ms))
        initial_silence_chunks = max(1, int(initial_silence_seconds * 1000 / chunk_ms))

        cmd = [
            "arecord", "-q",
            "-D", self.device,
            "-f", "S16_LE",
            "-r", str(self.sample_rate),
            "-c", "1",
            "-t", "raw",
            "-",  # rohes PCM auf stdout
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            raise RecorderError(f"arecord nicht installiert: {e}") from e

        recorded = bytearray()
        # ``streaming_speech`` steuert NUR den vorzeitigen Abbruch nach Stille.
        # Ob ASR läuft, entscheidet ``speech_present`` rückwirkend über
        # ``rms_values`` (alle Chunks, inkl. Settle) — s. Modul-Doku.
        rms_values: list[float] = []
        streaming_speech = False
        silence_streak = 0
        # Noise-Floor-Schätzung über die Settle-Phase: MIN statt Mittelwert,
        # damit weder der Chunk-0-Transient noch ein sofort lossprechendes
        # Kind den Floor (und damit die Schwellen) nach oben zieht.
        noise_floor: Optional[float] = None
        thr_speech = speech_rms if speech_rms is not None else LEGACY_SPEECH_RMS
        thr_silence = silence_rms if silence_rms is not None else LEGACY_SILENCE_RMS
        adaptive = speech_rms is None and silence_rms is None
        try:
            for chunk_idx in range(max_chunks):
                data = proc.stdout.read(chunk_bytes)
                if not data or len(data) < chunk_bytes:
                    break
                rms = chunk_rms(data, chunk_frames)
                rms_values.append(rms)

                if chunk_idx < settle_chunks:
                    # Einschwingphase: NICHT für die Speech-Detection nutzen
                    # (Einschalt-Transient), aber SEHR WOHL in den Puffer —
                    # sonst geht der Wortanfang verloren, wenn ein Kind sofort
                    # nach dem Prompt lospricht (Sprachbeginn <0,3s). Der DC-
                    # Transient im Puffer stört Whisper kaum (Log-Mel-Frontend);
                    # der geplante 80-Hz-Hochpass (1.4) räumt ihn später ganz weg.
                    # Floor-Schätzung per min über die Settle-Chunks (nimmt das
                    # leiseste Sub-Fenster, robust gegen sofort lautes Sprechen).
                    recorded += data
                    noise_floor = rms if noise_floor is None else min(noise_floor, rms)
                    continue
                if chunk_idx == settle_chunks and adaptive:
                    thr_speech, thr_silence = adaptive_thresholds(noise_floor or 0.0)
                    logger.debug(
                        "VAD adaptiv: floor=%.1f → speech≥%.1f, silence<%.1f",
                        noise_floor or 0.0, thr_speech, thr_silence,
                    )

                recorded += data
                if rms >= thr_speech:
                    streaming_speech = True
                    silence_streak = 0
                elif rms < thr_silence:
                    silence_streak += 1
                # In-between (silence ≤ rms < speech): keine Änderung —
                # so frisst leises Nachhallen den Silence-Counter nicht weg.
                if streaming_speech and silence_streak >= silence_chunks_target:
                    break
                # Initial-Silence-Cap: wenn nach initial_silence_chunks (ab
                # Settle-Ende) noch nichts gesprochen wurde, raus.
                if not streaming_speech and chunk_idx + 1 - settle_chunks >= initial_silence_chunks:
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()

        if not recorded:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise RecorderError(
                f"arecord lieferte keine Frames. Stderr: {stderr[:200]}"
            )

        path = Path(output_path)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(bytes(recorded))

        # Autoritativer ASR-Gate: robust über die ganze Aufnahme (s. Modul-Doku).
        speech_seen = speech_present(rms_values)
        duration = len(recorded) / 2 / self.sample_rate
        logger.info(
            "Voice-Aufnahme: %.2fs (%s, %d Chunks%s)",
            duration,
            "speech detected" if speech_seen else "kein speech",
            len(rms_values),
            f", {silence_streak * chunk_ms}ms silence am Ende" if streaming_speech else "",
        )
        return RecordingResult(path=path, speech_seen=speech_seen, duration_seconds=duration)
