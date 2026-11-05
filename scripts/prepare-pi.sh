#!/usr/bin/env bash
# Prepares a Raspberry Pi to be used for IceCatch only. DESTRUCTIVE:
#   - stops and removes ALL Docker/Podman containers, images and volumes
#   - removes Docker/Podman completely
#   - finally lists other services still listening on the network
# Run on the Pi as root (deploy.sh --clean does this). Asks before removing anything,
# unless ASSUME_YES=1. Messages are in Finnish.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Aja root-oikeuksin (sudo)"; exit 1; fi

say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }

remove_engine() {   # $1 = docker | podman
    local eng=$1
    command -v "$eng" >/dev/null 2>&1 || return 0
    say "$eng: kontit"
    "$eng" ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' || true

    if [[ "${ASSUME_YES:-}" != "1" ]]; then
        read -r -p "Poistetaanko KAIKKI yllä olevat kontit, niiden levykuvat ja volyymit sekä $eng itse? [k/E] " ans
        [[ "$ans" =~ ^[kKyY]$ ]] || { echo "Ohitetaan $eng."; return 0; }
    fi

    say "$eng: pysäytetään ja poistetaan kontit"
    ids=$("$eng" ps -aq || true)
    if [[ -n "$ids" ]]; then
        "$eng" update --restart=no $ids >/dev/null 2>&1 || true
        "$eng" stop -t 10 $ids || true
        "$eng" rm -f $ids || true
    fi
    say "$eng: poistetaan levykuvat, volyymit ja verkot"
    "$eng" system prune -a -f --volumes || true

    say "$eng: poistetaan ohjelmisto"
    systemctl disable --now "$eng".service "$eng".socket containerd.service 2>/dev/null || true
    pkgs=$(dpkg -l | awk '/^ii/ {print $2}' | grep -E '^(docker|docker-ce|docker-ce-cli|docker-ce-rootless-extras|docker-buildx-plugin|docker-compose-plugin|docker-compose|docker\.io|containerd|containerd\.io|runc|podman|podman-docker|buildah|skopeo)(:.*)?$' || true)
    if [[ -n "$pkgs" ]]; then
        DEBIAN_FRONTEND=noninteractive apt-get purge -y $pkgs
    fi
    # snap install
    if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then snap remove --purge docker; fi
    rm -rf /var/lib/docker /var/lib/containerd /etc/docker /var/lib/containers 2>/dev/null || true
    rm -f /etc/apt/sources.list.d/docker*.list
}

remove_engine docker
remove_engine podman
DEBIAN_FRONTEND=noninteractive apt-get autoremove -y >/dev/null || true

say "Muut verkossa kuuntelevat palvelut (tarkista, tarvitaanko näitä)"
ss -tlnp 2>/dev/null | awk 'NR==1 || !/127\.0\.0\.53|:22 /' || true

say "Valmistelu tehty"
