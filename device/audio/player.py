import logging
import threading
import time
from pathlib import Path
import mpv
from dataclasses import dataclass
from typing import Optional, Callable
from audio.library import Track, Album

logger = logging.getLogger(__name__)

# Google Voice HAT Soundcard (MAX98357A Speaker + INMP441 Mic an I²S).
# Card-Name kommt vom `dtoverlay=googlevoicehat-soundcard` in
# /boot/firmware/config.txt — Playback (Speaker) und Capture (Mic) auf
# der gleichen Karte.
AUDIO_DEVICE = "alsa/plughw:CARD=sndrpigooglevoi,DEV=0"


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

        # Prompt-Mode: temporäre Wiedergabe von System-Sounds (Boot-Jingle,
        # Bye-Sound). Während eines Prompts wird mpv auf system_volume gestellt
        # und nach Track-Ende auf die User-Lautstärke zurück. Buttons können den
        # Prompt via stop() jederzeit abwürgen — der Volume-Restore passiert dort
        # genauso.
        self._prompt_active: bool = False
        self._volume_before_prompt: Optional[int] = None

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

    def play_prompt(self, path: str, volume: int) -> None:
        """Spielt einen System-Prompt (Boot, WLAN, Bye) bei separater Lautstärke.

        Vor dem Abspielen wird die aktuelle mpv-Lautstärke gemerkt und auf
        ``volume`` (0-100) gesetzt. Beim Track-Ende (oder bei stop()) wird sie
        wiederhergestellt. ``is_prompt_playing`` wird währenddessen True — die
        Knopf-Handler im Main-Loop nutzen das, um Prompts per Druck abzubrechen
        statt zu pausieren.
        """
        volume = max(0, min(100, int(volume)))
        if not self._prompt_active:
            self._volume_before_prompt = self._state.volume
        self._prompt_active = True
        try:
            self._mpv.volume = volume
        except Exception as e:
            logger.warning("Prompt-Volume konnte nicht gesetzt werden: %s", e)
        # play_file kümmert sich um das eigentliche Abspielen + State-Reset.
        self.play_file(path, title=Path(path).stem)

    def is_prompt_playing(self) -> bool:
        return self._prompt_active

    def _restore_after_prompt(self) -> None:
        """Setzt mpv-Lautstärke nach Prompt-Ende auf den User-Wert zurück."""
        if not self._prompt_active:
            return
        target = self._volume_before_prompt
        self._prompt_active = False
        self._volume_before_prompt = None
        if target is None:
            return
        try:
            self._mpv.volume = target
        except Exception as e:
            logger.warning("Volume-Restore nach Prompt fehlgeschlagen: %s", e)

    def play_file(self, path: str, title: str = "", start_seconds: float = 0.0) -> None:
        """Play an arbitrary file path (used by playlist with locally cached audio).

        Disables album auto-advance — caller controls the next-track logic via
        ``on_track_end()`` callback.

        ``start_seconds`` lässt mpv die Wiedergabe direkt an dieser Position starten —
        wird für Resume-on-Replace genutzt.

        Defensive Sequenz: erst stop, kurz warten, dann play. Ohne das geht
        mpv beim 2. Track auf dem Multi-Device kakabox_audio (snd-aloop +
        MAX98357A) nach 250ms in "idle" → Track wird sofort übersprungen.
        Vermutlich Multi-Device-Race nach incomplete teardown des Vorgängers.
        """
        try:
            self._mpv.stop()
        except Exception:
            pass
        time.sleep(0.05)
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
        # Wenn ein Prompt per Knopfdruck abgebrochen wird, muss die User-
        # Lautstärke wieder greifen — sonst klebt das System-Volume auch an
        # der nächsten Musik-Wiedergabe.
        self._restore_after_prompt()
        logger.info("Stopped")

    def wait_until_idle(self, timeout: float = 8.0) -> None:
        """Blockt bis mpv die laufende Wiedergabe beendet hat (oder Timeout).

        Genutzt vom Bye-Prompt-Flow vor dem Poweroff: ohne dieses Warten
        würde systemctl poweroff mpv abschießen, bevor der Prompt fertig ist.
        Das kurze Anlauffenster (0.5s) deckt den Zeitraum zwischen play_file()
        und dem mpv-internen Wechsel idle→playing ab — sonst würde die
        Hauptwarte sofort zurückkehren, weil idle_active noch True ist.
        """
        start_deadline = time.monotonic() + 0.5
        while time.monotonic() < start_deadline:
            try:
                if not self._mpv.idle_active:
                    break
            except Exception:
                return
            time.sleep(0.05)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._mpv.idle_active:
                    return
            except Exception:
                return
            time.sleep(0.1)

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

    def set_speed(self, speed: float) -> None:
        """Wiedergabegeschwindigkeit (1.0 = normal). Nur fürs Speed-Mode-
        Easter-Egg gedacht — Kinder finden die Tonhöhenverschiebung lustig."""
        speed = max(0.25, min(4.0, float(speed)))
        try:
            self._mpv.speed = speed
        except Exception as e:
            logger.warning("set_speed failed: %s", e)

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

                # War das ein System-Prompt? Dann User-Lautstärke wieder her —
                # bevor der Callback ggf. einen neuen Track startet.
                was_prompt = self._prompt_active
                self._restore_after_prompt()

                # Callback-Aufruf außerhalb des Lock-State, damit der Callback
                # neue play_file()-Aufrufe machen kann. Bei Prompts gibt's keine
                # Playlist-Logik zu triggern — der Callback prüft selbst, ob er
                # was zu tun hat.
                cb = self._on_track_end
                if cb and not was_prompt:
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
