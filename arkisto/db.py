"""SQLite-tietokanta: striimit, aikataulut ja tallenteet.

Äänitiedostot ovat levyllä (RECORDINGS_DIR), tietokannassa niiden metatiedot
ja polku. Näin kanta pysyy pienenä ja tiedostot ovat suoraan kuunneltavissa.
"""
import sqlite3
import threading
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    silence_db    REAL    NOT NULL DEFAULT -50,   -- hiljaisuuden kynnys dBFS
    silence_min_s REAL    NOT NULL DEFAULT 3,     -- tätä pidemmät hiljaisuudet pois (0 = ei poistoa)
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY,
    stream_id     INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,                  -- ohjelman nimi, tallenteen otsikon pohja
    days          TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',  -- 0 = ma ... 6 = su
    start_time    TEXT NOT NULL,                  -- HH:MM paikallista aikaa
    duration_min  INTEGER NOT NULL,
    keep_days     INTEGER,                        -- säilytysaika, NULL = ikuisesti
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
    id              INTEGER PRIMARY KEY,
    stream_id       INTEGER REFERENCES streams(id) ON DELETE SET NULL,
    schedule_id     INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
    scheduled_start TEXT,                         -- aikataulun esiintymä (estää tuplat)
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL,
    planned_end     TEXT,
    ended_at        TEXT,
    raw_duration_s  REAL,                         -- ennen hiljaisuuden poistoa
    duration_s      REAL,                         -- lopullinen kesto
    file_path       TEXT,
    file_size       INTEGER,
    status          TEXT NOT NULL,                -- recording | processing | ready | empty | failed
    error           TEXT,
    keep            INTEGER NOT NULL DEFAULT 0,   -- suojattu automaattiselta poistolta
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rec_started ON recordings(started_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rec_occurrence
    ON recordings(schedule_id, scheduled_start) WHERE schedule_id IS NOT NULL;
"""

_local = threading.local()


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(str(config.DB_PATH), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA journal_mode = WAL")
        _local.conn = c
    return c


def init():
    config.ensure_dirs()
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def query(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def execute(sql, args=()):
    c = conn()
    cur = c.execute(sql, args)
    c.commit()
    return cur.lastrowid


def update(table, row_id, fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*fields.values(), row_id))
