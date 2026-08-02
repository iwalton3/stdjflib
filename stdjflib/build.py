"""Orchestration: downloads, then every library, then the manifest.

Downloads run first and sequentially — they are network-bound and the servers
are somebody else's. Generation runs in a thread pool, because ffmpeg is the
bottleneck and there are usually more cores than there is patience.
"""

from __future__ import annotations

import json
import os
import shutil
import time

from . import artwork, catalog, config, fetch, ff, libraries, nfo


def _say(msg: str = "") -> None:
    print(msg, flush=True)


# Anything that went wrong but did not stop the build. Collected so the summary
# can repeat it: a warning printed forty screens above "Done: 4736 items" is a
# warning nobody sees, and a build that quietly dropped a file should not look
# identical to one that did not.
WARNINGS: list[str] = []


def _warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  ! {msg}", flush=True)


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------

def fetch_sources(cfg) -> dict:
    """Download everything the tier needs. Returns {key: cached path}."""
    sources = catalog.for_tier(cfg.tier)
    if not sources:
        return {}

    if cfg.artwork_only:
        # Redrawing artwork is not a reason to go to the network. Whatever is
        # already in the cache is what the films were placed from.
        return {src.key: cfg.cache(src.cache_name) for src in sources
                if os.path.exists(cfg.cache(src.cache_name))}

    total = catalog.estimated_bytes(cfg.tier)
    _say(f"  {len(sources)} source files, about {fetch.human(total)}")
    got: dict[str, str] = {}

    for i, src in enumerate(sources, 1):
        dest = cfg.cache(src.cache_name)
        label = f"[{i}/{len(sources)}] {src.title}"

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            got[src.key] = dest
            continue
        if cfg.dry_run:
            _say(f"  {label} -> would download {fetch.human(src.size)}")
            got[src.key] = dest
            continue

        # Refuse anything whose licence is not what the catalog claims. For
        # archive.org this asks the item itself, right now.
        try:
            fetch.check_licence(src)
        except fetch.LicenceRefused as exc:
            _warn(str(exc))
            continue

        download_target = cfg.cache(src.url.rsplit("/", 1)[-1]) if src.unzip else dest
        last = [0.0]

        def progress(done, size, _label=label):
            now = time.time()
            if now - last[0] < 0.5 and done != size:
                return
            last[0] = now
            pct = f"{done * 100 // size:3d}%" if size else "  ? "
            print(f"\r  {_label}: {pct} of {fetch.human(size)}   ",
                  end="", flush=True)

        try:
            fetch.download(src.url, download_target, progress=progress)
            print()
            if src.unzip:
                _say(f"      unpacking {os.path.basename(download_target)}")
                fetch.unzip_one(download_target, dest, src.member)
                if not cfg.keep_cache:
                    os.unlink(download_target)
            got[src.key] = dest
        except Exception as exc:  # noqa: BLE001 - one bad mirror is not fatal
            print()
            _warn(f"{src.title}: {exc}")

    return got


def place_movies(root: str, cfg, cached: dict) -> list[dict]:
    """Copy downloaded films into the Movies library with NFO and artwork."""
    made = []
    subs_by_parent: dict[str, list] = {}
    for src in catalog.for_tier(cfg.tier):
        if src.kind == "subtitle" and src.parent:
            subs_by_parent.setdefault(src.parent, []).append(src)

    for src in catalog.movies_for_tier(cfg.tier):
        path = cached.get(src.key)
        if not path:
            continue
        folder = os.path.join(root, f"{src.title} ({src.year})")
        ext = os.path.splitext(path)[1]
        base = f"{src.title} ({src.year})"
        # Colons and slashes are legal in a title and fatal in a path.
        safe = base.replace("/", "-").replace(":", " -")
        dest = os.path.join(folder, safe + ext)

        if cfg.dry_run:
            made.append({"library": "Movies", "key": src.key, "path": dest})
            continue

        os.makedirs(folder, exist_ok=True)
        if not os.path.exists(dest) and not cfg.artwork_only:
            # Hard-link when we can: the cache and the library are usually on
            # the same filesystem, and the films are large.
            try:
                os.link(path, dest)
            except OSError:
                shutil.copy2(path, dest)

        for sub in subs_by_parent.get(src.key, []):
            sub_cache = cached.get(sub.key)
            if not sub_cache or not os.path.exists(sub_cache) or cfg.artwork_only:
                continue
            # Disambiguate the several French and Chinese variants, which would
            # otherwise overwrite each other.
            suffix = ""
            if "alt 2" in sub.title:
                suffix = ".alt2"
            elif "alt" in sub.title:
                suffix = ".alt"
            elif "繁體" in sub.title:
                suffix = ".hant"
            elif "Brasil" in sub.title:
                suffix = ".br"
            sub_dest = os.path.join(folder, f"{safe}.{sub.lang}{suffix}.srt")
            if not os.path.exists(sub_dest):
                try:
                    os.link(sub_cache, sub_dest)
                except OSError:
                    shutil.copy2(sub_cache, sub_dest)

        nfo.movie(os.path.join(folder, "movie.nfo"), key=src.key,
                  title=src.title, plot=src.plot, year=src.year,
                  runtime_minutes=src.runtime or 10, rating=8.0,
                  tags=["stdjflib", "downloaded",
                        "cc-by" if src.licence.startswith("CC") else "public-domain"])
        artwork.folder_images(folder, src.key, src.title, cfg,
                              kinds=artwork.SETS["movie"],
                              subtitle=f"{src.year} · {src.licence}")
        made.append({"library": "Movies", "key": src.key, "path": dest})
    return made


