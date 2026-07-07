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
    🟦 Encoder-Push      → 4× schnell während Wiedergabe → Speed-/Fast-Mode
    🟦 Encoder-Push ≥ 1s → STOP (Kaka/Voice/Random); bei Stille → Random starten
    🟦 Encoder im UZS    → Lauter (im Speed-Mode: schneller)
    🟦 Encoder gegen UZS → Leiser (im Speed-Mode: langsamer)
    (Pause/Play liegt auf Gelb, Voice-Push-to-Talk auf Blau.)

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
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from audio import AudioCache, KakaContent, Playlist, PlaylistSnapshot
from audio.library import scan
from audio.player import Player
from audio.spectrum import FileSpectrum
from hardware.audio_output import set_volume
from hardware.buttons import Buttons
from hardware.leds import Leds, LedsUnavailable
from hardware.nfc import PN532
from hardware.rotary_encoder import Encoder as RotaryEncoder
from network import Backend, BackendError
from network.play_sessions import PlaySessionReporter
from voice.asr import Recognizer, VoiceUnavailable, build_recognizer
from voice.catalog import build_catalog_from_file, build_title_map_from_file
from voice.intent import Candidate, has_magic_word
from voice.recorder import MicRecorder, RecorderError
from voice.router import route_transcript
from voice.tts import TitleSpeaker

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
PLAY_SESSION_QUEUE_PATH = Path(__file__).parent / "play_sessions_queue.json"
PROMPTS_DIR = Path("/usr/share/kakabox/prompts")  # vom Installer befüllt
APLAY_PROMPT_PID = Path("/run/kakabox/prompt_pid")  # vom Comitup-Callback geschrieben
TTS_DIR = Path("/usr/share/kakabox/tts")
# Vom Backend per ``tts_voice`` umschaltbare Stimmen (männlich/weiblich). Beide
# Modelle liegen auf der Box (Installer lädt beide) → Umschalten = nur Modellpfad
# wechseln, kein Download zur Laufzeit.
TTS_MODELS = {
    "male": TTS_DIR / "de_DE-thorsten-medium.onnx",
    "female": TTS_DIR / "de_DE-kerstin-low.onnx",
}
TTS_VOICE_DEFAULT = "male"
# Trägerphrase vor dem Titel ("Dieses Lied heißt … <Titel>"). Wird per TTS in
# der GEWÄHLTEN Stimme gesprochen, damit männlich/weiblich durchgängig passt.
TTS_TITLE_INTRO = "Dieses Lied heißt"
# Trägerphrase fürs gesprochene Voice-Feedback nach erkanntem + (ggf.)
# zauberwort-bestätigtem Befehl: "Ich spiele … <Name>". Gleicher Mechanismus
# wie TTS_TITLE_INTRO (Trägerphrase einmal pro Stimme gecacht, Name separat —
# der Name-Cache wird mit der Titel-Ansage geteilt).
TTS_NOW_PLAYING_INTRO = "Ich spiele"
VOLUME_STEP = 5            # Encoder-Klick = 5 Prozentpunkte
HEARTBEAT_INTERVAL = 30
AUDIO_SYNC_INTERVAL = 300  # 5 Minuten
SYNC_RETRY_BACKOFF_SECONDS = 3600  # 1h: failed Downloads nicht jeden Zyklus retry'en
                                   # (verhindert Log-Spam bei kaputten Backend-Storage-IDs)
TAG_REMOVAL_THRESHOLD = 5  # NFC: aufeinanderfolgende Leer-Reads bis "Chip entfernt".
                           # Bei ~0.25s/Poll = ~1.25s. Vorher 2 (~0.5s) → ein
                           # physisch aufliegender Chip, der nur intermittierend
                           # liest (PN532-Glitch), wurde ständig fälschlich als
                           # "entfernt" gewertet → Stop/Start-Schleife (Musik
                           # bricht ab) UND der Hintergrund-Refresh (neue Lieder
                           # + Orange-Animation) wurde abgebrochen, bevor er
                           # durchlief. Höhere Schwelle toleriert Lese-Aussetzer;
                           # echtes Abnehmen wird ~1.25s später erkannt (ok).

# Geheimer Speed-Mode (Easter Egg): 4× Encoder-Push in 3s während Wiedergabe →
# danach steuert der Encoder die Wiedergabegeschwindigkeit statt Lautstärke.
# Exit: nochmal Push, oder Chip vom Reader nehmen.
SPEED_BURST_COUNT = 4
SPEED_BURST_WINDOW = 3.0
SPEED_STEP = 0.1
SPEED_MIN = 0.5
SPEED_MAX = 2.0

# Voice-Push-to-Talk: Blau gedrückt → Padamm → Aufnehmen → Match.
# VAD-light bricht die Aufnahme automatisch ab, sobald VOICE_SILENCE_SECONDS
# am Stück Stille (nach erster erkannter Sprache) erreicht ist — sonst hartes
# Cap bei VOICE_MAX_SECONDS, damit längere Sätze möglich sind aber die Box
# nicht endlos wartet, wenn jemand nichts sagt.
VOICE_MAX_SECONDS = 7.0
VOICE_SILENCE_SECONDS = 1.4  # Nachlauf-Stille nach dem Sprechen, bis die
                             # Aufnahme abbricht. 1,4s statt 1,0s (ASR-Plan
                             # 1.8): 4–8-Jährige machen >1s Denkpausen MITTEN
                             # im Kommando ("spiele … ähm … den Zug") — mit
                             # 1,0s endete die Aufnahme nach "spiele". Die
                             # +0,4s pro Befehl sind durch die audio_ctx-
                             # Beschleunigung (asr.py) mehr als kompensiert.
VOICE_INITIAL_SILENCE_SECONDS = 4.5  # nichts gesagt nach 4,5s → Abbruch
                                     # (3,0s war für zögerliche Kinder zu
                                     # knapp — sie überlegen erst, WAS sie
                                     # sich wünschen).
# Follow-up-Aufnahme für die Zauberwort-Rückfrage ("Wie heißt das Zauberwort?"):
# kürzere Nachlauf-Stille als beim Hauptbefehl, damit es nach erkanntem
# "bitte" schnell weitergeht. 0,9s statt 0,4s (ASR-Plan 1.8): ein zögerliches
# "bii…tte" eines 4-Jährigen hat >0,4s Binnenpause und wurde abgeschnitten.
VOICE_ZAUBERWORT_SILENCE_SECONDS = 0.9
# Bare-Title-Fallback: Kinder sagen oft nur den Titel ("Der Zug hat keine
# Bremsen") OHNE "spiele" davor — dann lehnt der reguläre Parser mangels
# Play-Verb ab. Wir versuchen dann einen verb-freien Match, aber mit deutlich
# strengerem Score-Threshold als beim klaren "spiele …"-Befehl (0.55), damit
# zufälliges Gerede/Nuscheln nicht fälschlich einen Song auslöst. Empirisch:
# selbst stark vernuscheltes ASR landet beim richtigen Titel bei ~0.73+, klar
# daneben deutlich darunter — 0.70 trennt das sauber.
VOICE_BARE_TITLE_THRESHOLD = 0.70

# Mindestdauer der Orange-Sync-Optik bei erkannter Dongel-Änderung — auch wenn
# der Sync sofort fertig ist (nichts zu laden), bleibt das Feedback so lange
# sichtbar, damit der User es wahrnimmt. Danach der Bestätigungssound.
SYNC_FEEDBACK_MIN_S = 2.0

# Energiesparen: zweistufig bei Inaktivität (nichts spielt, kein Chip, kein Knopf).
#   1) nach STANDBY_TIMEOUT_S → Software-Standby: LEDs aus, CPU 'powersave',
#      Sync/Heartbeat pausiert, NFC-Poll gedrosselt. Aufwecken: beliebiger Knopf
#      (nur wecken) oder Chip auflegen (wecken + spielen).
#   2) nach SHUTDOWN_TIMEOUT_S → kompletter Shutdown mit "tschau kakau" (wie
#      grünes 5s-Halten) — danach manuell wieder einschalten.
STANDBY_TIMEOUT_S = 60
SHUTDOWN_TIMEOUT_S = 300
STANDBY_NFC_POLL_S = 0.6   # langsameres NFC-Polling im Standby (statt ~0.25s)


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


