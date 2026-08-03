"""NFO writers.

Every item in this library ships an NFO, and every NFO sets `<lockdata>true`.
That is the whole reason the library is usable for automated testing: without
it Jellyfin queries TMDB and friends on scan, and what the client sees depends
on the network, on the day, and on whatever a stranger last edited. With it,
two builds of the same tier present identical metadata forever.

The dialect is Kodi's, which is what Jellyfin's NFO reader implements. What
gets written is what `MediaBrowser.XbmcMetadata/Parsers/` actually reads —
`BaseNfoParser` plus the per-type parser — because a field the parser has no
`case` for is a field that changes nothing on screen.

Four things it reads are left out on purpose, and the reasons are worth
keeping:

- `watched`, `playcount`, `lastplayed` set *user* data, for the single account
  named in the NFO plugin's configuration. A fixture library that arrives with
  a viewing history has taken a decision that belongs to whoever is testing.
- `trailer` is a URL, and a library that is otherwise entirely offline should
  not have one item reaching for the network. The extras fixture ships real
  local trailer files instead.
- `lockedfields` locks a named subset of fields; `lockdata` already locks all
  of them, which is what this library wants everywhere.
- `namedseason` on a series is parsed with `reader.Skip()` — Jellyfin reads it
  and discards it. Season names come from `season.nfo`'s `seasonname`.

`outline` is the odd one: `BaseNfoSaver` writes it and `BaseNfoParser` has no
case for it, so Jellyfin round-trips it without ever using it. It stays,
because it costs a line and it is what the server's own saver would produce.
"""

from __future__ import annotations

import hashlib
import os
import xml.etree.ElementTree as ET

# Values are drawn from these deterministically so the library has variety
# without anything being random.
GENRES = ["Action", "Adventure", "Animation", "Comedy", "Documentary", "Drama",
          "Family", "Fantasy", "Horror", "Mystery", "Romance", "Sci-Fi",
          "Thriller", "Western"]
STUDIOS = ["Blender Foundation", "Standard QA Pictures", "Testcard Studios",
           "Reference Media Group", "Public Domain Archive"]
RATINGS = ["G", "PG", "PG-13", "R", "NR", "TV-14", "TV-MA", "TV-G"]
FIRST_NAMES = ["Ada", "Bo", "Cai", "Dara", "Eli", "Fay", "Gus", "Hana", "Ito",
               "Jo", "Kit", "Lior", "Mira", "Noa", "Ola", "Pax", "Quinn", "Rae"]
LAST_NAMES = ["Alvarez", "Brandt", "Chen", "Dahl", "Eriksen", "Farrow", "Gupta",
              "Haas", "Imani", "Jansen", "Kowalski", "Lindqvist", "Moreau"]

# `country` becomes ProductionLocations, which the parser splits on "/".
COUNTRIES = ["United States", "Germany", "Japan", "France", "Netherlands",
             "Brazil", "South Korea", "Canada"]

# Taglines sit under the title on a detail page, in their own type ramp. The
# last two are the cases worth having: one that is far too long for the space,
# and one carrying the punctuation that breaks naive escaping.
TAGLINES = [
    "One library. Every edge case.",
    "It was never about the codec.",
    "Some files resolve. Some do not.",
    "A tagline long enough to wrap past two lines on a phone, keep going "
    "across a television at ten feet, and still not be finished by the time "
    "the layout has given up on it entirely.",
    'They said "it renders fine here" — 100% of the time, & it did not.',
]

# CustomRating is free text and drives parental control, which is a different
# field from `mpaa` and a different code path. Deliberately not the same
# vocabulary as RATINGS: a client that shows one where the other belongs is
# then obvious rather than plausible.
CUSTOM_RATINGS = ["stdjflib-unrestricted", "stdjflib-guidance",
                  "stdjflib-restricted"]

# `airs_dayofweek` goes through `TVUtils.GetAirDays`, which only matches full
# English day names.
AIR_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]


