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

from . import artwork, catalog, config, fetch, ff, libraries, nfo, photos


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
    ("Mixed Content", libraries.build_mixed_content),
    ("Books", libraries.build_books),
    # Before "Box Sets" below, though nothing links the two: this library's
    # collections are built by the server during a scan, not by us.
    ("Auto Collections", libraries.build_auto_collections),
]


def run(cfg) -> dict:
    started = time.time()
    WARNINGS.clear()
    artwork.reset_drawn()
    if cfg.artwork_only:
        _say(f"stdjflib — redrawing the artwork of the {cfg.tier} tier "
             f"in {cfg.root}")
        _say("  media is left alone"
             + ("" if cfg.use_artwork else "; nothing is downloaded"))
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

    if cfg.use_artwork:
        # Up front rather than lazily inside the builders: this is the one
        # part of an artwork run that touches the network, and it should
        # finish (or fail) before several thousand images depend on it.
        _say("Photographs for the artwork (picsum.photos, Unsplash licence)")
        have = photos.download(cfg, say=_say)
        _say(f"  {have} photographs ready")
        if not have:
            _warn("no photographs available — falling back to drawn artwork")
        _say()

    items: list[dict] = []

    # Before the libraries that point at it. Not a library itself — it lives
    # under `.stdjflib/` precisely so no resolver ever sees it — so it is
    # built directly rather than through BUILDERS, and only when something
    # that names it is being built.
    rebuilt_extra: set = set()
    if cfg.wants("Movies") or cfg.wants("Shows"):
        origin_items = libraries.build_origin(cfg)
        if origin_items:
            rebuilt_extra.add("Origin")
            _say(f"Stream origin — {len(origin_items)} clip(s) at "
                 f"{cfg.stream_origin}")
            items += origin_items

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

    # After every library that owns media, because a collection is a list of
    # paths into them and has nothing of its own to build. `--only "Box Sets"`
    # is still meant to work, so the members it cannot see in this run are
    # looked up in the manifest the last build left behind.
    if cfg.wants("Box Sets"):
        _say("Box Sets")
        made = libraries.build_box_sets(
            cfg.path("Box Sets"), cfg, _member_paths(cfg, items))
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
        #
        # `rebuilt_extra` carries the origin clips, which are not a library and
        # so are not in `libs` — without it a `--only Movies` run would keep
        # the previous manifest's copy of them *and* add this run's, and the
        # manifest would grow a duplicate pair on every partial build.
        items = _carry_forward(cfg, set(libs) | rebuilt_extra) + items
        libs = {**_previous(cfg).get("libraries", {}), **libs}

    manifest = {
        "build_version": config.BUILD_VERSION,
        "tier": cfg.tier,
        "seed": cfg.seed,
        "ffmpeg": ff.version(cfg.ffmpeg),
        "hwaccel": cfg.hwaccel,
        "bulk": cfg.bulk,
        "use_artwork": cfg.use_artwork,
        # Recorded because it is baked into the `.strm` files and cannot be
        # changed at startup: `serve` binds the port it names, and `container`
        # checks whether the host it names is reachable from inside.
        "stream_origin": cfg.stream_origin,
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
        # The manifest is not rewritten from an artwork pass — it would record
        # this invocation's tier and bulk as though they had been built. The
        # one field that *is* about the artwork gets updated, so a later
        # redraw does not silently swap the library back.
        _remember_artwork_choice(cfg)
        if cfg.use_artwork:
            write_attribution(cfg)
        _say(f"Done: {artwork.drawn()} images redrawn across {len(items)} "
             f"items in {time.time() - started:.0f}s")
        _say("  the manifest was left as it was, apart from noting which "
             "artwork it now has")
        return manifest

    _say(f"Done: {len(items)} items in {time.time() - started:.0f}s"
         + (f", {len(WARNINGS)} skipped" if WARNINGS else ""))
    if not cfg.dry_run:
        _say(f"  manifest      {cfg.path(config.MANIFEST)}")
        _say(f"  attribution   {cfg.path(config.ATTRIBUTION)}")
        _say(f"  how to add it {cfg.path('README.md')}")
    return manifest


def _remember_artwork_choice(cfg) -> None:
    previous = read_manifest(cfg.root)
    if not previous or previous.get("use_artwork") == cfg.use_artwork:
        return
    previous["use_artwork"] = cfg.use_artwork
    write_manifest(cfg, previous)


def _previous(cfg) -> dict:
    return read_manifest(cfg.root)


def _member_paths(cfg, items: list[dict]) -> dict:
    """Item key -> path, for the collections to point at.

    This run's items win over the previous manifest's, so a rebuilt item is
    linked at wherever it landed this time. The manifest underneath is what
    makes `--only "Box Sets"` produce collections with members in them rather
    than empty folders.

    A multi-version item's manifest `path` is the folder the build wrote, and
    the item's path on the server is its primary version's file. `primary` is
    that file, recorded by the movies builder, and it is what a member has to
    name — the folder resolves to nothing.
    """
    def resolved(item):
        return item.get("primary") or item["path"]

    paths = {item["key"]: resolved(item)
             for item in _previous(cfg).get("items", []) if "key" in item}
    paths.update({item["key"]: resolved(item) for item in items})
    return paths


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
        "Every media file under `Test Media/`, `Shows/`, `Music/`,",
        "`Music Videos/`, `Photos/`, `Home Videos/`, `Mixed Content/` and",
        "`Books/` was generated by ffmpeg from synthetic sources. No third",
        "party holds rights in any of it. Two exceptions, neither of which",
        "adds one: the `.strm` files hold no media at all (see below), and",
        "the EPUBs, PDFs and comic archives under `Books/` are written",
        "directly by stdjflib rather than by ffmpeg, because they are not",
        "media. Their pages are generated images.",
        "",
    ]
    if cfg.use_artwork:
        credits = photos.credits(cfg)
        lines += [
            "## Artwork photographs",
            "",
            f"This library was built with `--use-artwork`, so the {len(credits)}",
            "photographs credited below sit behind its posters, backdrops,",
            "banners and covers. They come from Lorem Picsum",
            "(https://picsum.photos), which serves photographs from Unsplash.",
            "",
            "The Unsplash licence (https://unsplash.com/license) grants free",
            "use, commercial and not, with no permission required and no",
            "attribution obliged. The credits are here because crediting a",
            "photographer whose work you are redistributing is right, not",
            "because a licence compels it.",
            "",
        ]
        for ident, author, url in credits:
            lines.append(f"- picsum {ident}: {author} — {url}")
        lines.append("")
    else:
        lines += [
            "Every poster, backdrop, banner and logo was drawn by ffmpeg too.",
            "`--use-artwork` replaces those with photographs; see ATTRIBUTION",
            "in a library built that way.",
            "",
        ]
    streamed = sorted({catalog.by_key(k).attribution
                       for k in libraries.STRM_SOURCES.values()})
    lines += [
        "## Streamed content",
        "",
        "The `.strm` fixtures are one line of text each; nothing is copied and",
        "nothing is downloaded, at any tier. Most of them point at the items",
        "below, so playing one fetches it from its original host at the moment",
        "you press play. They are credited here anyway, because the library",
        "names them whether or not it holds a copy.",
        "",
        "Two do not appear below and need no credit: they point at",
        "`.stdjflib/origin/`, which is generated by ffmpeg like everything else",
        "here and served over HTTP from this machine so that an end-to-end",
        "playback test needs no network.",
        "",
    ]
    lines += [f"- {credit}" for credit in streamed]
    lines += [
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
  multi-part, extras, unicode titles, `.strm` stream files).
