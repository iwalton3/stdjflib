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

import json
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

    def test_lockdata_is_never_set(self):
        """The one metadata file here that must not lock.

        Scan order puts `Box Sets/` before `Movies/`, so on a fresh database
        every member resolves to nothing and 12.0 drops the link for good.
        `LockData` would make that permanent by sending `RefreshMetadata` down
        the `if (item.IsLocked)` early return, so the file could never be read
        a second time. `provision.refresh_collections` is the second pass this
        absence makes possible.
        """
        self.assertNotIn("LockData", self.write()["elements"])

    def test_display_order_is_written_even_though_it_is_discarded(self):
        """`MergeDisplayOrder` never copies it onto a BoxSet.

        It is gated on `replaceData || string.IsNullOrEmpty(target.
        DisplayOrder)` and `BoxSet`'s constructor pre-sets it to
        "PremiereDate", so the target is never empty and the first merge runs
        with `replaceData: false`. Measured as PremiereDate on a collection
        asking for SortName, on both versions.

        The field stays because the parser has a `case` for it and the
        server's own saver writes it, which is the bar this module is written
        to — and because a fixed merge should make the fixture start working
        rather than need rediscovering.
        """
        self.assertEqual(
            self.write(display_order="SortName")["DisplayOrder"], "SortName")

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
    def entries(self, spec) -> list:
        """Everything that ends up in `<CollectionItems>`, in order."""
        return list(spec["members"]) + list(spec.get("unresolvable", ()))

    def test_every_collection_declares_at_least_two_entries(self):
        # One member is a collection that cannot show an ordering, a member
        # count or a collapse rule. It would look built and test nothing.
        for spec in libraries.BOX_SETS:
            self.assertGreaterEqual(len(self.entries(spec)), 2, spec["key"])

    def test_members_name_items_the_build_produces(self):
        built = {f"movie-{key}" for key, *_rest in libraries.NAMING_CASES}
        built |= {key for key, *_rest in libraries.LEGACY_BOX_SET_FILMS}
        built |= {show["key"] for show in libraries.SHOWS}
        for spec in libraries.BOX_SETS:
            for key in spec["members"]:
                self.assertIn(key, built, f"{spec['key']} names {key}")

    def test_a_multi_version_member_is_resolved_through_its_primary(self):
        """The folder is not the item's path, so naming one needs `primary`.

        `build_movies` records the folder as `path` and the primary version's
        file as `primary`; `build._member_paths` prefers the second. A member
        naming a multi-version item without that field would be pointed at the
        folder, which resolves to nothing — silently, and only on a server.
        """
        shapes = {f"movie-{key}": shape
                  for key, _title, shape, _plot in libraries.NAMING_CASES}
        named = {key for spec in libraries.BOX_SETS for key in spec["members"]}
        multi = {key for key in named
                 if shapes.get(key) in ("versions", "editions")}
        self.assertTrue(multi, "no collection covers the multi-version case")

        source = _source()
        movies = source[source.index("def build_movies"):
                        source.index("def _legacy_box_set")]
        self.assertIn('"primary":', movies)
        build = open(
            os.path.join(os.path.dirname(libraries.__file__), "build.py"),
            encoding="utf-8")
        with build as fh:
            self.assertIn('item.get("primary")', fh.read())

    def test_both_of_the_resolver_conditions_are_covered(self):
        # `[boxset]` in the name, or a collection.xml inside. Either alone
        # resolves, so a fixture is needed for each.
        marked = [s for s in libraries.BOX_SETS
                  if boxsets.MARKER in s["folder"]]
        self.assertTrue(marked)
        self.assertEqual(len(libraries.BOX_SETS) - len(marked), 1)

    def test_exactly_one_collection_overrides_its_display_order(self):
        ordered = [s for s in libraries.BOX_SETS if s.get("display_order")]
        self.assertEqual(len(ordered), 1)
        self.assertIn(ordered[0]["display_order"], boxsets.DISPLAY_ORDERS)
        self.assertNotEqual(ordered[0]["display_order"], "PremiereDate",
                            "overriding the default with the default")

    def test_the_ordered_collection_can_tell_the_two_orders_apart(self):
        spec, = [s for s in libraries.BOX_SETS if s.get("display_order")]
        films = {key: (title, year)
                 for key, title, year in libraries.LEGACY_BOX_SET_FILMS}
        members = [films[key] for key in spec["members"]]
        self.assertEqual(len(members), len(spec["members"]),
                         "the ordered collection draws from another table now")
        by_name = [t for t, _y in sorted(members)]
        by_year = [t for t, _y in sorted(members, key=lambda m: m[1])]
        self.assertNotEqual(by_name, by_year)

    def test_exactly_one_collection_has_an_unresolvable_member(self):
        broken = [s for s in libraries.BOX_SETS if s.get("unresolvable")]
        self.assertEqual(len(broken), 1)
        for path in broken[0]["unresolvable"]:
            # Relative like every other member: the only thing wrong with it
            # must be that the file is absent. An absolute one would fail for
            # two reasons and prove neither.
            self.assertFalse(posixpath.isabs(path), path)
        # And it must still have something that does resolve, or "the
        # collection is short an item" is indistinguishable from "the
        # collection is empty".
        self.assertTrue(broken[0]["members"])

    def test_no_set_name_collides_with_a_collection_on_disk(self):
        """`CollectionPostScanTask` adds to an existing box set by name.

        The lookup is `boxSets.FirstOrDefault(b => b.Name == collectionName)`
        over every box set on the server, with no scope of any kind. A `<set>`
        named after one of the collections in `Box Sets/` would quietly pour
        movies into that fixture instead of creating its own.
        """
        on_disk = {spec["title"] for spec in libraries.BOX_SETS}
        on_disk.add("The Legacy Shelf")
        sets = {collection for *_rest, collection
                in libraries.AUTO_COLLECTION_MOVIES if collection}
        self.assertFalse(sets & on_disk)

    def test_keys_are_unique(self):
        keys = [spec["key"] for spec in libraries.BOX_SETS]
        self.assertEqual(len(set(keys)), len(keys))