# --------------------------------------------------------------------------
# Whole build
# --------------------------------------------------------------------------

BUILDERS = [
    ("Test Media", libraries.build_test_media),
    ("Shows", libraries.build_shows),
    ("Music", libraries.build_music),
    ("Music Videos", libraries.build_music_videos),
    ("Photos", libraries.build_photos),
    ("Home Videos", libraries.build_home_videos),
    ("Books", libraries.build_books),
]


def run(cfg) -> dict:
    started = time.time()
    WARNINGS.clear()
    artwork.reset_drawn()
    if cfg.artwork_only:
        _say(f"stdjflib — redrawing the artwork of the {cfg.tier} tier "
             f"in {cfg.root}")
        _say("  media is left alone; nothing is downloaded")
    else:
        _say(f"stdjflib — building the {cfg.tier} tier into {cfg.root}")
    _say(f"  ffmpeg: {ff.version(cfg.ffmpeg)}")
    if cfg.hwaccel:
        _say(f"  hardware encoding: {cfg.hwaccel} (large files only; "
             f"output is not byte-reproducible)")
    if not cfg.font_file:
        _say("  ! no font found — clips will not be labelled and bitmap "
             "subtitles will be skipped")
    _say()

    os.makedirs(cfg.root, exist_ok=True)
    os.makedirs(cfg.cache(), exist_ok=True)

    items: list[dict] = []

    if cfg.wants("Movies"):
        _say("Downloads")
        cached = fetch_sources(cfg)
        _say()
        _say("Movies")
        items += place_movies(cfg.path("Movies"), cfg, cached)
        items += libraries.build_movies(cfg.path("Movies"), cfg)
        _say(f"  {sum(1 for i in items if i['library'] == 'Movies')} items")
    else:
        cached = {}

    for name, builder in BUILDERS:
        if not cfg.wants(name):
            continue
        _say(name)
        made = builder(cfg.path(name), cfg)
        items += made
        _say(f"  {len(made)} items")

    if cfg.bulk:
        _say()
        _say(f"Bulk libraries — {cfg.bulk} items each, media shared by hard link")
        for name, builder in libraries.BULK_BUILDERS:
            if not cfg.wants(name):
                continue
            made = builder(cfg.path(name), cfg)
            items += made
            _say(f"  {name}: {len(made)} items")

    libs = cfg.libraries()
    if cfg.only:
        # A partial build must not throw away what it did not rebuild. Without
        # this, `--only Movies` rewrites the manifest with 28 entries and
        # `verify` forgets the other four thousand files still sitting on disk.
        # Entries for the libraries this run *did* build are dropped first, so
        # items that no longer exist do not survive as ghosts.
        items = _carry_forward(cfg, set(libs)) + items
        libs = {**_previous(cfg).get("libraries", {}), **libs}

    manifest = {
        "build_version": config.BUILD_VERSION,
        "tier": cfg.tier,
        "seed": cfg.seed,
        "ffmpeg": ff.version(cfg.ffmpeg),
        "hwaccel": cfg.hwaccel,
        "bulk": cfg.bulk,
        # This run's warnings only — a stale warning is worse than none.
        "warnings": list(WARNINGS),
        "libraries": libs,
        "items": items,
    }
    # An artwork pass rebuilt no media, so it has nothing to say about what is
    # on disk. Rewriting the manifest from it would record whatever tier and
    # bulk count *this* invocation happened to use as if they had been built.
    if not cfg.dry_run and not cfg.artwork_only:
        write_manifest(cfg, manifest)
        write_attribution(cfg)
        write_readme(cfg)

    _say()
    if WARNINGS:
        _say(f"{len(WARNINGS)} problem(s) — the build continued without these:")
        for warning in WARNINGS:
            _say(f"  ! {warning}")
        _say()
    if cfg.artwork_only:
        _say(f"Done: {artwork.drawn()} images redrawn across {len(items)} "
             f"items in {time.time() - started:.0f}s")
        _say("  the manifest was left as it was — no media changed")
        return manifest

    _say(f"Done: {len(items)} items in {time.time() - started:.0f}s"
         + (f", {len(WARNINGS)} skipped" if WARNINGS else ""))
    if not cfg.dry_run:
        _say(f"  manifest      {cfg.path(config.MANIFEST)}")
        _say(f"  attribution   {cfg.path(config.ATTRIBUTION)}")
        _say(f"  how to add it {cfg.path('README.md')}")
    return manifest


