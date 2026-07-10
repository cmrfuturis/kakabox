#!/usr/bin/env python3
"""Kakabox REST API — served by uvicorn in a background daemon thread.

Auth-Modell:
    Alle Endpoints erfordern einen Bearer-Token im ``Authorization``-Header.
    Der Token wird beim ersten Start in ``config.json`` als ``api_token``
    angelegt (32 zufällige urlsafe-Bytes) und ist vom Pi aus per
    ``jq -r .api_token /home/riffi/Dokumente/kakabox/device/config.json``
    abrufbar. Ohne Token gibt's 401 — sonst könnte jeder im Heim-WLAN die
    Box steuern (inkl. parental-Override), und das ist für ein Kindergerät
    inakzeptabel.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from main import Kakabox

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"

_security = HTTPBearer(auto_error=False)
_box: Kakabox | None = None


def _check_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_security),
) -> None:
    """FastAPI-Dependency: prüft Bearer-Token gegen config['api_token'].

    Konstantzeit-Vergleich via ``secrets.compare_digest``, damit kein
    Timing-Leak. Fehlt der Token in der Config → 503 (Server falsch
    konfiguriert), nicht 401, weil der Client da nichts gegen tun kann.
    """
    box = _get_box()
    expected = box.config.get("api_token")
    if not expected:
        raise HTTPException(status_code=503, detail="API token not initialised")
    if creds is None or not secrets.compare_digest(creds.credentials, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


app = FastAPI(
    title="Kakabox API",
    version="1.0.0",
    dependencies=[Depends(_check_auth)],
)


def start(box: Kakabox, host: str = "0.0.0.0", port: int = 8000) -> None:
    """Start the API server in a background thread. Call once from main."""
    global _box
    _box = box
    threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": host, "port": port, "log_level": "warning"},
        daemon=True,
    ).start()


def _get_box() -> Kakabox:
    if _box is None:
        raise HTTPException(status_code=503, detail="Player not initialised")
    return _box


def _save() -> None:
    # Über main.save_config statt direktem write_text (QS-Finding F10/A4):
    # nutzt denselben Lock + atomaren, 0600-gechmodten Schreibpfad wie die
    # Box-internen Config-Writes — sonst racen REST-API- und main-Writes auf
    # dieselbe Datei und der Token bliebe world-readable.
    from main import save_config
    save_config(_box.config)


# ── Pydantic models ────────────────────────────────────────────────────────────

class VolumeBody(BaseModel):
    volume: int = Field(..., ge=0, le=100, description="Volume level 0–100")


class TagBody(BaseModel):
    album_id: str = Field(..., description="Album ID to assign to the NFC tag")


# ── Status ─────────────────────────────────────────────────────────────────────

@app.get("/status", summary="Current playback state")
def status():
    """Returns playing/paused state, current track and album, and volume."""
    box = _get_box()
    s = box.player.get_state()
    return {
        "playing": s.playing,
        "paused": s.paused,
        "volume": s.volume,
        "current_track": asdict(s.current_track) if s.current_track else None,
        "current_album": {
            "id": s.current_album.id,
            "name": s.current_album.name,
            "track_index": s.track_index,
            "total_tracks": len(s.current_album.tracks),
        } if s.current_album else None,
    }


# ── Playback ───────────────────────────────────────────────────────────────────

@app.post("/play/{album_id}", summary="Play an album")
def play(album_id: str):
    """Start playing the album with the given ID from the first track."""
    box = _get_box()
    album = box.library.find_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail=f"Album '{album_id}' not found")
    if album_id in box.config.get("parental", {}).get("disabled_albums", []):
        raise HTTPException(status_code=403, detail="Album disabled by parental controls")
    # box.effects.reset() entfernt (QS-Finding A1): die Kakabox-Klasse hat kein
    # Attribut `effects` — AudioEffects wurde nie instanziiert. Der Aufruf ließ
    # JEDEN POST /play mit AttributeError/500 scheitern.
    box.player.play_album(album)
    return {"ok": True, "album_id": album_id}


@app.post("/pause", summary="Pause playback")
def pause():
    _get_box().player.pause()
    return {"ok": True}


@app.post("/resume", summary="Resume playback")
def resume():
    _get_box().player.resume()
    return {"ok": True}


@app.post("/stop", summary="Stop playback")
def stop():
    _get_box().player.stop()
    return {"ok": True}


@app.post("/toggle", summary="Toggle play / pause")
def toggle():
    _get_box().player.toggle_pause()
    return {"ok": True}


@app.post("/next", summary="Skip to next track")
def next_track():
    _get_box().player.next_track()
    return {"ok": True}


@app.post("/previous", summary="Go back to previous track")
def previous_track():
    _get_box().player.previous_track()
    return {"ok": True}


# ── Volume ─────────────────────────────────────────────────────────────────────

@app.post("/volume", summary="Set volume")
def set_volume(body: VolumeBody):
    """Set the playback volume (0 = silent, 100 = maximum)."""
    _get_box()._set_volume(body.volume)
    return {"ok": True, "volume": body.volume}


# ── Library ────────────────────────────────────────────────────────────────────

@app.get("/library", summary="List all albums")
def library():
    """Returns all albums in the music library (without track details)."""
    box = _get_box()
    return {
        "albums": [
            {"id": a.id, "name": a.name, "track_count": len(a.tracks)}
            for a in box.library.albums
        ]
    }


@app.get("/library/{album_id}", summary="Get album details")
def album(album_id: str):
    """Returns full album info including all tracks."""
    box = _get_box()
    a = box.library.find_album(album_id)
    if not a:
        raise HTTPException(status_code=404, detail=f"Album '{album_id}' not found")
    return asdict(a)


# ── NFC tag mappings ───────────────────────────────────────────────────────────

@app.get("/tags", summary="List all NFC tag mappings")
def get_tags():
    """Returns a dict of {uid: album_id} for all registered NFC tags."""
    return _get_box().config.get("tags", {})


@app.put("/tags/{uid}", summary="Assign NFC tag to an album")
def assign_tag(uid: str, body: TagBody):
    """Map an NFC tag UID to an album. Overwrites any existing mapping."""
    box = _get_box()
    if not box.library.find_album(body.album_id):
        raise HTTPException(status_code=404, detail=f"Album '{body.album_id}' not found")
    box.config.setdefault("tags", {})[uid] = body.album_id
    _save()
    return {"ok": True, "uid": uid, "album_id": body.album_id}


@app.delete("/tags/{uid}", summary="Remove an NFC tag mapping")
def remove_tag(uid: str):
    """Delete the mapping for the given NFC tag UID."""
    box = _get_box()
    tags = box.config.get("tags", {})
    if uid not in tags:
        raise HTTPException(status_code=404, detail=f"Tag '{uid}' not mapped")
    del tags[uid]
    _save()
    return {"ok": True}


# ── Parental controls ──────────────────────────────────────────────────────────

@app.get("/parental", summary="Get parental control settings")
def get_parental():
    """Returns the list of albums currently blocked by parental controls."""
    return _get_box().config.get("parental", {"disabled_albums": []})


@app.post("/parental/disable/{album_id}", summary="Block an album")
def disable_album(album_id: str):
    """Prevent the given album from being played (via NFC or API)."""
    box = _get_box()
    if not box.library.find_album(album_id):
        raise HTTPException(status_code=404, detail=f"Album '{album_id}' not found")
    disabled = box.config.setdefault("parental", {}).setdefault("disabled_albums", [])
    if album_id not in disabled:
        disabled.append(album_id)
        _save()
    return {"ok": True, "disabled_albums": disabled}


@app.post("/parental/enable/{album_id}", summary="Unblock an album")
def enable_album(album_id: str):
    """Remove the parental block from the given album."""
    box = _get_box()
    disabled = box.config.get("parental", {}).get("disabled_albums", [])
    if album_id in disabled:
        disabled.remove(album_id)
        _save()
    return {"ok": True, "disabled_albums": disabled}


# ──────────────────────────────────────────────────────────────────────
# Zauberwort-Modus
# Mit aktivem Modus muss der Voice-Command "bitte" enthalten — sonst
# spielt die Box den Prompt "Wie heißt das Zauberwort?" ab statt zu
# matchen. Erzieht zur Höflichkeit ohne den Spaß-Faktor zu killen.
# ──────────────────────────────────────────────────────────────────────

@app.get("/zauberwort", summary="Get zauberwort mode state")
def get_zauberwort():
    """Returns whether the magic-word mode is currently enabled."""
    return {"enabled": bool(_get_box().config.get("zauberwort_mode_enabled", False))}


@app.post("/zauberwort/enable", summary="Enable zauberwort mode")
def enable_zauberwort():
    """Voice-Commands müssen ab jetzt 'bitte' enthalten, sonst Prompt."""
    box = _get_box()
    box.config["zauberwort_mode_enabled"] = True
    _save()
    return {"ok": True, "enabled": True}


@app.post("/zauberwort/disable", summary="Disable zauberwort mode")
def disable_zauberwort():
    """Voice-Commands greifen wieder ohne 'bitte'."""
    box = _get_box()
    box.config["zauberwort_mode_enabled"] = False
    _save()
    return {"ok": True, "enabled": False}
