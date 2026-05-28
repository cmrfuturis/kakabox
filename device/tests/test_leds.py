"""Tests fuer hardware.leds — pure-function-Logik (kein Hardware-Zugriff).

Hardware-Init der Leds-Klasse braucht board+neopixel und läuft nur auf der
echten Pi-5-Hardware, ist also hier ausgeklammert.
"""
from __future__ import annotations

import pytest

from hardware.leds import (
    DEFAULT_SPEED_MAX,
    DEFAULT_SPEED_MIN,
    MAX_BRIGHTNESS,
    NFC_PRESENT_COLOR_BASE,
    NFC_PRESENT_PULSE_MAX_INTENSITY,
    NFC_PRESENT_PULSE_MIN_INTENSITY,
    RING_SIZE,
    SPEED_COLOR_BASE,
    STRIPS_TOTAL,
    VOLUME_COLORS,
    position_to_led_count,
    scale_to_intensity,
    speed_to_led_count,
    spectrum_to_led_colors,
    volume_to_led_count,
)


# ---- volume_to_led_count -----------------------------------------------------

@pytest.mark.parametrize("percent,expected_count", [
    (0,      0),    # leise → keine LED
    (-5,     0),    # robust gegen negative Werte
    (1,      1),    # sofort 1 LED, damit der User Feedback hat
    (12,     1),
    (12.5,   1),    # genau die Grenze
    (13,     2),    # ueber der Grenze → 2 LEDs
    (25,     2),
    (26,     3),
    (50,     4),
    (75,     6),
    (87,     7),
    (87.5,   7),
    (88,     8),
    (100,    8),
    (150,    8),    # robust gegen Werte > 100
])
def test_volume_to_led_count_thresholds(percent, expected_count):
    assert volume_to_led_count(percent) == expected_count


def test_volume_to_led_count_never_exceeds_ring_size():
    for percent in range(0, 201, 5):
        assert 0 <= volume_to_led_count(percent) <= RING_SIZE


def test_volume_to_led_count_is_monotonic():
    """Mehr Lautstaerke → mindestens gleich viele LEDs, nie weniger."""
    last = -1
    for percent in range(0, 101):
        c = volume_to_led_count(percent)
        assert c >= last, f"non-monotonic at {percent}%: was {last}, now {c}"
        last = c


# ---- VOLUME_COLORS -----------------------------------------------------------

def test_volume_colors_match_ring_size():
    """Jede Stufe muss eine Farbe haben — sonst IndexError im Live-Lauf."""
    assert len(VOLUME_COLORS) == RING_SIZE


def test_volume_colors_go_from_blue_to_red():
    """Erste Stufe blau-dominant, letzte rot-dominant — Sanity-Check des
    Gradients. Wenn jemand die Palette umsortiert, faengt das hier auf."""
    r0, g0, b0 = VOLUME_COLORS[0]
    rN, gN, bN = VOLUME_COLORS[-1]
    assert b0 > r0, f"erste Stufe muss blau-lastig sein: {VOLUME_COLORS[0]}"
    assert rN > bN, f"letzte Stufe muss rot-lastig sein: {VOLUME_COLORS[-1]}"


# ---- speed_to_led_count ------------------------------------------------------

@pytest.mark.parametrize("speed,expected_count", [
    (0.5,   1),     # genau Min
    (0.3,   1),     # unter Min → clamp
    (0.6,   1),     # knapp über Min, noch in der 1. Stufe
    (0.7,   2),     # ueber 1. Schwelle
    (1.0,   3),     # Normal-Geschwindigkeit → 3 LEDs (Mitte etwas links)
    (1.25,  4),
    (1.5,   6),
    (1.75,  7),
    (2.0,   8),     # Max
    (3.0,   8),     # ueber Max → clamp
])
def test_speed_to_led_count_thresholds(speed, expected_count):
    assert speed_to_led_count(speed) == expected_count


def test_speed_to_led_count_always_at_least_one():
    """Auch bei Speed = Min muss mindestens 1 LED leuchten — sonst sieht
    der Speed-Mode aus wie 'aus'."""
    for speed in (0.5, 0.3, -1.0, 0.0):
        assert speed_to_led_count(speed) >= 1


