# WLAN-Onboarding (Toniebox-Style)

Setup so, dass eine frisch entpackte Kakabox **ohne Bildschirm/Tastatur** ins Heim-WLAN eingebunden werden kann. Architektur orientiert sich an Sonos/Toniebox: Box öffnet einen Hotspot, Eltern verbinden sich kurz, wählen ihr WLAN auf einer Webseite, fertig.

## Komponenten

| Datei | Zweck |
|---|---|
| `comitup.conf` | Konfiguration für [comitup](https://davesteele.github.io/comitup/) (Hotspot-Manager). SSID-Pattern `Kakabox-<nnnn>`, Callback-Hook, mDNS-Name `kakabox.local`. |
| `kakabox-comitup-callback` | Wird von comitup bei Statuswechsel HOTSPOT/CONNECTING/CONNECTED aufgerufen. Spielt Audio-Ansagen ab und startet/stoppt `kakabox.service`. |
| `branding/templates/*.html` | Captive-Portal-Seiten mit Kakabox-Branding (deutsch, lila Theme). Ersetzen die Default-Comitup-Templates. |
| `kakabox.service` | systemd-Unit für den main-Loop. Wird **nicht** beim Boot gestartet — sondern erst, wenn comitup meldet, dass das Heim-WLAN steht. |
| `kakabox-reset-watcher.py` + `kakabox-reset.service` | Watcher auf den **roten Knopf**. 10 Sekunden gehalten → alle WLAN-Profile gelöscht + Reboot → kommt im Hotspot-Modus hoch. |
| `install-wifi-onboarding.sh` | Master-Installer. Idempotent. |

## Installation

```bash
sudo bash device/setup/install-wifi-onboarding.sh
```

Was passiert:
1. Pakete installieren (`comitup`, `espeak-ng`, `python3-gpiozero`, `python3-lgpio`, `nftables`)
2. `/etc/comitup.conf` mit Kakabox-Setup ersetzen
3. Captive-Portal-Templates austauschen (Backup unter `…/templates.orig`)
4. Audio-Ansagen via `espeak-ng` generieren (`/usr/share/kakabox/prompts/*.wav`)
5. systemd-Units installieren und `kakabox-reset.service` aktivieren
6. comitup-Service neu starten (kappt vorhandene WLAN-Verbindung)

## Bedienung (Eltern-Sicht)

### Erst-Inbetriebnahme
1. Kakabox einschalten — kein WLAN bekannt → öffnet **`Kakabox-1234`** (offen)
2. Box sagt: „*Hallo, ich bin im Einrichtungsmodus …*"
3. Smartphone/Laptop verbindet sich mit `Kakabox-1234`
4. Captive-Portal öffnet automatisch (oder im Browser `http://10.41.0.1`)
5. WLAN auswählen, Passwort eingeben, *Verbinden*
6. Box sagt: „*WLAN ist verbunden, die Kakabox ist jetzt einsatzbereit*"
7. Hotspot schaltet ab, Box ist im Heim-WLAN — `kakabox.service` startet automatisch

### WLAN ändern (Reset)
1. **Roten Knopf 10 Sekunden halten** (auch im laufenden Betrieb)
2. Box rebootet → kommt im Hotspot-Modus hoch → wie Erstinbetriebnahme

### Im Heim-Netz finden
Nach erfolgreichem Onboarding ist die Box per mDNS unter `kakabox.local` erreichbar (z. B. für die FastAPI von Max auf Port 8000).

## Sicherheits-Anmerkungen

- **Hotspot ist offen** (kein Passwort) — bewusst, weil's UX kostet (Tonibox/Sonos machen's auch so). WLAN-Passwort des Heim-Netzes wird kurz unverschlüsselt übertragen, aber nur in unmittelbarer Reichweite während des Setups.
- **Reset-Watcher läuft als root** (braucht's für `nmcli connection delete` von System-Connections und `systemctl reboot`). Code ist klein (≈40 Zeilen), audit-tauglich.
- **mDNS-Service** verrät Hostname `kakabox` und Port — kein sensibles Detail.

## Diagnose

```bash
# Live-Logs aller relevanten Services
sudo journalctl -u comitup -f
sudo journalctl -u kakabox.service -f
sudo journalctl -u kakabox-reset.service -f

# Callback-Aktivität
sudo tail -f /var/log/kakabox-callback.log

# Aktuelle WLAN-Profile
nmcli connection show

# Aktueller comitup-State
sudo comitup-cli
```

## Wenn etwas schiefgeht

| Symptom | Ursache / Fix |
|---|---|
| Hotspot kommt nicht | `sudo systemctl status comitup` — wenn `failed`, Logs anschauen. Häufig: `nftables` nicht installiert. |
| Captive-Portal zeigt englische Default-Seite | Templates wurden nicht überschrieben — Pfad prüfen: `ls /usr/share/comitup/web/templates/index.html`. Re-Install: `sudo bash device/setup/install-wifi-onboarding.sh` |
| Box findet WLAN, aber `kakabox.service` startet nicht | `tail /var/log/kakabox-callback.log` — wahrscheinlich Pfad-/Rechte-Problem. Service manuell: `sudo systemctl start kakabox.service` |
| Audio-Ansagen leise/silent | espeak-ng schreibt eine Test-WAV, mit `aplay -D plughw:CARD=MAX98357A,DEV=0 /usr/share/kakabox/prompts/setup_active.wav` direkt prüfen. Wenn lautlos → MAX98357A-Verkabelung (siehe Haupt-README). |
| Reset reagiert nicht | `sudo systemctl status kakabox-reset.service` — muss `active (running)` sein. Falls Crash: `journalctl -u kakabox-reset` |

## Referenzen

- [comitup-Doku](https://davesteele.github.io/comitup/)
- [comitup-Konfigurations-Optionen](https://github.com/davesteele/comitup/blob/master/conf/comitup.conf)
- Toniebox-Architektur als Inspiration (Hotspot-Captive-Portal mit Audio-Feedback)
