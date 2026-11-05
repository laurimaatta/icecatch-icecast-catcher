"""Post-processing: joining segments, removing silence, encoding.

Silence is everything that is not continuous speech or music: including single sounds
in the middle of a silence and background noise below the threshold.

Error messages end up in recordings.error and are shown in the UI, so they are in Finnish."""
import json
import logging
import re
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger("icecatch.processing")

# Linux limits a single argument to 128 kB. Only a longer filter graph goes through a
# file, because -filter_complex_script is deprecated in ffmpeg 7.
MAX_INLINE_GRAPH = 100_000


def probe_duration(path):
    """Returns the file duration in seconds, or None."""
    try:
        out = subprocess.run(
            [config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, errors="replace", timeout=120,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return None


TIME_RX = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def segment_duration(path):
    """True segment duration from its packets (no decoding).

    The header cannot be trusted: a file cut short by a crash reports ffmpeg's -t
    value as its duration, or nothing at all."""
    res = subprocess.run(
        [config.FFMPEG, "-hide_banner", "-nostdin", "-i", str(path),
         "-map", "0:a:0", "-c", "copy", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    times = TIME_RX.findall(res.stderr)
    if res.returncode != 0 or not times:
        return 0.0
    h, m, s = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


# Audio level is measured in blocks this long (s). Mean level, not peak: a single
# click barely raises the mean level of half a second.
BLOCK_S = 0.5
# Sound breaks a silence only when it lasts this long without a break (s).
# Shorter sounds (a cough, a thud, a click) are part of the silence.
MIN_SOUND_S = 2.0

RMS_RX = re.compile(r"RMS_level=(-?[\d.]+|-inf|inf|nan)")


def quiet_spans(levels, threshold_db, block_s=BLOCK_S, min_sound_s=MIN_SOUND_S):
    """Finds the stretches without programme. Returns [(start, end|None), ...] in seconds.

    levels = mean levels (dBFS) of consecutive blocks, block_s seconds each.
    Programme is sound louder than the threshold for at least min_sound_s seconds
    (a one-block dip does not break it). Everything else is silence."""
    n = len(levels)
    active = [lv > threshold_db for lv in levels]
    for i in range(1, n - 1):        # dips between syllables do not break the sound
        if not active[i] and active[i - 1] and active[i + 1]:
            active[i] = True
    need = max(1, round(min_sound_s / block_s))
    program = [False] * n
    i = 0
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j < n and active[j]:
            j += 1
        if j - i >= need:
            program[i:j] = [True] * (j - i)
        i = j
    spans, start = [], None
    for i, prog in enumerate(program):
        if not prog and start is None:
            start = i
        elif prog and start is not None:
            spans.append((round(start * block_s, 3), round(i * block_s, 3)))
            start = None
    if start is not None:
        spans.append((round(start * block_s, 3), None))
    return spans


def silence_cuts(silences, total, min_silence_s, keep_s=1.0):
    """Turns detected silences into time ranges to cut.

    Leading silence is removed almost entirely; of silences in the middle keep_s
    seconds are kept (half on each side); trailing silence keeps half of that."""
    keep = min(keep_s, min_silence_s / 2)
    cuts = []
    for start, end in silences:
        if end is None:
            end = total
        if end - start < min_silence_s:
            continue
        if start <= 0.05:            # start
            a, b = 0.0, end - 0.3
        elif end >= total - 0.05:    # end
            a, b = start + keep / 2, total + 1
        else:
            a, b = start + keep / 2, end - keep / 2
        if b > a:
            cuts.append((round(a, 3), round(b, 3)))
    return cuts


def output_ext():
    return "opus" if config.OUTPUT_FORMAT == "opus" else "mp3"


def _inputs_and_concat(segments):
    """ffmpeg inputs and the start of the filter graph: all segments in the same format,
    one after another -> works even if stream parameters change after an outage."""
    args = []
    for s in segments:
        args += ["-i", str(s)]
    parts = [f"[{i}:a:0]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[s{i}]"
             for i in range(len(segments))]
    chain = "".join(f"[s{i}]" for i in range(len(segments))) + f"concat=n={len(segments)}:v=0:a=1"
    return args, parts, chain


def measure_levels(segments, block_s=BLOCK_S):
    """Measures the mean level (RMS, dBFS) of the joined audio in blocks of block_s seconds."""
    args, parts, chain = _inputs_and_concat(segments)
    n = int(44100 * block_s)
    graph = ";".join(parts + [chain + f",asetnsamples=n={n}:p=0,astats=metadata=1:reset=1,"
                              "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level[out]"])
    cmd = [config.FFMPEG, "-hide_banner", "-nostats", "-y", *args,
           "-filter_complex", graph, "-map", "[out]", "-f", "null", "-"]
    # errors="replace": ffmpeg prints station tags as raw bytes (often Latin-1)
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        raise RuntimeError("Hiljaisuuden tunnistus epäonnistui: " + res.stderr[-500:])
    levels = []
    for m in RMS_RX.finditer(res.stderr):
        try:
            v = float(m.group(1))
        except ValueError:
            v = -120.0
        levels.append(v if v == v and v > -120 else -120.0)   # -inf and nan = digital silence
    return levels


def detect_silences(segments, threshold_db):
    """Pass 1: finds the stretches without programme. Returns [(start, end|None), ...]."""
    return quiet_spans(measure_levels(segments), threshold_db)


def process(segments, out_path, title, threshold_db=-50, min_silence_s=None, artist=""):
    """Joins the segments, removes silences and encodes into out_path.

    Returns (raw_duration_s, duration_s). Raises RuntimeError on failure.
    """
    segments = [Path(s) for s in segments if Path(s).exists() and Path(s).stat().st_size > 0]
    if not segments:
        raise RuntimeError("Ei tallennettua ääntä (striimi ei vastannut?)")

    # A header-only or broken segment must not ruin the rest of the recording
    durations = {s: segment_duration(s) for s in segments}
    segments = [s for s in segments if durations[s] > 0]
    if not segments:
        raise RuntimeError("Ei tallennettua ääntä (striimi ei vastannut?)")
    raw = sum(durations[s] for s in segments)

    if min_silence_s is None:
        min_silence_s = config.SILENCE_MIN_S
    cuts = []
    if min_silence_s > 0:
        cuts = silence_cuts(detect_silences(segments, threshold_db), raw, min_silence_s)
        log.info("Cutting %d quiet stretches (%.0f s)", len(cuts),
                 sum(min(b, raw) - a for a, b in cuts))

    args, parts, chain = _inputs_and_concat(segments)
    if cuts:
        expr = "+".join(f"between(t,{a},{b})" for a, b in cuts)
        chain += f",aselect='not({expr})',asetpts=N/SR/TB"
    if config.OUTPUT_FORMAT == "opus":
        chain += ",aresample=48000"
    graph = ";".join(parts + [chain + "[out]"])

    tmp = Path(str(out_path) + ".part")
    graph_file = Path(str(out_path) + ".filter")
    if len(graph) < MAX_INLINE_GRAPH:
        graph_args = ["-filter_complex", graph]
    else:
        graph_file.write_text(graph)
        graph_args = ["-filter_complex_script", str(graph_file)]
    cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args,
           *graph_args, "-map", "[out]", "-map_metadata", "-1"]
    if config.OUTPUT_FORMAT == "opus":
        cmd += ["-c:a", "libopus", "-b:a", config.OUTPUT_BITRATE]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", config.OUTPUT_BITRATE, "-id3v2_version", "3"]
    cmd += ["-metadata", f"title={title}"]
    if artist:
        cmd += ["-metadata", f"artist={artist}"]
    cmd += ["-f", "opus" if config.OUTPUT_FORMAT == "opus" else "mp3", str(tmp)]
    log.info("Processing %d segments -> %s", len(segments), out_path)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    finally:
        graph_file.unlink(missing_ok=True)
    if res.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg epäonnistui: " + (res.stderr or "")[-800:])
    tmp.replace(out_path)
    return raw, probe_duration(out_path) or 0.0
