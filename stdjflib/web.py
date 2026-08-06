"""Building jellyfin-web in a container, so npm never runs on this machine.

The browser UI is not needed to test a client — the API is what a client
talks to, and `serve` ran with `--nowebclient` for a long time — but it is
needed to *look* at the library, and building it means `npm ci` over about a
hundred and thirty packages with install scripts that run as you. That is the
one part of this tool that would execute somebody else's code, and the whole
project exists to avoid depending on things that can rugpull.

So it happens in a container, and the isolation is the point rather than a
convenience:

- the checkout is mounted **read-only** and copied to a scratch directory
  inside the container, so nothing a package does on install can reach the
  git repository, the library, or anything else on the host;
- the only writable mount is the output directory, which lives under
  `config.runtime_dir()` beside the server's other disposable artifacts and
  never inside the library;
- capabilities are dropped and privilege escalation is off. The network stays
  on, because `npm ci` cannot work without it — that is the risk being
  contained, not removed.

**Podman, and deliberately not Docker.** They are interchangeable in
`container.py`, where the job is to run a published image. They are not
interchangeable here: rootless podman runs the build in a user namespace as an
unprivileged user, and talking to a rootful Docker daemon would run it as root
on the host — strictly worse than running npm normally, which is what this is
for. With no podman the fallback is to build nothing and say so.

Not implemented on purpose: downloading a prebuilt bundle from jellyfin-web's
CI. That is the same trust decision as running the install scripts, minus the
ability to see what went in — it would undo the reason this module exists.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

# Pinned to a major that satisfies jellyfin-web's `engines` (node >= 24), on
# Debian rather than Alpine because several of the toolchain's packages ship
# prebuilt glibc binaries and fall back to compiling from source on musl.
IMAGE = "docker.io/library/node:24-bookworm-slim"

# What the container does. Written out here rather than inlined so it can be
# read: copy the checkout out of the read-only mount, install, build, and
# hand back only `dist`.
#
# `--ignore-scripts` is the mitigation that matters most and it is applied to
# `npm ci` alone: it stops every dependency's install hook, which is where a
# compromised package does its work. jellyfin-web's own build is a webpack run
# and needs no install hook of its own.
# `/tmp/build` and not `/build`: the image's `/` is `dr-xr-xr-x`, and dropping
# every capability takes CAP_DAC_OVERRIDE with them, so root cannot write there
# — the failure is `mkdir: cannot create directory '/build': Permission denied`
# and it reads like a podman fault rather than the hardening working.
SCRIPT = r"""
set -eu
mkdir -p /tmp/build
tar -C /src --exclude=./.git --exclude=./node_modules --exclude=./dist -cf - . \
    | tar -C /tmp/build -xf -
