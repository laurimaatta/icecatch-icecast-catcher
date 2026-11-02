"""SQLite-tietokanta: palvelimet, suosikit (ja myöhemmin tallenteet)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("STREAM_ARKISTO_DB",
                                 Path.home() / ".local/share/stream-arkisto/arkisto.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id          INTEGER PRIMARY KEY,
    base_url    TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL DEFAULT '',
    insecure    INTEGER NOT NULL DEFAULT 0,      -- ohita TLS-varmenteen tarkistus
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_scan   TEXT
);

-- Viimeisin havaittu tila jokaisesta mountista (käyttöliittymän listaa varten)
CREATE TABLE IF NOT EXISTS mounts_seen (
    server_id     INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    mount         TEXT NOT NULL,
    name          TEXT, description TEXT, genre TEXT, content_type TEXT,
    bitrate       INTEGER, listeners INTEGER, title TEXT,
    last_seen     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (server_id, mount)
);

CREATE TABLE IF NOT EXISTS favorites (
    id             INTEGER PRIMARY KEY,
    server_id      INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    mount          TEXT NOT NULL,
    label          TEXT NOT NULL DEFAULT '',     -- oma nimi, esim. "Oulun ry"
    record_enabled INTEGER NOT NULL DEFAULT 1,   -- tallennetaanko automaattisesti
    notes          TEXT NOT NULL DEFAULT '',
    added_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (server_id, mount)
);
"""


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    return con


# ---- palvelimet

def add_server(con, base_url: str, name: str = "", insecure: bool = False) -> int:
    con.execute("INSERT INTO servers(base_url, name, insecure) VALUES (?,?,?) "
                "ON CONFLICT(base_url) DO UPDATE SET name=excluded.name, insecure=excluded.insecure",
                (base_url, name, int(insecure)))
    con.commit()
    return con.execute("SELECT id FROM servers WHERE base_url=?", (base_url,)).fetchone()[0]


def list_servers(con):
    return con.execute("SELECT * FROM servers ORDER BY id").fetchall()


def get_server(con, ref: str):
    """ref = id tai osa URL:sta."""
    if str(ref).isdigit():
        return con.execute("SELECT * FROM servers WHERE id=?", (int(ref),)).fetchone()
    row = con.execute("SELECT * FROM servers WHERE lower(name)=lower(?)", (ref,)).fetchone()
    if row:
        return row
    rows = con.execute("SELECT * FROM servers WHERE base_url LIKE ? OR name LIKE ?",
                       (f"%{ref}%", f"%{ref}%")).fetchall()
    return rows[0] if len(rows) == 1 else None


def remove_server(con, server_id: int):
    con.execute("DELETE FROM servers WHERE id=?", (server_id,))
    con.commit()


def store_scan(con, server_id: int, mounts):
    con.executemany(
        """INSERT INTO mounts_seen(server_id, mount, name, description, genre, content_type,
                                   bitrate, listeners, title, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(server_id, mount) DO UPDATE SET
             name=excluded.name, description=excluded.description, genre=excluded.genre,
             content_type=excluded.content_type, bitrate=excluded.bitrate,
             listeners=excluded.listeners, title=excluded.title, last_seen=excluded.last_seen""",
        [(server_id, m.mount, m.name, m.description, m.genre, m.content_type,
          m.bitrate, m.listeners, m.title) for m in mounts])
    con.execute("UPDATE servers SET last_scan=datetime('now') WHERE id=?", (server_id,))
    con.commit()


# ---- suosikit

def add_favorite(con, server_id: int, mount: str, label: str = "") -> int:
    if not mount.startswith("/"):
        mount = "/" + mount
    con.execute("INSERT INTO favorites(server_id, mount, label) VALUES (?,?,?) "
                "ON CONFLICT(server_id, mount) DO UPDATE SET label=CASE WHEN excluded.label!='' "
                "THEN excluded.label ELSE favorites.label END",
                (server_id, mount, label))
    con.commit()
    return con.execute("SELECT id FROM favorites WHERE server_id=? AND mount=?",
                       (server_id, mount)).fetchone()[0]


def remove_favorite(con, fav_id: int) -> bool:
    cur = con.execute("DELETE FROM favorites WHERE id=?", (fav_id,))
    con.commit()
    return cur.rowcount > 0


def set_favorite(con, fav_id: int, **fields) -> bool:
    allowed = {"label", "record_enabled", "notes"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not fields:
        return False
    sets = ", ".join(f"{k}=?" for k in fields)
    cur = con.execute(f"UPDATE favorites SET {sets} WHERE id=?", (*fields.values(), fav_id))
    con.commit()
    return cur.rowcount > 0


def list_favorites(con):
    return con.execute(
        """SELECT f.*, s.base_url, s.base_url || f.mount AS url,
                  m.name AS stream_name, m.content_type, m.bitrate, m.last_seen
           FROM favorites f JOIN servers s ON s.id=f.server_id
           LEFT JOIN mounts_seen m ON m.server_id=f.server_id AND m.mount=f.mount
           ORDER BY s.id, f.mount""").fetchall()


def favorite_mounts(con, server_id: int) -> set[str]:
    return {r[0] for r in con.execute("SELECT mount FROM favorites WHERE server_id=?",
                                      (server_id,))}
