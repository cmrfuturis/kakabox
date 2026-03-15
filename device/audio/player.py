import logging
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
        self._mpv["msg-level"] = "all=error"
        self._state = PlaybackState()
        self._on_track_end: Optional[Callable] = None

        self._mpv.observe_property("eof-reached", self._on_eof)

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

    def _on_eof(self, _name, value) -> None:
        if not value:
            return
        if self._on_track_end:
            self._on_track_end()
        # auto-advance to next track
        album = self._state.current_album
        if album and self._state.track_index < len(album.tracks) - 1:
            self._state.track_index += 1
            self._play_current()
        else:
            self._state.playing = False
            self._state.current_track = None
            logger.info("Playback finished")

    def __del__(self):
        try:
            self._mpv.terminate()
        except Exception:
            pass
