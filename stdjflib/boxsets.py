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

## This is the one metadata file here that does **not** set `lockdata`

Everything else in this library locks, so a scan cannot reach the internet and
two builds present identical metadata. A collection must not, and the reason
is the scan order.

Libraries are scanned in the order they were added, which is alphabetical, so
`Box Sets/` is read **before** `Movies/`. At that moment none of its members
exist in the database yet, so every path resolves to nothing — and on 12.0 a
linked child that does not resolve is gone, because `LinkedChildEntity.ChildId`
is a non-nullable Guid and a path has no column to survive in. `LockData` then
makes that permanent: `MetadataService.RefreshMetadata` returns at
`if (item.IsLocked)` before the local providers run, so the file is never read
a second time and the collection stays empty forever.

Measured on 12.0, on a fresh database: zero `LinkedChildren` rows for every
collection here, while a probe collection created *after* the movies were in
the database got its rows immediately. Same file, same paths — only the order
differed.

Without `LockData` the provider re-runs, and `provision` asks for one
`FullRefresh` of the boxsets libraries once the scan is done. That pass finds
the movies and writes the links. `FullRefresh` and not the default, because
`BaseXmlProvider.HasChanged` compares the file's mtime against
`item.DateLastSaved` and would otherwise skip a file that has not changed —
and it has not; what changed is the rest of the library.

Nothing is lost by leaving it out. `lockdata` keeps remote providers away, and
for a BoxSet they are already gone twice over: the per-library `TypeOptions`
and the server-wide `MetadataOptions` both list `BoxSet` with an empty
`MetadataFetchers`. This is the same argument that applies to Books, which
cannot be locked at all.

## `<DisplayOrder>` is written and can never take effect

Kept anyway, and worth knowing about rather than quietly dropping.
`BaseItemXmlParser` has a `case` for it and sets it on the parsed item, and
`BaseXmlSaver` writes it back out — but `MetadataService.MergeDisplayOrder`
only copies the value when `replaceData || string.IsNullOrEmpty(target.
DisplayOrder)`, and `BoxSet`'s constructor sets `DisplayOrder =
"PremiereDate"`. The target is therefore never empty, the first merge runs
with `replaceData: false`, and the parsed value is discarded before anything
else sees it. Identical code in `v10.11.0` and on `master`; measured as
`PremiereDate` on a collection whose file asks for `SortName`.

So a collection sorts by premiere date whatever this says, on every server
this library targets. The field stays because the parser reads it, which is
the bar the rest of this module is written to, and because the day the merge
is fixed the fixture starts working and says so.

## The other half of the coverage is `API_COLLECTIONS`, and it is not a spare

A `collection.xml` and `POST /Collections` produce the same kind of item by
two mechanisms that behave differently, and the difference is not cosmetic:

| | `collection.xml` | `POST /Collections` |
| --- | --- | --- |
| members stored | in memory only | `LinkedChildren` rows |
| after a restart | **empty** | intact |
| `ChildCount` | 0, while a children query returns them | correct |
| lives in | the library, in git, rebuildable | the server's data directory |

Measured on 12.0. A freshly scanned server answers
`GET /Items?parentId=<collection>` with all three members of `The Linked
Collection`; restart it without rescanning and the same query answers zero,
while the API-made ones and the auto-made one keep theirs. `ChildCount` is
read off the database, so it says 0 the whole time — which is why the first
measurement of this said the fixtures were broken when they were not.

So both are shipped, with names that say which is which. The XML ones are
what a library on disk gives you, and they cover the resolver, the parser and
this bug. The API ones are what a client's own "new collection" button does,
and they are the ones that are still there tomorrow.

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


# Collections created through the API once the library has been scanned, since
# nothing on disk can produce a collection whose membership survives a restart.
# Members are manifest keys, resolved to item ids by `provision`.
#
# Names deliberately say "Api" so that a collection which is empty after a
# restart can be told from one that is not, without checking where it came
# from.
API_COLLECTIONS = [
    {
        "key": "api-collection",
        "name": "Api Made Collection",
        "members": ["movie-loose-file", "movie-folder-mismatch",
                    "movie-unicode-title"],
        "why": "The same three films as The Linked Collection, by the other "
               "mechanism. Side by side the two answer one question: whether "
               "what a client is looking at survives a server restart.",
    },
    {
        "key": "api-collection-mixed",
        "name": "Api Mixed Types",
        "members": ["standard-show", "movie-strm-loose"],
        "why": "A series and a .strm movie. A linked child can be any item, "
               "and the shortcut has no runtime until playback is asked for, "
               "so a collection can hold a row that cannot be laid out from "
               "its own metadata.",
    },
    {
        "key": "api-collection-versions",
        "name": "Api Multi Version Member",
        "members": ["movie-multi-version", "movie-multi-version-editions"],
        "why": "Both multi-version films, added by id rather than by path. "
               "CollectionPostScanTask refuses items with a PrimaryVersionId "
               "on 12.0, and this route does not — so the same two films are "
               "in a collection here and refused by the automatic one.",
    },
]


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
