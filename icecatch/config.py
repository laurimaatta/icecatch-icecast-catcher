"""Settings from environment variables (systemd reads them from /etc/icecatch.env)."""
import os
from pathlib import Path


def _env(name, default):
    return os.environ.get("ICECATCH_" + name, default)


DATA_DIR = Path(_env("DATA_DIR", "./data")).resolve()
DB_PATH = Path(_env("DB", str(DATA_DIR / "icecatch.db")))
LEGACY_DB_PATH = DATA_DIR / "arkisto.db"   # database name before the IceCatch rename
RECORDINGS_DIR = DATA_DIR / "recordings"   # finished recordings
WORK_DIR = DATA_DIR / "work"               # raw segments while recording

HOST = _env("HOST", "0.0.0.0")
PORT = int(_env("PORT", "8080"))

# Optional HTTP basic auth for the web UI. Empty = no password.
AUTH_USER = _env("USER", "")
AUTH_PASS = _env("PASS", "")

FFMPEG = _env("FFMPEG", "ffmpeg")
FFPROBE = _env("FFPROBE", "ffprobe")

# Output format: mp3 (plays everywhere) or opus (smaller)
OUTPUT_FORMAT = _env("FORMAT", "mp3")
OUTPUT_BITRATE = _env("BITRATE", "160k")

# Quiet stretches longer than this (s) are cut out. Not shown in the UI.
SILENCE_MIN_S = float(_env("SILENCE_MIN_S", "30"))

# A processed recording shorter than this (s) is marked empty
EMPTY_THRESHOLD_S = float(_env("EMPTY_THRESHOLD", "30"))

# Automatic recording when a broadcast starts
AUTO_POLL_S = float(_env("AUTO_POLL_S", "15"))    # how often streams are checked
AUTO_GRACE_S = float(_env("AUTO_GRACE_S", "180"))  # an outage this long ends the broadcast
FAILED_KEEP_DAYS = 7  # failed recordings stay visible in the status view this long
AUTO_DISCARD_S = 5  # an automatic recording with less audio than this is discarded
DEFAULT_MAX_DURATION_MIN = int(_env("MAX_DURATION_MIN", "240"))  # max length for new streams

# Disk space: this much is always left free. If space runs out, recording is skipped or
# stopped. Recordings are never deleted to make room.
MIN_FREE_MB = int(_env("MIN_FREE_MB", "1024"))
ASSUMED_KBPS = 320  # for space estimates when a stream does not report its bitrate

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def ensure_dirs():
    for d in (DATA_DIR, RECORDINGS_DIR, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)
