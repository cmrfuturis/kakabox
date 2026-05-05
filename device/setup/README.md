# WLAN-Onboarding (Toniebox-Style)

Setup so, dass eine frisch entpackte Kakabox **ohne Bildschirm/Tastatur** ins Heim-WLAN eingebunden werden kann. Architektur orientiert sich an Sonos/Toniebox: Box öffnet einen Hotspot, Eltern verbinden sich kurz, wählen ihr WLAN auf einer Webseite, fertig.

## Komponenten

| Datei | Zweck |
|---|---|
| `comitup.conf` | Konfiguration für [comitup](https://davesteele.github.io/comitup/) (Hotspot-Manager). SSID-Pattern `Kakabox-<nnnn>`, Callback-Hook, mDNS-Name `kakabox.local`. |
| `kakabox-comitup-callback` | Wird von comitup bei Statuswechsel HOTSPOT/CONNECTING/CONNECTED aufgerufen. Spielt nur die passenden Audio-Ansagen ab — der `kakabox.service` läuft unabhängig davon. |
| `branding/templates/*.html` | Captive-Portal-Seiten mit Kakabox-Branding (deutsch, lila Theme). Ersetzen die Default-Comitup-Templates. |
| `kakabox.service` | systemd-Unit für den main-Loop. Startet beim Boot (enabled) — Box muss auch ohne WLAN mit gecachten Kakas spielbar sein. |
| `kakabox-wifi-nuke` + `sudoers-kakabox` | Helper-Script (Root-only) das alle WLAN-Profile löscht und rebootet. main.py ruft es via NOPASSWD-sudo, wenn der **rote Knopf** ≥ 10 s gehalten wird. Sudoers ist eng eingegrenzt auf genau diesen Pfad ohne Argumente. |
| `install-wifi-onboarding.sh` | Master-Installer. Idempotent. |

## Installation

```bash
sudo bash device/setup/install-wifi-onboarding.sh
```

Was passiert:
1. Pakete installieren (`comitup`, `espeak-ng`, `python3-gpiozero`, `python3-lgpio`, `nftables`)
2. `/etc/comitup.conf` mit Kakabox-Setup ersetzen
3. Captive-Portal-Templates austauschen (Backup unter `…/templates.orig`)
4. Audio-Ansagen via `espeak-ng` generieren (`/usr/share/kakabox/prompts/*.wav`, deutsch + englisches "Ready to rumble!")
5. `kakabox.service` installieren und `enable`-en (startet beim nächsten Boot)
6. comitup-Service neu starten (kappt vorhandene WLAN-Verbindung)

## Bedienung (Eltern-Sicht)

### Erst-Inbetriebnahme
1. Kakabox einschalten — `kakabox.service` startet sofort, Box sagt **„Ready to rumble!"** und ist mit gecachten Kakas spielbar
2. Parallel: kein WLAN bekannt → comitup öffnet **`Kakabox-1234`** (offen) und sagt: „*Hallo, ich bin im Einrichtungsmodus …*"
3. Smartphone/Laptop verbindet sich mit `Kakabox-1234`
4. Captive-Portal öffnet automatisch (oder im Browser `http://10.41.0.1`)
5. WLAN auswählen, Passwort eingeben, *Verbinden*
6. Box sagt: „*WLAN ist verbunden, die Kakabox ist jetzt einsatzbereit*"
7. Hotspot schaltet ab, Box ist im Heim-WLAN — der bereits laufende `kakabox.service` kann jetzt mit dem Backend reden (Heartbeat / Audio-Sync). Aktuell muss der Service nach dem Online-Gehen einmal neugestartet werden, damit die Backend-Connection erneut versucht wird; siehe „Wenn etwas schiefgeht".

### WLAN ändern (Reset)
1. **Roten Knopf 10 Sekunden halten** (auch im laufenden Betrieb)
2. Box rebootet → kommt im Hotspot-Modus hoch → wie Erstinbetriebnahme

### Im Heim-Netz finden
Nach erfolgreichem Onboarding ist die Box per mDNS unter `kakabox.local` erreichbar. Die lokale REST-API hängt an Port `8001` (Auth nötig — siehe unten).

## Sicherheitsmodell

Die Box ist ein Kindergerät auf einem normalen Heim-WLAN. Wir nehmen an, dass dort sowohl wohlmeinende (Eltern-Phones) als auch potenziell feindselige Geräte (Gäste-Phones, IoT-Krempel, kompromittierte Drucker) hängen. Das Sicherheitsmodell deckt vier Schichten ab.

### 1. REST-API (`http://kakabox.local:8001`) — Bearer-Token

Jeder Endpoint verlangt einen `Authorization: Bearer <token>`-Header. Ohne Token → **401**. Der Token wird beim ersten Service-Start automatisch in `device/config.json` als `api_token` angelegt (32 zufällige urlsafe-Bytes, ≈256 bit).

**Token abrufen** (auf der Box per SSH oder lokal):
```bash
python3 -c "import json; print(json.load(open('/home/riffi/Dokumente/kakabox/device/config.json'))['api_token'])"
```

**Beispiel-Aufruf**:
```bash
TOKEN=…
curl -H "Authorization: Bearer $TOKEN" http://kakabox.local:8001/status
```

**Token rotieren** (z. B. wenn er versehentlich geleakt wurde): den `api_token`-Schlüssel aus `config.json` entfernen und Service neustarten — beim nächsten Start wird ein frischer generiert.

Threat addressed: Ohne Auth könnte jeder im selben WLAN parental-Kontrollen umkehren (`POST /parental/enable/...`), Lautstärke auf 100 setzen oder beliebige Inhalte abspielen.

### 2. Backend-Verbindung — HTTPS-Pflicht

Die Box weigert sich, mit einem unverschlüsselten Backend zu sprechen, außer es läuft auf `localhost`/`127.0.0.1` (lokales Dev-Setup auf dem Pi selbst). `KAKABOX_BACKEND` muss in Produktion auf eine `https://`-URL zeigen — alles andere wirft `BackendError` und die Box läuft offline-only weiter.

Damit liegt der Backend-Token nicht im Klartext im Heim-WLAN, und die SHA-256-Verifikation der gecacheten MP3s wird zu echtem Defense-in-Depth (sonst kann ein MITM Manifest + Datei kohärent austauschen).

**Konfiguration in der systemd-Unit**:
```ini
Environment=KAKABOX_BACKEND=https://kakaland.de
```

### 3. systemd-Hardening

`kakabox.service` läuft mit:

| Option | Wirkung |
|---|---|
| `User/Group=riffi` | Kein root |
| `ProtectSystem=full` | `/usr`, `/boot`, `/etc` read-only |
| `ProtectHome=read-only` | Andere Home-Verzeichnisse nicht zugreifbar |
| `ReadWritePaths=/home/riffi/Dokumente/kakabox/device` | Einziger r/w-Pfad |
| `PrivateTmp=true` | Eigener `/tmp`-Namespace |
| `ProtectControlGroups=true` | cgroups read-only |
| `ProtectHostname=true` / `ProtectClock=true` | Hostname + Systemzeit eingefroren |
| `ProtectKernelLogs=true` | `/dev/kmsg` gesperrt (keine Spurenverwischung) |
| `LockPersonality=true` | `personality(2)` eingefroren |
| `RestrictNamespaces=true` | Keine neuen Namespaces (kein Container-Escape) |
| `RestrictRealtime=true` | Kein RT-Scheduling-Missbrauch |
| `UMask=0077` | Neue Dateien nur für `riffi` lesbar |

`systemd-analyze security kakabox.service` → ≈ **7.0 / 10 (MEDIUM)**, runter von ≈ 9 (UNSAFE) ohne Hardening.

**Bewusst NICHT gesetzt**: `NoNewPrivileges=true` würde den `sudo`-Aufruf zu `/usr/local/bin/kakabox-wifi-nuke` (rote-Knopf-Reset) brechen. Der sudoers-Drop-in ist eng auf genau diesen einen Pfad ohne Argumente eingegrenzt; das Restrisiko ist tragbar.

### 4. Sensible Dateien

| Pfad | Modus | Inhalt |
|---|---|---|
| `device/config.json` | `0600` | API-Token, Lautstärke, Tag-Mappings, Parental-Liste |
| `device/box_identity.json` | `0600` | Backend-Token, Seriennummer, Activation-Code |

Beide sind in `.gitignore` und werden niemals committed.

### Akzeptierte Restrisiken

- **Hotspot beim Onboarding ist offen** (kein Passwort). Während der ≈30-Sekunden-Phase, in der Eltern ihr Heim-WLAN-Passwort ins Captive-Portal eintippen, läuft das Passwort über plain-WLAN. Tonibox/Sonos handhaben's identisch — das Risiko ist begrenzt auf Funkreichweite und das Setup-Zeitfenster. Zukünftige Verbesserung: WPA2 + auf der Box-Unterseite gedrucktes PSK.
- **mDNS-Bekanntmachung** — `kakabox.local` ist im LAN sichtbar. Kein Geheimnis, sondern Feature.
- **Kein Reconnect bei Backend-Verlust zur Laufzeit** — fällt das Backend nach dem Service-Start aus oder kommt erst danach hoch, läuft die Box bis zum nächsten Service-Restart offline. Die User-seitige Lösung steht in „Wenn etwas schiefgeht".

## Diagnose

```bash
# Live-Logs aller relevanten Services
sudo journalctl -u comitup -f
sudo journalctl -u kakabox.service -f

# Callback-Aktivität
sudo tail -f /var/log/kakabox-callback.log

# Aktuelle WLAN-Profile
nmcli connection show

# Aktueller comitup-State
sudo comitup-cli

# systemd-Hardening-Score überprüfen
systemd-analyze security kakabox.service
```

## Wenn etwas schiefgeht

| Symptom | Ursache / Fix |
|---|---|
| Hotspot kommt nicht | `sudo systemctl status comitup` — wenn `failed`, Logs anschauen. Häufig: `nftables` nicht installiert. |
| Captive-Portal zeigt englische Default-Seite | Templates wurden nicht überschrieben — Pfad prüfen: `ls /usr/share/comitup/web/templates/index.html`. Re-Install: `sudo bash device/setup/install-wifi-onboarding.sh` |
| Box findet WLAN, aber `kakabox.service` ist nicht backend-connected | Beim Boot war noch kein WLAN da, der Service hat keinen Backend-Token. Lösung: `sudo systemctl restart kakabox.service` — danach geht der Connect erneut los. |
| Audio-Ansagen leise/silent | espeak-ng schreibt eine Test-WAV, mit `aplay -D plughw:CARD=MAX98357A,DEV=0 /usr/share/kakabox/prompts/setup_active.wav` direkt prüfen. Wenn lautlos → MAX98357A-Verkabelung (siehe Haupt-README). |
| API-Calls liefern 401 | Token aus `device/config.json` holen (siehe „Sicherheitsmodell → REST-API"). Header: `Authorization: Bearer <token>`. |
| API-Calls liefern 503 „API token not initialised" | Erster Service-Start hat config.json nicht schreiben können (Permissions). `ls -la device/config.json` prüfen — sollte `riffi:riffi 0600` sein. |
| Backend-Connect schlägt mit `BackendError: Refusing insecure backend URL` fehl | `KAKABOX_BACKEND` zeigt auf plain-HTTP außerhalb von localhost. Auf `https://…` umstellen oder für lokales Dev `http://localhost:8000`. |
| Roter Knopf 10s lässt sich nicht halten | `journalctl -u kakabox.service` nach „WLAN-Reset wird ausgelöst". Wenn nichts: `cat /etc/sudoers.d/kakabox` muss riffi NOPASSWD für den Wifi-Nuke geben. |

## Referenzen

- [comitup-Doku](https://davesteele.github.io/comitup/)
- [comitup-Konfigurations-Optionen](https://github.com/davesteele/comitup/blob/master/conf/comitup.conf)
- Toniebox-Architektur als Inspiration (Hotspot-Captive-Portal mit Audio-Feedback)
