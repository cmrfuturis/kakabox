#!/usr/bin/env python3
"""
Kakabox — Hauptloop.

Eingaben:
    NFC-Tag auflegen → Backend-Lookup → spielt zugeordnete Lieder ab
    NFC-Tag entfernen → Wiedergabe stoppt; Position wird gemerkt für Resume
    🟢 Grün-Knopf       → Track zurück, oder Neustart wenn Track > 5s läuft (loop)
    🔴 Rot-Knopf        → Nächster Track (loop)
    🟦 Encoder-Push    → Pause/Play-Toggle
    🟦 Encoder im UZS  → Lauter
    🟦 Encoder gegen UZS → Leiser

Auto-Pairing:
    Server erkennt unbekannte Tags automatisch (auto_pairing_enabled),
    Provider-Tags kommen mit Name + Liedern aus dem Katalog.

Resume-on-Replace:
    Wird die zuletzt aktive Kaka kurz darauf wieder aufgelegt → läuft am
    gleichen Track + Position weiter. Andere Kaka → Memory wird verworfen.
"""
import json
import logging
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from audio import AudioCache, KakaContent, Playlist, PlaylistSnapshot
from audio.library import scan
from audio.player import Player
from hardware.audio_output import set_volume
from hardware.buttons import Buttons
from hardware.nfc import PN532
from hardware.rotary_encoder import Encoder as RotaryEncoder
from network import Backend, BackendError

# Optional: REST-API (von Max) — startet eine FastAPI parallel zum main-Loop.
# Wird best-effort geladen; falls Modul fehlt oder Port belegt ist, läuft die
# Box weiter ohne API.
try:
    from api.routes import start as start_api  # noqa: F401
except Exception as _api_err:
    start_api = None  # type: ignore

CONFIG_PATH = Path(__file__).parent / "config.json"
IDENTITY_PATH = Path(__file__).parent / "box_identity.json"
PROMPTS_DIR = Path("/usr/share/kakabox/prompts")  # vom Installer befüllt
VOLUME_STEP = 5            # Encoder-Klick = 5 Prozentpunkte
HEARTBEAT_INTERVAL = 30
AUDIO_SYNC_INTERVAL = 300  # 5 Minuten
TAG_REMOVAL_THRESHOLD = 2  # NFC: aufeinanderfolgende Leer-Reads bis "Chip entfernt"


def read_wifi_ssid() -> str | None:
    try:
        out = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    ssid = out.stdout.strip()
    return ssid or None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("kakabox")


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"volume": 70, "tags": {}, "parental": {"disabled_albums": []}}


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False))


@dataclass
class KakaMemory:
    """Zwischengespeicherter Wiedergabe-Stand der zuletzt entfernten Kaka."""
    tag_uid: str
    track_index: int
    position_seconds: float


