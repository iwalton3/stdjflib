"""`collection.xml` — the file that makes a folder a collection.

This is **not** the Kodi dialect `nfo.py` writes. A collection is read by
`MediaBrowser.LocalMetadata/Parsers/BoxSetXmlParser.cs`, which subclasses
`BaseItemXmlParser` — the older Emby-flavoured XML, rooted at `<Item>`, with
PascalCase element names and its own vocabulary. `<title>` means nothing here;
the element that sets the name is `<LocalTitle>`. Writing a `<collection>`
document with Kodi field names produces a file the server opens, finds nothing
it recognises in, and discards without a word.

What is written is what the parser has a `case` for, the same rule `nfo.py`
follows. The list was read out of `BaseItemXmlParser.FetchDataFromXmlNode` and
checked against `v10.11.0` as well as `master`: the two read an identical set
of fields, so nothing here needs a version guard.

Two fields the parser reads are deliberately left out:

- `Shares` and `OwnerUserId` are Playlist-only. `IHasShares` is implemented by
  `Playlist` and by nothing else, so a `<Shares>` block on a collection parses
  and is dropped on the floor. (`Folder.cs` has a comment claiming BoxSets have
  per-user visibility too. They do not — a collection's visibility is
  `IsVisibleStandalone` plus the parental check, and neither is per-user.)
- There is no equivalent of `<uniqueid type="stdjflib">`. Provider ids in this
  dialect are matched against `ProviderManager.GetExternalIdInfos`, so the only
  spellings that survive are real services' — `TmdbId` and friends. A made-up
  namespace is silently dropped rather than round-tripped, so the fixture key
  travels in `<Tags>` instead, where it is at least visible in a client.

## How a folder becomes a collection

`BoxSetResolver` takes a directory when **either** its name contains
`[boxset]` **or** it holds a `collection.xml`. Either condition alone is
enough, which is why one fixture here has the suffix and one does not. The
suffix is stripped from the resolved name; `<LocalTitle>` then overrides
whatever is left.

## Members are relative paths on purpose

`BaseItem.GetLinkedChild` resolves a member through
`FileSystem.MakeAbsolutePath(ContainingFolderPath, path)`, which is
`Path.GetFullPath(Path.Join(...))` for anything not already rooted — identical
code in 10.11 and 12.0. So `../Movies/Foo/Foo.mkv` is resolved against the
collection's own folder, and the same library works at `/srv/qa` on the host
and at `/media` inside a container. An absolute path would bake the host's
mount point into the fixture and break the container exactly the way
`--stream-origin` does, except silently: the collection still resolves, it is
simply empty.

The path has to be the **item's** path as Jellyfin recorded it, which is the
media file — not the folder that holds it, even when the folder is what names
the movie. The one exception is a multi-version item, whose path is its
primary version's file.
"""

from __future__ import annotations

import os
import posixpath
import xml.etree.ElementTree as ET

# Suffix that makes a directory a collection regardless of what is inside it.
MARKER = "[boxset]"

# What the server itself names the file. `BoxSetXmlProvider.GetXmlFile` joins
# it onto the item path and looks for nothing else.
FILENAME = "collection.xml"

# `BoxSet.DisplayOrder` is parsed with `Enum.TryParse<ItemSortBy>`, and
# anything that fails to parse falls back to PremiereDate — so a typo here is
# not an error, it is the default. These are the values that mean something.
DISPLAY_ORDERS = ("PremiereDate", "SortName", "ProductionYear", "DateCreated",
                  "Default")


def member_path(collection_folder: str, target: str) -> str:
    """The path to write for a member, relative to the collection's folder.

    Always forward slashes: this is read by `Path.Join`, which takes them on
    every platform, and a backslash would be a literal character in a name on
    the Linux servers this library is built for.
    """
    rel = os.path.relpath(os.path.abspath(target),
                          os.path.abspath(collection_folder))
    return rel.replace(os.sep, posixpath.sep)


def _text(parent, tag: str, value) -> None:
    if value is None or value == "":
        return
    ET.SubElement(parent, tag).text = str(value)


def write(path: str, *, title: str, plot: str, members: list[str],
          display_order: str | None = None, tags: list[str] | None = None,
          sort_title: str | None = None, year: int | None = None,
          content_rating: str | None = None,
          genres: list[str] | None = None) -> str:
    """Write `collection.xml` into the directory `path`.

    `members` are paths already made relative by `member_path`. A collection
    with no members is a legal thing to write and is not what this library
    wants anywhere: `BoxSet.IsLegacyBoxSet` keys off `LinkedChildren.Length ==
    0`, so an empty `<CollectionItems>` silently converts the fixture into the
    other kind of collection entirely — one whose children come from the
    filesystem.
    """
    root = ET.Element("Item")
    _text(root, "LocalTitle", title)
    _text(root, "SortTitle", sort_title)
    _text(root, "Overview", plot)
    _text(root, "ProductionYear", year)
    _text(root, "ContentRating", content_rating)
    if display_order:
        _text(root, "DisplayOrder", display_order)

    if genres:
        node = ET.SubElement(root, "Genres")
        for genre in genres:
            _text(node, "Genre", genre)

    if tags:
        node = ET.SubElement(root, "Tags")
        for tag in tags:
            _text(node, "Tag", tag)

    # Same reason as every NFO here: without it the server asks TMDB what this
    # collection is. `MetadataFetchers` is emptied for BoxSet in both provider
    # layers as well, and this is the third lock on the same door.
    #
    # It has the same consequence it has everywhere else in this library: an
    # item already in the database is never re-read from its metadata file, so
    # editing a collection.xml does not reach a server that has scanned it.
    # `serve --fresh` is what propagates a change.
    _text(root, "LockData", "true")

    node = ET.SubElement(root, "CollectionItems")
    for member in members:
        _text(ET.SubElement(node, "CollectionItem"), "Path", member)

    ET.indent(root, space="  ")
    out = os.path.join(path, FILENAME)
    os.makedirs(path, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n')
        fh.write(ET.tostring(root, encoding="unicode"))
        fh.write("\n")
    return out


def read_members(path: str) -> list[str]:
    """The member paths in a `collection.xml`, as written.

    A second statement of the parser rather than a call back into `write`, for
    the same reason `verify._ARTWORK_STEMS` restates the artwork mapping: a
    check that inherits its expectations from the thing it checks is not a
    check.
    """
    root = ET.parse(path).getroot()
    out = []
    for item in root.findall("./CollectionItems/CollectionItem"):
        node = item.find("Path")
        if node is not None and node.text:
            out.append(node.text.strip())
    return out
