"""Collections: the XML dialect, the resolver's two conditions, and the split
between the two shapes a box set comes in.

A collection has no media of its own — it is a name and a list of references —
so almost nothing about it is checkable by looking at what was built. Three
kinds of check take the place of probing:

* `collection.xml` is re-read by a parser written here against
  `BaseItemXmlParser`'s `case` list, not by calling `boxsets.read_members`,
  so a field written in the Kodi dialect by mistake fails here rather than
  being silently ignored by a server six minutes into a scan;
* the member paths are resolved exactly as `FileSystem.MakeAbsolutePath`
  resolves them, which is the property that makes the library portable
  between a host path and a container's `/media`;
* the placement rules are asserted over the tables in `libraries.py`, because
  which library a fixture lives in is decided by `MovieResolver`'s valid
  collection types and nothing at build time would notice it drifting.
"""

import os
import posixpath
import tempfile
import unittest
import xml.etree.ElementTree as ET

from stdjflib import boxsets, config, libraries


# --------------------------------------------------------------------------
# The dialect
# --------------------------------------------------------------------------

# The elements `BaseItemXmlParser.FetchDataFromXmlNode` has a `case` for and
# that mean something on a BoxSet. Read out of the server rather than out of
# `boxsets.py`, so writing a field the parser drops is a failure here.
PARSED_FIELDS = {
    "Added", "OriginalTitle", "LocalTitle", "CriticRating", "SortTitle",
    "Overview", "Description", "Language", "CountryCode", "LockedFields",
    "TagLines", "Countries", "ContentRating", "MPAARating", "CustomRating",
    "RunningTime", "AspectRatio", "LockData", "Network", "Director", "Writer",
    "Actors", "GuestStars", "Trailer", "DisplayOrder", "Trailers",
    "ProductionYear", "Rating", "IMDBrating", "BirthDate", "PremiereDate",
    "FirstAired", "DeathDate", "EndDate", "CollectionNumber", "Genres",
    "Tags", "Persons", "Studios", "Shares", "OwnerUserId", "Format3D",
    # BoxSetXmlParser's own case, on top of the base parser's.
    "CollectionItems",
}

# Kodi spellings that mean nothing in this dialect. Each one is a field that
# would look right in a diff against an NFO and do nothing on a server.
KODI_SPELLINGS = {"title", "plot", "outline", "uniqueid", "lockdata", "set",
                  "year", "genre", "tag", "sorttitle", "mpaa"}


def parse(path: str) -> dict:
    """Read a collection.xml the way the server's parser reads it.

    Deliberately not `boxsets.read_members`: this walks the whole document and
    reports every element it saw, so a field that no `case` matches shows up
    as an unparsed element instead of being skipped the way the server skips
    it — silently.
    """
    root = ET.parse(path).getroot()
    out = {"root": root.tag, "elements": [], "members": []}
    for child in root:
        out["elements"].append(child.tag)
        if child.tag == "CollectionItems":
            for entry in child:
                node = entry.find("Path")
                out["members"].append(None if node is None else node.text)
        elif child.tag in ("Genres", "Tags"):
            out[child.tag] = [g.text for g in child]
        else:
            out[child.tag] = child.text
    return out