class Kakabox:
    def __init__(self):
        logger.info("Starting Kakabox...")
        self.config = load_config()

        self.library = scan()
        logger.info(
            "Lokale Bibliothek: %d Alben, %d Tracks",
            len(self.library.albums),
            sum(len(a.tracks) for a in self.library.albums),
        )

        self.audio_cache = AudioCache()
        self.player = Player()

        self.nfc = PN532()
        self.buttons = self._safe_init("buttons", Buttons)
        self.encoder = self._safe_init("rotary encoder", RotaryEncoder)

        try:
            self.backend = Backend(IDENTITY_PATH)
            if not self.backend.ensure_connected():
                logger.warning(
                    "Nicht mit Backend verbunden — Tag-Scans nur lokal möglich."
                )
        except (BackendError, FileNotFoundError) as e:
            logger.warning("Backend deaktiviert: %s", e)
            self.backend = None

        self._volume = self.config.get("volume", 70)
        try:
            set_volume(self._volume)
        except Exception as e:
            logger.warning("ALSA volume control unavailable: %s", e)
        self.player.set_volume(self._volume)

        self._running = False
        self._current_playlist: Optional[Playlist] = None
        self._active_tag_uid: Optional[str] = None
        self._last_kaka_memory: Optional[KakaMemory] = None
        self._playlist_lock = threading.Lock()

        # Track-Ende-Callback an Player binden
        self.player.on_track_end(self._on_track_end)

        # Hardware-Inputs verdrahten
        self._wire_buttons()
        self._wire_encoder()

    @staticmethod
    def _safe_init(label: str, factory):
        try:
            return factory()
        except Exception as e:
            logger.warning("%s unavailable: %s — feature disabled", label, e)
            return None

    def _play_prompt(self, filename: str) -> None:
        """Spielt eine Boot-/Status-Ansage über den Player (gleiches ALSA-Device wie mpv).

        Wird verwendet bevor irgendeine Playlist läuft — der EOF-Callback des Players
        ist dann ein No-Op. Fehlt die Datei (Installer nicht durchgelaufen), wird
        leise übergangen.
        """
        path = PROMPTS_DIR / filename
        if not path.is_file():
            logger.debug("Prompt nicht gefunden: %s", path)
            return
        try:
            self.player.play_file(str(path), title=path.stem)
        except Exception as e:
            logger.warning("Prompt-Wiedergabe fehlgeschlagen (%s): %s", filename, e)

    # ------------------------------------------------------------------
    # Input-Verdrahtung
    # ------------------------------------------------------------------

    def _wire_buttons(self) -> None:
        if self.buttons is None:
            return
        self.buttons.on_green(self._on_green_pressed)
        self.buttons.on_red(self._on_red_pressed)
        self.buttons.on_red_held(self._on_red_held)
        self.buttons.on_push(self._on_push_pressed)

    def _wire_encoder(self) -> None:
        if self.encoder is None:
            return
        # gpiozero "clockwise" entspricht der physischen Drehung im Uhrzeigersinn
        # (mit CLK=GPIO17, DT=GPIO27 stimmt das hier; in einem früheren Test war
        # ich kurz verwirrt — diese Variante ist die richtige).
        self.encoder.on_clockwise(lambda: self._adjust_volume(+VOLUME_STEP))
        self.encoder.on_counterclockwise(lambda: self._adjust_volume(-VOLUME_STEP))

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True

        threading.Thread(target=self._nfc_loop, daemon=True, name="nfc").start()
        if self.backend and self.backend.is_connected:
            threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat").start()
            threading.Thread(target=self._audio_sync_loop, daemon=True, name="audio-sync").start()

        # REST-API (Max's Feature) optional starten. Auf Port 8001, damit der
        # Pi-Backend-Client (KAKABOX_BACKEND, default localhost:8000) weiterhin
        # mit der Laravel-Webapp auf 8000 sprechen kann ohne Konflikt. Wer die
        # API von außen ansprechen will, nutzt http://kakabox.local:8001.
        if start_api is not None:
            try:
                start_api(self, host="0.0.0.0", port=8001)
                logger.info("REST API started on http://0.0.0.0:8001")
            except Exception as e:
                logger.warning("REST API konnte nicht gestartet werden: %s", e)

        logger.info("Kakabox bereit. Chip auflegen oder Knopf drücken!")
        self._play_prompt("ready_to_rumble.wav")
        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    # ------------------------------------------------------------------
    # Hintergrund-Loops
    # ------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        self._send_heartbeat()
        while self._running:
            for _ in range(HEARTBEAT_INTERVAL * 10):
                if not self._running:
                    return
                time.sleep(0.1)
            self._send_heartbeat()

    def _audio_sync_loop(self) -> None:
        time.sleep(5)
        while self._running:
            self._sync_audio_manifest()
            for _ in range(AUDIO_SYNC_INTERVAL * 10):
                if not self._running:
                    return
                time.sleep(0.1)

    def _send_heartbeat(self) -> None:
        if not self.backend:
            return
        try:
            self.backend.heartbeat({
                "volume": self._volume,
                "wifi_ssid": read_wifi_ssid(),
            })
        except Exception as e:
            logger.warning("heartbeat failed: %s", e)

    def _sync_audio_manifest(self) -> None:
        if not self.backend or not self.backend.is_connected:
            return
        manifest = self.backend.audio_manifest()
        if not manifest:
            return

        files = manifest.get("manifest", [])
        files.sort(key=lambda m: 0 if m.get("priority") == "high" else 1)

        for entry in files:
            if not self._running:
                return
            content_id = entry.get("content_id")
            file_hash = entry.get("file_hash")
            if not content_id or not file_hash:
                continue
            if self.audio_cache.is_cached(content_id, file_hash):
                continue
            logger.info("Sync: lade '%s' (id=%d)", entry.get("title"), content_id)
            target = self.audio_cache.path_for(content_id)
            if self.backend.download_audio(content_id, target):
                actual_hash = self.audio_cache.compute_hash(target)
                if actual_hash != file_hash:
                    logger.error("Sync: Hash-Mismatch für content=%d — verworfen", content_id)
                    target.unlink(missing_ok=True)
                else:
                    self.backend.report_audio_cached(content_id, file_hash)

        total_mb, free_mb = self.audio_cache.storage_stats_mb()
        self.backend.report_storage(total_mb, free_mb)

    # ------------------------------------------------------------------
    # NFC-Loop (mit Multi-Chip-Tracking)
    # ------------------------------------------------------------------

    def _nfc_loop(self) -> None:
        seen_at: dict[str, float] = {}
        misses: dict[str, int] = {}
        active_uid: Optional[str] = None

        while self._running:
            try:
                uids = self.nfc.read_tags(timeout=0.5, max_targets=1)
            except Exception as e:
                logger.error("NFC error: %s", e)
                time.sleep(0.2)
                continue

            now = time.monotonic()
            current = set(uids)

            for uid in uids:
                if uid not in seen_at:
                    seen_at[uid] = now
                    if active_uid is not None and uid != active_uid:
                        logger.info("Chip %s zusätzlich erkannt — älterer aktiv.", uid)
                misses[uid] = 0

            for uid in list(seen_at.keys()):
                if uid in current:
                    continue
                misses[uid] = misses.get(uid, 0) + 1
                if misses[uid] >= TAG_REMOVAL_THRESHOLD:
                    del seen_at[uid]
                    misses.pop(uid, None)

            new_active = (
                min(seen_at.items(), key=lambda kv: (kv[1], kv[0]))[0]
                if seen_at else None
            )

            if new_active != active_uid:
                if new_active is None:
                    self._on_tag_removed(active_uid)
                else:
                    if active_uid is not None:
                        logger.info("Chip-Wechsel: %s → %s", active_uid, new_active)
                        self._on_tag_removed(active_uid)
                    self._handle_tag(new_active)
                active_uid = new_active

            time.sleep(0.2)

    # ------------------------------------------------------------------
    # Tag-Handling
    # ------------------------------------------------------------------

    def _handle_tag(self, uid: str) -> None:
        logger.info("NFC tag erkannt: %s", uid)

        if not self.backend or not self.backend.is_connected:
            self._fallback_local_lookup(uid)
            return

        try:
            response = self.backend.tag_scan(uid)
        except BackendError as e:
            logger.error("tag_scan error: %s", e)
            self._fallback_local_lookup(uid)
            return

        if response is None:
            logger.warning("Tag %s: Backend nicht erreichbar.", uid)
            self._fallback_local_lookup(uid)
            return

        status = response.get("status")
        kaka = response.get("kaka") or {}
        kaka_name = kaka.get("name", "?")

        if status == "play":
            logger.info("Tag %s → spiele '%s'", uid, kaka_name)
            self._start_kaka_playlist(uid, kaka)
        elif status == "paired":
            kind = response.get("kind", "?")
            logger.info("Tag %s angelernt (kind=%s, name='%s')", uid, kind, kaka_name)
            self._start_kaka_playlist(uid, kaka)
        elif status == "unknown":
            logger.info("Tag %s unbekannt. Auto-Pairing in der App aktivieren.", uid)
        elif status == "foreign_household":
            logger.warning("Tag %s gehört zu einem anderen Haushalt.", uid)
        else:
            logger.warning("Tag %s: unerwartete Backend-Antwort: %s", uid, response)

    def _start_kaka_playlist(self, uid: str, kaka: dict) -> None:
        contents_data = kaka.get("contents", [])
        if not contents_data:
            logger.info("Kaka '%s' hat noch keine Lieder.", kaka.get("name"))
            return

        contents = [
            KakaContent(
                content_id=c["id"],
                title=c.get("title", ""),
                file_hash=c.get("file_hash"),
                download_url=c.get("download_url"),
                cached_locally=bool(c.get("cached_locally")),
                sort_order=int(c.get("sort_order", 0)),
            )
            for c in contents_data
        ]

        # Resume? Wenn der gleiche Chip kurz vor uns dieselbe Playlist gespielt hat.
        start_index, start_position = self._compute_resume(uid)
        # Nach dem Lesen ist der Memory verbraucht (egal ob er gepasst hat)
        self._last_kaka_memory = None

        with self._playlist_lock:
            if self._current_playlist:
                self._current_playlist.stop()

            playlist = Playlist(
                contents=contents,
                cache=self.audio_cache,
                download_fn=lambda cid, path: self.backend.download_audio(cid, path),
                play_fn=self.player.play_file,
                stop_fn=self.player.stop,
                position_fn=self.player.current_position_seconds,
                seek_fn=self.player.seek_to,
            )
            self._current_playlist = playlist
            self._active_tag_uid = uid

        if not playlist.start(start_index=start_index, start_position=start_position):
            logger.warning("Konnte Playlist nicht starten.")

    def _compute_resume(self, uid: str) -> tuple[int, float]:
        """Wenn die zuletzt entfernte Kaka derselben UID = jetzt aufgelegt: resume."""
        mem = self._last_kaka_memory
        if mem and mem.tag_uid == uid:
            logger.info(
                "Resume: Track %d ab %.1fs (Chip war zuvor aufgelegt).",
                mem.track_index + 1, mem.position_seconds,
            )
            return mem.track_index, mem.position_seconds
        return 0, 0.0

    def _on_tag_removed(self, uid: Optional[str]) -> None:
        """Aktiver Chip vom Reader weg → Snapshot speichern + Wiedergabe stoppen."""
        with self._playlist_lock:
            playlist = self._current_playlist
            self._current_playlist = None
            removed_uid = self._active_tag_uid
            self._active_tag_uid = None

        if playlist and removed_uid:
            snapshot = playlist.snapshot()
            if snapshot:
                self._last_kaka_memory = KakaMemory(
                    tag_uid=removed_uid,
                    track_index=snapshot.track_index,
                    position_seconds=snapshot.position_seconds,
                )
                logger.info(
                    "Chip %s entfernt — gemerkt: Track %d ab %.1fs.",
                    removed_uid, snapshot.track_index + 1, snapshot.position_seconds,
                )
            playlist.stop()

        try:
            self.player.stop()
        except Exception as e:
            logger.warning("Player.stop fehlgeschlagen: %s", e)

    def _on_track_end(self) -> None:
        with self._playlist_lock:
            playlist = self._current_playlist
        if playlist:
            playlist.on_track_end()

    def _fallback_local_lookup(self, uid: str) -> None:
        album_id = self.config["tags"].get(uid)
        if not album_id:
            return
        disabled = self.config.get("parental", {}).get("disabled_albums", [])
        if album_id in disabled:
            logger.info("Album '%s' deaktiviert.", album_id)
            return
        album = self.library.find_album(album_id)
        if not album:
            return
        logger.info("Offline-Modus: spiele lokales Album '%s'", album.name)
        self.player.play_album(album)

    # ------------------------------------------------------------------
    # Knopf-Handler
    # ------------------------------------------------------------------

    def _on_green_pressed(self) -> None:
        """Grün: Track zurück, oder Neustart wenn schon > 5s gelaufen, mit Loop."""
        logger.info("🟢 Grün")
        with self._playlist_lock:
            playlist = self._current_playlist
        if playlist:
            playlist.previous()

    def _on_red_pressed(self) -> None:
        """Rot kurz: Nächster Track mit Loop."""
        logger.info("🔴 Rot")
        with self._playlist_lock:
            playlist = self._current_playlist
        if playlist:
            playlist.next()

    def _on_red_held(self) -> None:
        """Rot ≥ 10s: WLAN-Profile löschen + Reboot → Box kommt im Hotspot-Modus hoch.

        Die eigentliche privilegierte Arbeit (nmcli delete + reboot) macht das
        Helper-Script /usr/local/bin/kakabox-wifi-nuke. Sudoers-Drop-in
        /etc/sudoers.d/kakabox erlaubt riffi NOPASSWD nur für genau diesen Pfad.
        """
        logger.warning("🔴🔴🔴 Rot 10s gehalten — WLAN-Reset wird ausgelöst.")
        try:
            subprocess.run(
                ["sudo", "-n", "/usr/local/bin/kakabox-wifi-nuke"],
                check=False, timeout=10,
            )
        except Exception as e:
            logger.error("Reset fehlgeschlagen: %s", e)

    def _on_push_pressed(self) -> None:
        """Encoder-Druck: Pause/Resume-Toggle."""
        logger.info("🟦 Push (Pause/Play)")
        self.player.toggle_pause()

    def _adjust_volume(self, delta: int) -> None:
        new_vol = max(0, min(100, self._volume + delta))
        if new_vol == self._volume:
            return
        self._volume = new_vol
        try:
            set_volume(new_vol)
        except Exception as e:
            logger.warning("ALSA volume control unavailable: %s", e)
        self.player.set_volume(new_vol)
        self.config["volume"] = new_vol
        save_config(self.config)
        logger.info("🔊 Volume: %d%%", new_vol)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        logger.info("Shutting down...")
        self._running = False
        with self._playlist_lock:
            if self._current_playlist:
                self._current_playlist.stop()
        self.player.stop()
        self.nfc.close()
        if self.buttons is not None:
            self.buttons.close()
        if self.encoder is not None:
            self.encoder.close()
        save_config(self.config)
        logger.info("Bye.")


def main() -> None:
    box = Kakabox()

    def _sig_handler(sig, frame):
        box._running = False

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    box.run()


if __name__ == "__main__":
    main()
