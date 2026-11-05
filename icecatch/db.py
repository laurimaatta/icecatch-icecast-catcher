"""SQLite database: streams, schedules and recordings.

Audio files live on disk (RECORDINGS_DIR); the database holds their metadata and
path. This keeps the database small and the files directly playable.
"""
import sqlite3
import threading
from datetime import datetime

from . import config, events

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    silence_db    REAL    NOT NULL DEFAULT -50,   -- quieter than this (mean level, dBFS) is not programme
    silence_min_s REAL    NOT NULL DEFAULT 3,     -- unused, see config.SILENCE_MIN_S
    auto_record   INTEGER NOT NULL DEFAULT 0,     -- record whenever a broadcast starts
    max_duration_min INTEGER NOT NULL DEFAULT 240, -- max length of an automatic recording
    wait_offline  INTEGER NOT NULL DEFAULT 0,     -- no new recording until the broadcast has ended
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY,
    stream_id     INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,                  -- programme name, base of the recording title
    days          TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',  -- 0 = Mon ... 6 = Sun
    start_time    TEXT NOT NULL,                  -- HH:MM local time
    duration_min  INTEGER NOT NULL,
    keep_days     INTEGER,                        -- retention, NULL = forever
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
    id              INTEGER PRIMARY KEY,
    stream_id       INTEGER REFERENCES streams(id) ON DELETE SET NULL,
    schedule_id     INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
    scheduled_start TEXT,                         -- schedule occurrence (prevents duplicates)
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL,
    planned_end     TEXT,
    ended_at        TEXT,
    raw_duration_s  REAL,                         -- before silence removal
    duration_s      REAL,                         -- final duration
    file_path       TEXT,
    file_size       INTEGER,
    status          TEXT NOT NULL,                -- recording | processing | ready | empty | failed
    error           TEXT,
    keep            INTEGER NOT NULL DEFAULT 0,   -- favourite, protected from automatic deletion
    source          TEXT NOT NULL DEFAULT 'manual', -- manual | schedule | auto
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rec_started ON recordings(started_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rec_occurrence
    ON recordings(schedule_id, scheduled_start) WHERE schedule_id IS NOT NULL;
"""

# Columns added to an existing database (CREATE TABLE does not alter an old table)
MIGRATIONS = [
    ("streams", "auto_record", "INTEGER NOT NULL DEFAULT 0"),
    ("streams", "max_duration_min", "INTEGER NOT NULL DEFAULT 240"),
    ("streams", "wait_offline", "INTEGER NOT NULL DEFAULT 0"),
    ("recordings", "source", "TEXT NOT NULL DEFAULT 'manual'"),
]

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


def _rename_legacy_db():
    """Installs from before the IceCatch rename used arkisto.db."""
    old, new = config.LEGACY_DB_PATH, config.DB_PATH
    if new.exists() or not old.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        src = old.with_name(old.name + suffix)
        if src.exists():
            src.rename(new.with_name(new.name + suffix))


def init():
    config.ensure_dirs()
    _rename_legacy_db()
    c = conn()
    c.executescript(SCHEMA)
    for table, column, ddl in MIGRATIONS:
        if column not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
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
    events.notify()
    return cur.lastrowid


def update(table, row_id, fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*fields.values(), row_id))