class TestDialect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = self.dir.name
        self.addCleanup(self.dir.cleanup)

    def write(self, **kw):
        kw.setdefault("title", "A Collection")
        kw.setdefault("plot", "Some prose.")
        kw.setdefault("members", ["../Movies/One.mkv"])
        return parse(boxsets.write(self.folder, **kw))

    def test_root_element_is_Item(self):
        # BaseXmlSaver.GetRootElementName returns "Item". A <collection> root
        # is the Kodi dialect and is read by nothing.
        self.assertEqual(self.write()["root"], "Item")

    def test_every_element_written_is_one_the_parser_reads(self):
        got = self.write(display_order="SortName", tags=["stdjflib"],
                         sort_title="Zzz", year=2020, content_rating="PG",
                         genres=["Drama"])
        unknown = set(got["elements"]) - PARSED_FIELDS
        self.assertFalse(unknown, f"nothing reads {sorted(unknown)}")

    def test_no_kodi_spellings_leak_in(self):
        got = self.write(year=2020, tags=["stdjflib"], genres=["Drama"])
        self.assertFalse(set(got["elements"]) & KODI_SPELLINGS)

    def test_the_name_comes_from_LocalTitle(self):
        # `case "LocalTitle": item.Name = ...`. <title> is not read at all,
        # and a collection with neither is named after its folder.
        got = self.write(title="The Linked Collection")
        self.assertEqual(got["LocalTitle"], "The Linked Collection")
        self.assertNotIn("title", got["elements"])

    def test_lockdata_is_always_set(self):
        self.assertEqual(self.write()["LockData"], "true")

    def test_members_are_written_as_Path_entries(self):
        got = self.write(members=["../Movies/One.mkv", "../Movies/Two.mkv"])
        self.assertEqual(got["members"], ["../Movies/One.mkv",
                                          "../Movies/Two.mkv"])

    def test_display_order_is_omitted_rather_than_written_empty(self):
        # An empty <DisplayOrder> is not the default — `ReadNormalizedString`
        # gives null, the `if` skips it, and the constructor's PremiereDate
        # stands. Writing nothing says the same thing without implying a
        # choice was made.
        self.assertNotIn("DisplayOrder", self.write()["elements"])
        self.assertIn("DisplayOrder",
                      self.write(display_order="SortName")["elements"])

    def test_display_orders_are_ItemSortBy_names(self):
        # Anything Enum.TryParse<ItemSortBy> rejects falls back to
        # PremiereDate, so a typo here is not an error — it is the default,
        # wearing the label of whatever was meant.
        for order in boxsets.DISPLAY_ORDERS:
            self.assertTrue(order[0].isupper(), order)
            self.assertNotIn(" ", order)

    def test_read_members_agrees_with_a_full_parse(self):
        members = ["../Movies/One.mkv", "../a/b/Two (2020).mkv"]
        path = boxsets.write(self.folder, title="T", plot="P", members=members)
        self.assertEqual(boxsets.read_members(path), members)
        self.assertEqual(parse(path)["members"], members)


# --------------------------------------------------------------------------
# Member paths
# --------------------------------------------------------------------------

class TestMemberPaths(unittest.TestCase):
    """`FileSystem.MakeAbsolutePath(ContainingFolderPath, path)`.

    Anything not already rooted is `Path.GetFullPath(Path.Join(folder, path))`
    — identical code in v10.11.0 and on master — so a relative member is
    resolved against the collection's own folder and an absolute one is taken
    as it stands. The second is what bakes a host mount point into a fixture.
    """

    def resolve(self, folder: str, member: str) -> str:
        """What the server would open, given a member path."""
        if posixpath.isabs(member):
            return member
        return os.path.normpath(os.path.join(folder, member))

    def test_a_member_resolves_back_to_the_file_it_was_made_from(self):
        folder = "/srv/qa/Box Sets/The Linked Collection [boxset]"
        target = "/srv/qa/Movies/Loose File Movie (2020).mkv"
        member = boxsets.member_path(folder, target)
        self.assertFalse(posixpath.isabs(member))
        self.assertEqual(self.resolve(folder, member), target)

    def test_the_same_member_resolves_under_a_container_mount(self):
        # The whole reason the paths are relative. `--stream-origin` has to be
        # a build flag because a URL cannot be relative; a member path can.
        member = boxsets.member_path(
            "/srv/qa/Box Sets/C [boxset]", "/srv/qa/Movies/Film.mkv")
        self.assertEqual(self.resolve("/media/Box Sets/C [boxset]", member),
                         "/media/Movies/Film.mkv")

    def test_members_use_forward_slashes(self):
        member = boxsets.member_path("/a/Box Sets/C", "/a/Movies/Sub/F.mkv")
        self.assertNotIn("\\", member)
        self.assertIn("/", member)

    def test_a_member_inside_the_collection_folder_has_no_dotdot(self):
        member = boxsets.member_path("/a/C", "/a/C/Inside.mkv")
        self.assertEqual(member, "Inside.mkv")


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------

def _source() -> str:
    with open(libraries.__file__, encoding="utf-8") as fh:
        return fh.read()


def _legacy_source() -> str:
    """`_legacy_box_set`, as written.

    Read as text because the properties being checked are absences — no
    collection.xml, no Box Sets library — and an absence is not something a
    call to the function would report.
    """
    source = _source()
    return source[source.index("def _legacy_box_set"):
                  source.index("def build_box_sets")]


