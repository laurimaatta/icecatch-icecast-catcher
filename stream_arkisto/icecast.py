"""Icecast-palvelimen status-sivun haku ja parsinta.

Ensisijaisesti käytetään /status-json.xsl -rajapintaa (Icecast 2.4+), koska se on
koneluettava. Jos sitä ei ole, parsitaan /status.xsl -HTML-sivu. HTML-parseri
tunnistaa sekä Icecast 2.4:n (h3 "Mount Point /x" + table.yellowkeys) että
2.5:n (h3.box_title + table.table-keys) ulkoasun, koska se etsii vain
"Mount Point" -otsikoita ja niiden jälkeisiä kaksisarakkeisia taulukkorivejä.
"""
from __future__ import annotations

import json
import re
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass, asdict, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

USER_AGENT = "stream-arkisto/0.1"


@dataclass
class Mount:
    server: str                      # palvelimen perus-URL, esim. https://host:8443
    mount: str                       # esim. /radio1
    url: str                         # kuunneltava osoite (server + mount)
    name: str = ""
    description: str = ""
    genre: str = ""
    content_type: str = ""
    bitrate: Optional[int] = None
    listeners: Optional[int] = None
    listener_peak: Optional[int] = None
    title: str = ""                  # "Currently playing"
    stream_start: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- apurit

def normalize_base(url: str) -> str:
    """'https://h:8443/status.xsl' -> 'https://h:8443'. Polku säilyy, jos Icecast
    on julkaistu alihakemistoon (esim. https://h/icecast/status.xsl)."""
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    p = urlsplit(url)
    path = re.sub(r"/?(status(-json)?\.xsl|admin/.*)?$", "", p.path)
    return urlunsplit((p.scheme, p.netloc, path.rstrip("/"), "", ""))


def _to_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    m = re.search(r"-?\d+", str(v))
    return int(m.group()) if m else None


def http_get(url: str, timeout: float = 10.0, insecure: bool = False) -> str:
    ctx = ssl._create_unverified_context() if insecure else None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


# ---------------------------------------------------------------- JSON

def parse_status_json(text: str, base: str) -> list[Mount]:
    # Icecast 2.4 tuottaa joskus rikkinäistä JSONia (esim. "title": - ilman arvoa)
    text = re.sub(r':\s*-\s*([,}])', r': null\1', text)
    data = json.loads(text, strict=False)
    sources = (data.get("icestats") or {}).get("source") or []
    if isinstance(sources, dict):          # yksi mount -> objekti, ei lista
        sources = [sources]
    mounts = []
    for s in sources:
        listenurl = s.get("listenurl") or ""
        # listenurl sisältää usein palvelimen sisäisen nimen/portin -> käytetään vain polkua
        mount = urlsplit(listenurl).path or s.get("mount") or ""
        if not mount:
            continue
        known = {"listenurl", "server_name", "server_description", "server_type",
                 "audio_info", "genre", "bitrate", "listeners", "listener_peak",
                 "title", "stream_start", "stream_start_iso8601", "server_url", "artist"}
        title = s.get("title") or ""
        if s.get("artist"):
            title = f"{s['artist']} - {title}" if title else str(s["artist"])
        mounts.append(Mount(
            server=base, mount=mount, url=base + mount,
            name=str(s.get("server_name") or ""),
            description=str(s.get("server_description") or ""),
            genre=str(s.get("genre") or ""),
            content_type=str(s.get("server_type") or ""),
            bitrate=_to_int(s.get("bitrate") or s.get("audio_bitrate")
                            or s.get("ice-bitrate")),
            listeners=_to_int(s.get("listeners")),
            listener_peak=_to_int(s.get("listener_peak")),
            title=str(title),
            stream_start=str(s.get("stream_start_iso8601") or s.get("stream_start") or ""),
            extra={k: v for k, v in s.items() if k not in known},
        ))
    return mounts


# ---------------------------------------------------------------- HTML

_KEYMAP = {
    "stream name": "name",
    "stream title": "name",
    "stream description": "description",
    "description": "description",
    "genre": "genre",
    "content type": "content_type",
    "bitrate": "bitrate",
    "listeners (current)": "listeners",
    "current listeners": "listeners",
    "listeners": "listeners",
    "listeners (peak)": "listener_peak",
    "peak listeners": "listener_peak",
    "currently playing": "title",
    "current song": "title",
    "stream started": "stream_start",
}


