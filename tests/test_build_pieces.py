"""NFO output, the catalog's licence rules, and ffmpeg command assembly.

None of these run ffmpeg. The command-assembly tests exist because the
arguments are where the subtle breakage lives — an option in the wrong place
fails with an error that names the wrong thing entirely.
"""

import os
import pathlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from stdjflib import catalog, config, fetch, generate, nfo, recipes


class TestNfo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _parse(self, path):
        return ET.parse(path).getroot()

    def test_movie_locks_metadata(self):
        """Without lockdata Jellyfin refreshes from the internet on scan."""
        path = os.path.join(self.dir, "movie.nfo")
        nfo.movie(path, key="k", title="T", plot="P", year=2020,
                  runtime_minutes=10)
        root = self._parse(path)
        self.assertEqual(root.tag, "movie")
        self.assertEqual(root.findtext("lockdata"), "true")
        self.assertEqual(root.findtext("title"), "T")
        self.assertEqual(root.findtext("year"), "2020")

    def test_unique_id_uses_a_namespace_nothing_resolves(self):
        path = os.path.join(self.dir, "movie.nfo")
        nfo.movie(path, key="k", title="T", plot="P", year=2020,
                  runtime_minutes=10)
        node = self._parse(path).find("uniqueid")
        self.assertEqual(node.get("type"), "stdjflib")
        self.assertNotIn(node.get("type"), ("tmdb", "imdb", "tvdb"))

    def test_output_is_deterministic(self):
        """Two builds must produce byte-identical metadata."""
        a = os.path.join(self.dir, "a.nfo")
        b = os.path.join(self.dir, "b.nfo")
        for path in (a, b):
            nfo.movie(path, key="same-key", title="T", plot="Plot.", year=2020,
                      runtime_minutes=10)
        self.assertEqual(pathlib.Path(a).read_bytes(), pathlib.Path(b).read_bytes())

    def test_different_keys_give_different_details(self):
        a = os.path.join(self.dir, "a.nfo")
        b = os.path.join(self.dir, "b.nfo")
        nfo.movie(a, key="key-one", title="T", plot="P", year=2020,
                  runtime_minutes=10)
        nfo.movie(b, key="key-two", title="T", plot="P", year=2020,
                  runtime_minutes=10)
        self.assertNotEqual(pathlib.Path(a).read_text(), pathlib.Path(b).read_text())

    def test_episode_span_is_recorded(self):
        path = os.path.join(self.dir, "ep.nfo")
        nfo.episode(path, key="k", title="T", plot="P", season_no=1,
                    episode_no=3, aired="2020-01-01", runtime_minutes=22,
                    end_episode=4)
        root = self._parse(path)
        self.assertEqual(root.findtext("episode"), "3")
        self.assertEqual(root.findtext("episodenumberend"), "4")

    def test_titles_with_markup_are_escaped(self):
        path = os.path.join(self.dir, "x.nfo")
        nfo.movie(path, key="k", title="A & B <tag>", plot="P", year=2020,
                  runtime_minutes=1)
        self.assertEqual(self._parse(path).findtext("title"), "A & B <tag>")


class TestCatalogLicensing(unittest.TestCase):
    def test_every_source_declares_an_allowed_licence(self):
        for src in catalog.all_sources():
            with self.subTest(src.key):
                self.assertIn(src.licence, fetch.ALLOWED_LICENCES)

    def test_every_source_has_attribution(self):
        """CC-BY requires it, and it is the only record of provenance."""
        for src in catalog.all_sources():
            with self.subTest(src.key):
                self.assertTrue(src.attribution.strip())

    def test_minimal_tier_downloads_nothing(self):
        self.assertEqual(catalog.for_tier("minimal"), [])
        self.assertEqual(catalog.estimated_bytes("minimal"), 0)

    def test_tiers_nest(self):
        standard = {s.key for s in catalog.for_tier("standard")}
        full = {s.key for s in catalog.for_tier("full")}
        self.assertLess(standard, full)

    def test_archive_licence_matcher(self):
        ok = ["http://creativecommons.org/licenses/publicdomain/",
              "https://creativecommons.org/publicdomain/zero/1.0/",
              "http://creativecommons.org/licenses/by/4.0/"]
        bad = [None, "", "all rights reserved",
               "http://creativecommons.org/licenses/by-nc-nd/4.0/"]
        for value in ok:
            self.assertTrue(fetch.archive_ok(value), value)
        for value in bad:
            self.assertFalse(fetch.archive_ok(value), value)

    def test_noncommercial_licences_are_refused(self):
        """NC and ND cannot be redistributed freely; they must not slip in."""
        for bad in ("CC-BY-NC-4.0", "CC-BY-ND-4.0", "proprietary"):
            self.assertNotIn(bad, fetch.ALLOWED_LICENCES)


