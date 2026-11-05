#!/usr/bin/env bash
# Installs or updates IceCatch on a Linux machine with systemd (Raspberry Pi OS, Debian,
# Ubuntu). Run as root from the source directory:   sudo ./scripts/install.sh
# deploy.sh does the same over ssh. Time zone is changed only on request:
# TZ_WANTED=Europe/Helsinki. An install from before the IceCatch rename ("stream-arkisto")
# is migrated automatically: settings, data and service.
#
# Messages are in Finnish, like the rest of the user-facing text.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Aja root-oikeuksin: sudo $0"; exit 1; }
command -v systemctl >/dev/null 2>&1 || {
    echo "Asennusskripti tarvitsee systemd:n. Muilla koneilla käynnistä käsin: python3 -m icecatch (ks. README)"
    exit 1
}

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/icecatch
ENVFILE=/etc/icecatch.env
SERVICE=icecatch
USER_NAME=icecatch
DEFAULT_DATA=/var/lib/icecatch
TZ_WANTED="${TZ_WANTED:-}"

# Names before the IceCatch rename
OLD_SERVICE=stream-arkisto
OLD_APP=/opt/stream-arkisto
OLD_ENVFILE=/etc/stream-arkisto.env
OLD_DATA=/var/lib/stream-arkisto
OLD_USER=arkisto

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

env_get() {  # key file
    { grep -E "^$1=" "$2" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2- | tr -d '"'
}

env_set() {  # key value file; the value may contain any characters
    local key=$1 value=$2 file=$3
    if grep -qE "^$key=" "$file"; then
        K="$key" V="$value" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"] }
            index($0, k "=") == 1 { print k "=" v; next } { print }' "$file" > "$file.new"
        cat "$file.new" > "$file"   # keeps owner and permissions
        rm -f "$file.new"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

migrate_old_install() {
    [[ -f $OLD_ENVFILE || -f /etc/systemd/system/$OLD_SERVICE.service || -d $OLD_APP ]] || return 0
    say "Siirretään vanha stream-arkisto-asennus IceCatchiksi"

    if [[ -f /etc/systemd/system/$OLD_SERVICE.service ]]; then
        # Running recordings stay "recording" and continue under the new service
        systemctl disable --now "$OLD_SERVICE" >/dev/null 2>&1 || true
        echo "Vanha palvelu $OLD_SERVICE pysäytetty"
    fi

    if [[ -f $OLD_ENVFILE && ! -f $ENVFILE ]]; then
        install -m 600 -o root -g root "$SRC/deploy/icecatch.env" "$ENVFILE"
        while IFS= read -r line; do
            [[ $line =~ ^ARKISTO_([A-Z0-9_]+)=(.*)$ ]] || continue
            env_set "ICECATCH_${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "$ENVFILE"
        done < "$OLD_ENVFILE"
        echo "Asetukset siirretty: $OLD_ENVFILE -> $ENVFILE"
    fi

    # Data in the old default location moves with the rename; a custom location stays.
    if [[ -f $ENVFILE ]]; then
        local data
        data=$(env_get ICECATCH_DATA_DIR "$ENVFILE")
        if [[ -z $data || $data == "$OLD_DATA" || $data == "$DEFAULT_DATA" ]]; then
            if [[ -d $OLD_DATA && ! -e $DEFAULT_DATA ]]; then
                if [[ -f $OLD_DATA/arkisto.db ]] && command -v sqlite3 >/dev/null 2>&1; then
                    sqlite3 "$OLD_DATA/arkisto.db" ".backup '$OLD_DATA/arkisto-before-icecatch.db'"
                    echo "Tietokannan varmuuskopio: $DEFAULT_DATA/arkisto-before-icecatch.db"
                fi
                mv "$OLD_DATA" "$DEFAULT_DATA"
                echo "Data siirretty: $OLD_DATA -> $DEFAULT_DATA"
            elif [[ -d $OLD_DATA ]]; then
                echo "HUOM: sekä $OLD_DATA että $DEFAULT_DATA ovat olemassa. Vanhaa dataa ei siirretty."
            fi
            env_set ICECATCH_DATA_DIR "$DEFAULT_DATA" "$ENVFILE"
        fi
    fi

    rm -f "/etc/systemd/system/$OLD_SERVICE.service"
    rm -rf "/etc/systemd/system/$OLD_SERVICE.service.d" "$OLD_APP"
    rm -f "$OLD_ENVFILE"
    systemctl daemon-reload
    REMOVE_OLD_USER=1
}

say "Riippuvuudet (ffmpeg, python3, sqlite3)"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ffmpeg python3 sqlite3 >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q ffmpeg python3 sqlite >/dev/null || true
fi
for cmd in ffmpeg ffprobe python3; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "$cmd puuttuu: asenna se ja aja uudelleen"; exit 1; }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "Tarvitaan Python 3.9 tai uudempi"; exit 1; }
ffmpeg -version | head -1
python3 --version

