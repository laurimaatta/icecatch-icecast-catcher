"""Tallennus: ffmpeg-prosessit, aikataulun ajastin, käsittelyjono ja siivous."""
import logging
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, processing

log = logging.getLogger("arkisto.recorder")

USER_AGENT = "stream-arkisto/0.1"


def slugify(text, maxlen=50):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
    return (t or "tallenne")[:maxlen]


def resolve_url(url):
    """Jos URL on .pls/.m3u-soittolista, palauttaa sen ensimmäisen striimin."""
    low = url.lower().split("?")[0]
    if not (low.endswith(".pls") or low.endswith(".m3u")):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read(65536).decode("utf-8", "ignore")
        m = re.search(r"(https?://[^\s\"']+)", text)
        if m:
            return m.group(1)
    except Exception as e:
        log.warning("Soittolistan %s luku epäonnistui: %s", url, e)
    return url


_http_opts = None


def http_input_opts():
    """ffmpeg:n uudelleenyhdistysasetukset; vanhemmista versioista (<4.4)
    puuttuu reconnect_on_network_error, joten se lisätään vain jos tuettu."""
    global _http_opts
    if _http_opts is None:
        opts = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30"]
        try:
            h = subprocess.run([config.FFMPEG, "-hide_banner", "-h", "protocol=http"],
                               capture_output=True, text=True, timeout=10).stdout
            if "reconnect_on_network_error" in h:
                opts[4:4] = ["-reconnect_on_network_error", "1"]
        except Exception:
            pass
        _http_opts = opts + ["-rw_timeout", "20000000"]
    return _http_opts


def work_dir(rec_id):
    return config.WORK_DIR / f"rec_{rec_id}"


class RecordingJob(threading.Thread):
    """Tallentaa striimiä end-aikaan asti. Katkosten jälkeen yrittää uudelleen
    ja aloittaa uuden segmentin; segmentit yhdistetään käsittelyssä."""

    def __init__(self, manager, rec_id, url, end):
        super().__init__(daemon=True, name=f"rec-{rec_id}")
        self.manager = manager
        self.rec_id = rec_id
        self.url = url
        self.end = end
        self.stop_event = threading.Event()
        self.proc = None
        self.reconnects = 0

    def request_stop(self):
        self.stop_event.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.send_signal(signal.SIGINT)  # ffmpeg sulkee tiedoston siististi
            except Exception:
                pass

    def run(self):
        wd = work_dir(self.rec_id)
        wd.mkdir(parents=True, exist_ok=True)
        seg_no = len(list(wd.glob("seg*.mka")))
        url = resolve_url(self.url)
        log.info("Tallennus %s alkaa: %s -> %s", self.rec_id, url, self.end)
        while not self.stop_event.is_set():
            remaining = (self.end - datetime.now()).total_seconds()
            if remaining < 2:
                break
            seg = wd / f"seg{seg_no:03d}.mka"
            seg_no += 1
            cmd = [
                config.FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
                "-user_agent", USER_AGENT,
                *http_input_opts(),
                "-i", url,
                "-t", f"{remaining:.1f}",
                "-map", "0:a:0", "-c", "copy", "-f", "matroska", str(seg),
            ]
            started = time.monotonic()
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.PIPE, text=True)
                _, err = self.proc.communicate()
            except Exception as e:
                err = str(e)
            rc = self.proc.returncode if self.proc else -1
            self.proc = None
            if self.stop_event.is_set():
                break
            if (self.end - datetime.now()).total_seconds() < 2:
                break
            # Loppui ennen aikojaan: katko striimissä
            self.reconnects += 1
            msg = (err or "").strip().splitlines()[-1:] or [f"rc={rc}"]
            log.warning("Tallennus %s: striimi katkesi (%s), yritetään uudelleen", self.rec_id, msg[0])
            db.update("recordings", self.rec_id, {"error": f"Katkoja: {self.reconnects} ({msg[0][:200]})"})
            # odota vähän, jos yhteys kaatui heti
            wait = 5 if time.monotonic() - started > 30 else 15
            self.stop_event.wait(min(wait, max(0, remaining)))
        log.info("Tallennus %s päättyi", self.rec_id)
        self.manager._job_finished(self.rec_id)


