"""Thin wrappers around ffmpeg and ffprobe, plus capability detection.

The only place in the package that spawns a subprocess for media work.
"""

from __future__ import annotations

import functools
import json
import os
import shlex
import subprocess


class FFmpegError(RuntimeError):
    def __init__(self, argv, returncode, stderr):
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        # ffmpeg puts the real complaint above its per-stream summary, so a
        # short tail reliably shows everything except the cause.
        tail = "\n".join(stderr.strip().splitlines()[-25:])
        super().__init__(
            f"ffmpeg exited {returncode}\n  {shlex.join(argv)}\n{tail}"
        )


def run(argv: list[str], *, verbose: bool = False, timeout: int = 3600) -> str:
    """Run ffmpeg/ffprobe, raising FFmpegError with the useful part of stderr.

    ffmpeg writes everything to stderr and exits 0 on a surprising number of
    partial failures, so callers that care about the *output* should probe it
    afterwards rather than trusting the return code alone.
    """
    if verbose:
        print("  $", shlex.join(argv), flush=True)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise FFmpegError(argv, proc.returncode, proc.stderr)
    return proc.stderr


def probe(path: str, ffprobe: str = "ffprobe") -> dict:
    """Return ffprobe's JSON for a file, or {} if it cannot be read."""
    argv = [
        ffprobe, "-v", "error", "-show_format", "-show_streams",
        "-show_chapters", "-of", "json", path,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return {}
        return json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


@functools.lru_cache(maxsize=8)
def capabilities(ffmpeg: str = "ffmpeg") -> frozenset[str]:
    """The set of encoder names this ffmpeg can actually use.

    Recipes name encoders directly; anything missing is skipped with a note
    rather than failing the build, because the interesting encoders (truehd,
    dca, libfdk_aac, libsvtav1) are exactly the ones distributions disable.
    """
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        # Encoder lines look like " V....D libx264  H.264 ..." — flags, name, desc.
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


@functools.lru_cache(maxsize=8)
def version(ffmpeg: str = "ffmpeg") -> str:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=30,
        )
        return proc.stdout.splitlines()[0] if proc.stdout else "unknown"
    except (OSError, IndexError, subprocess.SubprocessError):
        return "unknown"


def have(ffmpeg: str, *encoders: str) -> bool:
    caps = capabilities(ffmpeg)
    return all(e in caps for e in encoders)


def missing(ffmpeg: str, *encoders: str) -> list[str]:
    caps = capabilities(ffmpeg)
    return [e for e in encoders if e not in caps]


def temp_path(path: str) -> str:
    """A hidden sibling of `path` that keeps the same extension.

    Two things matter here. Same directory, so the rename at the end is atomic
    even on the sshfs mount this usually points at. And **same extension** —
    ffmpeg picks its muxer from the extension when no `-f` is given, so writing
    to `album.flac.part` fails with "Error opening output files: Invalid
    argument", which names neither the extension nor the muxer and sends you
    looking at the input.
    """
    directory, name = os.path.split(path)
    return os.path.join(directory or ".", f".stdjflib-tmp-{name}")


def commit(tmp: str, path: str) -> None:
    os.replace(tmp, path)


def escape_drawtext(text: str) -> str:
    """Escape a literal for a drawtext `text=` value.

    Only used for short generated labels. Anything user-supplied or long goes
    through `textfile=` instead — see `generate.py`.
    """
    out = text.replace("\\", "\\\\")
    for ch in ":'%[],;=":
        out = out.replace(ch, "\\" + ch)
    return out