def test_speed_to_led_count_monotonic():
    last = -1
    speed = DEFAULT_SPEED_MIN
    while speed <= DEFAULT_SPEED_MAX + 0.01:
        c = speed_to_led_count(speed)
        assert c >= last, f"non-monotonic at speed={speed}: was {last}, now {c}"
        last = c
        speed += 0.05


def test_speed_color_base_is_purple():
    """Speed-Mode muss visuell vom Volume-Modus unterscheidbar sein. Lila
    heißt: R > 0, B > 0, G klein. Verhindert versehentliches Ändern z.B.
    auf Grün, das visuell zu nahe an Volume-Stufe 4 wäre."""
    r, g, b = SPEED_COLOR_BASE
    assert r > 50 and b > 50, f"Lila braucht R + B: {SPEED_COLOR_BASE}"
    assert g < r and g < b, f"G muss klein bleiben für Lila: {SPEED_COLOR_BASE}"


# ---- scale_to_intensity ------------------------------------------------------

def test_scale_to_intensity_at_max_brightness_is_identity():
    """Wenn die gewünschte Intensität gleich MAX_BRIGHTNESS ist, sollen die
    Farbwerte unverändert bleiben (Faktor = 1)."""
    assert scale_to_intensity((0, 255, 0), MAX_BRIGHTNESS) == (0, 255, 0)
    assert scale_to_intensity((180, 0, 200), MAX_BRIGHTNESS) == (180, 0, 200)


def test_scale_to_intensity_halves_at_half():
    """Intensität halb so hoch wie Max → Farbwerte halbiert."""
    half = MAX_BRIGHTNESS / 2
    r, g, b = scale_to_intensity((200, 100, 50), half)
    assert (r, g, b) == (100, 50, 25)


def test_scale_to_intensity_nfc_pulse_range():
    """NFC pulsiert zwischen MIN- und MAX-Intensität. ``scale_to_intensity``
    bildet die absolute Intensität relativ zu ``MAX_BRIGHTNESS`` ab.

    Erwartung aus den Konstanten ableiten, damit der Test bei einer
    Helligkeitsänderung (z.B. MIN 0.05→0.10) nicht veraltet — er prüft die
    Skalierungs-Logik, nicht einen hartkodierten Pegel.
    """
    low = scale_to_intensity(NFC_PRESENT_COLOR_BASE, NFC_PRESENT_PULSE_MIN_INTENSITY)
    high = scale_to_intensity(NFC_PRESENT_COLOR_BASE, NFC_PRESENT_PULSE_MAX_INTENSITY)

    min_factor = NFC_PRESENT_PULSE_MIN_INTENSITY / MAX_BRIGHTNESS
    expected_low = tuple(int(c * min_factor) for c in NFC_PRESENT_COLOR_BASE)
    assert low == expected_low
    # MAX-Intensität == MAX_BRIGHTNESS → Faktor 1.0 → Basisfarbe unverändert.
    assert high == NFC_PRESENT_COLOR_BASE


def test_nfc_pulse_range_is_sane():
    """Min < Max und beide < MAX_BRIGHTNESS (sonst klemmt scale_to_intensity)."""
    assert 0 <= NFC_PRESENT_PULSE_MIN_INTENSITY < NFC_PRESENT_PULSE_MAX_INTENSITY
    assert NFC_PRESENT_PULSE_MAX_INTENSITY <= MAX_BRIGHTNESS


# ---- position_to_led_count ---------------------------------------------------

@pytest.mark.parametrize("track_idx,total,expected", [
    (0,  5,  3),    # Track 1/5 → 16/5 ≈ 3
    (1,  5,  6),    # Track 2/5 → 32/5 ≈ 6
    (2,  5, 10),    # Track 3/5 → 48/5 ≈ 10
    (4,  5, 16),    # letzter Track → alle 16
    (0,  3,  5),    # Track 1/3 → 16/3 ≈ 5
    (2,  3, 16),    # letzter Track 3/3 → alle 16
    (0,  1, 16),    # einziger Track → voll
    (0,  16, 1),    # Track 1/16 → 1 LED
    (15, 16, 16),   # Track 16/16 → 16
])
def test_position_to_led_count_examples(track_idx, total, expected):
    assert position_to_led_count(track_idx, total) == expected


