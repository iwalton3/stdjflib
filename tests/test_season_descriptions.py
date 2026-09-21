"""Every season has a description, by whichever of the two routes reaches it.

A client that draws a season's description cannot be tested against a library
where no season has one -- and this one did not. Measured against the live 12.0
server: `/Shows/{id}/Seasons` returned no `Overview` for the first show in the
library, because five of the eight shows have a season *folder* with no
`season.nfo` and two have no season folder at all.

**Two routes, and neither covers the other.**

* `libraries._season_nfo` writes the file for every season that has a folder,
  from the one loop every style feeds. That is the right source for a fresh
  scan: it is on disk, offline, and survives a state wipe.
* `provision.apply_season_overviews` writes through the API for the rest -- a
  flat or absolutely-numbered show's season is synthesised from filenames and
  has nowhere to put a file. It is also the only route to a server that has
  **already** scanned the library, because `lockdata` locks the NFO out of a
  re-scan.

Found by the client's own e2e conformance test, which compares the keys the
real server sends against the keys the client's fake promises: the fake had
grown an `Overview` the server never sent.
"""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from stdjflib import libraries, provision


class SeasonNfoTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, season_no, show=None):
        libraries._season_nfo(self.dir, season_no, "show-key", "A Show",
                              show or {"year": 2020})
        return ET.parse(os.path.join(self.dir, "season.nfo")).getroot()

    def test_it_writes_a_plot(self):
        self.assertEqual("Season 2 of A Show.",
                         self._write(2).findtext("plot"))

    def test_season_zero_is_specials_in_both_places(self):
        """The same label rule the artwork beside it uses. A season called
        "Season 0" under a poster saying "Specials" is a fixture that tests the
        wrong thing."""
        root = self._write(0)

        self.assertEqual("Specials", root.findtext("title"))
        self.assertEqual("Specials of A Show.", root.findtext("plot"))

    def test_the_year_advances_with_the_season(self):
        """A library where every season shares one year cannot exercise a
        client that sorts or labels by it."""
        self.assertEqual("2020", self._write(1).findtext("year"))
        self.assertEqual("2022", self._write(3).findtext("year"))

    def test_specials_does_not_go_back_a_year(self):
        """Season 0 would otherwise be 2019 -- a year before the show existed,
        from `year + season_no - 1`."""
        self.assertEqual("2020", self._write(0).findtext("year"))


class EveryStyleIsCoveredTest(unittest.TestCase):
    """The reason the NFO is written from the `season_art` loop and not inside
    one style's branch: written per style, exactly one of eight shows had it.
    """

    def test_the_shows_table_has_styles_with_no_season_folder(self):
        """Which is what makes the API half necessary rather than tidy: these
        styles have nowhere to put a file."""
        styles = {show["style"] for show in libraries.SHOWS}

        self.assertIn("flat", styles)
        self.assertIn("absolute", styles)

    def test_and_styles_that_do_have_one(self):
        """The other half: these get the file, so a fresh scan reads it."""
        styles = {show["style"] for show in libraries.SHOWS}

        self.assertTrue({"seasons", "dated", "gaps"} & styles)


class _FakeJf:
    """A server with seasons, some described and some not.

    Models the field the real one answers with -- `Overview` present and empty
    -- because "absent" and "empty string" are the same thing to the caller and
    a fake that only did one of them would leave the other branch untested.
    """

    def __init__(self, seasons):
        self.seasons = seasons
        self.written = []
        self.queries = []

    def get(self, path, params=None):
        self.queries.append((path, dict(params or {})))
        if path == "/Users/Me":
            return {"Id": "u1"}
        if path == "/Items":
            return {"Items": [dict(s) for s in self.seasons]}
        return {}

    def set_overview(self, item_id, text):
        self.written.append((item_id, text))
        return True


class ApplySeasonOverviewsTest(unittest.TestCase):
    def _jf(self):
        return _FakeJf([
            {"Id": "s1", "Name": "Season 1", "SeriesName": "A Show",
             "Overview": "From its own NFO."},
            {"Id": "s2", "Name": "Season 2", "SeriesName": "A Show",
             "Overview": ""},
            {"Id": "s3", "Name": "Season 1", "SeriesName": "Flat Show"},
        ])

    def test_it_describes_only_the_ones_with_nothing(self):
        """A season that got its description from its NFO keeps the file's
        wording -- otherwise this becomes a second source of truth for one
        field, and the file stops being the thing under test."""
        jf = self._jf()

        provision.apply_season_overviews(jf, say=lambda _m: None)

        self.assertEqual([("s2", "Season 2 of A Show."),
                          ("s3", "Season 1 of Flat Show.")], jf.written)

    def test_it_returns_how_many_it_wrote(self):
        jf = self._jf()

        self.assertEqual(2, provision.apply_season_overviews(
            jf, say=lambda _m: None))

    def test_running_it_twice_writes_nothing_the_second_time(self):
        """Every provision runs it, including against a server already set up.
        Idempotence is the property that makes that safe."""
        jf = self._jf()
        provision.apply_season_overviews(jf, say=lambda _m: None)
        for season in jf.seasons:
            if season["Id"] in dict(jf.written):
                season["Overview"] = dict(jf.written)[season["Id"]]
        jf.written = []

        provision.apply_season_overviews(jf, say=lambda _m: None)

        self.assertEqual([], jf.written)

    def test_it_asks_for_the_field_it_tests(self):
        """`Overview` is not on a Season DTO unless asked for, so a query
        without it would report every season as undescribed and rewrite all of
        them on every provision."""
        jf = self._jf()

        provision.apply_season_overviews(jf, say=lambda _m: None)

        items = [params for path, params in jf.queries if path == "/Items"]
        self.assertTrue(items)
        self.assertIn("Overview", items[0].get("fields", ""))
        self.assertEqual("Season", items[0].get("includeItemTypes"))

    def test_a_server_with_no_seasons_is_not_an_error(self):
        jf = _FakeJf([])

        self.assertEqual(0, provision.apply_season_overviews(
            jf, say=lambda _m: None))
        self.assertEqual([], jf.written)


if __name__ == "__main__":
    unittest.main()
