"""Recording: ffmpeg processes, schedule timer, processing queue and cleanup.

Messages stored in recordings.error are shown in the UI, so they are in Finnish.
"""
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

from . import config, db, detect, events, processing

log = logging.getLogger("icecatch.recorder")

USER_AGENT = detect.USER_AGENT
STOP_GRACE_S = 15      # time ffmpeg gets to close its file after SIGINT
OVERRUN_GRACE_S = 30   # how far past the planned end a recording may run


class NoSpace(Exception):
    """Not enough disk space for a recording."""


def disk_free():
    return shutil.disk_usage(config.DATA_DIR).free


def output_kbps():
    m = re.match(r"(\d+)", config.OUTPUT_BITRATE)
    return int(m.group(1)) if m else 192


def bytes_for(seconds, rate_kbps):
    """Estimated space for a recording: raw audio and the finished file are both
    on disk during processing."""
    return seconds * ((rate_kbps or config.ASSUMED_KBPS) + output_kbps()) * 1000 / 8


def slugify(text, maxlen=50):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
    return (t or "recording")[:maxlen]


def resolve_url(url):
    """If the URL is a .pls/.m3u playlist, returns its first stream."""
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
        log.warning("Could not read playlist %s: %s", url, e)
    return url


_http_opts = None


def http_input_opts():
    """ffmpeg reconnect options; versions before 4.4 lack reconnect_on_network_error,
    so it is added only when supported."""
    global _http_opts
    if _http_opts is None:
        opts = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30"]
        try:
            h = subprocess.run([config.FFMPEG, "-hide_banner", "-h", "protocol=http"],
                               capture_output=True, text=True, errors="replace", timeout=10).stdout
            if "reconnect_on_network_error" in h:
                opts[4:4] = ["-reconnect_on_network_error", "1"]
        except Exception:
            pass
        _http_opts = opts + ["-rw_timeout", "20000000"]
    return _http_opts


def work_dir(rec_id):
    return config.WORK_DIR / f"rec_{rec_id}"


