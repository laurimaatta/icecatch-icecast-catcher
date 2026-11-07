"""HTTP server: JSON API, web UI, audio playback (Range support) and live change events.

Built on the Python standard library only, so no Python packages need to be installed.
API error messages are shown in the UI, so they are in Finnish.
"""
import base64
import hmac
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import NAME, __version__, config, db, events
from .recorder import NoSpace, delete_recording, resolve_url, slugify

log = logging.getLogger("icecatch.http")

ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- validation

def _str(body, key, required=False, default=None, maxlen=2000):
    v = body.get(key, default)
    if v is None:
        if required:
            raise ApiError(400, f"Kenttä '{key}' puuttuu")
        return None
    v = str(v).strip()
    if required and not v:
        raise ApiError(400, f"Kenttä '{key}' ei voi olla tyhjä")
    return v[:maxlen]


def _num(body, key, default=None, lo=None, hi=None, cast=float, allow_none=False):
    v = body.get(key, default)
    if v is None or v == "":
        if allow_none:
            return None
        if default is None:
            raise ApiError(400, f"Kenttä '{key}' puuttuu")
        v = default
    try:
        v = cast(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"Kenttä '{key}' ei ole numero")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ApiError(400, f"Kenttä '{key}' on sallitun välin ulkopuolella ({lo}–{hi})")
    return v


def _bool(body, key, default=True):
    v = body.get(key, default)
    return 1 if v in (True, 1, "1", "true", "on") else 0


def _url(body):
    u = _str(body, "url", required=True)
    if not re.match(r"^https?://", u):
        raise ApiError(400, "Osoitteen pitää alkaa http:// tai https://")
    return u


def _days(body):
    d = body.get("days", "0,1,2,3,4,5,6")
    if isinstance(d, list):
        d = ",".join(str(x) for x in d)
    try:
        days = sorted({int(x) for x in str(d).split(",") if x.strip() != ""})
    except ValueError:
        raise ApiError(400, "Virheellinen viikonpäivä")
    if not days or any(x < 0 or x > 6 for x in days):
        raise ApiError(400, "Valitse vähintään yksi viikonpäivä")
    return ",".join(map(str, days))


def _time(body):
    t = _str(body, "start_time", required=True)
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ApiError(400, "Aloitusaika muodossa HH:MM")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _qint(q, key, default=None):
    try:
        return int(q.get(key, default))
    except (TypeError, ValueError):
        raise ApiError(400, f"Parametri '{key}' ei ole numero")


def _get(table, row_id):
    r = db.one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
    if not r:
        raise ApiError(404, "Ei löydy")
    return r


# ---------------------------------------------------------------- status

@route("GET", r"/api/status")
def status(h, q, body):
    du = shutil.disk_usage(config.DATA_DIR)
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM recordings GROUP BY status")}
    total = db.one("SELECT COALESCE(SUM(file_size),0) s, COALESCE(SUM(duration_s),0) d FROM recordings")
    mgr = h.server.manager
    active = []
    for rid, info in mgr.active().items():
        r = db.one("SELECT r.id, r.title, r.started_at, s.name stream_name FROM recordings r "
                   "LEFT JOIN streams s ON s.id = r.stream_id WHERE r.id = ?", (rid,))
        if r:
            active.append({**r, **info})
    return {
        "name": NAME,
        "version": __version__,
        "now": db.now_iso(),
        "active": active,
        "processing": mgr.processing_now,
        "queue": mgr.proc_queue.qsize(),
        "disk": {"total": du.total, "used": du.used, "free": du.free, "path": str(config.DATA_DIR),
                 "low": du.free < config.MIN_FREE_MB * 1024 * 1024},
        "auto": [{**s, "on_air": mgr.on_air.get(s["id"])} for s in db.query(
            "SELECT id, name, max_duration_min, wait_offline FROM streams "
            "WHERE auto_record = 1 AND enabled = 1 ORDER BY name")],
        "counts": counts,
        "archive_bytes": total["s"],
        "archive_seconds": total["d"],
        "upcoming": upcoming(8),
    }


