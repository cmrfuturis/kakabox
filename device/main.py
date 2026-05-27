#!/usr/bin/env python3
"""
Kakabox — Hauptloop.

Eingaben:
    NFC-Tag auflegen   → Backend-Lookup → spielt zugeordnete Lieder ab
    NFC-Tag entfernen  → Wiedergabe stoppt; Position wird gemerkt für Resume
    🟢 Grün-Knopf       → Track zurück, oder Neustart wenn Track > 5s läuft (loop)
    🟢 Grün ≥ 1s        → STOP: Wiedergabe beenden + Resume-Position vergessen
    🟢 Grün ≥ 5s        → Box ausschalten (poweroff, tschau-Kakau-Prompt)
    🔴 Rot-Knopf        → Nächster Track (loop)
    🔴 Rot ≥ 1s         → STOP: Wiedergabe beenden + Resume-Position vergessen
    🔴 Rot ≥ 5s         → WLAN-Profile löschen (kein Reboot — Box bleibt an,
                          comitup öffnet Hotspot zum Re-Onboarding)
    🟦 Encoder-Push      → Pause/Play-Toggle
    🟦 Encoder-Push ≥ 1s → Voice-Push-to-Talk ("spiele bitte XY")
    🟦 Encoder im UZS    → Lauter
    🟦 Encoder gegen UZS → Leiser

Auto-Pairing:
    Server erkennt unbekannte Tags automatisch (auto_pairing_enabled),
    Provider-Tags kommen mit Name + Liedern aus dem Katalog.

Resume-on-Replace:
    Wird die zuletzt aktive Kaka kurz darauf wieder aufgelegt → läuft am
    gleichen Track + Position weiter. Andere Kaka → Memory wird verworfen.
"""
import hashlib
import json
import logging
import os
import random
import secrets
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
from audio.spectrum import SpectrumCapture
from hardware.audio_output import set_volume
from hardware.buttons import Buttons
from hardware.leds import Leds, LedsUnavailable
from hardware.nfc import PN532
from hardware.rotary_encoder import Encoder as RotaryEncoder
from network import Backend, BackendError
from voice.asr import VoiceUnavailable, build_recognizer
from voice.catalog import build_catalog_from_file
from voice.intent import Candidate, has_magic_word, parse_play_command
from voice.recorder import MicRecorder, RecorderError

# Optional: REST-API (von Max) — startet eine FastAPI parallel zum main-Loop.
# Wird best-effort geladen; falls Modul fehlt oder Port belegt ist, läuft die
# Box weiter ohne API.
try:
    from api.routes import start as start_api  # noqa: F401
except Exception as _api_err:
    start_api = None  # type: ignore

CONFIG_PATH = Path(__file__).parent / "config.json"
IDENTITY_PATH = Path(__file__).parent / "box_identity.json"
TAG_CACHE_PATH = Path(__file__).parent / "tag_cache.json"
VOICE_CATALOG_PATH = Path(__file__).parent / "voice_catalog.json"
PROMPTS_DIR = Path("/usr/share/kakabox/prompts")  # vom Installer befüllt
APLAY_PROMPT_PID = Path("/run/kakabox/prompt_pid")  # vom Comitup-Callback geschrieben
VOLUME_STEP = 5            # Encoder-Klick = 5 Prozentpunkte
HEARTBEAT_INTERVAL = 30
AUDIO_SYNC_INTERVAL = 300  # 5 Minuten
SYNC_RETRY_BACKOFF_SECONDS = 3600  # 1h: failed Downloads nicht jeden Zyklus retry'en
                                   # (verhindert Log-Spam bei kaputten Backend-Storage-IDs)
TAG_REMOVAL_THRESHOLD = 2  # NFC: aufeinanderfolgende Leer-Reads bis "Chip entfernt"

# Geheimer Speed-Mode (Easter Egg): 4× Encoder-Push in 3s während Wiedergabe →
# danach steuert der Encoder die Wiedergabegeschwindigkeit statt Lautstärke.
# Exit: nochmal Push, oder Chip vom Reader nehmen.
SPEED_BURST_COUNT = 4
SPEED_BURST_WINDOW = 3.0
SPEED_STEP = 0.1
SPEED_MIN = 0.5
SPEED_MAX = 2.0

# Voice-Push-to-Talk: Encoder ≥ 1s gehalten → Padamm → Aufnehmen → Match.
# VAD-light bricht die Aufnahme automatisch ab, sobald 1s am Stück Stille
# (nach erster erkannter Sprache) erreicht ist — sonst hartes Cap bei 5s,
# damit die Box nicht endlos auf jemanden wartet, der gerade gar nichts sagt.
VOICE_MAX_SECONDS = 5.0
VOICE_SILENCE_SECONDS = 1.0


def _kill_aplay_prompt() -> bool:
    """Bricht einen WLAN-Status-Prompt ab, der per ``aplay`` aus dem Comitup-
    Callback läuft (setup_active.wav / wifi_connected.wav).

    Der Callback schreibt seinen aplay-PID in /run/kakabox/prompt_pid; wir
    lesen ihn, schicken SIGTERM und räumen die Datei. Gibt True zurück, wenn
    tatsächlich ein Prompt gekillt wurde (Button-Handler nutzen den Wert, um
    zu wissen, dass der Druck "verbraucht" ist).
    """
    if not APLAY_PROMPT_PID.exists():
        return False
    try:
        pid_text = APLAY_PROMPT_PID.read_text().strip()
        pid = int(pid_text) if pid_text else 0
    except (OSError, ValueError):
        APLAY_PROMPT_PID.unlink(missing_ok=True)
        return False
    if pid <= 0:
        APLAY_PROMPT_PID.unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        logger.info("WLAN-Prompt (aplay pid=%d) per Knopfdruck abgebrochen.", pid)
        APLAY_PROMPT_PID.unlink(missing_ok=True)
        return True
    except ProcessLookupError:
        APLAY_PROMPT_PID.unlink(missing_ok=True)
        return False
    except PermissionError as e:
        logger.warning("Kein Recht zum Killen von aplay pid=%d: %s", pid, e)
        return False
    except Exception as e:
        logger.warning("Konnte aplay-Prompt nicht killen: %s", e)
        return False


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


DEFAULT_SYSTEM_VOLUME = 25  # Lautstärke für Boot-/WLAN-/Bye-Prompts (gedämpft, User-Wunsch — laute Default-Ansagen erschrecken)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {
        "volume": 70,
        "system_volume": DEFAULT_SYSTEM_VOLUME,
        "tags": {},
        "parental": {"disabled_albums": []},
    }


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False))