class TestAutoCollections(unittest.TestCase):
    """The library whose collections the server builds during a scan."""

    def test_it_is_an_ordinary_movies_library(self):
        # Nothing about the folder says "collections". The only difference is
        # one library option and what the NFOs carry.
        self.assertEqual(config.LIBRARIES[config.AUTO_COLLECTION_LIBRARY],
                         "movies")

    def test_a_set_of_two_and_a_set_of_one_are_both_covered(self):
        # `if (movieIds.Count >= 2)` — a set naming one movie creates no
        # collection at all, which is a state a client has to render too.
        counts = {}
        for *_rest, collection in libraries.AUTO_COLLECTION_MOVIES:
            counts[collection] = counts.get(collection, 0) + 1
        sizes = {name: n for name, n in counts.items() if name}
        self.assertIn(2, sizes.values())
        self.assertIn(1, sizes.values())

    def test_one_film_carries_no_set_at_all(self):
        # The control. It must end up in no collection whatsoever.
        self.assertIn(None, [collection for *_rest, collection
                             in libraries.AUTO_COLLECTION_MOVIES])

    def test_only_this_library_switches_the_option_on(self):
        from stdjflib import provision

        self.assertTrue(
            provision.library_options(
                auto_collection=True)["AutomaticallyAddToCollection"])
        self.assertFalse(
            provision.library_options()["AutomaticallyAddToCollection"])

    def test_the_codec_matrix_never_gets_it(self):
        """`Test Media/` has carried a `<set>` per codec group all along.

        `libraries.build_test_media` passes `collection=rec.group`, so every
        matrix movie already names a set. Switching the option on for that
        library would turn the matrix into eight box sets in the server's data
        directory without a line of it being written down as a fixture.
        """
        self.assertNotEqual(config.AUTO_COLLECTION_LIBRARY, "Test Media")
        self.assertIn("collection=rec.group", _source())


# --------------------------------------------------------------------------
# The other mechanism
# --------------------------------------------------------------------------

