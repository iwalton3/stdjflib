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

from . import artwork, ff, generate, nfo, recipes
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

    ordered = sorted(recipes.for_tier(cfg.tier),
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
     "three sources, not three items."),
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
]


def build_movies(root: str, cfg) -> list[dict]:
    """The naming-convention fixtures.

    Downloaded films are placed by `build.place_movies`, which owns the cache
    and the sidecar subtitles; this function only builds the synthetic
    fixtures that demonstrate each path convention.
    """
    made = []
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

        elif shape == "versions":
            folder = os.path.join(root, f"{safe} ({year})")
            variants = [
                ("Bluray-1080p", Video(width=1920, height=1080, bitrate="4000k")),
                ("WEBDL-720p", Video(width=1280, height=720, bitrate="1500k")),
                ("SDTV-480p", Video(width=854, height=480, bitrate="700k")),
            ]
            for tag, video in variants:
                v = _clip(rec, key=f"{rec.key}-{tag}", video=video)
                _emit(v, os.path.join(folder, f"{safe} ({year}) - [{tag}].mkv"),
                      cfg)
            if not cfg.dry_run:
                nfo.movie(os.path.join(folder, "movie.nfo"), key=rec.key,
                          title=title, plot=plot, year=year, runtime_minutes=1,
                          tags=["stdjflib", "naming"])
                artwork.folder_images(folder, rec.key, title, cfg)
            made.append({"library": "Movies", "key": rec.key, "path": folder})

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
        "key": "flat-show", "title": "Flat Show No Season Folders", "year": 2023,
        "style": "flat", "episodes": 6,
        "plot": "SxxExx files sitting directly in the show folder with no "
                "season directories at all.",
    },
]

EPISODE_TITLES = [
    "Pilot", "The Second One", "Something Happens", "A Complication",
    "The Turn", "Consequences", "The Long Night", "Resolution",
    "An Epilogue", "The Reunion", "Aftermath", "Full Circle",
]


def _season_artwork(series_folder: str, season_folder: str, season_no: int,
                    show_key: str, show_title: str, cfg, *,
                    in_series_folder: bool) -> None:
    """One season's artwork, in whichever of the two places it is being tested.

    A season's Primary is a poster like the series' own — the shape does not
    change just because the item is a season, and a client that draws seasons
    at 16:9 crops the title off every one of them.
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
    for show in SHOWS:
        title = show["title"]
        folder = os.path.join(root, f"{title} ({show['year']})")
        key = show["key"]

        if not cfg.dry_run:
            nfo.tvshow(os.path.join(folder, "tvshow.nfo"), key=key, title=title,
                       plot=show["plot"], year=show["year"],
                       tags=["stdjflib", "shows"])
            # One show carries every image type Jellyfin has, so a client can
            # be pointed at a single title to exercise all of them; the rest
            # get the ordinary series set.
            artwork.folder_images(
                folder, key, title, cfg,
                kinds=(artwork.SETS["everything"] if key == "standard-show"
                       else artwork.SETS["series"]))

        style = show["style"]
        rec = _short(title, f"{key}-ep", duration=10)

        def episode(season_no, ep_no, path, ep_title, aired, end=None,
                    _rec=rec, _key=key, _show=show):
            """Queue one episode. `_rec`/`_key`/`_show` are bound as defaults
            because they change on every turn of the enclosing loop, and a
            closure over them would see only the last show's values."""
            ep_key = f"{_key}-s{season_no}e{ep_no}"

            def run():
                v = _clip(_rec, key=ep_key, title=ep_title)
                if not _emit(v, path, cfg):
                    return None
                if cfg.dry_run:
                    return None
                nfo.episode(os.path.splitext(path)[0] + ".nfo",
                            key=ep_key, title=ep_title,
                            plot=_show["plot"], season_no=season_no,
                            episode_no=ep_no, aired=aired, runtime_minutes=1,
                            end_episode=end)
                # `<episode>-thumb.jpg`, which Jellyfin registers as the
                # episode's *Primary* — hence 16:9, and hence a row of
                # episodes laying out landscape rather than as posters.
                artwork.sidecar_images(path, ep_key, ep_title, cfg,
                                       kinds=("thumb",),
                                       subtitle=f"S{season_no:02d}E{ep_no:02d}")
                return {"library": "Shows", "key": ep_key, "path": path}

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
                    # one is `season01-poster.jpg` up in the series folder,
                    # which is where the resolver looks first; season two is
                    # `Season 02/poster.jpg`, which is where people put it.
                    # A client that only handles one shows half the posters.
                    _season_artwork(folder, sdir, season_no, key, title, cfg,
                                    in_series_folder=season_no == 1)
                for ep_no in range(1, count + 1):
                    et = EPISODE_TITLES[(ep_no - 1) % len(EPISODE_TITLES)]
                    episode(season_no, ep_no,
                            os.path.join(sdir, f"{title} - S{season_no:02d}E{ep_no:02d} - {et}.mkv"),
                            et, f"{show['year'] + season_no - 1}-0{season_no}-{ep_no:02d}")
            sdir = os.path.join(folder, "Season 00")
            if not cfg.dry_run:
                # Season 0 is spelled `season-specials-…`, not `season00-…`.
                # The one season name a client is most likely to get wrong.
                _season_artwork(folder, sdir, 0, key, title, cfg,
                                in_series_folder=True)
            for ep_no in range(1, show["specials"] + 1):
                episode(0, ep_no,
                        os.path.join(sdir, f"{title} - S00E{ep_no:02d} - Special {ep_no}.mkv"),
                        f"Special {ep_no}", f"{show['year']}-12-{24 + ep_no:02d}")

        elif style == "absolute":
            for ep_no in range(1, show["episodes"] + 1):
                et = EPISODE_TITLES[(ep_no - 1) % len(EPISODE_TITLES)]
                episode(1, ep_no,
                        os.path.join(folder, f"{title} - {ep_no:03d} - {et}.mkv"),
                        et, f"{show['year']}-01-{ep_no:02d}")

        elif style == "dated":
            for ep_no in range(1, show["episodes"] + 1):
                date = f"{show['year']}-03-{ep_no + 9:02d}"
                sdir = os.path.join(folder, f"Season {show['year']}")
                episode(show["year"], ep_no,
                        os.path.join(sdir, f"{title} - {date}.mkv"),
                        f"Episode of {date}", date)

        elif style == "double":
            sdir = os.path.join(folder, "Season 01")
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
            for ep_no in (1, 2, 5, 6, 9):
                et = EPISODE_TITLES[(ep_no - 1) % len(EPISODE_TITLES)]
                episode(1, ep_no,
                        os.path.join(sdir, f"{title} - S01E{ep_no:02d} - {et}.mkv"),
                        et, f"{show['year']}-09-{ep_no:02d}")

        elif style == "flat":
            for ep_no in range(1, show["episodes"] + 1):
                et = EPISODE_TITLES[(ep_no - 1) % len(EPISODE_TITLES)]
                episode(1, ep_no,
                        os.path.join(folder, f"{title} - S01E{ep_no:02d} - {et}.mkv"),
                        et, f"{show['year']}-02-{ep_no:02d}")

        made.append({"library": "Shows", "key": key, "path": folder})

    # Every episode across every show, in one pool rather than six.
    made += [item for item in _run_all(tasks, cfg) if item]
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
]