def kaka_fingerprint(kaka: dict) -> str:
    """Stabiler Hash über die abspielrelevanten Felder einer Kaka.

    Reihenfolge, Hinzufügen oder Entfernen eines Liedes ändert den Hash —
    stimmt der Server-Fingerprint mit dem lokalen überein, kann der Tag-
    Cache unverändert bleiben (kein Resync).
    """
    items = [
        (
            int(c.get("id") or 0),
            int(c.get("sort_order") or 0),
            (c.get("file_hash") or "").lower(),
        )
        for c in kaka.get("contents") or []
    ]
    items.sort()
    payload = json.dumps(items, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        self._ensure_api_token()
        self._tag_cache = self._load_tag_cache()

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
        # LEDs: optional — wenn Adafruit-Lib oder Pi-5-Backend fehlt, läuft die
        # Box ohne visuelles Feedback weiter. WICHTIG: solange DIN auf GPIO 18
        # liegt, blockiert die LED-Init den MAX98357A-Speaker (selber Pin).
        # Geplanter Fix: DIN auf GPIO 10 (SPI MOSI) umlöten → Pi5Neo statt
        # Adafruit; dann läuft beides parallel.
        # Bis dahin: Override via Env-Var, um Speaker für Audio-Tests freizugeben
        # (Service läuft dann mit Knöpfen + Speaker, nur ohne LED-Feedback).
        if os.environ.get("KAKABOX_DISABLE_LEDS") == "1":
            logger.info("LEDs deaktiviert via KAKABOX_DISABLE_LEDS — Speaker frei.")
            self.leds = None
        else:
            self.leds = self._safe_init("leds", Leds)
            if self.leds is not None:
                logger.info("LEDs initialisiert (Pi5Neo / SPI MOSI / GPIO 10)")

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
        self._system_volume = int(self.config.get("system_volume", DEFAULT_SYSTEM_VOLUME))
        # max_volume = HARD-Cap für die User-Lautstärke. Webapp kann per
        # rule.max_volume im Manifest einen Wert vorgeben (Eltern-Schutz).
        # Default 100 = kein Cap. Wird in _apply_rule_from_manifest gepflegt.
        self._max_volume = int(self.config.get("max_volume", 100))
        # Falls die persistierte volume schon über dem Cap liegt (z.B. weil
        # max_volume neu gesetzt wurde während die Box offline war), gleich
        # beim Boot klemmen.
        if self._volume > self._max_volume:
            logger.info(
                "Boot-Volume %d über Cap %d — auf Cap geklemmt.",
                self._volume, self._max_volume,
            )
            self._volume = self._max_volume
            self.config["volume"] = self._volume
        # MAX98357A hat keinen Hardware-Mixer → kein amixer-Call. Lautstärke
        # wird ausschließlich über mpv softvol gesteuert.
        self.player.set_volume(self._volume)

        self._running = False
        self._current_playlist: Optional[Playlist] = None
        self._active_tag_uid: Optional[str] = None
        self._last_kaka_memory: Optional[KakaMemory] = None
        self._playlist_lock = threading.Lock()

        # Speed-Mode-State (siehe SPEED_* Konstanten)
        self._speed_mode = False
        self._speed = 1.0
        self._push_times: list[float] = []

        # Random-Mode: Encoder-Push ≥ 1s startet eine zufällige Playlist aus
        # dem ganzen lokalen Audio-Cache (Lieder ohne Chip auflegen). Tag-
        # Auflegen unterbricht den Modus zugunsten der Tag-Playlist; Tag-
        # Wegnehmen geht in Ruhe (kein Auto-Random), Hold im Random startet
        # die Session neu (neue Reihenfolge).
        self._random_mode = False

        # Voice-Mode: True während ein per Sprache erkannter Track läuft.
        # Continue-Logik beim Voice-Track-Ende:
        #   - _voice_pending_tag_uid noch aktiv (Tag liegt noch drauf):
        #     Kakafigur-Wiedergabe wieder von vorne starten
        #   - sonst: Random-Modus
        # Tag-Removal während Voice → nur pending_uid clearen, Voice spielt durch.
        # Tag-Auflegen während Voice → normale Kakafigur-Logik (überschreibt Voice).
        self._voice_mode = False
        self._voice_pending_tag_uid: Optional[str] = None
        # Letztes Voice-Target, falls User per Grün den Voice-Track neu starten will.
        self._voice_last_target: Optional[Candidate] = None

        # Backoff-Map für Sync: content_id → time.monotonic() des letzten
        # Failures. Verhindert dass die Box jeden Sync-Zyklus erneut
        # Downloads für IDs versucht, die das Backend mit 404 abweist
        # (Backend-Storage-Inkonsistenz). Nach SYNC_RETRY_BACKOFF_SECONDS
        # darf jede ID erneut probiert werden — falls der Backend-Admin
        # die Datei zwischenzeitlich nachgereicht hat.
        self._sync_failures: dict[int, float] = {}

        # Spectrum-Capture: liest fortlaufend Audio vom snd-aloop Loopback
        # und füttert die LED-Streifen mit Frequenzbändern (audio-reaktiver
        # Tanz). Wird optional initialisiert — wenn snd-aloop / asound.conf
        # fehlt, läuft die Box weiter, nur die Streifen tanzen dann Pseudo.
        # Energy-Save: ``_spectrum_active`` event steuert ob der arecord-
        # Subprozess wirklich läuft. An: Wiedergabe spielt → Streifen tanzen.
        # Aus: Idle (kein Tag, kein Random, kein Voice-Track) → arecord stoppt,
        # FFT pausiert, CPU runter.
        self._spectrum: Optional[SpectrumCapture] = None
        self._spectrum_thread: Optional[threading.Thread] = None
        self._spectrum_stop = threading.Event()
        self._spectrum_active = threading.Event()
        if self.leds is not None:
            try:
                self._spectrum = SpectrumCapture(n_bands=16)
            except Exception:
                logger.exception("SpectrumCapture-Init fehlgeschlagen — Pseudo-Tanz bleibt")

        # Voice-Stack. Backend (vosk|whisper) kommt aus config.json → "voice.backend".
        # Recognizer instanziieren ist billig; das eigentliche Modell wird in einem
        # Daemon-Thread vorgeladen (Warmup), sodass die erste Push-to-Talk-Session
        # nicht auf den 1–3 s Modell-Load warten muss. Schlägt der Warmup fehl
        # (Paket/Modell fehlt), bleibt der bestehende Lazy-Load-Pfad in
        # transcribe_wav als Fallback aktiv und der echte Push-to-Talk wirft den
        # Fehler dann sichtbar.
        self._recognizer = build_recognizer(self.config.get("voice"))
        threading.Thread(
            target=self._warmup_recognizer, daemon=True, name="asr-warmup"
        ).start()
        self._mic_recorder = MicRecorder()
        self._voice_lock = threading.Lock()  # nur eine Voice-Session gleichzeitig

        # Track-Ende-Callback an Player binden
        self.player.on_track_end(self._on_track_end)

        # Hardware-Inputs verdrahten
        self._wire_buttons()
        self._wire_encoder()

    def _load_tag_cache(self) -> dict:
        if not TAG_CACHE_PATH.exists():
            return {}
        try:
            data = json.loads(TAG_CACHE_PATH.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Tag-Cache nicht ladbar: %s — starte leer.", e)
            return {}

    def _save_tag_cache(self) -> None:
        try:
            TAG_CACHE_PATH.write_text(
                json.dumps(self._tag_cache, indent=2, ensure_ascii=False)
            )
        except OSError as e:
            logger.warning("Tag-Cache speichern fehlgeschlagen: %s", e)

    def _update_tag_cache(self, uid: str, kaka: dict) -> None:
        """Cache-Eintrag schreiben, wenn sich der Fingerprint geändert hat.

        Stimmt Server- und Local-Fingerprint überein → keine Schreibarbeit.
        Bei Änderung wird der Eintrag komplett ersetzt; das deckt Reihenfolge,
        Hinzufügen und Löschen einzelner Lieder ab.
        """
        fingerprint = kaka_fingerprint(kaka)
        entry = self._tag_cache.get(uid)
        if entry and entry.get("fingerprint") == fingerprint:
            return
        self._tag_cache[uid] = {
            "fingerprint": fingerprint,
            "kaka": {
                "name": kaka.get("name", ""),
                "contents": kaka.get("contents", []),
            },
        }
        self._save_tag_cache()
        logger.info(
            "Tag-Cache: %s aktualisiert (%d Lieder, fingerprint=%s…)",
            uid, len(kaka.get("contents") or []), fingerprint[:8],
        )

    def _drop_tag_cache(self, uid: str) -> None:
        if self._tag_cache.pop(uid, None) is not None:
            self._save_tag_cache()
            logger.info("Tag-Cache: %s entfernt", uid)

    def _ensure_api_token(self) -> None:
        """Erzeugt einmalig einen Bearer-Token für die lokale REST-API.

        Ohne Token wäre die FastAPI auf Port 8001 für jeden im Heim-WLAN voll
        steuerbar (inkl. parental-Override) — siehe api/routes.py. 32 urlsafe-
        Bytes (~256 bit) reichen, der Token bleibt für die Lifetime der Box
        in config.json liegen.
        """
        if self.config.get("api_token"):
            return
        self.config["api_token"] = secrets.token_urlsafe(32)
        save_config(self.config)
        logger.info("Neuer API-Token in config.json angelegt.")

    @staticmethod
    def _safe_init(label: str, factory):
        try:
            return factory()
        except Exception as e:
            logger.warning("%s unavailable: %s — feature disabled", label, e)
            return None

    def _play_prompt(self, filename: str, volume: Optional[int] = None) -> None:
        """Spielt eine Boot-/Status-Ansage über den Player (gleiches ALSA-Device wie mpv).

        ``volume=None`` (Default) → nutzt ``self._volume`` (die aktuell vom
        User per Encoder gewählte Lautstärke). System-Prompts (Boot, WLAN-
        Reset, Tschau, Listening, Zauberwort) sollen sich so anhören wie die
        Musik, statt einen separaten gedämpften Pegel zu nutzen — sonst
        kommt manchen User der Boot-Sound zu leise vor, anderen zu laut.
        ``volume=<int>`` → expliziter Override (z.B. für system_volume,
        wenn ein Prompt mal anders sein soll).
        """
        path = PROMPTS_DIR / filename
        if not path.is_file():
            logger.debug("Prompt nicht gefunden: %s", path)
            return
        actual_volume = volume if volume is not None else self._volume
        try:
            self.player.play_prompt(str(path), actual_volume)
        except Exception as e:
            logger.warning("Prompt-Wiedergabe fehlgeschlagen (%s): %s", filename, e)

    # ------------------------------------------------------------------
    # Input-Verdrahtung
    # ------------------------------------------------------------------

    def _wire_buttons(self) -> None:
        if self.buttons is None:
            return
        self.buttons.on_green(self._on_green_pressed)
        self.buttons.on_green_stop(self._on_green_stop)
        self.buttons.on_green_held(self._on_green_held)
        self.buttons.on_red(self._on_red_pressed)
        self.buttons.on_red_stop(self._on_red_stop)
        self.buttons.on_red_held(self._on_red_held)
        self.buttons.on_push(self._on_push_pressed)
        self.buttons.on_push_held(self._on_push_held)
        self.buttons.on_yellow(self._on_yellow_pressed)
        self.buttons.on_blue(self._on_blue_pressed)

    def _wire_encoder(self) -> None:
        if self.encoder is None:
            return
        # gpiozero "clockwise" entspricht der physischen Drehung im Uhrzeigersinn
        # (mit CLK=GPIO17, DT=GPIO27 stimmt das hier; in einem früheren Test war
        # ich kurz verwirrt — diese Variante ist die richtige).
        # Im Speed-Mode steuert der Encoder die Wiedergabegeschwindigkeit
        # statt der Lautstärke — siehe _on_encoder_*.
        self.encoder.on_clockwise(self._on_encoder_cw)
        self.encoder.on_counterclockwise(self._on_encoder_ccw)

    def _on_encoder_cw(self) -> None:
        if self._speed_mode:
            self._adjust_speed(+SPEED_STEP)
        else:
            self._adjust_volume(+VOLUME_STEP)

    def _on_encoder_ccw(self) -> None:
        if self._speed_mode:
            self._adjust_speed(-SPEED_STEP)
        else:
            self._adjust_volume(-VOLUME_STEP)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True

        threading.Thread(target=self._nfc_loop, daemon=True, name="nfc").start()
        if self.backend and self.backend.is_connected:
            threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat").start()
            threading.Thread(target=self._audio_sync_loop, daemon=True, name="audio-sync").start()

        # Spectrum-Capture läuft dauerhaft im Hintergrund. arecord vom
        # Loopback blockt sauber, wenn gerade nichts gespielt wird (sendet
        # Stille / 0-Werte) — kein Schaden. LEDs nehmen die Werte nur an,
        # wenn ihre eigene Tanz-Animation aktiv ist (siehe leds.update_spectrum).
        if self._spectrum is not None:
            self._spectrum_stop.clear()
            self._spectrum_thread = threading.Thread(
                target=self._spectrum_loop, daemon=True, name="spectrum"
            )
            self._spectrum_thread.start()

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

        now = time.monotonic()
        cached_already = 0
        downloaded = 0
        failed_new = 0
        skipped_backoff = 0

        for entry in files:
            if not self._running:
                return
            content_id = entry.get("content_id")
            file_hash = entry.get("file_hash")
            if not content_id or not file_hash:
                continue
            if self.audio_cache.is_cached(content_id, file_hash):
                cached_already += 1
                continue
            # Backoff: wenn der Download vor < SYNC_RETRY_BACKOFF_SECONDS
            # bereits fehlgeschlagen ist, nicht nochmal versuchen. Spart
            # Log-Spam + Bandbreite, wenn das Backend dauerhaft 404 liefert.
            last_fail = self._sync_failures.get(content_id)
            if last_fail and now - last_fail < SYNC_RETRY_BACKOFF_SECONDS:
                skipped_backoff += 1
                continue

            logger.debug("Sync: lade '%s' (id=%d)", entry.get("title"), content_id)
            target = self.audio_cache.path_for(content_id)
            if self.backend.download_audio(content_id, target):
                actual_hash = self.audio_cache.compute_hash(target)
                if actual_hash != file_hash:
                    logger.error("Sync: Hash-Mismatch für content=%d — verworfen", content_id)
                    target.unlink(missing_ok=True)
                    self._sync_failures[content_id] = now
                    failed_new += 1
                else:
                    self.backend.report_audio_cached(content_id, file_hash)
                    self._sync_failures.pop(content_id, None)
                    downloaded += 1
            else:
                self._sync_failures[content_id] = now
                failed_new += 1

        # Eine kompakte Summary-Zeile statt 70+ einzelne Warnings.
        # Nur loggen wenn was passiert ist UND was zusagen ist.
        if downloaded or failed_new:
            logger.info(
                "Sync: %d neu geladen, %d fehlgeschlagen, %d bereits gecached, %d in Backoff",
                downloaded, failed_new, cached_already, skipped_backoff,
            )

        # Veraltete Dateien entfernen — der Manifest ist die Quelle der Wahrheit.
        # So werden Lieder, die in der Web-App aus allen Kakas entfernt wurden,
        # auch lokal aufgeräumt.
        keep_ids = {
            entry.get("content_id")
            for entry in files
            if entry.get("content_id")
        }
        deleted = self.audio_cache.cleanup(keep_ids)
        if deleted:
            logger.info("Sync: %d veraltete Audio-Dateien entfernt", deleted)

        total_mb, free_mb = self.audio_cache.storage_stats_mb()
        self.backend.report_storage(total_mb, free_mb)

        # Voice-Catalog aus dem Manifest neu schreiben — pro Song Titel + Aliase.
        # Wird vom voice/-Modul gelesen, damit "spiele eiskönigin" auch
        # Backend-Songs trifft (nicht nur die lokale Library).
        self._write_voice_catalog(files)

        # Per-Box-Settings aus dem Backend übernehmen (z.B. system_volume vom
        # Webapp-Override). Fehlt das Feld → bei aktuellem Wert bleiben.
        self._apply_settings_from_manifest(manifest.get("settings") or {})
        # Box-Rule (z.B. max_volume Hard-Cap) — separater Block weil
        # semantisch was anderes als die Settings.
        self._apply_rule_from_manifest(manifest.get("rule") or {})

    def _write_voice_catalog(self, files: list[dict]) -> None:
        """Schreibt eine kompakte Liste (content_id, title, aliases) für Voice-Match.

        Best-effort: bei IO-Fehlern (read-only FS, Disk voll) wird nur gewarnt —
        Voice-Match fällt dann auf den letzten gültigen Stand zurück.
        """
        songs = []
        for entry in files:
            cid = entry.get("content_id")
            if not cid:
                continue
            songs.append({
                "content_id": int(cid),
                "title": entry.get("title") or "",
                "aliases": list(entry.get("aliases") or []),
            })
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "songs": songs,
        }
        try:
            VOICE_CATALOG_PATH.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("Voice-Catalog konnte nicht geschrieben werden: %s", e)

    def _apply_settings_from_manifest(self, settings: dict) -> None:
        """Übernimmt Per-Box-Settings (aktuell: system_volume) aus dem Manifest.

        Override-Logik: Webapp-Wert gewinnt, wird in config.json gespiegelt
        damit ein Neustart nach Offline-Phase den letzten Stand behält. Fehlende
        oder ungültige Werte werden ignoriert (kein Reset auf Default).
        """
        sys_vol = settings.get("system_volume")
        if sys_vol is None:
            return
        try:
            sys_vol = int(sys_vol)
        except (TypeError, ValueError):
            return
        sys_vol = max(0, min(100, sys_vol))
        if sys_vol == self._system_volume:
            return
        logger.info("system_volume vom Backend: %d → %d", self._system_volume, sys_vol)
        self._system_volume = sys_vol
        self.config["system_volume"] = sys_vol
        save_config(self.config)

    def _apply_rule_from_manifest(self, rule: dict) -> None:
        """Übernimmt ``rule.max_volume`` aus dem Manifest als Hard-Cap.

        Der Cap begrenzt, wie weit der User die Lautstärke per Encoder
        hochdrehen kann (Eltern-Schutz). Wenn die aktuelle Lautstärke nach
        einem strenger werdenden Cap zu laut ist, wird sie sofort
        runtergezogen — sonst bliebe die laufende Wiedergabe ungebremst.
        Wird in config.json gespiegelt, damit Offline-Boot den letzten
        Stand behält.
        """
        max_vol = rule.get("max_volume")
        if max_vol is None:
            return
        try:
            max_vol = int(max_vol)
        except (TypeError, ValueError):
            logger.warning("rule.max_volume nicht numerisch: %r", max_vol)
            return
        max_vol = max(0, min(100, max_vol))
        if max_vol == self._max_volume:
            return
        logger.info("max_volume vom Backend: %d → %d", self._max_volume, max_vol)
        self._max_volume = max_vol
        self.config["max_volume"] = max_vol
        save_config(self.config)
        # Aktuelle Lautstärke über dem neuen Cap? Sofort runter — mit dem
        # üblichen _adjust_volume-Pfad, damit Player + LEDs konsistent sind.
        if self._volume > self._max_volume:
            self._adjust_volume(self._max_volume - self._volume)

    # ------------------------------------------------------------------
    # NFC-Loop (mit Multi-Chip-Tracking)
    # ------------------------------------------------------------------

    def _nfc_loop(self) -> None:
        seen_at: dict[str, float] = {}
        misses: dict[str, int] = {}
        active_uid: Optional[str] = None

        while self._running:
            try:
                uids = self.nfc.read_tags(timeout=0.2, max_targets=1)
            except Exception as e:
                # Hardware-Glitch (I2C busy, Timeout, …) darf nicht dazu
                # führen, dass die Box weiterspielt obwohl der Chip schon
                # weg ist. Behandle wie "kein Tag gesehen" — die Misses
                # zählen unten regulär weiter.
                logger.warning("NFC error: %s — werte als leere Lesung", e)
                uids = []

            now = time.monotonic()
            current = set(uids)
            logger.debug("NFC poll: uids=%s seen=%s active=%s", uids, list(seen_at), active_uid)

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
                    logger.info("NFC: Chip %s nach %d Misses verloren.", uid, misses[uid])
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

            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Tag-Handling
    # ------------------------------------------------------------------

    def _handle_tag(self, uid: str) -> None:
        logger.info("NFC tag erkannt: %s", uid)
        # Status-LED sofort grün — visuelles Feedback bevor wir Cache/Backend
        # befragen, damit der User direkt sieht "ja, Chip wurde gelesen".
        if self.leds is not None:
            self.leds.nfc_chip_present()

        # Cache-first: bekannter Tag mit allen Audios lokal → sofort spielen,
        # Backend-Sync läuft im Hintergrund (siehe _refresh_tag_in_background).
        # So entfällt der HTTP-Roundtrip im User-kritischen Pfad — auf einem Pi
        # über WLAN macht das den Unterschied zwischen ~0.5 s und mehreren
        # Sekunden bis zum ersten Ton.
        cached = self._tag_cache.get(uid)
        if cached and self._kaka_fully_local(cached.get("kaka") or {}):
            kaka = cached["kaka"]
            logger.info("Cache-Hit für %s → spiele '%s' sofort.", uid, kaka.get("name", "?"))
            self._start_kaka_playlist(uid, kaka)
            if self.backend and self.backend.is_connected:
                threading.Thread(
                    target=self._refresh_tag_in_background,
                    args=(uid,),
                    daemon=True,
                    name="tag-refresh",
                ).start()
            return

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
            logger.warning("Tag %s: Backend nicht erreichbar — versuche Cache.", uid)
            self._fallback_local_lookup(uid)
            return

        self._apply_tag_scan_response(uid, response)

    def _apply_tag_scan_response(self, uid: str, response: dict) -> None:
        status = response.get("status")
        kaka = response.get("kaka") or {}
        kaka_name = kaka.get("name", "?")

        if status == "play":
            logger.info("Tag %s → spiele '%s'", uid, kaka_name)
            self._update_tag_cache(uid, kaka)
            self._start_kaka_playlist(uid, kaka)
        elif status == "paired":
            kind = response.get("kind", "?")
            logger.info("Tag %s angelernt (kind=%s, name='%s')", uid, kind, kaka_name)
            self._update_tag_cache(uid, kaka)
            self._start_kaka_playlist(uid, kaka)
        elif status == "unknown":
            logger.info("Tag %s unbekannt. Auto-Pairing in der App aktivieren.", uid)
            self._drop_tag_cache(uid)
        elif status == "foreign_household":
            logger.warning("Tag %s gehört zu einem anderen Haushalt.", uid)
            self._drop_tag_cache(uid)
        else:
            logger.warning("Tag %s: unerwartete Backend-Antwort: %s", uid, response)

    def _kaka_fully_local(self, kaka: dict) -> bool:
        """True, wenn alle Lieder einer Kaka lokal im audio_cache liegen.

        Voraussetzung für den Cache-first-Sofortstart — sobald ein Track fehlt,
        bräuchten wir frische download_urls vom Server, also lieber den
        normalen Backend-Pfad nehmen.
        """
        contents = kaka.get("contents") or []
        if not contents:
            return False
        for c in contents:
            cid = int(c.get("id") or 0)
            if not cid:
                return False
            if not self.audio_cache.is_cached(cid, c.get("file_hash")):
                return False
        return True

    def _refresh_tag_in_background(self, uid: str) -> None:
        """Validiert den Cache nach Instant-Play gegen das Backend.

        - Bei ``play``/``paired``: Cache-Eintrag aktualisieren (greift beim
          nächsten Auflegen, falls sich der Inhalt geändert hat).
        - Bei ``unknown``/``foreign_household``: Tag wurde inzwischen vom
          Server abgelehnt — Wiedergabe stoppen, Cache verwerfen.
        - Bei Transport-Fehler: alles bleibt wie es ist (wir spielen ja
          schon, kein Schaden).
        """
        try:
            response = self.backend.tag_scan(uid)
        except BackendError as e:
            logger.warning("tag-refresh: %s", e)
            return
        if response is None:
            return

        status = response.get("status")
        kaka = response.get("kaka") or {}

        if status in ("play", "paired"):
            self._update_tag_cache(uid, kaka)
            return

        if status in ("unknown", "foreign_household"):
            logger.warning("tag-refresh: Tag %s ist nun '%s' — stoppe Wiedergabe.", uid, status)
            self._drop_tag_cache(uid)
            with self._playlist_lock:
                still_active = self._active_tag_uid == uid
                playlist = self._current_playlist if still_active else None
                if still_active:
                    self._current_playlist = None
                    self._active_tag_uid = None
            if playlist:
                playlist.stop()
                try:
                    self.player.stop()
                except Exception as e:
                    logger.warning("Player.stop fehlgeschlagen: %s", e)

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
                download_fn=lambda cid, path: bool(self.backend) and self.backend.download_audio(cid, path),
                play_fn=self.player.play_file,
                stop_fn=self.player.stop,
                position_fn=self.player.current_position_seconds,
                seek_fn=self.player.seek_to,
            )
            self._current_playlist = playlist
            self._active_tag_uid = uid
            # Tag-Playback überschreibt Random + Voice — beide Flags aus.
            self._random_mode = False
            self._voice_mode = False
            self._voice_pending_tag_uid = None
            self._voice_last_target = None

        if not playlist.start(start_index=start_index, start_position=start_position):
            logger.warning("Konnte Playlist nicht starten.")
            return
        # Streifen: Tanz an + kurze Position-Anzeige beim Start. Beim Resume
        # zeigt das gleich die richtige Stelle (start_index ≥ 0).
        if self.leds is not None:
            self.leds.strips_dance_start()
            self.leds.strips_show_position(
                playlist.current_index, playlist.length,
            )

    # ------------------------------------------------------------------
    # Random-Mode (Encoder-Push ≥ 1s)
    # ------------------------------------------------------------------

    def _all_cached_contents(self) -> list[KakaContent]:
        """Sammelt alle Tracks, die in irgendeinem Tag-Cache referenziert UND
        lokal gecached sind. De-dupliziert über content_id.

        So bekommen wir "alle Lieder der Box" ohne extra Bibliotheks-Index —
        der Tag-Cache ist die Quelle der Wahrheit für Track-Metadaten (Titel),
        der Audio-Cache für die tatsächlichen Dateien.
        """
        seen: set[int] = set()
        out: list[KakaContent] = []
        for entry in self._tag_cache.values():
            kaka = entry.get("kaka") or {}
            for c in kaka.get("contents", []):
                try:
                    cid = int(c.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if cid <= 0 or cid in seen:
                    continue
                if not self.audio_cache.is_cached(cid, c.get("file_hash")):
                    continue
                seen.add(cid)
                out.append(KakaContent(
                    content_id=cid,
                    title=c.get("title", ""),
                    file_hash=c.get("file_hash"),
                    download_url=c.get("download_url"),
                    cached_locally=True,
                    sort_order=0,  # für Random egal — wird eh geshuffled
                ))
        return out

    def _start_random_mode(self) -> None:
        """Startet (oder restartet) den Random-Modus: alle lokalen Tracks
        in zufälliger Reihenfolge. Funktioniert ohne Chip.

        Bei Hold während Random bereits läuft: einfach neue zufällige
        Reihenfolge generieren und von vorne anfangen (User-Wunsch: "wie
        eine session, die neu losgeht").
        """
        contents = self._all_cached_contents()
        if not contents:
            logger.warning("🎲 Random-Modus: keine gecachten Tracks gefunden.")
            return
        random.shuffle(contents)
        logger.info("🎲 Random-Modus startet mit %d Tracks", len(contents))

        with self._playlist_lock:
            if self._current_playlist:
                self._current_playlist.stop()

            playlist = Playlist(
                contents=contents,
                cache=self.audio_cache,
                download_fn=lambda cid, path: bool(self.backend) and self.backend.download_audio(cid, path),
                play_fn=self.player.play_file,
                stop_fn=self.player.stop,
                position_fn=self.player.current_position_seconds,
                seek_fn=self.player.seek_to,
            )
            self._current_playlist = playlist
            self._active_tag_uid = None  # kein Tag aktiv
            self._random_mode = True
            self._voice_mode = False
            self._voice_pending_tag_uid = None
            self._voice_last_target = None
            self._last_kaka_memory = None  # kein Resume aus Random

        if not playlist.start():
            logger.warning("Random-Playlist konnte nicht starten.")
            with self._playlist_lock:
                self._current_playlist = None
                self._random_mode = False
            return
        if self.leds is not None:
            self.leds.strips_dance_start()
            self.leds.strips_show_position(
                playlist.current_index, playlist.length,
            )
            # Random-Indikator auf NFC-LED: lila pulsierend, damit der
            # User auf einen Blick sieht "ich bin im Random-Modus".
            self.leds.nfc_random_active()

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
        """Aktiver Chip vom Reader weg → Snapshot speichern + Wiedergabe stoppen.

        Sonderfall Voice-Mode: Voice-Track läuft weiter (User-Wunsch). Wir
        clearen nur ``_voice_pending_tag_uid``, damit beim Voice-Ende die
        Continue-Logik weiß "kein Tag mehr, fall back auf Random".
        """
        if self._voice_mode:
            if uid == self._voice_pending_tag_uid:
                logger.info("Chip %s während Voice entfernt — Voice spielt weiter, "
                            "Continue → Random.", uid)
                self._voice_pending_tag_uid = None
            # NFC-LED aus (Tag wirklich weg), aber Streifen + Player bleiben.
            if self.leds is not None:
                self.leds.nfc_chip_absent()
            return
        # NFC-Status-LED aus + Streifen-Animation aus — "Chip ist weg" sofort
        # sichtbar, vor Snapshot etc.
        if self.leds is not None:
            self.leds.nfc_chip_absent()
            self.leds.strips_dance_stop()
        # Speed-Mode beenden — Snapshot/Resume soll mit Normalgeschwindigkeit
        # weiterlaufen, nicht im 200%-Chipmunk-Modus.
        if self._speed_mode:
            self._exit_speed_mode()

        with self._playlist_lock:
            playlist = self._current_playlist
            self._current_playlist = None
            removed_uid = self._active_tag_uid
            self._active_tag_uid = None
            # Random war ohnehin nicht aktiv (Tag lag drauf), aber sicher ist
            # sicher — Flag zurücksetzen, damit Folge-Aktionen sauber starten.
            self._random_mode = False

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
        if not playlist:
            return
        # Vorm Advance prüfen ob das der letzte Track war (Playlist beendet).
        # Im Voice-Mode triggern wir dann die Continue-Logik (Kakafigur oder Random).
        was_last = playlist.current_index >= playlist.length - 1
        playlist.on_track_end()
        if was_last and self._voice_mode:
            self._voice_continue()

    def _voice_continue(self) -> None:
        """Voice-Playlist zu Ende → Kakafigur fortsetzen (falls Tag noch drauf)
        sonst Random-Modus starten."""
        self._voice_mode = False
        self._voice_last_target = None
        pending = self._voice_pending_tag_uid
        self._voice_pending_tag_uid = None
        if pending:
            cached = self._tag_cache.get(pending)
            if cached:
                logger.info("Voice fertig → Kakafigur '%s' geht weiter", pending)
                self._start_kaka_playlist(pending, cached.get("kaka") or {})
                return
        logger.info("Voice fertig → Random-Modus")
        threading.Thread(
            target=self._start_random_mode, daemon=True, name="voice-random-fallback"
        ).start()

    def _fallback_local_lookup(self, uid: str) -> None:
        """Backend nicht erreichbar → erst Tag-Cache, dann Legacy-Album-Mapping.

        Tag-Cache wird beim erfolgreichen Online-Scan gepflegt; offline können
        wir damit ohne Backend abspielen, solange die Audio-Dateien im
        audio_cache liegen.
        """
        entry = self._tag_cache.get(uid)
        if entry:
            kaka = entry.get("kaka") or {}
            contents = kaka.get("contents") or []
            available = [
                c for c in contents
                if self.audio_cache.is_cached(int(c.get("id") or 0), c.get("file_hash"))
            ]
            if not available:
                logger.warning(
                    "Offline: Kaka '%s' bekannt (%d Lieder), aber keines lokal vorhanden.",
                    kaka.get("name", "?"), len(contents),
                )
                return
            if len(available) < len(contents):
                logger.info(
                    "Offline: %d von %d Liedern lokal verfügbar — Rest übersprungen.",
                    len(available), len(contents),
                )
            logger.info("Offline-Modus: spiele Kaka '%s' aus Tag-Cache.", kaka.get("name", "?"))
            self._start_kaka_playlist(uid, {**kaka, "contents": available})
            return

        # Legacy: alte uid → album_id-Zuordnung aus config.json
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

    def _abort_prompt_if_playing(self) -> bool:
        """Wenn ein System-Prompt läuft → abbrechen und True zurück.

        Wird von allen Button-Handlern als erstes aufgerufen: Ein Druck soll
        Boot-/WLAN-/Bye-Sounds direkt stoppen statt die normale Aktion
        auszulösen (sonst pausiert man z.B. den ready_to_rumble statt ihn zu
        stoppen). Deckt mpv-Prompts UND die per aplay laufenden WLAN-Prompts ab.
        """
        aborted = False
        if self.player.is_prompt_playing():
            logger.info("Prompt per Knopfdruck abgebrochen.")
            try:
                self.player.stop()
            except Exception as e:
                logger.warning("Player.stop nach Prompt-Abbruch fehlgeschlagen: %s", e)
            aborted = True
        if _kill_aplay_prompt():
            aborted = True
        return aborted

    def _on_green_pressed(self) -> None:
        """Grün: Track zurück, oder Neustart wenn schon > 5s gelaufen.

        Im Voice-Mode: Voice-Track neu starten (von vorne).
        Sonst: Playlist.previous + Resume (hebt Pause auf, damit Skip aus
        dem Pause-Zustand sofort spielt).
        """
        logger.info("🟢 Grün")
        if self._abort_prompt_if_playing():
            return
        if self._voice_mode and self._voice_last_target is not None:
            logger.info("🟢 Voice-Modus: Track neu starten")
            target = self._voice_last_target
            # _play_voice_target setzt voice_mode + räumt Playlist neu auf.
            # pending_tag_uid bleibt erhalten — User möchte das gleiche Verhalten.
            self._play_voice_target(target)
            return
        with self._playlist_lock:
            playlist = self._current_playlist
        if playlist:
            playlist.previous()
            self.player.resume()
            if self.leds is not None:
                self.leds.strips_show_position(
                    playlist.current_index, playlist.length,
                )
                # Track-Skip hebt Pause auf → NFC-LED zurück zum Mode-Status.
                self._restore_idle_led()

    def _on_red_pressed(self) -> None:
        """Rot: Nächster Track.

        Im Voice-Mode + Tag noch drauf: Voice abbrechen, Kakafigur beim
        nächsten Track weiter (User-Wunsch: "weiterklicken bei Voice + Tag
        drauf = Kakafigur nächster Track").
        Im Voice-Mode ohne Tag: einfach Voice-Continue (Random oder Stop).
        Sonst: Playlist.next + Resume.
        """
        logger.info("🔴 Rot")
        if self._abort_prompt_if_playing():
            return
        if self._voice_mode:
            pending = self._voice_pending_tag_uid
            if pending and pending in self._tag_cache:
                logger.info("🔴 Voice-Modus: zurück zur Kakafigur '%s', nächster Track", pending)
                # Voice abbrechen, Kakafigur starten, dann gleich next()
                self._voice_mode = False
                self._voice_last_target = None
                self._voice_pending_tag_uid = None
                self._start_kaka_playlist(pending, self._tag_cache[pending].get("kaka") or {})
                with self._playlist_lock:
                    playlist = self._current_playlist
                if playlist:
                    playlist.next()
                    self.player.resume()
                return
            # Voice ohne Tag → Random oder Stop via Continue-Logik
            self._voice_continue()
            return
        with self._playlist_lock:
            playlist = self._current_playlist
        if playlist:
            playlist.next()
            self.player.resume()
            if self.leds is not None:
                self.leds.strips_show_position(
                    playlist.current_index, playlist.length,
                )
                if self._active_tag_uid is not None:
                    self.leds.nfc_chip_present()

    def _on_green_stop(self) -> None:
        """Grün ≥ 1s: Wiedergabe komplett stoppen + Resume-Position vergessen."""
        logger.info("🟢⏵ Grün 1s — STOP")
        if self._abort_prompt_if_playing():
            return
        self._full_stop("Grün 1s")

    def _on_red_stop(self) -> None:
        """Rot ≥ 1s: Wiedergabe komplett stoppen + Resume-Position vergessen."""
        logger.info("🔴⏵ Rot 1s — STOP")
        if self._abort_prompt_if_playing():
            return
        self._full_stop("Rot 1s")

    def _full_stop(self, reason: str) -> None:
        """Hard-Stop: aktuelle Wiedergabe beenden + Resume-Memory leeren.

        Im Gegensatz zu ``_on_tag_removed`` wird hier KEIN Snapshot gemacht
        und ``_last_kaka_memory`` aktiv auf None gesetzt — ein danach
        aufgelegter Tag startet damit von vorne, statt an der alten
        Position weiterzulaufen. Genau so vom User gewünscht: Knopf-Stop
        ist final.
        """
        with self._playlist_lock:
            playlist = self._current_playlist
            self._current_playlist = None
            self._active_tag_uid = None
        if playlist:
            playlist.stop()
        try:
            self.player.stop()
        except Exception as e:
            logger.warning("Player.stop nach Full-Stop fehlgeschlagen: %s", e)
        self._last_kaka_memory = None
        self._random_mode = False
        self._voice_mode = False
        self._voice_pending_tag_uid = None
        self._voice_last_target = None
        if self.leds is not None:
            self.leds.strips_dance_stop()
            # NFC-LED aus (egal ob vorher grün=Tag, lila=Random oder gelb=Pause).
            self.leds.nfc_chip_absent()
        logger.info("Full stop (%s) — Memory geleert.", reason)

    def _on_green_held(self) -> None:
        """Grün ≥ 5s: Bye-Prompt → Poweroff.

        Die privilegierte Arbeit macht /usr/local/bin/kakabox-poweroff. Der
        sudoers-Drop-in erlaubt riffi NOPASSWD nur für genau diesen Pfad.

        Vor dem Poweroff wird tschau_kakau.wav abgespielt. Dafür müssen wir
        die laufende Playlist hart räumen, sonst feuert der EOF-Callback
        nach dem Bye-Prompt und versucht den nächsten Kaka-Track zu starten —
        die Box würde dann mitten im Lied ausgehen statt sauber zu verabschieden.
        """
        logger.warning("🟢🟢🟢 Grün 5s gehalten — Box wird ausgeschaltet.")

        with self._playlist_lock:
            playlist = self._current_playlist
            self._current_playlist = None
            self._active_tag_uid = None
        if playlist:
            playlist.stop()

        self._play_prompt("tschau_kakau.wav")
        self.player.wait_until_idle(timeout=8.0)

        try:
            subprocess.run(
                ["sudo", "-n", "/usr/local/bin/kakabox-poweroff"],
                check=False, timeout=10,
            )
        except Exception as e:
            logger.error("Power-off fehlgeschlagen: %s", e)

    def _on_red_held(self) -> None:
        """Rot ≥ 5s: WLAN-Profile löschen, OHNE Reboot.

        Comitup wird neu gestartet — weil dann kein WLAN-Profil mehr da ist,
        geht es automatisch in den Hotspot-Modus (Box bleibt eingeschaltet,
        Eltern können sich neu mit dem Captive-Portal verbinden).

        Die privilegierte Arbeit macht /usr/local/bin/kakabox-wifi-clear.

        Direkt danach spielen wir ``setup_active.wav`` ab — sofortiges
        Feedback "ich bin offline, bitte neu einrichten". Der Marker
        ``/run/kakabox/skip_hotspot_prompt`` (von kakabox-wifi-clear gesetzt)
        sorgt dafür, dass der comitup-Callback seinen sonst 12s verzögerten
        Hotspot-Prompt überspringt — sonst gäbe es eine Doppelansage.
        Der Prompt läuft über ``_play_prompt`` → respektiert ``system_volume``
        aus config.json und ist mit jedem Knopfdruck via
        ``_abort_prompt_if_playing`` abbrechbar.
        """
        logger.warning("🔴🔴🔴 Rot 5s gehalten — WLAN-Reset (ohne Reboot).")
        try:
            subprocess.run(
                ["sudo", "-n", "/usr/local/bin/kakabox-wifi-clear"],
                check=False, timeout=15,
            )
        except Exception as e:
            logger.error("WLAN-Clear fehlgeschlagen: %s", e)
            return
        self._play_prompt("setup_active.wav")

    def _on_push_pressed(self) -> None:
        """Encoder-Druck — ausschließlich für Speed-Mode-Gestik.

        - Im Speed-Mode: ein Druck verlässt den Modus.
        - Sonst: zählt zum Burst-Sliding-Window. 4× innerhalb SPEED_BURST_WINDOW
          während Wiedergabe → Speed-Mode an.
        - Kein Pause/Play mehr (das macht gelb), kein Voice (das macht blau).
        """
        if self._abort_prompt_if_playing():
            return

        if self._speed_mode:
            logger.info("🟦 Push — exit Speed-Mode")
            self._exit_speed_mode()
            return

        now = time.monotonic()
        # Sliding-Window: nur Pushes der letzten SPEED_BURST_WINDOW behalten
        self._push_times.append(now)
        self._push_times = [t for t in self._push_times if t > now - SPEED_BURST_WINDOW]

        with self._playlist_lock:
            playlist_active = self._current_playlist is not None

        logger.info(
            "🟦 Push (burst %d/%d, playlist=%s)",
            len(self._push_times), SPEED_BURST_COUNT, playlist_active,
        )

        if len(self._push_times) >= SPEED_BURST_COUNT and playlist_active:
            logger.info("🟦×%d → Speed-Mode aktiv", SPEED_BURST_COUNT)
            self._push_times.clear()
            self._enter_speed_mode()

    def _on_push_held(self) -> None:
        """Encoder-Push ≥ 1s → Random-Modus an/aus toggeln.

        - Random schon an → stoppen (alles aus, Stille).
        - Random aus → starten (zufällige Reihenfolge aus allen Tracks).
        - Speed-Mode beenden falls aktiv.

        Im Hintergrund-Thread, weil _start_random_mode die Playlist-Init
        + erstes play_file machen kann, und der gpiozero-Hold-Callback nicht
        lange blocken soll.
        """
        if self._abort_prompt_if_playing():
            return
        if self._speed_mode:
            self._exit_speed_mode()
        if self._random_mode:
            logger.info("🎲 Encoder-Push ≥ 1s — Random-Modus stoppen")
            self._full_stop("Push-Hold Random-Stop")
            return
        logger.info("🎲 Encoder-Push ≥ 1s — Random-Modus starten")
        threading.Thread(
            target=self._start_random_mode,
            daemon=True,
            name="random-mode-start",
        ).start()

    def _on_yellow_pressed(self) -> None:
        """Gelb — Pause/Play-Toggle.

        NFC-LED bei Pause IMMER gelb (auch im Random-Modus, damit der User
        visuelles Feedback bekommt). Bei Resume zurück zum Mode-spezifischen
        Status (grün=Tag, lila=Random, aus=nichts).
        """
        if self._abort_prompt_if_playing():
            return
        logger.info("🟡 Gelb (Pause/Play)")
        self.player.toggle_pause()
        if self.leds is not None:
            if self.player.get_state().paused:
                self.leds.nfc_chip_paused()
            else:
                self._restore_idle_led()

    def _restore_idle_led(self) -> None:
        """Setzt die NFC-LED auf den passenden "läuft normal"-Status:
        grün=Tag aktiv, lila=Random-Modus, sonst aus.

        Wird nach Pause/Resume + Track-Skip aufgerufen, damit die LED nicht
        im falschen Zustand hängenbleibt.
        """
        if self.leds is None:
            return
        if self._active_tag_uid is not None:
            self.leds.nfc_chip_present()
        elif self._random_mode:
            self.leds.nfc_random_active()
        else:
            self.leds.nfc_chip_absent()

    def _spectrum_loop(self) -> None:
        """Dauerhafter Audio-Capture + FFT für die LED-Streifen.

        arecord läuft permanent (nicht lifecycle-gekoppelt). Grund: das
        Multi-Device kakabox_audio (Speaker + Loopback) verträgt schnelles
        Auf-Zu vom Capture-Reader nicht — wenn arecord gerade neu startet
        während mpv versucht zu schreiben, blockiert der Loopback-Buffer
        und mpv geht nach ~250 ms als "idle" raus → Track wird sofort
        übersprungen. Permanent-Capture kostet ~1 % CPU dauerhaft, dafür
        ist Audio bombenstabil.

        Bei Stille (nichts spielt) liefert arecord 0-Samples → FFT-Bänder
        ~0 → LEDs entscheiden über SPECTRUM_SILENCE_THRESHOLD selbst dass
        sie aus bleiben.
        """
        if self._spectrum is None:
            return
        if not self._spectrum.start():
            logger.warning("Spectrum-Capture konnte nicht starten — LEDs ohne Tanz")
            return
        try:
            while not self._spectrum_stop.is_set():
                bands = self._spectrum.read_bands()
                if bands is None:
                    # Stream tot — kurz warten, dann neu öffnen.
                    if self._spectrum_stop.wait(1.0):
                        return
                    self._spectrum.stop()
                    if not self._spectrum.start():
                        return
                    continue
                if self.leds is not None:
                    self.leds.update_spectrum(bands)
        except Exception:
            logger.exception("Spectrum-Loop crashed")
        finally:
            if self._spectrum is not None:
                self._spectrum.close()

    def _warmup_recognizer(self) -> None:
        """Lädt das ASR-Modell beim Service-Start in den RAM.

        Läuft in einem Daemon-Thread, damit der Boot nicht blockiert und der
        Rest der Box (NFC-Loop, Buttons, Heartbeat) sofort verfügbar ist.
        Spart ~1–3 s beim ersten Push-to-Talk. Bei Paket/Modell-Fehlern wird
        nur geloggt — der Lazy-Load-Pfad in ``transcribe_wav`` greift dann
        beim echten Trigger und der Fehler wird dort sichtbar.
        """
        try:
            t0 = time.monotonic()
            self._recognizer.warmup()
            logger.info(
                "ASR-Modell vorgeladen (%s, %.1fs)",
                self._recognizer.backend, time.monotonic() - t0,
            )
        except VoiceUnavailable as e:
            logger.info("ASR-Warmup übersprungen: %s", e)
        except Exception:
            # Unerwarteter Fehler — den fangen wir bewusst weit, damit
            # ein Modell-Lade-Crash NIE den Box-Start kaputt macht.
            logger.exception("ASR-Warmup unerwartet fehlgeschlagen")

    def _on_blue_pressed(self) -> None:
        """Blau gedrückt → Voice-Push-to-Talk.

        Läuft in einem Hintergrund-Thread, weil die Aufnahme + ASR mehrere
        Sekunden blocken kann — der Button-Handler darf nicht stehenbleiben,
        sonst kommt kein zweites Event durch.
        """
        if self._abort_prompt_if_playing():
            return
        if self._speed_mode:
            # Während Speed-Mode beendet ein blauer Druck den Modus statt
            # Voice zu triggern — Voice + Speed-Mode parallel ist unintuitiv.
            self._exit_speed_mode()
            return
        if not self._voice_lock.acquire(blocking=False):
            logger.info("Voice bereits aktiv — Trigger ignoriert.")
            return
        threading.Thread(
            target=self._run_voice_activation,
            daemon=True,
            name="voice-ptt",
        ).start()

    def _run_voice_activation(self) -> None:
        """Padamm → Aufnehmen → ASR → Match → Wiedergabe. Lock-protected.

        Recovery-Verhalten (User-Wunsch): Wenn vorher eine Kakafigur lief und
        die Voice-Eingabe schiefgeht (kein Mic, ASR-Fehler, kein Match), soll
        die Kakafigur weiterlaufen. Wir snapshoten den Stand (tag + index +
        position) vor dem Voice-Flow und re-starten die Kakafigur mit
        Resume-Position falls kein Match. Gap-frei geht nicht (listening-
        Prompt + Aufnahme dauern ~3s), aber stabil.
        """
        # Snapshot des aktuellen Zustands für Recovery
        saved_tag_uid = self._active_tag_uid
        saved_random_mode = self._random_mode
        saved_track_index = 0
        saved_position = 0.0
        with self._playlist_lock:
            if self._current_playlist:
                saved_track_index = self._current_playlist.current_index
                try:
                    saved_position = self.player.current_position_seconds()
                except Exception:
                    saved_position = 0.0
        recovered = False  # True sobald entweder ein neuer Track läuft oder wir resumed haben

        def _restore_previous(reason: str) -> None:
            """Helper: vorheriges Playback fortsetzen.
            - Kakafigur drauf → resume mit gemerkter Position
            - Random war an → Random neu starten
            - Sonst → nichts (war ja vorher auch nichts)
            """
            if saved_tag_uid and saved_tag_uid in self._tag_cache:
                logger.info(
                    "Voice abgebrochen (%s) → Kakafigur '%s' resume Track %d ab %.1fs",
                    reason, saved_tag_uid, saved_track_index + 1, saved_position,
                )
                self._last_kaka_memory = KakaMemory(
                    tag_uid=saved_tag_uid,
                    track_index=saved_track_index,
                    position_seconds=saved_position,
                )
                self._start_kaka_playlist(
                    saved_tag_uid,
                    self._tag_cache[saved_tag_uid].get("kaka") or {},
                )
            elif saved_random_mode:
                logger.info("Voice abgebrochen (%s) → Random-Modus wieder an", reason)
                self._start_random_mode()

        # Während Voice-Eingabe → NFC-LED blau pulsieren (egal ob Tag drauf
        # war oder nicht — zeigt visuell "ich höre dir gerade zu").
        if self.leds is not None:
            self.leds.nfc_voice_active()

        try:
            # Vor dem Prompt sauber stoppen — wir können nicht pausieren weil
            # der Prompt selbst mpv.stop()+play() macht (defensive in play_file),
            # was den pausierten Stream eh zerstören würde.
            self._stop_for_voice()
            self._play_prompt("listening.wav")
            # Padamm zu Ende abspielen, sonst mischt's sich in die Aufnahme.
            self.player.wait_until_idle(timeout=2.0)

            try:
                wav = self._mic_recorder.record_until_silence(
                    max_seconds=VOICE_MAX_SECONDS,
                    silence_seconds=VOICE_SILENCE_SECONDS,
                )
            except RecorderError as e:
                logger.warning("Voice-Aufnahme fehlgeschlagen: %s", e)
                return

            try:
                # Grammar bewusst NICHT setzen — das kleine DE-Vosk-Modell
                # kennt viele Eigennamen (DIKKA, Bibi, Captain) nicht und
                # würde sie im Grammar-Modus komplett ignorieren. Der freie
                # Decoder transkribiert phonetisch, der Fuzzy-Match findet
                # den Song.
                text = self._recognizer.transcribe_wav(wav)
            except VoiceUnavailable as e:
                logger.warning("ASR nicht verfügbar: %s", e)
                return

            logger.info("Voice transkribiert: «%s»", text)

            # Zauberwort-Modus: nur abspielen, wenn "bitte" im Transkript steht.
            # Sonst Prompt "Wie heißt das Zauberwort?" — Kind muss den Befehl
            # nochmal mit Höflichkeit wiederholen. Der Modus wird per API
            # (POST /zauberwort/enable) oder direkt in config.json geschaltet.
            if self.config.get("zauberwort_mode_enabled") and not has_magic_word(text):
                logger.info("Zauberwort fehlt in «%s» — Prompt statt Match.", text)
                self._play_prompt("zauberwort.wav")
                return

            catalog = build_catalog_from_file(VOICE_CATALOG_PATH)
            if not catalog:
                logger.warning("Voice-Catalog leer — kein Match möglich.")
                return

            cmd = parse_play_command(text, catalog)
            if cmd is None:
                logger.info("Voice: kein Match für «%s»", text)
                return

            logger.info(
                "Voice match: kind=%s name='%s' score=%.2f query='%s'",
                cmd.target.kind, cmd.target.name, cmd.score, cmd.query,
            )
            # Match → Erfolgs-Sound (cartoonish), kurz warten bis er durch ist,
            # dann Voice-Target abspielen. Voice-Mode wird in _play_voice_target gesetzt.
            self._voice_pending_tag_uid = saved_tag_uid
            self._voice_last_target = cmd.target
            self._play_prompt("voice_success.wav")
            self.player.wait_until_idle(timeout=2.0)
            self._play_voice_target(cmd.target)
            recovered = True  # neue Wiedergabe läuft, kein Restore nötig
        finally:
            # Wenn vorher eine Kakafigur lief UND nichts Neues gestartet wurde:
            # Erst Error-Sound, dann Kakafigur/Random restoren.
            if not recovered:
                try:
                    self._play_prompt("voice_error.wav")
                    self.player.wait_until_idle(timeout=2.0)
                    _restore_previous("kein Match / Recording fail / ASR fail")
                except Exception as e:
                    logger.warning("Resume nach Voice-Abbruch fehlgeschlagen: %s", e)
            # NFC-LED zurück auf den passenden Status nach Voice:
            #   - Kakafigur wieder aktiv (Restore oder durch Voice-Match mit
            #     pending_tag) → grün
            #   - Random-Modus läuft → lila
            #   - sonst → aus
            if self.leds is not None:
                if self._active_tag_uid is not None or (
                    self._voice_mode and self._voice_pending_tag_uid is not None
                ):
                    self.leds.nfc_chip_present()
                elif self._random_mode:
                    self.leds.nfc_random_active()
                else:
                    self.leds.nfc_chip_absent()
            self._voice_lock.release()

    def _stop_for_voice(self) -> None:
        """Räumt vor der Aufnahme: laufende Playlist stoppen, Tag-State leeren.

        Sonst nimmt das Mic den laufenden Track mit auf (MAX98357A hat kein
        Echo-Cancellation) und der NFC-Loop würde später beim Tag-Removal
        unsere frisch gestartete Voice-Playlist abräumen.
        """
        with self._playlist_lock:
            playlist = self._current_playlist
            self._current_playlist = None
            self._active_tag_uid = None
        if playlist:
            playlist.stop()
        try:
            self.player.stop()
        except Exception as e:
            logger.warning("Player.stop vor Voice fehlgeschlagen: %s", e)

    def _play_voice_target(self, target: Candidate) -> None:
        """Spielt den per Voice gewählten Target ab.

        ``kind="track"`` → einzelne Datei aus dem Cache; ``kind="artist"`` →
        alle Tracks des Künstlers als Playlist. Nicht-gecachte Tracks werden
        übersprungen (kein Online-Download während des Voice-Flows).
        """
        if not target.content_ids:
            logger.warning("Voice-Target ohne content_ids: %s", target.name)
            return

        contents: list[KakaContent] = []
        for cid in target.content_ids:
            path = self.audio_cache.path_for(cid)
            if not path.is_file():
                logger.warning("Voice: Track %d nicht im Cache (%s)", cid, path)
                continue
            contents.append(KakaContent(
                content_id=cid,
                title=target.name,
                file_hash=None,
                download_url=None,
                cached_locally=True,
                sort_order=0,
            ))
        if not contents:
            logger.warning("Voice: keine spielbaren Tracks für '%s'", target.name)
            return

        with self._playlist_lock:
            playlist = Playlist(
                contents=contents,
                cache=self.audio_cache,
                download_fn=lambda cid, p: bool(self.backend) and self.backend.download_audio(cid, p),
                play_fn=self.player.play_file,
                stop_fn=self.player.stop,
                position_fn=self.player.current_position_seconds,
                seek_fn=self.player.seek_to,
            )
            self._current_playlist = playlist
            self._voice_mode = True
            self._random_mode = False

        if not playlist.start():
            logger.warning("Voice-Playlist konnte nicht starten.")
            self._voice_mode = False

    def _enter_speed_mode(self) -> None:
        self._speed_mode = True
        self._speed = 1.0
        self.player.set_speed(1.0)
        # Während des 4-Burst hat sich der Pause-Toggle u. U. ungeradzahlig
        # umgestellt — sicherstellen, dass die Wiedergabe wirklich läuft.
        if self.player.get_state().paused:
            self.player.resume()
        if self.leds is not None:
            self.leds.show_speed(self._speed)

    def _exit_speed_mode(self) -> None:
        self._speed_mode = False
        self._speed = 1.0
        self.player.set_speed(1.0)
        logger.info("⏩ Speed zurück auf 100%%")
        if self.leds is not None:
            self.leds.hide_speed()

    def _adjust_speed(self, delta: float) -> None:
        # Auf 2 Nachkommastellen runden, sonst bekommen wir durch float-Drift
        # Werte wie 1.0999999 statt 1.10.
        new_speed = round(max(SPEED_MIN, min(SPEED_MAX, self._speed + delta)), 2)
        if abs(new_speed - self._speed) < 1e-6:
            return
        self._speed = new_speed
        self.player.set_speed(new_speed)
        logger.info("⏩ Speed: %d%%", int(round(new_speed * 100)))
        if self.leds is not None:
            self.leds.show_speed(new_speed)

    def _adjust_volume(self, delta: int) -> None:
        # Hard-Cap aus rule.max_volume (Webapp / Eltern-Setting). Wenn die
        # Webapp keinen Cap gesetzt hat, bleibt _max_volume=100, also wie
        # vorher.
        new_vol = max(0, min(self._max_volume, self._volume + delta))
        if new_vol == self._volume:
            # Auch bei "schon am Anschlag"-Drehung Ring zeigen, damit der User
            # sieht: ja, ich habe registriert was du gedreht hast, mehr geht
            # halt nicht. Sonst bleibt der Ring stumm und es fühlt sich kaputt an.
            if self.leds is not None:
                self.leds.show_volume(self._volume)
            return
        self._volume = new_vol
        # Hinweis: kein amixer-Call mehr. MAX98357A hat keinen Hardware-Mixer;
        # jeder amixer-Subprocess blockte ~300ms und failte — staute den
        # Encoder-Pfad. mpv softvol via player.set_volume reicht aus.
        self.player.set_volume(new_vol)
        if self.leds is not None:
            self.leds.show_volume(new_vol)
        # save_config NICHT bei jedem Tick — SD-Karten-Write blockt den
        # Encoder-Loop. Wert wird beim regulären Shutdown gespeichert.
        self.config["volume"] = new_vol
        logger.info("🔊 Volume: %d%%", new_vol)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        logger.info("Shutting down...")
        self._running = False
        self._spectrum_stop.set()
        with self._playlist_lock:
            if self._current_playlist:
                self._current_playlist.stop()
        self.player.stop()
        self.nfc.close()
        if self.buttons is not None:
            self.buttons.close()
        if self.encoder is not None:
            self.encoder.close()
        if self.leds is not None:
            self.leds.close()
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