class TestCommandAssembly(unittest.TestCase):
    def _cmd(self, key, **kw):
        rec = {r.key: r for r in recipes.all_recipes()}[key]
        workdir = tempfile.mkdtemp()
        object.__setattr__(rec, "_embedded", [])
        try:
            return generate.build_command(rec, "/tmp/out." + rec.container,
                                          workdir, **kw)
        finally:
            object.__setattr__(rec, "_embedded", [])

    def test_muxer_is_named_explicitly(self):
        argv = self._cmd("v-h264-main")
        self.assertIn("-f", argv)
        self.assertIn("matroska", argv)

    def test_attach_comes_after_every_input(self):
        """`-attach` before an `-i` fails, blaming the input."""
        # A real file, because build_command only attaches one that exists.
        fd, font = tempfile.mkstemp(suffix=".ttf")
        os.write(fd, b"not really a font")
        os.close(fd)
        try:
            argv = self._cmd("s-ass-attached-font", font_file=font)
        finally:
            os.unlink(font)
        self.assertIn("-attach", argv)
        attach_at = argv.index("-attach")
        last_input = max(i for i, a in enumerate(argv) if a == "-i")
        self.assertGreater(attach_at, last_input)

    def test_faststart_on_mp4(self):
        self.assertIn("-movflags", self._cmd("c-mp4"))

    def test_experimental_codecs_get_strict_flag(self):
        argv = self._cmd("a-dts-51")
        self.assertIn("-strict", argv)

    def test_hardware_override_replaces_the_encoder(self):
        argv = self._cmd("v-h264-main", video_encoder="h264_nvenc")
        self.assertIn("h264_nvenc", argv)
        self.assertNotIn("libx264", argv)
        # x264-only options must not survive the swap.
        self.assertNotIn("-x264opts", argv)

    def test_audio_metadata_is_per_track(self):
        argv = self._cmd("x-many-audio")
        joined = " ".join(argv)
        self.assertIn("language=jpn", joined)
        self.assertIn("-disposition:a:2", joined)

    def test_duration_is_bounded(self):
        argv = self._cmd("v-h264-main")
        self.assertIn("-t", argv)


