"""Tests für Phase 3 (Server-Hybrid ASR).

Zwei Ebenen:
- ``Backend.transcribe_audio``: der HTTP-Aufruf isoliert (responses-Mock) —
  Erfolg, 503 (nicht scharf), Transportfehler, kaputte Antwort → alle
  best-effort, nie Exception, ``None`` bei Nichtverfügbarkeit.
- ``Kakabox._transcribe_command``: die Orchestrierung — Server zuerst, bei
  jedem Fehlschlag lokal; Opt-in-Tor (server_asr_enabled) und Online-/Standby-
  Tor. Datenschutzrelevant: OHNE Flag darf NIE Audio an den Server gehen.
"""
import json
import wave
from pathlib import Path

import pytest
import responses

import main
from network.backend import Backend


# --------------------------------------------------------------------------
# Backend.transcribe_audio (HTTP isoliert)
# --------------------------------------------------------------------------

@pytest.fixture
def identity_path(tmp_path: Path) -> Path:
    p = tmp_path / "box_identity.json"
    p.write_text(json.dumps({
        "serial_number": "KB-TEST-001",
        "api_token": "test-plain-token",
        "registered_at": "connected",
    }))
    return p


@pytest.fixture
def backend(identity_path: Path) -> Backend:
    return Backend(identity_path=identity_path, base_url="https://test")


def _wav(tmp_path: Path) -> Path:
    p = tmp_path / "cmd.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return p


@responses.activate
def test_transcribe_audio_success(backend, tmp_path):
    responses.post(
        "https://test/api/box/transcribe",
        json={"status": "ok", "text": "spiele die biene maja", "model": "small"},
        status=200,
    )
    text = backend.transcribe_audio(_wav(tmp_path))
    assert text == "spiele die biene maja"
    # Bearer-Auth mitgeschickt
    assert responses.calls[0].request.headers["Authorization"] == "Bearer test-plain-token"


@responses.activate
def test_transcribe_audio_503_returns_none(backend, tmp_path):
    # Server-ASR nicht scharfgeschaltet / Dienst down → None (lokal weiter).
    responses.post(
        "https://test/api/box/transcribe",
        json={"status": "unavailable", "reason": "disabled"},
        status=503,
    )
    assert backend.transcribe_audio(_wav(tmp_path)) is None


@responses.activate
def test_transcribe_audio_transport_error_returns_none(backend, tmp_path):
    responses.post(
        "https://test/api/box/transcribe",
        body=responses.ConnectionError("boom"),
    )
    assert backend.transcribe_audio(_wav(tmp_path)) is None


@responses.activate
def test_transcribe_audio_bad_json_returns_none(backend, tmp_path):
    responses.post("https://test/api/box/transcribe", body="not json", status=200)
    assert backend.transcribe_audio(_wav(tmp_path)) is None


@responses.activate
def test_transcribe_audio_non_object_json_returns_none(backend, tmp_path):
    # Wohlgeformtes, aber nicht-Objekt-JSON (z.B. von einem Proxy) darf NICHT
    # werfen ("Wirft NIE"-Vertrag) — payload.get() würde sonst crashen.
    responses.post("https://test/api/box/transcribe", json=[], status=200)
    assert backend.transcribe_audio(_wav(tmp_path)) is None


def test_transcribe_audio_offline_returns_none(tmp_path):
    # Kein Token → offline → kein Netzaufruf, None.
    p = tmp_path / "id.json"
    p.write_text(json.dumps({"serial_number": "x"}))
    b = Backend(identity_path=p, base_url="https://test")
    assert b.transcribe_audio(_wav(tmp_path)) is None


def test_transcribe_audio_missing_file_returns_none(backend, tmp_path):
    assert backend.transcribe_audio(tmp_path / "nope.wav") is None


# --------------------------------------------------------------------------
# Kakabox._transcribe_command (Orchestrierung: Server zuerst, Fallback lokal)
# --------------------------------------------------------------------------

