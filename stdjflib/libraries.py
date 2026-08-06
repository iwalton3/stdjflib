"""Builders for each library type.

The naming conventions here are the deliverable as much as the media is.
Jellyfin resolves an item's identity from its path before it reads a single
byte, so a QA library has to cover the shapes real libraries come in: folder
per movie and loose files, multi-version and multi-part, season folders and
absolute numbering, date-based episodes and double episodes, specials in
Season 00 and gaps where episodes are missing.

Every item gets an NFO with `<lockdata>true`, so nothing is ever fetched from
the internet and two builds present identical metadata.
"""

from __future__ import annotations

import concurrent.futures as futures
import dataclasses
import os

from . import artwork, boxsets, catalog, ff, generate, nfo, origin, recipes, strm
from .recipes import Audio, Recipe, Video

YEAR = 2020


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _clip(rec: Recipe, **kw) -> Recipe:
    return dataclasses.replace(rec, **kw)


def _run_all(tasks: list, cfg) -> list:
    """Run per-item build closures across a thread pool, preserving order.

    Each task is one whole item — encode, then its NFO and artwork — because
    all three are ffmpeg-bound and splitting them finer only adds coordination.
    `map` keeps results in submission order, so the manifest does not reshuffle
    between builds.

    Serial when there is one worker or nothing is being written, so `-j1` and
    `--dry-run` stay easy to read.
    """
    if cfg.dry_run or cfg.workers <= 1:
        return [task() for task in tasks]
    with futures.ThreadPoolExecutor(cfg.workers) as pool:
        return list(pool.map(lambda task: task(), tasks))


def _short(title: str, key: str, *, group: str = "Library",
           duration: int = 12, **kw) -> Recipe:
    """A small, cheap clip for structural fixtures that are not codec tests."""
    return Recipe(
        key=key, title=title, group=group, notes=kw.pop("notes", ""),
        duration=duration,
        video=kw.pop("video", Video(width=640, height=360, bitrate="500k")),
        audios=kw.pop("audios", (Audio(encoder="aac"),)),
        **kw,
    )


def _emit(rec: Recipe, path: str, cfg, *, allow_hw: bool = False) -> bool:
    if cfg.artwork_only:
        # The media is already there and is not what this run is about. Say
        # yes so the item's artwork and metadata still get written.
        return True
    try:
        res = generate.build(rec, path, cfg, allow_hw=allow_hw)
        return res.ok
    except (ff.FFmpegError, OSError, ValueError) as exc:
        print(f"    ! {os.path.basename(path)}: {str(exc).splitlines()[-1][:120]}")
        return False


def _strm(path: str, url: str, cfg, **kw) -> bool:
    """Write one stream file, on the same terms `_emit` writes media.

    A `.strm` is media as far as the build is concerned — it is what the item
    plays — so an artwork pass leaves it alone and says yes anyway, exactly as
    `_emit` does, and the item's NFO and images still get rewritten.
    """
    if cfg.dry_run or cfg.artwork_only:
        return True
    try:
        strm.write(path, url, **kw)
        return True
    except OSError as exc:
        print(f"    ! {os.path.basename(path)}: {exc}")
        return False


# --------------------------------------------------------------------------
# Test Media — the codec/container matrix
# --------------------------------------------------------------------------