def _previous(cfg) -> dict:
    return read_manifest(cfg.root)


def read_manifest(root: str) -> dict:
    """The manifest already on disk, or {} if there is none to read."""
    path = os.path.join(root, config.MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _carry_forward(cfg, rebuilt: set) -> list[dict]:
    """Manifest entries for libraries this run did not touch."""
    return [item for item in _previous(cfg).get("items", [])
            if item.get("library") not in rebuilt]


def write_manifest(cfg, manifest: dict) -> str:
    path = cfg.path(config.MANIFEST)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def write_attribution(cfg) -> str:
    """The credits CC-BY actually requires, plus provenance for everything else."""
    path = cfg.path(config.ATTRIBUTION)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        "# Attribution",
        "",
        "This library was assembled by stdjflib. Everything in it is either",
        "generated from scratch or comes from the sources credited below.",
        "",
        "## Generated content",
        "",
        "Every file under `Test Media/`, `Shows/`, `Music/`, `Music Videos/`,",
        "`Photos/`, `Home Videos/` and `Books/`, and every poster, backdrop and",
        "logo, was generated by ffmpeg from synthetic sources. No third party",
        "holds rights in any of it.",
        "",
        "## Downloaded content",
        "",
    ]
    used = catalog.for_tier(cfg.tier)
    if not used:
        lines.append("This tier downloads nothing.")
    else:
        seen = set()
        for src in used:
            if src.attribution in seen:
                continue
            seen.add(src.attribution)
            lines.append(f"- {src.attribution}")
        lines += [
            "",
            "The Blender open movies are licensed CC BY; that licence requires",
            "the credits above to travel with any copy of the material. The",
            "Prelinger Archives items are in the public domain, and each item's",
            "own archive.org metadata is re-checked at download time — stdjflib",
            "refuses to fetch anything that does not declare a public-domain or",
            "Creative Commons licence at the moment it runs.",
        ]
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


def write_readme(cfg) -> str:
    """Instructions for pointing Jellyfin at what was just built."""
    path = cfg.path("README.md")
    rows = "\n".join(f"| `{name}` | {kind} |"
                     for name, kind in cfg.libraries().items())
    bulk_note = ""
    if cfg.bulk:
        bulk_note = f"""
## The `Bulk *` libraries

Roughly {cfg.bulk} items each, for testing what happens at scale rather than
what happens to one file: paging, virtualised scrolling, thumbnail cache
pressure, search and sort. Titles spread evenly across A-Z, with non-Latin,
numeric-leading and very long titles mixed in at intervals; years span 1950 to
2025; one item in eight has no artwork so the placeholder path gets exercised.

The video and audio are **shared between items by hard link** — a bulk item
exists to be listed, not played, and a thousand separate encodes would cost
minutes to produce files nobody opens. Artwork and metadata *are* per item,
because that is what a thumbnail cache and a sort actually touch.

Add them as separate libraries, or leave them out entirely: nothing in the
curated libraries depends on them.
"""
    text = f"""# Standard Jellyfin QA Library

Built by [stdjflib]. Tier: **{cfg.tier}**.

## Adding it to Jellyfin

Dashboard → Libraries → Add Media Library, once per row:

| Folder | Content type |
| --- | --- |
{rows}

Then, for every one of them, **turn off all metadata and image downloaders**.
Every item here ships an NFO with `<lockdata>true</lockdata>` and its own
artwork, so the library is fully offline and identical on every build. Leaving
an internet provider enabled reintroduces exactly the variability this library
exists to remove.

In the library's settings that means:

- Metadata downloaders: uncheck everything.
- Image fetchers: uncheck everything.
- Leave **Nfo** enabled under metadata savers/readers.
- "Prefer embedded titles over filenames" — off, so the path conventions
  under test are what actually get used.

## What is in here

- `Movies/` — the Blender open movies, public-domain shorts, and fixtures for
  every path convention Jellyfin resolves (loose files, multi-version,
  multi-part, extras, unicode titles).
- `Test Media/` — the generated codec, container, subtitle, HDR, frame-rate,
  scan-type and aspect-ratio matrix. Every clip burns its own description into
  the picture and repeats it in the item's plot, so you can see what a file is
  while it plays.
- `Shows/` — six series covering season folders, absolute numbering,
  date-based episodes, double episodes, missing episodes and flat layouts.
- `Music/` — FLAC, MP3, Opus and ALAC albums; embedded and folder artwork;
  multi-disc, various-artists and completely untagged cases.
- `Photos/` — all eight EXIF orientations, and several image formats.
- `Books/`, `Home Videos/`, `Music Videos/` — smaller sets of the same idea.

`{config.ATTRIBUTION}` credits every downloaded source.
`{config.MANIFEST}` lists everything that was built, and `stdjflib verify`
checks the library against it.
{bulk_note}

[stdjflib]: https://github.com/  (see the generator repo)
"""
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path