class _StatusHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.mounts: list[dict] = []
        self._cur: Optional[dict] = None
        self._in_heading = False
        self._heading = ""
        self._cell: Optional[str] = None
        self._row: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4"):
            self._in_heading, self._heading = True, ""
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = ""
        elif tag == "a" and self._cur is not None:
            href = dict(attrs).get("href") or ""
            # Icecast 2.4: "Stream URL" -solu on linkki; talletetaan varalle
            if self._cell is not None and href and not href.endswith((".m3u", ".xspf", ".vclt")):
                self._cur.setdefault("_links", []).append(href)

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4") and self._in_heading:
            self._in_heading = False
            m = re.match(r"\s*Mount\s*Point\s*:?\s*(\S+)", self._heading, re.I)
            if m:
                self._cur = {"mount": m.group(1)}
                self.mounts.append(self._cur)
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join(self._cell.split()))
            self._cell = None
        elif tag == "tr" and self._cur is not None and len(self._row) == 2:
            key = self._row[0].rstrip(":").strip().lower()
            if key == "stream url":
                self._cur["_stream_url"] = self._row[1]
            elif key in _KEYMAP:
                self._cur.setdefault(_KEYMAP[key], self._row[1])

    def handle_data(self, data):
        if self._in_heading:
            self._heading += data
        if self._cell is not None:
            self._cell += data


def parse_status_html(text: str, base: str) -> list[Mount]:
    p = _StatusHTMLParser()
    p.feed(text)
    out = []
    for d in p.mounts:
        mount = d["mount"]
        if not mount.startswith("/"):
            continue
        out.append(Mount(
            server=base, mount=mount, url=base + mount,
            name=d.get("name", ""), description=d.get("description", ""),
            genre=d.get("genre", ""), content_type=d.get("content_type", ""),
            bitrate=_to_int(d.get("bitrate")), listeners=_to_int(d.get("listeners")),
            listener_peak=_to_int(d.get("listener_peak")),
            title=d.get("title", ""), stream_start=d.get("stream_start", ""),
        ))
    return out


# ---------------------------------------------------------------- julkinen API

def fetch_mounts(url: str, timeout: float = 10.0, insecure: bool = False) -> list[Mount]:
    """Hakee palvelimen mountit. Kokeilee ensin JSONia, sitten HTML:ää."""
    base = normalize_base(url)
    errors = []
    for path, parser in (("/status-json.xsl", parse_status_json),
                         ("/status.xsl", parse_status_html)):
        try:
            text = http_get(base + path, timeout, insecure)
            mounts = parser(text, base)
            if mounts or path == "/status.xsl":
                return sorted(mounts, key=lambda m: m.mount)
        except Exception as e:      # noqa: BLE001 - kokeillaan seuraavaa
            errors.append(f"{path}: {e}")
    raise RuntimeError(f"Status-sivua ei saatu luettua ({base}): " + "; ".join(errors))


@dataclass
class ProbeResult:
    mean_db: Optional[float]
    max_db: Optional[float]
    error: str = ""

    @property
    def silent(self) -> Optional[bool]:
        return None if self.max_db is None else self.max_db < SILENCE_MAX_DB


SILENCE_MAX_DB = -50.0   # jos huipputaso jää tämän alle, striimi tulkitaan hiljaiseksi


def probe_level(url: str, seconds: int = 6, timeout: float = 25.0) -> ProbeResult:
    """Kuuntelee striimiä hetken ffmpegillä ja palauttaa äänitason (dBFS).
    Status-sivu ei kerro, lähettääkö mount pelkkää hiljaisuutta - tämä kertoo."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info",
           "-user_agent", USER_AGENT, "-t", str(seconds), "-i", url,
           "-vn", "-af", "volumedetect", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return ProbeResult(None, None, "ffmpeg puuttuu (sudo apt install ffmpeg)")
    except subprocess.TimeoutExpired:
        return ProbeResult(None, None, "aikakatkaisu")
    mean = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)", r.stderr)
    mx = re.search(r"max_volume:\s*(-?[\d.]+|-inf)", r.stderr)
    if not mx:
        last = (r.stderr.strip().splitlines() or ["tuntematon virhe"])[-1]
        return ProbeResult(None, None, last[:200])
    f = lambda m: float("-inf") if m.group(1) == "-inf" else float(m.group(1))
    return ProbeResult(f(mean) if mean else None, f(mx))
