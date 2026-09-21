"""Missing and unaired episodes, and the season description they sit under.

A QA library built from generated files has no **virtual** episodes -- the
ones a client draws as "Missing" or "Unaired" -- because only the TMDb provider
creates them and this tool disables everything that talks to the internet. So
`stdjflib.missing` writes them straight into the disposable server's database.

**What these tests pin is the three things that failed silently** when the
route was first spiked: the row, its `AncestorIds`, and its own
`PresentationUniqueKey`. Getting any of them wrong produces a row that
`/Items?isMissing=true` can see and `/Shows/{id}/Episodes` cannot -- the same
symptom for all three, which is why fixing one and still seeing nothing reads
as "this route does not work".

The schema here is a stand-in with the columns the module names. It was
checked against a real `jellyfin.db` before it was written: the module run
against a `VACUUM INTO` copy of a live 12.0 server produced two rows with five
ancestor rows each, their own keys, a NULL path and the server's own `SortName`
shape. That is the evidence this file cannot carry, and the reason
`missing.check_schema` exists is that nothing here would notice the schema
moving underneath it.
"""

import datetime
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from stdjflib import missing, nfo

#: The columns `missing` reads or writes, plus two it only copies, in a table
#: shaped like the server's. Not the real DDL: the real one is ~75 columns of
#: which this module names thirteen, and a copy of it here would rot without
#: anybody noticing. `check_schema` is what catches the real one moving.
SCHEMA = """
CREATE TABLE BaseItems (
    Id TEXT NOT NULL PRIMARY KEY,
    Type TEXT,
    Name TEXT,
    SortName TEXT,
    Path TEXT,
    IsVirtualItem INTEGER,
    PresentationUniqueKey TEXT,
    IndexNumber INTEGER,
    ParentIndexNumber INTEGER,
    PremiereDate TEXT,
    SeasonId TEXT,
    SeriesId TEXT,
    SeriesPresentationUniqueKey TEXT,
    DateCreated TEXT,
    TopParentId TEXT
);
CREATE TABLE AncestorIds (
    ItemId TEXT NOT NULL,
    ParentItemId TEXT NOT NULL,
    PRIMARY KEY (ItemId, ParentItemId)
);
"""

EPISODE_TYPE = "MediaBrowser.Controller.Entities.TV.Episode"


