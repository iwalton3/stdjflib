"""Building the Jellyfin server in a container, so dotnet never runs here.

The same reasoning as `web.py`, one layer down. `dotnet publish` restores a
few hundred NuGet packages, and a package's MSBuild targets run as whoever is
building — the server's equivalent of an npm install script. So the build
happens in rootless podman under the isolation `web.py` uses: the checkout
mounted read-only and copied inside, capabilities dropped, no privilege
escalation, and one writable mount holding the output and the NuGet cache.
The network stays on because restore needs it.

What comes out is a self-contained publish, not an image. `serve` mounts it
over `/jellyfin` in the official image, whose entrypoint is exactly that
directory's `jellyfin`, so the runtime libraries and jellyfin-ffmpeg come from
the image and the part that changes with the checkout is the only part
rebuilt. `--on-host` keeps `jfserver.build` for anyone who opts back in.

Podman only, for `web.py`'s reason: a rootful Docker daemon would run the
build as root on the host.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess

from . import web

SDK_REPOSITORY = "mcr.microsoft.com/dotnet/sdk"
DEFAULT_SDK = "10.0"
CONFIGURATION = "Debug"

# `--no-same-owner`: rootless podman maps the invoking user to root, and a
# group it has no mapping for arrives as 65534. tar running as root tries to
# restore both, and `--cap-drop=ALL` took CAP_CHOWN — so without the flag the
# copy fails with "Cannot change ownership" before dotnet ever starts.
#
# `obj` and `bin` are excluded wherever they appear: an `--on-host` build or an
# old in-tree one leaves them in the checkout, and restoring on top of another
# machine's `project.assets.json` fails in ways that name neither.
SCRIPT = r"""
set -eu
mkdir -p /tmp/build
tar -C /src --exclude=./.git --exclude=obj --exclude=bin -cf - . \
    | tar -C /tmp/build --no-same-owner -xf -
cd /tmp/build
dotnet publish Jellyfin.Server/Jellyfin.Server.csproj -c "$CONFIGURATION" \
    --self-contained -r "$RID" -o /out/publish.part
rm -rf /out/publish
mv /out/publish.part /out/publish
"""


class BuildFailed(RuntimeError):
    pass


def sdk_image(source: str) -> str:
    """The SDK the checkout asks for in `global.json`, as an image tag.

    Read rather than hard-coded, so a checkout that moves to a new .NET major
    builds with that SDK instead of failing on the old one.
    """
    try:
        with open(os.path.join(source, "global.json"), encoding="utf-8") as fh:
            version = json.load(fh)["sdk"]["version"]
        major, minor = version.split(".")[:2]
        tag = f"{int(major)}.{int(minor)}"
    except (OSError, ValueError, KeyError, TypeError):
        tag = DEFAULT_SDK
    return f"{SDK_REPOSITORY}:{tag}"


def runtime_identifier() -> str:
    arch = {"x86_64": "x64", "amd64": "x64",
            "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if not arch:
        raise BuildFailed(f"no .NET runtime identifier for {platform.machine()}")
    return f"linux-{arch}"


def publish_dir(out: str) -> str:
    return os.path.join(out, "publish")


def _stamp_path(out: str) -> str:
    return os.path.join(out, "publish-stamp.json")


def _wanted(source: str) -> dict:
    return {"revision": web.revision(source), "configuration": CONFIGURATION,
            "rid": runtime_identifier(), "image": sdk_image(source)}


def is_current(out: str, source: str) -> bool:
    """True when `<out>/publish` was built from what is in `source` now."""
    if not os.path.exists(os.path.join(publish_dir(out), "jellyfin")):
        return False
    try:
        with open(_stamp_path(out), encoding="utf-8") as fh:
            built = json.load(fh)
    except (OSError, ValueError):
        return False
    return all(built.get(k) == v for k, v in _wanted(source).items())


def argv(podman: str, source: str, out: str) -> list[str]:
    return [
        podman, "run", "--rm",
        "--cap-drop=ALL",
        "--security-opt", "no-new-privileges",
        "-v", f"{source}:/src:ro",
        # The one writable mount. The NuGet cache lives inside it, so a second
        # build restores from disk instead of downloading ~2 GB again.
        "-v", f"{out}:/out",
        "-e", "HOME=/tmp",
        "-e", "NUGET_PACKAGES=/out/nuget",
        "-e", "DOTNET_CLI_TELEMETRY_OPTOUT=1",
        "-e", "DOTNET_NOLOGO=1",
        "-e", f"CONFIGURATION={CONFIGURATION}",
        "-e", f"RID={runtime_identifier()}",
        sdk_image(source), "sh", "-c", SCRIPT,
    ]


def build(source: str, out: str, *, verbose: bool = False, say=print) -> str:
    """Publish the server from `source` into `<out>/publish`. Returns that path."""
    podman = web.engine()
    if not podman:
        raise BuildFailed("no podman on PATH")
    os.makedirs(out, exist_ok=True)
    command = argv(podman, os.path.abspath(source), os.path.abspath(out))
    say(f"  building Jellyfin in {sdk_image(source)} "
        f"(dotnet never runs on this machine)")
    if verbose:
        say("  " + " ".join(command))
    result = subprocess.run(command, capture_output=not verbose, text=True)
    if result.returncode != 0:
        tail = ""
        if not verbose:
            text = (result.stdout or "") + (result.stderr or "")
            errors = [line for line in text.splitlines() if " error " in line]
            tail = "\n" + "\n".join((errors or text.splitlines())[-15:])
        raise BuildFailed(f"Jellyfin build failed (exit {result.returncode})" + tail)
    if not os.path.exists(os.path.join(publish_dir(out), "jellyfin")):
        raise BuildFailed(f"the build produced no jellyfin in {publish_dir(out)}")

    stamp = dict(_wanted(source), source=os.path.abspath(source),
                 image_digest=web.image_id(podman, sdk_image(source)))
    with open(_stamp_path(out), "w", encoding="utf-8") as fh:
        json.dump(stamp, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return publish_dir(out)