def upcoming(limit):
    """Next scheduled recordings within the coming week."""
    now = datetime.now()
    out = []
    for sc in db.query("SELECT sc.*, st.name stream_name FROM schedules sc JOIN streams st ON st.id = sc.stream_id "
                       "WHERE sc.enabled = 1 AND st.enabled = 1"):
        days = {int(d) for d in sc["days"].split(",") if d}
        hh, mm = (int(x) for x in sc["start_time"].split(":"))
        for off in range(0, 8):
            d = (now + timedelta(days=off)).date()
            if d.weekday() not in days:
                continue
            start = datetime(d.year, d.month, d.day, hh, mm)
            if start > now:
                out.append({"schedule_id": sc["id"], "title": sc["title"], "stream_name": sc["stream_name"],
                            "start": start.isoformat(), "duration_min": sc["duration_min"]})
                break
    out.sort(key=lambda x: x["start"])
    return out[:limit]


# ---------------------------------------------------------------- streams

@route("GET", r"/api/streams")
def streams_list(h, q, body):
    rows = db.query("SELECT s.*, (SELECT COUNT(*) FROM recordings r WHERE r.stream_id = s.id AND r.status = 'ready') AS recording_count "
                    "FROM streams s ORDER BY s.name")
    on_air = h.server.manager.on_air
    recording = h.server.manager.recording_stream_ids()
    return [{**r, "on_air": on_air.get(r["id"]), "recording": r["id"] in recording} for r in rows]


def _stream_fields(body):
    return {
        "name": _str(body, "name", required=True, maxlen=200),
        "url": _url(body),
        "enabled": _bool(body, "enabled"),
        "silence_db": _num(body, "silence_db", -50, -90, -10),
        "auto_record": _bool(body, "auto_record", default=False),
        "max_duration_min": _num(body, "max_duration_min", config.DEFAULT_MAX_DURATION_MIN, 1, 7 * 24 * 60, cast=int),
    }