def read_cpu_load_percent() -> int | None:
    """Aktuelle CPU-Auslastung (1-Min-Schnitt) als gerundeter Prozentwert.

    Quelle: /proc/loadavg, erstes Feld = Anzahl runnable/blocked Prozesse
    über die letzte Minute. Wir teilen durch die Anzahl der CPU-Cores und
    cappen bei 100 — load > #cores ist Overload, in der UI aber als 100%
    repräsentiert (Unterschiede darüber sind für die Eltern-Sicht irrelevant).
    Bei Lese-/Parse-Fehlern → None, Heartbeat sendet das Feld dann gar nicht.
    """
    try:
        loadavg_text = Path("/proc/loadavg").read_text()
        load_1m = float(loadavg_text.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    cores = os.cpu_count() or 1
    percent = round(load_1m / cores * 100.0)
    return max(0, min(100, percent))


# Bekannte Display-Server / Wayland-Compositoren. Läuft einer, sind wir im
# Desktop — das passiert auf der Box nur beim Entwickeln. Im Normalbetrieb
# (headless/Konsole) läuft keiner.
_GUI_SERVER_PROCS = "(Xorg|Xwayland|wayfire|labwc)"
_gui_active_cache: tuple[float, bool] | None = None
_GUI_CACHE_TTL_S = 30.0


def graphical_session_active() -> bool:
    """True, wenn gerade eine grafische Oberfläche läuft (= Dev-Modus).

    Erkennung über einen laufenden Display-Server/Compositor (``pgrep``), NICHT
    über ``$DISPLAY`` — der systemd-Service erbt keine Display-Env. Socket-Pfade
    (``/tmp/.X11-unix``) scheiden wegen ``PrivateTmp=true`` aus, ``/proc`` ist
    aber sichtbar. Ergebnis wird ``_GUI_CACHE_TTL_S`` gecacht, damit der
    Idle-Watcher nicht alle paar Sekunden einen Prozess spawnt.

    Fällt ``pgrep`` aus, behandeln wir das als "keine GUI" → der Auto-Shutdown
    bleibt scharf (sicheres Verhalten im Feld, wo pgrep immer existiert).
    """
    global _gui_active_cache
    now = time.monotonic()
    if _gui_active_cache is not None and now - _gui_active_cache[0] < _GUI_CACHE_TTL_S:
        return _gui_active_cache[1]
    try:
        # pgrep -x + gruppierte Alternation → exakter Match auf den ganzen
        # Prozessnamen (ein "(a|b)" wird sauber zu ^(a|b)$ verankert).
        res = subprocess.run(
            ["pgrep", "-x", _GUI_SERVER_PROCS], capture_output=True, timeout=2
        )
        active = res.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        active = False
    _gui_active_cache = (now, active)
    return active


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("kakabox")


DEFAULT_SYSTEM_VOLUME = 25  # Lautstärke für Boot-/WLAN-/Bye-Prompts (gedämpft, User-Wunsch — laute Default-Ansagen erschrecken)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            # Kann durch einen Spannungseinbruch mitten in save_config()
            # entstehen. Kaputte Datei aus dem Weg räumen statt beim Boot zu
            # crashen — die Box läuft mit Defaults weiter statt in eine
            # Restart-Loop zu geraten (tags/NFC-Zuordnungen sind dann zwar
            # weg, aber das Gerät bleibt benutzbar).
            broken_path = CONFIG_PATH.with_name(
                CONFIG_PATH.name + f".broken-{int(time.time())}"
            )
            try:
                CONFIG_PATH.rename(broken_path)
                logger.error(
                    "config.json war beschädigt — nach %s verschoben, starte mit Defaults.",
                    broken_path,
                )
            except OSError:
                logger.exception("Konnte kaputte config.json nicht umbenennen.")
    return {
        "volume": 70,
        "system_volume": DEFAULT_SYSTEM_VOLUME,
        "tags": {},
        "parental": {"disabled_albums": []},
    }


def save_config(config: dict) -> None:
    # Atomar: tmp-Datei + os.replace, damit ein Spannungseinbruch mitten im
    # Schreiben nie eine halbe/kaputte config.json hinterlässt (siehe
    # load_config()).
    tmp_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    tmp_path.replace(CONFIG_PATH)


_WEEKDAY_ABBREVS = ["mo", "di", "mi", "do", "fr", "sa", "so"]


def _parse_hhmm(value) -> Optional[dt_time]:
    """Parst ein "HH:MM"-String in ein datetime.time. None bei Unparsbarem
    (defensiv — eine kaputte/fehlende Ruhezeit darf die Box nicht crashen,
    nur diese eine Regel wird dann ignoriert)."""
    try:
        hh, mm = str(value).split(":")[:2]
        return dt_time(int(hh), int(mm))
    except (TypeError, ValueError, AttributeError):
        return None


def quiet_hours_active(quiet_hours: list[dict], now: datetime) -> bool:
    """True, wenn ``now`` in einem der konfigurierten Ruhezeit-Fenster liegt.

    Fenster können über Mitternacht laufen (start_time > end_time, z.B.
    20:00–07:00 für "jede Nacht"). In dem Fall gilt ein Fenster als aktiv,
    wenn entweder heute nach start_time ODER heute vor end_time liegt — wobei
    "vor end_time" zum GESTRIGEN Wochentag in ``days`` gehört (das Fenster hat
    gestern Abend begonnen und reicht in den heutigen Morgen hinein).
    """
    if not quiet_hours:
        return False
    today = _WEEKDAY_ABBREVS[now.weekday()]
    yesterday = _WEEKDAY_ABBREVS[(now.weekday() - 1) % 7]
    current = now.time()

    for window in quiet_hours:
        start = _parse_hhmm(window.get("start_time"))
        end = _parse_hhmm(window.get("end_time"))
        if start is None or end is None:
            continue
        days = window.get("days") or []
        if start <= end:
            if today in days and start <= current <= end:
                return True
        else:
            if today in days and current >= start:
                return True
            if yesterday in days and current <= end:
                return True
    return False


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

        # _safe_init statt direktem Konstruktor-Aufruf: ein I2C-Glitch beim
        # Boot (z.B. durch einen Spannungseinbruch) soll nur NFC deaktivieren,
        # nicht die ganze Box crashen — Voice/Buttons/Random-Mode funktionieren
        # auch ohne Tag-Leser weiter.
        self.nfc = self._safe_init("nfc", PN532)
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
        # Serialisiert Audio-Sync-Trigger: der 5-Min-Loop und der Sofort-Sync
        # beim Auflegen einer Figur (M1) dürfen nicht gleichzeitig laufen.
        self._sync_lock = threading.Lock()

        # Energiesparen (Standby/Shutdown bei Inaktivität, siehe *_TIMEOUT_S).
        self._last_activity = time.monotonic()
        self._standby = False
        # Verhindert Log-Spam, wenn der Auto-Shutdown im Dev-Modus (Desktop
        # läuft) wiederholt ausgesetzt wird — wir loggen das nur einmal.
        self._shutdown_suppressed_logged = False
        # Kurz nach dem Aufwecken Eingaben verwerfen, damit das Loslassen des
        # Weck-Drucks (down→up-Paar) nicht doch eine Aktion auslöst.
        self._wake_guard_until = 0.0

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

        # Yellow-Hold-State: Snapshot vom Pause-Status beim Druck, damit der
        # Release-Callback entscheiden kann ob Toggle (kurz) oder Resume (Hold).
        self._yellow_was_paused_before_press = False

        # LED-Streifen User-Toggle: per Gelb-Hold (≥ 3s) an/aus. Default aus,
        # damit der User sie bewusst aktiviert (kein lautes Lichtspiel beim
        # Boot).
        self._strips_user_enabled = False

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
        # Referenzzähler-Besitz der orangen Sync-Optik: Download-Loop UND Dongel-
        # Feedback teilen sich denselben Ring/Status-Puls. Nur der erste Pfad
        # startet, nur der letzte stoppt — verhindert, dass ein Pfad die Optik
        # eines parallel laufenden anderen vorzeitig abwürgt.
        self._sync_led_lock = threading.Lock()
        self._sync_led_owners = 0

        # Audio-Level-Poller: liest 20×/s den RMS-Pegel von mpv (via astats-
        # Filter, siehe player.AUDIO_DEVICE/af) und reicht ihn an die LED-
        # Streifen weiter (audio-reaktiver Tanz). Ersetzt den früheren snd-
        # aloop-Capture-Pfad, der mit Multi-Device auf Pi 5 / googlevoicehat
        # nicht stabil lief (mpv ging nach 250 ms in idle). Vorteil: keine
        # Soundkarten-Multiplexerei, kostet ~0% CPU. Nachteil: nur ein Wert
        # (RMS) statt 16-Band-Spektrum — Streifen pulsieren statt Bass/Treble
        # zu trennen.
        self._spectrum_thread: Optional[threading.Thread] = None
        self._spectrum_stop = threading.Event()

        # Voice-Stack. Backend (vosk|whisper) kommt aus config.json → "voice.backend".
        # Recognizer instanziieren ist billig; das eigentliche Modell wird in einem
        # Daemon-Thread vorgeladen (Warmup), sodass die erste Push-to-Talk-Session
        # nicht auf den 1–3 s Modell-Load warten muss. Schlägt der Warmup fehl
        # (Paket/Modell fehlt), bleibt der bestehende Lazy-Load-Pfad in
        # transcribe_wav als Fallback aktiv und der echte Push-to-Talk wirft den
        # Fehler dann sichtbar.
        self._recognizer = build_recognizer(self.config.get("voice"))
        # Schneller Keyword-Erkenner NUR fürs Zauberwort "bitte": Vosk (Kaldi,
        # streamt → ~1s) statt Whisper (fixe ~3.3s pro 30s-Encoder-Fenster, egal
        # wie kurz der Clip). Das Zauberwort ist EIN häufiges Wort — dafür ist
        # Vosk-small völlig ausreichend und spart bei der Rückfrage ~2s. Backend
        # bleibt sonst Whisper; nur der Zauberwort-Check geht über diesen Vosk.
        # Modellpfad aus dem vosk-Block der config (Default greift sonst).
        _vosk_cfg = dict((self.config.get("voice") or {}).get("vosk") or {})
        if "model_dir" in _vosk_cfg:
            _vosk_cfg["model_dir"] = Path(_vosk_cfg["model_dir"])
        try:
            self._magic_word_recognizer: Optional[Recognizer] = Recognizer(
                backend="vosk", **_vosk_cfg
            )
        except Exception as e:
            logger.info("Magic-Word-Vosk nicht verfügbar (%s) — Whisper-Fallback.", e)
            self._magic_word_recognizer = None
        # TTS für die Titel-Ansage ("Wie heißt dieses Lied?"). Piper primär,
        # espeak-ng-Fallback, beides lazy/optional — fehlt das Modell, bleibt
        # die Box voll funktionsfähig und sagt einen festen Prompt an. Stimme
        # (männlich/weiblich) kommt vom Backend, persistiert in config['tts_voice'].
        _voice_name = self.config.get("tts_voice", TTS_VOICE_DEFAULT)
        self._speaker = TitleSpeaker(
            model_path=TTS_MODELS.get(_voice_name, TTS_MODELS[TTS_VOICE_DEFAULT])
        )
        threading.Thread(
            target=self._warmup_voice, daemon=True, name="voice-warmup"
        ).start()
        self._mic_recorder = MicRecorder()
        self._voice_lock = threading.Lock()  # nur eine Voice-Session gleichzeitig

        # Track-Ende-Callback an Player binden
        self.player.on_track_end(self._on_track_end)

        # PlaySession-Reporter: meldet abgeschlossene Wiedergaben ans Backend.
        # send_fn ist defensiv — wenn backend zur Laufzeit weggeht (Token weg,
        # Netz offline), gibt Backend.play_session() False zurück und der
        # Reporter retryt später.
        def _send_session(payload: dict) -> bool:
            return bool(self.backend) and self.backend.play_session(payload)
        self.play_session_reporter = PlaySessionReporter(
            send_fn=_send_session,
            queue_path=PLAY_SESSION_QUEUE_PATH,
        )

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
        w = self._wrap_input  # zählt als Aktivität + weckt aus Standby (Druck wird dann verbraucht)
        self.buttons.on_green(w(self._on_green_pressed))
        self.buttons.on_green_stop(w(self._on_green_stop))
        self.buttons.on_green_held(w(self._on_green_held))
        self.buttons.on_red(w(self._on_red_pressed))
        self.buttons.on_red_stop(w(self._on_red_stop))
        self.buttons.on_red_held(w(self._on_red_held))
        self.buttons.on_push(w(self._on_push_pressed))
        self.buttons.on_push_held(w(self._on_push_held))
        self.buttons.on_yellow_down(w(self._on_yellow_down))
        self.buttons.on_yellow(w(self._on_yellow_pressed))
        self.buttons.on_yellow_held(w(self._on_yellow_held))
        self.buttons.on_blue(w(self._on_blue_pressed))

    def _wire_encoder(self) -> None:
        if self.encoder is None:
            return
        # gpiozero "clockwise" entspricht der physischen Drehung im Uhrzeigersinn
        # (mit CLK=GPIO17, DT=GPIO27 stimmt das hier; in einem früheren Test war
        # ich kurz verwirrt — diese Variante ist die richtige).
        # Im Speed-Mode steuert der Encoder die Wiedergabegeschwindigkeit
        # statt der Lautstärke — siehe _on_encoder_*.
        self.encoder.on_clockwise(self._wrap_input(self._on_encoder_cw))
        self.encoder.on_counterclockwise(self._wrap_input(self._on_encoder_ccw))

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

    def _playback_session_callbacks(
        self,
        source: str,
        kaka_id: Optional[int],
        used_zauberwort: Optional[bool] = None,
    ):
        """Liefert (on_start, on_end) für eine Playlist, die alle Track-Wechsel
        an den PlaySessionReporter durchreicht. Closures merken sich source
        und kaka_id — pro Playlist konstant, daher hier gebunden.
        """
        reporter = self.play_session_reporter

        def on_start(content) -> None:
            reporter.start(
                content_id=content.content_id,
                kaka_id=kaka_id,
                source=source,
                used_zauberwort=used_zauberwort,
            )

        def on_end(content, end_reason: str, position: float) -> None:
            reporter.end(end_reason=end_reason, position_seconds=position)

        return on_start, on_end

    def run(self) -> None:
        self._running = True
        # Normalbetrieb auf reaktivem Governor starten. Wichtig: der Standby
        # setzt 'powersave' — wird die Box im Standby neu gestartet/rebootet,
        # bliebe der Governor sonst dauerhaft auf powersave (zu langsam für
        # Voice-ASR/Playback).
        self._set_cpu_governor("ondemand")

        # Reporter-Worker startet immer — auch ohne Backend-Verbindung. Die
        # Queue persistiert auf Disk und wird verarbeitet, sobald sich die
        # Box wieder verbindet.
        self.play_session_reporter.start_worker()

        threading.Thread(target=self._nfc_loop, daemon=True, name="nfc").start()
        if self.backend and self.backend.is_connected:
            threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat").start()
            threading.Thread(target=self._audio_sync_loop, daemon=True, name="audio-sync").start()

        # Energiespar-Watcher: Standby nach 1 Min, Shutdown nach 5 Min Inaktivität.
        threading.Thread(target=self._idle_watch_loop, daemon=True, name="idle-watch").start()

        # Audio-Level-Loop: pollt mpv-RMS 20×/s und füttert LED-Streifen. Bei
        # paused/idle/Stille liefert player.current_audio_level() 0.0, LEDs
        # zeigen dann schwarz (im Dance-Mode). Sehr leichtgewichtig (nur
        # property-read), darf dauerhaft laufen.
        if self.leds is not None:
            self._spectrum_stop.clear()
            self._spectrum_thread = threading.Thread(
                target=self._audio_level_loop, daemon=True, name="audio-level"
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
    # Energiesparen: Standby (LEDs/CPU/Loops) + Auto-Shutdown
    # ------------------------------------------------------------------

    def _note_activity(self) -> None:
        self._last_activity = time.monotonic()

    def _is_active(self) -> bool:
        """True, wenn die Box gerade in Benutzung ist — dann kein Standby/Shutdown.

        Bewusst NICHT an _current_playlist gekoppelt: das bleibt nach
        "Playlist beendet" gesetzt und würde Standby dauerhaft verhindern.
        Maßgeblich sind: Chip liegt auf, oder es läuft (nicht-pausiert) Audio.
        Kurze Voice-Aufnahmen sind über die jüngste Knopf-Aktivität abgedeckt.
        """
        if self._active_tag_uid is not None:
            return True
        try:
            st = self.player.get_state()
            if getattr(st, "playing", False) and not getattr(st, "paused", False):
                return True
        except Exception:
            pass
        return False

    def _wrap_input(self, fn):
        """Wrapper für Knopf-/Encoder-Callbacks: zählt als Aktivität und weckt
        aus dem Standby. Im Standby wird der erste Druck NUR zum Aufwecken
        verbraucht (die eigentliche Aktion läuft nicht), damit niemand z.B.
        durch plötzliches Lautwerden erschrickt.
        """
        def wrapped(*args, **kwargs):
            self._note_activity()
            if self._standby:
                self._wake_from_standby("Knopf")
                return None
            # Direkt nach dem Aufwecken kurz alle Eingaben verwerfen (das
            # Loslassen des Weck-Drucks würde sonst eine Aktion triggern).
            if time.monotonic() < self._wake_guard_until:
                return None
            return fn(*args, **kwargs)
        return wrapped

    def _set_cpu_governor(self, name: str) -> None:
        """CPU-Governor via sudo-Helper setzen (best-effort; ohne Helper no-op)."""
        try:
            subprocess.run(
                ["sudo", "-n", "/usr/local/bin/kakabox-cpu-governor", name],
                check=False, capture_output=True, timeout=5,
            )
        except Exception as e:
            logger.debug("CPU-Governor %s nicht gesetzt: %s", name, e)

    def _enter_standby(self) -> None:
        if self._standby:
            return
        self._standby = True
        logger.info("💤 Standby (nach %ds inaktiv) — LEDs aus, CPU powersave, Sync pausiert.",
                    STANDBY_TIMEOUT_S)
        if self.leds is not None:
            try:
                self.leds.standby()
            except Exception as e:
                logger.warning("LED-Standby fehlgeschlagen: %s", e)
        self._set_cpu_governor("powersave")

    def _wake_from_standby(self, reason: str = "") -> None:
        if not self._standby:
            return
        self._standby = False
        self._note_activity()
        self._wake_guard_until = time.monotonic() + 1.0
        logger.info("☀️  Aufgeweckt (%s).", reason or "Aktivität")
        self._set_cpu_governor("ondemand")
        if self.leds is not None:
            try:
                self._restore_idle_led()
            except Exception:
                pass

    def _idle_watch_loop(self) -> None:
        """Inaktivitäts-Überwachung: nach STANDBY_TIMEOUT_S → Standby, nach
        SHUTDOWN_TIMEOUT_S → kompletter Shutdown (tschau kakau)."""
        while self._running:
            for _ in range(10):  # ~5s, reaktiv auf Stop
                if not self._running:
                    return
                time.sleep(0.5)

            if self._is_active():
                self._note_activity()
                continue

            idle = time.monotonic() - self._last_activity
            if idle >= SHUTDOWN_TIMEOUT_S:
                if graphical_session_active():
                    # Desktop läuft → wir entwickeln gerade. Kein Auto-Shutdown,
                    # sonst stirbt die Box mitten in der Arbeit weg. Der Standby
                    # (1 Min) ist davon unberührt und greift weiterhin. Nur
                    # einmal loggen, nicht alle paar Sekunden.
                    if not self._shutdown_suppressed_logged:
                        logger.info("🖥️  Grafische Oberfläche aktiv → "
                                    "Auto-Shutdown ausgesetzt (Dev-Modus).")
                        self._shutdown_suppressed_logged = True
                    continue
                logger.warning("⏻ %d Min inaktiv → Box fährt herunter.",
                               SHUTDOWN_TIMEOUT_S // 60)
                self._power_off("Auto-Shutdown nach Inaktivität")
                return
            if not self._standby and idle >= STANDBY_TIMEOUT_S:
                self._enter_standby()

    # ------------------------------------------------------------------
    # Hintergrund-Loops
    # ------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        self._send_heartbeat()
        self._poll_commands()
        while self._running:
            for _ in range(HEARTBEAT_INTERVAL * 10):
                if not self._running:
                    return
                time.sleep(0.1)
            self._send_heartbeat()
            # Webapp-Commands gleich nach dem Heartbeat abholen (#18). Ohne das
            # blieben Modus-/Finder-/Volume-/Resync-Aktionen aus der Webapp ohne
            # jede Wirkung auf der Box (nur Erfolgs-Toast, nichts passiert).
            self._poll_commands()

    def _audio_sync_loop(self) -> None:
        time.sleep(5)
        while self._running:
            self._sync_audio_manifest()
            for _ in range(AUDIO_SYNC_INTERVAL * 10):
                if not self._running:
                    return
                time.sleep(0.1)

    def _send_heartbeat(self) -> None:
        if not self.backend or self._standby:
            return
        payload: dict = {
            "volume": self._volume,
            "wifi_ssid": read_wifi_ssid(),
        }
        # Box-Modus mitmelden, damit die Webapp-Anzeige nicht dauerhaft stale
        # ist (#44). Wird per set_mode/refresh_settings-Command gepflegt.
        mode = self.config.get("mode")
        if mode:
            payload["mode"] = mode
        cpu_load = read_cpu_load_percent()
        if cpu_load is not None:
            # 1-Min-Schnitt aus /proc/loadavg. Wird auf einer Box auf der
            # Webapp als "Auslastung" angezeigt (kein Watt-Wert, das hat der
            # Pi nicht). 30-Sekunden-Heartbeat (HEARTBEAT_INTERVAL) → Wert
            # wandert recht zügig.
            payload["cpu_load_percent"] = cpu_load
        try:
            self.backend.heartbeat(payload)
        except Exception as e:
            logger.warning("heartbeat failed: %s", e)

    # ------------------------------------------------------------------
    # Webapp-Commands (Pull-Modell, #18)
    # ------------------------------------------------------------------

    def _poll_commands(self) -> None:
        """Holt anstehende Webapp-Commands und führt sie aus.

        Wird nach jedem Heartbeat aufgerufen. Ack erfolgt IMMER nach dem
        Versuch — auch bei unbekanntem oder fehlerhaftem Command —, weil diese
        Commands lokale, nicht-transiente Aktionen sind; ein nicht-ack'ter
        Eintrag würde sonst bei jedem Heartbeat erneut feuern (Poison-Pill).
        """
        if not self.backend or not self.backend.is_connected or self._standby:
            return
        try:
            commands = self.backend.fetch_commands()
        except Exception as e:
            logger.warning("fetch_commands fehlgeschlagen: %s", e)
            return
        for cmd in commands:
            cmd_id = cmd.get("id")
            name = cmd.get("command")
            payload = cmd.get("payload") or {}
            try:
                self._dispatch_command(name, payload)
            except Exception as e:
                logger.warning("Command '%s' (id=%s) warf %s — wird verworfen.", name, cmd_id, e)
            if cmd_id is not None and self.backend:
                self.backend.acknowledge_command(cmd_id)

    def _dispatch_command(self, name: str, payload: dict) -> None:
        """Führt einen einzelnen Webapp-Command aus. Siehe BoxCommand im
        Backend für das Vokabular."""
        if name == "set_volume":
            vol = payload.get("volume")
            if vol is not None:
                self._set_volume(int(vol))
        elif name == "set_mode":
            mode = payload.get("mode")
            if mode:
                self._set_mode(str(mode))
        elif name == "sync_audio":
            # Voller Manifest-Sync (kaka_id im Payload ignorieren wir — der
            # Sync deckt ohnehin alle Inhalte der Box ab).
            self._trigger_background_sync("command")
        elif name == "refresh_settings":
            # max_volume + enable_zauberwort über den bestehenden Rule-Pfad.
            self._apply_rule_from_manifest({
                "max_volume": payload.get("max_volume"),
                "enable_zauberwort": payload.get("enable_zauberwort"),
            })
            if payload.get("mode"):
                self._set_mode(str(payload["mode"]))
            # TTS-Stimme (männlich/weiblich) sofort umschalten, falls im Payload.
            self._apply_tts_voice(payload.get("tts_voice"))
        elif name == "finder":
            # Lokalisierungs-Ton in eigenem Thread, damit der Heartbeat-Loop
            # nicht durch die Wiedergabe blockiert.
            threading.Thread(target=self._play_finder, daemon=True, name="finder").start()
        else:
            logger.warning("Unbekannter Command '%s' — verworfen.", name)

    def _set_mode(self, mode: str) -> None:
        """Persistiert den Box-Modus (normal/abend/nacht/flug/offline) und meldet
        ihn fortan im Heartbeat. Die konkrete Modus-Semantik (z.B. Nacht =
        leiser) ist noch nicht implementiert — der Wert wird gespeichert, damit
        Webapp-Anzeige und spätere Modus-Logik konsistent sind.
        """
        valid = {"normal", "abend", "nacht", "flug", "offline"}
        if mode not in valid:
            logger.warning("Unbekannter Modus '%s' ignoriert.", mode)
            return
        if self.config.get("mode") == mode:
            return
        logger.info("Box-Modus: %s → %s", self.config.get("mode", "normal"), mode)
        self.config["mode"] = mode
        save_config(self.config)

    def _play_finder(self) -> None:
        """Spielt einen lauten 'Box-suchen'-Ton. Nutzt finder.wav, falls
        vorhanden — sonst eine vorhandene Ansage als hörbaren Fallback."""
        loud = max(self._volume, 80)
        if (PROMPTS_DIR / "finder.wav").is_file():
            self._play_prompt("finder.wav", volume=loud)
        else:
            logger.info("Kein finder.wav vorhanden — Fallback-Ansage für Finder.")
            self._play_prompt("ready_to_rumble.wav", volume=loud)

    def _trigger_background_sync(self, reason: str) -> None:
        """Stößt einen Audio-Sync sofort an (Hintergrund-Thread), statt auf den
        nächsten 5-Min-Zyklus zu warten — z.B. wenn eine Figur aufgelegt wird
        und ein neu verknüpftes Lied noch fehlt. _sync_audio_manifest ist gegen
        Mehrfachläufe selbst abgesichert, ein paralleler Trigger ist also
        gefahrlos (no-op, wenn schon einer läuft).
        """
        if not self.backend or not self.backend.is_connected:
            return
        threading.Thread(
            target=self._sync_audio_manifest, daemon=True, name=f"sync-{reason}",
        ).start()

    def _sync_audio_manifest(self) -> None:
        """Wrapper: serialisiert gleichzeitige Sync-Trigger (5-Min-Loop +
        Sofort-Sync beim Auflegen). Läuft schon einer, wird der Trigger
        verworfen — verhindert doppelte Downloads und Cleanup-Races.
        """
        if not self.backend or not self.backend.is_connected or self._standby:
            return
        if not self._sync_lock.acquire(blocking=False):
            logger.debug("Sync läuft bereits — Trigger übersprungen.")
            return
        try:
            self._sync_audio_manifest_locked()
        finally:
            self._sync_lock.release()

    def _sync_audio_manifest_locked(self) -> None:
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
        # Orange Sync-Optik (Kometen-Ring + Status-LED-Puls) während echter
        # Downloads. Lazy gestartet erst beim ersten tatsächlichen Download —
        # ein reiner No-op-Poll (alles gecached) blinkt also nicht.
        sync_led_started = False

        try:
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
                # Dedup: liegt der gleiche Inhalt (Hash) schon unter einer anderen
                # content_id im Cache? Backend hat manche Songs doppelt eingespielt
                # (gleicher Hash, andere ID) — kein zweiter Download nötig, einfach
                # hardlinken. Spart Bandbreite + Speicher.
                existing = self.audio_cache.find_by_hash(file_hash)
                if existing is not None:
                    if self.audio_cache.link_from(existing, content_id):
                        cached_already += 1
                        self.backend.report_audio_cached(content_id, file_hash)
                        continue
                # Backoff: wenn der Download vor < SYNC_RETRY_BACKOFF_SECONDS
                # bereits fehlgeschlagen ist, nicht nochmal versuchen. Spart
                # Log-Spam + Bandbreite, wenn das Backend dauerhaft 404 liefert.
                last_fail = self._sync_failures.get(content_id)
                if last_fail and now - last_fail < SYNC_RETRY_BACKOFF_SECONDS:
                    skipped_backoff += 1
                    continue

                # Erster echter Download → orange Optik an (referenzgezählt, teilt
                # sich die Optik sauber mit einer evtl. parallelen Dongel-Animation).
                if not sync_led_started:
                    self._sync_led_acquire()
                    sync_led_started = True

                logger.debug("Sync: lade '%s' (id=%d)", entry.get("title"), content_id)
                target = self.audio_cache.path_for(content_id)
                with self.audio_cache.download_guard(content_id):
                    # Erneut prüfen: eine laufende Playlist kann denselben
                    # Content parallel per Prefetch geladen haben, während wir
                    # hier auf den Lock gewartet haben.
                    if self.audio_cache.is_cached(content_id, file_hash):
                        cached_already += 1
                        continue
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
        finally:
            # Optik freigeben, sobald die Downloads durch sind (oder bei
            # Shutdown-Return mittendrin). Nur wenn wir sie selbst geholt haben.
            if sync_led_started:
                self._sync_led_release()

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

        Dedup: Backend hat manche Songs doppelt (gleicher Hash, andere
        content_id) — für den Sprachbefehl ist nur ein Eintrag pro Hash
        sinnvoll, sonst matched "spiele Hexentanz" zufällig eine der beiden
        IDs. Wir behalten den ersten und mergen die Aliase aller Dubletten.
        Fallback (Backend liefert keinen Hash): dedup per (Titel, lower).

        Best-effort: bei IO-Fehlern (read-only FS, Disk voll) wird nur gewarnt —
        Voice-Match fällt dann auf den letzten gültigen Stand zurück.
        """
        songs: list[dict] = []
        seen_keys: dict[str, int] = {}  # dedup-key → index in songs
        for entry in files:
            cid = entry.get("content_id")
            if not cid:
                continue
            title = entry.get("title") or ""
            aliases = list(entry.get("aliases") or [])
            genre = (entry.get("genre") or "").strip() or None
            key = entry.get("file_hash") or title.strip().lower()
            if not key:
                continue
            if key in seen_keys:
                # Aliase der Dublette in den bestehenden Eintrag mergen,
                # damit kein Aufrufname verloren geht.
                existing = songs[seen_keys[key]]
                for a in aliases:
                    if a and a not in existing["aliases"]:
                        existing["aliases"].append(a)
                continue
            seen_keys[key] = len(songs)
            songs.append({
                "content_id": int(cid),
                "title": title,
                "aliases": aliases,
                "genre": genre,
            })
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "songs": songs,
        }
        try:
            # Atomar: tmp-Datei + os.replace, damit ein gleichzeitiger Lesevorgang
            # (Push-to-Talk-Befehl, main.py build_catalog_from_file) nie eine
            # leere/abgeschnittene Datei sieht.
            tmp_path = VOICE_CATALOG_PATH.with_name(VOICE_CATALOG_PATH.name + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(VOICE_CATALOG_PATH)
        except OSError as e:
            logger.warning("Voice-Catalog konnte nicht geschrieben werden: %s", e)

    def _apply_settings_from_manifest(self, settings: dict) -> None:
        """Übernimmt Per-Box-Settings (system_volume, tts_voice) aus dem Manifest.

        Override-Logik: Webapp-Wert gewinnt, wird in config.json gespiegelt
        damit ein Neustart nach Offline-Phase den letzten Stand behält. Fehlende
        oder ungültige Werte werden ignoriert (kein Reset auf Default). Jedes Feld
        wird UNABHÄNGIG angewandt.
        """
        sys_vol = settings.get("system_volume")
        if sys_vol is not None:
            try:
                sys_vol = max(0, min(100, int(sys_vol)))
                if sys_vol != self._system_volume:
                    logger.info("system_volume vom Backend: %d → %d", self._system_volume, sys_vol)
                    self._system_volume = sys_vol
                    self.config["system_volume"] = sys_vol
                    save_config(self.config)
            except (TypeError, ValueError):
                pass

        self._apply_tts_voice(settings.get("tts_voice"))

    def _apply_tts_voice(self, voice_name) -> None:
        """Schaltet die TTS-Stimme (männlich/weiblich) gemäß Backend-Vorgabe um.

        Nur bei gültigem, geändertem Wert: config spiegeln, Modell wechseln und
        im Hintergrund vorladen (damit die erste Ansage nach dem Wechsel nicht
        auf den Modell-Load wartet). Unbekannte/leere Werte → ignorieren.
        """
        if voice_name not in TTS_MODELS:
            return
        if voice_name == self.config.get("tts_voice", TTS_VOICE_DEFAULT):
            return
        logger.info(
            "TTS-Stimme vom Backend: %s → %s",
            self.config.get("tts_voice", TTS_VOICE_DEFAULT), voice_name,
        )
        self.config["tts_voice"] = voice_name
        save_config(self.config)
        self._speaker.set_model(TTS_MODELS[voice_name])
        threading.Thread(target=self._speaker.warmup, daemon=True, name="tts-revoice").start()

    def _apply_rule_from_manifest(self, rule: dict) -> None:
        """Übernimmt die Box-Rule aus dem Manifest: ``max_volume`` (Hard-Cap)
        und ``enable_zauberwort`` (Höflichkeits-Gate fürs Voice-Matching).

        Beide Felder werden UNABHÄNGIG voneinander angewandt — ein fehlendes
        Feld lässt den lokalen Stand unverändert (kein Reset auf Default). Der
        max_volume-Cap begrenzt, wie weit der User per Encoder hochdrehen kann;
        liegt die aktuelle Lautstärke nach einem strengeren Cap zu hoch, wird
        sie sofort runtergezogen. Alles wird in config.json gespiegelt, damit
        ein Offline-Boot den letzten Stand behält.
        """
        # --- max_volume (Hard-Cap für die User-Lautstärke) ---
        max_vol = rule.get("max_volume")
        if max_vol is not None:
            try:
                max_vol = max(0, min(100, int(max_vol)))
            except (TypeError, ValueError):
                logger.warning("rule.max_volume nicht numerisch: %r", max_vol)
                max_vol = None
            if max_vol is not None and max_vol != self._max_volume:
                logger.info("max_volume vom Backend: %d → %d", self._max_volume, max_vol)
                self._max_volume = max_vol
                self.config["max_volume"] = max_vol
                save_config(self.config)
                # Aktuelle Lautstärke über dem neuen Cap? Sofort runter — mit dem
                # üblichen _adjust_volume-Pfad, damit Player + LEDs konsistent sind.
                if self._volume > self._max_volume:
                    self._adjust_volume(self._max_volume - self._volume)

        # --- enable_zauberwort (Höflichkeits-Gate) ---
        # Spiegelt den Webapp-Toggle nach config['zauberwort_mode_enabled'],
        # damit er ohne Box-Reboot greift (vorher las das Gerät die Rule nie aus
        # → der Modus war faktisch tot, egal was in der Webapp stand).
        zw = rule.get("enable_zauberwort")
        if zw is not None:
            zw = bool(zw)
            if zw != bool(self.config.get("zauberwort_mode_enabled", False)):
                logger.info("Zauberwort-Modus vom Backend: %s", "an" if zw else "aus")
                self.config["zauberwort_mode_enabled"] = zw
                save_config(self.config)

        # --- quiet_hours + blocked_category_ids (H5-Fix, QS-Audit 2026-07-07) ---
        # Vorher wurden diese Felder vom Gerät nie ausgelesen — die Kindersicherung
        # war reine Placebo-UI ("gespeichert ✅", die Box wusste nie davon). Beide
        # werden — wie max_volume/enable_zauberwort — unabhängig voneinander und
        # nur bei tatsächlicher Übermittlung angewandt (kein Feld im Payload =
        # lokaler Stand bleibt, z.B. wenn ein älteres Backend das Feld noch nicht
        # kennt). Durchsetzung selbst passiert in _start_kaka_playlist &co.
        if "quiet_hours" in rule:
            qh = rule.get("quiet_hours") or []
            if qh != self.config.get("quiet_hours"):
                logger.info("quiet_hours vom Backend aktualisiert (%d Fenster).", len(qh))
                self.config["quiet_hours"] = qh
                save_config(self.config)

        if "blocked_category_ids" in rule:
            blocked = rule.get("blocked_category_ids") or []
            if blocked != self.config.get("blocked_category_ids"):
                logger.info("blocked_category_ids vom Backend aktualisiert: %s", blocked)
                self.config["blocked_category_ids"] = blocked
                save_config(self.config)

    def _quiet_hours_now(self) -> bool:
        return quiet_hours_active(self.config.get("quiet_hours") or [], datetime.now())

    def _is_category_blocked(self, category_id) -> bool:
        if category_id is None:
            return False
        return category_id in (self.config.get("blocked_category_ids") or [])

    def _flash_playback_denied(self, chip_present: bool = True) -> None:
        """Kurzer roter LED-Blitz OHNE Ton — Feedback bei Ruhezeit/Kategorie-
        sperre (H5-Fix). Bewusst kein Sound/Prompt: eine Ruhezeit greift
        typischerweise abends/nachts, ein Erklär-Prompt würde genau dann
        wecken/stören, wenn er es am wenigsten soll.

        ``chip_present=True`` (Default, NFC-Pfad): danach zurück auf 'Chip
        erkannt' — der Tag liegt beim Aufruf aus _start_kaka_playlist ja noch
        auf dem Leser. ``False`` (Voice-Knopf ohne Chip): LED aus statt
        fälschlich grün zu pulsieren."""
        if self.leds is None:
            return
        self.leds.nfc_flash_error()
        time.sleep(1.2)
        if self.leds is not None:
            if chip_present:
                self.leds.nfc_chip_present()
            else:
                self.leds.nfc_chip_absent()

    # ------------------------------------------------------------------
    # NFC-Loop (mit Multi-Chip-Tracking)
    # ------------------------------------------------------------------

    def _nfc_loop(self) -> None:
        if self.nfc is None:
            return
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

            # Im Standby seltener pollen (spart CPU); ein aufgelegter Chip
            # weckt dann innerhalb von ~STANDBY_NFC_POLL_S.
            time.sleep(STANDBY_NFC_POLL_S if self._standby else 0.05)

    # ------------------------------------------------------------------
    # Tag-Handling
    # ------------------------------------------------------------------

    def _handle_tag(self, uid: str) -> None:
        logger.info("NFC tag erkannt: %s", uid)
        # Chip-Auflegen zählt als Aktivität und weckt aus dem Standby — anders
        # als ein Knopf wird hier NICHT verbraucht: die Box wacht auf UND spielt.
        self._note_activity()
        if self._standby:
            self._wake_from_standby("Chip")
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
        # Nur abspielbare Inhalte betrachten — eine unveröffentlichte/dateilose
        # Verknüpfung kann gar nicht lokal liegen und darf den Cache-First-Pfad
        # nicht blockieren (sonst nie Instant-Start). Spiegelt den Filter in
        # _start_kaka_playlist.
        contents = [c for c in (kaka.get("contents") or []) if c.get("playable", True)]
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
            # M2: laufende Playlist live nachziehen, falls sich der Inhalt der
            # noch aktiven Figur geändert hat (z.B. 3. Lied verknüpft) — ohne
            # die aktuelle Wiedergabe zu unterbrechen.
            self._refresh_active_playlist(uid, kaka)
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

    def _refresh_active_playlist(self, uid: str, kaka: dict) -> None:
        """Zieht die Track-Liste der LAUFENDEN Playlist nach, falls ``uid`` noch
        die aktive Figur ist. Baut KakaContent aus den (playable) Inhalten und
        ruft ``Playlist.update_contents`` — das laufende Audio bricht NICHT ab.

        Bei echter Änderung wird der LED-Track-Balken neu gesetzt und ein
        Sofort-Sync angestoßen, damit ein neu hinzugekommenes (noch fehlendes)
        Lied gleich geladen und beim Erreichen abspielbar ist.
        """
        new_contents = [
            KakaContent(
                content_id=c["id"],
                title=c.get("title", ""),
                file_hash=c.get("file_hash"),
                download_url=c.get("download_url"),
                cached_locally=bool(c.get("cached_locally")),
                sort_order=int(c.get("sort_order", 0)),
                category_id=c.get("category_id"),
            )
            for c in (kaka.get("contents") or [])
            if c.get("id") and c.get("playable", True)
            # Kategoriesperre (H5-Fix): ein neu verknüpftes, gesperrtes Lied darf
            # nicht in die laufende Playlist rutschen.
            and not self._is_category_blocked(c.get("category_id"))
        ]
        # Leere Liste nicht anwenden — sonst würde eine laufende Wiedergabe
        # ihre Track-Liste verlieren. (Status-Wechsel auf "keine Lieder" wird
        # ohnehin über den normalen Tag-Scan-Pfad behandelt.)
        if not new_contents:
            return

        with self._playlist_lock:
            playlist = self._current_playlist if self._active_tag_uid == uid else None
        if playlist is None:
            return

        if playlist.update_contents(new_contents):
            if self.leds is not None:
                self.leds.strips_show_position(
                    playlist.current_index, playlist.length,
                )
            # Dongel-Änderung erkannt → Sync MIT Orange-Feedback + Bestätigungs-
            # sound (eigener Thread, blockiert den Tag-Refresh nicht).
            threading.Thread(
                target=self._run_sync_with_feedback, args=(uid, kaka),
                daemon=True, name="sync-feedback",
            ).start()

    def _run_sync_with_feedback(self, uid: str, kaka: dict) -> None:
        """Sync für eine erkannte Dongel-Änderung MIT Feedback: Status-LED orange
        pulsieren + Ring-Komet (mind. SYNC_FEEDBACK_MIN_S, auch wenn nichts zu
        laden ist), danach der Voice-Success-Sound als Bestätigung, dass der Sync
        geklappt hat. Läuft im Hintergrund — Wiedergabe/Bedienung bleiben möglich.
        """
        self._sync_led_acquire()
        t0 = time.monotonic()
        try:
            self._sync_audio_manifest()
        finally:
            remaining = SYNC_FEEDBACK_MIN_S - (time.monotonic() - t0)
            if remaining > 0:
                time.sleep(remaining)
            self._sync_led_release()
        self._play_sync_confirmation(uid, kaka)

    def _play_sync_confirmation(self, uid: str, kaka: dict) -> None:
        """Bestätigungssound (derselbe wie bei einem Voice-Treffer). Läuft gerade
        ein Lied, wird die Position gemerkt, der Sound kurz darübergespielt und
        das Lied an der Stelle fortgesetzt (KEIN erneuter Sync → keine Schleife).
        """
        with self._playlist_lock:
            resume = self._active_tag_uid == uid and self._current_playlist is not None
            idx = self._current_playlist.current_index if resume else 0
        pos = 0.0
        if resume:
            try:
                pos = float(self.player.current_position_seconds() or 0.0)
            except Exception:
                pos = 0.0

        self._play_prompt("voice_success.wav")
        self.player.wait_until_idle(timeout=2.0)

        if resume:
            self._last_kaka_memory = KakaMemory(
                tag_uid=uid, track_index=idx, position_seconds=pos,
            )
            self._start_kaka_playlist(uid, kaka, trigger_sync=False)

    def _start_kaka_playlist(self, uid: str, kaka: dict, trigger_sync: bool = True) -> None:
        # Ruhezeit aktiv? Stumm ablehnen — kein Ton/Prompt, nur kurzer roter
        # LED-Blitz (H5-Fix). Gilt für die ganze Box, unabhängig von der Kaka.
        if self._quiet_hours_now():
            logger.info("Ruhezeit aktiv — Wiedergabe von '%s' unterdrückt (still).", kaka.get("name"))
            self._flash_playback_denied()
            return

        # Nur tatsächlich auslieferbare Lieder (veröffentlicht + Audiodatei) in
        # die Playlist nehmen. 'playable' kommt vom Backend (formatKaka); fehlt
        # das Feld (älteres Backend), gilt der Track als spielbar (Kompat).
        # So zählt der LED-Track-Balken nie ein Lied mit, das nie geladen
        # werden kann ("3/3 angezeigt, aber 3. Lied unerreichbar").
        # trigger_sync=False: Resume-Aufrufe (z.B. nach dem Sync-Bestätigungssound)
        # dürfen KEINEN neuen Sync auslösen — sonst Feedback→Sync→Feedback-Schleife.
        contents_data = [
            c for c in kaka.get("contents", []) if c.get("playable", True)
        ]
        # Kategoriesperre (H5-Fix): einzelne gesperrte Lieder rausfiltern, statt
        # die ganze Kaka zu blockieren — eine Kaka kann Lieder aus mehreren
        # Kategorien enthalten.
        had_contents = bool(contents_data)
        contents_data = [
            c for c in contents_data if not self._is_category_blocked(c.get("category_id"))
        ]
        if not contents_data:
            if had_contents:
                logger.info(
                    "Alle Lieder von '%s' sind kategoriegesperrt — Wiedergabe unterdrückt (still).",
                    kaka.get("name"),
                )
                self._flash_playback_denied()
            else:
                logger.info("Kaka '%s' hat noch keine abspielbaren Lieder.", kaka.get("name"))
            return

        contents = [
            KakaContent(
                content_id=c["id"],
                title=c.get("title", ""),
                file_hash=c.get("file_hash"),
                download_url=c.get("download_url"),
                cached_locally=bool(c.get("cached_locally")),
                sort_order=int(c.get("sort_order", 0)),
                category_id=c.get("category_id"),
            )
            for c in contents_data
        ]

        # Resume? Wenn der gleiche Chip kurz vor uns dieselbe Playlist gespielt hat.
        start_index, start_position = self._compute_resume(uid)
        # Nach dem Lesen ist der Memory verbraucht (egal ob er gepasst hat)
        self._last_kaka_memory = None

        kaka_id = kaka.get("id")
        on_start, on_end = self._playback_session_callbacks(
            source="kaka", kaka_id=kaka_id,
        )

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
                on_track_start=on_start,
                on_track_end=on_end,
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
        # M1: Beim Auflegen sofort einen Sync anstoßen — so werden neu
        # verknüpfte Lieder gleich geladen (statt erst beim nächsten 5-Min-
        # Zyklus), und _jump_to lädt notfalls beim Erreichen blockierend nach.
        if trigger_sync:
            self._trigger_background_sync("tag-placed")

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
                # Kategoriesperre (H5-Fix): gesperrte Lieder landen nicht im
                # Random-Pool — sonst würde eine Sperre nur beim direkten
                # Auflegen greifen, aber der Random-Modus sie unterlaufen.
                if self._is_category_blocked(c.get("category_id")):
                    continue
                seen.add(cid)
                out.append(KakaContent(
                    content_id=cid,
                    title=c.get("title", ""),
                    file_hash=c.get("file_hash"),
                    download_url=c.get("download_url"),
                    cached_locally=True,
                    sort_order=0,  # für Random egal — wird eh geshuffled
                    category_id=c.get("category_id"),
                ))
        return out

    def _start_random_mode(self) -> None:
        """Startet (oder restartet) den Random-Modus: alle lokalen Tracks
        in zufälliger Reihenfolge. Funktioniert ohne Chip.

        Bei Hold während Random bereits läuft: einfach neue zufällige
        Reihenfolge generieren und von vorne anfangen (User-Wunsch: "wie
        eine session, die neu losgeht").
        """
        # Ruhezeit aktiv? Stumm ablehnen (H5-Fix) — kein Chip beteiligt, daher
        # kein LED-Flash hier (der ist an den "Chip liegt auf"-Kontext geknüpft).
        if self._quiet_hours_now():
            logger.info("Ruhezeit aktiv — Random-Modus-Start unterdrückt (still).")
            return
        contents = self._all_cached_contents()
        if not contents:
            logger.warning("🎲 Random-Modus: keine gecachten Tracks gefunden.")
            return
        random.shuffle(contents)
        logger.info("🎲 Random-Modus startet mit %d Tracks", len(contents))

        on_start, on_end = self._playback_session_callbacks(
            source="manual", kaka_id=None,
        )

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
                on_track_start=on_start,
                on_track_end=on_end,
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

    def _resume_in_place(self, session: Optional[dict]) -> bool:
        """Setzt die exakt vorher laufende Playlist an derselben Stelle fort.

        Nach der Titel-Frage ("Wie heißt dieses Lied?"): dieselbe Track-Liste,
        derselbe Index, dieselbe Position — egal ob Kaka, Random oder Voice-
        Auswahl. So läuft DAS LIED weiter, statt neu zu mischen oder das nächste
        zu starten. ``session`` ist der Snapshot aus ``_run_voice_activation``.
        Gibt True zurück, wenn fortgesetzt wurde.
        """
        if not session or not session.get("contents"):
            return False
        source = session.get("source", "manual")
        used_zw = bool(self.config.get("zauberwort_mode_enabled")) if source == "voice" else False
        on_start, on_end = self._playback_session_callbacks(
            source=source, kaka_id=session.get("kaka_id"), used_zauberwort=used_zw,
        )
        with self._playlist_lock:
            if self._current_playlist:
                self._current_playlist.stop()
            playlist = Playlist(
                contents=list(session["contents"]),
                cache=self.audio_cache,
                download_fn=lambda cid, path: bool(self.backend) and self.backend.download_audio(cid, path),
                play_fn=self.player.play_file,
                stop_fn=self.player.stop,
                position_fn=self.player.current_position_seconds,
                seek_fn=self.player.seek_to,
                on_track_start=on_start,
                on_track_end=on_end,
            )
            self._current_playlist = playlist
            self._active_tag_uid = session.get("tag_uid")
            self._random_mode = bool(session.get("random_mode"))
            self._voice_mode = bool(session.get("voice_mode"))
            self._voice_pending_tag_uid = session.get("voice_pending")
            self._voice_last_target = session.get("voice_target")

        if not playlist.start(start_index=session.get("index", 0),
                              start_position=session.get("position", 0.0)):
            logger.warning("Resume nach Titel-Frage konnte nicht starten.")
            return False
        logger.info(
            "Titel-Frage: setze fort (Track %d ab %.1fs, Quelle=%s).",
            session.get("index", 0) + 1, session.get("position", 0.0), source,
        )
        if self.leds is not None:
            self.leds.strips_dance_start()
            self.leds.strips_show_position(playlist.current_index, playlist.length)
        return True

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
            # Reason "kaka_removed" landet in der Wiedergabe-Historie —
            # so erkennt man "Kind hat den Chip nur kurz aufgelegt" vs.
            # "Track ist normal durchgelaufen".
            playlist.stop(reason="kaka_removed")

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
        self._power_off("Grün 5s gehalten")

    def _power_off(self, reason: str) -> None:
        """Sauberer Shutdown: laufende Playlist hart räumen (sonst startet der
        EOF-Callback nach dem Bye-Prompt den nächsten Track), tschau-kakau
        abspielen, dann /usr/local/bin/kakabox-poweroff (sudoers NOPASSWD).
        Genutzt vom grünen 5s-Halten UND vom Auto-Shutdown nach Inaktivität.
        """
        logger.warning("Power-off (%s).", reason)
        with self._playlist_lock:
            playlist = self._current_playlist
            self._current_playlist = None
            self._active_tag_uid = None
        if playlist:
            playlist.stop()

        # Falls im Standby: LEDs sind aus — der Bye-Prompt soll trotzdem hörbar
        # sein (läuft über system_volume).
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
        """Encoder-Kurzklick → Speed-/Fast-Mode-Gestik (Easter-Egg).

        - Im Speed-Mode: ein Klick verlässt den Modus wieder.
        - Sonst: zählt zum Burst-Sliding-Window. 4× innerhalb SPEED_BURST_WINDOW
          während Wiedergabe → Speed-Mode an (Tempo dann per Drehen).
        - Kein Pause/Play (Gelb), kein Voice (Blau). STOP liegt auf dem
          ≥1s-Hold (_on_push_held) — Kurz-Klick und Hold schließen sich aus.
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
        """Encoder-Push ≥ 1s → STOP wenn etwas läuft; sonst Random starten.

        Stop gilt für JEDE Quelle (Kaka/RFID, Voice, Random): ``_full_stop``
        beendet hart und leert das Resume-Memory — danach Stille. Liegt eine Kaka
        noch auf, bleibt es still (die NFC-Schleife triggert nur bei Tag-WECHSEL);
        zum Weiterhören Kaka abnehmen + wieder auflegen → startet von vorne.

        Läuft gerade NICHTS, dient derselbe ≥1s-Hold zum Random-Start (zufällige
        Reihenfolge aus allen Tracks) — so bleibt das Starten ohne Chip erreichbar.
        Der kurze Klick ist davon getrennt (Speed-/Fast-Mode, _on_push_pressed).

        Im Hintergrund-Thread, weil _start_random_mode die Playlist-Init + erstes
        play_file machen kann und der gpiozero-Hold-Callback nicht blocken soll.
        """
        if self._abort_prompt_if_playing():
            return
        if self._speed_mode:
            self._exit_speed_mode()
        playing = (
            self.player.get_state().playing
            or self._active_tag_uid is not None
            or self._random_mode
            or self._voice_mode
        )
        if playing:
            logger.info("🟦 Encoder-Push ≥ 1s — Wiedergabe stoppen (Kaka/Voice/Random)")
            self._full_stop("Push-Hold Stop")
            return
        logger.info("🎲 Encoder-Push ≥ 1s — nichts läuft → Random-Modus starten")
        threading.Thread(
            target=self._start_random_mode,
            daemon=True,
            name="random-mode-start",
        ).start()

    def _on_yellow_down(self) -> None:
        """Sofort bei Gelb-Press (vor Hold/Release). Snapshot + Pause.

        Wir pausieren JEDEN Druck (auch Kurz-Druck) sofort — das gibt
        sofortiges Audio-Feedback. Der spätere Release-Handler entscheidet
        anhand des Pre-Press-Snapshots ob's ein Toggle (Kurz) oder Resume
        (Hold) war.
        """
        if self._abort_prompt_if_playing():
            return
        self._yellow_was_paused_before_press = self.player.get_state().paused
        if not self._yellow_was_paused_before_press:
            self.player.pause()
            # NFC-LED auf gelb wenn Tag oder Random aktiv (sofortige Optik).
            if self.leds is not None and (
                self._active_tag_uid is not None or self._random_mode
            ):
                self.leds.nfc_chip_paused()

    def _on_yellow_pressed(self) -> None:
        """Gelb — kurzer Druck (< YELLOW_HOLD_SECONDS) → klassischer Toggle.

        Bei kurzem Press hat ``_on_yellow_down`` schon pausiert. Jetzt
        invertieren wir den Pre-Press-State: war vorher Play → bleibt
        jetzt Pause; war vorher Pause → jetzt Play (resume).
        """
        logger.info("🟡 Gelb (Pause/Play)")
        if self._yellow_was_paused_before_press:
            self.player.resume()
        if self.leds is not None:
            if self.player.get_state().paused:
                if self._active_tag_uid is not None or self._random_mode:
                    self.leds.nfc_chip_paused()
            else:
                self._restore_idle_led()

    def _on_yellow_held(self) -> None:
        """Gelb-Hold (≥ YELLOW_HOLD_SECONDS) bei Release → LED-Streifen
        toggeln + Musik resume (ungeachtet Pre-Press-State).

        User-Wunsch: Hold = Strips-Toggle + Musik läuft beim Loslassen weiter.
        """
        logger.info("🟡⏵ Gelb 3s — LED-Streifen toggeln")
        # Music: immer resume (war während Hold pausiert wegen _on_yellow_down)
        if not self._yellow_was_paused_before_press:
            self.player.resume()
        # Strips-Toggle
        self._toggle_strips()
        # NFC-LED zurück zum Mode-Status
        if self.leds is not None:
            self._restore_idle_led()

    def _toggle_strips(self) -> None:
        """Schaltet die LED-Streifen-Animation an/aus mit Sweep-Animation."""
        self._strips_user_enabled = not self._strips_user_enabled
        logger.info("LED-Streifen %s", "AN" if self._strips_user_enabled else "AUS")
        if self.leds is None:
            return
        if self._strips_user_enabled:
            self.leds.strips_user_enable()
        else:
            self.leds.strips_user_disable()

    def _sync_led_acquire(self) -> None:
        """Orange Sync-Optik referenzgezählt einschalten (über alle Sync-Pfade).

        Nur der 0→1-Übergang startet die Optik tatsächlich — ein zweiter
        paralleler Pfad (z.B. Dongel-Feedback während eines Hintergrund-Downloads)
        erhöht nur den Zähler.
        """
        if self.leds is None:
            return
        with self._sync_led_lock:
            self._sync_led_owners += 1
            first = self._sync_led_owners == 1
        if first:
            self.leds.sync_start()

    def _sync_led_release(self) -> None:
        """Sync-Optik ausschalten, sobald der LETZTE Pfad fertig ist (1→0)."""
        if self.leds is None:
            return
        with self._sync_led_lock:
            if self._sync_led_owners > 0:
                self._sync_led_owners -= 1
            last = self._sync_led_owners == 0
        if last:
            self.leds.sync_stop()
            self._restore_idle_led()

    def _restore_idle_led(self) -> None:
        """Setzt die NFC-LED auf den passenden "läuft normal"-Status:
        gelb=pausiert (mit Tag/Random), grün=Tag aktiv, lila=Random, sonst aus.

        Wird nach Pause/Resume, Track-Skip und nach der Sync-Optik aufgerufen,
        damit die LED nicht im falschen Zustand hängenbleibt. Der Pause-Zweig
        spiegelt die Gelb-Knopf-Logik — sonst würde z.B. ein Hintergrund-Sync
        eine pausierte Box fälschlich auf grün ("läuft") setzen.
        """
        if self.leds is None:
            return
        if self.player.get_state().paused and (
            self._active_tag_uid is not None or self._random_mode
        ):
            self.leds.nfc_chip_paused()
        elif self._active_tag_uid is not None:
            self.leds.nfc_chip_present()
        elif self._random_mode:
            self.leds.nfc_random_active()
        else:
            self.leds.nfc_chip_absent()

    def _audio_level_loop(self) -> None:
        """Parallele ffmpeg-Decode pro Track + 20×/s FFT-Bänder an LED-Streifen.

        Lifecycle: bei Track-Wechsel wird ein neuer ``FileSpectrum`` auf den
        neuen Dateipfad gespawnt (ffmpeg streamt rohes PCM, die Pipe blockt
        selbst sobald wir vorlaufen). Bei jedem Poll lesen wir bis zur mpv-
        Wiedergabeposition voraus + berechnen 16 Frequenzbänder, die an
        ``leds.update_spectrum`` gehen.

        Bei paused/idle: keine neuen Bänder → LEDs zeigen schwarz nach 2 s.
        """
        current_spectrum: Optional[FileSpectrum] = None
        current_path: Optional[str] = None
        last_pos: float = 0.0
        try:
            while not self._spectrum_stop.is_set():
                # Streifen aus (Default!) → der teure Pfad (zweiter ffmpeg-Decode
                # derselben Datei + 20-Hz-FFT) bringt nichts: _render_dance
                # verwirft das Spektrum, solange _strips_user_enabled False ist.
                # Also Decoder abbauen und langsam leerlaufen, bis der User die
                # Streifen per Gelb-Hold einschaltet. Das ist die Hauptersparnis
                # beim reinen Musikhören — sonst decodiert die Box jeden Song
                # doppelt, nur um das Ergebnis wegzuwerfen.
                if not self._strips_user_enabled:
                    if current_spectrum is not None:
                        current_spectrum.close()
                        current_spectrum = None
                    current_path = None
                    # 5 Hz statt 20 Hz im Leerlauf — reicht, um das Einschalten
                    # der Streifen zügig zu bemerken, kostet aber fast nichts.
                    if self._spectrum_stop.wait(0.2):
                        return
                    continue

                try:
                    path = self.player.current_track_path()
                    pos = self.player.current_position_seconds()
                    paused = self.player.is_paused()
                except Exception:
                    path, pos, paused = None, 0.0, False

                # Track-Wechsel oder Stop → alten Decoder beenden
                if path != current_path:
                    if current_spectrum is not None:
                        current_spectrum.close()
                        current_spectrum = None
                    current_path = path
                    if path is not None:
                        current_spectrum = FileSpectrum(path, n_bands=16)
                        if not current_spectrum.start(start_seconds=pos):
                            current_spectrum = None
                        last_pos = pos

                # Rückwärtsseek > 1s → ffmpeg neustarten
                if current_spectrum is not None and pos + 1.0 < last_pos:
                    current_spectrum.stop()
                    current_spectrum = FileSpectrum(path, n_bands=16)
                    if not current_spectrum.start(start_seconds=pos):
                        current_spectrum = None
                last_pos = pos

                # Bänder lesen + an LEDs
                if current_spectrum is not None and not paused:
                    bands = current_spectrum.read_bands_at(pos)
                    if bands is None:
                        # EOF / Fehler → Decoder zu, beim nächsten Track neu
                        current_spectrum.close()
                        current_spectrum = None
                        current_path = None
                    elif self.leds is not None:
                        # Lautstärke koppelt die LED-Intensität: User-Volume
                        # relativ zum max_volume-Cap. Bei Eltern-Limit (z.B.
                        # 30) bedeutet "Volume=30" = volle LED-Reaktion, weil
                        # das ist was die Box maximal hergibt. Unter 10% wird
                        # komplett dunkel (Box flüstert nur → Lichter still).
                        #
                        # Sporadik-Effekt bei leiser Musik: nur die stärksten
                        # Bänder überleben den Threshold. Bei vol_ratio=0.10
                        # blitzen nur Peaks (≥0.55), bei vol_ratio≥0.70 läuft
                        # alles durch — disco statt firefly.
                        vol_ratio = self._volume / max(1, self._max_volume)
                        if vol_ratio < 0.10:
                            bands = [0.0] * len(bands)
                        else:
                            peak_threshold = max(0.0, 0.65 * (0.70 - vol_ratio))
                            if peak_threshold > 0:
                                bands = [b if b >= peak_threshold else 0.0 for b in bands]
                            if vol_ratio < 1.0:
                                bands = [b * vol_ratio for b in bands]
                        try:
                            self.leds.update_spectrum(bands)
                        except Exception:
                            pass

                if self._spectrum_stop.wait(0.05):
                    return
        finally:
            if current_spectrum is not None:
                current_spectrum.close()

    def _warmup_voice(self) -> None:
        """Lädt ASR- UND TTS-Modell beim Service-Start in den RAM.

        Läuft in einem Daemon-Thread, damit der Boot nicht blockiert und der
        Rest der Box (NFC-Loop, Buttons, Heartbeat) sofort verfügbar ist.
        Spart ~1–3 s beim ersten Push-to-Talk (ASR) und ~3 s beim ersten
        "Wie heißt dieses Lied?" (Piper-Modell-Load). Bei Paket/Modell-Fehlern
        wird nur geloggt — die Lazy-Load-Pfade greifen dann beim echten Trigger.
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

        # Vosk-Keyword-Modell fürs Zauberwort vorab laden — sonst zahlt die erste
        # "bitte"-Rückfrage den einmaligen ~1-3s Modell-Load. Best-effort.
        if self._magic_word_recognizer is not None:
            try:
                self._magic_word_recognizer.warmup()
                logger.info("Zauberwort-ASR (vosk) vorgeladen.")
            except VoiceUnavailable as e:
                logger.info("Zauberwort-ASR-Warmup übersprungen: %s", e)
            except Exception:
                logger.exception("Zauberwort-ASR-Warmup fehlgeschlagen")

        # TTS separat — eigener try, damit ein TTS-Problem das ASR-Warmup-Log
        # nicht verschluckt. TitleSpeaker.warmup schluckt seine Fehler selbst.
        t1 = time.monotonic()
        self._speaker.warmup()
        logger.info("TTS-Warmup fertig (%.1fs)", time.monotonic() - t1)

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
        # Ruhezeiten-Gate VOR der Aufnahme (ASR-Plan 1.11): Während der
        # Ruhezeit wäre jede Wiedergabe ohnehin unterdrückt (H5) — statt das
        # Kind erst aufzunehmen, zu transkribieren und dann abzulehnen, wird
        # der Flow hier still abgekürzt (roter LED-Blitz, kein Ton). Auch die
        # Titel-Frage ist gegenstandslos: während der Ruhezeit spielt nichts.
        # Datensparsam obendrein — es entsteht gar keine Aufnahme.
        if self._quiet_hours_now():
            logger.info("Ruhezeit aktiv — Voice-Aufnahme unterdrückt (still).")
            threading.Thread(
                target=self._flash_playback_denied,
                kwargs={"chip_present": self._active_tag_uid is not None},
                daemon=True, name="voice-quiet-denied",
            ).start()
            return
        if not self._voice_lock.acquire(blocking=False):
            logger.info("Voice bereits aktiv — Trigger ignoriert.")
            return
        threading.Thread(
            target=self._voice_activation_guarded,
            daemon=True,
            name="voice-ptt",
        ).start()

    def _voice_activation_guarded(self) -> None:
        """Wrapper um _run_voice_activation, der den _voice_lock GARANTIERT
        freigibt — auch bei einer Exception im Snapshot-/LED-Code, der vor dem
        inneren try/finally von _run_voice_activation läuft. Ohne diesen Wrapper
        würde ein solcher Fehler den Lock dauerhaft halten und Voice bis zum
        nächsten Reboot lahmlegen.
        """
        try:
            self._run_voice_activation()
        except Exception:
            logger.exception("Voice-Aktivierung mit unerwartetem Fehler abgebrochen.")
        finally:
            self._voice_lock.release()

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
        # War vorher eine per-Sprache gewählte Auswahl aktiv (Track/Artist/Genre)?
        # Die setzt KEIN _active_tag_uid — ohne eigenes Merken bliebe die Box nach
        # einer Titel-Frage stumm (kein Tag, kein Random → nichts zu resumen).
        saved_voice_target = self._voice_last_target if self._voice_mode else None
        saved_voice_pending = self._voice_pending_tag_uid
        saved_track_index = 0
        saved_position = 0.0
        # Titel des laufenden Tracks JETZT merken — _stop_for_voice() (gleich)
        # löscht Playlist + Player-State, danach ist er für die "Wie heißt
        # dieses Lied?"-Antwort nicht mehr abrufbar.
        saved_title: Optional[str] = None
        # Vollständiger Wiedergabe-Snapshot, um nach der Titel-Frage EXAKT dasselbe
        # Lied an derselben Position fortzusetzen (statt Reshuffle/Neustart).
        saved_session: Optional[dict] = None
        with self._playlist_lock:
            if self._current_playlist:
                saved_track_index = self._current_playlist.current_index
                saved_title = self._current_playlist.current_title()
                try:
                    saved_position = self.player.current_position_seconds()
                except Exception:
                    saved_position = 0.0
                # Quelle/kaka_id für die Session-Callbacks aus dem aktiven Modus.
                if self._active_tag_uid:
                    _src = "kaka"
                    _kid = ((self._tag_cache.get(self._active_tag_uid) or {}).get("kaka") or {}).get("id")
                elif self._voice_mode:
                    _src, _kid = "voice", None
                else:
                    _src, _kid = "manual", None
                saved_session = {
                    "contents": self._current_playlist.contents_snapshot(),
                    "index": saved_track_index,
                    "position": saved_position,
                    "source": _src,
                    "kaka_id": _kid,
                    "tag_uid": self._active_tag_uid,
                    "random_mode": self._random_mode,
                    "voice_mode": self._voice_mode,
                    "voice_pending": self._voice_pending_tag_uid,
                    "voice_target": self._voice_last_target,
                }
        recovered = False  # True sobald entweder ein neuer Track läuft oder wir resumed haben

        def _restore_previous(reason: str) -> None:
            """Helper: vorheriges Playback fortsetzen.
            - Kakafigur drauf → resume mit gemerkter Position
            - Per-Sprache gewählte Auswahl → neu starten (Track/Artist/Genre)
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
            elif saved_voice_target is not None:
                # Voice-Auswahl neu anstoßen (kein Positions-Resume — bei Genre
                # neu gemischt). Pending-Tag erhalten, damit ein späteres Track-
                # Ende wie zuvor zur Kakafigur bzw. Random zurückführt.
                logger.info(
                    "Voice abgebrochen (%s) → Voice-Auswahl '%s' wieder an",
                    reason, saved_voice_target.name,
                )
                self._voice_pending_tag_uid = saved_voice_pending
                self._play_voice_target(saved_voice_target)
            elif saved_random_mode:
                logger.info("Voice abgebrochen (%s) → Random-Modus wieder an", reason)
                self._start_random_mode()

        # Während Voice-Eingabe → NFC-LED blau pulsieren (egal ob Tag drauf
        # war oder nicht — zeigt visuell "ich höre dir gerade zu").
        if self.leds is not None:
            self.leds.nfc_voice_active()

        # Governor-Boost für die latenzkritische Voice-Phase (ASR-Plan 1.2,
        # Variante A): best-effort — der sudo-Helper akzeptiert "performance"
        # erst nach dem Setup-Update (setup/kakabox-cpu-governor); bis dahin
        # lehnt er das Argument ab und es bleibt beim ondemand des Wake-Pfads.
        governor_boost = bool((self.config.get("voice") or {}).get("governor_boost", True))
        if governor_boost:
            self._set_cpu_governor("performance")
        try:
            # Vor dem Prompt sauber stoppen — wir können nicht pausieren weil
            # der Prompt selbst mpv.stop()+play() macht (defensive in play_file),
            # was den pausierten Stream eh zerstören würde.
            self._stop_for_voice()
            self._play_prompt("listening.wav")
            # Padamm zu Ende abspielen, sonst mischt's sich in die Aufnahme.
            # Das Prompt-Echo im Raum klingt danach noch kurz nach — die
            # Settle-Phase des Recorders (erste ~0,3s ohne Speech-Detection)
            # fängt das mit ab (ASR-Plan 1.10).
            self.player.wait_until_idle(timeout=2.0)

            try:
                rec = self._mic_recorder.record_until_silence(
                    max_seconds=VOICE_MAX_SECONDS,
                    silence_seconds=VOICE_SILENCE_SECONDS,
                    initial_silence_seconds=VOICE_INITIAL_SILENCE_SECONDS,
                )
            except RecorderError as e:
                logger.warning("Voice-Aufnahme fehlgeschlagen: %s", e)
                return

            # Halluzinations-Gate Teil 1 (ASR-Plan 1.5a): Ohne erkannte
            # Sprache gar nicht erst transkribieren — Whisper macht aus
            # Stille/Rauschen sonst plausible Sätze, die falsche Lieder
            # starten. Dieses Gate sitzt bewusst VOR der ASR, damit es in
            # einer späteren Hybrid-Stufe auch den Server-Upload verhindert.
            if not rec.speech_seen:
                logger.info("Voice: VAD sah keine Sprache — überspringe ASR (Error-Ton).")
                self._save_voice_sample(rec, transcript=None, route=None)
                return

            try:
                # initial_prompt/Grammar bewusst NICHT gesetzt: Für Deutsch
                # zeigt die Literatur ineffektives bis schädliches Prompt-
                # Biasing bei Whisper — erst nach positivem A/B gegen das
                # Stufe-0-Testset aktivieren (ASR-Plan 1.9). Der freie
                # Decoder transkribiert phonetisch, der Fuzzy-/Phonetik-
                # Match (intent.py) findet den Song.
                text = self._recognizer.transcribe_wav(rec.path)
            except VoiceUnavailable as e:
                logger.warning("ASR nicht verfügbar: %s", e)
                return

            logger.info("Voice transkribiert: «%s»", text)

            # Routing-Entscheidung als pure Funktion (voice/router.py) —
            # geteilt mit dem Eval-Harness (tools/eval_voice.py), damit der
            # Harness exakt das misst, was die Box tut (ASR-Plan Stufe 0).
            catalog = build_catalog_from_file(VOICE_CATALOG_PATH)
            route = route_transcript(
                text, catalog,
                zauberwort_enabled=bool(self.config.get("zauberwort_mode_enabled")),
                bare_title_threshold=VOICE_BARE_TITLE_THRESHOLD,
            )
            self._save_voice_sample(rec, transcript=text, route=route)

            if route.action == "hallucination":
                logger.info("Voice: Transkript leer/bekannte Halluzination — Error-Ton.")
                return

            # Titel-Frage ("Wie heißt dieses Lied?"). Kein Zauberwort-Gate
            # (eine Frage ist kein Play). Antwort = der vorm _stop_for_voice
            # gemerkte Titel; danach läuft die Musik weiter.
            if route.action == "title_question":
                logger.info("Voice: Titel-Frage erkannt → Titel: %s", saved_title)
                if saved_title:
                    self._speak_current_title(saved_title)
                else:
                    self._play_prompt("voice_no_title.wav")
                    self.player.wait_until_idle(timeout=4.0)
                # EXAKT dasselbe Lied an derselben Position fortsetzen (User-
                # Wunsch: weiterspielen, nicht das nächste / nicht neu mischen).
                # Fällt das aus (kein Snapshot), regulärer Restore als Fallback.
                if not self._resume_in_place(saved_session):
                    _restore_previous("titel-frage beantwortet")
                recovered = True
                return

            # Zauberwort-Gate für Random UND Play: Aktion erkannt, aber
            # "bitte" fehlt → "Wie heißt das Zauberwort?" und ein zweites Mal
            # lauschen; ohne "bitte" kein Playback (finally → Error-Ton).
            # Läuft bewusst NACH dem Matching (Z2): der Prompt kommt nur,
            # wenn wirklich etwas gefunden wurde.
            if route.action in ("random", "play") and route.needs_magic_word:
                logger.info("Zauberwort fehlt in «%s» — frage nach.", text)
                if not self._await_zauberwort():
                    logger.info("Zauberwort nicht gesagt — kein Playback.")
                    return

            # Random-Wunsch ("spiele irgendwas") — gleiche Erfolgs-Choreo wie
            # ein Treffer (Flash-Grün + success-Sound).
            if route.action == "random":
                logger.info("Voice: Random-Wunsch erkannt → Random-Modus.")
                if self.leds is not None:
                    self.leds.nfc_flash_success()
                self._play_prompt("voice_success.wav")
                self.player.wait_until_idle(timeout=2.0)
                self._start_random_mode()
                # Nur als "recovered" werten, wenn Random wirklich lief (sonst
                # gibt's keine gecachten Tracks → finally spielt Error + Restore).
                if self._random_mode:
                    recovered = True
                return

            if route.action != "play" or route.command is None:
                logger.info("Voice: kein Match für «%s» → Error-Ton.", text)
                return

            cmd = route.command
            logger.info(
                "Voice match: kind=%s name='%s' score=%.2f margin=%.2f query='%s'",
                cmd.target.kind, cmd.target.name, cmd.score, cmd.margin, cmd.query,
            )

            # Match (+ ggf. Zauberwort bestätigt) → Erfolgs-Feedback: NFC-LED static sattes Grün während
            # der success-Sound spielt. Danach Voice-Track. Der finally-Block
            # setzt die LED dann zurück auf den richtigen Pulse-Status (grün
            # falls Tag noch drauf via _voice_pending_tag_uid).
            self._voice_pending_tag_uid = saved_tag_uid
            self._voice_last_target = cmd.target
            if self.leds is not None:
                self.leds.nfc_flash_success()
            # Gesprochenes Feedback "Ich spiele <Name>" in der TTS-Stimme —
            # zeitgleich mit dem Grün-Flash (die Lampe steht statisch, während
            # die Ansage läuft). Kommt erst hier, also nach erkanntem Befehl UND
            # bestandenem Zauberwort-Gate. Fällt die TTS ganz aus, kommt der
            # feste Erfolgston, damit immer eine hörbare Bestätigung bleibt.
            if not self._speak_now_playing(cmd.target.name):
                self._play_prompt("voice_success.wav")
                self.player.wait_until_idle(timeout=2.0)
            self._play_voice_target(cmd.target)
            recovered = True  # neue Wiedergabe läuft, kein Restore nötig
        finally:
            # Wenn vorher eine Kakafigur lief UND nichts Neues gestartet wurde:
            # Static rot + Error-Sound, dann Kakafigur/Random restoren.
            if not recovered:
                try:
                    if self.leds is not None:
                        self.leds.nfc_flash_error()
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
            # Governor-Boost zurücknehmen (Gegenstück zum Start des Flows).
            # Immer ondemand — das ist der reguläre Wach-Zustand; der Standby-
            # Pfad übernimmt powersave weiterhin selbst.
            if governor_boost:
                self._set_cpu_governor("ondemand")
            # Lock-Freigabe NICHT hier — sie passiert garantiert im
            # _voice_activation_guarded-Wrapper, auch wenn oben (Snapshot, LED)
            # VOR diesem try eine Exception fliegt (sonst leakt der Lock und
            # Voice wäre bis zum Reboot tot).

    def _save_voice_sample(self, rec, transcript, route) -> None:
        """Stufe-0-Testset-Aufbau (ASR-Plan): Voice-Aufnahme + Metadaten behalten.

        Nur aktiv mit ``voice.keep_samples: true`` in config.json — Default AUS
        (Kinder-Stimmdaten!). Samples bleiben lokal unter device/voice_samples/
        (per .gitignore ausgeschlossen, landet nie in Git oder Backups) und
        werden nach ``voice.samples_keep_days`` (Default 14) automatisch
        gelöscht. Pro Sample eine Sidecar-JSON mit Live-Transkript und
        Routing-Ergebnis — beim Labeln wird dann nur korrigiert statt neu
        getippt. Best-effort: Fehler hier dürfen den Voice-Flow nie brechen.
        """
        cfg = self.config.get("voice") or {}
        if not cfg.get("keep_samples", False):
            return
        try:
            samples_dir = Path(__file__).parent / "voice_samples"
            samples_dir.mkdir(exist_ok=True)
            # Retention: Alt-Samples beim nächsten Schreiben wegräumen.
            keep_days = float(cfg.get("samples_keep_days", 14))
            cutoff = time.time() - keep_days * 86400
            for old in samples_dir.iterdir():
                try:
                    if old.is_file() and old.stat().st_mtime < cutoff:
                        old.unlink(missing_ok=True)
                except OSError:
                    pass
            ts = time.strftime("%Y%m%d-%H%M%S")
            stem = samples_dir / f"sample-{ts}"
            shutil.copyfile(rec.path, stem.with_suffix(".wav"))
            cmd = route.command if route is not None else None
            meta = {
                "recorded_at": ts,
                "duration_seconds": round(rec.duration_seconds, 2),
                "speech_seen": rec.speech_seen,
                "transcript": transcript,
                "action": route.action if route is not None else "no_speech",
                "matched": {
                    "name": cmd.target.name,
                    "kind": cmd.target.kind,
                    "id": cmd.target.id,
                    "score": round(cmd.score, 3),
                    "margin": round(cmd.margin, 3),
                } if cmd is not None else None,
                # Zum Labeln ergänzen: ground_truth_text, expected_intent,
                # expected_song_id, alter, distanz (siehe tools/eval_voice.py).
            }
            stem.with_suffix(".json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Voice-Sample nicht gespeichert: %s", e)

    def _await_zauberwort(self) -> bool:
        """Spielt "Wie heißt das Zauberwort?" und lauscht ein zweites Mal.

        Gibt True zurück, wenn das Kind "bitte" sagt (positionsunabhängig, via
        has_magic_word). Follow-up-Timing (Z4): max 7s lauschen bevor bei
        Stille abgebrochen wird, kurze Nachlauf-Stille (s. Konstante) → nach
        "bitte" startet es zügig. Die NFC-LED bleibt blau ("lauscht"); das
        grüne Erfolgs-Feedback setzt der aufrufende Erfolgs-Pfad.
        """
        if self.leds is not None:
            self.leds.nfc_voice_active()
        self._play_prompt("zauberwort.wav")
        # Prompt fertig abspielen, sonst mischt er sich in die Aufnahme.
        self.player.wait_until_idle(timeout=2.0)
        try:
            rec = self._mic_recorder.record_until_silence(
                max_seconds=VOICE_MAX_SECONDS,
                silence_seconds=VOICE_ZAUBERWORT_SILENCE_SECONDS,
                initial_silence_seconds=VOICE_INITIAL_SILENCE_SECONDS,
            )
        except RecorderError as e:
            logger.warning("Zauberwort-Aufnahme fehlgeschlagen: %s", e)
            return False
        # Kein Speech laut VAD → "bitte" kann nicht drin sein; spart den
        # ASR-Lauf und verhindert Halluzinations-Treffer auf Stille.
        if not rec.speech_seen:
            logger.info("Zauberwort-Aufnahme ohne Sprache (VAD) — werte als Nein.")
            return False
        return self._detect_magic_word(rec.path)

    def _detect_magic_word(self, wav) -> bool:
        """Prüft schnell, ob "bitte" in der Aufnahme steckt.

        Nutzt den leichten Vosk-Keyword-Erkenner mit Grammar nur ``["bitte"]``
        (~1s) statt der vollen Whisper-Transkription (~3.3s, fixes 30s-Fenster).
        Vosk-small erkennt das eine, häufige Wort sehr zuverlässig. Fällt Vosk
        aus (Paket/Modell fehlt), Fallback auf den Haupt-Recognizer (Whisper),
        damit das Zauberwort-Gate auch ohne Vosk funktioniert.
        """
        if self._magic_word_recognizer is not None:
            try:
                text = self._magic_word_recognizer.transcribe_wav(wav, grammar=["bitte"])
                logger.info("Zauberwort-Check (vosk): «%s»", text)
                return has_magic_word(text)
            except VoiceUnavailable as e:
                logger.info("Vosk-Zauberwort nicht verfügbar (%s) → Whisper-Fallback.", e)
            except Exception:
                logger.exception("Vosk-Zauberwort-Check fehlgeschlagen → Whisper-Fallback.")
        try:
            text = self._recognizer.transcribe_wav(wav)
            logger.info("Zauberwort-Antwort (whisper): «%s»", text)
            return has_magic_word(text)
        except VoiceUnavailable as e:
            logger.warning("ASR (Zauberwort) nicht verfügbar: %s", e)
            return False

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

    def _speak_current_title(self, title: str) -> None:
        """Sagt den Titel in der konfigurierten Stimme an: Trägerphrase
        "Dieses Lied heißt" + Titel — BEIDES per TTS, damit männlich/weiblich
        durchgängig passt. Die TTS-WAVs liegen außerhalb von PROMPTS_DIR (Cache),
        laufen also DIREKT über den Player (``_play_prompt`` sucht nur unter
        PROMPTS_DIR). Schlägt die Titel-Synthese fehl, kommt der feste "weiß ich
        gerade nicht"-Prompt statt Stille.
        """
        if self.leds is not None:
            self.leds.nfc_flash_success()
        intro = self._speaker.synth_to_wav(TTS_TITLE_INTRO)  # pro Stimme gecacht
        title_wav = self._speaker.synth_to_wav(title)
        if title_wav is None:
            logger.warning("TTS lieferte keine WAV für «%s».", title)
            self._play_prompt("voice_no_title.wav")
            self.player.wait_until_idle(timeout=4.0)
            return
        try:
            if intro is not None:
                self.player.play_prompt(str(intro), self._volume)
                self.player.wait_until_idle(timeout=4.0)
            self.player.play_prompt(str(title_wav), self._volume)
            self.player.wait_until_idle(timeout=8.0)
        except Exception as e:
            logger.warning("Titel-Ansage abspielen fehlgeschlagen: %s", e)

    def _speak_now_playing(self, name: str) -> bool:
        """Sagt "Ich spiele <name>" in der konfigurierten Stimme an — Trägerphrase
        + Name, beides per TTS (wie _speak_current_title), damit männlich/weiblich
        durchgängig passt. Der Grün-Flash setzt der Aufrufer VORHER, sodass Lampe
        und Ansage zusammen kommen.

        Gibt True zurück, wenn die Ansage lief; False, wenn die TTS keine WAV
        lieferte (Piper+espeak beide weg) — dann spielt der Aufrufer den festen
        ``voice_success.wav``-Erfolgston, damit IMMER ein Feedback kommt.
        """
        name = (name or "").strip()
        if not name:
            return False
        intro = self._speaker.synth_to_wav(TTS_NOW_PLAYING_INTRO)  # pro Stimme gecacht
        name_wav = self._speaker.synth_to_wav(name)  # Name-Cache geteilt mit Titel-Ansage
        if name_wav is None:
            logger.info("TTS lieferte keine WAV für «%s» → Erfolgston-Fallback.", name)
            return False
        try:
            if intro is not None:
                self.player.play_prompt(str(intro), self._volume)
                self.player.wait_until_idle(timeout=4.0)
            self.player.play_prompt(str(name_wav), self._volume)
            self.player.wait_until_idle(timeout=8.0)
            return True
        except Exception as e:
            logger.warning("Now-Playing-Ansage abspielen fehlgeschlagen: %s", e)
            return False

    def _play_voice_target(self, target: Candidate) -> None:
        """Spielt den per Voice gewählten Target ab.

        ``kind="track"`` → einzelne Datei aus dem Cache; ``kind="artist"`` →
        alle Tracks des Künstlers als Playlist; ``kind="genre"`` → alle Tracks
        des Genres in zufälliger Reihenfolge (wie eine frische Genre-Session).
        Nicht-gecachte Tracks werden übersprungen (kein Online-Download während
        des Voice-Flows).
        """
        if not target.content_ids:
            logger.warning("Voice-Target ohne content_ids: %s", target.name)
            return

        # Ruhezeit aktiv? Stumm ablehnen (H5-Fix). Kategoriesperre ist hier
        # NICHT möglich — voice_catalog.json trägt aktuell keine category_id
        # (bekannte Lücke, siehe QS-Audit 2026-07-07 Folgearbeiten).
        if self._quiet_hours_now():
            logger.info("Ruhezeit aktiv — Voice-Wiedergabe von '%s' unterdrückt (still).", target.name)
            return

        # Genre fühlt sich wie Random innerhalb der Kategorie an → mischen. Track
        # und Artist behalten ihre Catalog-Reihenfolge.
        content_ids = list(target.content_ids)
        if target.kind == "genre":
            random.shuffle(content_ids)

        # Echten Einzeltitel pro content_id nachschlagen: Bei Artist/Genre ist
        # target.name nur der Künstler-/Kategoriename — für korrekte Playback-
        # Logs UND die spätere Titel-Ansage ("Wie heißt dieses Lied?") brauchen
        # die Tracks ihren echten Titel. Fallback target.name nur bei Map-Miss.
        title_map = build_title_map_from_file(VOICE_CATALOG_PATH)

        contents: list[KakaContent] = []
        for cid in content_ids:
            path = self.audio_cache.path_for(cid)
            if not path.is_file():
                logger.warning("Voice: Track %d nicht im Cache (%s)", cid, path)
                continue
            contents.append(KakaContent(
                content_id=cid,
                title=title_map.get(cid, target.name),
                file_hash=None,
                download_url=None,
                cached_locally=True,
                sort_order=0,
            ))
        if not contents:
            logger.warning("Voice: keine spielbaren Tracks für '%s'", target.name)
            return

        used_zauberwort = bool(self.config.get("zauberwort_mode_enabled"))
        on_start, on_end = self._playback_session_callbacks(
            source="voice",
            kaka_id=None,
            used_zauberwort=used_zauberwort,
        )

        with self._playlist_lock:
            playlist = Playlist(
                contents=contents,
                cache=self.audio_cache,
                download_fn=lambda cid, p: bool(self.backend) and self.backend.download_audio(cid, p),
                play_fn=self.player.play_file,
                stop_fn=self.player.stop,
                position_fn=self.player.current_position_seconds,
                seek_fn=self.player.seek_to,
                on_track_start=on_start,
                on_track_end=on_end,
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

    def _set_volume(self, volume: int) -> None:
        """Setzt die Lautstärke ABSOLUT (0..100), gekappt durch _max_volume.

        Genutzt von der lokalen REST-API (/volume, api/routes.py) und vom
        set_volume-Command (#18). Delegiert an _adjust_volume, damit Player,
        LED-Ring und config konsistent bleiben. (Vorher rief die API ein
        nicht existierendes _set_volume → AttributeError.)
        """
        try:
            target = int(volume)
        except (TypeError, ValueError):
            logger.warning("_set_volume: ungültiger Wert %r", volume)
            return
        target = max(0, min(self._max_volume, target))
        self._adjust_volume(target - self._volume)

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
        try:
            self.player.stop()
        except Exception as e:
            # Ein mpv-IPC-Fehler hier (z.B. SIGTERM/poweroff-Race) darf nicht
            # die restlichen Shutdown-Schritte (Peripherie-Cleanup, Config-
            # Save) verhindern — siehe die anderen try/except unten.
            logger.warning("player.stop() beim Shutdown fehlgeschlagen: %s", e)
        # Reporter-Worker bekommt noch eine kurze Chance, die Queue zu
        # flushen, bevor das Process-Exit den Daemon-Thread killt. Disk-
        # Persistenz ist die Backup-Garantie, aber wir sparen damit eine
        # Iteration nach dem nächsten Boot.
        try:
            self.play_session_reporter.stop_worker(timeout=2.0)
        except Exception as e:
            logger.warning("PlaySessionReporter Stop fehlgeschlagen: %s", e)
        if self.nfc is not None:
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