def _seeded(key: str, options: list, n: int = 1) -> list:
    """Pick `n` distinct options from `options`, stable for a given key."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    picked, i = [], 0
    while len(picked) < min(n, len(options)):
        choice = options[digest[i % len(digest)] % len(options)]
        if choice not in picked:
            picked.append(choice)
        i += 1
        if i > 200:
            break
    return picked


def _bucket(key: str, n: int) -> int:
    """A stable 0..n-1 for `key`.

    Not `hash()`: Python salts string hashing per process, so a field chosen
    with it would differ between two runs of the same build and the NFOs would
    never compare equal.
    """
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % n


def person(key: str, index: int) -> str:
    first = _seeded(f"{key}-first-{index}", FIRST_NAMES)[0]
    last = _seeded(f"{key}-last-{index}", LAST_NAMES)[0]
    return f"{first} {last}"


def _text(parent: ET.Element, tag: str, value) -> None:
    if value is None or value == "":
        return
    ET.SubElement(parent, tag).text = str(value)


def _write(root: ET.Element, path: str) -> str:
    ET.indent(root, space="  ")
    payload = (b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
               + ET.tostring(root, encoding="utf-8") + b"\n")

    # Identical output is the *normal* case: nothing here depends on the
    # clock, so a rebuild produces the same bytes. Writing them anyway would
    # move every mtime, and Jellyfin refreshes metadata off mtimes — so a
    # rerun that changed nothing would still cost a full rescan.
    try:
        with open(path, "rb") as fh:
            if fh.read() == payload:
                return path
    except OSError:
        pass

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, path)
    return path


def _common(root: ET.Element, *, key: str, title: str, plot: str, year: int,
            rating: float, runtime_minutes: int, tags: list[str],
            sort_title: str | None = None, mpaa: str | None = None,
            people: bool = True) -> None:
    _text(root, "title", title)
    _text(root, "originaltitle", title)
    if sort_title:
        _text(root, "sorttitle", sort_title)
    _text(root, "plot", plot)
    _text(root, "outline", plot.split(".")[0] + "." if plot else "")
    _text(root, "year", year)
    _text(root, "premiered", f"{year}-01-01")
    _text(root, "runtime", max(1, runtime_minutes))
    _text(root, "mpaa", mpaa or _seeded(key, RATINGS)[0])

    # One in four ships without a tagline. The absent case is the one a client
    # is likeliest to lay out wrongly — a reserved gap where nothing is, or a
    # title that jumps position between two items in the same row.
    if _bucket(f"{key}-tagline", 4):
        _text(root, "tagline", _seeded(f"{key}-tagline", TAGLINES)[0])
    # ProductionLocations. Some items carry two, because the parser splits on
    # "/" and a client that renders the raw string shows "Japan/France".
    for place in _seeded(key, COUNTRIES, 1 + _bucket(f"{key}-country", 2)):
        _text(root, "country", place)
    # Out of 100, against the community rating's out of 10. Two scales on one
    # page is the point: a client that renders one in the other's units shows
    # a film rated 83 out of 10, and nothing about that looks like a bug until
    # you know what the number is.
    _text(root, "criticrating", 40 + _bucket(f"{key}-critic", 60))
    # Parental control's own field, which is not `mpaa` and is mostly absent
    # in a real library. Deliberately not the RATINGS vocabulary, so the two
    # are told apart on sight when a client puts one in the other's place.
    if _bucket(f"{key}-custom", 3) == 0:
        _text(root, "customrating", _seeded(f"{key}-custom", CUSTOM_RATINGS)[0])

    ratings = ET.SubElement(root, "ratings")
    node = ET.SubElement(ratings, "rating", {"name": "stdjflib", "max": "10",
                                             "default": "true"})
    _text(node, "value", f"{rating:.1f}")
    # Not `hash()` — Python salts string hashing per process, so it would give a
    # different vote count on every build and the NFOs would never compare equal.
    _text(node, "votes", 100 + int(hashlib.sha256(key.encode()).hexdigest()[:4], 16) % 900)

    for genre in _seeded(key, GENRES, 2):
        _text(root, "genre", genre)
    _text(root, "studio", _seeded(key, STUDIOS)[0])
    for tag in tags:
        _text(root, "tag", tag)

    if people:
        _text(root, "director", person(key, 0))
        _text(root, "credits", person(key, 1))
        for i in range(3):
            actor = ET.SubElement(root, "actor")
            _text(actor, "name", person(key, 10 + i))
            _text(actor, "role", ["Lead", "Supporting", "Voice"][i])
            _text(actor, "type", "Actor")
            _text(actor, "order", i)

    # A stable synthetic id under a namespace no real provider uses, so nothing
    # tries to resolve it against a live service.
    _text(root, "uniqueid", key)
    root.find("uniqueid").set("type", "stdjflib")
    root.find("uniqueid").set("default", "true")

    # Without this Jellyfin refreshes from the internet and the library stops
    # being reproducible — which defeats the entire purpose.
    _text(root, "lockdata", "true")
    _text(root, "dateadded", "2020-01-01 00:00:00")


def movie(path: str, *, key: str, title: str, plot: str, year: int,
          runtime_minutes: int, rating: float = 7.0, tags: list[str] | None = None,
          collection: str | None = None, sort_title: str | None = None,
          mpaa: str | None = None) -> str:
    root = ET.Element("movie")
    _common(root, key=key, title=title, plot=plot, year=year, rating=rating,
            runtime_minutes=runtime_minutes, tags=tags or [],
            sort_title=sort_title, mpaa=mpaa)
    if collection:
        node = ET.SubElement(root, "set")
        _text(node, "name", collection)
    return _write(root, path)


def tvshow(path: str, *, key: str, title: str, plot: str, year: int,
           rating: float = 7.5, tags: list[str] | None = None,
           status: str = "Ended", end_year: int | None = None,
           display_order: str | None = None) -> str:
    """A series.

    `display_order` is the one field here that changes what the *server* does
    rather than only what a client draws: `LibraryManager.
    FillMissingEpisodeNumbersFromPath` compares it against "absolute" and
    resolves episode numbers differently when it matches. "aired" and "dvd"
    are the other two values jellyfin-web offers and neither is acted on.
    """
    root = ET.Element("tvshow")
    _common(root, key=key, title=title, plot=plot, year=year, rating=rating,
            runtime_minutes=22, tags=tags or [])
    _text(root, "status", status)
    # A series that says it ended and never says when leaves the series page
    # half-populated, which is a state worth being able to see on purpose.
    if status == "Ended":
        _text(root, "enddate", f"{end_year or year + 2}-12-31")
    _text(root, "airs_dayofweek", _seeded(key, AIR_DAYS)[0])
    _text(root, "airs_time", ["20:00", "21:00", "22:30"][_bucket(key, 3)])
    if display_order:
        _text(root, "displayorder", display_order)
    return _write(root, path)


def season(path: str, *, key: str, title: str, plot: str, number: int,
           year: int) -> str:
    root = ET.Element("season")
    # Both spellings: `title` is `BaseNfoParser`'s and `seasonname` is
    # `SeasonNfoParser`'s own, and they set the same field. A series NFO's
    # `namedseason` does not — Jellyfin parses that one with `reader.Skip()` —
    # so this file is the only place a season's name can come from.
    _text(root, "title", title)
    _text(root, "seasonname", title)
    _text(root, "seasonnumber", number)
    _text(root, "plot", plot)
    _text(root, "year", year)
    _text(root, "premiered", f"{year}-01-01")
    _text(root, "uniqueid", key)
    root.find("uniqueid").set("type", "stdjflib")
    root.find("uniqueid").set("default", "true")
    _text(root, "lockdata", "true")
    _text(root, "dateadded", "2020-01-01 00:00:00")
    return _write(root, path)


def episode(path: str, *, key: str, title: str, plot: str, season_no: int,
            episode_no: int, aired: str, runtime_minutes: int,
            rating: float = 7.0, end_episode: int | None = None,
            show_title: str | None = None, people: bool = True,
            airs_after_season: int | None = None,
            airs_before_season: int | None = None,
            airs_before_episode: int | None = None) -> str:
    """One episode.

    The three `airs_*` values are how a special says where it belongs in
    watch order. They are `Episode.AirsAfterSeasonNumber`,
    `AirsBeforeSeasonNumber` and `AirsBeforeEpisodeNumber`, and a client that
    ignores them files every special at the end of the show instead of between
    the two episodes it sits between — which is the whole reason a Season 00
    exists.
    """
    root = ET.Element("episodedetails")
    _text(root, "title", title)
    # SeriesName. Jellyfin has it from the folder already; a client that reads
    # the episode alone — a search hit, a Next Up card — does not.
    _text(root, "showtitle", show_title)
    _text(root, "season", season_no)
    _text(root, "episode", episode_no)
    if end_episode is not None:
        # A file holding two episodes. Jellyfin reads the span from the
        # filename, but the NFO has to agree or the second one goes missing.
        _text(root, "episodenumberend", end_episode)
    _text(root, "airsafter_season", airs_after_season)
    _text(root, "airsbefore_season", airs_before_season)
    _text(root, "airsbefore_episode", airs_before_episode)
    _text(root, "plot", plot)
    _text(root, "aired", aired)
    _text(root, "premiered", aired)
    _text(root, "runtime", max(1, runtime_minutes))
    if people:
        # An episode carries its own cast and crew, and a client's episode
        # page has somewhere to put them. Guest stars are the point: they
        # differ per episode, so a client showing the series cast on every
        # episode is caught here rather than looking plausible.
        _text(root, "director", person(key, 0))
        _text(root, "credits", person(key, 1))
        for i in range(2):
            actor = ET.SubElement(root, "actor")
            _text(actor, "name", person(key, 20 + i))
            _text(actor, "role", ["Guest Star", "Voice"][i])
            _text(actor, "type", "Actor")
            _text(actor, "order", i)
    ratings = ET.SubElement(root, "ratings")
    node = ET.SubElement(ratings, "rating", {"name": "stdjflib", "max": "10",
                                             "default": "true"})
    _text(node, "value", f"{rating:.1f}")
    _text(root, "uniqueid", key)
    root.find("uniqueid").set("type", "stdjflib")
    root.find("uniqueid").set("default", "true")
    _text(root, "lockdata", "true")
    _text(root, "dateadded", "2020-01-01 00:00:00")
    return _write(root, path)


def musicvideo(path: str, *, key: str, title: str, artist: str, album: str,
               year: int, plot: str, runtime_minutes: int,
               rating: float = 7.0, tags: list[str] | None = None) -> str:
    """A music video.

    Jellyfin has no music-video parser of its own: `MusicVideo` is read by
    `MovieNfoParser`, which is where `artist` and `album` come from. So the
    whole of `_common` applies here too, and leaving it out was the difference
    between a music video with a genre, a studio and a cast and one with four
    fields.
    """
    root = ET.Element("musicvideo")
    _common(root, key=key, title=title, plot=plot, year=year, rating=rating,
            runtime_minutes=runtime_minutes, tags=tags or [])
    _text(root, "artist", artist)
    _text(root, "album", album)
    return _write(root, path)


def collection(path: str, *, key: str, title: str, plot: str) -> str:
    root = ET.Element("collection")
    _text(root, "title", title)
    _text(root, "plot", plot)
    _text(root, "uniqueid", key)
    root.find("uniqueid").set("type", "stdjflib")
    _text(root, "lockdata", "true")
    return _write(root, path)