class RecordingJob(threading.Thread):
    """Records a stream until `end`. After an outage it retries and starts a new
    segment; segments are joined during processing."""

    def __init__(self, manager, rec_id, url, end, stream_id=None, auto=False, rate_kbps=None):
        super().__init__(daemon=True, name=f"rec-{rec_id}")
        self.manager = manager
        self.rec_id = rec_id
        self.url = url
        self.end = end
        self.stop_event = threading.Event()
        self.proc = None
        self.reconnects = 0
        self.stream_id = stream_id
        self.auto = auto              # ends when the broadcast ends
        self.rate_kbps = rate_kbps
        self.finish_reason = "end"    # end | stop | offline | disk

    def request_stop(self, reason="stop"):
        self.finish_reason = reason
        self.stop_event.set()

    def _wait_for_return(self, url):
        """The stream dropped. Returns True if the broadcast resumes within the grace period."""
        deadline = time.monotonic() + config.AUTO_GRACE_S
        while time.monotonic() < deadline and datetime.now() < self.end:
            if self.stop_event.is_set():
                return True  # the main loop handles stopping
            if detect.check(url).on_air:
                return True
            self.stop_event.wait(min(config.AUTO_POLL_S, 10))
        return False

    def _wait_ffmpeg(self, p):
        """Waits for ffmpeg to exit and returns its stderr output.

        ffmpeg's -t counts audio duration, not wall-clock time, so a stalled stream
        could otherwise record past the planned end. The stop request is checked
        here, so it cannot get lost while ffmpeg is starting."""
        deadline = self.end + timedelta(seconds=OVERRUN_GRACE_S)
        interrupted_at = None
        while True:
            try:
                return p.communicate(timeout=1)[1]
            except subprocess.TimeoutExpired:
                pass
            if interrupted_at is None:
                if self.stop_event.is_set() or datetime.now() >= deadline:
                    p.send_signal(signal.SIGINT)  # ffmpeg closes the file cleanly
                    interrupted_at = time.monotonic()
            elif time.monotonic() - interrupted_at > STOP_GRACE_S:
                log.warning("Recording %s: ffmpeg did not exit, killing it", self.rec_id)
                p.kill()

    def run(self):
        wd = work_dir(self.rec_id)
        wd.mkdir(parents=True, exist_ok=True)
        seg_no = len(list(wd.glob("seg*.mka")))
        url = resolve_url(self.url)
        log.info("Recording %s starts: %s -> %s", self.rec_id, url, self.end)
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
                "-map", "0:a:0", "-c", "copy",
                # Write to disk at once: with defaults, low-bitrate audio stays in the
                # buffer and a crash or power cut would lose the whole segment.
                "-flush_packets", "1", "-cluster_time_limit", "1000",
                "-f", "matroska", str(seg),
            ]
            started = time.monotonic()
            self.proc = None
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                             text=True, errors="replace")
                err = self._wait_ffmpeg(self.proc)
            except Exception as e:
                err = str(e)
            rc = self.proc.returncode if self.proc else -1
            self.proc = None
            if self.stop_event.is_set():
                break
            if (self.end - datetime.now()).total_seconds() < 2:
                break
            # Ended early: an outage in the stream or the end of the broadcast
            if self.auto and not self._wait_for_return(url):
                self.finish_reason = "offline"
                log.info("Recording %s: broadcast ended", self.rec_id)
                break
            self.reconnects += 1
            msg = (err or "").strip().splitlines()[-1:] or [f"rc={rc}"]
            log.warning("Recording %s: stream dropped (%s), retrying", self.rec_id, msg[0])
            db.update("recordings", self.rec_id, {"error": f"Katkoja: {self.reconnects} ({msg[0][:200]})"})
            # wait a little if the connection failed immediately
            if not self.auto:  # an automatic recording has already waited for the return
                wait = 5 if time.monotonic() - started > 30 else 15
                self.stop_event.wait(min(wait, max(0, remaining)))
        # Max length reached but the broadcast had already ended: the next one may start at once.
        if self.auto and self.finish_reason == "end" and not detect.check(url).on_air:
            self.finish_reason = "offline"
        log.info("Recording %s ended", self.rec_id)
        self.manager._job_finished(self.rec_id)


