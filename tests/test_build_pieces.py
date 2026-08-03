"""NFO output, the catalog's licence rules, and ffmpeg command assembly.

None of these run ffmpeg. The command-assembly tests exist because the
arguments are where the subtle breakage lives — an option in the wrong place
fails with an error that names the wrong thing entirely.
"""

import os
import pathlib
import re
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


class TestMultiVersionNaming(unittest.TestCase):
    """The version fixtures, against the server's rules spelled out again here.

    `Emby.Naming/Video/VideoListResolver.cs` is the authority, and these
    patterns are copied out of it rather than imported from `libraries.py`,
    for the same reason `verify._ARTWORK_STEMS` restates the artwork mapping:
    a check that derives its expectations from the thing it is checking would
    pass just as happily on a table that was wrong.
    """

    # `IsEligibleForMultiVersion`: strip the folder name off the front, and
    # what is left must be empty or start with `-`, `_`, `.` or a `[tag]`.
    ELIGIBLE = re.compile(r"^(?:$|[-_.]|\[[^]]*\])")
    # `ResolutionRegex`, which is what elects the primary when no file is
    # named exactly like its folder.
    RESOLUTION = re.compile(r"[0-9]{2}[0-9]+[ip]", re.IGNORECASE)

    def _names(self, table, base):
        from stdjflib import libraries

        return [libraries.version_path(base, tag, ext)
                for tag, ext, _video, _audios in table]

    def _tables(self):
        from stdjflib import libraries

        return {
            "MOVIE_VERSIONS": libraries.MOVIE_VERSIONS,
            "MOVIE_EDITIONS": libraries.MOVIE_EDITIONS,
            "EPISODE_VERSIONS": libraries.EPISODE_VERSIONS,
            "EPISODE_EDITIONS": libraries.EPISODE_EDITIONS,
        }

    def test_movie_versions_are_eligible_for_grouping(self):
        """One ineligible filename disqualifies the whole folder, so the
        fixture would silently become three separate films."""
        from stdjflib import libraries

        folder = "Multi Version Movie (2020)"
        for name, table in (("MOVIE_VERSIONS", libraries.MOVIE_VERSIONS),
                            ("MOVIE_EDITIONS", libraries.MOVIE_EDITIONS)):
            for path in self._names(table, folder):
                with self.subTest(name, path=path):
                    stem = os.path.splitext(os.path.basename(path))[0]
                    self.assertTrue(stem.startswith(folder), stem)
                    rest = stem[len(folder):].strip()
                    self.assertRegex(rest, self.ELIGIBLE)

    def test_episode_versions_all_carry_one_episode_number(self):
        """Episodes group on the parsed number, so every version has to agree
        on it — and no two versions may parse to different episodes."""
        from stdjflib import libraries

        base = "Multi Version Show - S01E01 - Pilot"
        for name, table in (("EPISODE_VERSIONS", libraries.EPISODE_VERSIONS),
                            ("EPISODE_EDITIONS", libraries.EPISODE_EDITIONS)):
            keys = {re.search(r"S(\d+)E(\d+)", path).groups()
                    for path in self._names(table, base)}
            self.assertEqual(len(keys), 1, name)

    def test_resolution_tagged_sets_have_exactly_one_winner(self):
        """The highest resolution is the primary, so a tie would make which
        file plays depend on the filename sort instead."""
        from stdjflib import libraries

        for name, table in (("MOVIE_VERSIONS", libraries.MOVIE_VERSIONS),
                            ("EPISODE_VERSIONS", libraries.EPISODE_VERSIONS)):
            found = [self.RESOLUTION.search(tag) for tag, *_ in table]
            self.assertTrue(all(found), f"{name}: a tag names no resolution")
            values = [int(m.group()[:-1]) for m in found]
            self.assertEqual(len(values), len(set(values)), name)
            # Written highest-first, so the table reads as its own answer.
            self.assertEqual(values, sorted(values, reverse=True), name)

    def test_edition_tagged_sets_name_no_resolution(self):
        """Their point is the other primary rule. A resolution in one of these
        tags would quietly hand the decision back to the sort above."""
        from stdjflib import libraries

        for name, table in (("MOVIE_EDITIONS", libraries.MOVIE_EDITIONS),
                            ("EPISODE_EDITIONS", libraries.EPISODE_EDITIONS)):
            for tag, *_ in table:
                self.assertIsNone(self.RESOLUTION.search(tag), f"{name}: {tag}")

    def test_episode_editions_are_written_in_the_order_they_resolve(self):
        """With no resolution anywhere the primary is the first filename in
        sort order, so `Aired` has to come first in the table too."""
        from stdjflib import libraries

        tags = [tag for tag, *_ in libraries.EPISODE_EDITIONS]
        self.assertEqual(tags, sorted(tags))

    def test_versions_differ_in_what_a_client_would_show(self):
        """A version picker listing three identical encodes tests nothing."""
        for name, table in self._tables().items():
            shapes = {(video.encoder, video.width, video.height, ext,
                       tuple((a.encoder, a.channels) for a in audios))
                      for _tag, ext, video, audios in table}
            self.assertEqual(len(shapes), len(table), name)

    def test_version_paths_are_distinct(self):
        for name, table in self._tables().items():
            paths = self._names(table, "Item (2020)")
            self.assertEqual(len(set(paths)), len(table), name)

    def test_every_version_encoder_is_within_its_channel_limit(self):
        """The tables bypass `recipes.py`, so `test_recipes.py` never sees
        them — and `eac3`/`truehd` refusing 7.1 is not discoverable from the
        encoder list."""
        for name, table in self._tables().items():
            for _tag, _ext, _video, audios in table:
                for audio in audios:
                    cap = generate.MAX_CHANNELS.get(audio.encoder)
                    if cap is not None:
                        self.assertLessEqual(audio.channels, cap,
                                             f"{name}: {audio.encoder}")

    def test_both_movie_version_spellings_are_built(self):
        from stdjflib import libraries

        shapes = {shape for _key, _title, shape, _plot
                  in libraries.NAMING_CASES}
        self.assertIn("versions", shapes)
        self.assertIn("editions", shapes)

    def test_a_show_covers_multi_version_episodes(self):
        from stdjflib import libraries

        styles = {show["style"] for show in libraries.SHOWS}
        self.assertIn("versions", styles)


