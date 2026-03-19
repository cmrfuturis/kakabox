#!/usr/bin/env python3
"""
Kakabox — main event loop.

NFC tag    → play mapped album
Gesture LEFT / RIGHT  → previous / next track
Gesture UP  / DOWN    → volume up / down
Gesture WAVE          → cycle encoder effect mode (SPEED → PITCH → VINYL)
Encoder rotation      → control current audio effect
"""
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from audio.effects import AudioEffects
from audio.library import scan
from audio.player import Player
from hardware.audio_output import set_volume
from hardware.encoder import Encoder
from hardware.gesture import Gesture, GestureSensor
from hardware.nfc import PN532

CONFIG_PATH = Path(__file__).parent / "config.json"
VOLUME_STEP = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("kakabox")


# ----------------------------------------------------------------------
# Config helpers
# ----------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"volume": 70, "tags": {}, "parental": {"disabled_albums": []}}


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False))


# ----------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------

class Kakabox:
    def __init__(self):
        logger.info("Starting Kakabox...")

        self.config = load_config()

        self.library = scan()
        logger.info(
            "Library: %d albums, %d tracks",
            len(self.library.albums),
            sum(len(a.tracks) for a in self.library.albums),
        )

        self.player = Player()
        self.effects = AudioEffects(self.player._mpv)

        self.nfc     = PN532()
        self.gesture = GestureSensor()
        self.encoder = Encoder()

        self._volume = self.config.get("volume", 70)
        set_volume(self._volume)
        self.player.set_volume(self._volume)

        self._last_tag: str | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True

        nfc_thread = threading.Thread(target=self._nfc_loop, daemon=True)
        nfc_thread.start()

        logger.info("Kakabox ready. Tap a tag or wave your hand!")

        try:
            self._main_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    def _main_loop(self) -> None:
        """Fast loop — gesture + encoder at ~100 ms."""
        while self._running:
            self._handle_gesture()
            self._handle_encoder()
            time.sleep(0.1)

    def _nfc_loop(self) -> None:
        """Background loop — NFC polling."""
        while self._running:
            try:
                uid = self.nfc.read_tag(timeout=0.5)
                if uid and uid != self._last_tag:
                    self._last_tag = uid
                    self._handle_tag(uid)
                elif not uid:
                    self._last_tag = None   # tag removed — allow re-tap
            except Exception as e:
                logger.error("NFC error: %s", e)
            time.sleep(0.2)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_tag(self, uid: str) -> None:
        logger.info("NFC tag detected: %s", uid)

        album_id = self.config["tags"].get(uid)
        if not album_id:
            logger.info("Tag %s is not mapped. Use the app to assign it.", uid)
            return

        disabled = self.config.get("parental", {}).get("disabled_albums", [])
        if album_id in disabled:
            logger.info("Album '%s' is disabled by parental controls.", album_id)
            return

        album = self.library.find_album(album_id)
        if not album:
            logger.warning("Album '%s' not found in library.", album_id)
            return

        logger.info("Playing: %s", album.name)
        self.effects.reset()
        self.player.play_album(album)

    def _handle_gesture(self) -> None:
        try:
            g = self.gesture.read()
        except Exception as e:
            logger.error("Gesture error: %s", e)
            return

        if g == Gesture.NONE:
            return

        if g == Gesture.LEFT:
            logger.info("Gesture: PREVIOUS track")
            self.player.previous_track()

        elif g == Gesture.RIGHT:
            logger.info("Gesture: NEXT track")
            self.player.next_track()

        elif g == Gesture.UP:
            self._set_volume(self._volume + VOLUME_STEP)

        elif g == Gesture.DOWN:
            self._set_volume(self._volume - VOLUME_STEP)

        elif g == Gesture.WAVE:
            mode = self.effects.next_mode()
            logger.info("Effect mode → %s", mode.name)

    def _handle_encoder(self) -> None:
        try:
            delta = self.encoder.read_delta()
        except Exception as e:
            logger.error("Encoder error: %s", e)
            return
        self.effects.apply_delta(delta)

    def _set_volume(self, volume: int) -> None:
        volume = max(0, min(100, volume))
        self._volume = volume
        set_volume(volume)
        self.player.set_volume(volume)
        self.config["volume"] = volume
        save_config(self.config)
        logger.info("Volume: %d%%", volume)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        logger.info("Shutting down...")
        self._running = False
        self.player.stop()
        self.nfc.close()
        self.gesture.close()
        self.encoder.close()
        save_config(self.config)
        logger.info("Bye.")


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def main() -> None:
    box = Kakabox()

    def _sig_handler(sig, frame):
        box._running = False

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    box.run()


if __name__ == "__main__":
    main()
