import logging
import threading
import time
import mpv
from dataclasses import dataclass
from typing import Optional, Callable
from audio.library import Track, Album

logger = logging.getLogger(__name__)

AUDIO_DEVICE = "alsa/plughw:CARD=MAX98357A,DEV=0"


@dataclass
class PlaybackState:
    playing: bool = False
    paused: bool = False
    current_track: Optional[Track] = None
    current_album: Optional[Album] = None
    track_index: int = 0    # position within current album's track list
    volume: int = 60        # 0–100


class Player:
    def __init__(self):
        self._mpv = mpv.MPV(
            audio_device=AUDIO_DEVICE,
            audio_format="s16",
            video=False,
            terminal=False,
        )
        # 'warn' (statt 'error') zeigt uns ALSA-Probleme im Log,
        # ohne das Log mit Debug-Spam zu fluten.
        self._mpv["msg-level"] = "all=warn"
        self._state = PlaybackState()
        self._on_track_end: Optional[Callable] = None

        # Track-Ende-Erkennung via Polling auf idle-active. Robuster als der
        # eof-reached Property-Observer (der nach play() nicht zuverlässig feuerte
        # und bei Auto-Advance-Tracks komplett ausfiel).
        self._eof_thread = threading.Thread(
            target=self._eof_watch_loop, daemon=True, name="player-eof"
        )
        self._eof_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def play_album(self, album: Album, start_index: int = 0) -> None:
        """Play an album from a given track index."""
        if not album.tracks:
            logger.warning("Album '%s' has no tracks", album.name)
            return
        self._state.current_album = album
        self._state.track_index = start_index
        self._play_current()

    def play_track(self, track: Track) -> None:
        """Play a single track without album context."""
        self._state.current_album = None
        self._state.current_track = track
        self._state.playing = True
        self._state.paused = False
        self._mpv.play(track.path)
        logger.info("Playing: %s", track.title)

    def play_file(self, path: str, title: str = "", start_seconds: float = 0.0) -> None:
        """Play an arbitrary file path (used by playlist with locally cached audio).

        Disables album auto-advance — caller controls the next-track logic via
        ``on_track_end()`` callback.

        ``start_seconds`` lässt mpv die Wiedergabe direkt an dieser Position starten —
        wird für Resume-on-Replace genutzt.
        """
        self._state.current_album = None
        synthetic = Track(id=str(path), title=title or str(path), path=str(path), index=0)
        self._state.current_track = synthetic
        self._state.playing = True
        self._state.paused = False
        if start_seconds and start_seconds > 0:
            # mpv "start"-Property gilt für die nächste loadfile-Action
            self._mpv["start"] = str(start_seconds)
        else:
            self._mpv["start"] = "0"
        self._mpv.play(str(path))

    def current_position_seconds(self) -> float:
        """Aktuelle Wiedergabeposition in Sekunden (oder 0 wenn nichts läuft)."""
        try:
            pos = self._mpv.time_pos
            return float(pos) if pos is not None else 0.0
        except Exception:
            return 0.0

    def seek_to(self, seconds: float) -> None:
        """Setze die aktuelle Wiedergabeposition (z. B. für Track-Neustart)."""
        try:
            self._mpv.seek(seconds, "absolute")
        except Exception as e:
            logger.warning("Seek fehlgeschlagen: %s", e)

    def pause(self) -> None:
        if self._state.playing:
            self._mpv.pause = True
            self._state.paused = True
            logger.info("Paused")

    def resume(self) -> None:
        if self._state.paused:
            self._mpv.pause = False
            self._state.paused = False
            logger.info("Resumed")

    def toggle_pause(self) -> None:
        if self._state.paused:
            self.resume()
        else:
            self.pause()

    def stop(self) -> None:
        self._mpv.stop()
        self._state.playing = False
        self._state.paused = False
        self._state.current_track = None
        logger.info("Stopped")

    def next_track(self) -> None:
        album = self._state.current_album
        if album and self._state.track_index < len(album.tracks) - 1:
            self._state.track_index += 1
            self._play_current()
        else:
            self.stop()

    def previous_track(self) -> None:
        if self._state.track_index > 0:
            self._state.track_index -= 1
            self._play_current()

    def set_volume(self, volume: int) -> None:
        """Set volume 0–100."""
        volume = max(0, min(100, volume))
        self._state.volume = volume
        self._mpv.volume = volume

    def get_state(self) -> PlaybackState:
        return self._state

    def on_track_end(self, callback: Callable) -> None:
        """Register a callback invoked when a track finishes."""
        self._on_track_end = callback

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _play_current(self) -> None:
        album = self._state.current_album
        if not album:
            return
        track = album.tracks[self._state.track_index]
        self._state.current_track = track
        self._state.playing = True
        self._state.paused = False
        self._mpv.play(track.path)
        logger.info("Playing [%d/%d]: %s", self._state.track_index + 1,
                    len(album.tracks), track.title)

    def _eof_watch_loop(self) -> None:
        """Pollt den mpv-Idle-Status und feuert ``_on_track_end``-Callback,
        wenn ein Track natürlich zu Ende lief (idle-active wechselt False→True
        während ``self._state.playing == True``).

        Wird in einem Daemon-Thread ausgeführt. Lebt für die gesamte Player-
        Lebensdauer; Selbstabbruch durch Garbage-Collection des Players.
        """
        prev_idle = True  # vor erstem play() ist mpv idle
        while True:
            try:
                idle = bool(self._mpv.idle_active)
            except Exception:
                # mpv terminated → Loop beenden
                return

            if idle and not prev_idle and self._state.playing and not self._state.paused:
                # Übergang Playing → Idle = Track-Ende
                self._state.playing = False
                self._state.current_track = None
                logger.info("Track-Ende erkannt (mpv idle).")

                # Callback-Aufruf außerhalb des Lock-State, damit der Callback
                # neue play_file()-Aufrufe machen kann.
                cb = self._on_track_end
                if cb:
                    try:
                        cb()
                    except Exception as e:
                        logger.error("on_track_end callback fehlgeschlagen: %s", e)

                # Album-Auto-Advance bleibt erhalten (für lokale Bibliothek).
                album = self._state.current_album
                if album and self._state.track_index < len(album.tracks) - 1:
                    self._state.track_index += 1
                    self._play_current()

            prev_idle = idle
            time.sleep(0.2)

    def __del__(self):
        try:
            self._mpv.terminate()
        except Exception:
            pass