class Manager:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()
        self.proc_queue = queue.Queue()
        self.processing_now = None
        self._stop = threading.Event()
        self._last_cleanup = 0

    # ---------- käynnistys ----------
    def start(self):
        self._recover()
        threading.Thread(target=self._process_loop, daemon=True, name="processor").start()
        threading.Thread(target=self._schedule_loop, daemon=True, name="scheduler").start()

    def _recover(self):
        """Palvelun uudelleenkäynnistyksen jälkeen: jatka kesken jääneitä."""
        now = datetime.now()
        for r in db.query("SELECT r.*, s.url FROM recordings r LEFT JOIN streams s ON s.id = r.stream_id "
                          "WHERE r.status IN ('recording', 'processing')"):
            end = datetime.fromisoformat(r["planned_end"]) if r["planned_end"] else now
            if r["status"] == "recording" and r["url"] and end > now + timedelta(seconds=10):
                log.info("Jatketaan keskeytynyttä tallennusta %s", r["id"])
                self._start_job(r["id"], r["url"], end)
            else:
                db.update("recordings", r["id"], {"status": "processing", "ended_at": r["ended_at"] or db.now_iso()})
                self.proc_queue.put(r["id"])

    # ---------- tallennus ----------
    def _start_job(self, rec_id, url, end):
        job = RecordingJob(self, rec_id, url, end)
        with self.lock:
            self.jobs[rec_id] = job
        job.start()
        return job

    def start_recording(self, stream, end, title, schedule=None, scheduled_start=None):
        now = db.now_iso()
        rec_id = db.execute(
            "INSERT INTO recordings (stream_id, schedule_id, scheduled_start, title, started_at, "
            "planned_end, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (stream["id"], schedule["id"] if schedule else None, scheduled_start, title,
             now, end.replace(microsecond=0).isoformat(), "recording", now),
        )
        self._start_job(rec_id, stream["url"], end)
        return rec_id

    def stop_recording(self, rec_id):
        with self.lock:
            job = self.jobs.get(rec_id)
        if job:
            job.request_stop()
            return True
        return False

    def shutdown(self):
        """Palvelun sammutus: ffmpegit suljetaan siististi, mutta tila jää
        'recording':ksi, jotta tallennus jatkuu uudelleenkäynnistyksen jälkeen."""
        self._stop.set()
        with self.lock:
            jobs = list(self.jobs.values())
        for j in jobs:
            j.request_stop()
        for j in jobs:
            j.join(timeout=15)

    def _job_finished(self, rec_id):
        with self.lock:
            self.jobs.pop(rec_id, None)
        if self._stop.is_set():
            return
        db.update("recordings", rec_id, {"status": "processing", "ended_at": db.now_iso()})
        self.proc_queue.put(rec_id)

    def active(self):
        with self.lock:
            return {rid: {"end": j.end.isoformat(), "reconnects": j.reconnects}
                    for rid, j in self.jobs.items()}

    # ---------- käsittely ----------
    def _process_loop(self):
        while not self._stop.is_set():
            rec_id = self.proc_queue.get()
            self.processing_now = rec_id
            try:
                self._process_one(rec_id)
            except Exception as e:  # pragma: no cover
                log.exception("Käsittely %s kaatui", rec_id)
                db.update("recordings", rec_id, {"status": "failed", "error": str(e)[:1000]})
            finally:
                self.processing_now = None

    def _process_one(self, rec_id):
        r = db.one("SELECT * FROM recordings WHERE id = ?", (rec_id,))
        if not r:
            shutil.rmtree(work_dir(rec_id), ignore_errors=True)
            return
        s = db.one("SELECT * FROM streams WHERE id = ?", (r["stream_id"],)) if r["stream_id"] else None
        thr = s["silence_db"] if s else -50
        min_sil = s["silence_min_s"] if s else 3
        wd = work_dir(rec_id)
        segs = sorted(wd.glob("seg*.mka"))
        started = datetime.fromisoformat(r["started_at"])
        out_dir = config.RECORDINGS_DIR / f"{started:%Y}" / f"{started:%m}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{started:%Y-%m-%d_%H%M}_{slugify(r['title'])}_{rec_id}.{processing.output_ext()}"
        try:
            raw, dur = processing.process(segs, out, r["title"], thr, min_sil,
                                          artist=s["name"] if s else "")
        except Exception as e:
            log.error("Tallenteen %s käsittely epäonnistui: %s", rec_id, e)
            db.update("recordings", rec_id, {"status": "failed", "error": str(e)[:1000]})
            # raakadata jätetään talteen jos sitä on, poistetaan jos ei
            if not segs:
                shutil.rmtree(wd, ignore_errors=True)
            return
        status = "ready" if dur >= config.EMPTY_THRESHOLD_S else "empty"
        db.update("recordings", rec_id, {
            "status": status, "raw_duration_s": round(raw, 1), "duration_s": round(dur, 1),
            "file_path": str(out.relative_to(config.RECORDINGS_DIR)), "file_size": out.stat().st_size,
        })
        shutil.rmtree(wd, ignore_errors=True)
        log.info("Tallenne %s valmis: %.0f s -> %.0f s", rec_id, raw, dur)

    def reprocess(self, rec_id):
        self.proc_queue.put(rec_id)

    # ---------- ajastin ----------
    def _schedule_loop(self):
        while not self._stop.is_set():
            try:
                self._check_schedules()
                if time.time() - self._last_cleanup > 3600:
                    self._last_cleanup = time.time()
                    self.cleanup()
            except Exception:
                log.exception("Ajastinvirhe")
            self._stop.wait(15)

    def _check_schedules(self):
        now = datetime.now()
        rows = db.query(
            "SELECT sc.*, st.url, st.name AS stream_name FROM schedules sc "
            "JOIN streams st ON st.id = sc.stream_id WHERE sc.enabled = 1 AND st.enabled = 1")
        for sc in rows:
            days = {int(d) for d in sc["days"].split(",") if d.strip() != ""}
            hh, mm = (int(x) for x in sc["start_time"].split(":"))
            for offset in (0, -1):  # eilinen esiintymä voi jatkua yli keskiyön
                d = (now + timedelta(days=offset)).date()
                if d.weekday() not in days:
                    continue
                start = datetime(d.year, d.month, d.day, hh, mm)
                end = start + timedelta(minutes=sc["duration_min"])
                if not (start <= now < end - timedelta(seconds=10)):
                    continue
                key = start.isoformat()
                if db.one("SELECT id FROM recordings WHERE schedule_id = ? AND scheduled_start = ?",
                          (sc["id"], key)):
                    continue
                title = f"{sc['title']} {start:%d.%m.%Y}"
                stream = {"id": sc["stream_id"], "url": sc["url"]}
                log.info("Ajastettu tallennus alkaa: %s", title)
                self.start_recording(stream, end, title, schedule=sc, scheduled_start=key)

    # ---------- siivous ----------
    def cleanup(self):
        """Poistaa aikataulun säilytysajan ylittäneet tallenteet (paitsi suojatut)."""
        rows = db.query(
            "SELECT r.id, r.started_at, sc.keep_days FROM recordings r "
            "JOIN schedules sc ON sc.id = r.schedule_id "
            "WHERE sc.keep_days IS NOT NULL AND r.keep = 0 AND r.status IN ('ready','empty','failed')")
        now = datetime.now()
        n = 0
        for r in rows:
            if datetime.fromisoformat(r["started_at"]) < now - timedelta(days=r["keep_days"]):
                delete_recording(r["id"])
                n += 1
        if n:
            log.info("Siivous: poistettiin %d vanhaa tallennetta", n)


def delete_recording(rec_id):
    r = db.one("SELECT * FROM recordings WHERE id = ?", (rec_id,))
    if not r:
        return False
    if r["file_path"]:
        p = config.RECORDINGS_DIR / r["file_path"]
        p.unlink(missing_ok=True)
    shutil.rmtree(work_dir(rec_id), ignore_errors=True)
    db.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
    return True
