"""File-basierter Spectrum-Analyser für die LED-Streifen.

Statt vom ALSA-Loopback abzuzapfen (Multi-Device + mpv = hangs auf Pi 5),
decodieren wir die aktuell spielende Audio-Datei parallel mit ffmpeg und
berechnen FFT-Bänder synchron zur mpv-Wiedergabeposition. ffmpeg streamt
rohes PCM in eine Pipe; wir drainen das im Tempo der Wiedergabe — die Pipe
self-throttled, ffmpeg blockt sobald wir hinterherhinken.

Threading: Eine Instanz pro Track. Lifecycle wird vom Spectrum-Loop in
main.py gesteuert: bei Track-Wechsel alte Instanz schliessen, neue auf
den Dateinamen starten.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Optional

import numpy as np

logger = logging.getLogger("kakabox.spectrum")

SAMPLE_RATE = 22050
CHUNK_FRAMES = 1024  # ~46 ms bei 22.05 kHz — passt zu DANCE_FPS=20
BYTES_PER_SAMPLE = 2  # S16_LE mono
FREQ_MIN_HZ = 60.0
FREQ_MAX_HZ = 10000.0
NORM_DIVISOR = 12.5  # tuned in legacy SpectrumCapture für sichtbares Tanzen


class FileSpectrum:
    """Decodiert eine Audio-Datei mit ffmpeg + liefert FFT-Bänder an der
    aktuellen Wiedergabeposition.

    Self-throttling: ffmpeg füllt seine Stdout-Pipe (~64 KB Kernel-Buffer)
    und blockt sobald wir nicht mehr lesen — perfekte Synchronisation ohne
    Polling. Sollte mpv vorlaufen (seek vorwärts), holen wir per "drain
    until target_sample" auf. Seek rückwärts → restart erforderlich.
    """

    def __init__(
        self,
        file_path: str,
        n_bands: int = 16,
        sample_rate: int = SAMPLE_RATE,
        chunk_frames: int = CHUNK_FRAMES,
    ) -> None:
        self.file_path = file_path
        self.n_bands = n_bands
        self.sample_rate = sample_rate
        self.chunk_frames = chunk_frames
        self.chunk_bytes = chunk_frames * BYTES_PER_SAMPLE
        self._proc: Optional[subprocess.Popen] = None
        self._position_samples = 0
        self._window = np.hanning(chunk_frames).astype(np.float32)
        freqs = np.fft.rfftfreq(chunk_frames, 1.0 / sample_rate)
        edges = np.geomspace(FREQ_MIN_HZ, FREQ_MAX_HZ, n_bands + 1)
        self._band_indices: list[np.ndarray] = []
        for i in range(n_bands):
            mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
            self._band_indices.append(np.where(mask)[0])

    def start(self, start_seconds: float = 0.0) -> bool:
        """Spawnt ffmpeg. Optional ``start_seconds`` skipped die Datei am
        Anfang (für resume-on-replace). Idempotent."""
        if self._proc is not None and self._proc.poll() is None:
            return True
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "quiet"]
        if start_seconds > 0:
            cmd += ["-ss", f"{start_seconds:.3f}"]
        cmd += [
            "-i", self.file_path,
            "-f", "s16le",
            "-ar", str(self.sample_rate),
            "-ac", "1",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            self._position_samples = int(start_seconds * self.sample_rate)
            return True
        except FileNotFoundError:
            logger.error("ffmpeg nicht installiert — FileSpectrum nicht verfügbar")
            return False

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass

    def read_bands_at(self, target_time_seconds: float) -> Optional[list[float]]:
        """Liest bis ``target_time_seconds`` voraus und gibt FFT-Bänder zurück.

        Returns None bei: kein Subprozess, EOF, Seek-Backward > 1s
        (Caller sollte dann ``start(start_seconds=target)`` neu aufrufen).
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        if proc.poll() is not None:
            return None

        target_sample = int(target_time_seconds * self.sample_rate)
        skip = target_sample - self._position_samples
        # > 1s Rückwärtsseek → restart, sonst lesen wir am falschen Ort
        if skip < -self.sample_rate:
            return None
        if skip > 0:
            # Drainen in Blöcken; ffmpeg gibt selber Tempo vor (Pipe blockt)
            bytes_to_skip = skip * BYTES_PER_SAMPLE
            while bytes_to_skip > 0:
                want = min(bytes_to_skip, 65536)
                got = proc.stdout.read(want)
                if not got:
                    return None
                bytes_to_skip -= len(got)
                self._position_samples += len(got) // BYTES_PER_SAMPLE

        data = proc.stdout.read(self.chunk_bytes)
        if not data or len(data) < self.chunk_bytes:
            return None
        self._position_samples += self.chunk_frames

        mono = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        windowed = mono * self._window
        spectrum = np.abs(np.fft.rfft(windowed))

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
