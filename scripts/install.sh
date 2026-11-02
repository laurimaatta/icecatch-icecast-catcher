#!/usr/bin/env bash
# Asentaa tai päivittää stream-arkiston Pi:lle. Ajetaan Pi:llä root-oikeuksin
# lähdekoodihakemistosta (deploy.sh tekee tämän automaattisesti).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Aja root-oikeuksin (sudo)"; exit 1; fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/stream-arkisto
ENVFILE=/etc/stream-arkisto.env
USER_NAME=arkisto
TZ_WANTED="${TZ_WANTED:-Europe/Helsinki}"

say() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }

say "Asennetaan riippuvuudet (ffmpeg, python3, sqlite3)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ffmpeg python3 sqlite3 >/dev/null
ffmpeg -version | head -1
python3 --version

say "Aikavyöhyke"
cur_tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone)
if [[ "$cur_tz" != "$TZ_WANTED" ]]; then
    timedatectl set-timezone "$TZ_WANTED"
    echo "Vaihdettu $cur_tz -> $TZ_WANTED (aikataulut ovat paikallista aikaa)"
else
    echo "$cur_tz"
fi

say "Käyttäjä ja asetukset"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home /var/lib/stream-arkisto --shell /usr/sbin/nologin "$USER_NAME"
if [[ ! -f "$ENVFILE" ]]; then
    install -m 640 -o root -g "$USER_NAME" "$SRC/deploy/stream-arkisto.env" "$ENVFILE"
    echo "Luotu $ENVFILE"
else
    echo "Säilytetään olemassa oleva $ENVFILE"
fi
DATA_DIR=$(grep -E '^ARKISTO_DATA_DIR=' "$ENVFILE" | cut -d= -f2- | tr -d '"' || true)
DATA_DIR=${DATA_DIR:-/var/lib/stream-arkisto}
mkdir -p "$DATA_DIR"
chown "$USER_NAME:$USER_NAME" "$DATA_DIR"
echo "Data: $DATA_DIR"

say "Kopioidaan ohjelma -> $APP"
mkdir -p "$APP"
rm -rf "$APP/arkisto" "$APP/static"
cp -r "$SRC/arkisto" "$SRC/static" "$APP/"
cp "$SRC/README.md" "$APP/" 2>/dev/null || true
find "$APP" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$APP"

say "systemd-palvelu"
install -m 644 "$SRC/deploy/stream-arkisto.service" /etc/systemd/system/stream-arkisto.service
systemctl daemon-reload
systemctl enable stream-arkisto >/dev/null 2>&1
systemctl restart stream-arkisto
sleep 2
systemctl --no-pager --lines=5 status stream-arkisto || true

PORT=$(grep -E '^ARKISTO_PORT=' "$ENVFILE" | cut -d= -f2- | tr -d '"' || true)
PORT=${PORT:-8080}
say "Valmis!  Avaa selaimessa:"
for ip in $(hostname -I); do [[ "$ip" == *:* ]] || echo "   http://$ip:$PORT"; done
echo "   http://$(hostname).local:$PORT"
echo
echo "Lokit:  journalctl -u stream-arkisto -f"