class TestPlacement(unittest.TestCase):
    """Which library each shape has to live in, and why it is not a preference.

    `MovieResolver._validCollectionTypes` is movies, homevideos, musicvideos,
    tvshows, photos. `IsInvalid` returns true for everything else, so in a
    `boxsets` library no media file resolves to anything at all. A collection
    whose children come from the disk therefore cannot live there, and one
    whose children come from `collection.xml` does not care.
    """

    def test_the_box_sets_library_is_added_as_boxsets(self):
        self.assertEqual(config.LIBRARIES["Box Sets"], "boxsets")

    def test_it_is_not_called_Collections(self):
        # CollectionManager.EnsureLibraryFolder adds a library of its own
        # under the localized name "Collections" the first time anything
        # creates a server-owned collection. Two libraries, one name.
        self.assertNotIn("Collections", config.LIBRARIES)

    def test_the_legacy_shelf_is_in_the_movies_library(self):
        # Not in Box Sets: its children are files, and files in a boxsets
        # library resolve to nothing.
        self.assertIn(boxsets.MARKER, libraries.LEGACY_BOX_SET)
        self.assertIn('"library": "Movies"', _legacy_source())
        self.assertIn("_legacy_box_set(root, cfg)", _source())

    def test_the_legacy_shelf_ships_no_collection_xml(self):
        # `BoxSet.IsLegacyBoxSet` is false as soon as LinkedChildren is
        # non-empty, so writing one here would convert the fixture into the
        # other case and delete the one it covers. Nothing in the movies
        # builder may write that file.
        self.assertNotIn("boxsets.write", _legacy_source())

    def test_the_films_on_the_shelf_have_different_years(self):
        # DisplayOrder defaults to PremiereDate. Same year on both and the
        # fixture cannot show whether a client honoured it.
        years = [year for _key, _title, year in libraries.LEGACY_BOX_SET_FILMS]
        self.assertEqual(len(set(years)), len(years))

    def test_the_shelf_orders_differently_by_year_and_by_name(self):
        # The point of the fixture: a client sorting by name instead of by
        # premiere date is visibly wrong rather than coincidentally right.
        by_year = [t for _k, t, _y in
                   sorted(libraries.LEGACY_BOX_SET_FILMS, key=lambda f: f[2])]
        by_name = sorted(t for _k, t, _y in libraries.LEGACY_BOX_SET_FILMS)
        self.assertNotEqual(by_year, by_name)


class TestBoxSetTable(unittest.TestCase):
    def test_every_collection_declares_at_least_two_members(self):
        # One member is a collection that cannot show an ordering, a member
        # count or a collapse rule. It would look built and test nothing.
        for spec in libraries.BOX_SETS:
            self.assertGreaterEqual(len(spec["members"]), 2, spec["key"])

    def test_members_name_items_that_exist(self):
        built = {f"movie-{key}" for key, *_rest in libraries.NAMING_CASES}
        for spec in libraries.BOX_SETS:
            for key in spec["members"]:
                self.assertIn(key, built, f"{spec['key']} names {key}")

    def test_members_are_not_multi_version_items(self):
        # A multi-version movie's item path is its *primary version's* file,
        # not the folder the manifest records for it, so naming one by its
        # manifest path produces a member that resolves to nothing.
        shapes = {f"movie-{key}": shape
                  for key, _title, shape, _plot in libraries.NAMING_CASES}
        for spec in libraries.BOX_SETS:
            for key in spec["members"]:
                self.assertNotIn(shapes[key], ("versions", "editions"),
                                 f"{spec['key']} names the folder of {key}")

    def test_exactly_one_collection_carries_the_marker(self):
        # Both halves of BoxSetResolver's condition are covered, and each by
        # exactly one fixture: the [boxset] suffix, and collection.xml alone.
        marked = [s for s in libraries.BOX_SETS
                  if boxsets.MARKER in s["folder"]]
        self.assertEqual(len(marked), 1)
        self.assertEqual(len(libraries.BOX_SETS) - len(marked), 1)

    def test_keys_are_unique(self):
        keys = [spec["key"] for spec in libraries.BOX_SETS]
        self.assertEqual(len(set(keys)), len(keys))


if __name__ == "__main__":
    unittest.main()
