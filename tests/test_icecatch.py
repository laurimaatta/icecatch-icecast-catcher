"""Recorder and server tests. ffmpeg tests are skipped if ffmpeg is missing."""
import base64
import http.client
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="icecatch-test")
os.environ["ICECATCH_DATA_DIR"] = _TMP

from icecatch import config, db, detect, events, processing, recorder, server  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def make_segment(path, *filters, metadata=None):
    """Creates an MP3 segment; filters = lavfi sources joined one after another."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for f in filters:
        cmd += ["-f", "lavfi", "-i", f]
    labels = "".join(f"[{i}:a]" for i in range(len(filters)))
    cmd += ["-filter_complex", f"{labels}concat=n={len(filters)}:v=0:a=1[o]", "-map", "[o]"]
    if metadata:
        cmd += ["-metadata", metadata]
    cmd += ["-c:a", "libmp3lame", "-f", "matroska", str(path)]
    subprocess.run(cmd, check=True)


class SilenceCuts(unittest.TestCase):
    def test_middle_silence_keeps_one_second(self):
        self.assertEqual(processing.silence_cuts([(10.0, 20.0)], 60, 3), [(10.5, 19.5)])

    def test_short_silence_is_kept(self):
        self.assertEqual(processing.silence_cuts([(10.0, 12.0)], 60, 3), [])

    def test_leading_silence_removed(self):
        self.assertEqual(processing.silence_cuts([(0.0, 10.0)], 60, 3), [(0.0, 9.7)])

    def test_unterminated_trailing_silence_runs_to_end(self):
        self.assertEqual(processing.silence_cuts([(50.0, None)], 60, 3), [(50.5, 61)])


class QuietSpans(unittest.TestCase):
    Q, S = -90, -30   # silence and programme, dBFS in 0.5 s blocks

    def test_isolated_sounds_do_not_break_silence(self):
        lv = [self.Q] * 40 + [self.S] * 2 + [self.Q] * 40 + [self.S] * 3 + [self.Q] * 40 + [self.S] * 100
        self.assertEqual(processing.quiet_spans(lv, -50), [(0.0, 62.5)])

    def test_sustained_sound_breaks_silence(self):
        lv = [self.Q] * 40 + [self.S] * 4 + [self.Q] * 40
        self.assertEqual(processing.quiet_spans(lv, -50), [(0.0, 20.0), (22.0, None)])

    def test_short_dip_inside_speech_is_bridged(self):
        lv = [self.S] * 2 + [self.Q] + [self.S] * 2 + [self.Q] * 10
        self.assertEqual(processing.quiet_spans(lv, -50), [(2.5, None)])

    def test_noise_below_threshold_is_silence(self):
        self.assertEqual(processing.quiet_spans([-60] * 100, -50), [(0.0, None)])
        self.assertEqual(processing.quiet_spans([], -50), [])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg puuttuu")
class Processing(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))

    def test_silence_removed(self):
        seg = self.dir / "seg000.mka"
        make_segment(seg, "sine=440:d=5", "anullsrc=r=44100:cl=mono:d=8", "sine=440:d=5")
        raw, dur = processing.process([seg], self.dir / "o.mp3", "Ääkkös", -50, 3)
        self.assertAlmostEqual(raw, 18, delta=0.3)
        self.assertAlmostEqual(dur, 11, delta=1)  # detection works in 0.5 s blocks

    def test_single_sounds_in_long_silence_are_removed(self):
        # 10 s programme, 40 s silence with two clicks, 10 s programme; default 30 s
        seg = self.dir / "seg000.mka"
        make_segment(seg, "sine=440:d=10", "anullsrc=r=44100:cl=mono:d=15", "sine=440:d=0.3",
                     "anullsrc=r=44100:cl=mono:d=12", "sine=880:d=0.5", "anullsrc=r=44100:cl=mono:d=12.2",
                     "sine=440:d=10")
        raw, dur = processing.process([seg], self.dir / "o.mp3", "t", -50)
        self.assertAlmostEqual(raw, 60, delta=0.5)
        self.assertAlmostEqual(dur, 21, delta=1)

    def test_pause_shorter_than_default_is_kept(self):
        seg = self.dir / "seg000.mka"
        make_segment(seg, "sine=440:d=10", "anullsrc=r=44100:cl=mono:d=20", "sine=440:d=10")
        raw, dur = processing.process([seg], self.dir / "o.mp3", "t", -50)
        self.assertAlmostEqual(dur, 40, delta=0.5)

    def test_non_utf8_stream_tags_do_not_fail(self):
        # Stations' ICY names are often Latin-1, and ffmpeg prints them as raw bytes
        seg = self.dir / "seg000.mka"
        make_segment(seg, "sine=440:d=4")
        latin1 = self.dir / "latin1.mka"
        subprocess.run([b"ffmpeg", b"-hide_banner", b"-loglevel", b"error", b"-i", str(seg).encode(),
                        b"-c", b"copy", b"-metadata", b"icy-name=Testi\xe4", str(latin1).encode()], check=True)
        raw, dur = processing.process([latin1], self.dir / "o.mp3", "t", -50, 3)
        self.assertGreater(dur, 3)

    def test_unusable_segment_does_not_lose_others(self):
        good = self.dir / "seg000.mka"
        make_segment(good, "sine=440:d=4")
        bad = self.dir / "seg001.mka"
        bad.write_bytes(b"\x1a\x45\xdf\xa3 ei oikeaa aanta")
        raw, dur = processing.process([good, bad], self.dir / "o.mp3", "t", -50, 3)
        self.assertAlmostEqual(raw, 4, delta=0.3)

    def test_no_usable_audio_raises(self):
        bad = self.dir / "seg000.mka"
        bad.write_bytes(b"roskaa")
        with self.assertRaises(RuntimeError):
            processing.process([bad], self.dir / "o.mp3", "t", -50, 3)

    def test_killed_segment_keeps_audio_with_true_duration(self):
        seg = self.dir / "seg000.mka"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-re", "-f", "lavfi",
               "-i", "sine=440", "-t", "600", "-c:a", "libmp3lame",
               "-flush_packets", "1", "-cluster_time_limit", "1000", "-f", "matroska", str(seg)]
        p = subprocess.Popen(cmd)
        time.sleep(3)
        p.kill()
        p.wait()
        dur = processing.segment_duration(seg)
        self.assertGreater(dur, 1.5)
        self.assertLess(dur, 4)  # the header would claim 600 s


class Job(unittest.TestCase):
    def _job(self, end):
        return recorder.RecordingJob(mock.Mock(), 1, "http://x", end)

    def _finish_reason_at_max_length(self, on_air):
        job = recorder.RecordingJob(mock.Mock(), 1, "http://x", datetime.now(), auto=True)
        with mock.patch.object(recorder, "work_dir", return_value=Path(tempfile.mkdtemp(dir=_TMP))), \
                mock.patch.object(detect, "check", return_value=detect.Broadcast(on_air)):
            job.run()
        return job.finish_reason

    def test_max_length_with_broadcast_still_on_waits_for_end(self):
        self.assertEqual(self._finish_reason_at_max_length(True), "end")

    def test_max_length_after_broadcast_ended_allows_next_start(self):
        self.assertEqual(self._finish_reason_at_max_length(False), "offline")

    def _sleeper(self):
        # stderr pipe as in a real run, so communicate returns the same shape
        return subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True)

    def test_stop_request_interrupts_ffmpeg(self):
        job = self._job(datetime.now() + timedelta(hours=1))
        p = self._sleeper()
        threading.Timer(0.2, job.request_stop).start()
        job._wait_ffmpeg(p)
        self.assertEqual(p.returncode, -signal.SIGINT)

    def test_overrun_past_planned_end_is_interrupted(self):
        job = self._job(datetime.now() - timedelta(seconds=recorder.OVERRUN_GRACE_S + 1))
        p = self._sleeper()
        job._wait_ffmpeg(p)
        self.assertEqual(p.returncode, -signal.SIGINT)

    def test_ffmpeg_ignoring_sigint_is_killed(self):
        job = self._job(datetime.now() + timedelta(hours=1))
        job.stop_event.set()
        p = subprocess.Popen(["python3", "-c", "import signal,time;"
                              "signal.signal(signal.SIGINT,signal.SIG_IGN);print(flush=True);time.sleep(60)"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p.stdout.readline()
        with mock.patch.object(recorder, "STOP_GRACE_S", 0):
            job._wait_ffmpeg(p)
        self.assertEqual(p.returncode, -signal.SIGKILL)


class Base(unittest.TestCase):
    def setUp(self):
        db.init()
        for t in ("recordings", "schedules", "streams"):
            db.execute(f"DELETE FROM {t}")


class Scheduler(Base):
    def setUp(self):
        super().setUp()
        self.sid = db.execute("INSERT INTO streams (name,url,created_at) VALUES ('a','http://x',?)", (db.now_iso(),))
        self.m = recorder.Manager()
        self.m.start_recording = mock.Mock()

    def _schedule(self, start, minutes):
        return db.execute(
            "INSERT INTO schedules (stream_id,title,start_time,duration_min,created_at) VALUES (?,?,?,?,?)",
            (self.sid, "Ohjelma", f"{start:%H:%M}", minutes, db.now_iso()))

    def test_starts_inside_window(self):
        self._schedule(datetime.now() - timedelta(minutes=1), 30)
        self.m._check_schedules()
        self.m.start_recording.assert_called_once()

    def test_outside_window_does_nothing(self):
        self._schedule(datetime.now() + timedelta(hours=2), 30)
        self.m._check_schedules()
        self.m.start_recording.assert_not_called()

    def test_show_past_midnight_uses_previous_day(self):
        now = datetime.now()
        with mock.patch.object(recorder, "datetime", wraps=datetime) as dt:
            dt.now.return_value = now.replace(hour=0, minute=30, second=0, microsecond=0)
            self._schedule(now.replace(hour=23, minute=0), 120)
            self.m._check_schedules()
        self.m.start_recording.assert_called_once()
        self.assertTrue(self.m.start_recording.call_args.kwargs["scheduled_start"].endswith("T23:00:00"))

    def test_deleted_occurrence_does_not_restart(self):
        start = datetime.now() - timedelta(minutes=1)
        sc = self._schedule(start, 30)
        self.m.skip_occurrence(sc, start.replace(second=0, microsecond=0).isoformat())
        self.m._check_schedules()
        self.m.start_recording.assert_not_called()


class HttpCase(Base):
    def setUp(self):
        super().setUp()
        self.mgr = mock.Mock(processing_now=None)
        self.mgr.active.return_value = {}
        self.mgr.proc_queue.qsize.return_value = 0
        self.srv = server.Server(("127.0.0.1", 0), self.mgr)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.host = f"127.0.0.1:{self.srv.server_address[1]}"

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection(self.host, timeout=10)
        c.request(method, path, None if body is None else json.dumps(body), headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def _auth(self, cred):
        return {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}


class Http(HttpCase):
    def test_non_ascii_password(self):
        with mock.patch.object(config, "AUTH_USER", "lauri"), mock.patch.object(config, "AUTH_PASS", "sääli"):
            self.assertEqual(self.req("GET", "/api/status")[0], 401)
            self.assertEqual(self.req("GET", "/api/status", headers=self._auth("lauri:sääli"))[0], 200)
            self.assertEqual(self.req("GET", "/api/status", headers=self._auth("lauri:väärä"))[0], 401)

    def test_cross_origin_write_blocked_same_origin_allowed(self):
        body = {"name": "a", "url": "http://x"}
        self.assertEqual(self.req("POST", "/api/streams", body, {"Origin": "http://paha.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/streams", body, {"Origin": f"http://{self.host}"})[0], 200)
        self.assertEqual(self.req("POST", "/api/streams", body)[0], 200)  # e.g. curl

    def test_static_traversal_blocked(self):
        self.assertEqual(self.req("GET", "/index.html")[0], 200)
        self.assertEqual(self.req("GET", "/../icecatch/config.py")[0], 404)
        self.assertEqual(self.req("GET", "/%2e%2e/README.md")[0], 404)

    def test_bad_input_returns_400(self):
        self.assertEqual(self.req("GET", "/api/recordings?limit=x")[0], 400)
        self.assertEqual(self.req("POST", "/api/streams", ["lista"])[0], 400)
        sid = json.loads(self.req("POST", "/api/streams", {"name": "a", "url": "http://x"})[1])["id"]
        body = {"stream_id": sid, "title": "t", "start_time": "10:00", "days": "ma"}
        self.assertEqual(self.req("POST", "/api/schedules", body)[0], 400)

    def test_delete_scheduled_recording_marks_occurrence_skipped(self):
        sid = db.execute("INSERT INTO streams (name,url,created_at) VALUES ('a','http://x',?)", (db.now_iso(),))
        sc = db.execute("INSERT INTO schedules (stream_id,title,start_time,duration_min,created_at) "
                        "VALUES (?,'t','10:00',30,?)", (sid, db.now_iso()))
        rid = db.execute("INSERT INTO recordings (stream_id,schedule_id,scheduled_start,title,started_at,status,created_at) "
                         "VALUES (?,?,'2026-01-01T10:00:00','t',?,'ready',?)", (sid, sc, db.now_iso(), db.now_iso()))
        self.assertEqual(self.req("DELETE", f"/api/recordings/{rid}")[0], 200)
        self.mgr.skip_occurrence.assert_called_once_with(sc, "2026-01-01T10:00:00")


class Migration(unittest.TestCase):
    def test_old_database_gets_new_columns(self):
        path = Path(_TMP) / "vanha.db"
        import sqlite3
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE streams (id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, "
                  "enabled INTEGER NOT NULL DEFAULT 1, silence_db REAL NOT NULL DEFAULT -50, "
                  "silence_min_s REAL NOT NULL DEFAULT 3, created_at TEXT NOT NULL)")
        c.execute("INSERT INTO streams (name,url,created_at) VALUES ('a','http://x','2026')")
        c.commit()
        c.close()
        with mock.patch.object(config, "DB_PATH", path), mock.patch.object(db, "_local", threading.local()):
            db.init()
            db.init()  # a second run must not fail
            s = db.one("SELECT * FROM streams")
            db.conn().close()
        self.assertEqual((s["name"], s["auto_record"], s["max_duration_min"]), ("a", 0, 240))


class LegacyDatabase(unittest.TestCase):
    def test_arkisto_db_is_renamed(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        import sqlite3
        c = sqlite3.connect(d / "arkisto.db")
        c.execute("CREATE TABLE t (x)")
        c.commit()
        c.close()
        with mock.patch.object(config, "DB_PATH", d / "icecatch.db"), \
                mock.patch.object(config, "LEGACY_DB_PATH", d / "arkisto.db"), \
                mock.patch.object(db, "_local", threading.local()):
            db.init()
            names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
            db.conn().close()
        self.assertIn("t", names)
        self.assertFalse((d / "arkisto.db").exists())


class Events(HttpCase):
    def _open(self):
        c = http.client.HTTPConnection(self.host, timeout=10)
        c.request("GET", "/api/events")
        r = c.getresponse()
        self.addCleanup(c.close)
        return r

    def _event(self, r):
        lines = []
        while True:
            line = r.fp.readline().decode().rstrip("\n")
            if not line:
                if any(x.startswith("event:") for x in lines):
                    return dict(x.split(": ", 1) for x in lines if not x.startswith(("retry", ":")))
                lines = []
                continue
            lines.append(line)

    def test_write_sends_change_event(self):
        r = self._open()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(self._event(r)["event"], "hello")
        self.req("POST", "/api/streams", {"name": "a", "url": "http://x"})
        self.assertEqual(self._event(r)["event"], "change")

    def test_wait_returns_on_notify(self):
        rev = events.revision()
        threading.Timer(0.1, events.notify).start()
        self.assertNotEqual(events.wait(rev, 5), rev)


class MountCheck(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/ei":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html" if self.path == "/html" else "audio/mpeg")
                self.send_header("icy-br", "128")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def test_states(self):
        self.assertEqual(detect.check(self.base + "/live"), detect.Broadcast(True, 128))
        self.assertFalse(detect.check(self.base + "/ei").on_air)
        self.assertFalse(detect.check(self.base + "/html").on_air)
        self.assertFalse(detect.check("http://127.0.0.1:9/x", timeout=1).on_air)


class Watcher(Base):
    def setUp(self):
        super().setUp()
        self.sid = db.execute("INSERT INTO streams (name,url,auto_record,max_duration_min,created_at) "
                              "VALUES ('Radio','http://x',1,120,?)", (db.now_iso(),))
        self.m = recorder.Manager()
        self.m._start_job = mock.Mock()
        p = mock.patch.object(recorder, "disk_free", return_value=10 ** 13)
        self.free = p.start()
        self.addCleanup(p.stop)

    def _poll(self, on_air, rate=128):
        with mock.patch.object(detect, "check", return_value=detect.Broadcast(on_air, rate)):
            self.m._watch_streams()

    def _flag(self):
        return db.one("SELECT wait_offline w FROM streams WHERE id=?", (self.sid,))["w"]

    def test_starts_when_broadcast_starts_with_max_length(self):
        self._poll(False)
        self.m._start_job.assert_not_called()
        self._poll(True)
        self.m._start_job.assert_called_once()
        r = db.one("SELECT * FROM recordings")
        self.assertEqual(r["source"], "auto")
        minutes = (datetime.fromisoformat(r["planned_end"]) - datetime.fromisoformat(r["started_at"])).total_seconds() / 60
        self.assertAlmostEqual(minutes, 120, delta=0.1)

    def test_not_selected_stream_is_ignored(self):
        db.update("streams", self.sid, {"auto_record": 0})
        self._poll(True)
        self.m._start_job.assert_not_called()

    def test_active_recording_is_not_doubled(self):
        job = mock.Mock(stream_id=self.sid, end=datetime.now(), rate_kbps=128)
        self.m.jobs[99] = job
        self._poll(True)
        self.m._start_job.assert_not_called()

    def _finish(self, reason):
        job = recorder.RecordingJob(self.m, 7, "http://x", datetime.now(), self.sid, auto=True)
        job.finish_reason = reason
        self.m.jobs[7] = job
        db.execute("INSERT INTO recordings (id,stream_id,title,started_at,status,created_at) "
                   "VALUES (7,?,'t',?,'recording',?)", (self.sid, db.now_iso(), db.now_iso()))
        self.m._job_finished(7)

    def test_after_max_length_waits_until_broadcast_has_ended(self):
        self._finish("end")
        self.assertEqual(self._flag(), 1)
        self._poll(True)  # same broadcast continues: no new recording
        self.m._start_job.assert_not_called()
        self._poll(False)  # broadcast ended
        self.assertEqual(self._flag(), 0)
        self._poll(True)  # next broadcast
        self.m._start_job.assert_called_once()

    def test_manual_stop_does_not_restart_same_broadcast(self):
        self._finish("stop")
        self.assertEqual(self._flag(), 1)

    def test_broadcast_end_allows_next_start(self):
        self._finish("offline")
        self.assertEqual(self._flag(), 0)

    def test_no_space_reported_once_per_broadcast_and_nothing_started(self):
        self.free.return_value = 0
        self._poll(True)
        self._poll(True)
        rows = db.query("SELECT * FROM recordings")
        self.assertEqual([r["status"] for r in rows], ["failed"])
        self.assertIn("Levytila", rows[0]["error"])
        self.m._start_job.assert_not_called()
        self._poll(False)
        self._poll(True)
        self.assertEqual(len(db.query("SELECT id FROM recordings")), 2)

    def test_recording_starts_when_space_is_freed_during_broadcast(self):
        self.free.return_value = 0
        self._poll(True)
        self.free.return_value = 10 ** 13
        self._poll(True)
        self.m._start_job.assert_called_once()


class DiskSpace(Base):
    def setUp(self):
        super().setUp()
        self.m = recorder.Manager()
        self.free = 0
        p = mock.patch.object(recorder, "disk_free", side_effect=lambda: self.free)
        p.start()
        self.addCleanup(p.stop)

    def _rec(self, status="ready"):
        return db.execute("INSERT INTO recordings (title,started_at,status,file_size,created_at) "
                          "VALUES ('t','2026-01-01T10:00:00',?,1000,?)", (status, db.now_iso()))

    def _ids(self):
        return [r["id"] for r in db.query("SELECT id FROM recordings ORDER BY id")]

    def test_no_space_refuses_and_never_deletes_recordings(self):
        ids = [self._rec(), self._rec()]
        with self.assertRaises(recorder.NoSpace):
            self.m.ensure_space(3600)
        self.assertEqual(self._ids(), ids)

    def test_need_accounts_for_duration_and_active_recordings(self):
        self.assertAlmostEqual(recorder.bytes_for(3600, 128), 3600 * (128 + 160) * 125)
        base = self.m.space_needed(3600, 128)
        self.m.jobs[1] = mock.Mock(end=datetime.now() + timedelta(hours=1), rate_kbps=128)
        self.assertGreater(self.m.space_needed(3600, 128), base * 1.1)

    def test_recording_stopped_when_disk_fills_and_nothing_deleted(self):
        old = self._rec()
        job = mock.Mock(rec_id=self._rec(status="recording"))
        self.m.jobs[job.rec_id] = job
        self.m._guard_disk()
        job.request_stop.assert_called_once_with("disk")
        self.assertIn("levy", db.one("SELECT error FROM recordings WHERE id=?", (job.rec_id,))["error"])
        self.assertEqual(self._ids(), [old, job.rec_id])

    def test_enough_space_does_not_stop_recording(self):
        self.free = 10 ** 13
        job = mock.Mock(rec_id=1)
        self.m.jobs[1] = job
        self.m._guard_disk()
        job.request_stop.assert_not_called()


class SilentRecordings(Base):
    def _process(self, duration):
        sid = db.execute("INSERT INTO streams (name,url,wait_offline,created_at) VALUES ('a','http://x',1,?)",
                         (db.now_iso(),))
        rid = db.execute("INSERT INTO recordings (stream_id,title,started_at,status,source,created_at) "
                         "VALUES (?,'t',?,'processing','auto',?)", (sid, db.now_iso(), db.now_iso()))

        def fake(segs, out, *a, **k):
            Path(out).write_bytes(b"x")
            return 14400.0, duration
        with mock.patch.object(processing, "process", side_effect=fake):
            recorder.Manager()._process_one(rid)
        return sid, rid

    def test_fully_silent_is_discarded_and_next_may_start(self):
        sid, rid = self._process(0.3)
        self.assertIsNone(db.one("SELECT id FROM recordings WHERE id=?", (rid,)))
        self.assertEqual(db.one("SELECT wait_offline w FROM streams WHERE id=?", (sid,))["w"], 0)

    def test_short_real_audio_is_kept(self):
        sid, rid = self._process(20.0)
        self.assertEqual(db.one("SELECT status FROM recordings WHERE id=?", (rid,))["status"], "empty")


class Favorites(HttpCase):
    def test_mark_and_filter(self):
        a = db.execute("INSERT INTO recordings (title,started_at,status,created_at) "
                       "VALUES ('a','2026-01-01T10:00:00','ready',?)", (db.now_iso(),))
        b = db.execute("INSERT INTO recordings (title,started_at,status,created_at) "
                       "VALUES ('b','2026-01-02T10:00:00','ready',?)", (db.now_iso(),))
        status, data = self.req("PUT", f"/api/recordings/{a}", {"keep": True})
        self.assertEqual((status, json.loads(data)["keep"]), (200, 1))
        ids = lambda q: [r["id"] for r in json.loads(self.req("GET", f"/api/recordings{q}")[1])["items"]]
        self.assertEqual(ids("?keep=1"), [a])
        self.assertEqual(ids(""), [b, a])
        self.req("PUT", f"/api/recordings/{a}", {"keep": False})
        self.assertEqual(ids("?keep=1"), [])

    def test_retention_does_not_touch_favorite(self):
        sid = db.execute("INSERT INTO streams (name,url,created_at) VALUES ('a','http://x',?)", (db.now_iso(),))
        sc = db.execute("INSERT INTO schedules (stream_id,title,start_time,duration_min,keep_days,created_at) "
                        "VALUES (?,'t','10:00',30,1,?)", (sid, db.now_iso()))
        rows = [db.execute("INSERT INTO recordings (schedule_id,title,started_at,status,keep,created_at) "
                           "VALUES (?,'t','2020-01-01T10:00:00','ready',?,?)", (sc, k, db.now_iso())) for k in (1, 0)]
        recorder.Manager().cleanup()
        self.assertEqual([r["id"] for r in db.query("SELECT id FROM recordings")], rows[:1])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg puuttuu")
class EventStream(unittest.TestCase):
    def test_fifteen_minutes_of_leading_silence_removed(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        seg = d / "seg000.mka"
        make_segment(seg, "anullsrc=r=44100:cl=mono:d=900", "sine=440:d=30")
        raw, dur = processing.process([seg], d / "o.mp3", "Tilaisuus", -50, 3)
        self.assertAlmostEqual(raw, 930, delta=1)
        self.assertAlmostEqual(dur, 30, delta=1)


class RecordingList(HttpCase):
    def setUp(self):
        super().setUp()
        self.s1 = db.execute("INSERT INTO streams (name,url,created_at) VALUES ('Alfa','http://a',?)", (db.now_iso(),))
        self.s2 = db.execute("INSERT INTO streams (name,url,created_at) VALUES ('Beta','http://b',?)", (db.now_iso(),))
        self.a = self._rec("Cee", "2026-01-10T10:00:00", self.s1, "auto", 100, 1000)
        self.b = self._rec("Aaa", "2026-02-10T23:30:00", self.s2, "manual", 300, 500)
        self.c = self._rec("bee", "2026-03-10T10:00:00", self.s1, "schedule", 200, 2000)
        self.d = self._rec("Käynnissä", "2026-04-10T10:00:00", self.s2, "auto", None, None, status="recording")

    def _rec(self, title, started, sid, source, dur, size, status="ready", keep=0):
        return db.execute(
            "INSERT INTO recordings (stream_id,title,started_at,status,source,duration_s,file_size,keep,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (sid, title, started, status, source, dur, size, keep, db.now_iso()))

    def _ids(self, query):
        status, data = self.req("GET", f"/api/recordings{query}")
        self.assertEqual(status, 200)
        return [r["id"] for r in json.loads(data)["items"]]

    def test_filter_by_stream_and_source(self):
        self.assertEqual(self._ids(f"?stream_id={self.s1}"), [self.c, self.a])
        self.assertEqual(self._ids("?source=auto"), [self.d, self.a])
        self.assertEqual(self._ids(f"?source=auto&stream_id={self.s1}"), [self.a])

    def test_filter_by_date_includes_both_end_days(self):
        self.assertEqual(self._ids("?from=2026-02-10&to=2026-03-10"), [self.c, self.b])
        self.assertEqual(self._ids("?to=2026-02-10"), [self.b, self.a])
        self.assertEqual(self._ids("?from=2026-04-01"), [self.d])

    def test_sorting(self):
        self.assertEqual(self._ids("?sort=oldest"), [self.a, self.b, self.c, self.d])
        self.assertEqual(self._ids("?sort=title"), [self.b, self.c, self.a, self.d])
        self.assertEqual(self._ids("?sort=stream"), [self.c, self.a, self.d, self.b])
        self.assertEqual(self._ids("?sort=longest"), [self.b, self.c, self.a, self.d])
        self.assertEqual(self._ids("?sort=shortest"), [self.a, self.c, self.b, self.d])
        self.assertEqual(self._ids("?sort=largest"), [self.c, self.a, self.b, self.d])
        self.assertEqual(self._ids("?sort=smallest"), [self.b, self.a, self.c, self.d])

    def test_unknown_sort_rejected(self):
        self.assertEqual(self.req("GET", "/api/recordings?sort=r.id;DROP")[0], 400)

    def _delete(self, ids):
        status, data = self.req("POST", "/api/recordings/delete", {"ids": ids})
        return status, json.loads(data)

    def test_bulk_delete(self):
        status, res = self._delete([self.a, self.b])
        self.assertEqual((status, sorted(res["deleted"]), res["skipped"]), (200, sorted([self.a, self.b]), []))
        self.assertEqual(self._ids(""), [self.d, self.c])

    def test_bulk_delete_skips_favorites_active_and_missing(self):
        fav = self._rec("Suosikki", "2026-05-01T10:00:00", self.s1, "auto", 10, 10, keep=1)
        self.mgr.processing_now = self.c
        status, res = self._delete([self.a, fav, self.d, self.c, 99999, self.a])
        self.assertEqual(res["deleted"], [self.a])
        self.assertEqual({x["id"] for x in res["skipped"]}, {fav, self.d, self.c, 99999})
        self.assertEqual(sorted(self._ids("")), sorted([self.b, self.c, self.d, fav]))

    def test_bulk_delete_marks_schedule_occurrence_skipped(self):
        sc = db.execute("INSERT INTO schedules (stream_id,title,start_time,duration_min,created_at) "
                        "VALUES (?,'t','10:00',30,?)", (self.s1, db.now_iso()))
        db.update("recordings", self.a, {"schedule_id": sc, "scheduled_start": "2026-01-10T10:00:00"})
        self._delete([self.a])
        self.mgr.skip_occurrence.assert_called_once_with(sc, "2026-01-10T10:00:00")

    def test_bulk_delete_bad_input(self):
        for ids in ([], "1", ["1"], [True], [1.5], list(range(1001))):
            self.assertEqual(self.req("POST", "/api/recordings/delete", {"ids": ids})[0], 400, ids)
        self.assertEqual(self.req("POST", "/api/recordings/delete", {})[0], 400)