class TestConfig(unittest.TestCase):
    def test_rejects_unknown_tier(self):
        with self.assertRaises(ValueError):
            config.BuildConfig(root="/tmp", tier="enormous")

    def test_rejects_unknown_hwaccel(self):
        with self.assertRaises(ValueError):
            config.BuildConfig(root="/tmp", hwaccel="magic")

    def test_hw_encoder_is_off_unless_asked(self):
        cfg = config.BuildConfig(root="/tmp")
        self.assertIsNone(cfg.hw_encoder("libx264"))
        cfg = config.BuildConfig(root="/tmp", hwaccel="nvenc")
        self.assertEqual(cfg.hw_encoder("libx264"), "h264_nvenc")
        # No hardware equivalent for these, so they stay on software.
        self.assertIsNone(cfg.hw_encoder("mpeg2video"))

    def test_only_filters_libraries(self):
        cfg = config.BuildConfig(root="/tmp", only=("Movies",))
        self.assertTrue(cfg.wants("Movies"))
        self.assertFalse(cfg.wants("Music"))
        self.assertTrue(config.BuildConfig(root="/tmp").wants("Music"))

    def test_tier_inclusion(self):
        self.assertTrue(config.tier_includes("full", "minimal"))
        self.assertFalse(config.tier_includes("minimal", "full"))

    def test_runtime_dir_is_off_the_library(self):
        """Server state must not land on the library's mount."""
        # gettempdir() caches, so the env var is not what to patch here.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tempfile, "tempdir", tmp):
                path = config.runtime_dir("/mnt/sshfs/qa", "jellyfin")
        self.assertTrue(path.startswith(tmp + os.sep), path)
        self.assertTrue(path.endswith(os.sep + "jellyfin"), path)

    def test_runtime_dir_is_stable_and_per_library(self):
        """Stable so a second serve reuses the build; distinct so two
        libraries never share one server database."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tempfile, "tempdir", tmp):
                one = config.runtime_dir("/srv/qa", "jellyfin")
                again = config.runtime_dir("/srv/qa/", "jellyfin")
                other = config.runtime_dir("/srv/qa-two", "jellyfin")
        self.assertEqual(one, again)
        self.assertNotEqual(one, other)

    def test_temp_path_keeps_the_extension(self):
        """ffmpeg picks its muxer from the extension when -f is absent."""
        from stdjflib import ff

        tmp = ff.temp_path("/a/b/album.flac")
        self.assertTrue(tmp.endswith(".flac"))
        self.assertEqual(os.path.dirname(tmp), "/a/b")


if __name__ == "__main__":
    unittest.main()


class TestDownloadRetry(unittest.TestCase):
    """The short-read path, which a real 1.1 GB download hit on the first run."""

    def setUp(self):
        from stdjflib import fetch

        self.fetch = fetch
        self.dir = tempfile.mkdtemp()
        self.dest = os.path.join(self.dir, "file.bin")
        self._sleep = fetch.time.sleep
        fetch.time.sleep = lambda _s: None  # do not actually back off

    def tearDown(self):
        self.fetch.time.sleep = self._sleep

    def test_resumes_across_attempts_and_converges(self):
        """Each attempt writes a bit more; the file must end up whole."""
        payload = b"x" * 1000
        calls = []

        def flaky(url, dest, *, progress=None):
            have = (os.path.getsize(dest + ".part")
                    if os.path.exists(dest + ".part") else 0)
            calls.append(have)
            with open(dest + ".part", "ab") as fh:
                fh.write(payload[have:have + 300])
            got = os.path.getsize(dest + ".part")
            if got < len(payload):
                raise IOError("short read")
            os.replace(dest + ".part", dest)
            return dest

        self.fetch._download_once = flaky
        try:
            self.fetch.download("http://example/x", self.dest)
        finally:
            del self.fetch._download_once
            from stdjflib import fetch as _f
            import importlib
            importlib.reload(_f)

        self.assertEqual(pathlib.Path(self.dest).read_bytes(), payload)
        # Resumed from where it left off rather than restarting each time.
        self.assertEqual(calls, [0, 300, 600, 900])

    def test_gives_up_after_the_attempt_limit(self):
        from stdjflib import fetch

        fetch.time.sleep = lambda _s: None
        original = fetch._download_once
        fetch._download_once = lambda *a, **k: (_ for _ in ()).throw(
            IOError("always fails"))
        try:
            with self.assertRaises(IOError) as ctx:
                fetch.download("http://example/x", self.dest, attempts=3)
            self.assertIn("gave up after 3 attempts", str(ctx.exception))
        finally:
            fetch._download_once = original


class TestBulk(unittest.TestCase):
    """Naming and scaling for the bulk libraries."""

    def test_names_are_unique_across_a_large_run(self):
        """Duplicate titles collide as paths and ruin sort testing."""
        from stdjflib import libraries

        names = [libraries.bulk_name(i) for i in range(5000)]
        self.assertEqual(len(names), len(set(names)))

    def test_names_are_stable(self):
        from stdjflib import libraries

        self.assertEqual(libraries.bulk_name(1234), libraries.bulk_name(1234))

    def test_names_spread_across_the_alphabet(self):
        """Jump-to-letter and sort are only testable if the index is spread."""
        from stdjflib import libraries

        initials = {libraries.bulk_name(i)[0].upper() for i in range(1000)}
        letters = {c for c in initials if c.isalpha() and c.isascii()}
        self.assertGreaterEqual(len(letters), 20)

    def test_awkward_titles_appear_regularly(self):
        """Non-Latin and overlong titles must land mid-list, not just at edges."""
        from stdjflib import libraries

        awkward = [i for i in range(1000)
                   if not libraries.bulk_name(i).isascii()
                   or len(libraries.bulk_name(i)) > 60]
        self.assertGreater(len(awkward), 15)
        self.assertLess(max(awkward) - min(awkward), 1000)

    def test_years_span_a_useful_range(self):
        from stdjflib import libraries

        years = {libraries.bulk_year(i) for i in range(500)}
        self.assertLessEqual(min(years), 1955)
        self.assertGreaterEqual(max(years), 2020)

    def test_safe_name_strips_path_hostile_characters(self):
        from stdjflib import libraries

        got = libraries.safe_name('a/b:c\\d?e*f"g<h>i|j')
        for bad in '/\\:?*"<>|':
            self.assertNotIn(bad, got)

    def test_safe_name_keeps_unicode(self):
        from stdjflib import libraries

        self.assertIn("日本語", libraries.safe_name("日本語のタイトル"))

    def test_bulk_defaults_to_full_tier_only(self):
        from stdjflib.cli import resolve_bulk

        self.assertEqual(resolve_bulk(None, "full"), config.DEFAULT_BULK)
        self.assertEqual(resolve_bulk(None, "standard"), 0)
        self.assertEqual(resolve_bulk(None, "minimal"), 0)

    def test_explicit_bulk_overrides_the_tier_default(self):
        from stdjflib.cli import resolve_bulk

        self.assertEqual(resolve_bulk(0, "full"), 0)        # opt out of full
        self.assertEqual(resolve_bulk(50, "minimal"), 50)   # opt in below full
        self.assertEqual(resolve_bulk(-5, "full"), 0)       # never negative

    def test_libraries_listing_includes_bulk_only_when_enabled(self):
        plain = config.BuildConfig(root="/tmp", tier="full")
        self.assertNotIn("Bulk Movies", plain.libraries())
        withbulk = config.BuildConfig(root="/tmp", tier="full", bulk=10)
        self.assertIn("Bulk Movies", withbulk.libraries())
        self.assertIn("Movies", withbulk.libraries())

    def test_every_bulk_library_has_a_builder(self):
        from stdjflib import libraries

        named = {name for name, _ in libraries.BULK_BUILDERS}
        self.assertEqual(named, set(config.BULK_LIBRARIES))


class TestPartialBuildManifest(unittest.TestCase):
    """A `--only` build must not forget the libraries it did not rebuild."""

    def setUp(self):
        from stdjflib import build, config as cfgmod

        self.build = build
        self.cfgmod = cfgmod
        self.dir = tempfile.mkdtemp()

    def _write(self, items, libraries):
        cfg = self.cfgmod.BuildConfig(root=self.dir)
        self.build.write_manifest(cfg, {"items": items, "libraries": libraries})

    def test_untouched_libraries_are_carried_forward(self):
        self._write(
            [{"library": "Movies", "key": "a", "path": "/x/a"},
             {"library": "Shows", "key": "b", "path": "/x/b"},
             {"library": "Bulk Movies", "key": "c", "path": "/x/c"}],
            {"Movies": "movies", "Shows": "tvshows"})
        cfg = self.cfgmod.BuildConfig(root=self.dir, only=("Movies",))
        kept = self.build._carry_forward(cfg, {"Movies"})
        keys = {item["key"] for item in kept}
        self.assertEqual(keys, {"b", "c"})

    def test_rebuilt_library_entries_are_dropped(self):
        """Otherwise a removed item survives as a ghost that verify chases."""
        self._write([{"library": "Movies", "key": "old", "path": "/x/old"}],
                    {"Movies": "movies"})
        cfg = self.cfgmod.BuildConfig(root=self.dir, only=("Movies",))
        kept = self.build._carry_forward(cfg, {"Movies"})
        self.assertEqual(kept, [])

    def test_missing_manifest_is_not_an_error(self):
        cfg = self.cfgmod.BuildConfig(root=os.path.join(self.dir, "nope"))
        self.assertEqual(self.build._carry_forward(cfg, {"Movies"}), [])
        self.assertEqual(self.build._previous(cfg), {})