def _library(path, seasons=(("season-1", 2),)):
    """A database with real episodes in it, and their ancestry."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for season_id, count in seasons:
        for number in range(1, count + 1):
            item_id = "%s-ep%d" % (season_id, number)
            conn.execute(
                "INSERT INTO BaseItems (Id, Type, Name, SortName, Path,"
                " IsVirtualItem, PresentationUniqueKey, IndexNumber,"
                " ParentIndexNumber, PremiereDate, SeasonId, SeriesId,"
                " SeriesPresentationUniqueKey, DateCreated, TopParentId)"
                " VALUES (?,?,?,?,?,0,?,?,1,?,?,?,?,?,?)",
                (item_id, EPISODE_TYPE, "Episode %d" % number,
                 "001 - %04d - Episode %d" % (number, number),
                 "/media/%s.mkv" % item_id,
                 missing.presentation_key(item_id), number,
                 "2001-01-01 00:00:00", season_id, "series-1", "serieskey",
                 "2001-01-01 00:00:00", "top-1"))
            # Five, as a real episode has: library, series, season and the
            # two folders above them. The count is not the point; having
            # some, and the virtual one having the SAME ones, is.
            for parent in ("lib-1", "series-1", season_id, "folder-1",
                           "top-1"):
                conn.execute("INSERT INTO AncestorIds (ItemId, ParentItemId)"
                             " VALUES (?,?)", (item_id, parent))
    conn.commit()
    conn.close()
    return path


class MissingEpisodeInjectionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = _library(os.path.join(self.dir, "jellyfin.db"))

    def _rows(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM BaseItems WHERE IsVirtualItem = 1"
            " ORDER BY IndexNumber").fetchall()

    def test_it_makes_one_of_each(self):
        """A pair, because the pair is the point: "we do not have this" and
        "this has not aired yet" differ only by the air date, and a client has
        to tell them apart."""
        missing.inject(self.db)

        names = [row["Name"] for row in self._rows()]
        self.assertEqual(["Missing Episode", "Unaired Episode"], names)

    def test_the_unaired_one_is_in_the_future(self):
        now = datetime.datetime(2020, 6, 1)

        missing.inject(self.db, now=now)

        dates = {row["Name"]: row["PremiereDate"] for row in self._rows()}
        self.assertLess(dates["Missing Episode"], "2020-06-01")
        self.assertGreater(dates["Unaired Episode"], "2020-06-01")

    def test_it_has_no_file(self):
        missing.inject(self.db)

        self.assertEqual([None, None], [row["Path"] for row in self._rows()])

    def test_it_carries_the_templates_ancestry(self):
        """The first thing that failed silently. A raw insert has no
        `AncestorIds` rows, the season query filters on ancestry, and the
        symptom is a row `/Items?isMissing=true` can see and
        `/Shows/{id}/Episodes` cannot."""
        missing.inject(self.db)
        conn = sqlite3.connect(self.db)

        for row in self._rows():
            got = {r[0] for r in conn.execute(
                "SELECT ParentItemId FROM AncestorIds WHERE ItemId = ?",
                (row["Id"],))}
            self.assertEqual(
                {"lib-1", "series-1", "season-1", "folder-1", "top-1"}, got,
                "%s does not hang where its siblings do" % row["Name"])

    def test_each_one_has_its_own_presentation_key(self):
        """The second, and it has the SAME symptom as the first -- which is
        why they are indistinguishable until both are right. A copied key
        makes the server treat the rows as one item and drop the rest."""
        missing.inject(self.db)
        rows = self._rows()
        template_key = missing.presentation_key("season-1-ep1")

        keys = [row["PresentationUniqueKey"] for row in rows]
        self.assertEqual(len(set(keys)), len(keys), "the keys collide")
        self.assertNotIn(template_key, keys, "copied the template's key")
        for row in rows:
            self.assertEqual(missing.presentation_key(row["Id"]),
                             row["PresentationUniqueKey"])

    def test_it_sorts_where_its_number_says(self):
        """`001 - 0097 - Name`, the shape the server writes for a real one.
        Without it the episode sorts as though it had no number."""
        missing.inject(self.db)

        self.assertEqual(
            ["001 - 0097 - Missing Episode", "001 - 0098 - Unaired Episode"],
            [row["SortName"] for row in self._rows()])

    def test_running_it_twice_changes_nothing(self):
        """A re-provision of an existing server must not accumulate them."""
        first = missing.inject(self.db)

        second = missing.inject(self.db)

        self.assertEqual([], second["created"])
        self.assertEqual(first["episodes"], second["episodes"])
        self.assertEqual(2, len(self._rows()))

    def test_it_picks_the_same_season_every_time(self):
        """Deterministic, so a test written against the fixture survives a
        rebuild."""
        db2 = _library(os.path.join(self.dir, "second.db"),
                       seasons=(("season-2", 2), ("season-1", 3)))

        self.assertEqual("season-1", missing.inject(self.db)["season_id"])
        self.assertEqual("season-1", missing.inject(db2)["season_id"])

    def test_a_library_with_no_episodes_is_not_an_error(self):
        """A books-only build. Nothing to hang one off is a report, not a
        failure -- the fixture is a nicety and the server is fine without it."""
        empty = os.path.join(self.dir, "empty.db")
        conn = sqlite3.connect(empty)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

        result = missing.inject(empty)

        self.assertIsNone(result["season_id"])
        self.assertEqual([], result["created"])


class SchemaGuardTest(unittest.TestCase):
    """The one thing this module is really coupled to.

    One code path serves 10.11 and 12.0 alike, because both are on the EF Core
    schema. A future migration that renames a column would otherwise leave this
    inserting rows the server ignores -- silently, which is the failure this
    whole fixture exists to make visible.
    """

    def test_a_moved_column_is_loud(self):
        path = os.path.join(tempfile.mkdtemp(), "moved.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            SCHEMA.replace("IsVirtualItem INTEGER", "IsVirtual INTEGER"))
        conn.commit()
        conn.close()

        with self.assertRaises(missing.SchemaMismatch) as caught:
            missing.inject(path)

        self.assertIn("IsVirtualItem", str(caught.exception))

    def test_something_that_is_not_a_jellyfin_database_is_loud(self):
        path = os.path.join(tempfile.mkdtemp(), "other.db")
        sqlite3.connect(path).execute("CREATE TABLE Junk (x INTEGER)")

        with self.assertRaises(missing.SchemaMismatch):
            missing.inject(path)


class DatabasePathTest(unittest.TestCase):
    """Two run modes put the database in two places: `serve --on-host` passes
    `--datadir <state>/data`, and the container mounts `<state>/config`."""

    def setUp(self):
        self.state = tempfile.mkdtemp()

    def _make(self, relative):
        path = os.path.join(self.state, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()
        return path

    def test_the_on_host_layout(self):
        path = self._make(os.path.join("data", "jellyfin.db"))

        self.assertEqual(path, missing.database_path(self.state))

    def test_the_container_layout(self):
        path = self._make(os.path.join("config", "data", "jellyfin.db"))

        self.assertEqual(path, missing.database_path(self.state))

    def test_the_serve_layout_is_one_deeper(self):
        """`serve` passes `--datadir <state>/data` and Jellyfin puts its own
        `data` directory under that, so the database is at
        `<state>/data/data/jellyfin.db`."""
        path = self._make(os.path.join("data", "data", "jellyfin.db"))

        self.assertEqual(path, missing.database_path(self.state))

    def _real(self, relative):
        path = self._make(relative)
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        return path

    def test_an_empty_decoy_beside_it_does_not_win(self):
        """**Measured on a live 12.0 `serve` state:** it has an EMPTY
        `data/jellyfin.db` as well as the real one two directories down, and
        taking the first path that exists picked the empty one -- whose missing
        `BaseItems` then read as "the schema moved", a wrong-file error wearing
        a schema-drift message on the one route this module has.

        What fixes *that* case is the candidate ORDER, and this test would pass
        on the order alone."""
        decoy = self._make(os.path.join("data", "jellyfin.db"))
        real = self._real(os.path.join("data", "data", "jellyfin.db"))

        self.assertEqual(real, missing.database_path(self.state))
        self.assertFalse(missing.is_items_database(decoy))

    def test_a_decoy_the_order_does_not_save_us_from(self):
        """The test the one above cannot be: with the decoy at the *earlier*
        candidate, only reading the contents finds the real database.

        Written after the mutation for the check above survived -- it passed
        with the content probe removed, because the order already answered it.
        A future layout puts a file where this one puts the decoy, and order is
        then the wrong instrument."""
        decoy = self._make(os.path.join("decoy", "jellyfin.db"))
        real = self._real(os.path.join("real", "jellyfin.db"))
        with mock.patch.object(missing, "DB_CANDIDATES",
                               (os.path.join("decoy", "jellyfin.db"),
                                os.path.join("real", "jellyfin.db"))):
            self.assertEqual(real, missing.database_path(self.state))
        self.assertTrue(os.path.exists(decoy))

    def test_neither_names_a_path_rather_than_nothing(self):
        """So the caller's error message says where it looked."""
        self.assertTrue(
            missing.database_path(self.state).endswith("jellyfin.db"))


