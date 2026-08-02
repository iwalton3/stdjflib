"""NFO writers.

Every item in this library ships an NFO, and every NFO sets `<lockdata>true`.
That is the whole reason the library is usable for automated testing: without
it Jellyfin queries TMDB and friends on scan, and what the client sees depends
on the network, on the day, and on whatever a stranger last edited. With it,
two builds of the same tier present identical metadata forever.

The dialect is Kodi's, which is what Jellyfin's NFO reader implements. Fields
Jellyfin ignores are omitted rather than written and wasted.
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
           status: str = "Ended") -> str:
    root = ET.Element("tvshow")
    _common(root, key=key, title=title, plot=plot, year=year, rating=rating,
            runtime_minutes=22, tags=tags or [])
    _text(root, "status", status)
    return _write(root, path)


def season(path: str, *, key: str, title: str, plot: str, number: int,
           year: int) -> str:
    root = ET.Element("season")
    _text(root, "title", title)
    _text(root, "seasonnumber", number)
    _text(root, "plot", plot)
    _text(root, "year", year)
    _text(root, "lockdata", "true")
    return _write(root, path)


def episode(path: str, *, key: str, title: str, plot: str, season_no: int,
            episode_no: int, aired: str, runtime_minutes: int,
            rating: float = 7.0, end_episode: int | None = None) -> str:
    root = ET.Element("episodedetails")
    _text(root, "title", title)
    _text(root, "season", season_no)
    _text(root, "episode", episode_no)
    if end_episode is not None:
        # A file holding two episodes. Jellyfin reads the span from the
        # filename, but the NFO has to agree or the second one goes missing.
        _text(root, "episodenumberend", end_episode)
    _text(root, "plot", plot)
    _text(root, "aired", aired)
    _text(root, "runtime", max(1, runtime_minutes))
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
               year: int, plot: str, runtime_minutes: int) -> str:
    root = ET.Element("musicvideo")
    _text(root, "title", title)
    _text(root, "artist", artist)
    _text(root, "album", album)
    _text(root, "year", year)
    _text(root, "plot", plot)
    _text(root, "runtime", max(1, runtime_minutes))
    _text(root, "uniqueid", key)
    root.find("uniqueid").set("type", "stdjflib")
    _text(root, "lockdata", "true")
    return _write(root, path)


def collection(path: str, *, key: str, title: str, plot: str) -> str:
    root = ET.Element("collection")
    _text(root, "title", title)
    _text(root, "plot", plot)
    _text(root, "uniqueid", key)
    root.find("uniqueid").set("type", "stdjflib")
    _text(root, "lockdata", "true")
    return _write(root, path)
