# Spotify auf der Kakabox (Spotify-Chip)

Ein bestimmter RFID-Chip (config.json → `spotify.tag_uids`) schaltet die
Spotify-Wiedergabe an statt eine Kaka zu spielen:

- **Chip aufgelegt** → Spotify spielt weiter, wo es pausiert wurde. Ist der
  Player leer (frisch gebootet, nie benutzt), startet die Default-Playlist
  aus `spotify.uri` — ohne konfigurierte URI passiert nichts außer einem
  roten LED-Blitz, bis einmal per Spotify-App Musik auf „kakabox" gestartet
  wurde.
- **Chip entfernt** → Pause.
- **Drehregler** → steuert auch die Spotify-Lautstärke (gedeckelt durch
  `max_volume`, wie bei lokaler Wiedergabe). Ruhezeiten (`quiet_hours`)
  gelten ebenfalls.

## Architektur

```
RFID-Chip → main.py (_start_spotify / _on_tag_removed)
                │  REST (localhost:3678)
                ▼
        go-librespot-Daemon  ── streamt von Spotify (Premium-Konto)
                │
                ▼
        ALSA "kakamix" (dmix, /etc/asound.conf)  ← teilt sich die
        MAX98357A-Karte mit mpv (Box-Sounds/Kakas)
```

go-librespot (https://github.com/devgianlu/go-librespot) meldet die Box als
Spotify-Connect-Gerät **„kakabox"** an. Musik aussuchen geht jederzeit über
die Spotify-App, angemeldet mit dem Box-Konto — der Chip bleibt der
An/Aus-Schalter. Zeroconf ist bewusst aus: nur das per OAuth verknüpfte
Box-Konto kann die Box steuern, kein fremdes Konto im WLAN.

**Wichtig:** Ein Spotify-Konto = ein Stream. Die Box braucht deshalb ein
eigenes Family-Mitgliedskonto (kein Spotify-Kids-Konto — die können kein
Connect). Läuft die Box auf dem Konto und jemand startet am Handy mit
demselben Konto Musik, stoppt die Box.

## Installation

```bash
sudo bash device/setup/spotify/install.sh
```

Lädt das Release-Binary (Checksum-geprüft) nach `/opt/go-librespot`,
installiert die systemd-Unit und startet den Daemon.

## Einmaliger Spotify-Login (OAuth)

Beim ersten Start wartet der Daemon auf einen Browser-Login:

```bash
journalctl -u go-librespot -n 20    # zeigt den OAuth-Link
```

Den Link **im Browser auf dem Pi** öffnen (Callback geht an
`127.0.0.1:36842`) und mit dem Box-Spotify-Konto anmelden. Danach liegen
die Credentials in `/opt/go-librespot/state.json` (Verzeichnis 700) und der
Login ist dauerhaft.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Chip → roter Blitz | `journalctl -u kakabox -n 20` — Daemon down? Kein Login? Kein Kontext + keine `spotify.uri`? |
| Daemon läuft nicht | `systemctl status go-librespot` / `journalctl -u go-librespot -n 50` |
| „Device busy" im Audio | Beide Player MÜSSEN über `kakamix` gehen (mpv: `audio/player.py`, Daemon: `config.yml`) — nie direkt `plughw` |
| Box erscheint nicht in der Spotify-App | App mit dem **Box-Konto** angemeldet? Login (OAuth) abgeschlossen? |

## Bekannte Grenzen (v1)

- Kein Offline-Betrieb: ohne Internet macht der Chip nichts (lokale Kakas
  funktionieren natürlich weiter).
- Titel-Buttons (weiter/zurück) steuern nur lokale Wiedergabe, noch nicht
  Spotify (`SpotifyController.next_track/prev_track` existieren schon).
- Voice-Assistent während Spotify: die Antwort mischt sich über die Musik
  (dmix), statt sie zu ducken.
- LED-Tanz (Audio-reaktiv) bleibt bei Spotify aus — der RMS-Abgriff hängt
  an mpv.
