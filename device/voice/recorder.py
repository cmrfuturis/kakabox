"""Push-to-Talk-Aufnahme via ``arecord``.

Vosk erwartet 16 kHz mono 16-bit PCM — exakt das, was ``arecord -f S16_LE
-r 16000 -c 1 -t raw`` liefert. Wir umgehen also bewusst sounddevice/pyaudio
und nutzen das ALSA-Cli-Tool, das auf jedem Pi schon installiert ist.

VAD-light: ``record_until_silence`` streamt vom Mic und bricht ab, sobald
nach erster erkannter Sprache eine zusammenhängende Stille-Phase erreicht
ist — natürlicher als hartes 3-Sekunden-Cap und schneller, wenn jemand
nur "spiele Bambi" sagt. Hartes ``max_seconds``-Cap verhindert Endlosläufe,
wenn der User gar nicht spricht (oder weiter ins Mic murmelt).

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
from pathlib import Path

logger = logging.getLogger(__name__)

# Logitech-C920-Webcam hat ein brauchbares Stereo-Mic eingebaut; via ``plughw``
# konvertiert ALSA automatisch auf das von Vosk gewünschte Format (mono 16 kHz).
# CARD-Name statt Karten-Nummer macht's stabil gegen USB-Reordering bei Reboot.
# INMP441 MEMS-Mic über Google Voice HAT Overlay. Selbe Karte wie der
# MAX98357A-Speaker (sndrpigooglevoi) — Capture-Side liefert mono I²S-Daten.
DEFAULT_DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"
DEFAULT_SAMPLE_RATE = 16000

# VAD-Schwellen (16-bit RMS, max 32767). Empirisch bestimmt mit der C920 bei
# Tisch-Distanz: Idle-Noise-Floor ~50-100, normale Stimme RMS 200-1000.
# Zwischen ``silence`` und ``speech`` liegt eine breite "Hold"-Zone (80..180):
# Konsonanten und Pausen zwischen Wörtern fallen oft da rein und brechen
# weder die Aufnahme ab noch zählen sie als Speech — exakt was wir wollen.
DEFAULT_SPEECH_RMS = 180.0
DEFAULT_SILENCE_RMS = 80.0


class RecorderError(RuntimeError):
    pass


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
        speech_rms: float = DEFAULT_SPEECH_RMS,
        silence_rms: float = DEFAULT_SILENCE_RMS,
        output_path: Path | str = "/tmp/kakabox_ptt.wav",
    ) -> Path:
        """Nimmt auf, bis ``silence_seconds`` zusammenhängende Stille NACH erster
        Sprache erreicht ist — spätestens nach ``max_seconds`` hart abgebrochen.

        Schreibt eine 16 kHz mono S16_LE-WAV. Wirft ``RecorderError`` wenn
        arecord nicht startet oder keine Frames liefert.
        """
        chunk_ms = 100
        chunk_frames = int(self.sample_rate * chunk_ms / 1000)
        chunk_bytes = chunk_frames * 2  # S16_LE = 2 byte pro Sample
        max_chunks = max(1, int(max_seconds * 1000 / chunk_ms))
        silence_chunks_target = max(1, int(silence_seconds * 1000 / chunk_ms))

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
        speech_seen = False
        silence_streak = 0
        try:
            for _ in range(max_chunks):
                data = proc.stdout.read(chunk_bytes)
                if not data or len(data) < chunk_bytes:
                    break
                recorded += data
                samples = struct.unpack(f"<{chunk_frames}h", data)
                rms = math.sqrt(sum(s * s for s in samples) / chunk_frames)
                if rms >= speech_rms:
                    speech_seen = True
                    silence_streak = 0
                elif rms < silence_rms:
                    silence_streak += 1
                # In-between (silence_rms ≤ rms < speech_rms): keine Änderung —
                # so frisst leises Nachhallen den Silence-Counter nicht weg.
                if speech_seen and silence_streak >= silence_chunks_target:
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

        duration = len(recorded) / 2 / self.sample_rate
        logger.info(
            "Voice-Aufnahme: %.2fs (%s%s)",
            duration,
            "speech detected" if speech_seen else "kein speech",
            f", {silence_streak * chunk_ms}ms silence am Ende" if speech_seen else "",
        )
        return path
