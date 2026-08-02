"""Optional Live TV, backed by faketvsource.

Live TV is a large part of a Jellyfin client that a media library cannot reach
at all: guide grids, channel logos, timers, recordings, and the tuner
negotiation behind them. faketvsource serves an M3U playlist, an XMLTV guide
and one MPEG-TS stream per channel, so the whole surface becomes testable
without buying a tuner.

Two things here are easy to get wrong and fail quietly.

**M3U and HDHomeRun are different code paths in Jellyfin** — different tuner
host implementations, different discovery, different stream URLs. Testing one
says nothing about the other, so which to use is a knob rather than a default.

**The URL Jellyfin gets must work from where Jellyfin is.** faketvsource runs
on this machine; a containerised server reaching `127.0.0.1` finds itself, not
the tuner. That is what `--public-url` is for, and getting it wrong produces a
tuner that saves without complaint and then serves channels that never play.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _say(msg: str = "") -> None:
    print(msg, flush=True)


DEFAULT_PORT = 8409
DEFAULT_SEED = "stdjflib"
TUNER_TYPES = ("m3u", "hdhomerun")

# Where a container reaches the host. Podman and Docker spell it differently,
# and neither is resolvable outside a container — so this is only ever used
# for the URL handed to a containerised server.
HOST_FROM_CONTAINER = {
    "podman": "host.containers.internal",
    "docker": "host.docker.internal",
}

# Jellyfin's XMLTV provider keyword-matches these to set IsNews/IsSports/
# IsKids/IsMovie on a programme. faketvsource emits categories chosen to hit
# them; passing them explicitly means the flags do not depend on whatever the
# server happens to default to.
NEWS_CATEGORIES = ["News", "News magazine", "Current affairs"]
SPORTS_CATEGORIES = ["Sports", "Sports event", "Sports talk"]
KIDS_CATEGORIES = ["Children", "Kids", "Children's/Youth programmes"]
MOVIE_CATEGORIES = ["Movie", "Film", "Movie / Drama"]


def find_source(hint: str | None = None) -> str | None:
    """Locate a faketvsource checkout."""
    candidates = [hint] if hint else []
    candidates += [
        os.path.expanduser("~/Desktop/faketvsource"),
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "..", "faketvsource"),
        os.path.expanduser("~/faketvsource"),
    ]
    for path in candidates:
        if path and os.path.exists(os.path.join(path, "faketv.py")):
            return os.path.abspath(path)
    return None


class FakeTv:
    """A faketvsource process, run for as long as the server needs it."""

    def __init__(self, source: str, state: str, *, port: int = DEFAULT_PORT,
                 seed: str = DEFAULT_SEED, tuner_count: int = 0,
                 public_host: str | None = None, verbose: bool = False):
        self.source = source
        self.state = state
        self.port = port
        self.seed = seed
        self.tuner_count = tuner_count
        self.public_host = public_host
        self.verbose = verbose
        self.process: subprocess.Popen | None = None
        self.log_handle = None

    @property
    def local_url(self) -> str:
        """Where *we* reach it, for health checks."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def public_url(self) -> str:
        """Where *Jellyfin* reaches it. Not always the same thing."""
        host = self.public_host or "127.0.0.1"
        return f"http://{host}:{self.port}"

    @property
    def log_path(self) -> str:
        return os.path.join(self.state, "faketv.out")

    def argv(self) -> list[str]:
        argv = [sys.executable, os.path.join(self.source, "faketv.py"),
                "--port", str(self.port), "--seed", self.seed,
                "--tuner-count", str(self.tuner_count)]
        if self.public_host:
            # faketvsource builds stream URLs from the Host header unless told
            # otherwise, and a container's Host header is not reachable from
            # the container.
            argv += ["--public-url", self.public_url]
        if self.verbose:
            argv.append("-v")
        return argv

    def start(self) -> None:
        os.makedirs(self.state, exist_ok=True)
        self.log_handle = open(self.log_path, "ab")
        self.process = subprocess.Popen(
            self.argv(), stdout=self.log_handle, stderr=subprocess.STDOUT,
            cwd=self.source, start_new_session=True)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def wait_until_up(self, timeout: int = 60) -> int:
        """Block until the playlist is served. Returns the channel count."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if not self.alive():
                raise RuntimeError(
                    f"faketvsource exited during startup:\n{self.log_tail()}")
            try:
                with urllib.request.urlopen(
                        self.local_url + "/playlist.m3u", timeout=5) as resp:
                    body = resp.read().decode("utf-8", "replace")
                if "#EXTM3U" in body:
                    return body.count("#EXTINF")
                last = "playlist did not look like M3U"
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last = exc
            time.sleep(0.5)
        raise TimeoutError(f"faketvsource did not answer in {timeout}s ({last})")

    def stop(self, timeout: int = 15) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.process.terminate()
            deadline = time.time() + timeout
            while time.time() < deadline and self.process.poll() is None:
                time.sleep(0.2)
            if self.process.poll() is None:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self.process.kill()
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None

    def log_tail(self, lines: int = 20) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                return "\n".join(fh.read().splitlines()[-lines:])
        except OSError:
            return "(no faketvsource log)"


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


# --------------------------------------------------------------------------
# Wiring it into a server
# --------------------------------------------------------------------------

def configure(jf, base_url: str, *, tuner_type: str = "m3u",
              tuner_count: int = 0, replace: bool = True, say=_say) -> dict:
    """Add the tuner and the XMLTV guide, and refresh the listings.

    `base_url` is faketvsource as *the server* can reach it.
    """
    if tuner_type not in TUNER_TYPES:
        raise ValueError(f"tuner type must be one of {TUNER_TYPES}")

    if replace:
        # There is no GET for either of these — `/LiveTv/TunerHosts` answers
        # POST and DELETE only, and asking it for a list returns 405 with an
        # empty body, which says nothing about what went wrong. What already
        # exists is in the Live TV configuration document instead.
        existing = jf.get("/System/Configuration/livetv") or {}
        for tuner in existing.get("TunerHosts") or []:
            jf.request("DELETE", "/LiveTv/TunerHosts",
                       params={"id": tuner["Id"]}, expect_json=False)
        for provider in existing.get("ListingProviders") or []:
            jf.request("DELETE", "/LiveTv/ListingProviders",
                       params={"id": provider["Id"]}, expect_json=False)

    # The HDHomeRun host discovers itself from the base URL; the M3U host wants
    # the playlist directly. Handing either the other one's URL fails at save
    # time, which is at least loud.
    url = base_url if tuner_type == "hdhomerun" else f"{base_url}/playlist.m3u"
    tuner = jf.post("/LiveTv/TunerHosts", body={
        "Type": tuner_type,
        "Url": url,
        "FriendlyName": f"faketvsource ({tuner_type})",
        "TunerCount": tuner_count,
        "AllowHWTranscoding": False,
        "AllowStreamSharing": True,
        "EnableStreamLooping": False,
        "ImportFavoritesOnly": False,
        "IgnoreDts": False,
        "ReadAtNativeFramerate": False,
        "UserAgent": "stdjflib",
    })
    say(f"  tuner: {tuner_type} -> {url}")

    listings = jf.post("/LiveTv/ListingProviders",
                       params={"validateListings": "false",
                               "validateLogin": "false"},
                       body={
                           "Type": "xmltv",
                           "Path": f"{base_url}/guide.xml",
                           "EnableAllTuners": True,
                           "EnabledTuners": [],
                           "NewsCategories": NEWS_CATEGORIES,
                           "SportsCategories": SPORTS_CATEGORIES,
                           "KidsCategories": KIDS_CATEGORIES,
                           "MovieCategories": MOVIE_CATEGORIES,
                           "PreferredLanguage": "eng",
                           "UserAgent": "stdjflib",
                       })
    say(f"  guide: xmltv -> {base_url}/guide.xml")

    return {"tuner": tuner, "listings": listings, "url": base_url,
            "type": tuner_type}


def refresh_guide(jf, *, timeout: int = 600, say=_say) -> bool:
    """Run the guide refresh task and wait for it.

    Channels only appear once this has run — a freshly added tuner shows an
    empty Live TV section until then, which looks like a broken tuner.
    """
    tasks = jf.scheduled_tasks()
    task = next((t for t in tasks if t.get("Key") == "RefreshGuide"), None)
    if task is None:
        say("  ! no RefreshGuide task; is Live TV enabled on this server?")
        return False

    jf.post(f"/ScheduledTasks/Running/{task['Id']}", expect_json=False)
    deadline = time.time() + timeout
    time.sleep(2)
    while time.time() < deadline:
        current = next((t for t in jf.scheduled_tasks()
                        if t.get("Key") == "RefreshGuide"), None)
        if current is None or current.get("State") == "Idle":
            return True
        time.sleep(2)
    return False


def summarise(jf, say=_say) -> dict:
    """Report what the server ended up with."""
    channels = jf.get("/LiveTv/Channels", params={"limit": 1}) or {}
    programs = jf.get("/LiveTv/Programs", params={"limit": 1}) or {}
    counts = {"channels": channels.get("TotalRecordCount", 0),
              "programmes": programs.get("TotalRecordCount", 0)}
    say(f"  {counts['channels']} channels, {counts['programmes']} programmes")
    return counts
