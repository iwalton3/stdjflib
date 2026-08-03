"""Tiers, paths and the build configuration object.

Everything the rest of the package needs to know about *where* things go and
*how much* to build lives here. Nothing in this module runs ffmpeg or touches
the network.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import tempfile

# Tiers are cumulative: "standard" builds everything marked minimal + standard.
# Order matters — `tier_includes` compares by index.
TIERS = ("minimal", "standard", "full")

TIER_HELP = {
    "minimal": "generated media only, no downloads (~2 GB) — the CI tier",
    "standard": "adds the Blender open movies and public-domain shorts (~25 GB)",
    "full": "adds 4K, 60 fps, stereo-3D and the long tail (~150 GB)",
}

# Library folders, and the Jellyfin collection type each one should be added as.
# The key is the directory name under the output root; the value is what you
# pick in Dashboard -> Libraries -> Add Media Library.
LIBRARIES = {
    # Real films, and the path-convention fixtures. Kept apart from the codec
    # matrix so a Movies library still looks like a Movies library.
    "Movies": "movies",
    # The generated codec/container matrix. Add it as a Movies library too —
    # it is separate so you can point a test at it without 87 synthetic files
    # drowning the real ones.
    "Test Media": "movies",
    "Shows": "tvshows",
    "Music": "music",
    "Music Videos": "musicvideos",
    "Photos": "homevideos",  # "Photos" in the UI is homevideos+photos
    "Home Videos": "homevideos",
    # The same content type as the two above, and deliberately untidy where
    # they are tidy: videos and photographs in one tree, at uneven depths,
    # with folders that hold one kind, the other, or both. Separate so the
    # tidy case stays available on its own.
    "Mixed Content": "homevideos",
    "Books": "books",
}

# Scale libraries, kept entirely separate from the curated ones above. These
# answer a different question: not "does the client handle this format" but
# "does it still work with a thousand of them" — paging, virtualised scroll,
# thumbnail cache eviction, search and sort. Mixing them into `Movies/` would
# bury the fixtures that are there to be looked at individually.
BULK_LIBRARIES = {
    "Bulk Movies": "movies",
    "Bulk Shows": "tvshows",
    "Bulk Music": "music",
    "Bulk Music Videos": "musicvideos",
    "Bulk Photos": "homevideos",
    "Bulk Books": "books",
}

# Items per bulk library at the full tier. Chosen against the client under
# test: jellyfin-mpv-shim pages at 100 and fetches 300 at a time, so a
# thousand crosses both boundaries several times over instead of sitting just
# past the first one.
DEFAULT_BULK = 1000

# Everything the tool writes that is not library content.
STATE_DIR = ".stdjflib"
CACHE_DIR = os.path.join(STATE_DIR, "cache")
MANIFEST = os.path.join(STATE_DIR, "manifest.json")
ATTRIBUTION = os.path.join(STATE_DIR, "ATTRIBUTION.md")


def runtime_dir(root: str, name: str) -> str:
    """Local scratch for a server instance built from the library at `root`.

    Server state, build artifacts and faketvsource logs do not belong beside
    the library. The library is frequently on a network mount — sshfs, NFS —
    and SQLite over one of those is slow at best and corrupt at worst, while
    `dotnet build` over sshfs is unusable. None of it is worth keeping either:
    the instances are disposable and `--fresh` exists to delete them.

    The path is derived from `root` rather than fixed, so two libraries do not
    end up sharing one server's database, and stable across runs, so a second
    `serve` reuses the same state and the same build. It is under the system
    temp directory, so a reboot takes it — which costs a Jellyfin rebuild and
    a factory-fresh server, both of which are cheap and both of which are
    states worth passing through anyway.

    Pass `--state` / `--artifacts` to override.
    """
    root = os.path.abspath(root)
    tag = hashlib.sha256(root.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    # /tmp is shared, so the per-user directory is made narrow before anything
    # is written into it — otherwise another user could get there first.
    base = os.path.join(tempfile.gettempdir(), f"stdjflib-{os.getuid()}")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return os.path.join(base, f"{os.path.basename(root) or 'library'}-{tag}", name)


# Bumped when a change makes previously built output stale. `verify` warns when
# the manifest on disk was written by a different build version.
BUILD_VERSION = 1

# Every generated timestamp derives from this, so two builds of the same tier
# produce byte-identical NFO files. Deliberately not "now".
EPOCH = "2020-01-01T00:00:00Z"


def tier_includes(build_tier: str, item_tier: str) -> bool:
    """True when a build at `build_tier` should include an item marked `item_tier`."""
    return TIERS.index(item_tier) <= TIERS.index(build_tier)


@dataclasses.dataclass(frozen=True)
class BuildConfig:
    root: str
    tier: str = "standard"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    jobs: int = 0  # 0 -> pick from cpu count
    seed: str = "stdjflib"
    dry_run: bool = False
    verbose: bool = False
    font_file: str | None = None
    only: tuple[str, ...] = ()  # restrict to these library folders
    keep_cache: bool = True
    hwaccel: str | None = None  # None | "nvenc" — see `hw_encoder`
    bulk: int = 0               # items per bulk library; 0 builds none
    # Redraw the images and leave the media alone. Every builder still runs,
    # so the artwork lands in exactly the places a build would put it —
    # including image types added since the library was built, which a pass
    # over the files on disk could never discover.
    artwork_only: bool = False
    # Back the artwork with photographs instead of drawn shapes, so the
    # library photographs like a real one. Opt-in: it downloads, and it drops
    # the type stamp that makes a misplaced image obvious. See `photos.py`.
    use_artwork: bool = False

    def __post_init__(self):
        if self.tier not in TIERS:
            raise ValueError(f"unknown tier {self.tier!r}, expected one of {TIERS}")
        if self.hwaccel not in (None, "nvenc"):
            raise ValueError(f"unknown hwaccel {self.hwaccel!r}")

    @property
    def workers(self) -> int:
        """How many ffmpeg processes to run at once.

        ffmpeg threads its own encode, so oversubscribing hurts. Half the
        logical CPUs keeps each encoder with real cores to work with while
        still filling a many-core machine.
        """
        if self.jobs:
            return self.jobs
        return max(1, (os.cpu_count() or 2) // 2)

    def hw_encoder(self, software: str) -> str | None:
        """The hardware equivalent of a software encoder, if one applies.

        Deliberately narrow. Hardware encoders produce different bytes from
        libx264/libx265, so switching them on makes a build non-reproducible —
        which matters, because the whole point of this library is that two
        people can build the same one. It is opt-in, recorded in the manifest,
        and never used for the codec-matrix files whose exact encoding *is*
        the test. `build.py` asks for it only on the large files, where the
        saving is minutes rather than seconds.
        """
        if self.hwaccel != "nvenc":
            return None
        return {"libx264": "h264_nvenc", "libx265": "hevc_nvenc",
                "libsvtav1": "av1_nvenc"}.get(software)

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def cache(self, *parts: str) -> str:
        return os.path.join(self.root, CACHE_DIR, *parts)

    def wants(self, library: str) -> bool:
        return not self.only or library in self.only

    def libraries(self) -> dict:
        """Every library folder this build will produce, and its content type."""
        out = dict(LIBRARIES)
        if self.bulk:
            out.update(BULK_LIBRARIES)
        return {name: kind for name, kind in out.items() if self.wants(name)}

    def includes(self, item_tier: str) -> bool:
        return tier_includes(self.tier, item_tier)


def find_font() -> str | None:
    """Locate a TTF for ffmpeg's drawtext.

    Broad coverage beats good Latin here. The burned-in labels name audio and
    subtitle tracks in the language they are in, so a label can hold Japanese,
    Hebrew and Latin at once — and drawtext takes exactly one font with no
    fallback, so anything the font lacks renders as tofu boxes. A CJK font is
    preferred for that reason, not for looks.

    A missing font degrades the library rather than breaking it: clips build
    unlabelled and bitmap subtitles are skipped, and the build says so.
    """
    candidates = [
        # Wide coverage first — these carry CJK as well as Latin.
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        # Then the usual Latin/Cyrillic/Greek/Hebrew workhorses.
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return font_for_lang(None)


# ISO 639-2 (what the recipes and Jellyfin use) to the 639-1 codes fontconfig
# wants. Only the scripts this library actually generates text in.
_FC_LANG = {
    "eng": "en", "fra": "fr", "deu": "de", "spa": "es", "ita": "it",
    "por": "pt", "nld": "nl", "rus": "ru", "ell": "el", "heb": "he",
    "ara": "ar", "fas": "fa", "jpn": "ja", "kor": "ko", "zho": "zh",
    "ces": "cs", "pol": "pl", "hun": "hu", "hrv": "hr", "dan": "da",
    "nor": "no", "ind": "id",
}


def font_for_lang(lang: str | None) -> str | None:
    """Ask fontconfig for a font that covers this language's script.

    Used where the text is known to be in one language — rasterising a bitmap
    subtitle track, for instance — because there a per-script choice is both
    possible and strictly better than one font for everything.
    """
    fc = shutil.which("fc-match")
    if not fc:
        return None
    pattern = "sans"
    if lang:
        code = _FC_LANG.get(lang, lang[:2])
        pattern = f"sans:lang={code}"
    import subprocess

    try:
        out = subprocess.run([fc, "-f", "%{file}", pattern],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    path = out.stdout.strip()
    return path if out.returncode == 0 and os.path.exists(path) else None