class TestMixedContent(unittest.TestCase):
    """Naming rules `PhotoResolver` enforces silently.

    A photograph that trips either of these is not resolved as a photo and
    also not reported as anything — it simply is not in the library, and the
    folder quietly holds fewer items than it has files.
    """

    # `PhotoResolver._ignoreFiles`, matched with StartsWith against the
    # filename, so a photo called "cover story.jpg" is not a photo.
    IGNORED_PREFIXES = ("folder", "thumb", "landscape", "fanart", "backdrop",
                        "poster", "cover", "logo", "default")

    def _folder(self, entry):
        """The filenames `build_mixed_content` will write for one entry."""
        folder, n_videos, n_photos, _dom, _odd, _note = entry
        label = folder.replace("/", " ") or "Root"
        videos = [f"{label} Clip {i}" for i in range(1, n_videos + 1)]
        photos = [f"{label} Photo {i:02d}" for i in range(1, n_photos + 1)]
        # What the builder writes beside each clip, for two clips in three.
        stills = [f"{name}-thumb" for i, name in enumerate(videos, 1) if i % 3]
        return videos, photos, stills

    def test_no_photo_starts_with_an_ignored_prefix(self):
        from stdjflib import libraries

        for entry in libraries.MIXED_CONTENT:
            _videos, photos, _stills = self._folder(entry)
            for name in photos:
                self.assertFalse(
                    name.lower().startswith(self.IGNORED_PREFIXES),
                    f"{name} would be dropped as artwork, not resolved")

    def test_no_photo_is_mistaken_for_a_video_sidecar(self):
        """`IsOwnedByResolvedMedia` drops any image whose name starts with a
        video's name in the same folder — the rule that makes `<clip>-thumb`
        artwork rather than a photograph, and that would just as happily eat
        a real photograph filed next to a similarly named clip."""
        from stdjflib import libraries

        for entry in libraries.MIXED_CONTENT:
            videos, photos, _stills = self._folder(entry)
            for photo in photos:
                for video in videos:
                    self.assertFalse(
                        photo.lower().startswith(video.lower()),
                        f"{photo} would be read as artwork for {video}")

    def test_every_still_is_owned_by_its_clip(self):
        """The other direction: a sidecar that does *not* match its clip
        becomes a stray photo item in among the videos."""
        from stdjflib import libraries

        for entry in libraries.MIXED_CONTENT:
            videos, _photos, stills = self._folder(entry)
            for still in stills:
                self.assertTrue(
                    any(still.lower().startswith(v.lower()) for v in videos),
                    f"{still} belongs to no clip and would resolve as a photo")

    def test_the_library_covers_all_four_folder_kinds(self):
        from stdjflib import libraries

        kinds = {(bool(v), bool(p))
                 for _f, v, p, _d, _o, _n in libraries.MIXED_CONTENT}
        self.assertIn((True, True), kinds)    # both
        self.assertIn((True, False), kinds)   # video only
        self.assertIn((False, True), kinds)   # photo only

    def test_folders_sit_at_more_than_one_depth(self):
        """A client that handles one level of nesting can still lose its way
        at three, which is the reason for the uneven tree."""
        from stdjflib import libraries

        depths = {entry[0].count("/") + 1 if entry[0] else 0
                  for entry in libraries.MIXED_CONTENT}
        self.assertGreaterEqual(len(depths), 3)
        self.assertIn(0, depths)  # the library root is itself a mixed folder

    def test_every_folder_has_an_odd_shape_out(self):
        """The median-aspect-ratio decision is only visible if something in
        the row disagrees with it."""
        from stdjflib import libraries

        for folder, _v, photos, dominant, odd, _n in libraries.MIXED_CONTENT:
            self.assertIn(dominant, libraries.MIXED_SHAPES, folder)
            self.assertIn(odd, libraries.MIXED_SHAPES, folder)
            if photos > 1:
                self.assertNotEqual(dominant, odd, folder)