cd /tmp/build
npm ci --no-audit --no-fund --ignore-scripts
npm run build:production
rm -rf /out/dist.part
mkdir -p /out/dist.part
cp -a dist/. /out/dist.part/
rm -rf /out/dist
mv /out/dist.part /out/dist
"""


class BuildFailed(RuntimeError):
    pass


def engine() -> str | None:
    """Podman, or nothing. See the module docstring for why Docker is out."""
    return shutil.which("podman")


def source_dir(jellyfin_source: str) -> str | None:
    """The jellyfin-web checkout beside the server checkout, if there is one.

    The same guess `jfserver.find_web_client` makes about where a built one
    would be, one directory up.
    """
    guess = os.path.join(os.path.dirname(os.path.abspath(jellyfin_source.rstrip("/"))),
                         "jellyfin-web")
    return guess if os.path.exists(os.path.join(guess, "package.json")) else None


def revision(src: str) -> str:
    """What the source is, for deciding whether a rebuild is needed.

    The git commit when there is one. A checkout with local edits reports the
    commit plus `-dirty`, so working on jellyfin-web rebuilds every time
    rather than serving a stale bundle that looks current.
    """
    try:
        head = subprocess.run(["git", "-C", src, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30)
        if head.returncode != 0:
            return "unknown"
        rev = head.stdout.strip()
        dirty = subprocess.run(["git", "-C", src, "status", "--porcelain"],
                               capture_output=True, text=True, timeout=60)
        return rev + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def image_id(podman: str, image: str) -> str:
    """The digest of the image actually used, so a moved tag is visible.

    A tag is not a pin. Recording what it resolved to means a base image that
    changed under a rebuild shows up in the stamp rather than being invisible.
    """
    got = subprocess.run([podman, "image", "inspect", image, "--format", "{{.Digest}}"],
                         capture_output=True, text=True)
    return got.stdout.strip() if got.returncode == 0 else "unknown"


def stamp_path(out: str) -> str:
    return os.path.join(out, "stamp.json")


def stamp(out: str) -> dict:
    try:
        with open(stamp_path(out), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def is_current(out: str, src: str) -> bool:
    """True when `<out>/dist` was built from what is in `src` right now."""
    built = os.path.join(out, "dist")
    if not os.path.exists(os.path.join(built, "index.html")):
        return False
    return stamp(out).get("revision") == revision(src)


def build(src: str, out: str, *, image: str = IMAGE, verbose: bool = False,
          say=print) -> str:
    """Build jellyfin-web from `src` into `<out>/dist`. Returns that path."""
    podman = engine()
    if not podman:
        raise BuildFailed("no podman on PATH")

    os.makedirs(out, exist_ok=True)
    argv = [
        podman, "run", "--rm",
        # Everything a hostile install script would want, refused. The network
        # is the exception and cannot be closed: npm needs the registry.
        "--cap-drop=ALL",
        "--security-opt", "no-new-privileges",
        # `:ro` on the source is the load-bearing one. The build copies out of
        # it; nothing it runs can write back into the checkout.
        "-v", f"{src}:/src:ro",
        "-v", f"{out}:/out",
        # No `-w`: podman refuses to start when the working directory does
        # not exist in the image ("workdir does not exist", exit 126). The
        # script makes its own and `cd`s into it.
        #
        # npm writes a cache and a logs directory; without a home it picks
        # `/` and fails on a read-only-ish container with a confusing EACCES.
        "-e", "HOME=/tmp",
        "-e", "npm_config_cache=/tmp/npm-cache",
        image, "sh", "-c", SCRIPT,
    ]

    say(f"  building jellyfin-web in {os.path.basename(image)} "
        f"(npm never runs on this machine)")
    if verbose:
        say("  " + " ".join(argv))
    result = subprocess.run(argv, capture_output=not verbose, text=True)
    if result.returncode != 0:
        tail = ""
        if not verbose and result.stderr:
            tail = "\n" + "\n".join(result.stderr.strip().splitlines()[-15:])
        raise BuildFailed(f"jellyfin-web build failed (exit {result.returncode})"
                          + tail)

    built = os.path.join(out, "dist")
    if not os.path.exists(os.path.join(built, "index.html")):
        raise BuildFailed(f"the build produced no index.html in {built}")

    with open(stamp_path(out), "w", encoding="utf-8") as fh:
        json.dump({"revision": revision(src), "source": src,
                   "image": image, "image_digest": image_id(podman, image)},
                  fh, indent=2, sort_keys=True)
        fh.write("\n")
    return built


def ensure(jellyfin_source: str, out: str, *, enabled: bool = True,
           verbose: bool = False, say=print) -> tuple[str | None, str]:
    """Get a built jellyfin-web if one can be had. Returns (path, why).

    Never raises: a server with no web client is a working server, and the
    failure to produce a browser UI must not take down a run whose actual
    purpose is the API. `why` is what to print either way.
    """
    if not enabled:
        return None, "skipped (--no-web)"

    src = source_dir(jellyfin_source)
    if not src:
        return None, "no jellyfin-web checkout beside the server source"

    if is_current(out, src):
        return os.path.join(out, "dist"), f"cached, built from {revision(src)[:10]}"

    if not engine():
        return None, ("no podman — jellyfin-web is built in a container on "
                      "purpose, and Docker is not a substitute here")

    try:
        built = build(src, out, verbose=verbose, say=say)
    except BuildFailed as exc:
        return None, f"build failed: {exc}"
    return built, f"built from {revision(src)[:10]}"