TRACK_NAMES = ["Opening", "Second Movement", "Interlude", "The Long One",
               "Reprise", "Closing", "Hidden Track", "Bonus"]


def build_music(root: str, cfg) -> list[dict]:
    """One task per album, tracks sequential within it.

    The album is the unit because the embedded-art case writes one cover file,
    uses it for every track, then deletes it — a lifecycle that would need
    locking if two albums shared a worker mid-flight.
    """
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
                        out.append({"library": "Music",
                                    "key": f"{album['key']}-d{disc}t{n}",
                                    "path": path})
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


def build_books(root: str, cfg) -> list[dict]:
    """EPUB and CBZ, both of which are zip files this can write directly."""
    import zipfile

    made = []
    if cfg.dry_run:
        return made

    for i, (title, author) in enumerate(
            [("The Standard Reference", "Ada Alvarez"),
             ("A Second Volume", "Bo Brandt"),
             ("日本語の本", "Cai Chen")], 1):
        folder = os.path.join(root, author)
        os.makedirs(folder, exist_ok=True)
        epub = os.path.join(folder, f"{title}.epub")
        _write_epub(epub, title, author, cfg)
        made.append({"library": "Books", "key": f"book-{i}", "path": epub})

    # A comic archive: a zip of numbered images, which is all a CBZ is.
    comics = os.path.join(root, "Comics")
    os.makedirs(comics, exist_ok=True)
    cbz = os.path.join(comics, "A Test Comic 001.cbz")
    pages = []
    for p in range(1, 5):
        page = os.path.join(comics, f".page{p}.jpg")
        if artwork.draw("photo", f"cbz-{p}", f"Page {p}", page, cfg,
                        size=(1200, 1800), stamp=False):
            pages.append(page)
    if pages:
        with zipfile.ZipFile(cbz, "w") as zf:
            for p, page in enumerate(pages, 1):
                zf.write(page, f"{p:03d}.jpg")
        for page in pages:
            os.unlink(page)
        made.append({"library": "Books", "key": "cbz-1", "path": cbz})
    return made


def _write_epub(path: str, title: str, author: str, cfg=None) -> None:
    import zipfile

    # An EPUB holds no artwork, so a redraw has nothing to change in one —
    # and rewriting it would only churn the zip's timestamps and send
    # Jellyfin off to rescan a file that is identical in every way that
    # matters.
    if cfg is not None and cfg.artwork_only and os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        # mimetype must be first and stored uncompressed — that is what makes
        # the file identifiable as an EPUB by its first bytes.
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml",
                    '<?xml version="1.0"?>\n'
                    '<container version="1.0" '
                    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                    '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
                    'media-type="application/oebps-package+xml"/></rootfiles>\n'
                    '</container>\n')
        zf.writestr("OEBPS/content.opf",
                    f'<?xml version="1.0" encoding="utf-8"?>\n'
                    f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                    f'unique-identifier="bookid">\n'
                    f'  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
                    f'    <dc:identifier id="bookid">stdjflib-{title}</dc:identifier>\n'
                    f'    <dc:title>{title}</dc:title>\n'
                    f'    <dc:creator>{author}</dc:creator>\n'
                    f'    <dc:language>en</dc:language>\n'
                    f'  </metadata>\n'
                    f'  <manifest><item id="c1" href="ch1.xhtml" '
                    f'media-type="application/xhtml+xml"/></manifest>\n'
                    f'  <spine><itemref idref="c1"/></spine>\n'
                    f'</package>\n')
        zf.writestr("OEBPS/ch1.xhtml",
                    f'<?xml version="1.0" encoding="utf-8"?>\n'
                    f'<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                    f'<title>{title}</title></head><body>\n'
                    f'<h1>{title}</h1><p>By {author}.</p>\n'
                    f'<p>Generated by stdjflib as library test content.</p>\n'
                    f'</body></html>\n')


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
                artwork.folder_images(folder, meta["key"], meta["title"], cfg,
                                      kinds=("poster", "backdrop"), seq=index)
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
                            aired=f"{meta['year']}-01-01", runtime_minutes=1)
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
            _write_epub(path, meta["title"], author, cfg)
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