class _FakeBackend:
    def __init__(self, connected=True, result="server-text"):
        self.is_connected = connected
        self._result = result
        self.calls = 0

    def transcribe_audio(self, wav, timeout=None):
        self.calls += 1
        return self._result


class _FakeRecognizer:
    def __init__(self, text="local-text"):
        self.text = text
        self.calls = 0

    def transcribe_wav(self, wav):
        self.calls += 1
        return self.text


def _box(server_enabled, backend, recognizer, standby=False):
    box = object.__new__(main.Kakabox)
    box.config = {"voice": {"server_asr_enabled": server_enabled}}
    box.backend = backend
    box._recognizer = recognizer
    box._standby = standby
    return box


def test_flag_off_never_calls_server():
    """Datenschutz-Tor: ohne server_asr_enabled geht NIE Audio zum Server."""
    be, rec = _FakeBackend(), _FakeRecognizer()
    box = _box(False, be, rec)
    assert box._transcribe_command("cmd.wav") == "local-text"
    assert be.calls == 0
    assert rec.calls == 1


def test_flag_on_uses_server_result():
    be, rec = _FakeBackend(result="spiele bibi und tina"), _FakeRecognizer()
    box = _box(True, be, rec)
    assert box._transcribe_command("cmd.wav") == "spiele bibi und tina"
    assert be.calls == 1
    assert rec.calls == 0   # Server lieferte → lokal gar nicht erst versucht


def test_server_none_falls_back_local():
    be, rec = _FakeBackend(result=None), _FakeRecognizer()
    box = _box(True, be, rec)
    assert box._transcribe_command("cmd.wav") == "local-text"
    assert be.calls == 1
    assert rec.calls == 1


def test_server_empty_falls_back_local():
    # Server lief, hörte aber nichts ("") → lokale Zweitchance.
    be, rec = _FakeBackend(result=""), _FakeRecognizer()
    box = _box(True, be, rec)
    assert box._transcribe_command("cmd.wav") == "local-text"
    assert rec.calls == 1


def test_server_exception_falls_back_local():
    class _BoomBackend:
        is_connected = True
        def transcribe_audio(self, wav, timeout=None):
            raise RuntimeError("boom")
    rec = _FakeRecognizer()
    box = _box(True, _BoomBackend(), rec)
    assert box._transcribe_command("cmd.wav") == "local-text"
    assert rec.calls == 1


def test_offline_uses_local():
    be, rec = _FakeBackend(connected=False), _FakeRecognizer()
    box = _box(True, be, rec)
    assert box._transcribe_command("cmd.wav") == "local-text"
    assert be.calls == 0


def test_standby_uses_local():
    be, rec = _FakeBackend(), _FakeRecognizer()
    box = _box(True, be, rec, standby=True)
    assert box._transcribe_command("cmd.wav") == "local-text"
    assert be.calls == 0


# --------------------------------------------------------------------------
# _voice_uses_server (Aufnahme-LED: lila = Server, blau = lokal)
# --------------------------------------------------------------------------

def _led_box(server_enabled, *, online, connected=True, standby=False):
    box = object.__new__(main.Kakabox)
    box.config = {"voice": {"server_asr_enabled": server_enabled}}
    box.backend = _FakeBackend(connected=connected)
    box._server_online = online
    box._standby = standby
    return box


def test_voice_uses_server_when_enabled_and_online():
    assert _led_box(True, online=True)._voice_uses_server() is True


def test_voice_uses_local_when_flag_off():
    # Feature aus → blau, auch wenn online (Server wird eh nicht genutzt).
    assert _led_box(False, online=True)._voice_uses_server() is False


def test_voice_uses_local_when_offline():
    # Der eigentliche Sinn: offline → blau, nicht lila.
    assert _led_box(True, online=False)._voice_uses_server() is False


def test_voice_uses_local_when_standby():
    assert _led_box(True, online=True, standby=True)._voice_uses_server() is False


def test_voice_uses_local_when_no_token():
    assert _led_box(True, online=True, connected=False)._voice_uses_server() is False
