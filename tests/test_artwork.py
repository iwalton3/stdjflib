"""The image types, their shapes, and the names Jellyfin looks for.

None of these run ffmpeg. What is being tested is the table: a poster that is
secretly 16:9, or a season poster written under a name the server never looks
at, produces a library that looks right and tests nothing — and neither
failure shows up as an error anywhere.
"""

import unittest

from stdjflib import artwork, libraries, verify


class TestSpecs(unittest.TestCase):
    def test_every_kind_is_fully_described(self):
        for kind, spec in artwork.SPECS.items():
            self.assertIn(kind, artwork.LAYOUT, f"{kind} has no layout")
            self.assertIn(spec.mode, ("opaque", "wordmark", "cutout"))
            self.assertGreater(spec.width, 0)
            self.assertGreater(spec.height, 0)

    def test_the_shape_matches_the_ratio_it_claims(self):
        """The stamp on the image is what a tester reads off it."""
        for kind, spec in artwork.SPECS.items():
            left, right = spec.ratio.split(":")
            claimed = float(left) / float(right)
            self.assertAlmostEqual(
                spec.aspect, claimed, delta=claimed * 0.01,
                msg=f"{kind} is {spec.width}x{spec.height} but says {spec.ratio}")

    def test_the_shapes_jellyfin_actually_expects(self):
        # Spelled out rather than computed: these four numbers are the whole
        # reason this module exists, and a table can be edited by accident.
        self.assertAlmostEqual(artwork.SPECS["poster"].aspect, 2 / 3, places=3)
        self.assertAlmostEqual(artwork.SPECS["square"].aspect, 1.0, places=3)
        self.assertAlmostEqual(artwork.SPECS["backdrop"].aspect, 16 / 9, places=3)
        self.assertAlmostEqual(artwork.SPECS["thumb"].aspect, 16 / 9, places=3)
        # jellyfin-web's banner card is padding-bottom: 18.5%.
        self.assertAlmostEqual(artwork.SPECS["banner"].aspect, 1 / 0.185,
                               delta=0.05)

    def test_transparency_decides_the_extension(self):
        for kind, spec in artwork.SPECS.items():
            self.assertEqual(spec.ext, "png" if spec.transparent else "jpg",
                             f"{kind} has the wrong extension")


class TestNames(unittest.TestCase):
    def test_folder_names_are_the_ones_the_server_reads(self):
        self.assertEqual(artwork.filename("poster"), "poster.jpg")
        self.assertEqual(artwork.filename("logo"), "logo.png")
        self.assertEqual(artwork.filename("banner"), "banner.jpg")
        # Music prefers "folder", and LocalImageProvider prefers "landscape"
        # over "thumb" for ImageType.Thumb.
        self.assertEqual(artwork.filename("square"), "folder.jpg")
        self.assertEqual(artwork.filename("thumb"), "landscape.jpg")

    def test_sidecar_backdrops_are_fanart_not_backdrop(self):
        """`<name>-backdrop` only resolves for an item in its own folder."""
        self.assertEqual(artwork.sidecar_name("Film (2020)", "backdrop"),
                         "Film (2020)-fanart.jpg")

    def test_episode_stills_are_thumb_exactly(self):
        """EpisodeLocalImageProvider has its own list; landscape is not on it."""
        self.assertEqual(artwork.sidecar_name("Show - S01E01", "thumb"),
                         "Show - S01E01-thumb.jpg")

    def test_square_has_no_sidecar_spelling(self):
        with self.assertRaises(ValueError):
            artwork.sidecar_name("Album", "square")

    def test_season_zero_is_specials(self):
        self.assertEqual(artwork.season_name(0, "poster"),
                         "season-specials-poster.jpg")
        self.assertEqual(artwork.season_name(1, "poster"), "season01-poster.jpg")
        self.assertEqual(artwork.season_name(12, "backdrop"),
                         "season12-fanart.jpg")
        # A date-based show numbers its season by year, and the marker keeps
        # every digit rather than being truncated to two.
        self.assertEqual(artwork.season_name(2019, "poster"),
                         "season2019-poster.jpg")

    def test_verify_reads_back_every_name_that_is_written(self):
        """The two tables are written independently; this is what ties them.

        If `artwork.py` starts writing a name `verify.py` does not know, the
        image stops being checked — silently, because an unrecognised file is
        simply not artwork as far as the walk is concerned.
        """
        for kind in artwork.FOLDER_STEM:
            self.assertEqual(verify._artwork_kind(artwork.filename(kind)), kind)
        for kind in artwork.SIDECAR_STEM:
            self.assertEqual(
                verify._artwork_kind(artwork.sidecar_name("An Item", kind)),
                kind)
        for kind in artwork.SEASON_STEM:
            self.assertEqual(verify._artwork_kind(artwork.season_name(3, kind)),
                             kind)

    def test_media_and_metadata_are_not_mistaken_for_artwork(self):
        for name in ("Film (2020).mkv", "movie.nfo", "00007 - A Photo.jpg",
                     "orientation-3-rotated-180.jpg", "format-png.png"):
            self.assertIsNone(verify._artwork_kind(name), name)


class TestSets(unittest.TestCase):
    def test_sets_only_name_real_kinds(self):
        for name, kinds in artwork.SETS.items():
            for kind in kinds:
                self.assertIn(kind, artwork.SPECS, f"{name} wants {kind}")

    def test_music_is_square_not_a_poster(self):
        """The bug this table exists to prevent.

        jellyfin-web renders MusicAlbum, MusicArtist and Playlist in square
        cards, so a 2:3 cover is pillarboxed or cropped in every music view.
        """
        self.assertEqual(artwork.SETS["album"], ("square",))
        self.assertIn("square", artwork.SETS["artist"])
        self.assertNotIn("poster", artwork.SETS["artist"])

    def test_an_episode_gets_a_still_and_a_season_gets_a_poster(self):
        self.assertEqual(artwork.SETS["episode"], ("thumb",))
        self.assertIn("poster", artwork.SETS["season"])
        self.assertNotIn("thumb", artwork.SETS["season"])

    def test_something_carries_every_type(self):
        """One item per library has the lot, so all of them are reachable."""
        for kind in ("poster", "backdrop", "logo", "banner", "thumb", "disc",
                     "art"):
            self.assertIn(kind, artwork.SETS["everything"])


class TestDeterminism(unittest.TestCase):
    def test_palette_is_stable_and_varies(self):
        self.assertEqual(artwork.palette("a-key"), artwork.palette("a-key"))
        self.assertNotEqual(artwork.palette("a-key"), artwork.palette("b-key"))

    def test_logo_styles_are_spread_not_all_one(self):
        """A library of white-on-transparent logos passes on any dark theme."""
        seen = {artwork.logo_style_for(f"item-{i}") for i in range(60)}
        self.assertEqual(seen, set(artwork.LOGO_STYLES))

    def test_photo_shapes_are_mixed(self):
        """A photo row's median has to disagree with some of its members."""
        ratios = {round(w / h, 2) for w, h in libraries.BULK_PHOTO_SHAPES}
        self.assertGreater(len(ratios), 2)
        self.assertTrue(any(w < h for w, h in libraries.BULK_PHOTO_SHAPES),
                        "no portrait photos, so the median is never contested")


if __name__ == "__main__":
    unittest.main()