def build_test_media(root: str, cfg) -> list[dict]:
    """The generated matrix, one folder per group, one movie per recipe.

    Ordered longest-first. The three-hour clip and the 8K one dominate the
    wall clock, so starting them last leaves every other worker idle while one
    finishes; starting them first fills the tail with short work instead.
    """
    def task(rec):
        def run():
            folder = os.path.join(root, rec.group)
            base = f"{rec.title} ({YEAR})"
            # Slashes and colons are legal in a title and fatal in a filename.
            safe = base.replace("/", "-").replace(":", " -")
            media = os.path.join(folder, f"{safe}.{rec.container}")

            big = (rec.video and rec.video.height >= 1440) or rec.duration > 600
            if not _emit(rec, media, cfg, allow_hw=big):
                return None

            if not cfg.dry_run:
                nfo.movie(
                    os.path.join(folder, f"{safe}.nfo"),
                    key=rec.key, title=rec.title, plot=rec.notes, year=YEAR,
                    runtime_minutes=max(1, rec.duration // 60),
                    tags=["stdjflib", "generated", rec.group],
                    collection=rec.group,
                    sort_title=f"{rec.group} — {rec.title}",
                )
                # One folder holds a whole codec group, so these are loose
                # files and only the sidecar naming resolves.
                artwork.sidecar_images(media, rec.key, rec.title, cfg,
                                       kinds=("poster",), subtitle=rec.group)
            return {"library": "Test Media", "key": rec.key, "path": media,
                    "group": rec.group}
        return run

    # `library` is what keeps the audiobooks out of here: they are declared as
    # recipes so `verify` re-probes them, and placed by `build_books`, because
    # an audiobook only resolves as one inside a `books` library.
    ordered = sorted((r for r in recipes.for_tier(cfg.tier)
                      if r.library == "Test Media"),
                     key=lambda r: -(r.duration * (r.video.height if r.video else 1)))
    return [item for item in _run_all([task(r) for r in ordered], cfg) if item]


# --------------------------------------------------------------------------
# Movies — real films plus path-convention fixtures
# --------------------------------------------------------------------------

# Each entry is a way Jellyfin can be handed a movie. The plot explains the
# convention, so browsing the library is also reading the documentation.
NAMING_CASES = [
    ("loose-file", "Loose File Movie", "flat",
     "A movie sitting directly in the library root with no folder of its own. "
     "Jellyfin has to take the title and year from the filename alone."),
    ("folder-mismatch", "Folder Name Disagrees", "mismatch",
     "The folder says one title and the file says another. Which one wins is "
     "not obvious, and clients disagree about what to display."),
    ("multi-version", "Multi Version Movie", "versions",
     "Three encodes of one film in one folder, distinguished by the bracketed "
     "tags Jellyfin reads as version names. They must appear as one item with "
     "three sources, not three items. No file is named exactly like the "
     "folder, so the resolution in the tag decides which source is primary: "
     "the 1080p one."),
    ("multi-version-editions", "Named Editions Movie", "editions",
     "The other multi-version spelling. One file is named exactly like the "
     "folder, which makes it the primary version outright, and the two "
     "alternates are named for an edition rather than a resolution. They also "
     "differ in codec, channel count and container, so a client's version "
     "picker has something to distinguish them by and switching source is "
     "audible."),
    ("multi-part", "Multi Part Movie", "parts",
     "One film split across three part files. Jellyfin stacks these into a "
     "single playable item; a client that lists them separately plays a third "
     "of the film and stops."),
    ("with-extras", "Movie With Extras", "extras",
     "A film with trailers, deleted scenes, interviews and a behind-the-scenes "
     "reel in the folder layout Jellyfin recognises. Extras must not appear as "
     "films in their own right."),
    ("bracket-id", "Movie With Provider Id", "flat",
     "The filename carries a provider id in brackets. It is deliberately a "
     "made-up namespace, so nothing tries to resolve it against a live service."),
    ("unicode-title", "Ünïcödé — 日本語 — العربية", "flat",
     "Non-ASCII throughout the title, mixing Latin diacritics, CJK and a "
     "right-to-left script in one filename. Exercises path encoding, sorting "
     "and text shaping at once."),
    ("very-long-title", "A Movie With An Extremely Long Title That Goes On Well "
     "Past Any Reasonable Column Width And Keeps Going", "flat",
     "A title long enough to overflow every list column and card label."),
    ("strm-loose", "Remote Stream Movie", "strm-flat",
     "A .strm file: one line of text holding a URL, resolved as a movie and "
     "played from the network. Nothing is probed on a scan, so this item has "
     "no runtime, no stream list and no resolution at all until something "
     "asks to play it — measured, and not for want of a <runtime> in the NFO, "
     "which is there and does not fill it. Everything a client can draw "
     "before playback is the metadata and the images beside the file, and the "
     "media source is remote, which is a code path a library of local files "
     "never reaches."),
    ("strm-comments", "Commented Stream File", "strm-folder",
     "The same thing with everything the parser is supposed to tolerate: a "
     "comment header, blank lines, a tab-indented URL, and a second URL "
     "underneath. Jellyfin takes the first line that is neither blank nor a "
     "'#' comment and ignores the rest — a .strm is one source, not a "
     "playlist. A client showing two versions here is reading the file itself "
     "and reading it wrong."),
    ("strm-rtsp", "RTSP Stream Movie", "strm-flat",
     "A stream file naming rtsp:// rather than http://. Jellyfin accepts four "
     "schemes — http, https, rtsp and rtp — so this item's media source has a "
     "protocol a client that assumed HTTP has never had to render. The "
     "address is a loopback one with nothing behind it: the protocol field is "
     "the fixture, not the stream."),
    ("strm-local-path", "Stream File Naming A Local Path", "strm-flat",
     "A stream file containing a filesystem path instead of a URL. Jellyfin "
     "refuses it twice over — once in the parser, again in "
     "BaseItem.GetVersionInfo — because honouring it would make a .strm a way "
     "to read any file on the server. What it resolves to instead, measured, "
     "is an item whose only media source is the stream file itself: protocol "
     "File, not remote, pointing at this .strm. That is the awkward state "
     "being tested — everything about the item looks playable, and what a "
     "client would be asked to open is a line of text."),
    ("strm-versions", "Local And Remote Versions", "strm-versions",
     "One film with two sources, one on disk and one on the network. The "
     "file named exactly like the folder is local and primary; the alternate "
     "is a .strm. Multi-version grouping compares filenames without their "
     "extensions, so a stream file sits in a version set beside real media — "
     "and switching between them switches between a local file and a URL."),
    ("strm-origin", "Local Origin Stream Movie", "strm-flat",
     "The same shape as Remote Stream Movie, pointing at an HTTP origin "
     "running on this machine instead of at archive.org. As far as Jellyfin "
     "is concerned the source is just as remote — protocol Http, IsRemote "
     "true, no probe on a scan — but nothing leaves the box, so this is the "
     "one an end-to-end playback test can use with the network unplugged. It "
     "needs the origin server running: `stdjflib serve` starts it, and the "
     "URL is baked in at build time, so a server that is not on this machine "
     "needs `build --stream-origin`."),
    ("strm-origin-versions", "Local Origin Versions", "strm-origin-versions",
     "One film, two sources, and neither of them needs a network: the primary "
     "is a 10-second file on disk, the alternate is a .strm pointing at the "
     "30-second clip on this machine's origin. Switching version switches "
     "between a local file and a URL, and the media really is three times as "
     "long on one side as the other. Measured, and worth knowing before "
     "asserting on it: the alternate reports **no runtime at all** until it "
     "plays, even from PlaybackInfo and even with mediaSourceId pinned to it. "
     "MediaSourceManager only forces the remote probe when the *item's* path "
     "ends in .strm, and a version set's path is its primary's — so the "
     "shortcut in the set is never probed. Tell the sources apart by Path, "
     "IsRemote or Id; RunTimeTicks will not do it here. Origin Primary "
     "Versions is the same set built the other way up, where it does — the "
     "two are a pair and neither substitutes for the other."),
    ("strm-origin-primary", "Origin Primary Versions",
     "strm-origin-primary-versions",
     "The same version set built the other way up, and the pair is the point. "
     "Here the .strm is the file named exactly like its folder, which makes it "
     "the primary source outright — so the *item's* path ends in .strm, "
     "MediaSourceManager's probe gate fires on it, and both sources come back "
     "carrying their own runtime: 30 seconds from the origin on the primary, "
     "20 from the local file behind it. In Local Origin Versions, where the "
     "primary is the local file, the shortcut alternate is never probed and "
     "reports nothing. So one of these two says whether a client reads a "
     "version's own duration, and the other says what it does when there is "
     "none to read. Neither substitutes for the other."),
    ("strm-long", "Long Origin Stream Movie", "strm-flat",
     "A stream fixture that is 400 seconds long, which is the only reason it "
     "exists. Jellyfin refuses to keep a resume point for anything shorter "
     "than MinResumeDurationSeconds — 300 by default — and zeroes the "
     "position and marks the item played instead. Every other clip in this "
     "library is 12 to 30 seconds, so this is the one item a resume, "
     "progress or continue-watching test can be pointed at. The position is "
     "only kept between 5% and 90%, so the window that holds one is 20s to "
     "360s. It is encoded at a deliberately poor bitrate: nothing about a "
     "resume point needs to look good, and 400 seconds at the usual rate "
     "would outweigh the rest of the tier."),
]

# Where the `.strm` fixtures point. Every playable one names a *catalogue*
# source rather than an address of its own, so a stream file cannot end up
# referencing something the licence gate has never had an opinion about — and
# `build.write_attribution` credits them from this table whether or not the
# tier downloaded anything. A stream file is a line of text, so these fixtures
# cost nothing and exist even in the minimal tier.
#
# One film per fixture rather than one film everywhere, so browsing these is
# not the same eleven minutes six times over.
STRM_SOURCES = {
    "movie-strm-loose": "prelinger-aboutban1935",
    "movie-strm-comments": "prelinger-sniffles1955",
    "movie-strm-versions": "prelinger-careofth1949",
    "show-strm-episode": "prelinger-sleepfor1950",
    "show-strm-version": "prelinger-eatforhe1954",
    "music-strm": "prelinger-parkcons1938",
}

# The two that are *not* meant to play. Both are deliberately local: an
# unroutable loopback port and a filesystem path. Neither goes near the
# network, and neither is supposed to — what they test is the protocol field
# and the refusal, not a stream.
STRM_UNPLAYABLE = {
    "movie-strm-rtsp": "rtsp://127.0.0.1:8554/stdjflib/no-such-stream",
    "movie-strm-local-path":
        "/srv/media/Movies/Some Film (2020)/Some Film (2020).mkv",
}

# The clips behind the local origin. These are generated exactly like
# everything else here and served over HTTP from `.stdjflib/origin/` by
# `origin.py`, so the URL is remote as far as Jellyfin is concerned and
# nothing leaves the machine.
#
# They are the reason an end-to-end playback test does not need a network. The
# archive.org fixtures are the better test — a real host, real TLS, real
# redirects — and they are unusable in CI and on a metered connection, so both
# exist and the local ones are named so a test can pick them deliberately.
#
# `origin-long.mp4` is the one whose *duration* is the fixture.
# `UserDataManager.UpdatePlayState` enforces
# `ServerConfiguration.MinResumeDurationSeconds`, which defaults to **300**:
# anything shorter has its position zeroed and is marked played outright, so
# no resume point can exist for it at all. Every other clip in this library is
# 12-30 seconds, which means that until this one existed there was nothing a
# resume, progress or continue-watching test could be pointed at. 400s clears
# the threshold with room to spare — and because the same method only keeps a
# position between `MinResumePct` 5 and `MaxResumePct` 90, the window that
# actually holds one here is 20s to 360s.
#
# Its bitrate is deliberately miserable. 400 seconds at the 1500k the other
# clips use would be some 80 MB in a library whose whole minimal tier is 400,
# and nothing about a resume point needs to look good.
# name -> (container, seconds, video, audio). The long one names its audio
# bitrate too: at the default the soundtrack alone would outweigh the picture
# over 400 seconds, and a mono 48k stream is still a stream to switch to and
# still audible enough to tell you playback is running.
ORIGIN_CLIPS = {
    "origin-movie.mkv": ("mkv", 30,
                         Video(width=1280, height=720, bitrate="1500k"),
                         (Audio(encoder="aac"),)),
    "origin-episode.mp4": ("mp4", 30,
                           Video(width=854, height=480, bitrate="900k"),
                           (Audio(encoder="aac"),)),
    "origin-long.mp4": ("mp4", 400,
                        Video(width=640, height=360, bitrate="90k"),
                        (Audio(encoder="aac", channels=1, bitrate="48k"),)),
}

# Which fixture names which clip, and the library it belongs to so a partial
# build does not produce clips nothing points at. Several fixtures may share
# one clip: the file is a stream *target*, not an item, so two `.strm` files
# naming it are still two separate items.
# What the local primary of `strm-origin-versions` runs for, against the 30s
# of the clip its alternate streams. Named because the ratio is the fixture:
# a version picker whose entries report the same runtime cannot tell you which
# source you got.
ORIGIN_VERSION_LOCAL_SECONDS = 10

# And the local *alternate* of `strm-origin-primary-versions`, the set built
# the other way up. Distinct from both 10 and 30 so that a runtime alone says
# which of the two version fixtures you are looking at as well as which source
# within it.
ORIGIN_PRIMARY_LOCAL_SECONDS = 20

ORIGIN_FIXTURES = {
    "movie-strm-origin": ("Movies", "origin-movie.mkv"),
    "movie-strm-origin-versions": ("Movies", "origin-movie.mkv"),
    "movie-strm-origin-primary": ("Movies", "origin-movie.mkv"),
    "movie-strm-long": ("Movies", "origin-long.mp4"),
    "show-strm-origin": ("Shows", "origin-episode.mp4"),
}


def origin_targets(base_url: str) -> dict:
    """Fixture key -> URL, for a given origin base URL."""
    return {key: f"{base_url}/{name}"
            for key, (_lib, name) in ORIGIN_FIXTURES.items()}


def strm_targets(cfg) -> dict:
    """Every stream fixture's URL, remote and local-origin alike."""
    return {
        **{key: catalog.by_key(src).url for key, src in STRM_SOURCES.items()},
        **STRM_UNPLAYABLE,
        **origin_targets(cfg.stream_origin),
    }


def build_origin(cfg) -> list[dict]:
    """The clips the local-origin `.strm` fixtures point at.

    Deliberately not inside any library folder — `PhotoResolver` and the video
    resolvers scan what they are given, and a folder of media inside a library
    would turn the origin into items in their own right, which is the one thing
    a stream target must not be.
    """
    # Only what this run's libraries actually name. A `--only Movies` build
    # that produced the episode clip too would leave a file on the origin that
    # nothing points at, and `verify` would rightly say so.
    needed = sorted({name for lib, name in ORIGIN_FIXTURES.values()
                     if cfg.wants(lib)})

    made = []
    target_dir = origin.directory(cfg.root)
    for name in needed:
        ext, seconds, video, audios = ORIGIN_CLIPS[name]
        rec = _short(
            "Served over HTTP by the local origin",
            f"origin-{os.path.splitext(name)[0]}",
            duration=seconds, video=video, audios=audios, container=ext,
            notes="This clip is not a library item. It sits under "
                  ".stdjflib/origin/ and exists to be fetched over HTTP by "
                  "the .strm fixture that names it.")
        path = os.path.join(target_dir, name)
        if _emit(rec, path, cfg):
            made.append({"library": "Origin", "key": rec.key, "path": path})
    return made


# How Jellyfin decides that several files are one item in several versions,
# and which of them is the primary source. Both halves are worth knowing
# because both are what these fixtures are shaped to exercise.
#
# For a **movie**, `VideoListResolver.IsEligibleForMultiVersion` requires every
# filename in the folder to start with the folder's own name, and what follows
# to be either nothing at all, a leading `-`/`_`/`.`, or a bracketed tag. One
# file failing that disqualifies the whole folder — so the versions become
# three separate films, which is the failure this fixture is here to catch.
#
# For an **episode** the rule is different and much looser: files are grouped
# by the season and episode number parsed out of the name, so anything after
# `S01E01` is free-form and two files only have to agree on the number.
#
# Which version is primary is `OrganizeAlternateVersions`: a file named exactly
# like its folder wins outright (movies only — there is no such rule for
# episodes), otherwise the resolution in the name decides, matched as
# `[0-9]{2}[0-9]+[ip]` and sorted numerically descending, and a set that names
# no resolution anywhere falls back to sorting the filenames.
_VERSION_TAG = " - [{}]"


def version_path(base: str, tag: str, ext: str) -> str:
    """The path of one version of the item whose base path is `base`.

    `base` is the path a single-version item would have, without its
    extension. The bracket is not decoration: it is one of the two spellings
    `IsEligibleForMultiVersion` accepts, and the one that stays readable when
    the tag is an edition name rather than a resolution.
    """
    return base + _VERSION_TAG.format(tag) + "." + ext


# The three encodes of `multi-version`. Ordered as the server will order them,
# highest resolution first, so reading the table tells you which is primary.
MOVIE_VERSIONS = (
    ("Bluray-1080p", "mkv", Video(width=1920, height=1080, bitrate="4000k"),
     (Audio(encoder="aac"),)),
    ("WEBDL-720p", "mkv", Video(width=1280, height=720, bitrate="1500k"),
     (Audio(encoder="aac"),)),
    ("SDTV-480p", "mkv", Video(width=854, height=480, bitrate="700k"),
     (Audio(encoder="aac"),)),
)

# The alternates of `multi-version-editions`. No resolution appears in any tag,
# on purpose — these name an *edition*, which is what the bracket syntax is
# really for, and it means the primary can only come from the exact-name file.
# The audio differs as well as the picture so that switching version in a
# client is something you can hear rather than something you have to trust.
MOVIE_EDITIONS = (
    ("Directors Cut", "mkv", Video(width=1280, height=720, bitrate="1500k"),
     (Audio(encoder="ac3", channels=6),)),
    ("Theatrical", "mp4",
     Video(encoder="libx265", width=854, height=480, bitrate="700k"),
     (Audio(encoder="aac"),)),
)


def build_movies(root: str, cfg) -> list[dict]:
    """The naming-convention fixtures.

    Downloaded films are placed by `build.place_movies`, which owns the cache
    and the sidecar subtitles; this function only builds the synthetic
    fixtures that demonstrate each path convention.
    """
    made = []
    targets = strm_targets(cfg)
    for key, title, shape, plot in NAMING_CASES:
        year = YEAR
        safe = title.replace("/", "-").replace(":", " -")
        rec = _short(title, f"movie-{key}", notes=plot)

        if shape == "flat":
            name = f"{safe} ({year})"
            if key == "bracket-id":
                name += " [stdjflibid-0001]"
            media = os.path.join(root, f"{name}.mkv")
            if _emit(rec, media, cfg) and not cfg.dry_run:
                nfo.movie(os.path.join(root, f"{name}.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=1,
                          tags=["stdjflib", "naming"])
                # A film with no folder of its own: every image has to carry
                # the filename as a prefix, and a client that only ever saw
                # folder-per-movie libraries has never loaded one.
                artwork.sidecar_images(media, rec.key, title, cfg,
                                       kinds=artwork.SETS["movie"])
            made.append({"library": "Movies", "key": rec.key, "path": media})

        elif shape == "mismatch":
            folder = os.path.join(root, f"Folder Says This ({year})")
            media = os.path.join(folder, f"But The File Says That ({year}).mkv")
            if _emit(rec, media, cfg) and not cfg.dry_run:
                nfo.movie(os.path.join(folder, "movie.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=1,
                          tags=["stdjflib", "naming"])
                artwork.folder_images(folder, rec.key, title, cfg)
            made.append({"library": "Movies", "key": rec.key, "path": media})

        elif shape in ("versions", "editions"):
            folder = os.path.join(root, f"{safe} ({year})")
            base = os.path.join(folder, f"{safe} ({year})")
            if shape == "editions":
                # The exact-name file. Its presence is the whole point of this
                # case: it overrides the resolution sort and becomes the
                # primary source no matter what the alternates are called.
                _emit(_clip(rec, key=f"{rec.key}-primary",
                            video=Video(width=1920, height=1080,
                                        bitrate="4000k")),
                      base + ".mkv", cfg)
            table = MOVIE_EDITIONS if shape == "editions" else MOVIE_VERSIONS
            for tag, ext, video, audios in table:
                v = _clip(rec, key=f"{rec.key}-{tag}", video=video,
                          audios=audios, container=ext)
                _emit(v, version_path(base, tag, ext), cfg)
            if not cfg.dry_run:
                # One `movie.nfo` for the item, not one per version: the
                # versions are sources of a single film, and a metadata file
                # per source would be describing items that do not exist.
                nfo.movie(os.path.join(folder, "movie.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=1,
                          tags=["stdjflib", "naming"])
                artwork.folder_images(folder, rec.key, title, cfg)
            # `path` is the folder, which is what a build produced; `primary`
            # is the file Jellyfin records as the *item's* path once
            # `OrganizeAlternateVersions` has run, and the two are not the
            # same thing. Anything naming this item by path — a collection
            # member, for one — has to use the second, and the rule that picks
            # it differs between the two spellings: an exact-name file wins
            # outright, and with none the highest resolution does.
            made.append({"library": "Movies", "key": rec.key, "path": folder,
                         "primary": base + ".mkv" if shape == "editions"
                         else version_path(base, MOVIE_VERSIONS[0][0],
                                           MOVIE_VERSIONS[0][1])})

        elif shape == "strm-flat":
            name = f"{safe} ({year})"
            media = os.path.join(root, f"{name}.strm")
            if _strm(media, targets[rec.key], cfg) and not cfg.dry_run:
                nfo.movie(os.path.join(root, f"{name}.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=11,
                          tags=["stdjflib", "naming", "strm"])
                # A shortcut is never probed, so these images are not merely
                # the preferred artwork — they are the only artwork the item
                # can ever have. There is no embedded poster to fall back to
                # and no frame to grab one from.
                artwork.sidecar_images(media, rec.key, title, cfg,
                                       kinds=artwork.SETS["movie"])
            made.append({"library": "Movies", "key": rec.key, "path": media,
                         "stream": targets[rec.key]})

        elif shape == "strm-folder":
            folder = os.path.join(root, f"{safe} ({year})")
            media = os.path.join(folder, f"{safe} ({year}).strm")
            ok = _strm(
                media, targets[rec.key], cfg,
                header=["Written by stdjflib. Everything above the first URL "
                        "is a comment,",
                        "and everything below it is ignored.",
                        "",
                        "https://example.invalid/decoy-in-a-comment.mkv"],
                # The tab is deliberate: FetchShortcutInfo strips tabs before
                # it trims, so an indented URL is still a URL.
                trailing=["", "\thttps://example.invalid/second-url-never-read.mkv"])
            if ok and not cfg.dry_run:
                nfo.movie(os.path.join(folder, "movie.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=11,
                          tags=["stdjflib", "naming", "strm"])
                artwork.folder_images(folder, rec.key, title, cfg)
            made.append({"library": "Movies", "key": rec.key, "path": media,
                         "stream": targets[rec.key]})

        elif shape in ("strm-versions", "strm-origin-versions"):
            folder = os.path.join(root, f"{safe} ({year})")
            base = os.path.join(folder, f"{safe} ({year})")
            # The exact-name file, and the one thing here that is real media:
            # it is primary by the folder-name rule, so the item plays locally
            # unless a client is asked for the other source.
            #
            # Ten seconds against the origin clip's thirty, for the local-origin
            # spelling. A version picker whose two entries report the same
            # runtime cannot tell you which one you got, so a test asserting on
            # the source it switched to would pass without switching anything.
            local = _clip(rec, key=f"{rec.key}-local",
                          video=Video(width=1280, height=720, bitrate="1500k"))
            if shape == "strm-origin-versions":
                local = _clip(local, duration=ORIGIN_VERSION_LOCAL_SECONDS)
            _emit(local, base + ".mkv", cfg)
            remote = version_path(base, "Remote Stream", "strm")
            _strm(remote, targets[rec.key], cfg)
            if not cfg.dry_run:
                nfo.movie(os.path.join(folder, "movie.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=11,
                          tags=["stdjflib", "naming", "strm"])
                artwork.folder_images(folder, rec.key, title, cfg)
            made.append({"library": "Movies", "key": rec.key, "path": folder})
            # The stream file gets an entry of its own as well as the folder's:
            # it is one file among an item's several sources, so nothing else
            # in the manifest would name it and `verify` would never read it.
            made.append({"library": "Movies", "key": f"{rec.key}-remote",
                         "path": remote, "stream": targets[rec.key]})

        elif shape == "strm-origin-primary-versions":
            folder = os.path.join(root, f"{safe} ({year})")
            base = os.path.join(folder, f"{safe} ({year})")
            # The stream file is the one named exactly like its folder, and
            # that is the entire fixture. `OrganizeAlternateVersions` makes an
            # exact-name file the primary outright, so the *item's* path ends
            # in `.strm` — which is the disjunct that
            # `MediaSourceManager.GetPlaybackMediaSources` actually tests. The
            # remote probe therefore fires, and unlike the set built the other
            # way up, both sources report a real runtime.
            primary = base + ".strm"
            _strm(primary, targets[rec.key], cfg)
            _emit(_clip(rec, key=f"{rec.key}-local",
                        duration=ORIGIN_PRIMARY_LOCAL_SECONDS,
                        video=Video(width=1280, height=720, bitrate="1500k")),
                  version_path(base, "Local File", "mkv"), cfg)
            if not cfg.dry_run:
                nfo.movie(os.path.join(folder, "movie.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=11,
                          tags=["stdjflib", "naming", "strm"])
                artwork.folder_images(folder, rec.key, title, cfg)
            made.append({"library": "Movies", "key": rec.key, "path": folder})
            made.append({"library": "Movies", "key": f"{rec.key}-primary",
                         "path": primary, "stream": targets[rec.key]})

        elif shape == "parts":
            folder = os.path.join(root, f"{safe} ({year})")
            for part in (1, 2, 3):
                v = _clip(rec, key=f"{rec.key}-p{part}",
                          title=f"{title} part {part}")
                _emit(v, os.path.join(folder,
                                      f"{safe} ({year}) - part{part}.mkv"), cfg)
            if not cfg.dry_run:
                nfo.movie(os.path.join(folder, "movie.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=3,
                          tags=["stdjflib", "naming"])
                artwork.folder_images(folder, rec.key, title, cfg)
            made.append({"library": "Movies", "key": rec.key, "path": folder})

        elif shape == "extras":
            folder = os.path.join(root, f"{safe} ({year})")
            _emit(rec, os.path.join(folder, f"{safe} ({year}).mkv"), cfg)
            # Both spellings Jellyfin accepts: a suffix on the main file, and
            # the named sub-folders.
            _emit(_clip(rec, key=f"{rec.key}-tr", duration=6),
                  os.path.join(folder, f"{safe} ({year})-trailer.mkv"), cfg)
            for sub, label in (("extras", "An Extra"),
                               ("behind the scenes", "The Making Of"),
                               ("deleted scenes", "A Deleted Scene"),
                               ("interviews", "An Interview"),
                               ("trailers", "Another Trailer")):
                _emit(_clip(rec, key=f"{rec.key}-{sub}", duration=6),
                      os.path.join(folder, sub, f"{label}.mkv"), cfg)
            if not cfg.dry_run:
                nfo.movie(os.path.join(folder, "movie.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=1,
                          tags=["stdjflib", "naming", "extras"])
                # The one film carrying every image type Jellyfin has, so a
                # client can be pointed at a single title to see all of them.
                artwork.folder_images(folder, rec.key, title, cfg,
                                      kinds=artwork.SETS["everything"])
            made.append({"library": "Movies", "key": rec.key, "path": folder})

    made += _legacy_box_set(root, cfg)
    return made


# --------------------------------------------------------------------------
# The collection that is a folder of films
# --------------------------------------------------------------------------
#
# This one lives in the *Movies* library and it has to. `MovieResolver`
# refuses a `boxsets` library outright — `_validCollectionTypes` is movies,
# homevideos, musicvideos, tvshows, photos, and `IsInvalid` returns true for
# anything else — so a media file inside a box set in the Box Sets library
# resolves to nothing at all. (The comment above the file branch in
# `MovieResolver.Resolve` says "the collection type must be movies or
# boxsets". The code it sits on top of tests only for movies. The comment is
# wrong.)
#
# So the two shapes a collection comes in are split across two libraries, not
# by preference but by what resolves:
#
#   `Box Sets/`  collection.xml, members by path -> LinkedChildren
#   `Movies/`    a folder of films, no XML        -> children from the disk
#
# `BoxSet.IsLegacyBoxSet` is what tells them apart: a path outside the
# server's data directory **and** no linked children. Give this folder a
# collection.xml and it stops being this case.
#
# It is also the fixture that reaches Movies -> Collections in jellyfin-web,
# whose tab is `itemType: [BoxSet]` parented to the movies library — a
# collection in the Box Sets library is not in scope for that query and does
# not appear there.

LEGACY_BOX_SET = "The Legacy Shelf [boxset]"

# Different years on purpose: `DisplayOrder` defaults to PremiereDate, so a
# client ordering these by name instead is visibly wrong rather than
# ambiguous. Sort names are the reverse of the years for the same reason.
LEGACY_BOX_SET_FILMS = [
    ("legacy-shelf-late", "Zebra, The Last Film On The Shelf", 2014),
    ("legacy-shelf-early", "Aardvark, The First Film On The Shelf", 2019),
]

LEGACY_BOX_SET_PLOT = (
    "A collection with no collection.xml: the folder is the box set and the "
    "films inside it are its children, read off the disk on every scan rather "
    "than out of a metadata file. Jellyfin calls this shape a legacy box set "
    "and decides it by absence — no linked children, and a path outside its "
    "own data directory. Both films are ordinary Movie items as well, so they "
    "appear here and in the movie list unless the client collapses items that "
    "belong to a box set."
)

FILM_IN_BOX_SET_PLOT = (
    "One of the two films inside a box set folder. It resolves as a Movie in "
    "its own right — the box set is its parent, not its owner — so a client "
    "that does not collapse box set members shows it twice."
)


def _legacy_box_set(root: str, cfg) -> list[dict]:
    folder = os.path.join(root, LEGACY_BOX_SET)
    made = []
    for key, title, year in LEGACY_BOX_SET_FILMS:
        rec = _short(title, key, notes=FILM_IN_BOX_SET_PLOT)
        name = f"{title} ({year})"
        media = os.path.join(folder, f"{name}.mkv")
        if _emit(rec, media, cfg) and not cfg.dry_run:
            nfo.movie(os.path.join(folder, f"{name}.nfo"), key=rec.key,
                      title=title, plot=FILM_IN_BOX_SET_PLOT, year=year,
                      runtime_minutes=1, tags=["stdjflib", "collection"])
            # Loose files sharing a folder, so every image is prefixed —
            # the box set itself owns the folder and takes the unprefixed
            # names below.
            artwork.sidecar_images(media, rec.key, title, cfg,
                                   kinds=("poster",))
        made.append({"library": "Movies", "key": rec.key, "path": media})

    if made and not cfg.dry_run:
        artwork.folder_images(folder, "legacy-shelf", "The Legacy Shelf", cfg,
                              kinds=artwork.SETS["movie"])
    return made


# --------------------------------------------------------------------------
# Box Sets — collections that are a list of paths
# --------------------------------------------------------------------------
#
# Every collection here owns no media. Its members are films that already
# exist in `Movies/`, named by a path relative to the collection's own folder,
# which is what makes the library portable between a host path and the
# container's `/media`. See `boxsets.py` for the parser these are written
# against.
#
# Members are declared by manifest key rather than by path so that renaming a
# fixture in `NAMING_CASES` cannot silently empty a collection — a key that no
# longer exists is a build warning and then a `verify` failure, where a stale
# path would be neither.

BOX_SETS = [
    {
        "key": "boxset-linked",
        "folder": f"The Linked Collection {boxsets.MARKER}",
        "title": "The Linked Collection",
        "members": ["movie-loose-file", "movie-folder-mismatch",
                    "movie-unicode-title"],
        "plot": "A collection that owns no files. Its three members live in "
                "the Movies library and are named here by relative path, "
                "which is what Jellyfin stores as linked children. The name "
                "on screen comes from <LocalTitle> in collection.xml and not "
                "from the folder, whose [boxset] suffix is stripped before "
                "either is considered.",
    },
    {
        "key": "boxset-xml-only",
        "folder": "Collection Without The Marker",
        "title": "Collection Without The Marker",
        "members": ["movie-very-long-title", "movie-bracket-id"],
        "plot": "The same thing with no [boxset] in the folder name. "
                "BoxSetResolver takes a directory on either condition — the "
                "suffix or the presence of collection.xml — so this resolves "
                "as a collection on the strength of the file alone. Remove "
                "the file and the folder becomes an ordinary folder, which "
                "in a boxsets library resolves to nothing.",
    },
    {
        "key": "boxset-display-order",
        "folder": f"Display Order Is Ignored {boxsets.MARKER}",
        "title": "Display Order Is Ignored",
        "members": ["legacy-shelf-early", "legacy-shelf-late"],
        "display_order": "SortName",
        "plot": "This collection.xml asks for SortName and the server sorts "
                "by premiere date anyway, on both 10.11 and 12.0. The parser "
                "reads DisplayOrder and the saver writes it back out, but "
                "MergeDisplayOrder only copies the value when the target's is "
                "empty — and BoxSet's constructor has already set it to "
                "PremiereDate, so the parsed value is discarded before "
                "anything sees it. Its two films have years running opposite "
                "to their names, so the order on screen says which rule won: "
                "Zebra then Aardvark is the date, and the reverse would mean "
                "somebody fixed the merge. Both are also the filesystem "
                "children of The Legacy Shelf, which makes them the one pair "
                "here belonging to two collections at once.",
    },
    {
        "key": "boxset-cross-library",
        "folder": f"Two Libraries One Collection {boxsets.MARKER}",
        "title": "Two Libraries, One Collection",
        "members": ["standard-show", "movie-loose-file"],
        "plot": "A collection whose members are not all the same type and do "
                "not all come from the same library: a series from Shows and "
                "a film from Movies. A linked child can be any item, and a "
                "client that assumes a box set holds movies fails here. It is "
                "also the fixture for linkedChildAncestorIds, the /Items "
                "parameter that filters collections by the library their "
                "members came from — which exists on 12.0 and in no 10.11.",
    },
    {
        "key": "boxset-multi-version",
        "folder": f"Versions Inside A Collection {boxsets.MARKER}",
        "title": "Versions Inside A Collection",
        "members": ["movie-multi-version", "movie-multi-version-editions"],
        "plot": "Both multi-version films, each named by the file that is its "
                "primary source rather than by the folder that holds its "
                "three. The folder is not the item's path — Jellyfin records "
                "the primary version's file — so naming the folder here "
                "produces a member that resolves to nothing at all. The two "
                "spellings also pick their primary differently: one by an "
                "exact-name file, the other by the highest resolution. On "
                "12.0 these two are additionally the films that "
                "CollectionPostScanTask would refuse to add to an automatic "
                "collection, because it skips anything with a "
                "PrimaryVersionId; on 10.11 it adds them.",
    },
    {
        "key": "boxset-broken-member",
        "folder": f"One Member Is Missing {boxsets.MARKER}",
        "title": "One Member Is Missing",
        "members": ["movie-strm-loose"],
        # Deliberately naming nothing. Relative to the collection's folder,
        # like every other member, so the only thing wrong with it is that the
        # file is not there.
        "unresolvable": ["../../Movies/A Film Nobody Built (2020).mkv"],
        "plot": "One member that resolves and one that names a file which has "
                "never existed. This is the fixture for the difference "
                "between the two server versions: on 12.0 the missing one is "
                "dropped for good, because linked children live in a table "
                "whose child column is a non-nullable id and a path has "
                "nowhere to survive, so this collection holds one item "
                "forever. On 10.11 the link is kept as JSON on the item and "
                "would start working the moment the file appeared. Neither "
                "server tells anybody, so a collection that is short an item "
                "looks exactly like a collection that was built that way.",
    },
]


# --------------------------------------------------------------------------
# Auto Collections — the box sets the *server* builds
# --------------------------------------------------------------------------
#
# An ordinary movies library, distinguished only by what its NFOs say and by
# the one option `provision.py` turns on for it. `<set><name>` is read by
# `MovieNfoParser` into `Movie.CollectionName`; `CollectionPostScanTask` then
# groups every movie by that name and creates a box set per group — but only
# in libraries whose `AutomaticallyAddToCollection` is true, and it is false
# everywhere else here on purpose.
#
# The result does not live in this library, or in any library on disk. It is
# created through `CollectionManager.CreateCollectionAsync`, which puts a
# `<name> [boxset]` folder under `<data>/collections` and adds a library
# called "Collections" to hold it. So this is the one collection fixture that
# `verify` cannot check and `--fresh` rebuilds from nothing.
#
# Two rules in that task are worth having a fixture for, and the second one is
# a trap:
#
#   * **A set naming only one movie creates nothing.** The task's own comment
#     says so — `if (movieIds.Count >= 2)` — so a lone `<set>` is a field with
#     no visible effect anywhere.
#   * **An existing box set of the same name is added to rather than created.**
#     The lookup is `boxSets.FirstOrDefault(b => b.Name == collectionName)`
#     across *every* box set on the server, with no scope. Name a `<set>` after
#     one of the collections in `Box Sets/` and the task quietly pours movies
#     into that fixture. None of these names collide, and none of them should
#     be made to.

AUTO_SET = "The Automatic Set"
AUTO_SET_OF_ONE = "The Set Of One"

AUTO_COLLECTION_MOVIES = [
    ("auto-set-first", "First Of The Automatic Set", 2011, AUTO_SET),
    ("auto-set-second", "Second Of The Automatic Set", 2012, AUTO_SET),
    ("auto-set-lonely", "The Only Film In Its Set", 2013, AUTO_SET_OF_ONE),
    # No `<set>` at all, so nothing should sweep it up. A client showing this
    # one inside a collection is reading something other than the NFO.
    ("auto-set-none", "In No Set At All", 2014, None),
]

AUTO_SET_PLOT = (
    "One of two films whose NFO carries the same <set>. Nothing on disk says "
    "they belong together — no collection.xml, no shared folder — so the box "
    "set holding them exists only because the server built it during the scan "
    "that followed, and it lives in the server's own data directory rather "
    "than in this library. Delete the database and it is gone until the next "
    "scan; nothing here can be verified offline."
)

AUTO_SET_OF_ONE_PLOT = (
    "The only film naming its <set>, which is why that set does not exist. "
    "CollectionPostScanTask refuses to create a collection for fewer than two "
    "movies, so this NFO field is read, stored on the item as CollectionName, "
    "and produces nothing a client can navigate to."
)

AUTO_SET_NONE_PLOT = (
    "No <set> at all, in the one library where sets become collections. The "
    "control: it must appear in no box set whatsoever."
)


def build_auto_collections(root: str, cfg) -> list[dict]:
    made = []
    plots = {AUTO_SET: AUTO_SET_PLOT, AUTO_SET_OF_ONE: AUTO_SET_OF_ONE_PLOT,
             None: AUTO_SET_NONE_PLOT}
    for key, title, year, collection in AUTO_COLLECTION_MOVIES:
        plot = plots[collection]
        rec = _short(title, key, notes=plot)
        name = f"{title} ({year})"
        media = os.path.join(root, f"{name}.mkv")
        if _emit(rec, media, cfg) and not cfg.dry_run:
            nfo.movie(os.path.join(root, f"{name}.nfo"), key=rec.key,
                      title=title, plot=plot, year=year, runtime_minutes=1,
                      tags=["stdjflib", "collection"], collection=collection)
            artwork.sidecar_images(media, rec.key, title, cfg,
                                   kinds=artwork.SETS["movie"])
        made.append({"library": "Auto Collections", "key": rec.key,
                     "path": media, "set": collection})
    return made


def build_box_sets(root: str, cfg, members: dict) -> list[dict]:
    """Collections whose members are paths into the other libraries.

    `members` maps a manifest key to the path Jellyfin resolves that item at —
    the media file, and for a multi-version item its primary version's file
    rather than the folder the build recorded.

    A spec's `unresolvable` entries are written verbatim and are meant to
    resolve to nothing. They go into the file after the real members so that
    the ones which do resolve are not reordered by the presence of one that
    does not.
    """
    made = []
    for spec in BOX_SETS:
        folder = os.path.join(root, spec["folder"])
        paths = []
        for key in spec["members"]:
            target = members.get(key)
            if not target:
                print(f"    ! {spec['folder']}: no item built for {key!r}")
                continue
            paths.append(boxsets.member_path(folder, target))
        broken = list(spec.get("unresolvable", ()))

        if not cfg.dry_run:
            os.makedirs(folder, exist_ok=True)
            boxsets.write(folder, title=spec["title"], plot=spec["plot"],
                          members=paths + broken, year=YEAR,
                          display_order=spec.get("display_order"),
                          tags=["stdjflib", "collection", spec["key"]])
            # A collection has no media to extract an image from, and
            # `CollectionImageProvider` — which would build a collage out of
            # its members — is an IDynamicImageProvider, so the empty
            # `ImageFetchers` that keeps TMDB out switches it off as well.
            # These drawn images are the only ones the item can have.
            artwork.folder_images(folder, spec["key"], spec["title"], cfg,
                                  kinds=artwork.SETS["movie"])
        entry = {"library": "Box Sets", "key": spec["key"], "path": folder,
                 "members": paths}
        if broken:
            entry["unresolvable"] = broken
        made.append(entry)
    return made


# --------------------------------------------------------------------------
# Shows
# --------------------------------------------------------------------------

SHOWS = [
    {
        "key": "standard-show", "title": "The Standard Show", "year": 2020,
        "style": "seasons", "seasons": [(1, 6), (2, 4)], "specials": 2,
        "plot": "Two seasons in numbered season folders, plus specials in "
                "Season 00. The ordinary shape, and the control for the rest.",
    },
    {
        "key": "absolute-show", "title": "Absolute Numbering Show", "year": 2021,
        "style": "absolute", "episodes": 8,
        # The one metadata field in this file that changes what the *server*
        # does: `FillMissingEpisodeNumbersFromPath` compares `DisplayOrder`
        # against "absolute" and resolves the numbers differently when it
        # matches. Without it the server is guessing at what `- 003 -` means.
        "display_order": "absolute",
        "plot": "Episodes numbered straight through with no seasons and no "
                "season folders, the way fansubbed anime arrives. Jellyfin has "
                "to map absolute numbers onto a season itself.",
    },
    {
        "key": "datebased-show", "title": "Date Based Show", "year": 2019,
        "style": "dated", "episodes": 6,
        "plot": "Episodes identified by broadcast date rather than number, as "
                "daily programmes are. Sorting by 'episode number' is "
                "meaningless here and must fall back to the date.",
    },
    {
        "key": "double-show", "title": "Double Episode Show", "year": 2022,
        "style": "double", "episodes": 6,
        "plot": "Some files hold two episodes and are named with a span. Both "
                "episodes must be listed, both must resolve to the one file, "
                "and neither may go missing.",
    },
    {
        "key": "gappy-show", "title": "Show With Missing Episodes", "year": 2018,
        "style": "gaps", "episodes": 10,
        "plot": "Episodes 1, 2, 5, 6 and 9 only. The gaps are the test: a "
                "client that renders position rather than episode number "
                "mislabels everything after the first hole.",
    },
    {
        "key": "versions-show", "title": "Multi Version Show", "year": 2024,
        "style": "versions",
        "plot": "One season where most episodes exist in more than one "
                "encode. Jellyfin groups episode files by the season and "
                "episode number in the name, so these eight files must appear "
                "as four episodes, three of them with a source picker. "
                "Episode grouping arrived in Jellyfin 12.0; on 10.11 every "
                "file is its own episode, and that difference is the point of "
                "this show.",
    },
    {
        "key": "strm-show", "title": "Remote Stream Show", "year": 2025,
        "style": "strm",
        "plot": "A season assembled from stream files. Episode one is a .strm "
                "and nothing else, episode two is ordinary media for "
                "comparison, episode three has both — one local source and "
                "one remote, grouped as versions of the same episode — and "
                "episode four streams from an HTTP origin running on this "
                "machine, so it plays with the network unplugged. A "
                "shortcut is never probed on a scan, so an episode built this "
                "way has no runtime, no streams, no chapters and no extracted "
                "still until something asks to play it: everything a client "
                "renders in the episode list has to come from the NFO and the "
                "thumbnail beside it.",
    },
    {
        "key": "flat-show", "title": "Flat Show No Season Folders", "year": 2023,
        "style": "flat", "episodes": 6,
        "plot": "SxxExx files sitting directly in the show folder with no "
                "season directories at all.",
    },
]

# The versions of one episode, and the same two shapes as the movie tables
# above: tags that name a resolution, where the highest is primary, and tags
# that name a cut, where the primary falls to the filename sort. There is no
# exact-name rule for episodes — the grouping key is the season and episode
# number alone — so an episode's primary is always one of these two answers.
EPISODE_VERSIONS = (
    ("Bluray-1080p", "mkv", Video(width=1920, height=1080, bitrate="4000k"),
     (Audio(encoder="aac"),)),
    ("WEBDL-720p", "mp4", Video(width=1280, height=720, bitrate="1500k"),
     (Audio(encoder="aac"),)),
    ("SDTV-480p", "mkv", Video(width=854, height=480, bitrate="700k"),
     (Audio(encoder="aac"),)),
)

EPISODE_EDITIONS = (
    ("Aired", "mkv", Video(width=1280, height=720, bitrate="1500k"),
     (Audio(encoder="aac"),)),
    ("Uncensored", "mkv", Video(width=1280, height=720, bitrate="1500k"),
     (Audio(encoder="ac3", channels=6),)),
)

EPISODE_TITLES = [
    "Pilot", "The Second One", "Something Happens", "A Complication",
    "The Turn", "Consequences", "The Long Night", "Resolution",
    "An Epilogue", "The Reunion", "Aftermath", "Full Circle",
]


def _season_artwork(series_folder: str, season_folder: str | None,
                    season_no: int, show_key: str, show_title: str, cfg, *,
                    in_series_folder: bool) -> None:
    """One season's artwork, in whichever of the two places it is being tested.

    A season's Primary is a poster like the series' own — the shape does not
    change just because the item is a season, and a client that draws seasons
    at 16:9 crops the title off every one of them. It gets a backdrop and a
    landscape too, because Jellyfin reads all three for a season and a client
    that lays seasons out in a wide row needs the landscape to have one.

    `season_folder` is None for a season that has no folder — a flat or
    absolutely-numbered show still has a season one, and the series-folder
    spelling is then the only place its artwork can go. That case is why the
    two spellings are not interchangeable.
    """
    key = f"{show_key}-s{season_no}"
    label = "Specials" if season_no == 0 else f"Season {season_no}"
    title = f"{show_title}\n{label}"
    if in_series_folder:
        artwork.season_images(series_folder, season_no, key, title, cfg,
                              subtitle=label)
    else:
        artwork.folder_images(season_folder, key, title, cfg,
                              kinds=artwork.SETS["season"], subtitle=label)


def build_shows(root: str, cfg) -> list[dict]:
    made = []
    tasks: list = []
    targets = strm_targets(cfg)
    for show in SHOWS:
        title = show["title"]
        folder = os.path.join(root, f"{title} ({show['year']})")
        key = show["key"]

        if not cfg.dry_run:
            nfo.tvshow(os.path.join(folder, "tvshow.nfo"), key=key, title=title,
                       plot=show["plot"], year=show["year"],
                       tags=["stdjflib", "shows"],
                       display_order=show.get("display_order"))
            # One show carries every image type Jellyfin has, so a client can
            # be pointed at a single title to exercise all of them; the rest
            # get the ordinary series set.
            artwork.folder_images(
                folder, key, title, cfg,
                kinds=(artwork.SETS["everything"] if key == "standard-show"
                       else artwork.SETS["series"]))

        style = show["style"]
        rec = _short(title, f"{key}-ep", duration=10)

        # (season number, its folder, whether the artwork goes up in the
        # series folder). Filled in by each style below and drawn once at the
        # end, because every show has seasons whether or not it has season
        # *folders* — a flat or absolutely-numbered show still gets a season
        # one, and `season01-poster.jpg` in the series folder is the only
        # place artwork for a season with no folder of its own can live.
        season_art: list[tuple[int, str | None, bool]] = []

        def episode(season_no, ep_no, path, ep_title, aired, end=None,
                    versions=None, stream=None, streams=(), airs=None,
                    _rec=rec, _key=key, _show=show):
            """Queue one episode. `_rec`/`_key`/`_show` are bound as defaults
            because they change on every turn of the enclosing loop, and a
            closure over them would see only the last show's values.

            `versions` builds the same episode as several files instead of one,
            taking `path` as the base name and putting the bracketed tag before
            the extension. Every version gets its own NFO and its own still,
            even though only the primary's are read: which file the server
            elects primary is the server's decision, and a fixture that had to
            guess right would show an episode with no artwork when it guessed
            wrong. The duplicates also make the show behave sanely on a server
            with no episode grouping at all, where each file really is its own
            episode.

            `stream` makes the episode's own file a `.strm` playing that URL
            instead of media — `path` should then already end in `.strm`.
            `streams` is `(tag, url)` pairs adding stream files *beside*
            whatever else the episode has, which is how one episode ends up
            with a local source and a remote one.
            """
            ep_key = f"{_key}-s{season_no}e{ep_no}"
            base = os.path.splitext(path)[0]
            if versions:
                files = [(version_path(base, tag, ext), video, audios,
                          f"{ep_key}-{tag}", None)
                         for tag, ext, video, audios in versions]
            else:
                files = [(path, None, None, ep_key, stream)]
            files += [(version_path(base, tag, "strm"), None, None,
                       f"{ep_key}-{tag}", url) for tag, url in streams]

            def run():
                out = []
                for target, video, audios, file_key, url in files:
                    if url is not None:
                        if not _strm(target, url, cfg):
                            continue
                    else:
                        v = _clip(_rec, key=file_key, title=ep_title)
                        if video:
                            v = _clip(v, video=video, audios=audios,
                                      container=os.path.splitext(target)[1][1:])
                        if not _emit(v, target, cfg):
                            continue
                    if cfg.dry_run:
                        continue
                    # Keyed on the episode, not the file: every version is the
                    # same episode, so they must not disagree about its
                    # metadata or draw themselves a different still.
                    nfo.episode(os.path.splitext(target)[0] + ".nfo",
                                key=ep_key, title=ep_title,
                                plot=_show["plot"], season_no=season_no,
                                episode_no=ep_no, aired=aired,
                                runtime_minutes=1, end_episode=end,
                                show_title=_show["title"], **(airs or {}))
                    # `<episode>-thumb.jpg`, which Jellyfin registers as the
                    # episode's *Primary* — hence 16:9, and hence a row of
                    # episodes laying out landscape rather than as posters.
                    artwork.sidecar_images(
                        target, ep_key, ep_title, cfg, kinds=("thumb",),
                        subtitle=f"S{season_no:02d}E{ep_no:02d}")
                    entry = {"library": "Shows", "key": file_key,
                             "path": target}
                    if url is not None:
                        entry["stream"] = url
                    out.append(entry)
                return out

            tasks.append(run)

        if style == "seasons":
            for season_no, count in show["seasons"]:
                sdir = os.path.join(folder, f"Season {season_no:02d}")
                if not cfg.dry_run:
                    nfo.season(os.path.join(sdir, "season.nfo"), key=f"{key}-s{season_no}",
                               title=f"Season {season_no}", number=season_no,
                               plot=f"Season {season_no} of {title}.",
                               year=show["year"] + season_no - 1)
                # Both spellings Jellyfin accepts, one per season. Season
                # one is `season01-poster.jpg` up in the series folder, which
                # is where the resolver looks first; season two is
                # `Season 02/poster.jpg`, which is where people put it. A
                # client that only handles one shows half the posters.
                season_art.append((season_no, sdir, season_no == 1))
                for ep_no in range(1, count + 1):
                    et = EPISODE_TITLES[(ep_no - 1) % len(EPISODE_TITLES)]
                    episode(season_no, ep_no,
                            os.path.join(sdir, f"{title} - S{season_no:02d}E{ep_no:02d} - {et}.mkv"),
                            et, f"{show['year'] + season_no - 1}-0{season_no}-{ep_no:02d}")
            sdir = os.path.join(folder, "Season 00")
            # Season 0 is spelled `season-specials-…`, not `season00-…`. The
            # one season name a client is most likely to get wrong.
            season_art.append((0, sdir, True))
            # A special is not simply "at the end of the show". It says where
            # in watch order it belongs, and the two spellings for that are
            # both here: one that airs before the series starts and one that
            # airs after season one finishes. A client that ignores the fields
            # files both at the end, which looks tidy and is wrong.
            specials_airs = [
                {"airs_before_season": 1, "airs_before_episode": 1},
                {"airs_after_season": 1},
            ]
            for ep_no in range(1, show["specials"] + 1):
                episode(0, ep_no,
                        os.path.join(sdir, f"{title} - S00E{ep_no:02d} - Special {ep_no}.mkv"),
                        f"Special {ep_no}", f"{show['year']}-12-{24 + ep_no:02d}",
                        airs=specials_airs[(ep_no - 1) % len(specials_airs)])

        elif style == "absolute":
            season_art.append((1, None, True))
            for ep_no in range(1, show["episodes"] + 1):
                et = EPISODE_TITLES[(ep_no - 1) % len(EPISODE_TITLES)]
                episode(1, ep_no,
                        os.path.join(folder, f"{title} - {ep_no:03d} - {et}.mkv"),
                        et, f"{show['year']}-01-{ep_no:02d}")

        elif style == "dated":
            season_art.append((show["year"],
                               os.path.join(folder, f"Season {show['year']}"),
                               True))
            for ep_no in range(1, show["episodes"] + 1):
                date = f"{show['year']}-03-{ep_no + 9:02d}"
                sdir = os.path.join(folder, f"Season {show['year']}")
                episode(show["year"], ep_no,
                        os.path.join(sdir, f"{title} - {date}.mkv"),
                        f"Episode of {date}", date)

        elif style == "double":
            sdir = os.path.join(folder, "Season 01")
            season_art.append((1, sdir, True))
            ep_no = 1
            while ep_no <= show["episodes"]:
                if ep_no % 3 == 0:
                    et = f"{EPISODE_TITLES[ep_no - 1]} / {EPISODE_TITLES[ep_no]}"
                    episode(1, ep_no,
                            os.path.join(sdir, f"{title} - S01E{ep_no:02d}-E{ep_no + 1:02d} - {EPISODE_TITLES[ep_no - 1]}.mkv"),
                            et, f"{show['year']}-05-{ep_no:02d}", end=ep_no + 1)
                    ep_no += 2
                else:
                    et = EPISODE_TITLES[ep_no - 1]
                    episode(1, ep_no,
                            os.path.join(sdir, f"{title} - S01E{ep_no:02d} - {et}.mkv"),
                            et, f"{show['year']}-05-{ep_no:02d}")
                    ep_no += 1

        elif style == "gaps":
            sdir = os.path.join(folder, "Season 01")
            season_art.append((1, sdir, False))
            for ep_no in (1, 2, 5, 6, 9):
                et = EPISODE_TITLES[(ep_no - 1) % len(EPISODE_TITLES)]
                episode(1, ep_no,
                        os.path.join(sdir, f"{title} - S01E{ep_no:02d} - {et}.mkv"),
                        et, f"{show['year']}-09-{ep_no:02d}")

        elif style == "versions":
            sdir = os.path.join(folder, "Season 01")
            season_art.append((1, sdir, False))

            # Bound as defaults for the same reason `episode` binds its own:
            # they change on every turn of the enclosing loop. `episode` too,
            # which is itself redefined per show.
            def versioned(ep_no, versions, episode=episode, sdir=sdir,
                          title=title, show=show):
                et = EPISODE_TITLES[ep_no - 1]
                episode(1, ep_no,
                        os.path.join(sdir, f"{title} - S01E{ep_no:02d} - {et}.mkv"),
                        et, f"{show['year']}-04-{ep_no:02d}", versions=versions)

            # Three encodes, one of them in a different container, tagged with
            # resolutions — so the 1080p file is the primary source and the
            # other two are alternates behind it.
            versioned(1, EPISODE_VERSIONS)
            # One file, sitting in among the rest. The control: grouping keys
            # on the episode number, so an episode with a single encode has to
            # come through it untouched.
            versioned(2, None)
            # Two cuts, neither naming a resolution. The primary falls to the
            # filename sort, which puts `Aired` ahead of `Uncensored`.
            versioned(3, EPISODE_EDITIONS)
            # The same idea with a folder per episode, directly under the
            # series rather than inside `Season 01`. `SeasonResolver` declines
            # a folder whose name parses to an episode number instead of a
            # season, which is what lets the two files inside it resolve as
            # versions of S01E04.
            ep4 = f"{title} - S01E04 - {EPISODE_TITLES[3]}"
            episode(1, 4, os.path.join(folder, ep4, ep4 + ".mkv"),
                    EPISODE_TITLES[3], f"{show['year']}-04-04",
                    versions=EPISODE_VERSIONS[:2])

        elif style == "strm":
            sdir = os.path.join(folder, "Season 01")
            season_art.append((1, sdir, False))
            names = [f"{title} - S01E{n:02d} - {EPISODE_TITLES[n - 1]}"
                     for n in (1, 2, 3, 4)]
            # Episode one: a stream file standing on its own, which is the
            # shape a scraper-fed library is entirely made of.
            episode(1, 1, os.path.join(sdir, names[0] + ".strm"),
                    EPISODE_TITLES[0], f"{show['year']}-06-01",
                    stream=targets["show-strm-episode"])
            # Episode two: ordinary media, so the difference between a probed
            # episode and a shortcut is visible in one list rather than across
            # two shows.
            episode(1, 2, os.path.join(sdir, names[1] + ".mkv"),
                    EPISODE_TITLES[1], f"{show['year']}-06-02")
            # Episode three: both at once. Episode grouping keys on the season
            # and episode number parsed out of the name and nothing else, so
            # the extension is free to differ — one episode, a local source and
            # a remote one behind the same source picker.
            episode(1, 3, os.path.join(sdir, names[2] + ".mkv"),
                    EPISODE_TITLES[2], f"{show['year']}-06-03",
                    streams=(("Remote Stream",
                              targets["show-strm-version"]),))
            # Episode four: the same as episode one, but served from this
            # machine. The episode an end-to-end playback test can use with
            # the network unplugged.
            episode(1, 4, os.path.join(sdir, names[3] + ".strm"),
                    EPISODE_TITLES[3], f"{show['year']}-06-04",
                    stream=targets["show-strm-origin"])

        elif style == "flat":
            season_art.append((1, None, True))
            for ep_no in range(1, show["episodes"] + 1):
                et = EPISODE_TITLES[(ep_no - 1) % len(EPISODE_TITLES)]
                episode(1, ep_no,
                        os.path.join(folder, f"{title} - S01E{ep_no:02d} - {et}.mkv"),
                        et, f"{show['year']}-02-{ep_no:02d}")

        if not cfg.dry_run:
            for season_no, sdir, in_series in season_art:
                _season_artwork(folder, sdir, season_no, key, title, cfg,
                                in_series_folder=in_series)

        made.append({"library": "Shows", "key": key, "path": folder})

    # Every episode across every show, in one pool rather than seven. One task
    # is one episode and yields one entry per file, which is more than one
    # wherever the episode exists in several versions.
    groups = _run_all(tasks, cfg)
    made += [item for group in groups for item in (group or [])]
    return made


# --------------------------------------------------------------------------
# Music
# --------------------------------------------------------------------------

ALBUMS = [
    {"key": "flac-album", "artist": "The Reference Tones",
     "album": "Lossless Sessions", "year": 2020, "codec": "flac", "ext": "flac",
     "tracks": 6, "art": "embedded",
     "note": "FLAC with artwork embedded in the file rather than beside it."},
    {"key": "mp3-album", "artist": "The Reference Tones",
     "album": "Compressed Sessions", "year": 2018, "codec": "libmp3lame",
     "ext": "mp3", "tracks": 5, "art": "folder",
     "note": "MP3 with a folder.jpg and no embedded art."},
    {"key": "opus-album", "artist": "Nightly Build",
     "album": "Modern Codecs", "year": 2022, "codec": "libopus", "ext": "opus",
     "tracks": 4, "art": "embedded",
     "note": "Opus in Ogg."},
    {"key": "alac-album", "artist": "Nightly Build",
     "album": "Apple Lossless", "year": 2021, "codec": "alac", "ext": "m4a",
     "tracks": 4, "art": "folder",
     "note": "ALAC in MP4."},
    {"key": "va-album", "artist": "Various Artists",
     "album": "A Compilation", "year": 2019, "codec": "flac", "ext": "flac",
     "tracks": 6, "art": "folder", "various": True,
     "note": "A compilation where every track has a different artist but the "
             "album artist is Various Artists. Grouping by track artist "
             "shatters this album into six."},
    {"key": "multidisc", "artist": "The Reference Tones",
     "album": "Two Discs", "year": 2017, "codec": "flac", "ext": "flac",
     "tracks": 4, "discs": 2, "art": "folder",
     "note": "A two-disc set. Track numbering restarts on disc two, so a "
             "client ignoring the disc number shows two track ones."},
    {"key": "untagged", "artist": "Unknown Artist", "album": "Untagged Album",
     "year": 0, "codec": "libmp3lame", "ext": "mp3", "tracks": 3,
     "art": "none", "untagged": True,
     "note": "No tags at all. Everything must come from the path, or from "
             "nowhere."},
    {"key": "unicode-album", "artist": "アーティスト", "album": "アルバム",
     "year": 2023, "codec": "flac", "ext": "flac", "tracks": 4, "art": "embedded",
     "note": "Japanese artist, album and track names."},
    {"key": "strm-album", "artist": "Nightly Build", "album": "Remote Sessions",
     "year": 2025, "codec": "", "ext": "strm", "tracks": 3, "art": "folder",
     "stream": True,
     "note": "Tracks that are stream files rather than audio. `.strm` is in "
             "Jellyfin's audio extension list as well as its video one, so in "
             "a music library the audio resolver claims it and the item is a "
             "track — the extension decides the type, and what the URL points "
             "at is never consulted. Nothing is probed, so there are no tags "
             "and no embedded cover: the album art can only be the folder.jpg "
             "beside them, and the track number and artist are simply absent. "
             "The shortcut goes no further than that. `BaseItem.GetVersionInfo` "
             "substitutes the URL for the file only inside `if (item is Video)`, "
             "so a track's media source stays the .strm on disk — protocol "
             "File, IsRemote false. Measured on 12.0: this resolves and does "
             "not play, and a client is handed a text file to open."},
]

TRACK_NAMES = ["Opening", "Second Movement", "Interlude", "The Long One",
               "Reprise", "Closing", "Hidden Track", "Bonus"]


def build_music(root: str, cfg) -> list[dict]:
    """One task per album, tracks sequential within it.

    The album is the unit because the embedded-art case writes one cover file,
    uses it for every track, then deletes it — a lifecycle that would need
    locking if two albums shared a worker mid-flight.
    """
    targets = strm_targets(cfg)

    def album_task(album):
        def run():
            out = []
            artist_dir = os.path.join(root, album["artist"])
            album_dir = os.path.join(
                artist_dir,
                f"{album['album']} ({album['year']})" if album["year"]
                else album["album"])
            discs = album.get("discs", 1)
            art_path = None
            if not cfg.dry_run:
                # Album art is square, not a 2:3 poster. The server says so
                # itself — MusicAlbum and MusicArtist both override
                # `GetDefaultPrimaryImageAspectRatio()` to return 1 — and
                # clients lay music out in square cards on the strength of it.
                # A portrait cover is therefore pillarboxed or cropped, and
                # which of the two it is is the bug worth finding.
                name = (artwork.filename("square") if album["art"] == "folder"
                        else ".cover.jpg")
                if album["art"] != "none":
                    art_path = artwork.draw(
                        "square", album["key"], album["album"],
                        os.path.join(album_dir, name), cfg,
                        subtitle=album["artist"])

            for disc in range(1, discs + 1):
                target = (album_dir if discs == 1
                          else os.path.join(album_dir, f"Disc {disc}"))
                for n in range(1, album["tracks"] + 1):
                    title = TRACK_NAMES[(n - 1) % len(TRACK_NAMES)]
                    track_artist = (f"Guest Artist {n}" if album.get("various")
                                    else album["artist"])
                    path = os.path.join(
                        target, f"{n:02d} - {title}.{album['ext']}")
                    if _audio_track(
                            path, cfg, album, disc, n, title, track_artist,
                            art_path if album["art"] == "embedded" else None):
                        entry = {"library": "Music",
                                 "key": f"{album['key']}-d{disc}t{n}",
                                 "path": path}
                        if album.get("stream"):
                            entry["stream"] = targets["music-strm"]
                        out.append(entry)
            # The embedded cover was only ever a staging file.
            if album["art"] == "embedded" and art_path and os.path.exists(art_path):
                os.unlink(art_path)
            out.append({"library": "Music", "key": album["key"],
                        "path": album_dir})
            return out
        return run

    albums = [a for a in ALBUMS
              if a["ext"] != "opus" or ff.have(cfg.ffmpeg, "libopus")]
    results = _run_all([album_task(a) for a in albums], cfg)
    made = [item for group in results for item in (group or [])]

    # Artist artwork, once per artist rather than once per album — two albums
    # by one artist would otherwise race for the same file. An artist's
    # Primary is square like an album's, and the backdrop and logo are what a
    # client draws behind an artist page.
    if not cfg.dry_run:
        for artist in dict.fromkeys(a["artist"] for a in albums):
            artwork.folder_images(os.path.join(root, artist),
                                  f"artist-{artist}", artist, cfg,
                                  kinds=artwork.SETS["artist"])
    return made


def _audio_track(path: str, cfg, album, disc: int, n: int, name: str,
                 track_artist: str, cover: str | None) -> bool:
    """One tagged audio file. Tags come from ffmpeg's metadata options."""
    if album.get("stream"):
        # A shortcut has no tags at all, so `untagged` would be redundant here
        # and `cover` has nothing to embed into — the two things this function
        # otherwise exists to do.
        return _strm(path, strm_targets(cfg)["music-strm"], cfg)
    if cfg.dry_run:
        return True
    if cfg.artwork_only:
        # The audio is already encoded and is not what this run is about —
        # but for an album whose art lives *inside* the files, the picture is
        # the only thing that is stale, and leaving it would mean "redraw
        # every image" quietly skipped a third of the music library.
        if (cover and os.path.exists(cover) and os.path.exists(path)
                and album["ext"] in ("flac", "mp3", "m4a")):
            return _reembed_cover(path, cover, cfg)
        return os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A different pitch per track, so you can hear which one is playing and
    # whether shuffle actually shuffled.
    freq = 220 * (2 ** ((n - 1) / 12.0))
    seconds = album.get("seconds", 20)
    argv = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i",
            f"aevalsrc=exprs=0.3*sin(2*PI*{freq:.1f}*t)|0.3*sin(2*PI*{freq * 1.5:.1f}*t)"
            f":c=stereo:s=44100:d={seconds}"]
    embed = cover and os.path.exists(cover) and album["ext"] in ("flac", "mp3", "m4a")
    if embed:
        argv += ["-i", cover]
    argv += ["-map", "0:a"]
    if embed:
        argv += ["-map", "1:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    argv += ["-c:a", album["codec"], "-t", str(seconds)]
    if album["codec"] == "libmp3lame":
        argv += ["-b:a", album.get("bitrate", "192k")]

    if not album.get("untagged"):
        argv += [
            "-metadata", f"title={name}",
            "-metadata", f"artist={track_artist}",
            "-metadata", f"album_artist={album['artist']}",
            "-metadata", f"album={album['album']}",
            "-metadata", f"track={n}/{album['tracks']}",
            "-metadata", f"disc={disc}/{album.get('discs', 1)}",
            "-metadata", f"genre={'Electronic'}",
        ]
        if album["year"]:
            argv += ["-metadata", f"date={album['year']}"]
    tmp = ff.temp_path(path)
    argv += [tmp]
    try:
        ff.run(argv, verbose=cfg.verbose, timeout=300)
        os.replace(tmp, path)
        return True
    except (ff.FFmpegError, OSError) as exc:
        print(f"    ! {os.path.basename(path)}: {str(exc).splitlines()[-1][:110]}")
        return False


def _reembed_cover(path: str, cover: str, cfg) -> bool:
    """Swap the picture inside an audio file, keeping the audio bit-for-bit.

    `-c:a copy` is the point: re-encoding to change a cover would make the
    library's audio differ from the one everyone else built, for the sake of
    a thumbnail.
    """
    tmp = ff.temp_path(path)
    argv = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-i", path, "-i", cover,
            # Only the audio from the original — an mp3 that already carries a
            # cover would otherwise end up with two.
            "-map", "0:a", "-map", "1:v", "-c:a", "copy",
            "-c:v", "mjpeg", "-disposition:v", "attached_pic", tmp]
    try:
        ff.run(argv, verbose=cfg.verbose, timeout=120)
        os.replace(tmp, path)
        return True
    except (ff.FFmpegError, OSError) as exc:
        print(f"    ! {os.path.basename(path)}: {str(exc).splitlines()[-1][:110]}")
        if os.path.exists(tmp):
            os.unlink(tmp)
        return False


# --------------------------------------------------------------------------
# Photos, Home Videos, Books, Music Videos
# --------------------------------------------------------------------------

# EXIF orientation 1-8. A viewer that ignores the tag renders 2-8 wrong, and
# each wrong one is wrong in a visibly different way.
ORIENTATIONS = {
    1: "normal", 2: "mirrored", 3: "rotated 180", 4: "mirrored, 180",
    5: "mirrored, 270 CW", 6: "rotated 90 CW", 7: "mirrored, 90 CW",
    8: "rotated 270 CW",
}


def build_photos(root: str, cfg) -> list[dict]:
    made = []
    album = os.path.join(root, "EXIF Orientation")
    for code, label in ORIENTATIONS.items():
        path = os.path.join(album, f"orientation-{code}-{label.replace(', ', '-').replace(' ', '-')}.jpg")
        if cfg.dry_run:
            continue
        # The picture says which way up it should appear; the EXIF tag says how
        # to get there. Disagreement is visible at a glance.
        # 3:2, because a square would hide half the orientations: mirroring
        # a square looks like rotating it.
        got = artwork.draw("photo", f"exif-{code}", f"TOP\nEXIF {code}", path,
                           cfg, subtitle=label)
        if got:
            _set_exif_orientation(path, code, cfg)
            made.append({"library": "Photos", "key": f"exif-{code}", "path": path})

    formats = os.path.join(root, "Image Formats")
    for ext, note in (("jpg", "baseline JPEG"), ("png", "PNG with alpha"),
                      ("webp", "WebP"), ("gif", "GIF")):
        path = os.path.join(formats, f"format-{ext}.{ext}")
        if cfg.dry_run:
            continue
        if artwork.draw("photo", f"fmt-{ext}", ext.upper(), path, cfg,
                        subtitle=note):
            made.append({"library": "Photos", "key": f"fmt-{ext}", "path": path})

    big = os.path.join(formats, "very-large-8000x6000.jpg")
    if not cfg.dry_run:
        try:
            ff.run([cfg.ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
                    "-i", "testsrc2=s=8000x6000:d=1", "-frames:v", "1",
                    "-q:v", "4", big], verbose=cfg.verbose, timeout=180)
            made.append({"library": "Photos", "key": "photo-large", "path": big})
        except (ff.FFmpegError, OSError):
            pass
    return made


def _set_exif_orientation(path: str, code: int, cfg) -> None:
    """Write an EXIF orientation tag into a JPEG by splicing in an APP1 segment.

    ffmpeg cannot write EXIF, and the whole point of these files is the tag, so
    the segment is assembled by hand. It is a minimal little-endian TIFF header
    with exactly one IFD entry.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if not data.startswith(b"\xff\xd8"):
        return
    tiff = (b"II\x2a\x00" + (8).to_bytes(4, "little")        # header, IFD at 8
            + (1).to_bytes(2, "little")                       # one entry
            + (0x0112).to_bytes(2, "little")                  # Orientation
            + (3).to_bytes(2, "little")                       # SHORT
            + (1).to_bytes(4, "little")                       # count
            + code.to_bytes(2, "little") + b"\x00\x00"        # value, padded
            + (0).to_bytes(4, "little"))                      # no next IFD
    payload = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    # Insert immediately after SOI, before whatever APP segment ffmpeg wrote.
    with open(path, "wb") as fh:
        fh.write(data[:2] + app1 + data[2:])


def build_home_videos(root: str, cfg) -> list[dict]:
    made = []
    for folder, items in (
        ("2019 Holiday", ["Arrival", "The Beach", "Going Home"]),
        ("2020 Garden", ["Spring", "Summer"]),
    ):
        for i, name in enumerate(items, 1):
            rec = _short(name, f"home-{folder}-{i}", duration=8,
                         video=Video(width=1280, height=720, fps="30",
                                     bitrate="2000k"))
            path = os.path.join(root, folder, f"{name}.mp4")
            rec = _clip(rec, container="mp4")
            if _emit(rec, path, cfg):
                if not cfg.dry_run:
                    # Deliberately mixed: some 16:9 stills, some 2:3 posters.
                    # jellyfin-web shapes a row from the *median* aspect ratio
                    # of what is in it, so a folder holding both is the case
                    # where the median decides against half the artwork — the
                    # Home Videos shape mismatch, reproducible on demand.
                    kind = "thumb" if i % 2 else "poster"
                    artwork.sidecar_images(path, rec.key, name, cfg,
                                           kinds=(kind,), subtitle=folder)
                made.append({"library": "Home Videos", "key": rec.key,
                             "path": path})
    return made


def build_music_videos(root: str, cfg) -> list[dict]:
    made = []
    for artist, tracks in (("The Reference Tones", ["Opening", "Reprise"]),
                           ("Nightly Build", ["Modern Codecs"])):
        for i, track in enumerate(tracks, 1):
            rec = _short(track, f"mv-{artist}-{i}", duration=10,
                         video=Video(width=1280, height=720, bitrate="1500k"))
            rec = _clip(rec, container="mp4")
            path = os.path.join(root, artist, f"{artist} - {track}.mp4")
            if _emit(rec, path, cfg):
                if not cfg.dry_run:
                    nfo.musicvideo(os.path.splitext(path)[0] + ".nfo",
                                   key=rec.key, title=track, artist=artist,
                                   album="Singles", year=YEAR,
                                   plot=f"Music video for {track}.",
                                   runtime_minutes=1)
                    # Several videos share the artist's folder, so these can
                    # only be sidecars. Poster and still both, because a
                    # music video is the one item type where clients disagree
                    # about which shape the row should be.
                    artwork.sidecar_images(path, rec.key, track, cfg,
                                           kinds=("poster", "thumb"),
                                           subtitle=artist)
                made.append({"library": "Music Videos", "key": rec.key,
                             "path": path})
    return made


# --------------------------------------------------------------------------
# Mixed Content — videos and photographs in one tree
# --------------------------------------------------------------------------

# Jellyfin's "Home videos and photos" library holds both kinds at once, and a
# real one is never tidy about it: a folder is all video, all photo, both, or
# a container for more folders, and which of those it is changes between
# siblings. `Home Videos/` is deliberately tidy — dated folders of clips — so
# nothing there asks the question this library exists to ask.
#
# Two things are being tested. The first is that a client can render a folder
# holding two different *kinds* of item without one of them disappearing or
# being counted wrongly. The second is shape: jellyfin-web picks a row's
# layout from the **median** aspect ratio of what is in it, so a folder of
# portrait photographs beside a folder of 16:9 clips is exactly where that
# decision gets made — and the odd one out in each folder is what shows
# whether the row shaped itself around the median or around the first item.
MIXED_SHAPES = {
    "landscape": (1800, 1200),
    "portrait": (1200, 1800),
    "square": (1500, 1500),
    "wide": (1920, 1080),
}

# folder (relative, "" for the library root), videos, photos, the shape most
# of the photos are, the shape the last one is instead, and what it is for.
MIXED_CONTENT = [
    ("", 1, 1, "landscape", "portrait",
     "A clip and a photograph loose in the library root, with no folder of "
     "their own. The root of a mixed library is itself a mixed folder, which "
     "a client that only ever renders albums never meets."),
    ("Both At Once", 2, 4, "landscape", "portrait",
     "One folder holding videos and photographs together. Sorting, counting "
     "and the row's shape all have to cope with two kinds at once."),
    ("Videos Only", 3, 0, "wide", "wide",
     "Nothing but clips, so the folder reads as a video album."),
    ("Photos Only", 0, 6, "portrait", "wide",
     "Nothing but photographs, and portrait ones — the median lands on 2:3 "
     "and the single wide frame is the one that has to survive it."),
    ("Trips/2019/Spring", 1, 3, "square", "landscape",
     "Three levels down, and mixed again. Depth is the test: a client that "
     "handles one level of nesting can still lose its breadcrumb at three."),
    ("Trips/2019/Summer", 0, 4, "landscape", "square",
     "A sibling of the folder above that holds only photographs, so two "
     "folders in the same row describe themselves differently."),
    ("Trips/2020", 2, 0, "wide", "wide",
     "A sibling at a shallower depth holding only clips, so the tree is "
     "uneven as well as mixed."),
]


def build_mixed_content(root: str, cfg) -> list[dict]:
    """One task per folder, since each is a handful of small encodes and draws."""
    def task(entry):
        folder, n_videos, n_photos, dominant, odd, note = entry

        def run():
            out = []
            target = os.path.join(root, folder) if folder else root
            label = folder.replace("/", " ") or "Root"
            slug = safe_name(label).lower().replace(" ", "-")

            for i in range(1, n_videos + 1):
                key = f"mixed-{slug}-v{i}"
                name = f"{label} Clip {i}"
                rec = _clip(_short(name, key, duration=6, notes=note,
                                   video=Video(width=1280, height=720,
                                               bitrate="1500k")),
                            container="mp4")
                path = os.path.join(target, f"{name}.mp4")
                if not _emit(rec, path, cfg):
                    continue
                # Two clips in three carry their own still; the third has
                # none, so the folder exercises the sidecar path and whatever
                # a client falls back to, side by side rather than in
                # different libraries.
                if not cfg.dry_run and i % 3:
                    artwork.sidecar_images(path, key, name, cfg,
                                           kinds=("thumb",), subtitle=label)
                out.append({"library": "Mixed Content", "key": key,
                            "path": path})

            for i in range(1, n_photos + 1):
                key = f"mixed-{slug}-p{i}"
                name = f"{label} Photo {i:02d}"
                path = os.path.join(target, f"{name}.jpg")
                if cfg.dry_run:
                    out.append({"library": "Mixed Content", "key": key,
                                "path": path})
                    continue
                # The last one is deliberately the wrong shape for its row.
                shape = MIXED_SHAPES[odd if i == n_photos else dominant]
                if artwork.draw("photo", key, name, path, cfg,
                                subtitle=label, seq=i,
                                text=not cfg.use_artwork, size=shape):
                    out.append({"library": "Mixed Content", "key": key,
                                "path": path})
            return out
        return run

    groups = _run_all([task(entry) for entry in MIXED_CONTENT], cfg)
    return [item for group in groups for item in (group or [])]


# --------------------------------------------------------------------------
# Books
# --------------------------------------------------------------------------
#
# The whole library is filenames and archive members, so the tables below are
# the deliverable. Four rules of Jellyfin's decide every one of them, and none
# of them is guessable from the outside:
#
# 1. **A directory holding exactly one supported file is one book**, named
#    after the *directory* (`BookResolver.GetBook`). Only `.azw .azw3 .cb7
#    .cbr .cbt .cbz .epub .mobi .pdf` count towards the "exactly one", so an
#    NFO, a `.xml` sidecar or a poster beside it is invisible to the tally.
#    Two supported files and the rule stops applying to the whole folder, and
#    every file in it is resolved on its own instead.
#
# 2. So **the filename parser is only reachable in a folder that holds more
#    than one book.** `BOOK_SHELF` is that folder, and it is the only place
#    `BookFileNameParser` runs at all.
#
# 3. **The two paths disagree about SeriesName.** A loose file falls back to
#    its parent directory's name; a directory-book falls back to the empty
#    string. Same file, different answer, depending only on what else is in
#    the folder.
#
# 4. **Books read no NFO.** There is no `BookNfoParser` and no `BookNfoSaver`
#    in `MediaBrowser.XbmcMetadata` — nothing parses one for a Book or an
#    AudioBook. Metadata comes from the formats themselves, which is why the
#    dialect tables below exist and why there are no `.nfo` files here.

# The shelf: one folder holding several books, which is what switches the
# resolver from the directory rule to per-file parsing. Everything about these
# filenames is load-bearing — they are the test.
#
# `BookFileNameParser` tries five regexes in order and takes the first that
# matches, so the shape of the name decides which fields exist at all.
BOOK_SHELF = "Ines Imani"

# **`dc:title` in an EPUB beats the filename.** `EpubProvider` reads the OPF
# and overwrites `Name`, so an EPUB whose internal title disagrees with its
# filename hides whatever the parser made of the filename — which is exactly
# how the three author folders above come back named after their books rather
# than after their folders. That case is worth having and it is theirs. Here
# it would destroy the fixture, so every EPUB below embeds **the name the
# parser should produce**, and `test_books.py` holds the two to each other.
#
# The one row the parser gives *no* name to cannot be an EPUB at all — any
# `dc:title` would invent one — so it is a PDF, which no provider reads.
#
# (stem, extension, key, the title to embed / None where the parser gives none,
#  pages for a PDF, what the parser should make of it)
BOOK_SHELF_FILES = [
    # Regex 2 — the Goodreads spelling, and the only one that yields all four
    # fields at once. This is the case a client's series grouping should work
    # from.
    ("Ascent (The Meridian Cycle, #1) (2018)", "epub", "book-shelf-vol1",
     "Ascent", None,
     "name, series, index and year, all four from the filename"),
    ("Descent (The Meridian Cycle, #2) (2019)", "epub", "book-shelf-vol2",
     "Descent", None,
     "the second volume of the same series, so the grouping has something "
     "to group"),
    # Regex 1 — matches first and has **no `name` group at all**, so
    # `BookResolver` sets `Name` to the empty string and
    # `ResolverHelper.EnsureName` then backfills it from the filename. The
    # result is an item whose *title is a filename*, `#` and bracketed year
    # and all, sitting in a series with an index and a year that were parsed
    # correctly. Measured on 12.0.
    #
    # A PDF rather than an EPUB because a `dc:title` would land on top of the
    # backfill and hide it — which is what this fixture did until it was
    # measured.
    ("The Meridian Cycle #3 (2020)", "pdf", "book-shelf-vol3", None, 3,
     "series, index and year, and a Name that is the raw filename — regex 1 "
     "has no name group, so the server backfills one"),
    # Regex 3 — index and name, and no series, so SeriesName falls back to
    # the parent directory. That fallback is the rule this file proves.
    ("01 - The Early Years (2015)", "epub", "book-shelf-numbered",
     "The Early Years", None,
     "index and name; no series in the filename, so SeriesName falls back "
     "to the parent folder"),
    # The comic volume/chapter convention, which is a second regex applied to
    # whatever the first one called the name. The suffix is *not* stripped, so
    # the name it produces still carries "v02 c015".
    ("Adrift v02 c015", "epub", "book-shelf-volume-chapter",
     "Adrift v02 c015", None,
     "ParentIndexNumber 2 and IndexNumber 15 from the v/c suffix, which "
     "stays in the name"),
    # Regex 4 — name and year only. A PDF, so the page-count case lands on
    # the loose-file path rather than the directory one, and with a different
    # page count from the other one so the number is visibly read rather than
    # echoed.
    ("The Standard Manual (1994)", "pdf", "book-pdf", "The Standard Manual", 6,
     "a PDF: the server counts its pages with PDFium and stores "
     "pageCount * 10000 as RunTimeTicks, and extracts no cover at all"),
]

# One book alone in a folder of its own — the directory rule, deliberately
# rather than by accident. The *folder* name is what gets parsed, and
# SeriesName comes back empty rather than falling back to the parent, which
# is the opposite of what the same filename does on the shelf above.
BOOK_ALONE_AUTHOR = "Jo Jansen"
BOOK_ALONE = "The Solitary Volume (2001)"
# What `BookFileNameParser` makes of that folder name, and therefore what the
# EPUB inside must call itself.
BOOK_ALONE_PARSED_NAME = "The Solitary Volume"

# The formats Jellyfin catalogues and nothing can open. `BookResolver` takes
# them, so they browse with metadata and artwork like any other book, and
# then every client dead-ends: jellyfin-web's three players claim epub, pdf
# and the comic archives, and nothing else. What a client does at that point —
# offer a download, say the format is unsupported, hide the play button — is
# the decision this folder exists to be tested against.
#
# Two files, so the folder is not itself a one-file directory-book.
#
# NOT `.cba`: it looks like it belongs and the server does not accept it.
# `_validExtensions` is exactly `.azw .azw3 .cb7 .cbr .cbt .cbz .epub .mobi
# .pdf` — a `.cba` resolves to nothing and would sit in the library as an
# invisible file, which is a worse fixture than none.
UNOPENABLE_FOLDER = "Unopenable Formats"
UNOPENABLE_FILES = [
    ("A Kindle Format Book (2011)", "azw3", "book-azw3"),
    ("A Mobipocket Book (2005)", "mobi", "book-mobi"),
]

# --- comics ---------------------------------------------------------------
#
# Jellyfin reads comic metadata three ways, and which one wins is decided by
# registration order in `ApplicationHost`, not by merit: ComicBookInfo, then
# the external `ComicInfo.xml`, then the internal one, **first with any
# metadata wins outright**. So each archive below carries exactly one dialect;
# an archive with two would only ever prove which one is first.
#
# Two of the three are hard-restricted to `.cbz` by extension, which is what
# `Ignored Internal Info 005.cbt` is for — the same bytes, in an archive
# Jellyfin can otherwise read perfectly well, ignored on the strength of the
# extension alone.
#
# Every one of these sets `Title`, and every Title says which dialect it came
# from. A comic whose name on screen is its filename is a comic whose metadata
# was not read, and you can see that without opening anything.
COMICS_FOLDER = "Comics"

# Pages are 1200x1800 JPEGs. Four is enough: the page count is what is
# checked, and it is checked against this number.
COMIC_PAGES = 4

# The two cover rules, which are the whole of `ComicImageProvider`:
#   1. an entry named exactly `cover.<ext>` at the archive root, tried in the
#      order .png .jpeg .jpg .webp .bmp .gif — exact, case-sensitive, no path;
#   2. failing that, the alphabetically first entry by full key.
#
# Rule 2 is right by luck whenever page one happens to sort first, which is
# why `A Test Comic 001.cbz` proves nothing about it. `Scan Credits Cover`
# is the realistic way it goes wrong: a scanlator credit page filed as `000 -`
# sorts ahead of page one and becomes the cover. `Named Cover` is the same
# archive with `cover.jpg` added, and nothing else changed.
SCAN_CREDITS_PAGE = "000 - Scan Credits.jpg"

# The series every dialect claims, so a comic showing it got its metadata from
# somewhere and a comic showing its filename did not.
COMIC_SERIES = "The Signal Archive"


def _comic_pages(folder: str, key: str, cfg, count: int = COMIC_PAGES,
                 credits_page: bool = False) -> list[tuple[str, str]]:
    """Draw a comic's pages and return (name-in-archive, path-on-disk).

    The images are hidden temporaries beside the archive, keyed by the comic
    so two of them being built at once cannot collide, and removed once the
    archive is written — a loose page in a library folder would be an item.
    """
    pages = []
    if credits_page:
        path = os.path.join(folder, f".{key}-credits.jpg")
        if artwork.draw("photo", f"{key}-credits", "Scan Credits", path, cfg,
                        size=(1200, 1800), stamp=False):
            pages.append((SCAN_CREDITS_PAGE, path))
    for n in range(1, count + 1):
        path = os.path.join(folder, f".{key}-page{n}.jpg")
        if artwork.draw("photo", f"{key}-{n}", f"Page {n}", path, cfg,
                        size=(1200, 1800), stamp=False):
            pages.append((f"{n:03d}.jpg", path))
    return pages


def build_books(root: str, cfg) -> list[dict]:
    """EPUB, PDF, comic archives and audiobooks.

    None of it is media except the audiobooks, so almost all of it is written
    directly by `books.py` rather than by ffmpeg.
    """
    from . import books

    made: list[dict] = []
    if cfg.dry_run:
        return made

    # --- the three author folders, each a directory-book -------------------
    #
    # One epub alone in a folder is the directory rule, so the *folder* name is
    # what the resolver parses. These come back named after the book anyway,
    # because `EpubProvider` reads `content.opf` and `dc:title` overrides the
    # resolver — which is worth knowing, and is the reason a Books library can
    # look correct while the path convention behind it is doing something else.
    for i, (title, author) in enumerate(
            [("The Standard Reference", "Ada Alvarez"),
             ("A Second Volume", "Bo Brandt"),
             ("日本語の本", "Cai Chen")], 1):
        epub = os.path.join(root, author, f"{title}.epub")
        if not cfg.artwork_only or not os.path.exists(epub):
            books.write_epub(epub, title, author)
        made.append({"library": "Books", "key": f"book-{i}", "path": epub})

    # --- the shelf: several books in one folder, so filenames are parsed ---
    shelf = os.path.join(root, BOOK_SHELF)
    for stem, ext, key, title, pages, _why in BOOK_SHELF_FILES:
        path = os.path.join(shelf, f"{stem}.{ext}")
        if cfg.artwork_only and os.path.exists(path):
            pass
        elif ext == "pdf":
            books.write_pdf(path, title or stem, BOOK_SHELF, pages)
        else:
            books.write_epub(path, title, BOOK_SHELF)
        entry = {"library": "Books", "key": key, "path": path}
        if ext == "pdf":
            # Recorded so `verify` can re-count them rather than trust that
            # the writer was asked for the right number.
            entry["pages"] = pages
        made.append(entry)

    # --- one book alone in its own directory ------------------------------
    alone = os.path.join(root, BOOK_ALONE_AUTHOR, BOOK_ALONE,
                         f"{BOOK_ALONE}.epub")
    if not cfg.artwork_only or not os.path.exists(alone):
        # The name the *folder* parses to, for the same reason as the shelf:
        # a `dc:title` that disagreed would be the thing on screen, and the
        # directory rule would be invisible behind it.
        books.write_epub(alone, BOOK_ALONE_PARSED_NAME, BOOK_ALONE_AUTHOR)
    made.append({"library": "Books", "key": "book-alone", "path": alone})

    # --- the formats that resolve and cannot be opened --------------------
    for stem, ext, key in UNOPENABLE_FILES:
        path = os.path.join(root, UNOPENABLE_FOLDER, f"{stem}.{ext}")
        if not cfg.artwork_only or not os.path.exists(path):
            books.write_palmdb(path, stem, UNOPENABLE_FOLDER)
        made.append({"library": "Books", "key": key, "path": path})

    made += _build_comics(os.path.join(root, COMICS_FOLDER), cfg)
    made += _build_audiobooks(root, cfg)
    return made


# One row per archive. `dialect` is the single metadata convention it carries,
# and "single" is the point: `ComicProvider` walks its providers in a fixed
# order — ComicBookInfo, then the external `ComicInfo.xml`, then the internal
# one — and returns the **first** that finds anything. An archive carrying two
# would only ever demonstrate which one is first, so each here carries one and
# `test_books.py` holds it to that.
#
#   dialect  "none"      no metadata anywhere; the name comes from the filename
#            "external"  ComicInfo.xml beside the archive, as `<name>.xml`
#            "internal"  ComicInfo.xml inside the archive — .cbz only
#            "bookinfo"  ComicBookInfo JSON in the zip's archive comment — .cbz only
#            "ignored"   internal metadata in a container that is not .cbz, so
#                        it is never read
COMICS = [
    {"key": "cbz-1", "file": "A Test Comic 001.cbz", "dialect": "none",
     "credits_page": False, "named_cover": False,
     "why": "the plain case: no metadata, and pages that happen to sort so "
            "that the cover rule is right by luck"},
    {"key": "cbz-external-info", "file": "The Signal Archive 002.cbz",
     "dialect": "external", "credits_page": False, "named_cover": False,
     "title": "Sidecar ComicInfo Dialect", "number": 2, "year": 2017,
     "publisher": "Standard QA Pictures", "genres": ["Adventure", "Mystery"],
     "writer": "Ada Alvarez", "penciller": "Bo Brandt", "colourist": "Cai Chen",
     "summary": "Read from a ComicInfo.xml file next to the archive. This is "
                "the only one of the three providers with no extension "
                "restriction.",
     "why": "ComicInfo.xml beside the archive"},
    {"key": "cbz-internal-info", "file": "The Signal Archive 003.cbz",
     "dialect": "internal", "credits_page": False, "named_cover": False,
     "title": "Internal ComicInfo Dialect", "number": 3, "year": 2018,
     "publisher": "Testcard Studios", "genres": ["Sci-Fi"],
     "writer": "Dara Dahl",
     "summary": "Read from a ComicInfo.xml inside the archive. Only a .cbz is "
                "opened for this. Its page count is one more than its pages, "
                "because the server counts every entry in the archive.",
     "why": "ComicInfo.xml inside the archive, which is .cbz only"},
    {"key": "cbz-book-info", "file": "The Signal Archive 004.cbz",
     "dialect": "bookinfo", "credits_page": False, "named_cover": False,
     "title": "ComicBookInfo Dialect", "number": 4, "year": 2019, "month": 6,
     "publisher": "Reference Media Group", "genre": "Fantasy",
     "credits": [("Eli Eriksen", "Writer"), ("Fay Farrow", "Penciller")],
     "summary": "Read from JSON in the zip's archive comment — a trailer "
                "nothing else looks at, which is why the convention could be "
                "bolted on to an existing format at all. .cbz only.",
     "why": "ComicBookInfo JSON in the zip comment"},
    {"key": "cbt-ignored-info", "file": "Ignored Internal Info 005.cbt",
     "dialect": "ignored", "credits_page": False, "named_cover": False,
     "title": "THIS TITLE MUST NOT APPEAR", "number": 99, "year": 1999,
     "publisher": "Wrong Publisher", "genres": ["Horror"],
     "writer": "Nobody At All",
     "summary": "If you can read this in Jellyfin, the .cbz-only restriction "
                "on InternalComicInfoProvider is gone.",
     "why": "the same internal metadata in a .cbt, which is read as an "
            "archive and ignored as metadata"},
    {"key": "cbz-sorted-cover", "file": "Scan Credits Cover 006.cbz",
     "dialect": "none", "credits_page": True, "named_cover": False,
     "why": "no cover.jpg, and a scan-credits page that sorts ahead of page "
            "one — so the cover rule picks the wrong page"},
    {"key": "cbz-named-cover", "file": "Named Cover 007.cbz",
     "dialect": "none", "credits_page": True, "named_cover": True,
     "why": "the same archive with cover.jpg added and nothing else changed, "
            "so the difference between the two covers is the rule"},
]

# Which dialects put a member inside the archive, and which put one beside it.
# `ignored` is `internal` in a container the provider refuses to open, so it
# writes the same member and expects it to do nothing.
DIALECTS_INSIDE = ("internal", "ignored")
DIALECTS_BESIDE = ("external",)
DIALECTS_IN_COMMENT = ("bookinfo",)


def comic_entries(comic: dict) -> list[str]:
    """Every entry the archive will hold, in the order they are written.

    Spelled out here rather than measured off the file, so the expectation
    exists before the archive does — `verify` compares the built archive
    against it, and the cover rule is applied to it in `test_books.py`.
    """
    names = []
    if comic["credits_page"]:
        names.append(SCAN_CREDITS_PAGE)
    names += [f"{n:03d}.jpg" for n in range(1, COMIC_PAGES + 1)]
    if comic["named_cover"]:
        names.append("cover.jpg")
    if comic["dialect"] in DIALECTS_INSIDE:
        names.append("ComicInfo.xml")
    return names


def _build_comics(folder: str, cfg) -> list[dict]:
    """One archive per metadata dialect, plus the two cover rules."""
    from . import books

    os.makedirs(folder, exist_ok=True)
    made = []
    for comic in COMICS:
        path = os.path.join(folder, comic["file"])
        entries = comic_entries(comic)
        made.append({"library": "Books", "key": comic["key"], "path": path,
                     "archive": {"entries": len(entries),
                                 "cover": books.archive_cover(entries)}})
        if cfg.artwork_only and os.path.exists(path):
            continue

        pages = _comic_pages(folder, comic["key"], cfg,
                             credits_page=comic["credits_page"])
        if not pages:
            continue
        if comic["named_cover"]:
            cover = os.path.join(folder, f".{comic['key']}-cover.jpg")
            if artwork.draw("photo", f"{comic['key']}-cover", "Cover", cover,
                            cfg, size=(1200, 1800), stamp=False):
                pages.append(("cover.jpg", cover))

        extra = None
        comment = None
        if comic["dialect"] in DIALECTS_INSIDE:
            extra = {"ComicInfo.xml": _comicinfo_for(comic)}
        elif comic["dialect"] in DIALECTS_BESIDE:
            sidecar = os.path.splitext(path)[0] + ".xml"
            with open(sidecar, "w", encoding="utf-8") as fh:
                fh.write(_comicinfo_for(comic))
        elif comic["dialect"] in DIALECTS_IN_COMMENT:
            comment = books.comicbookinfo_json(
                title=comic["title"], series=COMIC_SERIES,
                issue=comic["number"], year=comic["year"],
                month=comic["month"], publisher=comic["publisher"],
                genre=comic["genre"], comments=comic["summary"],
                credits=comic["credits"], tags=["stdjflib", "comic"])

        if path.endswith(".cbt"):
            books.write_cbt(path, pages, extra=extra)
        else:
            books.write_cbz(path, pages, extra=extra, comment=comment)
        for _name, page in pages:
            # A loose page left in a library folder would be an item.
            if os.path.exists(page):
                os.unlink(page)
    return made


def _comicinfo_for(comic: dict) -> str:
    from . import books

    return books.comicinfo_xml(
        title=comic["title"],
        series="Wrong Series" if comic["dialect"] == "ignored" else COMIC_SERIES,
        number=comic["number"], year=comic["year"], summary=comic["summary"],
        publisher=comic["publisher"], genres=comic["genres"],
        writer=comic["writer"], penciller=comic.get("penciller", ""),
        colourist=comic.get("colourist", ""))


def _build_audiobooks(root: str, cfg) -> list[dict]:
    """The two shapes, each alone in a folder of its own.

    A folder rather than a loose file for both, because that is what the
    server's own directory branch is for and because it is how audiobooks
    arrive. The single `.m4b` takes its name from the folder; the rip's six
    parts take theirs from their filenames.
    """
    by_key = {r.key: r for r in recipes.all_recipes()}
    made = []

    single = by_key["book-m4b"]
    folder = os.path.join(root, recipes.AUDIOBOOK_AUTHOR, single.title)
    path = os.path.join(folder, f"{single.title}.{single.container}")
    if _emit(single, path, cfg):
        made.append({"library": "Books", "key": single.key, "path": path})

    rip = os.path.join(root, recipes.RIP_AUTHOR, recipes.RIP_TITLE)
    for part in range(1, recipes.RIP_PARTS + 1):
        rec = by_key[f"book-rip-{part:02d}"]
        path = os.path.join(rip, f"{rec.title}.{rec.container}")
        if _emit(rec, path, cfg):
            made.append({"library": "Books", "key": rec.key, "path": path})
    return made


# --------------------------------------------------------------------------
# Bulk libraries — scale rather than coverage
# --------------------------------------------------------------------------

# What is distinct per item and what is shared is the whole design of this
# section, and the split is deliberate:
#
#   Media is shared. A bulk item is almost never played; it exists to be
#   listed, scrolled past, searched and sorted. Encoding a thousand separate
#   three-second clips would cost minutes to produce a thousand files nobody
#   opens, so a small pool is encoded once and every item hard-links to one of
#   them. Distinct paths, distinct metadata, shared bytes.
#
#   Artwork is distinct. That is the point: a thousand identical posters would
#   never evict anything from a thumbnail cache, and the cache is one of the
#   main things a library this size is meant to stress.
#
#   Some items have no artwork at all, so the placeholder path gets exercised
#   too — a client that only ever draws real posters looks fine until a real
#   library hands it an item without one.

# One adjective per letter, so titles spread evenly across an A-Z index rather
# than clumping — which is what makes jump-to-letter and sort actually testable.
BULK_ADJECTIVES = [
    "Amber", "Bright", "Crimson", "Distant", "Eastern", "Frozen", "Golden",
    "Hidden", "Iron", "Jagged", "Keen", "Lonely", "Midnight", "Northern",
    "Open", "Pale", "Quiet", "Restless", "Silver", "Twin", "Umbral", "Vivid",
    "Wandering", "Xeric", "Yellow", "Zephyr",
]

BULK_NOUNS = [
    "Anchor", "Beacon", "Circuit", "Delta", "Ember", "Fathom", "Garden",
    "Harbor", "Island", "Junction", "Kite", "Lantern", "Meridian", "Nexus",
    "Orbit", "Passage", "Quarry", "River", "Signal", "Threshold", "Undertow",
    "Valley", "Window", "Xylem", "Yard", "Zenith", "Archive", "Bridge",
    "Canyon", "Dune", "Echo", "Forge", "Gate", "Horizon", "Inlet", "Jetty",
    "Keystone", "Ledge", "Mirror", "Node",
]

# Titles that are not plain ASCII, mixed in at a steady interval so sorting,
# search and text shaping meet them somewhere in the middle of a long list
# rather than only at the edges.
BULK_AWKWARD = [
    "Ünïcödé Völume", "日本語のタイトル", "Заглавие на кириллица",
    "Τίτλος στα ελληνικά", "عنوان عربي", "כותרת בעברית",
    "A Title That Simply Keeps Going And Going Well Past Any Column Width",
    "!Exclamation First", "1970 Numeric Lead", "Ålesund Nordic",
]

_ROMAN = ["", " II", " III", " IV", " V", " VI", " VII", " VIII"]

# How many distinct clips back a bulk library.
BULK_POOL = 12

# The shapes a bulk photo comes in. Four of them, unevenly weighted, because
# the question a photo library asks a client is what it does with a row whose
# median aspect ratio does not describe most of the pictures in it. Landscape
# first, so the median lands there and the portraits are the ones squeezed.
BULK_PHOTO_SHAPES = [(1800, 1200), (1600, 1200), (1200, 1800), (1500, 1500),
                     (1920, 1080), (1800, 1200)]


def bulk_name(index: int) -> str:
    """A unique, stable title for bulk item `index`.

    Every tenth-ish item is deliberately awkward — non-Latin script, leading
    punctuation, a leading digit, or far too long.
    """
    if index % 37 == 36:
        awkward = BULK_AWKWARD[(index // 37) % len(BULK_AWKWARD)]
        return f"{awkward} {index:05d}"
    pair = index % (len(BULK_ADJECTIVES) * len(BULK_NOUNS))
    adjective = BULK_ADJECTIVES[pair % len(BULK_ADJECTIVES)]
    noun = BULK_NOUNS[(pair // len(BULK_ADJECTIVES)) % len(BULK_NOUNS)]
    cycle = index // (len(BULK_ADJECTIVES) * len(BULK_NOUNS))
    return f"{adjective} {noun}{_ROMAN[cycle % len(_ROMAN)]}"


def bulk_year(index: int) -> int:
    """Spread across a range wide enough for decade filters to bite."""
    return 1950 + (index * 7) % 76


def safe_name(text: str) -> str:
    """Strip what a filesystem will not take, keeping what Jellyfin parses."""
    for bad, good in (("/", "-"), (":", " -"), ("\\", "-"), ("?", ""),
                      ("*", ""), ('"', "'"), ("<", "("), (">", ")"), ("|", "-")):
        text = text.replace(bad, good)
    return text.strip().rstrip(".")


def _link_or_copy(src: str, dest: str) -> bool:
    """Hard-link `src` to `dest`, copying only if the link is refused.

    Not every filesystem obliges. sshfs and SMB both accept the call and give
    you a copy anyway — `ln` returns success and the link count stays at one —
    so a bulk build costs real bytes per item there rather than sharing them.
    That is why the pool clips are as small as they are: on a filesystem that
    does link, their size barely matters, and on one that does not, it is the
    only thing that does.
    """
    if os.path.exists(dest):
        return True
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    try:
        os.link(src, dest)
        return True
    except OSError:
        try:
            import shutil

            shutil.copy2(src, dest)
            return True
        except OSError:
            return False


def _bulk_pool(cfg, kind: str, container: str, count: int = BULK_POOL) -> list[str]:
    """Encode (once) the clips bulk items hard-link to.

    Lives under the cache rather than in a library folder, so Jellyfin never
    scans the originals and `clean` treats them like the downloads they
    resemble. Same filesystem as the library, which is what makes the
    hard-links work.
    """
    out: list[str] = []
    tasks = []
    for i in range(count):
        path = cfg.cache("bulk", f"{kind}-{i:02d}.{container}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            out.append(path)
            continue

        def make(i=i, path=path):
            # Deliberately tiny. These are never watched, and on a filesystem
            # that cannot hard-link (sshfs and SMB both silently fall back to
            # copying) every byte here is multiplied by the item count.
            if kind == "audio":
                album = {"codec": "libmp3lame", "ext": container, "tracks": 1,
                         "album": "Bulk", "artist": "Bulk", "year": 2000,
                         "seconds": 5, "bitrate": "48k"}
                ok = _audio_track(path, cfg, album, 1, i + 1, f"Take {i + 1}",
                                  "Bulk", None)
            else:
                rec = _short(f"Bulk clip {i + 1}", f"bulk-pool-{i}", duration=3,
                             video=Video(width=256, height=144, bitrate="60k"))
                rec = _clip(rec, container=container)
                ok = _emit(rec, path, cfg)
            return path if ok else None

        tasks.append(make)

    if tasks and not cfg.dry_run:
        out += [p for p in _run_all(tasks, cfg) if p]
    return out or ([cfg.cache("bulk", f"{kind}-00.{container}")] if cfg.dry_run
                   else [])


def _bulk_common(index: int, key: str) -> dict:
    """The metadata every bulk item shares the shape of."""
    return {
        "key": key,
        "title": bulk_name(index),
        "year": bulk_year(index),
        "plot": (f"Bulk item {index}. Generated for scale testing — paging, "
                 f"virtualised scrolling, thumbnail cache pressure, search and "
                 f"sort. The media is shared between bulk items; the metadata "
                 f"and artwork are not."),
        "rating": 1.0 + (index * 13 % 90) / 10.0,
    }


def build_bulk_movies(root: str, cfg) -> list[dict]:
    pool = _bulk_pool(cfg, "video", "mkv")
    if not pool:
        return []

    def task(i):
        def run():
            meta = _bulk_common(i, f"bulk-movie-{i:05d}")
            safe = safe_name(meta["title"])
            folder = os.path.join(root, f"{safe} ({meta['year']})")
            media = os.path.join(folder, f"{safe} ({meta['year']}).mkv")
            if cfg.dry_run:
                return {"library": "Bulk Movies", "key": meta["key"],
                        "path": media}
            if not _link_or_copy(pool[i % len(pool)], media):
                return None
            nfo.movie(os.path.join(folder, "movie.nfo"),
                      key=meta["key"], title=meta["title"], plot=meta["plot"],
                      year=meta["year"], runtime_minutes=1,
                      rating=meta["rating"], tags=["stdjflib", "bulk"])
            # One in eight ships without a poster, on purpose.
            if i % 8:
                artwork.folder_images(folder, meta["key"], meta["title"], cfg,
                                      kinds=("poster",), seq=i,
                                      subtitle=str(meta["year"]))
            return {"library": "Bulk Movies", "key": meta["key"], "path": media}
        return run

    return [x for x in _run_all([task(i) for i in range(cfg.bulk)], cfg) if x]


def build_bulk_shows(root: str, cfg) -> list[dict]:
    """Episodes total `cfg.bulk`, spread over shows of deliberately uneven size.

    Uneven because a client that pages a season list correctly at twenty
    episodes can still break at two hundred, and because a one-episode show is
    its own edge case.
    """
    pool = _bulk_pool(cfg, "video", "mkv")
    if not pool:
        return []

    sizes = [1, 6, 13, 24, 60, 120]
    shows, made_episodes, i = [], 0, 0
    while made_episodes < cfg.bulk:
        count = min(sizes[i % len(sizes)], cfg.bulk - made_episodes)
        shows.append((i, count))
        made_episodes += count
        i += 1

    def show_task(index, episodes):
        def run():
            out = []
            meta = _bulk_common(index, f"bulk-show-{index:05d}")
            safe = safe_name(meta["title"])
            folder = os.path.join(root, f"{safe} ({meta['year']})")
            if not cfg.dry_run:
                nfo.tvshow(os.path.join(folder, "tvshow.nfo"), key=meta["key"],
                           title=meta["title"], plot=meta["plot"],
                           year=meta["year"], rating=meta["rating"],
                           tags=["stdjflib", "bulk"])
                # Landscape as well as poster and backdrop, because at this
                # scale a series row is where a client actually chooses
                # between the two shapes — and one show in five ships without
                # one, so the fallback (poster cropped to 16:9, or a
                # placeholder) is common rather than theoretical.
                kinds = ("poster", "backdrop")
                if index % 5:
                    kinds += ("thumb",)
                artwork.folder_images(folder, meta["key"], meta["title"], cfg,
                                      kinds=kinds, seq=index)
            # Long shows split across seasons of 24, so season lists get big too.
            for n in range(1, episodes + 1):
                season_no = (n - 1) // 24 + 1
                ep_no = (n - 1) % 24 + 1
                sdir = os.path.join(folder, f"Season {season_no:02d}")
                name = f"{safe} - S{season_no:02d}E{ep_no:02d} - Episode {ep_no}"
                media = os.path.join(sdir, name + ".mkv")
                ep_key = f"{meta['key']}-s{season_no}e{ep_no}"
                if cfg.dry_run:
                    out.append({"library": "Bulk Shows", "key": ep_key,
                                "path": media})
                    continue
                if not _link_or_copy(pool[(index + n) % len(pool)], media):
                    continue
                nfo.episode(os.path.join(sdir, name + ".nfo"), key=ep_key,
                            title=f"Episode {ep_no}", plot=meta["plot"],
                            season_no=season_no, episode_no=ep_no,
                            aired=f"{meta['year']}-01-01", runtime_minutes=1,
                            show_title=meta["title"])
                # A quarter get a still, so the missing-thumb path is common.
                if n % 4 == 0:
                    artwork.sidecar_images(
                        media, ep_key, f"Episode {ep_no}", cfg,
                        kinds=("thumb",), seq=index + n,
                        subtitle=f"S{season_no:02d}E{ep_no:02d}")
                out.append({"library": "Bulk Shows", "key": ep_key,
                            "path": media})
            return out
        return run

    groups = _run_all([show_task(idx, n) for idx, n in shows], cfg)
    return [item for group in groups for item in (group or [])]


def build_bulk_music(root: str, cfg) -> list[dict]:
    """Tracks total `cfg.bulk`, ten to an album, across many artists."""
    pool = _bulk_pool(cfg, "audio", "mp3")
    if not pool:
        return []

    per_album = 10
    albums = max(1, cfg.bulk // per_album)

    def album_task(index):
        def run():
            out = []
            meta = _bulk_common(index, f"bulk-album-{index:05d}")
            artist = f"{BULK_ADJECTIVES[index % len(BULK_ADJECTIVES)]} Ensemble"
            safe = safe_name(meta["title"])
            album_dir = os.path.join(root, safe_name(artist),
                                     f"{safe} ({meta['year']})")
            if not cfg.dry_run and index % 6:
                artwork.folder_images(album_dir, meta["key"], meta["title"],
                                      cfg, kinds=("square",), seq=index,
                                      subtitle=artist)
            count = min(per_album, cfg.bulk - index * per_album)
            for n in range(1, count + 1):
                path = os.path.join(album_dir, f"{n:02d} - Track {n}.mp3")
                key = f"{meta['key']}-t{n}"
                if cfg.dry_run:
                    out.append({"library": "Bulk Music", "key": key,
                                "path": path})
                    continue
                if _link_or_copy(pool[(index + n) % len(pool)], path):
                    out.append({"library": "Bulk Music", "key": key,
                                "path": path})
            return out
        return run

    groups = _run_all([album_task(i) for i in range(albums)], cfg)
    return [item for group in groups for item in (group or [])]


def build_bulk_music_videos(root: str, cfg) -> list[dict]:
    pool = _bulk_pool(cfg, "video", "mp4")
    if not pool:
        return []
    count = max(1, cfg.bulk // 4)

    def task(i):
        def run():
            meta = _bulk_common(i, f"bulk-mv-{i:05d}")
            artist = f"{BULK_ADJECTIVES[i % len(BULK_ADJECTIVES)]} Ensemble"
            safe = safe_name(meta["title"])
            path = os.path.join(root, safe_name(artist),
                                f"{safe_name(artist)} - {safe}.mp4")
            if cfg.dry_run:
                return {"library": "Bulk Music Videos", "key": meta["key"],
                        "path": path}
            if not _link_or_copy(pool[i % len(pool)], path):
                return None
            nfo.musicvideo(os.path.splitext(path)[0] + ".nfo", key=meta["key"],
                           title=meta["title"], artist=artist, album="Bulk",
                           year=meta["year"], plot=meta["plot"],
                           runtime_minutes=1)
            return {"library": "Bulk Music Videos", "key": meta["key"],
                    "path": path}
        return run

    return [x for x in _run_all([task(i) for i in range(count)], cfg) if x]


def build_bulk_photos(root: str, cfg) -> list[dict]:
    """Every photo is drawn individually — here the image *is* the item."""
    def task(i):
        def run():
            meta = _bulk_common(i, f"bulk-photo-{i:05d}")
            album = f"Album {i // 100:02d}"
            path = os.path.join(root, album,
                                f"{i:05d} - {safe_name(meta['title'])}.jpg")
            if cfg.dry_run:
                return {"library": "Bulk Photos", "key": meta["key"],
                        "path": path}
            # A spread of shapes rather than a thousand identical 16:9
            # frames: a client picks a row's layout from the median aspect
            # ratio of what is in it, and a library that is all one shape
            # never exercises that.
            if not artwork.draw("photo", meta["key"], meta["title"], path, cfg,
                                subtitle=f"photo {i:05d}", seq=i,
                                # A real photograph is the item; a label
                                # written across it would only say the
                                # filename again.
                                text=not cfg.use_artwork,
                                size=BULK_PHOTO_SHAPES[i % len(BULK_PHOTO_SHAPES)]):
                return None
            return {"library": "Bulk Photos", "key": meta["key"], "path": path}
        return run

    return [x for x in _run_all([task(i) for i in range(cfg.bulk)], cfg) if x]


def build_bulk_books(root: str, cfg) -> list[dict]:
    """EPUBs are pure zip writes, so these cost almost nothing to produce."""
    from . import books
    count = max(1, cfg.bulk // 4)

    def task(i):
        def run():
            meta = _bulk_common(i, f"bulk-book-{i:05d}")
            author = f"{BULK_ADJECTIVES[i % len(BULK_ADJECTIVES)]} Author"
            path = os.path.join(root, safe_name(author),
                                f"{safe_name(meta['title'])}.epub")
            if cfg.dry_run:
                return {"library": "Bulk Books", "key": meta["key"],
                        "path": path}
            # An EPUB holds no artwork, so a redraw has nothing to change
            # in one — and rewriting it would churn the zip's timestamps and
            # send Jellyfin off to rescan a file identical in every way that
            # matters.
            if not (cfg.artwork_only and os.path.exists(path)):
                books.write_epub(path, meta["title"], author)
            return {"library": "Bulk Books", "key": meta["key"], "path": path}
        return run

    return [x for x in _run_all([task(i) for i in range(count)], cfg) if x]


BULK_BUILDERS = [
    ("Bulk Movies", build_bulk_movies),
    ("Bulk Shows", build_bulk_shows),
    ("Bulk Music", build_bulk_music),
    ("Bulk Music Videos", build_bulk_music_videos),
    ("Bulk Photos", build_bulk_photos),
    ("Bulk Books", build_bulk_books),
]
