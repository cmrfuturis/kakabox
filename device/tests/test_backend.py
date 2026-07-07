"""Tests für network.backend.Backend mit responses-Mocking."""
import json
from pathlib import Path

import pytest
import responses

from network.backend import Backend


@pytest.fixture
def identity_path(tmp_path: Path) -> Path:
    p = tmp_path / "box_identity.json"
    p.write_text(json.dumps({
        "serial_number": "KB-TEST-001",
        "activation_code": "TEST123",
        "api_token": "test-plain-token",
        "registered_at": "connected",
    }))
    return p


@pytest.fixture
def backend(identity_path: Path) -> Backend:
    return Backend(identity_path=identity_path, base_url="https://test")


@responses.activate
def test_tag_scan_provider_paired(backend):
    responses.post(
        "https://test/api/box/tag-scan",
        json={
            "status": "paired",
            "kind": "provider",
            "kaka": {"id": 1, "name": "Eule", "contents": []},
        },
        status=200,
    )

    result = backend.tag_scan("aa:bb:cc:dd")

    assert result["status"] == "paired"
    assert result["kind"] == "provider"
    # UID wurde uppercase normalisiert
    body = json.loads(responses.calls[0].request.body)
    assert body["tag_uid"] == "AA:BB:CC:DD"


@responses.activate
def test_tag_scan_unknown_returns_dict(backend):
    responses.post(
        "https://test/api/box/tag-scan",
        json={"status": "unknown", "message": "Tag ist nicht angelernt."},
        status=404,
    )
    result = backend.tag_scan("DE:AD:BE:EF")
    assert result["status"] == "unknown"


@responses.activate
def test_tag_scan_foreign_household(backend):
    responses.post(
        "https://test/api/box/tag-scan",
        json={"status": "foreign_household"},
        status=403,
    )
    result = backend.tag_scan("FF:FF:FF:FF")
    assert result["status"] == "foreign_household"


@responses.activate
def test_tag_scan_invalid_token_clears_local_token(backend, identity_path):
    responses.post(
        "https://test/api/box/tag-scan",
        json={"error": "Ungültiger API-Token."},
        status=401,
    )
    result = backend.tag_scan("aa:bb")
    assert result is None

    # Token wurde lokal entfernt
    saved = json.loads(identity_path.read_text())
    assert saved["api_token"] is None
    assert backend.is_connected is False


@responses.activate
def test_audio_manifest_returns_parsed_response(backend):
    responses.get(
        "https://test/api/box/audio-manifest",
        json={
            "manifest": [{"content_id": 1, "title": "Lied", "file_hash": "h", "priority": "high"}],
            "total_files": 1,
            "total_size_bytes": 1000,
        },
    )
    result = backend.audio_manifest()
    assert result["total_files"] == 1
    assert result["manifest"][0]["title"] == "Lied"


@responses.activate
def test_download_audio_writes_file_atomically(backend, tmp_path):
    payload = b"\xff\xfb\x90\x44 fake mp3 content"
    responses.get(
        "https://test/api/box/audio/42/download",
        body=payload,
        status=200,
    )

    target = tmp_path / "out" / "42.mp3"
    ok = backend.download_audio(42, target)

    assert ok is True
    assert target.read_bytes() == payload
    # .part-File wurde aufgeräumt
    assert not target.with_suffix(".mp3.part").exists()


@responses.activate
def test_report_audio_cached_posts_hash(backend):
    responses.post(
        "https://test/api/box/audio/7/cached",
        json={"status": "ok"},
        status=200,
    )
    assert backend.report_audio_cached(7, "x" * 64) is True
    body = json.loads(responses.calls[0].request.body)
    assert body == {"file_hash": "x" * 64}


@responses.activate
def test_report_storage_posts_mb_values(backend):
    responses.post(
        "https://test/api/box/storage-status",
        json={"status": "ok"},
        status=200,
    )
    assert backend.report_storage(total_mb=64000, free_mb=12000) is True
    body = json.loads(responses.calls[0].request.body)
    assert body == {"total_mb": 64000, "free_mb": 12000}


@responses.activate
def test_play_session_posts_payload(backend):
    responses.post(
        "https://test/api/box/play-session",
        json={"status": "ok", "id": 42},
        status=200,
    )
    payload = {
        "client_uuid": "00000000-0000-0000-0000-000000000001",
        "content_id": 7,
        "kaka_id": 3,
        "source": "kaka",
        "started_at": "2026-05-28T10:00:00Z",
        "ended_at":   "2026-05-28T10:02:00Z",
        "duration_seconds": 120,
        "end_reason": "completed",
    }
    assert backend.play_session(payload) is True
    body = json.loads(responses.calls[0].request.body)
    assert body["content_id"] == 7
    assert body["source"] == "kaka"


@responses.activate
def test_play_session_4xx_returns_false_without_clearing_token(backend, identity_path):
    responses.post(
        "https://test/api/box/play-session",
        json={"error": "validation_failed"},
        status=422,
    )
    assert backend.play_session({"source": "kaka"}) is False
    # Token darf NICHT entfernt werden, sonst tilgt eine kaputte Box ihren
    # Login wegen einer Validation-Failure.
    saved = json.loads(identity_path.read_text())
    assert saved["api_token"] == "test-plain-token"


@responses.activate
def test_play_session_401_clears_local_token(backend, identity_path):
    responses.post(
        "https://test/api/box/play-session",
        json={"error": "unauthorized"},
        status=401,
    )
    assert backend.play_session({"source": "kaka"}) is False
    saved = json.loads(identity_path.read_text())
    assert saved["api_token"] is None


@responses.activate
def test_upload_voice_command_sends_audio_and_meta(backend, tmp_path):
    """Der Box-Upload-Vertrag: WAV als multipart 'audio' + Metadaten als
    Form-Felder, None-Werte rausgefiltert (kein literales 'None')."""
    responses.post("https://test/api/box/voice-command",
                   json={"status": "ok", "id": 5}, status=201)
    wav = tmp_path / "cmd.wav"
    wav.write_bytes(b"RIFF....WAVEdata")

    ok = backend.upload_voice_command(wav, {
        "transcript": "spiele bibi",
        "action": "play",
        "matched_name": "Bibi & Tina",
        "matched_kind": "artist",
        "matched_content_id": None,   # muss NICHT als Feld gesendet werden
        "duration_seconds": 3.1,
        "recorded_at": "2026-07-07T22:30:00",
    })

    assert ok is True
    req = responses.calls[0].request
    assert req.headers["Authorization"] == "Bearer test-plain-token"
    body = req.body  # multipart
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    assert 'name="audio"' in body
    assert "spiele bibi" in body
    assert 'name="matched_content_id"' not in body  # None gefiltert


@responses.activate
def test_upload_voice_command_without_audio_sends_meta_only(backend, tmp_path):
    responses.post("https://test/api/box/voice-command",
                   json={"status": "ok", "id": 6}, status=201)
    missing = tmp_path / "gibtsnicht.wav"

    ok = backend.upload_voice_command(missing, {"action": "no_match", "transcript": "häää"})

    assert ok is True
    body = responses.calls[0].request.body
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    assert 'name="audio"' not in body
    assert "no_match" in body


def test_upload_voice_command_offline_returns_false(tmp_path):
    from network.backend import Backend
    p = tmp_path / "id.json"
    p.write_text('{"serial_number":"S","activation_code":"C","registered_at":"pending"}')
    b = Backend(identity_path=p, base_url="https://test")   # kein api_token → offline
    assert b.upload_voice_command(tmp_path / "x.wav", {"action": "play"}) is False
