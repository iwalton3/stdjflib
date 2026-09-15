"""Build and run a Jellyfin server from source, in a throwaway instance.

Two rules shape this:

**Nothing is written into the Jellyfin checkout.** Builds go to a separate
`--artifacts-path`, which also sidesteps the common case of a checkout with
root-owned `obj/` directories from an old build — there are 42 of them in the
tree this was written against, and a plain `dotnet build` dies on the first
one with "Permission denied" and a path that does not obviously explain why.

**The instance is disposable.** Data, config, cache and logs all go under one
directory that can be deleted to get a factory-fresh server, which is the
state most worth being able to reach on demand.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from xml.etree import ElementTree

DEFAULT_PORT = 8096


def find_dotnet() -> str | None:
    return shutil.which("dotnet")


def server_project(source: str) -> str:
    return os.path.join(source, "Jellyfin.Server", "Jellyfin.Server.csproj")


def looks_like_jellyfin(source: str) -> bool:
    return os.path.exists(server_project(source))


def build(source: str, artifacts: str, *, configuration: str = "Debug",
          verbose: bool = False) -> str:
    """Compile the server and return the path to jellyfin.dll."""
    dotnet = find_dotnet()
    if not dotnet:
        raise RuntimeError("dotnet is not on PATH; cannot build from source")
    if not looks_like_jellyfin(source):
        raise RuntimeError(f"{source} does not look like a Jellyfin checkout "
                           f"(no Jellyfin.Server/Jellyfin.Server.csproj)")

    argv = [dotnet, "build", server_project(source), "-c", configuration,
            "--artifacts-path", artifacts]
    if not verbose:
        argv += ["--verbosity", "quiet", "--nologo"]
    proc = subprocess.run(argv, capture_output=not verbose, text=True)
    if proc.returncode != 0:
        tail = (proc.stdout or "") + (proc.stderr or "")
        raise RuntimeError("jellyfin build failed:\n"
                           + "\n".join(tail.strip().splitlines()[-25:]))
    return dll_path(artifacts, configuration)


def dll_path(artifacts: str, configuration: str = "Debug") -> str:
    return os.path.join(artifacts, "bin", "Jellyfin.Server",
                        configuration.lower(), "jellyfin.dll")


def find_web_client(source: str, extra: str | None = None) -> str | None:
    """A built jellyfin-web, if there is one to be found.

    Entirely optional: the API is what a client talks to, and the web UI needs
    an npm build that has nothing to do with testing one. Without it the
    server runs with `--nowebclient`.

    `extra` is looked at first and is where `web.py` puts a bundle it built in
    a container. A `dist/` already sitting in the checkout wins over nothing
    but loses to that, because the container build is the one whose provenance
    this tool knows — an existing `dist/` was produced by an npm run somebody
    else made, under rules nobody here can see.
    """
    candidates = [
        extra,
        os.path.join(os.path.dirname(source.rstrip("/")), "jellyfin-web", "dist"),
        os.path.join(source, "web"),
        "/usr/share/jellyfin/web",
    ]
    for path in candidates:
        if path and os.path.isdir(path) and os.path.exists(
                os.path.join(path, "index.html")):
            return path
    return None


def server_arguments(state: str, web_dir: str | None,
                     ffmpeg: str | None) -> list[str]:
    """What the server is told, identically on the host and in a container.

    Identical on purpose: the container mounts every one of these paths at
    the place it has here, so one state directory works under either.
    """
    args = [
        "--datadir", os.path.join(state, "data"),
        "--configdir", os.path.join(state, "config"),
        "--cachedir", os.path.join(state, "cache"),
        "--logdir", os.path.join(state, "log"),
        "--nonetchange",
    ]
    if web_dir:
        args += ["--webdir", web_dir]
    else:
        args.append("--nowebclient")
    if ffmpeg:
        args += ["--ffmpeg", ffmpeg]
    return args


def write_network_config(state: str, port: int,
                         addresses: tuple[str, ...]) -> int:
    """Pin the port before first start, and the bind addresses every start.

    Returns the port the server will listen on, which is the file's rather
    than `port` once a server has written it.

    The server writes `network.xml` on first run and then treats it as the
    source of truth, so setting the port afterwards means restarting.

    The addresses are rewritten into an existing file too, because that is
    the only thing that decides them: Kestrel listens on
    `NetworkManager.GetAllBindInterfaces`, which is `LocalNetworkAddresses`
    or, when that is empty, every interface. A `Kestrel__*` environment
    variable is never consulted. An admin can install a plugin, so an
    existing state directory is corrected rather than left on the LAN.

    In a container `addresses` must be empty and the publish decides who can
    connect. Measured: bound to 127.0.0.1 inside the container's namespace,
    every connection through a `-p 127.0.0.1:...` publish is reset.
    """
    path = os.path.join(state, "config", "network.xml")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<NetworkConfiguration '
                'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
                f"  <InternalHttpPort>{port}</InternalHttpPort>\n"
                f"  <PublicHttpPort>{port}</PublicHttpPort>\n"
                "  <EnableHttps>false</EnableHttps>\n"
                "  <RequireHttps>false</RequireHttps>\n"
                "  <AutoDiscovery>false</AutoDiscovery>\n"
                "  <EnableUPnP>false</EnableUPnP>\n"
                "  <EnableRemoteAccess>true</EnableRemoteAccess>\n"
                "  <LocalNetworkAddresses />\n"
                "</NetworkConfiguration>\n")

    tree = ElementTree.parse(path)
    element = tree.getroot().find("LocalNetworkAddresses")
    if element is None:
        element = ElementTree.SubElement(tree.getroot(), "LocalNetworkAddresses")
    element.clear()
    for address in addresses:
        ElementTree.SubElement(element, "string").text = address
    tree.write(path, encoding="utf-8", xml_declaration=True)

    internal = tree.getroot().findtext("InternalHttpPort")
    return int(internal) if internal and internal.strip().isdigit() else port


class Instance:
    """A running server, and the directories it owns."""

    def __init__(self, dll: str, state_dir: str, *, port: int = DEFAULT_PORT,
                 web_dir: str | None = None, ffmpeg: str | None = None,
                 listen: tuple[str, ...] = (), verbose: bool = False):
        self.dll = dll
        self.state = state_dir
        self.port = port
        # Loopback always: provisioning talks to 127.0.0.1.
        self.listen = ("127.0.0.1",
                       *(a for a in listen if a != "127.0.0.1"))
        self.web_dir = web_dir
        self.ffmpeg = ffmpeg
        self.verbose = verbose
        self.process: subprocess.Popen | None = None
        self.log_handle = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def log_path(self) -> str:
        return os.path.join(self.state, "server.out")

    def argv(self) -> list[str]:
        return [find_dotnet(), self.dll,
                *server_arguments(self.state, self.web_dir, self.ffmpeg)]

    def start(self) -> None:
        for sub in ("data", "config", "cache", "log"):
            os.makedirs(os.path.join(self.state, sub), exist_ok=True)
        self._write_network_config()
        # Its own first-run detection also keys off the data directory, which
        # is why a fresh state dir is a fresh server.
        self.log_handle = open(self.log_path, "ab")
        self.process = subprocess.Popen(
            self.argv(), stdout=self.log_handle, stderr=subprocess.STDOUT,
            start_new_session=True)

    def _write_network_config(self) -> None:
        write_network_config(self.state, self.port, self.listen)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self, timeout: int = 30) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            # The server runs in its own session, so signal the group — a bare
            # terminate leaves the dotnet host behind holding the port.
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

    def log_tail(self, lines: int = 25) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 16384))
                text = fh.read().decode("utf-8", "replace")
            return "\n".join(text.splitlines()[-lines:])
        except OSError:
            return "(no server log)"


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0