class Manager:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()
        self.proc_queue = queue.Queue()
        self._processing_now = None
        self._stop = threading.Event()
        self._last_cleanup = 0
        self._skipped = set()  # schedule occurrences deleted by the user
        self._space_lock = threading.RLock()
        self._nospace_reported = set()  # streams whose lack of space is already recorded
        self.on_air = {}  # stream_id -> is the broadcast on (latest check)

    @property
    def processing_now(self):
        return self._processing_now

    @processing_now.setter
    def processing_now(self, rec_id):
        if rec_id != self._processing_now:
            self._processing_now = rec_id
            events.notify()

    def _set_on_air(self, stream_id, value):
        if self.on_air.get(stream_id) != value:
            self.on_air[stream_id] = value
            events.notify()

    # ---------- startup ----------
    def start(self):
        self._recover()
        threading.Thread(target=self._process_loop, daemon=True, name="processor").start()
        threading.Thread(target=self._schedule_loop, daemon=True, name="scheduler").start()
        threading.Thread(target=self._watch_loop, daemon=True, name="watcher").start()

    def _recover(self):
        """After a service restart: continue unfinished work."""
        now = datetime.now()
        for r in db.query("SELECT r.*, s.url FROM recordings r LEFT JOIN streams s ON s.id = r.stream_id "
                          "WHERE r.status IN ('recording', 'processing')"):
            end = datetime.fromisoformat(r["planned_end"]) if r["planned_end"] else now
            if r["status"] == "recording" and r["url"] and end > now + timedelta(seconds=10):
                log.info("Resuming interrupted recording %s", r["id"])
                self._start_job(r["id"], r["url"], end, r["stream_id"], r["source"] == "auto")
            else:
                if r["status"] == "recording" and r["source"] == "auto" and r["stream_id"]:
                    db.update("streams", r["stream_id"], {"wait_offline": 1})
                db.update("recordings", r["id"], {"status": "processing", "ended_at": r["ended_at"] or db.now_iso()})
                self.proc_queue.put(r["id"])

    # ---------- recording ----------
    def _start_job(self, rec_id, url, end, stream_id=None, auto=False, rate_kbps=None):
        job = RecordingJob(self, rec_id, url, end, stream_id, auto, rate_kbps)
        with self.lock:
            self.jobs[rec_id] = job
        job.start()
        events.notify()
        return job

    def start_recording(self, stream, end, title, schedule=None, scheduled_start=None,
                        source="manual", rate_kbps=None):
        """Starts a recording. Raises NoSpace if there is not enough disk space."""
        self.ensure_space((end - datetime.now()).total_seconds(), rate_kbps)
        now = db.now_iso()
        rec_id = db.execute(
            "INSERT INTO recordings (stream_id, schedule_id, scheduled_start, title, started_at, "
            "planned_end, status, created_at, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (stream["id"], schedule["id"] if schedule else None, scheduled_start, title,
             now, end.replace(microsecond=0).isoformat(), "recording", now, source),
        )
        self._start_job(rec_id, stream["url"], end, stream["id"], source == "auto", rate_kbps)
        return rec_id

    def _record_missed(self, stream_id, title, error, source, schedule_id=None, scheduled_start=None):
        """Leaves a visible entry for a recording that could not be made."""
        now = db.now_iso()
        db.execute(
            "INSERT INTO recordings (stream_id, schedule_id, scheduled_start, title, started_at, "
            "status, error, created_at, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (stream_id, schedule_id, scheduled_start, title, now, "failed", error, now, source))

    # ---------- disk space ----------
    def space_needed(self, seconds, rate_kbps=None):
        """Space for a new recording plus the remaining part of running ones, plus the reserve."""
        with self.lock:
            jobs = list(self.jobs.values())
        now = datetime.now()
        active = sum(bytes_for(max(0, (j.end - now).total_seconds()), j.rate_kbps) for j in jobs)
        return bytes_for(seconds, rate_kbps) + active + config.MIN_FREE_MB * 1024 * 1024

    def ensure_space(self, seconds, rate_kbps=None):
        with self._space_lock:
            need = self.space_needed(seconds, rate_kbps)
            free = disk_free()
            if free < need:
                raise NoSpace(f"Levytila ei riitä: tarvitaan arviolta {need / 1e9:.1f} Gt, "
                              f"vapaana {free / 1e9:.1f} Gt")

    def _guard_disk(self):
        """Stops recordings cleanly before the disk fills up. Recordings are never deleted."""
        reserve = config.MIN_FREE_MB * 1024 * 1024
        with self.lock:
            jobs = list(self.jobs.values())
        if not jobs or disk_free() >= reserve:
            return
        for j in jobs:
            log.error("Disk full: stopping recording %s", j.rec_id)
            db.update("recordings", j.rec_id, {"error": "Tallennus pysäytettiin, koska levy on täynnä"})
            j.request_stop("disk")

    def stop_recording(self, rec_id):
        with self.lock:
            job = self.jobs.get(rec_id)
        if job:
            job.request_stop()
            return True
        return False

    def shutdown(self):
        """Service shutdown: ffmpeg processes are closed cleanly, but the status stays
        'recording' so the recording continues after a restart."""
        self._stop.set()
        with self.lock:
            jobs = list(self.jobs.values())
        for j in jobs:
            j.request_stop()
        for j in jobs:
            j.join(timeout=STOP_GRACE_S + 15)  # a level check may be in progress

    def _job_finished(self, rec_id):
        if self._stop.is_set():
            with self.lock:
                self.jobs.pop(rec_id, None)
            return
        with self.lock:
            job = self.jobs.get(rec_id)
        # Max length reached or stopped by the user: no new recording of the same
        # broadcast. Marked before the job is removed so the watcher cannot slip in.
        if job and job.auto and job.stream_id and job.finish_reason != "offline":
            db.update("streams", job.stream_id, {"wait_offline": 1})
        with self.lock:
            self.jobs.pop(rec_id, None)
        db.update("recordings", rec_id, {"status": "processing", "ended_at": db.now_iso()})
        self.proc_queue.put(rec_id)

    def active(self):
        with self.lock:
            return {rid: {"end": j.end.isoformat(), "reconnects": j.reconnects, "auto": j.auto}
                    for rid, j in self.jobs.items()}

    def recording_stream_ids(self):
        with self.lock:
            return {j.stream_id for j in self.jobs.values()}

    # ---------- processing ----------
    def _process_loop(self):
        while not self._stop.is_set():
            rec_id = self.proc_queue.get()
            self.processing_now = rec_id
            try:
                self._process_one(rec_id)
            except Exception as e:  # pragma: no cover
                log.exception("Processing %s crashed", rec_id)
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
        wd = work_dir(rec_id)
        segs = sorted(wd.glob("seg*.mka"))
        started = datetime.fromisoformat(r["started_at"])
        out_dir = config.RECORDINGS_DIR / f"{started:%Y}" / f"{started:%m}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{started:%Y-%m-%d_%H%M}_{slugify(r['title'])}_{rec_id}.{processing.output_ext()}"
        try:
            raw, dur = processing.process(segs, out, r["title"], thr,
                                          artist=s["name"] if s else "")
        except Exception as e:
            log.error("Processing recording %s failed: %s", rec_id, e)
            db.update("recordings", rec_id, {"status": "failed", "error": str(e)[:1000]})
            # keep raw data if there is any, remove the directory if not
            if not segs:
                shutil.rmtree(wd, ignore_errors=True)
            return
        status = "ready" if dur >= config.EMPTY_THRESHOLD_S else "empty"
        db.update("recordings", rec_id, {
            "status": status, "raw_duration_s": round(raw, 1), "duration_s": round(dur, 1),
            "file_path": str(out.relative_to(config.RECORDINGS_DIR)), "file_size": out.stat().st_size,
        })
        shutil.rmtree(wd, ignore_errors=True)
        log.info("Recording %s ready: %.0f s -> %.0f s", rec_id, raw, dur)
        if status == "empty":
            # Too little audio to be worth keeping; empty recordings are never listed.
            log.info("Recording %s had almost no audio, deleting", rec_id)
            if r["schedule_id"]:
                self.skip_occurrence(r["schedule_id"], r["scheduled_start"])
            delete_recording(rec_id)
            if r["source"] == "auto" and dur < config.AUTO_DISCARD_S and r["stream_id"]:
                # Nothing but silence. If the mount stays up all the time, the next event
                # may begin at any moment, so the next recording may start right away.
                db.update("streams", r["stream_id"], {"wait_offline": 0})

    def skip_occurrence(self, schedule_id, scheduled_start):
        """Keeps a deleted recording's schedule from starting again while the
        programme's time window is still open."""
        self._skipped.add((schedule_id, scheduled_start))

    def reprocess(self, rec_id):
        self.proc_queue.put(rec_id)

    # ---------- scheduler ----------
    def _schedule_loop(self):
        while not self._stop.is_set():
            try:
                self._check_schedules()
                self._guard_disk()
                if time.time() - self._last_cleanup > 3600:
                    self._last_cleanup = time.time()
                    self.cleanup()
            except Exception:
                log.exception("Scheduler error")
            self._stop.wait(15)

    def _check_schedules(self):
        now = datetime.now()
        rows = db.query(
            "SELECT sc.*, st.url, st.name AS stream_name FROM schedules sc "
            "JOIN streams st ON st.id = sc.stream_id WHERE sc.enabled = 1 AND st.enabled = 1")
        for sc in rows:
            days = {int(d) for d in sc["days"].split(",") if d.strip() != ""}
            hh, mm = (int(x) for x in sc["start_time"].split(":"))
            for offset in (0, -1):  # yesterday's occurrence may run past midnight
                d = (now + timedelta(days=offset)).date()
                if d.weekday() not in days:
                    continue
                start = datetime(d.year, d.month, d.day, hh, mm)
                end = start + timedelta(minutes=sc["duration_min"])
                if not (start <= now < end - timedelta(seconds=10)):
                    continue
                key = start.isoformat()
                if (sc["id"], key) in self._skipped:
                    continue
                if db.one("SELECT id FROM recordings WHERE schedule_id = ? AND scheduled_start = ?",
                          (sc["id"], key)):
                    continue
                title = f"{sc['title']} {start:%d.%m.%Y}"
                stream = {"id": sc["stream_id"], "url": sc["url"]}
                log.info("Scheduled recording starts: %s", title)
                try:
                    self.start_recording(stream, end, title, schedule=sc, scheduled_start=key,
                                         source="schedule")
                except NoSpace as e:
                    log.error("Scheduled recording %s skipped: %s", title, e)
                    self._record_missed(sc["stream_id"], title, str(e), "schedule", sc["id"], key)

    # ---------- automatic recording ----------
    def _watch_loop(self):
        while not self._stop.is_set():
            try:
                self._watch_streams()
            except Exception:
                log.exception("Watcher error")
            self._stop.wait(config.AUTO_POLL_S)

    def _watch_streams(self):
        """Starts recording the selected streams whose broadcast has begun."""
        rows = db.query("SELECT * FROM streams WHERE auto_record = 1 AND enabled = 1")
        watched = {s["id"] for s in rows}
        if any(k not in watched for k in self.on_air):
            self.on_air = {k: v for k, v in self.on_air.items() if k in watched}
            events.notify()
        # Automatic recording was switched off (or the stream removed): stop its recording
        with self.lock:
            orphans = [j for j in self.jobs.values() if j.auto and j.stream_id not in watched]
        for j in orphans:
            if not j.stop_event.is_set():
                log.info("Recording %s: automatic recording switched off, stopping", j.rec_id)
                j.request_stop()
        for s in rows:
            if s["id"] in self.recording_stream_ids():
                self._set_on_air(s["id"], True)
                continue
            state = detect.check(resolve_url(s["url"]))
            self._set_on_air(s["id"], state.on_air)
            if not state.on_air:
                self._nospace_reported.discard(s["id"])
                if s["wait_offline"]:
                    db.update("streams", s["id"], {"wait_offline": 0})
                continue
            if s["wait_offline"]:
                continue
            now = datetime.now()
            title = f"{s['name']} {now:%d.%m.%Y %H:%M}"
            end = now + timedelta(minutes=s["max_duration_min"])
            log.info("Broadcast started: %s", s["name"])
            try:
                self.start_recording(s, end, title, source="auto", rate_kbps=state.rate_kbps)
            except NoSpace as e:
                if s["id"] not in self._nospace_reported:
                    self._nospace_reported.add(s["id"])
                    log.error("Broadcast %s not recorded: %s", title, e)
                    self._record_missed(s["id"], title, str(e), "auto")

    # ---------- cleanup ----------
    def cleanup(self):
        """Deletes recordings past their schedule's retention time (except favourites),
        failed recordings after FAILED_KEEP_DAYS and leftover empty ones."""
        cutoff = (datetime.now() - timedelta(days=config.FAILED_KEEP_DAYS)).isoformat(timespec="seconds")
        junk = db.query("SELECT id FROM recordings WHERE status = 'empty' "
                        "OR (status = 'failed' AND started_at < ?)", (cutoff,))
        for r in junk:
            delete_recording(r["id"])
        if junk:
            log.info("Cleanup: deleted %d failed or empty recordings", len(junk))
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
            log.info("Cleanup: deleted %d old recordings", n)


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
