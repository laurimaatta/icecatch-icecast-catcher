"""Komentorivi: palvelimien hallinta, striimilistaus ja suosikit.

Esimerkkejä:
  python3 -m stream_arkisto server add https://nettiradio.srk.fi:8443/status.xsl --name SRK
  python3 -m stream_arkisto scan SRK              # listaa mountit (★ = suosikki)
  python3 -m stream_arkisto scan 1 --probe        # mittaa myös äänitason (hiljaiset näkyvät)
  python3 -m stream_arkisto fav add 1 /oulu --label "Oulun ry"
  python3 -m stream_arkisto fav list
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from . import db
from .icecast import fetch_mounts, normalize_base, probe_level


def _server_or_die(con, ref):
    s = db.get_server(con, ref)
    if not s:
        sys.exit(f"Palvelinta '{ref}' ei löydy (tai haku osui useaan). Katso: server list")
    return s


def _trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_server(a, con):
    if a.action == "add":
        base = normalize_base(a.url)
        sid = db.add_server(con, base, a.name or "", a.insecure)
        print(f"Palvelin #{sid}: {base}")
        try:
            mounts = fetch_mounts(base, insecure=a.insecure)
            db.store_scan(con, sid, mounts)
            print(f"  {len(mounts)} mountia löytyi. Listaa: scan {sid}")
        except Exception as e:
            print(f"  Varoitus: status-sivua ei saatu luettua: {e}")
    elif a.action == "list":
        for s in db.list_servers(con):
            print(f"#{s['id']:<3} {s['name'] or '-':<15} {s['base_url']}  "
                  f"(viim. haku: {s['last_scan'] or 'ei koskaan'})")
    elif a.action == "rm":
        s = _server_or_die(con, a.url)
        db.remove_server(con, s["id"])
        print(f"Poistettu #{s['id']} {s['base_url']} (myös sen suosikit)")


def cmd_scan(a, con):
    servers = [_server_or_die(con, a.server)] if a.server else db.list_servers(con)
    results = []
    for s in servers:
        try:
            mounts = fetch_mounts(s["base_url"], insecure=bool(s["insecure"]))
        except Exception as e:
            print(f"#{s['id']} {s['base_url']}: VIRHE {e}", file=sys.stderr)
            continue
        db.store_scan(con, s["id"], mounts)
        favs = db.favorite_mounts(con, s["id"])
        if a.favorites:
            mounts = [m for m in mounts if m.mount in favs]
        if a.filter:
            q = a.filter.lower()
            mounts = [m for m in mounts if q in (m.mount + m.name + m.description).lower()]

        probes = {}
        if a.probe and mounts:
            with ThreadPoolExecutor(max_workers=a.jobs) as ex:
                for m, p in zip(mounts, ex.map(lambda m: probe_level(m.url, a.seconds), mounts)):
                    probes[m.mount] = p
        if a.hide_silent:
            mounts = [m for m in mounts if not (probes.get(m.mount) and probes[m.mount].silent)]

        if a.json:
            for m in mounts:
                d = m.to_dict()
                d["favorite"] = m.mount in favs
                if m.mount in probes:
                    p = probes[m.mount]
                    d.update(max_db=p.max_db, mean_db=p.mean_db, silent=p.silent)
                results.append(d)
            continue

        print(f"\n#{s['id']} {s['name'] or s['base_url']}  ({len(mounts)} mountia)")
        hdr = f"   {'Mount':<28} {'Nimi':<30} {'Tyyppi':<11} {'kbps':>4} {'Kuul':>4}"
        print(hdr + ("  Taso" if a.probe else ""))
        for m in mounts:
            star = "★" if m.mount in favs else " "
            ctype = (m.content_type or "").replace("audio/", "")
            line = (f" {star} {_trunc(m.mount, 28):<28} {_trunc(m.name, 30):<30} "
                    f"{_trunc(ctype, 11):<11} {m.bitrate or '':>4} "
                    f"{'' if m.listeners is None else m.listeners:>4}")
            if a.probe:
                p = probes.get(m.mount)
                if p is None or p.max_db is None:
                    line += f"  ? {p.error if p else ''}"
                else:
                    line += f"  {'HILJAINEN' if p.silent else 'ääntä'} (max {p.max_db:.0f} dB)"
            print(line)
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_fav(a, con):
    if a.action == "add":
        s = _server_or_die(con, a.server)
        for mount in a.mounts:
            fid = db.add_favorite(con, s["id"], mount, a.label or "")
            print(f"Suosikki #{fid}: {s['base_url']}{mount if mount.startswith('/') else '/' + mount}")
    elif a.action == "rm":
        for fid in a.ids:
            print(f"#{fid}: {'poistettu' if db.remove_favorite(con, fid) else 'ei löytynyt'}")
    elif a.action == "set":
        rec = None if a.record is None else int(a.record == "on")
        ok = db.set_favorite(con, a.id, label=a.label, record_enabled=rec, notes=a.notes)
        print("Päivitetty" if ok else "Ei muutoksia / ei löytynyt")
    elif a.action == "list":
        for f in db.list_favorites(con):
            rec = "REC" if f["record_enabled"] else "   "
            print(f"#{f['id']:<3} {rec} {_trunc(f['label'] or f['stream_name'] or '', 28):<28} "
                  f"{f['url']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="stream_arkisto", description="Icecast-striimiarkisto")
    p.add_argument("--db", default=str(db.DEFAULT_DB), help="tietokantatiedosto")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("server", help="palvelimien hallinta")
    pss = ps.add_subparsers(dest="action", required=True)
    x = pss.add_parser("add"); x.add_argument("url"); x.add_argument("--name")
    x.add_argument("--insecure", action="store_true", help="ohita TLS-varmenteen tarkistus")
    pss.add_parser("list")
    x = pss.add_parser("rm"); x.add_argument("url", help="id tai osa URL:sta")

    pc = sub.add_parser("scan", help="hae ja listaa palvelimen mountit")
    pc.add_argument("server", nargs="?", help="id tai osa URL:sta (oletus: kaikki)")
    pc.add_argument("--favorites", "-f", action="store_true", help="vain suosikit")
    pc.add_argument("--filter", "-q", help="hakusana")
    pc.add_argument("--probe", "-p", action="store_true", help="mittaa äänitaso ffmpegillä")
    pc.add_argument("--hide-silent", action="store_true", help="piilota hiljaiset (vaatii --probe)")
    pc.add_argument("--seconds", type=int, default=6, help="mittauksen kesto (s)")
    pc.add_argument("--jobs", "-j", type=int, default=4, help="rinnakkaisia mittauksia")
    pc.add_argument("--json", action="store_true")

    pf = sub.add_parser("fav", help="suosikit")
    pfs = pf.add_subparsers(dest="action", required=True)
    x = pfs.add_parser("add"); x.add_argument("server"); x.add_argument("mounts", nargs="+")
    x.add_argument("--label")
    x = pfs.add_parser("rm"); x.add_argument("ids", nargs="+", type=int)
    x = pfs.add_parser("set"); x.add_argument("id", type=int); x.add_argument("--label")
    x.add_argument("--record", choices=["on", "off"]); x.add_argument("--notes")
    pfs.add_parser("list")

    a = p.parse_args(argv)
    if getattr(a, "hide_silent", False):
        a.probe = True
    con = db.connect(a.db)
    {"server": cmd_server, "scan": cmd_scan, "fav": cmd_fav}[a.cmd](a, con)


if __name__ == "__main__":
    main()
