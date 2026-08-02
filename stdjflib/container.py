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
import shutil
import subprocess

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
                 verbose: bool = False):
        self.library = os.path.abspath(library)
        self.state = os.path.abspath(state)
        self.runtime = runtime
        self.image = image
        self.name = name
        self.port = port
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
            self._run("rm", "-f", self.name, check=False)

    def pull(self) -> None:
        self._run("pull", self.image, capture=not self.verbose)

    # -- lifecycle --------------------------------------------------------

    def argv(self) -> list[str]:
        args = [
            "run", "-d", "--name", self.name,
            "-p", f"{self.port}:8096",
            "-v", _mount(os.path.join(self.state, "config"), CONFIG_MOUNT),
            "-v", _mount(os.path.join(self.state, "cache"), CACHE_MOUNT),
            "-v", _mount(self.library, MEDIA_MOUNT, read_only=True),
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
        return args

    def start(self, *, replace: bool = True) -> str:
        for sub in ("config", "cache"):
            os.makedirs(os.path.join(self.state, sub), exist_ok=True)
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
            "-v", _mount(self.library, MEDIA_MOUNT, read_only=True),
            "--entrypoint", "/bin/sh", self.image,
            "-c", f"ls {MEDIA_MOUNT} | head -20",
        ]
        proc = self._run(*probe, check=False)
        listing = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "").strip()[-400:]
        if not listing:
            return False, (f"the container sees {MEDIA_MOUNT} as empty. "
                           f"If {self.library} is on sshfs or another FUSE "
                           f"mount, the container may not be able to traverse "
                           f"it — build a library on local disk and point at "
                           f"that instead.")
        return True, ", ".join(listing[:8])
