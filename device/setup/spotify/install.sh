#!/usr/bin/env bash
# Installiert den go-librespot-Daemon für die Spotify-Chip-Funktion.
# Idempotent — kann nach einem Versions-Bump einfach erneut laufen.
#
#   sudo bash device/setup/spotify/install.sh
#
# Danach (einmalig): OAuth-Link aus dem Journal im Browser öffnen und mit
# dem Box-Spotify-Konto anmelden — siehe README.md.
set -euo pipefail

VERSION="v0.7.4"
URL="https://github.com/devgianlu/go-librespot/releases/download/${VERSION}/go-librespot_linux_arm64.tar.gz"
# sha256 des entpackten Binaries aus dem ${VERSION}-Release (selbst geprüft).
SHA256="116743e7617dd60298aded446c6b61f9b8bc2981090a57ca580d77738512c134"

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/go-librespot

mkdir -p "$DEST"

if ! echo "${SHA256}  ${DEST}/go-librespot" | sha256sum -c --quiet 2>/dev/null; then
    echo "Lade go-librespot ${VERSION} …"
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    curl -sSL -o "$TMP/gl.tar.gz" "$URL"
    tar xzf "$TMP/gl.tar.gz" -C "$TMP" go-librespot
    echo "${SHA256}  $TMP/go-librespot" | sha256sum -c --quiet
    install -m 755 "$TMP/go-librespot" "$DEST/go-librespot"
else
    echo "Binary ist aktuell (${VERSION})."
fi

# Config nur beim Erststart kopieren — lokale Anpassungen nicht überbügeln.
if [ ! -f "$DEST/config.yml" ]; then
    cp "$HERE/config.yml" "$DEST/config.yml"
fi

# state.json enthält nach dem OAuth-Login die Spotify-Credentials →
# Verzeichnis gehört dem Service-User und ist für andere tabu.
chown -R riffi:riffi "$DEST"
chmod 700 "$DEST"

cp "$HERE/go-librespot.service" /etc/systemd/system/go-librespot.service
systemctl daemon-reload
systemctl enable --now go-librespot.service

echo
echo "Fertig. OAuth-Status prüfen mit:"
echo "  journalctl -u go-librespot -n 20"