class TestApiCollections(unittest.TestCase):
    """`POST /Collections` — the route whose membership persists.

    A `collection.xml` is parsed into the item and never written to the
    `LinkedChildren` table, so its members are in memory only: right after a
    scan, gone after a restart, and `ChildCount` reports 0 the whole time.
    Both mechanisms are shipped because that difference is a thing a client
    has to cope with, not a thing to pick a winner from.
    """

    def test_the_two_tables_do_not_share_a_name(self):
        # Side by side is the point. A collection that is empty after a
        # restart has to be tellable from one that is not, on screen, without
        # going and looking at where it came from.
        on_disk = {spec["title"] for spec in libraries.BOX_SETS}
        on_disk.add("The Legacy Shelf")
        by_api = {spec["name"] for spec in boxsets.API_COLLECTIONS}
        self.assertFalse(on_disk & by_api)

    def test_api_names_say_so(self):
        for spec in boxsets.API_COLLECTIONS:
            self.assertTrue(spec["name"].startswith("Api"), spec["name"])

    def test_keys_are_unique_across_both_tables(self):
        keys = ([s["key"] for s in libraries.BOX_SETS]
                + [s["key"] for s in boxsets.API_COLLECTIONS])
        self.assertEqual(len(set(keys)), len(keys))

    def test_members_name_items_the_build_produces(self):
        built = {f"movie-{key}" for key, *_rest in libraries.NAMING_CASES}
        built |= {key for key, *_rest in libraries.LEGACY_BOX_SET_FILMS}
        built |= {show["key"] for show in libraries.SHOWS}
        for spec in boxsets.API_COLLECTIONS:
            for key in spec["members"]:
                self.assertIn(key, built, f"{spec['key']} names {key}")

    def test_one_api_collection_mirrors_an_xml_one(self):
        """The controlled comparison: same films, different mechanism.

        Without a pair that differs *only* in how it was made, "this
        collection is empty" is not attributable to anything.
        """
        xml = {spec["key"]: spec["members"] for spec in libraries.BOX_SETS}
        mirrors = [s for s in boxsets.API_COLLECTIONS
                   if s["members"] == xml.get("boxset-linked")]
        self.assertEqual(len(mirrors), 1)


class TestApiProvisioning(unittest.TestCase):
    def setUp(self):
        from unittest import mock

        self.mock = mock
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = self.dir.name
        os.makedirs(os.path.join(self.root, ".stdjflib"))
        manifest = {"items": [
            {"key": k, "path": os.path.join(self.root, "Movies", f"{k}.mkv")}
            for k in ("movie-loose-file", "movie-folder-mismatch",
                      "movie-unicode-title")]}
        with open(os.path.join(self.root, config.MANIFEST), "w") as fh:
            json.dump(manifest, fh)

    def client(self, existing=()):
        jf = self.mock.Mock()
        jf.get.return_value = {"Id": "user-1"}
        jf.box_sets.return_value = [{"Name": n} for n in existing]
        jf.item_id_at.side_effect = lambda path, uid: "id-" + os.path.basename(path)
        jf.create_collection.return_value = "made"
        return jf

    def test_it_looks_the_members_up_at_the_server_path(self):
        # Inside a container the library is at /media. Asking for a host path
        # finds nothing, and would build an empty collection in silence.
        from stdjflib import provision

        jf = self.client()
        provision.create_api_collections(jf, self.root, "/media",
                                         say=lambda *a: None)
        asked = [c.args[0] for c in jf.item_id_at.call_args_list]
        self.assertTrue(asked)
        for path in asked:
            self.assertTrue(path.startswith("/media/"), path)

    def test_an_existing_collection_is_left_alone(self):
        from stdjflib import provision

        names = [s["name"] for s in boxsets.API_COLLECTIONS]
        jf = self.client(existing=names)
        made = provision.create_api_collections(jf, self.root, self.root,
                                                say=lambda *a: None)
        self.assertEqual(made, {})
        jf.create_collection.assert_not_called()

    def test_a_member_that_resolves_to_nothing_is_named(self):
        from stdjflib import provision

        jf = self.client()
        jf.item_id_at.side_effect = lambda path, uid: None
        said = []
        provision.create_api_collections(jf, self.root, self.root,
                                         say=said.append)
        self.assertTrue(any("no item found" in line for line in said))
        # Nothing resolved, so nothing is created — an empty collection would
        # look like a fixture rather than a failure.
        jf.create_collection.assert_not_called()

    def test_no_manifest_is_reported_and_not_a_crash(self):
        from stdjflib import provision

        os.unlink(os.path.join(self.root, config.MANIFEST))
        said = []
        self.assertEqual(
            provision.create_api_collections(self.client(), self.root,
                                             self.root, say=said.append), {})
        self.assertTrue(any("manifest" in line for line in said))

    def test_a_multi_version_member_is_looked_up_by_its_primary(self):
        from stdjflib import provision

        with open(os.path.join(self.root, config.MANIFEST), "w") as fh:
            json.dump({"items": [{"key": "movie-multi-version",
                                  "path": os.path.join(self.root, "Movies", "MV"),
                                  "primary": os.path.join(self.root, "Movies",
                                                          "MV", "primary.mkv")}]},
                      fh)
        jf = self.client()
        provision.create_api_collections(jf, self.root, self.root,
                                         say=lambda *a: None)
        asked = [c.args[0] for c in jf.item_id_at.call_args_list]
        self.assertIn(os.path.join(self.root, "Movies", "MV", "primary.mkv"),
                      asked)


