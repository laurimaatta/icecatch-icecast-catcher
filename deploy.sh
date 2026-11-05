#!/usr/bin/env bash
# Installs or updates IceCatch on a remote machine (Raspberry Pi or another systemd host).
#
#   ./deploy.sh            install or update
#   ./deploy.sh --clean    DANGEROUS: first removes ALL containers, images, volumes and
#                          Docker from the target (asks for confirmation), then installs
#
# The target is the ssh alias "pi" (change: PI_HOST=other ./deploy.sh).
# The target's time zone is set to this machine's zone (change: TZ_WANTED=Europe/Helsinki).
set -euo pipefail
cd "$(dirname "$0")"

HOST="${PI_HOST:-pi}"
TZ_WANTED="${TZ_WANTED:-$(timedatectl show -p Timezone --value 2>/dev/null || true)}"
TMP=/tmp/icecatch-src

echo "→ Kopioidaan lähdekoodi koneelle $HOST"
tar czf - --exclude='./data' --exclude='__pycache__' --exclude='.git' \
    icecatch static scripts deploy README.md LICENSE \
  | ssh "$HOST" "rm -rf $TMP && mkdir -p $TMP && tar xzf - -C $TMP"

if [[ "${1:-}" == "--clean" ]]; then
    echo "→ Valmistellaan kohde (kontit ja Docker pois)"
    ssh -t "$HOST" "sudo bash $TMP/scripts/prepare-pi.sh"
fi

echo "→ Asennetaan"
ssh -t "$HOST" "sudo env TZ_WANTED='$TZ_WANTED' bash $TMP/scripts/install.sh"
