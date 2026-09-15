"""Run Jellyfin in a container instead of building it from source.

Podman and Docker take the same arguments for everything used here, so one
implementation covers both; `--runtime` only picks which binary to invoke.

The container sees the library at `/media`, which is not where this machine
sees it — so provisioning has to send the server *its* path, not ours. That is
what `media_root` is for, and getting it wrong produces libraries that are
created successfully and then scan to zero items.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from . import jfserver

RUNTIMES = ("podman", "docker")

# `latest` on purpose: the usual reason to test against a container rather than
# a source build is "does the client still work with what people are actually
# running". Pin with --image when you need a fixed target.
DEFAULT_IMAGE = "docker.io/jellyfin/jellyfin:latest"
DEFAULT_NAME = "stdjflib-jellyfin"

# Where the official image expects things.
MEDIA_MOUNT = "/media"
CONFIG_MOUNT = "/config"
CACHE_MOUNT = "/cache"


class ContainerError(RuntimeError):
    pass


def available(runtime: str) -> bool:
    return shutil.which(runtime) is not None


def pick_runtime(preferred: str | None = None) -> str:
    if preferred:
        if not available(preferred):
            raise ContainerError(f"{preferred} is not on PATH")
        return preferred
    for runtime in RUNTIMES:
        if available(runtime):
            return runtime
    raise ContainerError("neither podman nor docker is on PATH")


def selinux_enabled() -> bool:
    """Whether volume mounts need a relabel suffix.

    `:z` on a system without SELinux is accepted and pointless; on a system
    with it, leaving it off makes every bind mount unreadable inside the
    container for reasons that look like a permissions bug in the image.
    """
    try:
        with open("/sys/fs/selinux/enforce", encoding="ascii") as fh:
            return fh.read().strip() == "1"
    except OSError:
        return False


def _mount(host: str, dest: str, *, read_only: bool = False) -> str:
    flags = ["ro"] if read_only else []
    if selinux_enabled():
        flags.append("z")
    suffix = ":" + ",".join(flags) if flags else ""
    return f"{host}:{dest}{suffix}"


class Container:
    """One Jellyfin container, and the host directories it uses."""

    def __init__(self, library: str, state: str, *, runtime: str = "podman",
                 image: str = DEFAULT_IMAGE, name: str = DEFAULT_NAME,
                 port: int = 8096, extra_args: tuple[str, ...] = (),
                 listen: tuple[str, ...] = (),
                 host_loopback_ports: tuple[int, ...] = (),
                 verbose: bool = False):
        self.library = os.path.abspath(library)
        self.state = os.path.abspath(state)
        self.runtime = runtime
        self.image = image
        self.name = name
        self.port = port
        self.container_port = 8096
        self.listen = ("127.0.0.1",
                       *(a for a in listen if a != "127.0.0.1"))
        self.host_loopback_ports = tuple(host_loopback_ports)
        self.extra_args = tuple(extra_args)
        self.verbose = verbose

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def media_root(self) -> str:
        """The library path *the server* sees. Not the one we see."""
        return MEDIA_MOUNT

    # -- runtime plumbing -------------------------------------------------

    def _run(self, *args: str, check: bool = True,
             capture: bool = True) -> subprocess.CompletedProcess:
        argv = [self.runtime, *args]
        if self.verbose:
            print("  $", " ".join(argv), flush=True)
        proc = subprocess.run(argv, capture_output=capture, text=True,
                              timeout=600)
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise ContainerError(
                f"{' '.join(argv)} failed:\n{detail[-1200:]}{self._hint(detail)}")
        return proc

    def _hint(self, detail: str) -> str:
        """Turn the common runtime failures into something actionable.

        "permission denied ... /var/run/docker.sock" is the one that matters:
        a stock Docker needs root or docker-group membership, and the raw
        message says nothing about either.
        """
        low = detail.lower()
        if "docker.sock" in low and "permission denied" in low:
            return ("\n\nDocker's socket is root-owned on a stock install. "
                    "Either run this under sudo, add yourself to the `docker` "
                    "group, or use `--runtime podman`, which needs neither.")
        if "cannot connect to the docker daemon" in low:
            return "\n\nThe Docker daemon does not appear to be running."
        if "port is already allocated" in low or "address already in use" in low:
            return f"\n\nPort {self.port} is taken; pass --port."
        return ""

    def exists(self) -> bool:
        proc = self._run("container", "inspect", self.name, check=False)
        return proc.returncode == 0

    def running(self) -> bool:
        proc = self._run("container", "inspect", "-f", "{{.State.Running}}",
                         self.name, check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def remove(self) -> None:
        if self.exists():
            # `-v` takes the anonymous volumes the image's VOLUME lines create
            # for whatever is not bind-mounted over them — /config and /cache
            # under `serve` — which would otherwise pile up two per run.
            self._run("rm", "-f", "-v", self.name, check=False)

    def pull(self) -> None:
        self._run("pull", self.image, capture=not self.verbose)

    # -- lifecycle --------------------------------------------------------

    def _volumes(self) -> list[str]:
        return [
            "-v", _mount(os.path.join(self.state, "config"), CONFIG_MOUNT),
            "-v", _mount(os.path.join(self.state, "cache"), CACHE_MOUNT),
            "-v", _mount(self.library, MEDIA_MOUNT, read_only=True),
        ]

    def _command(self) -> list[str]:
        """Arguments after the image, for its entrypoint."""
        return []

    def _prepare_state(self) -> None:
        for sub in ("config", "cache"):
            os.makedirs(os.path.join(self.state, sub), exist_ok=True)

    def argv(self) -> list[str]:
        args = ["run", "-d", "--name", self.name]
        if self.host_loopback_ports:
            # pasta's -T forwards the container's own 127.0.0.1:PORT to the
            # host's, so faketvsource and the origin can listen on loopback and
            # URLs baked as 127.0.0.1 still work inside. Measured: without it
            # the connection is refused. Docker has no equivalent.
            if self.runtime != "podman":
                raise ContainerError("forwarding host loopback into a "
                                     "container needs podman's pasta")
            args += ["--network", "pasta:" + ",".join(
                f"-T,{port}" for port in self.host_loopback_ports)]
        # A bare `-p PORT:8096` publishes on every interface.
        for address in self.listen:
            args += ["-p", f"{address}:{self.port}:{self.container_port}"]
        args += self._volumes()
        args += [
            # The image's own healthcheck curls localhost; nothing here uses
            # it, and on some hosts it spams the journal. Keep the run quiet.
            "--stop-timeout", "30",
        ]
        if self.runtime == "podman":
            # Rootless podman maps container root to the invoking user, so the
            # bind-mounted config directory is writable without keep-id. What
            # it does need is somewhere to write when the image drops
            # privileges, which the official image does not — so nothing more
            # is required here. Left explicit because the alternative
            # (--userns=keep-id) breaks the config mount instead of fixing it.
            pass
        args += list(self.extra_args)
        args.append(self.image)
        args += self._command()
        return args

    def start(self, *, replace: bool = True) -> str:
        self._prepare_state()
        if replace:
            self.remove()
        elif self.exists():
            if self.running():
                return self.name
            self._run("start", self.name)
            return self.name
        proc = self._run(*self.argv())
        return proc.stdout.strip()[:12] or self.name

    def stop(self) -> None:
        if self.running():
            self._run("stop", self.name, check=False)

    def logs(self, lines: int = 30) -> str:
        proc = self._run("logs", "--tail", str(lines), self.name, check=False)
        return ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no logs)"

    def alive(self) -> bool:
        return self.running()

    # -- checks -----------------------------------------------------------

    def check_library_visible(self) -> tuple[bool, str]:
        """Confirm the container can actually read the library.

        Worth doing before provisioning rather than after: a bind mount that
        the container cannot traverse produces libraries that are created
        without error and then scan to nothing, which reads as a Jellyfin
        problem rather than a mount one. FUSE mounts (sshfs among them) are
        the usual cause, and whether they work depends on the runtime, the
        rootless mapping, and whether the mount allows other users.
        """
        probe = [
            "run", "--rm",
            "-v", _mount(self.library, self.media_root, read_only=True),
            "--entrypoint", "/bin/sh", self.image,
            "-c", f"ls {shlex.quote(self.media_root)} | head -20",
        ]
        proc = self._run(*probe, check=False)
        listing = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "").strip()[-400:]
        if not listing:
            return False, (f"the container sees {self.media_root} as empty. "
                           f"If {self.library} is on sshfs or another FUSE "
                           f"mount, the container may not be able to traverse "
                           f"it — build a library on local disk and point at "
                           f"that instead.")
        return True, ", ".join(listing[:8])


class SourceBuiltServer(Container):
    """A `jfbuild` publish, run in the official image in place of its server.

    Every path is mounted where it is on the host — the state, the library,
    the web bundle — and the server gets `jfserver.server_arguments` for them,
    exactly as `serve --on-host` does. So one state directory serves both:
    library paths, item ids (a hash of the path), recorded image paths and the
    admin password mean the same on either side.

    Only the server comes from the build. The runtime libraries and
    jellyfin-ffmpeg are the image's, and its `JELLYFIN_FFMPEG` beats whatever
    `encoding.xml` recorded from an `--on-host` run: `MediaEncoder.
    SetFFmpegPath` takes a command line or environment path before the file.
    """

    def __init__(self, library: str, state: str, publish: str, *,
                 web_dir: str | None = None, port: int = 8096, **kw):
        kw.setdefault("name", f"stdjflib-serve-{port}")
        super().__init__(library, state, port=port, **kw)
        self.publish = os.path.abspath(publish)
        self.web_dir = os.path.abspath(web_dir) if web_dir else None
        self.container_port = port

    @property
    def media_root(self) -> str:
        return self.library

    def _volumes(self) -> list[str]:
        volumes = [
            "-v", _mount(self.state, self.state),
            "-v", _mount(self.library, self.library, read_only=True),
            # Over the image's own server; its entrypoint is /jellyfin/jellyfin.
            "-v", _mount(self.publish, "/jellyfin", read_only=True),
        ]
        if self.web_dir:
            volumes += ["-v", _mount(self.web_dir, self.web_dir, read_only=True)]
        return volumes

    def _command(self) -> list[str]:
        return jfserver.server_arguments(self.state, self.web_dir, None)

    def _prepare_state(self) -> None:
        for sub in ("data", "config", "cache", "log"):
            os.makedirs(os.path.join(self.state, sub), exist_ok=True)
        self.container_port = jfserver.write_network_config(self.state,
                                                            self.port, ())

    @property
    def log_path(self) -> str:
        """The server's own log directory, mounted where it is on the host."""
        return os.path.join(self.state, "log")

    def log_tail(self, lines: int = 25) -> str:
        return self.logs(lines)

    def stop(self) -> None:
        super().stop()
        self.remove()
