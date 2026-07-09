"""Tests für audio.spotify (SpotifyController + URI-Normalisierung).

Der echte go-librespot-Daemon wird nicht angefasst — _get/_post werden
gemockt. Die HTTP-Schicht selbst ist trivial (requests), interessant sind
die Entscheidungslogik von turn_on() und das URI-Parsing.
"""
import threading

import pytest

from audio.spotify import SpotifyController, normalize_spotify_uri


# ----------------------------------------------------------------------
# normalize_spotify_uri
# ----------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("spotify:playlist:37i9dQZF1DX10zKzsJ2jva",
     "spotify:playlist:37i9dQZF1DX10zKzsJ2jva"),
    ("spotify:album:6akEvsycLGftJxYudPjmqK",
     "spotify:album:6akEvsycLGftJxYudPjmqK"),
    # Share-Link aus der App, mit Tracking-Query
    ("https://open.spotify.com/playlist/37i9dQZF1DX10zKzsJ2jva?si=abc123",
     "spotify:playlist:37i9dQZF1DX10zKzsJ2jva"),
    # Internationalisierte Links
    ("https://open.spotify.com/intl-de/album/6akEvsycLGftJxYudPjmqK",
     "spotify:album:6akEvsycLGftJxYudPjmqK"),
    ("  spotify:track:11dFghVXANMlKmJXsNCbNl  ",
     "spotify:track:11dFghVXANMlKmJXsNCbNl"),
])
def test_normalize_valid(value, expected):
    assert normalize_spotify_uri(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "   ", "kinderlieder", "https://example.com/playlist/abc",
    "spotify:kaka:123",
])
def test_normalize_invalid(value):
    assert normalize_spotify_uri(value) is None


# ----------------------------------------------------------------------
# turn_on-Entscheidungslogik
# ----------------------------------------------------------------------

def _controller(status=None, default_uri=None):
    """Controller mit gemockter HTTP-Schicht; zeichnet POSTs auf."""
    c = SpotifyController(default_uri=default_uri)
    c._get = lambda path: status
    c.posts = []
    c._post = (lambda path, payload=None, **kw:
               c.posts.append((path, payload)) or True)
    return c


def test_turn_on_daemon_unreachable():
    c = _controller(status=None)
    assert c.turn_on() is False
    assert c.posts == []


def test_turn_on_not_logged_in():
    c = _controller(status={"stopped": True})
    assert c.turn_on() is False
    assert c.posts == []


def test_turn_on_resumes_paused_context():
    c = _controller(status={
        "username": "box", "stopped": False, "paused": True,
        "track": {"uri": "spotify:track:x"},
    })
    assert c.turn_on() is True
    assert ("/player/resume", None) in c.posts


def test_turn_on_empty_player_starts_default_uri():
    c = _controller(
        status={"username": "box", "stopped": True, "track": None},
        default_uri="https://open.spotify.com/playlist/37i9dQZF1DX10zKzsJ2jva",
    )
    assert c.turn_on() is True
    assert ("/player/play",
            {"uri": "spotify:playlist:37i9dQZF1DX10zKzsJ2jva"}) in c.posts


def test_turn_on_empty_player_without_default_uri():
    c = _controller(status={"username": "box", "stopped": True, "track": None})
    assert c.turn_on() is False
    assert c.posts == []


def test_turn_on_passes_volume():
    c = _controller(status={
        "username": "box", "stopped": False, "paused": True,
        "track": {"uri": "spotify:track:x"},
    })
    sent = threading.Event()
    volumes = []

    def fake_post(path, payload=None, **kw):
        if path == "/player/volume":
            volumes.append(payload["volume"])
            sent.set()
        c.posts.append((path, payload))
        return True

    c._post = fake_post
    assert c.turn_on(volume=42) is True
    assert sent.wait(2.0), "Volume-Worker hat nicht gesendet"
    assert volumes == [42]


# ----------------------------------------------------------------------
# is_playing (Standby-Check)
# ----------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    (None, False),
    ({"username": "box", "stopped": True, "paused": False, "track": None}, False),
    ({"username": "box", "stopped": False, "paused": True,
      "track": {"uri": "x"}}, False),
    ({"username": "box", "stopped": False, "paused": False,
      "track": {"uri": "x"}}, True),
])
def test_is_playing(status, expected):
    c = _controller(status=status)
    assert c.is_playing() is expected


# ----------------------------------------------------------------------
# Volume-Coalescing
# ----------------------------------------------------------------------

def test_volume_clamped_and_coalesced():
    c = SpotifyController()
    c._get = lambda path: None
    done = threading.Event()
    sent = []

    def fake_post(path, payload=None, **kw):
        sent.append(payload["volume"])
        done.set()
        return True

    c._post = fake_post
    # Worker schläft noch auf dem Event → mehrere Ticks landen als EIN Send
    # (mindestens der letzte Wert muss ankommen, geklemmt auf 0..100).
    c.set_volume_async(150)
    c.set_volume_async(-5)
    c.set_volume_async(77)
    assert done.wait(2.0), "Volume-Worker hat nicht gesendet"
    # Der letzte gesendete Wert muss der zuletzt gesetzte (geklemmte) sein.
    deadline = threading.Event()
    deadline.wait(0.2)  # kurze Karenz für evtl. zweiten Send
    assert sent[-1] == 77
    assert all(0 <= v <= 100 for v in sent)
