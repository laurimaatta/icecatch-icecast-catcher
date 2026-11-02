"""Tallenteen jälkikäsittely: segmenttien yhdistäminen, hiljaisuuden poisto, koodaus."""
import json
import logging
import re
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger("arkisto.processing")


def probe_duration(path):
    """Palauttaa tiedoston keston sekunteina tai None."""
    try:
        out = subprocess.run(
            [config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return None


SILENCE_RX = re.compile(r"silence_(start|end): (-?[\d.]+)")


def silence_cuts(silences, total, min_silence_s, keep_s=1.0):
    """Muuntaa havaitut hiljaisuudet poistettaviksi aikaväleiksi.

    Alun hiljaisuus poistetaan lähes kokonaan, keskellä olevista jätetään
    keep_s sekuntia (puolet kummallekin puolelle), lopun hiljaisuudesta myös."""
    keep = min(keep_s, min_silence_s / 2)
    cuts = []
    for start, end in silences:
        if end is None:
            end = total
        if end - start < min_silence_s:
            continue
        if start <= 0.05:            # alku
            a, b = 0.0, end - 0.3
        elif end >= total - 0.05:    # loppu
            a, b = start + keep / 2, total + 1
        else:
            a, b = start + keep / 2, end - keep / 2
        if b > a:
            cuts.append((round(a, 3), round(b, 3)))
    return cuts


def output_ext():
    return "opus" if config.OUTPUT_FORMAT == "opus" else "mp3"


def _inputs_and_concat(segments):
    """ffmpeg-syötteet ja suodinketjun alku: kaikki segmentit samaan muotoon
    ja peräkkäin -> kestää vaikka striimin parametrit vaihtuisivat katkon jälkeen."""
    args = []
    for s in segments:
        args += ["-i", str(s)]
    parts = [f"[{i}:a:0]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[s{i}]"
             for i in range(len(segments))]
    chain = "".join(f"[s{i}]" for i in range(len(segments))) + f"concat=n={len(segments)}:v=0:a=1"
    return args, parts, chain


def detect_silences(segments, threshold_db, min_silence_s):
    """Vaihe 1: etsii hiljaiset kohdat. Palauttaa [(alku, loppu|None), ...]."""
    args, parts, chain = _inputs_and_concat(segments)
    graph = ";".join(parts + [chain + f",silencedetect=n={threshold_db}dB:d={min_silence_s}[out]"])
    cmd = [config.FFMPEG, "-hide_banner", "-nostats", "-y", *args,
           "-filter_complex", graph, "-map", "[out]", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("Hiljaisuuden tunnistus epäonnistui: " + res.stderr[-500:])
    out, cur = [], None
    for kind, val in SILENCE_RX.findall(res.stderr):
        if kind == "start":
            cur = max(0.0, float(val))
        elif cur is not None:
            out.append((cur, float(val)))
            cur = None
    if cur is not None:
        out.append((cur, None))
    return out


def process(segments, out_path, title, threshold_db=-50, min_silence_s=3, artist=""):
    """Yhdistää segmentit, poistaa hiljaisuudet ja koodaa out_path-tiedostoon.

    Palauttaa (raw_duration_s, duration_s). Heittää RuntimeErrorin virheessä.
    """
    segments = [Path(s) for s in segments if Path(s).exists() and Path(s).stat().st_size > 0]
    if not segments:
        raise RuntimeError("Ei tallennettua ääntä (striimi ei vastannut?)")

    raw = sum(probe_duration(s) or 0 for s in segments)

    cuts = []
    if min_silence_s and min_silence_s > 0:
        cuts = silence_cuts(detect_silences(segments, threshold_db, min_silence_s), raw, min_silence_s)
        log.info("Hiljaisia kohtia poistetaan %d (%.0f s)", len(cuts),
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
    graph_file.write_text(graph)
    cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args,
           "-filter_complex_script", str(graph_file), "-map", "[out]", "-map_metadata", "-1"]
    if config.OUTPUT_FORMAT == "opus":
        cmd += ["-c:a", "libopus", "-b:a", config.OUTPUT_BITRATE]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", config.OUTPUT_BITRATE, "-id3v2_version", "3"]
    cmd += ["-metadata", f"title={title}"]
    if artist:
        cmd += ["-metadata", f"artist={artist}"]
    cmd += ["-f", "opus" if config.OUTPUT_FORMAT == "opus" else "mp3", str(tmp)]
    log.info("Käsitellään %d segmenttiä -> %s", len(segments), out_path)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        graph_file.unlink(missing_ok=True)
    if res.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg epäonnistui: " + (res.stderr or "")[-800:])
    tmp.replace(out_path)
    return raw, probe_duration(out_path) or 0.0
