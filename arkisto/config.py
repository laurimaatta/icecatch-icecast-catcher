"""Asetukset ympäristömuuttujista (systemd lukee ne tiedostosta /etc/stream-arkisto.env)."""
import os
from pathlib import Path


def _env(name, default):
    return os.environ.get(name, default)


DATA_DIR = Path(_env("ARKISTO_DATA_DIR", "./data")).resolve()
DB_PATH = Path(_env("ARKISTO_DB", str(DATA_DIR / "arkisto.db")))
RECORDINGS_DIR = DATA_DIR / "recordings"   # valmiit tallenteet
WORK_DIR = DATA_DIR / "work"               # raakasegmentit tallennuksen aikana

HOST = _env("ARKISTO_HOST", "0.0.0.0")
PORT = int(_env("ARKISTO_PORT", "8080"))

# Valinnainen perustunnistautuminen web-liittymään. Tyhjä = ei salasanaa.
AUTH_USER = _env("ARKISTO_USER", "")
AUTH_PASS = _env("ARKISTO_PASS", "")

FFMPEG = _env("ARKISTO_FFMPEG", "ffmpeg")
FFPROBE = _env("ARKISTO_FFPROBE", "ffprobe")

# Lopullisen tiedoston muoto: mp3 (toimii kaikkialla) tai opus (pienempi)
OUTPUT_FORMAT = _env("ARKISTO_FORMAT", "mp3")
OUTPUT_BITRATE = _env("ARKISTO_BITRATE", "160k")

# Jos käsitelty tallenne on tätä lyhyempi (s), se merkitään tyhjäksi
EMPTY_THRESHOLD_S = float(_env("ARKISTO_EMPTY_THRESHOLD", "30"))

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def ensure_dirs():
    for d in (DATA_DIR, RECORDINGS_DIR, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)
