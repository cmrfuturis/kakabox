"""WS2812 LED-Steuerung der Kakabox.

Hardware-Setup (Stand: Mai 2026):
  - 25 WS2812 als Daisy-Chain an GPIO 18 (Pin 12)
  - 5V extern versorgt, GND gemeinsam mit Pi
  - Reihenfolge (logisch): #0-7 LED-Ring, #8 NFC-Status,
    #9-16 Streifen Links, #17-24 Streifen Rechts

⚠ Hardware-Konflikt: GPIO 18 ist auch I²S0_BCLK für den MAX98357A-Speaker.
Solange LED-Steuerung läuft, ist der Speaker stumm — geplanter Fix: DIN auf
GPIO 10 (SPI MOSI) umlöten, dann auf Pi5Neo umstellen. Bis dahin gegenseitig
ausschließend.

Helligkeit ist fest auf MAX 25% begrenzt — kinderverträglich, weniger
Stromhunger (25 LEDs voll weiß bei 100% wären ~1.5A; bei 25% ~400mA).

Die Library ``adafruit-circuitpython-neopixel`` + ``Adafruit-Blinka-
Raspberry-Pi5-Neopixel`` nutzt PIO im RP1-Chip — funktioniert auf Pi 5,
braucht je nach Setup root (über das Service ist es root, manuell ggf. sudo).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Tuple

logger = logging.getLogger(__name__)

# Hardware-Konstanten
LED_COUNT = 25
MAX_BRIGHTNESS = 0.25  # global cap — Kinderschutz + Stromsparen

# Logische Zonen — slices über den Strip
ZONE_RING = slice(0, 8)         # 8 LEDs um den Encoder
ZONE_NFC = slice(8, 9)          # 1 LED für NFC-Status
ZONE_STRIP_LEFT = slice(9, 17)  # 8 LEDs linker Streifen
ZONE_STRIP_RIGHT = slice(17, 25)  # 8 LEDs rechter Streifen

RING_SIZE = ZONE_RING.stop - ZONE_RING.start

Color = Tuple[int, int, int]
BLACK: Color = (0, 0, 0)

# Volume-Visualisierung — 8 Stufen "kalt → warm" über den Encoder-Ring.
# Index 0 = leiseste sichtbare Stufe (1/8), Index 7 = volle Lautstärke.
# Bei Stufe N leuchten N LEDs in der Farbe VOLUME_COLORS[N-1].
VOLUME_COLORS: list[Color] = [
    (0,   0,   255),   # 1/8 — Blau (leise)
    (0,   150, 255),   # 2/8 — Cyan
    (0,   255, 200),   # 3/8 — Türkis
    (0,   255, 0),     # 4/8 — Grün
    (200, 255, 0),     # 5/8 — Gelb-Grün
    (255, 200, 0),     # 6/8 — Gelb-Orange
    (255, 100, 0),     # 7/8 — Orange
    (255, 0,   0),     # 8/8 — Rot (laut)
]

# Sekunden nach letzter Drehung, bis der Ring wieder ausgeht.
VOLUME_AUTO_OFF_S = 5.0

# Speed-Mode-Visualisierung — durchgängig lila, sanft pulsierend.
# Soll sich klar vom Volume-Modus (Regenbogen) unterscheiden.
SPEED_COLOR_BASE: Tuple[int, int, int] = (180, 0, 200)  # Lila bei Vollpegel
SPEED_PULSE_HZ = 1.0                # eine Schwingung pro Sekunde
SPEED_PULSE_FACTOR_MIN = 0.40       # ergibt 25% * 0.40 = 10% effektive Helligkeit
SPEED_PULSE_FACTOR_MAX = 1.00       # ergibt 25% * 1.00 = 25% effektive Helligkeit
SPEED_PULSE_FPS = 20                # 50 ms zwischen Updates — flüssig genug

# Player-Speed-Range muss zur main.py SPEED_MIN/SPEED_MAX passen — der LED-
# Wrapper kennt die Defaults selbst, sodass speed_to_led_count() ohne Import
# aus main testbar bleibt.
DEFAULT_SPEED_MIN = 0.5
DEFAULT_SPEED_MAX = 2.0

# NFC-Status — grün, wenn ein Chip aufgelegt ist. Pulsiert sanft sekündlich
# zwischen 5% (kaum wahrnehmbar) und 15% (gedämpft, nicht stechend).
# Beide Werte absolut (0..1), unabhängig vom globalen MAX_BRIGHTNESS-Cap.
NFC_PRESENT_COLOR_BASE: Tuple[int, int, int] = (0, 255, 0)
NFC_PRESENT_PULSE_MIN_INTENSITY = 0.05
NFC_PRESENT_PULSE_MAX_INTENSITY = 0.15
NFC_PULSE_HZ = 1.0    # eine Schwingung pro Sekunde
NFC_PULSE_FPS = 20    # 50 ms zwischen Updates — flüssig

# Streifen (links + rechts, je 8 LEDs = 16 zusammenhängend) —
# Wiedergabe-Visualisierung. Zwei Modi:
#   - "dance": tanzt im Rhythmus (echtes Audio-Spectrum sobald verfügbar,
#     sonst zeitbasierter Pseudo-Effekt als Fallback)
#   - "position": zeigt für POSITION_DISPLAY_S Sekunden, wie viele Tracks
#     schon gespielt sind (z.B. 3/5 → 10 von 16 LEDs leuchten)
# Wechselt automatisch nach Ablauf zurück zu "dance".
STRIPS_TOTAL = (ZONE_STRIP_RIGHT.stop - ZONE_STRIP_LEFT.start)  # = 16
DANCE_FPS = 25
PSEUDO_DANCE_BPM = 120  # virtueller Beat für die Fallback-Animation
POSITION_DISPLAY_S = 5.0
POSITION_COLOR_BASE: Tuple[int, int, int] = (100, 200, 255)  # hellblau
POSITION_INTENSITY = 0.15  # absolut — gedämpft, nicht stechend


def volume_to_led_count(percent: float) -> int:
    """0..100% → 0..8 LEDs. Schwellen sind 12.5%-Schritte (100/8).

    Pure function für Tests. Schon 1% zeigt 1 LED (math.ceil), damit der User
    sofort visuelles Feedback bekommt sobald er den Encoder berührt — sonst
    wäre der erste Klick still und das Auto-Off würde nie greifen.
    """
    if percent <= 0:
        return 0
    return min(RING_SIZE, math.ceil(percent / (100 / RING_SIZE)))


def scale_to_intensity(
    base_color: Tuple[int, int, int],
    intensity: float,
    max_brightness: float = MAX_BRIGHTNESS,
) -> Tuple[int, int, int]:
    """Skaliert ``base_color`` so, dass die effektive Helligkeit ``intensity``
    (0..1, absolut) entspricht — gegeben dass NeoPixel global mit
    ``max_brightness`` arbeitet.

    Warum nötig: ``brightness`` an der NeoPixel-Strip wirkt auf ALLE 25 LEDs
    gemeinsam. Wenn wir die NFC-LED auf 15% dimmen wollten via brightness,
    würden Ring und Streifen mitdunkeln. Stattdessen lassen wir brightness
    konstant bei MAX_BRIGHTNESS und skalieren die einzelnen Farbwerte.

    Beispiel: bei MAX_BRIGHTNESS=0.25 und gewünschter intensity=0.15 ist
    ``factor = 0.15 / 0.25 = 0.60`` → Grün (0,255,0) wird zu (0,153,0).
    """
    if max_brightness <= 0:
        return BLACK
    factor = max(0.0, min(1.0, intensity / max_brightness))
    return tuple(int(c * factor) for c in base_color)  # type: ignore[return-value]


def position_to_led_count(
    track_idx: int,
    total: int,
    strip_size: int = STRIPS_TOTAL,
) -> int:
    """Track-Position (0-basiert) + Gesamtanzahl → Anzahl leuchtender LEDs.

    Beispiele (strip_size=16):
      total=5, track_idx=0 → round(1/5 * 16) = 3
      total=5, track_idx=4 → 16 (alle)
      total=3, track_idx=0 → 5  (round(1/3 * 16))
      total=3, track_idx=2 → 16
      total=1, track_idx=0 → 16  (einziger Track → voll)

    Pure function — robust gegen total<=0, track_idx ausserhalb des Bereichs.
    """
    if total <= 0:
        return 0
    progress = (max(0, track_idx) + 1) / total
    progress = min(1.0, progress)
    return max(1, min(strip_size, round(progress * strip_size)))


def spectrum_to_led_colors(
    bands: list[float],
    strip_size: int = STRIPS_TOTAL,
) -> list[Tuple[int, int, int]]:
    """Audio-Spektrum (normalisiert 0..1 pro Band) → Regenbogen-Farben pro LED.

    Erwartet exakt ``strip_size`` Werte (z.B. 16 Frequenzbänder). Jede LED
    bekommt eine feste Farbe (Hue aus Regenbogen), Helligkeit kommt vom
    jeweiligen Band-Amplitudenwert. Pure function für Tests.

    Wenn weniger/mehr Bänder reinkommen, wird interpoliert/abgeschnitten.
    """
    out: list[Tuple[int, int, int]] = []
    n = len(bands) if bands else 0
    for i in range(strip_size):
        # Band-Index: gleichmässige Zuordnung
        if n == 0:
            amp = 0.0
        elif n == strip_size:
            amp = bands[i]
        else:
            amp = bands[min(n - 1, int(i / strip_size * n))]
        amp = max(0.0, min(1.0, amp))
        # Hue: gleichmäßiger Regenbogen über alle LEDs
        hue = int(255 * i / strip_size) % 256
        base = _hue_to_rgb(hue)
        # Intensity skaliert mit der Amplitude — leise Bänder fast aus
        intensity = MAX_BRIGHTNESS * amp
        out.append(scale_to_intensity(base, intensity))
    return out


def _hue_to_rgb(h: int) -> Tuple[int, int, int]:
    """Hue 0..255 → RGB. Klassischer NeoPixel-Trick (3 Sektoren je 85)."""
    if h < 85:
        return (h * 3, 255 - h * 3, 0)
    if h < 170:
        h -= 85
        return (255 - h * 3, 0, h * 3)
    h -= 170
    return (0, h * 3, 255 - h * 3)


def speed_to_led_count(
    speed: float,
    min_speed: float = DEFAULT_SPEED_MIN,
    max_speed: float = DEFAULT_SPEED_MAX,
) -> int:
    """Player-Speed → 1..8 LEDs.

    1 LED bei ``min_speed``, 8 LEDs bei ``max_speed``. Im Gegensatz zu
    ``volume_to_led_count`` gibt es nie 0 LEDs — solange der Speed-Mode
    aktiv ist, soll immer mindestens eine LED leuchten (sonst sieht es so
    aus als wäre der Modus aus).
    """
    if speed <= min_speed:
        return 1
    if speed >= max_speed:
        return RING_SIZE
    span = max_speed - min_speed
    return max(1, min(RING_SIZE, math.ceil((speed - min_speed) / span * RING_SIZE)))


class LedsUnavailable(RuntimeError):
    """LED-Stack kann nicht laufen — Paket fehlt oder Hardware nicht verfügbar."""


class Leds:
    """Wrapper um die 25er-WS2812-Kette mit Zonen-API.

    Konstruktion ist billig; tatsächliche Hardware-Allokation passiert beim
    ersten Schreibvorgang. Bei Init-Fehlern (z.B. Paket fehlt) wird
    ``LedsUnavailable`` geworfen — der Rest der Box läuft davon unabhängig.
    """

    def __init__(self, brightness: float = MAX_BRIGHTNESS) -> None:
        try:
            import board  # type: ignore
            import neopixel  # type: ignore
        except ImportError as e:
            raise LedsUnavailable(
                "adafruit-circuitpython-neopixel fehlt. Installation: "
                ".venv/bin/pip install adafruit-circuitpython-neopixel "
                "Adafruit-Blinka-Raspberry-Pi5-Neopixel"
            ) from e

        capped = max(0.0, min(brightness, MAX_BRIGHTNESS))
        if capped != brightness:
            logger.info(
                "LED-Helligkeit %.2f auf Cap %.2f reduziert (Kinderschutz)",
                brightness, MAX_BRIGHTNESS,
            )
        self._pixels = neopixel.NeoPixel(
            board.D18, LED_COUNT,
            brightness=capped, auto_write=False,
            pixel_order=neopixel.GRB,
        )
        # NeoPixel ist nicht thread-safe; Auto-Off-Timer und Speed-Pulse
        # feuern aus separaten Threads, deshalb alle Schreibvorgänge unter
        # demselben Lock.
        self._lock = threading.Lock()
        self._volume_off_timer: threading.Timer | None = None
        # Speed-Mode-Pulse läuft in eigenem Daemon-Thread, gesteuert über
        # ein Event. _speed_led_count wird live updated, ohne den Thread
        # neu zu starten — der nächste Frame nimmt den neuen Wert auf.
        self._speed_pulse_thread: threading.Thread | None = None
        self._speed_pulse_stop = threading.Event()
        self._speed_led_count = 0
        # NFC-Status-Pulse: gleiches Schema (Thread + Event), aber eigene
        # Zone (nur die eine NFC-LED), eigene Frequenz, eigene Range.
        self._nfc_pulse_thread: threading.Thread | None = None
        self._nfc_pulse_stop = threading.Event()
        # Streifen-Animation: ein Thread rendert je nach _strips_mode
        # entweder Pseudo-Tanz/Spectrum oder Track-Position. Spektrum-Daten
        # liefert ein externer Spectrum-Capture-Thread via update_spectrum().
        self._strips_thread: threading.Thread | None = None
        self._strips_stop = threading.Event()
        self._strips_mode: str = "idle"  # "idle" | "dance" | "position"
        self._strips_position_until: float = 0.0
        self._strips_position_track: int = 0
        self._strips_position_total: int = 0
        # Letztes Audio-Spectrum (16 Werte 0..1). Wird vom Spectrum-Capture
        # geschrieben, vom Dance-Loop gelesen. Mit Timestamp, damit der
        # Dance-Loop auf Pseudo fallbacked wenn Audio-Capture verstummt.
        self._spectrum_bands: list[float] = [0.0] * STRIPS_TOTAL
        self._spectrum_updated_at: float = 0.0

    # ---- Roh-Zugriff -----------------------------------------------------

    def __setitem__(self, idx, color: Color) -> None:
        self._pixels[idx] = color

    def __getitem__(self, idx) -> Color:
        return self._pixels[idx]

    def show(self) -> None:
        """Sendet die Pixel an die LEDs. Bis hier passiert auf der Hardware nichts."""
        self._pixels.show()

    def off(self) -> None:
        """Alle LEDs aus."""
        self._pixels.fill(BLACK)
        self._pixels.show()

    def fill(self, color: Color) -> None:
        """Alle LEDs in einer Farbe."""
        self._pixels.fill(color)
        self._pixels.show()

    # ---- Zonen-API -------------------------------------------------------

    def fill_zone(self, zone: slice, color: Color) -> None:
        for i in range(*zone.indices(LED_COUNT)):
            self._pixels[i] = color
        self._pixels.show()

    def ring(self, color: Color) -> None:
        """LED-Ring um den Encoder."""
        self.fill_zone(ZONE_RING, color)

    def nfc(self, color: Color) -> None:
        """Einzelne NFC-Status-LED (rohe Farbe, ungesteuerte Intensität)."""
        self.fill_zone(ZONE_NFC, color)

    def nfc_chip_present(self) -> None:
        """Chip aufgelegt → grünes Status-Licht, sanft pulsierend 5–15% bei 1 Hz.

        Startet den Pulse-Thread (idempotent — zweimal Aufrufen tut nichts).
        Bei ``nfc_chip_absent`` wird der Thread gestoppt und die LED gelöscht.
        """
        with self._lock:
            if (
                self._nfc_pulse_thread is not None
                and self._nfc_pulse_thread.is_alive()
            ):
                return  # läuft schon
            self._nfc_pulse_stop.clear()
            self._nfc_pulse_thread = threading.Thread(
                target=self._nfc_pulse_loop,
                daemon=True,
                name="led-nfc-pulse",
            )
            self._nfc_pulse_thread.start()

    def nfc_chip_absent(self) -> None:
        """Chip ist weg → Pulse stoppen + Status-LED aus."""
        self._nfc_pulse_stop.set()
        thread = self._nfc_pulse_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._nfc_pulse_thread = None
        with self._lock:
            for i in range(*ZONE_NFC.indices(LED_COUNT)):
                self._pixels[i] = BLACK
            self._pixels.show()

    def _nfc_pulse_loop(self) -> None:
        """20-fps-Loop: skaliert NFC_PRESENT_COLOR_BASE mit Sinus-Intensität
        zwischen MIN und MAX. Läuft bis ``_nfc_pulse_stop`` gesetzt wird."""
        t0 = time.monotonic()
        frame_time = 1.0 / NFC_PULSE_FPS
        amplitude = (
            NFC_PRESENT_PULSE_MAX_INTENSITY - NFC_PRESENT_PULSE_MIN_INTENSITY
        )
        try:
            while not self._nfc_pulse_stop.is_set():
                t = time.monotonic() - t0
                # sin geht von -1..1, /2+0.5 normalisiert auf 0..1
                pulse = 0.5 * (1 + math.sin(2 * math.pi * NFC_PULSE_HZ * t))
                intensity = NFC_PRESENT_PULSE_MIN_INTENSITY + amplitude * pulse
                color = scale_to_intensity(NFC_PRESENT_COLOR_BASE, intensity)
                with self._lock:
                    for i in range(*ZONE_NFC.indices(LED_COUNT)):
                        self._pixels[i] = color
                    self._pixels.show()
                if self._nfc_pulse_stop.wait(frame_time):
                    return
        except Exception:
            logger.exception("NFC-Pulse-Loop crashed")

    def strip_left(self, color: Color) -> None:
        self.fill_zone(ZONE_STRIP_LEFT, color)

    def strip_right(self, color: Color) -> None:
        self.fill_zone(ZONE_STRIP_RIGHT, color)

    def strips(self, color: Color) -> None:
        """Beide Streifen gleichzeitig."""
        for i in range(ZONE_STRIP_LEFT.start, ZONE_STRIP_RIGHT.stop):
            self._pixels[i] = color
        self._pixels.show()

    # ---- Volume-Visualisierung ------------------------------------------

    def show_volume(self, percent: float) -> None:
        """Zeigt aktuelle Lautstärke auf dem Encoder-Ring an.

        Verhalten:
          - 0% → Ring komplett aus (Auto-Off-Timer wird trotzdem armed, damit
            wir bei nochmaligem "leiser drehen" nicht hängenbleiben).
          - 1..12.5% → 1 LED in Blau
          - ...
          - 87.5..100% → 8 LEDs in Rot
        Nach VOLUME_AUTO_OFF_S Sekunden ohne erneuten Aufruf geht der Ring
        wieder aus. Jeder weitere Aufruf resettet den Timer.

        Thread-safe — kann aus jedem Thread gerufen werden.
        """
        count = volume_to_led_count(percent)
        color = VOLUME_COLORS[count - 1] if count > 0 else BLACK
        with self._lock:
            for i in range(RING_SIZE):
                self._pixels[i] = color if i < count else BLACK
            self._pixels.show()
            self._reset_volume_auto_off()

    def _reset_volume_auto_off(self) -> None:
        """Cancel + restart des Auto-Off-Timers. Lock vom Caller halten."""
        if self._volume_off_timer is not None:
            self._volume_off_timer.cancel()
        self._volume_off_timer = threading.Timer(
            VOLUME_AUTO_OFF_S, self._ring_off_locked
        )
        self._volume_off_timer.daemon = True
        self._volume_off_timer.start()

    def _ring_off_locked(self) -> None:
        """Schaltet nur den Ring aus — NFC + Streifen bleiben wie sie sind."""
        with self._lock:
            for i in range(*ZONE_RING.indices(LED_COUNT)):
                self._pixels[i] = BLACK
            self._pixels.show()

    # ---- Speed-Mode-Visualisierung --------------------------------------

    def show_speed(
        self,
        speed: float,
        min_speed: float = DEFAULT_SPEED_MIN,
        max_speed: float = DEFAULT_SPEED_MAX,
    ) -> None:
        """Aktiviert die lila pulsierende Speed-Anzeige (oder aktualisiert sie).

        Beim ersten Aufruf startet der Pulse-Thread; weitere Aufrufe (z.B.
        Speed-Änderung im laufenden Modus) updaten nur die Anzahl der LEDs.
        Verdrängt den Volume-Auto-Off-Timer — solange Speed-Mode aktiv ist,
        soll der Ring durchgängig leuchten.
        """
        self._speed_led_count = speed_to_led_count(speed, min_speed, max_speed)
        with self._lock:
            if self._volume_off_timer is not None:
                self._volume_off_timer.cancel()
                self._volume_off_timer = None
            if (
                self._speed_pulse_thread is None
                or not self._speed_pulse_thread.is_alive()
            ):
                self._speed_pulse_stop.clear()
                self._speed_pulse_thread = threading.Thread(
                    target=self._speed_pulse_loop,
                    daemon=True,
                    name="led-speed-pulse",
                )
                self._speed_pulse_thread.start()

    def hide_speed(self) -> None:
        """Stoppt den Pulse-Thread und löscht den Ring. NFC + Streifen bleiben."""
        self._speed_pulse_stop.set()
        thread = self._speed_pulse_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._speed_pulse_thread = None
        self._speed_led_count = 0
        with self._lock:
            for i in range(*ZONE_RING.indices(LED_COUNT)):
                self._pixels[i] = BLACK
            self._pixels.show()

    def _speed_pulse_loop(self) -> None:
        """20-fps-Loop: skaliert SPEED_COLOR_BASE mit Sinus-Faktor und schreibt
        den Ring. Läuft bis ``_speed_pulse_stop`` gesetzt wird."""
        t0 = time.monotonic()
        frame_time = 1.0 / SPEED_PULSE_FPS
        amplitude = SPEED_PULSE_FACTOR_MAX - SPEED_PULSE_FACTOR_MIN
        try:
            while not self._speed_pulse_stop.is_set():
                t = time.monotonic() - t0
                # sin geht von -1..1, /2+0.5 normalisiert auf 0..1
                pulse = 0.5 * (1 + math.sin(2 * math.pi * SPEED_PULSE_HZ * t))
                factor = SPEED_PULSE_FACTOR_MIN + amplitude * pulse
                color = tuple(int(c * factor) for c in SPEED_COLOR_BASE)
                count = self._speed_led_count  # snapshot — kann sich live ändern
                with self._lock:
                    for i in range(RING_SIZE):
                        self._pixels[i] = color if i < count else BLACK
                    self._pixels.show()
                # Event.wait gibt True zurück sobald gesetzt → sauber raus
                if self._speed_pulse_stop.wait(frame_time):
                    return
        except Exception:
            logger.exception("Speed-Pulse-Loop crashed")

    # ---- Streifen: Tanz + Track-Position --------------------------------

    def strips_dance_start(self) -> None:
        """Aktiviert die Tanz-Animation auf beiden Streifen.

        Wenn vom Spectrum-Capture Frequenz-Bänder geliefert werden, tanzt's
        audio-reaktiv. Solange keine Daten kommen (oder älter als 1s), fällt
        die Animation auf eine zeitbasierte Pseudo-Welle zurück — damit
        sieht der User immer Bewegung, nicht stumme LEDs.
        """
        with self._lock:
            self._strips_mode = "dance"
            self._ensure_strips_thread_locked()

    def strips_dance_stop(self) -> None:
        """Beendet jede Streifen-Animation; LEDs aus."""
        self._strips_stop.set()
        thread = self._strips_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._strips_thread = None
        with self._lock:
            self._strips_mode = "idle"
            for i in range(ZONE_STRIP_LEFT.start, ZONE_STRIP_RIGHT.stop):
                self._pixels[i] = BLACK
            self._pixels.show()

    def strips_show_position(self, track_idx: int, total: int) -> None:
        """Zeigt aktuelle Track-Position auf den Streifen — für 5 s, dann
        Auto-Rückkehr zum Tanz.

        Sofort renderbar (Render-Loop pickt es im nächsten Frame auf). Wenn
        der Tanz aus ist, geht's danach in "idle" (keine LEDs).
        """
        with self._lock:
            self._strips_position_until = time.monotonic() + POSITION_DISPLAY_S
            self._strips_position_track = track_idx
            self._strips_position_total = total
            self._strips_mode = "position"
            self._ensure_strips_thread_locked()

    def update_spectrum(self, bands: list[float]) -> None:
        """Wird vom Spectrum-Capture-Thread aufgerufen. Live-Update der Daten.

        ``bands`` ist eine Liste von 0..1-Werten (eine pro Frequenzband).
        Der Dance-Loop liest sie im nächsten Frame.
        """
        # Kein Lock nötig — wir tauschen die Liste atomic. Race ist benign
        # (worst case rendert ein Frame mit alten Werten).
        self._spectrum_bands = list(bands)
        self._spectrum_updated_at = time.monotonic()

    def _ensure_strips_thread_locked(self) -> None:
        """Startet den Render-Thread falls noch nicht aktiv. Lock vom Caller."""
        if self._strips_thread is not None and self._strips_thread.is_alive():
            return
        self._strips_stop.clear()
        self._strips_thread = threading.Thread(
            target=self._strips_loop, daemon=True, name="led-strips"
        )
        self._strips_thread.start()

    def _strips_loop(self) -> None:
        """Render-Loop für die Streifen. Switched zwischen dance/position
        modes anhand von _strips_mode + Position-Timeout."""
        frame_time = 1.0 / DANCE_FPS
        try:
            while not self._strips_stop.is_set():
                # Mode-Übergang prüfen: Position läuft nach 5s aus
                mode = self._strips_mode
                if mode == "position" and time.monotonic() > self._strips_position_until:
                    with self._lock:
                        self._strips_mode = "dance"
                    mode = "dance"

                if mode == "dance":
                    self._render_dance()
                elif mode == "position":
                    self._render_position()
                # "idle" → Loop exits beim nächsten _strips_stop.wait

                if self._strips_stop.wait(frame_time):
                    return
        except Exception:
            logger.exception("Strips-Loop crashed")

    def _render_dance(self) -> None:
        """Audio-reaktiv wenn Spectrum frisch (<1s), sonst Pseudo-Welle."""
        spectrum_age = time.monotonic() - self._spectrum_updated_at
        if spectrum_age < 1.0 and any(self._spectrum_bands):
            colors = spectrum_to_led_colors(self._spectrum_bands)
        else:
            colors = self._pseudo_dance_colors()
        with self._lock:
            for i, c in enumerate(colors):
                self._pixels[ZONE_STRIP_LEFT.start + i] = c
            self._pixels.show()

    def _pseudo_dance_colors(self) -> list[Tuple[int, int, int]]:
        """Zeitbasierte Regenbogen-Welle mit virtuellem Beat. Fallback für
        Live-Test ohne echte Audio-Quelle."""
        t = time.monotonic()
        colors: list[Tuple[int, int, int]] = []
        bpm_phase = 2 * math.pi * (PSEUDO_DANCE_BPM / 60.0) * t
        for i in range(STRIPS_TOTAL):
            # Welle wandert durch den Streifen (Phase-Versatz pro LED)
            led_phase = i / STRIPS_TOTAL * 2 * math.pi
            beat = 0.5 * (1 + math.sin(bpm_phase + led_phase * 2))
            intensity = MAX_BRIGHTNESS * (0.20 + 0.80 * beat)  # 5..25%
            hue = (int(t * 30) + int(255 * i / STRIPS_TOTAL)) % 256
            colors.append(scale_to_intensity(_hue_to_rgb(hue), intensity))
        return colors

    def _render_position(self) -> None:
        """Statische Anzeige: n_lit weiße LEDs links, Rest schwarz."""
        n_lit = position_to_led_count(
            self._strips_position_track,
            self._strips_position_total,
        )
        color = scale_to_intensity(POSITION_COLOR_BASE, POSITION_INTENSITY)
        with self._lock:
            for i in range(STRIPS_TOTAL):
                self._pixels[ZONE_STRIP_LEFT.start + i] = color if i < n_lit else BLACK
            self._pixels.show()

    # ---- Cleanup ---------------------------------------------------------

    def close(self) -> None:
        """LEDs ausschalten + Resourcen freigeben."""
        self._speed_pulse_stop.set()
        self._nfc_pulse_stop.set()
        self._strips_stop.set()
        if self._volume_off_timer is not None:
            self._volume_off_timer.cancel()
        try:
            self.off()
        except Exception:
            pass
        try:
            self._pixels.deinit()
        except Exception:
            pass