REMOVE_OLD_USER=0
migrate_old_install

say "Aikavyöhyke"
cur_tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "?")
if [[ -n $TZ_WANTED && $cur_tz != "$TZ_WANTED" ]]; then
    timedatectl set-timezone "$TZ_WANTED"
    echo "Vaihdettu $cur_tz -> $TZ_WANTED (aikataulut ovat paikallista aikaa)"
else
    echo "$cur_tz (aikataulut ovat tämän vyöhykkeen aikaa)"
fi

say "Käyttäjä ja asetukset"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$DEFAULT_DATA" --shell /usr/sbin/nologin "$USER_NAME"
if [[ ! -f $ENVFILE ]]; then
    install -m 640 -o root -g "$USER_NAME" "$SRC/deploy/icecatch.env" "$ENVFILE"
    echo "Luotu $ENVFILE"
else
    echo "Säilytetään olemassa oleva $ENVFILE"
fi
chown root:"$USER_NAME" "$ENVFILE"
chmod 640 "$ENVFILE"
DATA_DIR=$(env_get ICECATCH_DATA_DIR "$ENVFILE")
DATA_DIR=${DATA_DIR:-$DEFAULT_DATA}
mkdir -p "$DATA_DIR"
chown -R "$USER_NAME:$USER_NAME" "$DATA_DIR"
echo "Data: $DATA_DIR"

say "Kopioidaan ohjelma -> $APP"
mkdir -p "$APP"
rm -rf "$APP/icecatch" "$APP/static"
cp -r "$SRC/icecatch" "$SRC/static" "$APP/"
cp "$SRC/README.md" "$SRC/LICENSE" "$APP/" 2>/dev/null || true
find "$APP" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$APP"
chmod -R u=rwX,go=rX "$APP"   # the service user must be able to read it

say "systemd-palvelu"
install -m 644 "$SRC/deploy/icecatch.service" "/etc/systemd/system/$SERVICE.service"
# ProtectSystem=strict: the data directory must be writable wherever it is
mkdir -p "/etc/systemd/system/$SERVICE.service.d"
printf '[Service]\nReadWritePaths=%s\n' "$DATA_DIR" > "/etc/systemd/system/$SERVICE.service.d/data-dir.conf"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 2
systemctl --no-pager --lines=5 status "$SERVICE" || true

if [[ $REMOVE_OLD_USER == 1 ]] && id "$OLD_USER" >/dev/null 2>&1; then
    userdel "$OLD_USER" 2>/dev/null && echo "Vanha käyttäjä $OLD_USER poistettu" || true
fi

PORT=$(env_get ICECATCH_PORT "$ENVFILE")
PORT=${PORT:-8080}
say "Valmis! Avaa selaimessa:"
for ip in $(hostname -I 2>/dev/null || true); do [[ $ip == *:* ]] || echo "   http://$ip:$PORT"; done
echo "   http://$(hostname).local:$PORT"
echo
echo "Asetukset: $ENVFILE   Lokit: journalctl -u $SERVICE -f"
