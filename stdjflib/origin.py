"""A local HTTP origin, so a `.strm` fixture can be played with no network.

The stream fixtures that point at archive.org are the honest test — a real
remote host, real TLS, real redirects — and they are useless in CI, on a
metered connection, or on a machine that is deliberately offline. So there is
a second set pointing at this: a couple of generated clips under
`.stdjflib/origin/`, served over HTTP from the same machine, whose URLs are
still remote as far as Jellyfin is concerned. An end-to-end playback test can
use those and touch nothing outside the box.

It is a file server and nothing else, but two details are not optional:

**Range requests have to work.** `SimpleHTTPRequestHandler` ignores `Range`
and answers 200 with the whole body. Jellyfin's probe and every seek ask for a
byte range, and ffmpeg treats a 200 where it asked for 206 as a server that
cannot seek — so playback starts, seeking silently does nothing, and the
fixture tests less than it looks like it does. Hence `_parse_range`, 206, and
`Accept-Ranges` on every response.

**The URL is baked into the `.strm` at build time**, which is the same trap
`livetv.py` documents for faketvsource, arriving from the other direction:
faketvsource is told at startup how the server will reach it, whereas these
files were written earlier and cannot be told anything. A containerised
Jellyfin reaching `127.0.0.1` finds itself. `stdjflib build --stream-origin`
is what writes a different host into them, and `describe_reachability` is what
says so before a scan turns it into a library of items that will not play.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# One past faketvsource's 8409, so the two can run together and a firewall
# rule covering "the stdjflib ports" covers a contiguous pair.
DEFAULT_PORT = 8410

# Under `.stdjflib/`, which is beside the library rather than in it — a folder
# of media inside a library folder would be scanned, and the origin clips
# would appear as items in their own right, which is exactly what a stream
# fixture is not.
DIRNAME = "origin"

# Only what this actually serves. `mimetypes` guesses `.mkv` differently
# depending on the machine's mime.types, and a fixture that plays here and not
# there is worse than one that never plays.
CONTENT_TYPES = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
}

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def directory(root: str) -> str:
    """Where the origin media lives for the library at `root`."""
    from . import config

    return os.path.join(root, config.STATE_DIR, DIRNAME)


def default_base_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}"


def port_of(base_url: str) -> int:
    """The port a recorded base URL asks for, so `serve` binds the right one."""
    tail = base_url.rsplit(":", 1)[-1].split("/", 1)[0]
    return int(tail) if tail.isdigit() else 80


def _parse_range(header: str | None, size: int):
    """`(start, end)` inclusive, `None` for the whole file, `False` if unsatisfiable.

    Only the single-range form is handled. A multipart range response is a
    different content type and nothing in this path ever asks for one, so
    anything with a comma in it is answered whole — which is allowed, and is
    what a server that does not implement it is supposed to do.
    """
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if not match or "," in header:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:
        # `bytes=-500` is the last 500 bytes, not "up to byte 500". Getting
        # this backwards serves the wrong end of the file and looks like
        # corruption rather than a bug.
        length = int(last)
        if length == 0:
            return False
        start = max(0, size - length)
        return start, size - 1
    start = int(first)
    if start >= size:
        return False
    end = int(last) if last else size - 1
    return start, min(end, size - 1)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"       # so keep-alive and 206 behave
    server_version = "stdjflib-origin"

    # Set by `Origin.start`.
    root = ""
    log_lines: list = []

    def log_message(self, fmt, *args):
        # The default writes to stderr, which would interleave with the build
        # and the server's own output. Kept in memory instead, where
        # `Origin.log_tail` can produce it if something did not play.
        line = f"{self.address_string()} {fmt % args}"
        self.log_lines.append(line)
        del self.log_lines[:-200]

    def _resolve(self):
        """The file this request names, or None if it names anything else.

        Flat by design: one directory, no subdirectories, so "is this path
        inside the root" is `dirname == root` and there is no way to spell a
        traversal that survives it.
        """
        name = urllib.parse.unquote(self.path.split("?", 1)[0].lstrip("/"))
        if not name or "/" in name or "\\" in name:
            return None
        path = os.path.join(self.root, name)
        if not os.path.isfile(path):
            return None
        return path

    def _fail(self, status: int, size: int | None = None):
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        if size is not None:
            self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve(self, *, body: bool):
        path = self._resolve()
        if path is None:
            self._fail(404)
            return
        size = os.path.getsize(path)
        span = _parse_range(self.headers.get("Range"), size)
        if span is False:
            self._fail(416, size)
            return

        start, end = (0, size - 1) if span is None else span
        length = end - start + 1
        self.send_response(206 if span is not None else 200)
        self.send_header("Content-Type", CONTENT_TYPES.get(
            os.path.splitext(path)[1].lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if span is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not body:
            return

        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # A client that stopped playing mid-file. Ordinary, and
                    # not worth a traceback in the log.
                    return
                remaining -= len(chunk)

    def do_GET(self):
        self._serve(body=True)

    def do_HEAD(self):
        self._serve(body=False)


class Origin:
    """The origin server, on a daemon thread for as long as the caller wants it.

    A thread rather than a child process, deliberately: every other long-lived
    thing here is a subprocess in its own session and needs the
    `cli._stop_on_signals` treatment to be cleaned up, whereas a daemon thread
    cannot outlive the interpreter and so cannot leave a port held after a
    `kill`. There is nothing to reap.
    """

    def __init__(self, root: str, *, port: int = DEFAULT_PORT,
                 bind: str = "0.0.0.0"):
        self.root = directory(root)
        self.port = port
        self.bind = bind
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.log_lines: list = []

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def files(self) -> list[str]:
        try:
            return sorted(name for name in os.listdir(self.root)
                          if os.path.isfile(os.path.join(self.root, name)))
        except OSError:
            return []

    def start(self) -> None:
        handler = type("_BoundHandler", (_Handler,),
                       {"root": self.root, "log_lines": self.log_lines})
        # Bind on 0.0.0.0 so a container reaching in through
        # host.containers.internal finds it; loopback-only would work for a
        # local server and fail for every other topology.
        self.httpd = ThreadingHTTPServer((self.bind, self.port), handler)
        self.httpd.daemon_threads = True
        # Port 0 means "any free one", which is what a test wants and what a
        # `.strm` can never use — the URL was written before this ran. Read it
        # back so `local_url` is true either way.
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="stdjflib-origin", daemon=True)
        self.thread.start()

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.thread = None

    def log_tail(self, lines: int = 20) -> str:
        return "\n".join(self.log_lines[-lines:]) or "(no origin requests)"


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def describe_reachability(base_url: str, *, from_container: str | None = None):
    """Whether a server will be able to fetch from `base_url`. `(ok, why)`.

    This is the check that stops a containerised run from scanning a library
    of items that resolve, look right, and never play — the same failure
    `livetv.py` describes, except that here the URL was written into the files
    at build time and cannot be corrected at startup.
    """
    host = base_url.split("://", 1)[-1].split(":", 1)[0].split("/", 1)[0]
    loopback = host in ("127.0.0.1", "localhost", "::1", "[::1]")
    if from_container and loopback:
        return False, (
            f"the stream fixtures name {base_url}, and inside a container "
            f"that address is the container itself. Rebuild with "
            f"`--stream-origin http://{from_container}:{port_of(base_url)}` "
            f"for the origin fixtures to play.")
    return True, ""


def reachable(base_url: str, name: str, timeout: float = 5.0) -> bool:
    """Whether one origin file actually answers. Used to skip, never to fail."""
    request = urllib.request.Request(f"{base_url}/{urllib.parse.quote(name)}",
                                     method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False