def test_position_to_led_count_robust_against_bad_input():
    """Defensive: negative Indizes oder ueberzaehlige Werte sollen den
    Caller nicht crashen lassen."""
    assert position_to_led_count(-1, 5) == 3       # negativ → track 0
    assert position_to_led_count(100, 5) == 16     # ueber total → voll
    assert position_to_led_count(0, 0) == 0        # total=0 → nichts
    assert position_to_led_count(0, -5) == 0


def test_position_to_led_count_monotonic_within_total():
    """Innerhalb einer Playlist soll mehr Track-Index immer ≥ LEDs ergeben."""
    for total in (1, 3, 5, 10, 16, 50):
        last = -1
        for idx in range(total):
            c = position_to_led_count(idx, total)
            assert c >= last, f"non-monotonic at idx={idx}, total={total}"
            last = c


# ---- spectrum_to_led_colors --------------------------------------------------

def test_spectrum_to_led_colors_silent_is_dark():
    """Bei komplett stillem Audio (alle bands=0) sollen die LEDs aus sein."""
    colors = spectrum_to_led_colors([0.0] * STRIPS_TOTAL)
    assert len(colors) == STRIPS_TOTAL
    assert all(c == (0, 0, 0) for c in colors)


def test_spectrum_to_led_colors_loud_is_lit():
    """Bei vollem Pegel pro Band sollen alle LEDs leuchten (nicht schwarz)."""
    colors = spectrum_to_led_colors([1.0] * STRIPS_TOTAL)
    assert len(colors) == STRIPS_TOTAL
    assert all(c != (0, 0, 0) for c in colors)


def test_spectrum_to_led_colors_handles_empty_input():
    """Wenn der Spectrum-Capture noch nichts geliefert hat, soll der Aufruf
    nicht crashen — Fallback auf alle aus."""
    colors = spectrum_to_led_colors([])
    assert len(colors) == STRIPS_TOTAL
    assert all(c == (0, 0, 0) for c in colors)


def test_spectrum_to_led_colors_interpolates_band_count():
    """Wenn weniger Bänder als LEDs kommen (z.B. 8 statt 16), wird dupliziert."""
    colors = spectrum_to_led_colors([1.0] * 8)
    assert len(colors) == STRIPS_TOTAL
    assert all(c != (0, 0, 0) for c in colors)


def test_spectrum_to_led_colors_uses_rainbow_hues():
    """Verschiedene Positionen sollen unterschiedliche Hues haben (kein
    Mono-Color-Bar). Sanity-Check fuer die Rainbow-Verteilung."""
    colors = spectrum_to_led_colors([1.0] * STRIPS_TOTAL)
    # Erste, mittlere, letzte LED sollen sich farblich unterscheiden
    assert colors[0] != colors[STRIPS_TOTAL // 2]
    assert colors[0] != colors[-1]


def test_scale_to_intensity_clamps_above_max():
    """Wenn jemand intensity > max_brightness setzt, soll geclampt werden
    (sonst wuerde *factor > 1.0 Farbwerte > 255 erzeugen → invalid)."""
    color = scale_to_intensity((255, 255, 255), 1.0)  # 1.0 > 0.25
    assert color == (255, 255, 255)


def test_scale_to_intensity_zero_yields_black():
    assert scale_to_intensity((255, 0, 0), 0.0) == (0, 0, 0)


def test_scale_to_intensity_negative_yields_black():
    """Negative Intensität wird auf 0 geclampt — niemand soll inverse Farben
    bekommen, nur weil ein Aufrufer Mist berechnet hat."""
    assert scale_to_intensity((255, 0, 0), -0.5) == (0, 0, 0)


def test_nfc_present_color_is_green():
    """Sanity-Check: die NFC-Status-Basis-Farbe muss grün sein (G dominant)."""
    r, g, b = NFC_PRESENT_COLOR_BASE
    assert g > r and g > b, f"NFC-Status muss grün sein: {NFC_PRESENT_COLOR_BASE}"
