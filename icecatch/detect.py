"""Broadcast detection: is the stream's mount up?

An Icecast mount exists only while a source is connected. Audio level is not checked,
because event streams may start with a long silence and must still be recorded.
Quiet parts are removed in post-processing.
"""
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from . import __version__

USER_AGENT = f"IceCatch/{__version__}"


@dataclass
class Broadcast:
    on_air: bool
    rate_kbps: Optional[int] = None


def check(url, timeout=8):
    """Does the mount answer with audio? The connection is closed at once; no audio is read."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # The certificate is not verified, because ffmpeg does not verify it when recording
    # either: a self-signed Icecast must not look permanently off the air.
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            if r.headers.get("Content-Type", "").lower().startswith("text/"):
                return Broadcast(False)  # the server's HTML page, not a stream
            try:
                rate = int(r.headers.get("icy-br", "").split(",")[0])
            except ValueError:
                rate = None
            return Broadcast(True, rate)
    except (urllib.error.URLError, OSError, ValueError):
        return Broadcast(False)
