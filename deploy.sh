#!/usr/bin/env bash
# Asentaa/päivittää stream-arkiston Pi:lle omalta koneelta käsin.
#
#   ./deploy.sh            asenna tai päivitä
#   ./deploy.sh --siivoa   poista ensin kaikki kontit ja Docker Pi:ltä, sitten asenna
#
# Kohde on ssh-alias "pi" (vaihda: PI_HOST=jokumuu ./deploy.sh)
set -euo pipefail
cd "$(dirname "$0")"

HOST="${PI_HOST:-pi}"
TMP=/tmp/stream-arkisto-src

echo "→ Kopioidaan lähdekoodi koneelle $HOST"
tar czf - --exclude='./data' --exclude='__pycache__' --exclude='.git' \
    arkisto static scripts deploy README.md \
  | ssh "$HOST" "rm -rf $TMP && mkdir -p $TMP && tar xzf - -C $TMP"

if [[ "${1:-}" == "--siivoa" ]]; then
    echo "→ Valmistellaan Pi (kontit ja Docker pois)"
    ssh -t "$HOST" "sudo bash $TMP/scripts/valmistele-pi.sh"
fi

echo "→ Asennetaan"
ssh -t "$HOST" "sudo bash $TMP/scripts/install.sh"