class TestTheRefreshThatFillsThemIn(unittest.TestCase):
    """`refresh_disk_collections` — the pass that makes an on-disk collection
    hold anything at all.

    Libraries are scanned in the order they were added, which is
    alphabetical, so `Box Sets/` is read before `Movies/` and every member
    path resolves to nothing at the moment it is first read. On 12.0 that
    linked child is then gone — `LinkedChildEntity.ChildId` is a non-nullable
    Guid — so a first scan leaves all six collections **empty** and says
    nothing about it. `boxsets.py` described this pass from the beginning;
    nothing was running it, and a `--fresh` server shipped six collections
    that resolve, are named, draw artwork and hold nothing.
    """

    def setUp(self):
        import unittest.mock as mock
        self.mock = mock

    def client(self, folders=None, members=1):
        jf = self.mock.Mock()
        jf.virtual_folders.return_value = folders if folders is not None else [
            {"Name": "Box Sets", "CollectionType": "boxsets", "ItemId": "lib1"},
            {"Name": "Movies", "CollectionType": "movies", "ItemId": "lib2"},
        ]
        jf.get.return_value = {"Id": "user-1", "TotalRecordCount": members}
        jf.box_sets.return_value = [{"Id": "b%d" % i, "Name": spec["title"]}
                                    for i, spec in enumerate(
                                        libraries.BOX_SETS)]
        return jf

    def test_only_the_boxsets_libraries_are_refreshed(self):
        from stdjflib import provision

        jf = self.client()
        provision.refresh_disk_collections(jf, say=lambda *a: None)
        self.assertEqual([c.args[0] for c in jf.refresh_item.call_args_list],
                         ["lib1"])

    def test_it_asks_for_a_full_refresh(self):
        """The default mode compares the file's mtime against
        `item.DateLastSaved` and skips a file that has not changed — and none
        of them have. What changed is the rest of the library."""
        from stdjflib import provision

        jf = self.client()
        provision.refresh_disk_collections(jf, say=lambda *a: None)
        _args, kwargs = jf.refresh_item.call_args
        self.assertEqual(kwargs.get("mode", "FullRefresh"), "FullRefresh")

    def test_it_reports_every_collection_that_came_back(self):
        from stdjflib import provision

        said = []
        count = provision.refresh_disk_collections(self.client(),
                                                   say=said.append)
        self.assertEqual(count, len(libraries.BOX_SETS))
        for spec in libraries.BOX_SETS:
            self.assertTrue(any(spec["title"] in line for line in said),
                            "%s was not reported" % spec["title"])

    def test_a_collection_that_stayed_empty_is_called_out(self):
        """The whole failure mode is silence, so the one thing this must not
        do is finish quietly when the members did not come back."""
        from stdjflib import provision

        jf = self.client(members=0)
        said = []
        with self.mock.patch.object(provision, "REFRESH_TIMEOUT", 0):
            self.assertEqual(
                provision.refresh_disk_collections(jf, say=said.append), 0)
        self.assertTrue(any("still empty" in line for line in said))

    def test_provision_actually_calls_it(self):
        """The bug this pass fixes was not that it was wrong — it did not
        run. `boxsets.py` described it from the first commit and nothing
        called it, so every `--fresh` server shipped empty collections while
        the documentation said otherwise. Source-level, because the caller
        needs a server and this question does not."""
        import inspect

        from stdjflib import provision

        source = inspect.getsource(provision.provision)
        self.assertIn("refresh_disk_collections(", source,
                      "provision() does not run the refresh pass, so the "
                      "on-disk collections come out empty")

    def test_no_boxsets_library_is_not_an_error(self):
        from stdjflib import provision

        jf = self.client(folders=[{"Name": "Movies",
                                   "CollectionType": "movies"}])
        said = []
        self.assertEqual(
            provision.refresh_disk_collections(jf, say=said.append), 0)
        self.assertFalse(jf.refresh_item.called)
        self.assertEqual(said, [])

    def test_a_library_that_will_not_refresh_does_not_stop_provisioning(self):
        from stdjflib import provision

        jf = self.client()
        jf.refresh_item.side_effect = RuntimeError("server said no")
        said = []
        with self.mock.patch.object(provision, "REFRESH_TIMEOUT", 0):
            provision.refresh_disk_collections(jf, say=said.append)
        self.assertTrue(any("server said no" in line for line in said))


if __name__ == "__main__":
    unittest.main()
