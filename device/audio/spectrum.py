"""Audio-Spectrum-Capture vom ALSA snd-aloop Loopback.

Liest fortlaufend Stereo-PCM vom Loopback-Capture-Device, mittelt zu mono,
FFT und mappt das auf N logarithmisch verteilte Frequenzbänder. Ergebnis
sind Werte 0..1 pro Band — direkt für ``Leds.update_spectrum(bands)``.

Hintergrund: ``/etc/asound.conf`` definiert ``kakabox_audio`` als Multi-
Device, das jeden Audio-Stream parallel an den MAX98357A-Speaker UND in
``hw:Loopback,0,0`` schreibt. Auf der Capture-Seite (``hw:Loopback,1,0``)
können wir den Stream live mitlesen. Latenz: ein Chunk (~23 ms bei
1024 Frames / 44.1 kHz).

Threading: Der Read-Loop wird vom Caller in einem Daemon-Thread betrieben
(siehe main.py). Die Klasse selbst hält nur den arecord-Subprozess.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Optional

import numpy as np

logger = logging.getLogger("kakabox.spectrum")

LOOPBACK_DEVICE = "hw:Loopback,1,0"
# 22.05 kHz reicht für 16 Bänder von 60 Hz..11 kHz vollkommen aus (Nyquist
# = 11 kHz). Halbiert CPU-Last für FFT gegenüber 44.1 kHz, ohne dass das
# Tanzen visuell anders aussieht (16 Bänder können eh keine 16 kHz darstellen).
SAMPLE_RATE = 22050
CHANNELS = 2  # stereo, wird intern auf mono gemittelt
CHUNK_FRAMES = 1024  # ~46 ms bei 22.05 kHz — passt zu DANCE_FPS=20
BYTES_PER_SAMPLE = 2  # S16_LE

# Frequenzbereich für Musik: ~60 Hz (Bass) bis ~16 kHz (Brillanz).
# Höhere Bänder hätten kaum Energie und wären meist tot.
FREQ_MIN_HZ = 60.0
FREQ_MAX_HZ = 10000.0  # < Nyquist von 22.05 kHz / 2

# Normalisierungs-Konstante: typische Musik-FFT-Magnituden liegen je nach
# Band bei ~5..40 für Pop/Rock-Mix. Wir teilen durch diesen Wert, dann
# clampen auf 1.0. Niedriger Wert = empfindlicher (auch leise Musik tanzt).
NORM_DIVISOR = 12.5


class SpectrumCapture:
    """Kapselt arecord-Subprozess + FFT-Band-Berechnung.

    Lifecycle: ``start()`` öffnet den Subprozess, ``read_bands()`` liest
    einen Chunk + berechnet Bänder, ``stop()`` schließt sauber. Die Klasse
    ist NICHT thread-safe — nur ein Reader-Thread sollte ``read_bands``
    rufen, lifecycle-Calls (``start``/``stop``) ggf. vom Main-Thread.
    """

    def __init__(
        self,
        n_bands: int = 16,
        device: str = LOOPBACK_DEVICE,
        sample_rate: int = SAMPLE_RATE,
        chunk_frames: int = CHUNK_FRAMES,
    ) -> None:
        self.n_bands = n_bands
        self.device = device
        self.sample_rate = sample_rate
        self.chunk_frames = chunk_frames
        self._proc: Optional[subprocess.Popen] = None
        self._window = np.hanning(chunk_frames).astype(np.float32)
        # Pre-compute: welche FFT-Bins gehören zu welchem Band? Logarithmisch
        # verteilt von FREQ_MIN_HZ bis FREQ_MAX_HZ.
        freqs = np.fft.rfftfreq(chunk_frames, 1.0 / sample_rate)
        edges = np.geomspace(FREQ_MIN_HZ, FREQ_MAX_HZ, n_bands + 1)
        self._band_indices: list[np.ndarray] = []
        for i in range(n_bands):
            mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
            self._band_indices.append(np.where(mask)[0])

    def start(self) -> bool:
        """Öffnet arecord. Idempotent. Returns False, wenn arecord fehlt."""
        if self._proc is not None and self._proc.poll() is None:
            return True
        cmd = [
            "arecord", "-q",
            "-D", self.device,
            "-f", "S16_LE",
            "-r", str(self.sample_rate),
            "-c", str(CHANNELS),
            "-t", "raw",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            logger.info("Spectrum-Capture gestartet (%s)", self.device)
            return True
        except FileNotFoundError as e:
            logger.error("arecord nicht installiert: %s", e)
            return False

    def stop(self) -> None:
        """Beendet arecord. Idempotent."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass
        logger.info("Spectrum-Capture gestoppt")

    def read_bands(self) -> Optional[list[float]]:
        """Liest einen Chunk, berechnet Bänder. Blockiert bis Chunk voll
        gelesen ist (~23 ms). Returns None wenn Stream tot/leer."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        chunk_bytes = self.chunk_frames * CHANNELS * BYTES_PER_SAMPLE
        data = proc.stdout.read(chunk_bytes)
        if not data or len(data) < chunk_bytes:
            return None

        # int16-Stereo → float32-Mono (Mittelwert beider Kanäle), normalisiert.
        arr = np.frombuffer(data, dtype=np.int16).reshape(-1, CHANNELS)
        mono = arr.mean(axis=1).astype(np.float32) / 32768.0

        # Hann-Fenster vor FFT (verhindert Spektral-Leak an Chunk-Grenzen)
        windowed = mono * self._window
        spectrum = np.abs(np.fft.rfft(windowed))

        # Pro Band: Maximum der FFT-Bins in der Band-Range.
        # max statt mean: Peaks "tanzen" deutlicher als der Durchschnitt.
        bands: list[float] = []
        for indices in self._band_indices:
            if len(indices) == 0:
                bands.append(0.0)
                continue
            val = float(spectrum[indices].max()) / NORM_DIVISOR
            bands.append(min(1.0, val))
        return bands

    def close(self) -> None:
        self.stop()