class TestSignalHandling(unittest.TestCase):
    """`serve` must clean up on a kill, not only on Ctrl-C.

    Its children run in their own sessions so that stopping them can signal a
    whole process group, which also means nothing else will ever reach them.
    If the `finally` that stops them does not run, a Jellyfin is left holding
    the port and the next run fails on that instead of on the real cause.
    """

    def setUp(self):
        import signal

        from stdjflib import cli

        self.signal = signal
        self.cli = cli
        self.before = {s: signal.getsignal(s)
                       for s in (signal.SIGTERM, signal.SIGHUP)}

    def tearDown(self):
        for signum, old in self.before.items():
            self.signal.signal(signum, old)

    def test_sigterm_unwinds_like_ctrl_c(self):
        """Python's default SIGTERM kills the interpreter outright — no
        exception, so no `finally`, so no cleanup."""
        for name in ("SIGTERM", "SIGHUP"):
            with self.subTest(name):
                signum = getattr(self.signal, name)
                cleaned = []
                with self.assertRaises(KeyboardInterrupt):
                    with self.cli._stop_on_signals():
                        try:
                            os.kill(os.getpid(), signum)
                        finally:
                            cleaned.append(name)
                self.assertEqual(cleaned, [name],
                                 "the finally block did not run")

    def test_a_second_signal_is_not_caught(self):
        """The escape hatch: if the shutdown itself wedges, killing again has
        to work. So the handler puts the default back before it raises.

        Checked from inside the block — on the way out the helper restores
        whatever was there before, which would hide this.
        """
        disposition = "handler never ran"
        with self.cli._stop_on_signals():
            try:
                os.kill(os.getpid(), self.signal.SIGTERM)
            except KeyboardInterrupt:
                disposition = self.signal.getsignal(self.signal.SIGTERM)
        self.assertIs(disposition, self.signal.SIG_DFL)

    def test_handlers_are_put_back_afterwards(self):
        """A long-running command is not the only thing in the process; the
        helper has to leave the disposition as it found it."""
        original = self.signal.getsignal(self.signal.SIGTERM)
        with self.cli._stop_on_signals():
            self.assertNotEqual(self.signal.getsignal(self.signal.SIGTERM),
                                original)
        self.assertEqual(self.signal.getsignal(self.signal.SIGTERM), original)

    def test_every_command_that_starts_a_child_installs_them(self):
        """`serve`, `container` and `provision` all start processes that
        outlive a bare kill. Adding a fourth without this is the way the bug
        comes back."""
        import inspect

        for name in ("cmd_serve", "cmd_container", "cmd_provision"):
            with self.subTest(name):
                src = inspect.getsource(getattr(self.cli, name))
                self.assertIn("_stop_on_signals()", src)


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
