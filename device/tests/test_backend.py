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
    return Backend(identity_path=identity_path, base_url="http://test")


@responses.activate
def test_tag_scan_provider_paired(backend):
    responses.post(
        "http://test/api/box/tag-scan",
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
        "http://test/api/box/tag-scan",
        json={"status": "unknown", "message": "Tag ist nicht angelernt."},
        status=404,
    )
    result = backend.tag_scan("DE:AD:BE:EF")
    assert result["status"] == "unknown"


@responses.activate
def test_tag_scan_foreign_household(backend):
    responses.post(
        "http://test/api/box/tag-scan",
        json={"status": "foreign_household"},
        status=403,
    )
    result = backend.tag_scan("FF:FF:FF:FF")
    assert result["status"] == "foreign_household"


@responses.activate
def test_tag_scan_invalid_token_clears_local_token(backend, identity_path):
    responses.post(
        "http://test/api/box/tag-scan",
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
        "http://test/api/box/audio-manifest",
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
        "http://test/api/box/audio/42/download",
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
        "http://test/api/box/audio/7/cached",
        json={"status": "ok"},
        status=200,
    )
    assert backend.report_audio_cached(7, "x" * 64) is True
    body = json.loads(responses.calls[0].request.body)
    assert body == {"file_hash": "x" * 64}


@responses.activate
def test_report_storage_posts_mb_values(backend):
    responses.post(
        "http://test/api/box/storage-status",
        json={"status": "ok"},
        status=200,
    )
    assert backend.report_storage(total_mb=64000, free_mb=12000) is True
    body = json.loads(responses.calls[0].request.body)
    assert body == {"total_mb": 64000, "free_mb": 12000}