class SeasonDescriptionTest(unittest.TestCase):
    """The other half of the same fixture work: a season has to *have* a
    description before a client can be blamed for not drawing one.

    `nfo.season` already writes a `plot` and `libraries.py` already passes one,
    so this is a pin rather than a change -- which is worth having, because the
    thing it protects against is somebody trimming a field nobody asserts on.
    """

    def test_a_season_nfo_carries_a_plot(self):
        import xml.etree.ElementTree as ET

        path = os.path.join(tempfile.mkdtemp(), "season.nfo")
        nfo.season(path, key="k-s1", title="Season 1",
                   plot="Season 1 of A Show.", number=1, year=2020)

        root = ET.parse(path).getroot()
        self.assertEqual("Season 1 of A Show.", root.findtext("plot"))

    def test_the_built_library_passes_one(self):
        """The half the writer cannot promise: `libraries.py` has to hand it
        over, and a plot argument nobody passes is a field that is never
        there."""
        import ast
        import inspect

        from stdjflib import libraries

        tree = ast.parse(inspect.getsource(libraries))
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "season"]
        self.assertTrue(calls, "libraries.py no longer writes a season NFO")
        for call in calls:
            self.assertIn("plot", [kw.arg for kw in call.keywords],
                          "a season NFO is written with no description")


if __name__ == "__main__":
    unittest.main()
