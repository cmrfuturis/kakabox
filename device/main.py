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

        # Speed-Mode-State (siehe SPEED_* Konstanten)
        self._speed_mode = False
        self._speed = 1.0
        self._push_times: list[float] = []

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

    def _play_prompt(self, filename: str) -> None:
        """Spielt eine Boot-/Status-Ansage über den Player (gleiches ALSA-Device wie mpv).

        Nutzt ``player.play_prompt`` mit ``self._system_volume`` — so klebt die
        oft-zu-laute Default-Lautstärke nicht an Boot-/Bye-Sounds. Der Knopf-
        Druck während eines Prompts bricht ihn via ``player.stop()`` ab (siehe
        Button-Handler).
        """
        path = PROMPTS_DIR / filename
        if not path.is_file():
            logger.debug("Prompt nicht gefunden: %s", path)
            return
        try:
            self.player.play_prompt(str(path), self._system_volume)
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
        """Grün: Track zurück, oder Neustart wenn schon > 5s gelaufen, mit Loop."""
        logger.info("🟢 Grün")
        if self._abort_prompt_if_playing():
            return
        with self._playlist_lock:
            playlist = self._current_playlist
        if playlist:
            playlist.previous()
            if self.leds is not None:
                self.leds.strips_show_position(
                    playlist.current_index, playlist.length,
                )

    def _on_red_pressed(self) -> None:
        """Rot kurz: Nächster Track mit Loop."""
        logger.info("🔴 Rot")
        if self._abort_prompt_if_playing():
            return
        with self._playlist_lock:
            playlist = self._current_playlist
        if playlist:
            playlist.next()
            if self.leds is not None:
                self.leds.strips_show_position(
                    playlist.current_index, playlist.length,
                )

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
        if self.leds is not None:
            self.leds.strips_dance_stop()
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

    def _on_yellow_pressed(self) -> None:
        """Gelb — Pause/Play-Toggle."""
        if self._abort_prompt_if_playing():
            return
        logger.info("🟡 Gelb (Pause/Play)")
        self.player.toggle_pause()

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
        """Padamm → Aufnehmen → ASR → Match → Wiedergabe. Lock-protected."""
        try:
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
            self._play_voice_target(cmd.target)
        finally:
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

        if not playlist.start():
            logger.warning("Voice-Playlist konnte nicht starten.")

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
        try:
            set_volume(new_vol)
        except Exception as e:
            logger.warning("ALSA volume control unavailable: %s", e)
        self.player.set_volume(new_vol)
        self.config["volume"] = new_vol
        save_config(self.config)
        logger.info("🔊 Volume: %d%%", new_vol)
        if self.leds is not None:
            self.leds.show_volume(new_vol)

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