@route("POST", r"/api/streams")
def streams_create(h, q, body):
    f = _stream_fields(body)
    f["created_at"] = db.now_iso()
    rid = db.execute(f"INSERT INTO streams ({','.join(f)}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
    return _get("streams", rid)


@route("PUT", r"/api/streams/(\d+)")
def streams_update(h, q, body, sid):
    _get("streams", sid)
    db.update("streams", sid, _stream_fields(body))
    return _get("streams", sid)


@route("DELETE", r"/api/streams/(\d+)")
def streams_delete(h, q, body, sid):
    _get("streams", sid)
    db.execute("DELETE FROM streams WHERE id = ?", (sid,))
    return {"ok": True}


@route("POST", r"/api/streams/(\d+)/test")
def streams_test(h, q, body, sid):
    s = _get("streams", sid)
    return probe_url(s["url"])


@route("POST", r"/api/streams/test")
def streams_test_url(h, q, body):
    return probe_url(_url(body))


def probe_url(url):
    from .recorder import resolve_url
    real = resolve_url(url)
    try:
        res = subprocess.run(
            [config.FFPROBE, "-v", "error", "-rw_timeout", "10000000", "-show_entries",
             "stream=codec_name,bit_rate,sample_rate,channels:format_tags", "-of", "json", real],
            capture_output=True, text=True, errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Aikakatkaisu: striimi ei vastannut 20 sekunnissa"}
    if res.returncode != 0:
        return {"ok": False, "error": (res.stderr or "ffprobe epäonnistui").strip()[-300:]}
    data = json.loads(res.stdout or "{}")
    st = next((x for x in data.get("streams", []) if x.get("codec_name")), {})
    tags = data.get("format", {}).get("tags", {})
    return {"ok": True, "resolved_url": real, "codec": st.get("codec_name"),
            "bit_rate": st.get("bit_rate") or tags.get("icy-br"),
            "sample_rate": st.get("sample_rate"), "channels": st.get("channels"),
            "station": tags.get("icy-name"), "genre": tags.get("icy-genre"),
            "now_playing": tags.get("StreamTitle")}


@route("POST", r"/api/streams/(\d+)/record")
def streams_record(h, q, body, sid):
    s = _get("streams", sid)
    minutes = _num(body, "duration_min", 60, 1, 24 * 60)
    now = datetime.now()
    title = _str(body, "title") or f"{s['name']} {now:%d.%m.%Y %H:%M}"
    try:
        rid = h.server.manager.start_recording(s, now + timedelta(minutes=minutes), title)
    except NoSpace as e:
        raise ApiError(507, str(e))
    return _get("recordings", rid)


# ---------------------------------------------------------------- schedules

@route("GET", r"/api/schedules")
def schedules_list(h, q, body):
    return db.query("SELECT sc.*, st.name stream_name FROM schedules sc JOIN streams st ON st.id = sc.stream_id "
                    "ORDER BY sc.start_time, sc.title")


def _schedule_fields(body):
    sid = _num(body, "stream_id", cast=int)
    _get("streams", sid)
    return {
        "stream_id": sid,
        "title": _str(body, "title", required=True, maxlen=200),
        "days": _days(body),
        "start_time": _time(body),
        "duration_min": _num(body, "duration_min", 60, 1, 24 * 60, cast=int),
        "keep_days": _num(body, "keep_days", None, 1, 36500, cast=int, allow_none=True),
        "enabled": _bool(body, "enabled"),
    }


@route("POST", r"/api/schedules")
def schedules_create(h, q, body):
    f = _schedule_fields(body)
    f["created_at"] = db.now_iso()
    rid = db.execute(f"INSERT INTO schedules ({','.join(f)}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
    return _get("schedules", rid)


@route("PUT", r"/api/schedules/(\d+)")
def schedules_update(h, q, body, sid):
    _get("schedules", sid)
    db.update("schedules", sid, _schedule_fields(body))
    return _get("schedules", sid)


@route("DELETE", r"/api/schedules/(\d+)")
def schedules_delete(h, q, body, sid):
    _get("schedules", sid)
    db.execute("DELETE FROM schedules WHERE id = ?", (sid,))
    return {"ok": True}


# ---------------------------------------------------------------- recordings

RECORDING_SORTS = {
    "newest": "r.started_at DESC",
    "oldest": "r.started_at ASC",
    "title": "r.title COLLATE NOCASE ASC, r.started_at DESC",
    "stream": "s.name IS NULL, s.name COLLATE NOCASE ASC, r.started_at DESC",
    "longest": "r.duration_s IS NULL, r.duration_s DESC, r.started_at DESC",
    "shortest": "r.duration_s IS NULL, r.duration_s ASC, r.started_at DESC",
    "largest": "r.file_size IS NULL, r.file_size DESC, r.started_at DESC",
    "smallest": "r.file_size IS NULL, r.file_size ASC, r.started_at DESC",
}


@route("GET", r"/api/recordings")
def recordings_list(h, q, body):
    where, args = [], []
    if q.get("q"):
        where.append("(r.title LIKE ? OR r.description LIKE ? OR r.tags LIKE ?)")
        args += [f"%{q['q']}%"] * 3
    if q.get("stream_id"):
        where.append("r.stream_id = ?")
        args.append(_qint(q, "stream_id"))
    if q.get("status"):
        where.append("r.status = ?")
        args.append(q["status"])
    if q.get("source"):
        where.append("r.source = ?")
        args.append(q["source"])
    if q.get("keep"):
        where.append("r.keep = ?")
        args.append(1 if _qint(q, "keep") else 0)
    if q.get("from"):
        where.append("r.started_at >= ?")
        args.append(q["from"])
    if q.get("to"):
        where.append("r.started_at < ?")
        args.append(q["to"] + "T99")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    order = RECORDING_SORTS.get(q.get("sort", "newest"))
    if not order:
        raise ApiError(400, "Tuntematon järjestys")
    limit = min(_qint(q, "limit", 100), 500)
    offset = _qint(q, "offset", 0)
    total = db.one(f"SELECT COUNT(*) n FROM recordings r {sql_where}", args)["n"]
    rows = db.query(
        f"SELECT r.*, s.name stream_name FROM recordings r LEFT JOIN streams s ON s.id = r.stream_id "
        f"{sql_where} ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset])
    return {"total": total, "items": rows}


@route("GET", r"/api/recordings/(\d+)")
def recordings_get(h, q, body, rid):
    return db.one("SELECT r.*, s.name stream_name FROM recordings r LEFT JOIN streams s ON s.id = r.stream_id "
                  "WHERE r.id = ?", (rid,)) or _get("recordings", rid)


@route("PUT", r"/api/recordings/(\d+)")
def recordings_update(h, q, body, rid):
    _get("recordings", rid)
    f = {}
    if "title" in body:
        f["title"] = _str(body, "title", required=True, maxlen=300)
    if "description" in body:
        f["description"] = _str(body, "description", default="", maxlen=20000) or ""
    if "tags" in body:
        tags = body["tags"]
        if isinstance(tags, list):
            tags = ",".join(tags)
        f["tags"] = ",".join(t.strip() for t in str(tags).split(",") if t.strip())[:1000]
    if "keep" in body:
        f["keep"] = _bool(body, "keep")
    db.update("recordings", rid, f)
    return recordings_get(h, q, body, rid)


def _delete_blocker(h, r):
    """Why the recording cannot be deleted right now, or None."""
    if r["status"] == "recording":
        return "Tallennus on käynnissä – pysäytä se ensin"
    if h.server.manager.processing_now == r["id"]:
        return "Tallennetta käsitellään juuri nyt – yritä hetken päästä"
    return None


def _delete(h, r):
    if r["schedule_id"] and r["scheduled_start"]:
        h.server.manager.skip_occurrence(r["schedule_id"], r["scheduled_start"])
    delete_recording(r["id"])


@route("DELETE", r"/api/recordings/(\d+)")
def recordings_delete(h, q, body, rid):
    r = _get("recordings", rid)
    blocker = _delete_blocker(h, r)
    if blocker:
        raise ApiError(409, blocker)
    _delete(h, r)
    return {"ok": True}


@route("POST", r"/api/recordings/delete")
def recordings_delete_many(h, q, body):
    """Bulk delete. Favourites (★) are skipped because they are marked to be kept."""
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 1000 \
            or not all(type(i) is int for i in ids):
        raise ApiError(400, "Kentän 'ids' pitää olla lista tallenteiden numeroita (enintään 1000)")
    deleted, skipped = [], []
    for rid in dict.fromkeys(ids):
        r = db.one("SELECT * FROM recordings WHERE id = ?", (rid,))
        reason = "Ei löydy" if not r else "Merkitty säilytettäväksi" if r["keep"] else _delete_blocker(h, r)
        if reason:
            skipped.append({"id": rid, "reason": reason})
        else:
            _delete(h, r)
            deleted.append(rid)
    return {"deleted": deleted, "skipped": skipped}


@route("POST", r"/api/recordings/(\d+)/stop")
def recordings_stop(h, q, body, rid):
    _get("recordings", rid)
    if not h.server.manager.stop_recording(rid):
        raise ApiError(409, "Tallennus ei ole käynnissä")
    return {"ok": True}


@route("POST", r"/api/recordings/(\d+)/reprocess")
def recordings_reprocess(h, q, body, rid):
    from .recorder import work_dir
    r = _get("recordings", rid)
    if r["status"] != "failed" or not any(work_dir(rid).glob("seg*.mka")):
        raise ApiError(409, "Uudelleenkäsittely onnistuu vain epäonnistuneelle tallenteelle, jonka raakadata on tallessa")
    db.update("recordings", rid, {"status": "processing", "error": None})
    h.server.manager.reprocess(rid)
    return {"ok": True}


# ---------------------------------------------------------------- live events

EVENT_PING_S = 25      # keeps proxies and dead-connection detection happy
EVENT_SETTLE_S = 0.3   # collects a burst of writes into one event


def stream_events(h):
    """Server-Sent Events: sends `change` whenever data or recorder state changes.
    The browser then reloads what it shows."""
    h.close_connection = True
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    rev = events.revision()
    h.wfile.write(f"retry: 3000\nevent: hello\ndata: {rev}\n\n".encode())
    h.wfile.flush()
    while not h.server.stopping.is_set():
        new = events.wait(rev, EVENT_PING_S)
        if new == rev:
            h.wfile.write(b": ping\n\n")
        else:
            h.server.stopping.wait(EVENT_SETTLE_S)
            rev = events.revision()
            h.wfile.write(f"event: change\ndata: {rev}\n\n".encode())
        h.wfile.flush()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = NAME
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # --- helpers
    def _send(self, status, data=b"", ctype="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode())

    def _authorized(self):
        if not config.AUTH_USER:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
            except Exception:
                return False
            # as bytes: compare_digest rejects non-ASCII str
            user_ok = hmac.compare_digest(user.encode(), config.AUTH_USER.encode())
            pass_ok = hmac.compare_digest(pw.encode(), config.AUTH_PASS.encode())
            return user_ok and pass_ok
        return False

    def _cross_origin_write(self):
        """Blocks changes made by other sites through the browser (CSRF): browsers
        always send Origin, and the login would be sent along too."""
        origin = self.headers.get("Origin")
        if self.command in ("GET", "HEAD") or not origin:
            return False
        return urlparse(origin).netloc != self.headers.get("Host")

    def _dispatch(self):
        if not self._authorized():
            self._send(401, b"Kirjaudu", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": f'Basic realm="{NAME}", charset="UTF-8"'})
            return
        if self._cross_origin_write():
            self.close_connection = True
            self._json(403, {"error": "Pyyntö toisesta alkuperästä estetty"})
            return
        url = urlparse(self.path)
        path = url.path
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if path == "/api/events" and self.command == "GET":
                return stream_events(self)
            if path.startswith("/api/"):
                return self._api(path, q)
            if path.startswith("/media/"):
                return self._media(path, q)
            if path.startswith("/live/"):
                return self._live(path)
            return self._static(path)
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.exception("Error: %s %s", self.command, self.path)
            try:
                self._json(500, {"error": str(e)})
            except Exception:
                pass

    def _api(self, path, q):
        body = {}
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise ApiError(400, "Virheellinen JSON")
            if not isinstance(body, dict):
                raise ApiError(400, "Virheellinen JSON")
        for method, rx, fn in ROUTES:
            if method != self.command:
                continue
            m = rx.match(path)
            if m:
                args = [int(a) if a.isdigit() else a for a in m.groups()]
                return self._json(200, fn(self, q, body, *args))
        raise ApiError(404, "Tuntematon osoite")

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        f = (config.STATIC_DIR / path.lstrip("/")).resolve()
        if not f.is_relative_to(config.STATIC_DIR) or not f.is_file():
            raise ApiError(404, "Ei löydy")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def _live(self, path):
        """Live listening: the browser is sent to the stream itself, so the audio
        does not pass through this machine."""
        m = re.match(r"^/live/(\d+)$", path)
        if not m:
            raise ApiError(404, "Ei löydy")
        s = _get("streams", int(m.group(1)))
        self._send(302, headers={"Location": resolve_url(s["url"]), "Cache-Control": "no-store"})

    def _media(self, path, q):
        m = re.match(r"^/media/(\d+)", path)
        if not m:
            raise ApiError(404, "Ei löydy")
        r = _get("recordings", int(m.group(1)))
        if not r["file_path"]:
            raise ApiError(404, "Tallenne ei ole vielä valmis")
        f = (config.RECORDINGS_DIR / r["file_path"]).resolve()
        if not f.is_relative_to(config.RECORDINGS_DIR.resolve()) or not f.is_file():
            raise ApiError(404, "Tiedosto puuttuu levyltä")
        size = f.stat().st_size
        ctype = "audio/ogg" if f.suffix == ".opus" else "audio/mpeg"
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
        if q.get("download"):
            name = f"{slugify(r['title'], 80)}{f.suffix}"
            headers["Content-Disposition"] = f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(r['title'] + f.suffix)}"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            mm = re.match(r"bytes=(\d*)-(\d*)", rng)
            if mm:
                if mm.group(1):
                    start = int(mm.group(1))
                    if mm.group(2):
                        end = min(int(mm.group(2)), size - 1)
                elif mm.group(2):  # last N bytes
                    start = max(0, size - int(mm.group(2)))
                if start >= size or start > end:
                    self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
                    return
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    do_GET = _dispatch
    do_HEAD = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, manager):
        super().__init__(addr, Handler)
        self.manager = manager
        self.stopping = threading.Event()   # ends open event streams (daemon threads)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()