- `.stdjflib/origin/` — not a library. Two clips served over HTTP on port
  8410 by `stdjflib serve`, so the `.strm` fixtures that point at them play
  with no network. Do not add this as a library; it would turn a stream
  target into an item.
- `Test Media/` — the generated codec, container, subtitle, HDR, frame-rate,
  scan-type and aspect-ratio matrix. Every clip burns its own description into
  the picture and repeats it in the item's plot, so you can see what a file is
  while it plays.
- `Shows/` — eight series covering season folders, absolute numbering,
  date-based episodes, double episodes, missing episodes, flat layouts,
  episodes that exist in several versions, and a season built from `.strm`
  stream files.
- `Music/` — FLAC, MP3, Opus and ALAC albums; embedded and folder artwork;
  multi-disc, various-artists and completely untagged cases.
- `Photos/` — all eight EXIF orientations, and several image formats.
- `Mixed Content/` — videos and photographs in one tree, at uneven depths,
  with folders that hold one kind, the other, or both. The untidy counterpart
  to `Home Videos/`.
- `Books/` — EPUB, PDF and comic archives, plus **audiobooks in both shapes
  the server produces, at both lengths that matter**: a chaptered `.m4b` that
  resolves to a single item with real chapter rows, and a multi-file rip that
  resolves to one item per file, joined only by their `Album` tag — each of
  them once too short to hold a resume position and once long enough. An
  audiobook's resume window is measured in *minutes* off each end, not in
  percentages, so nothing under ten minutes can hold one at all. Also the
  three comic metadata dialects (two of
  which the server reads only from a `.cbz`), both comic cover rules, the
  formats that catalogue and that no client can open, and one book per
  filename convention the resolver parses. `Epub Two Dialect/` is EPUB 2.0.1 —
  an NCX rather than a nav document, calibre series metadata, MARC relator
  roles and an embedded cover, none of which an EPUB 3 file can express.
  `Long Form/` is the pair meant for
  testing a *reader* rather than a resolver: a 24-chapter EPUB with a real
  table of contents, and a 240-page PDF. Folders here are load-bearing: a
  folder holding exactly one book resolves as *the book*, named after the
  folder — so the folder itself is never an item — and stops doing so at the
  second one. Both things a folder of audiobooks can mean are covered:
  `Lior Levy/` holds three *different* books loose in one folder, each with
  its own `Album`, which is the only field that distinguishes them from the
  chapters of one; `Mo Mensah/` holds a rip in a subfolder and loose books
  beside it. **Descriptions** are here too, in each of the places the server
  reads one from — a `.m4b`'s `description` (falling back to `comment`), an
  `.mp3`'s ID3 `TIT3` frame, an EPUB's `dc:description` — plus one on a
  *folder*, which no file on disk can carry and which `provision` sets
  through the API after the scan.
- `Home Videos/`, `Music Videos/` — smaller sets of the same idea.

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
