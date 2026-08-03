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


class TestStrm(unittest.TestCase):
    """Stream files, against `ProbeProvider.FetchShortcutInfo` restated here.

    The parsing rule is written out again rather than imported from
    `strm.py`, for the same reason `TestMultiVersionNaming` copies out
    `VideoListResolver`'s patterns: a check that asks the implementation what
    the answer is agrees with it however wrong it is.
    """

    # `FetchShortcutInfo`: strip tabs, CR and LF from every line, trim it, and
    # take the first that is neither empty nor a `#` comment.
    @staticmethod
    def reference_target(text):
        for line in text.split("\n"):
            line = line.replace("\t", "").replace("\r", "").strip()
            if line and not line.startswith("#"):
                return line
        return None

    # The four schemes the same method accepts. Anything else is logged as
    # "invalid or non-remote" and dropped.
    SCHEMES = ("http", "https", "rtsp", "rtp")

    def setUp(self):
        from stdjflib import libraries, origin, strm

        self.strm = strm
        self.origin = origin
        self.libraries = libraries
        self.dir = tempfile.mkdtemp()
        self.targets = libraries.strm_targets(config.BuildConfig(root=self.dir))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, url, **kw):
        path = os.path.join(self.dir, name)
        self.strm.write(path, url, **kw)
        return path

    def test_accepted_schemes_match_the_server(self):
        self.assertEqual(tuple(self.strm.SCHEMES), self.SCHEMES)

    def test_a_bare_path_is_not_remote(self):
        """The refusal is the point: honouring a local path would make a
        `.strm` a way to read any file on the server."""
        for line in ("/srv/media/film.mkv", "C:\\media\\film.mkv",
                     "file:///srv/media/film.mkv", "../film.mkv", "film.mkv"):
            with self.subTest(line=line):
                self.assertFalse(self.strm.is_remote(line))

    def test_remote_urls_are_remote(self):
        for scheme in self.SCHEMES:
            with self.subTest(scheme=scheme):
                self.assertTrue(self.strm.is_remote(f"{scheme}://host/path"))
                # Upper case reaches `Uri.Scheme` lowered, so it is accepted.
                self.assertTrue(self.strm.is_remote(f"{scheme.upper()}://host/x"))

    def test_a_scheme_with_nothing_after_it_is_not_remote(self):
        self.assertFalse(self.strm.is_remote("http://"))

    def test_comments_blank_lines_and_indentation_are_skipped(self):
        path = self._write(
            "a.strm", "https://example.invalid/wanted.mp4",
            header=["a comment", "", "https://example.invalid/decoy.mp4"],
            trailing=["", "\thttps://example.invalid/ignored.mp4"])
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(self.strm.first_line(path),
                         "https://example.invalid/wanted.mp4")
        # And the independent reader agrees with the one under test.
        self.assertEqual(self.strm.first_line(path),
                         self.reference_target(text))

    def test_a_tab_indented_url_is_still_the_url(self):
        path = os.path.join(self.dir, "b.strm")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n\n\t  https://example.invalid/x.mp4  \t\n")
        self.assertEqual(self.strm.first_line(path),
                         "https://example.invalid/x.mp4")

    def test_a_file_of_only_comments_yields_nothing(self):
        path = os.path.join(self.dir, "c.strm")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# nothing here\n#\n\n")
        self.assertIsNone(self.strm.first_line(path))
        self.assertIsNone(self.strm.target(path))

    def test_target_refuses_what_the_server_refuses(self):
        path = self._write("d.strm", "/srv/media/film.mkv")
        self.assertEqual(self.strm.first_line(path), "/srv/media/film.mkv")
        self.assertIsNone(self.strm.target(path))

    def test_written_files_end_in_a_newline_with_no_bom(self):
        """`File.ReadAllLines` copes with both, so writing either would be
        testing .NET — and a BOM would make the file differ by platform."""
        path = self._write("e.strm", "https://example.invalid/x.mp4")
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)

    def test_every_playable_target_comes_from_the_catalogue(self):
        """A stream file naming an address of its own would be pointing at
        something the licence gate has never had an opinion about."""
        urls = {src.url for src in catalog.all_sources()}
        for key, source_key in self.libraries.STRM_SOURCES.items():
            with self.subTest(key=key):
                self.assertIn(self.targets[key], urls)
                self.assertEqual(catalog.by_key(source_key).url,
                                 self.targets[key])

    def test_playable_targets_are_streamable_not_archives(self):
        """A `.zip` is a perfectly good catalogue entry and a useless URL to
        hand a player."""
        for key in self.libraries.STRM_SOURCES.values():
            with self.subTest(key=key):
                self.assertFalse(catalog.by_key(key).unzip)
                self.assertFalse(catalog.by_key(key).url.endswith(".zip"))

    def test_the_unplayable_fixtures_reach_no_third_party(self):
        """They exist to test a protocol field and a refusal. Either one
        pointing at a real host would make the library fetch something when
        somebody pressed play on it to see what happened."""
        for key, url in self.libraries.STRM_UNPLAYABLE.items():
            with self.subTest(key=key):
                self.assertFalse(url.startswith(("http://", "https://")))
                if "://" in url:
                    self.assertIn("127.0.0.1", url)

    def test_every_fixture_key_has_a_target_and_the_reverse(self):
        used = {rec_key for rec_key, _t, shape, _p in self.libraries.NAMING_CASES
                if shape.startswith("strm")}
        declared = {k[len("movie-"):] for k in self.targets
                    if k.startswith("movie-")}
        self.assertEqual(used, declared)

    def test_the_strm_version_filename_groups_with_its_local_sibling(self):
        """The alternate is only a version of the film if its name is eligible;
        otherwise it silently becomes a second film with the same poster."""
        folder = "Local And Remote Versions (2020)"
        name = self.libraries.version_path(folder, "Remote Stream", "strm")
        stem = os.path.splitext(name)[0]
        self.assertTrue(stem.startswith(folder))
        self.assertRegex(stem[len(folder):].strip(),
                         TestMultiVersionNaming.ELIGIBLE)

    def test_the_strm_episode_versions_parse_to_one_episode(self):
        """Episode grouping keys on the parsed season and episode number and
        nothing else, which is what lets an `.mkv` and a `.strm` be one
        episode with two sources."""
        base = "Remote Stream Show - S01E03 - Something Happens"
        both = [base + ".mkv",
                self.libraries.version_path(base, "Remote Stream", "strm")]
        keys = {re.search(r"S(\d+)E(\d+)", path).groups() for path in both}
        self.assertEqual(len(keys), 1)

    def test_the_stream_album_carries_no_codec_to_encode_with(self):
        """`.strm` is in Jellyfin's audio extension list, so a music library
        resolves one as a track — but there is nothing to encode, and an album
        that still named a codec would be one edit away from trying to."""
        albums = [a for a in self.libraries.ALBUMS if a.get("stream")]
        self.assertTrue(albums)
        for album in albums:
            self.assertEqual(album["ext"], "strm")
            self.assertFalse(album["codec"])
            # No embedded cover can exist in a file the server never opens.
            self.assertNotEqual(album["art"], "embedded")


class TestOriginRanges(unittest.TestCase):
    """The local origin, against what a player actually asks it for.

    RFC 9110's byte-range rules are restated here rather than imported: the
    suffix form (`bytes=-500` is the *last* 500 bytes) is the one that is easy
    to implement backwards, and an implementation asked whether it agrees with
    itself always says yes.
    """

    def setUp(self):
        from stdjflib import origin

        self.origin = origin

    def test_no_header_means_the_whole_file(self):
        self.assertIsNone(self.origin._parse_range(None, 100))
        self.assertIsNone(self.origin._parse_range("", 100))

    def test_an_open_ended_range_runs_to_the_end(self):
        self.assertEqual(self.origin._parse_range("bytes=10-", 100), (10, 99))

    def test_a_closed_range_is_inclusive(self):
        self.assertEqual(self.origin._parse_range("bytes=0-0", 100), (0, 0))
        self.assertEqual(self.origin._parse_range("bytes=10-19", 100), (10, 19))

    def test_a_suffix_range_is_the_last_n_bytes(self):
        """Reading this as 'up to byte 500' serves the wrong end of the file,
        which looks like corruption rather than a bug."""
        self.assertEqual(self.origin._parse_range("bytes=-20", 100), (80, 99))
        self.assertEqual(self.origin._parse_range("bytes=-500", 100), (0, 99))

    def test_an_end_past_the_file_is_clamped(self):
        self.assertEqual(self.origin._parse_range("bytes=90-999", 100), (90, 99))

    def test_a_start_past_the_file_is_unsatisfiable(self):
        self.assertIs(self.origin._parse_range("bytes=100-", 100), False)
        self.assertIs(self.origin._parse_range("bytes=-0", 100), False)

    def test_multipart_and_junk_fall_back_to_the_whole_file(self):
        for header in ("bytes=0-9,20-29", "items=0-9", "bytes=abc", "bytes="):
            with self.subTest(header=header):
                self.assertIsNone(self.origin._parse_range(header, 100))


class TestOriginServer(unittest.TestCase):
    """Serve a real file over a real socket and ask for real ranges.

    Worth doing end to end rather than in pieces: the failure this guards
    against is a 200 where the client asked for 206, which every unit of the
    code can be correct about individually while the response is still wrong.
    """

    @classmethod
    def setUpClass(cls):
        import urllib.error
        import urllib.request

        from stdjflib import config, origin

        cls.request = urllib.request
        cls.http_error = urllib.error.HTTPError
        cls.dir = tempfile.mkdtemp()
        media = os.path.join(cls.dir, config.STATE_DIR, origin.DIRNAME)
        os.makedirs(media, exist_ok=True)
        cls.body = bytes(range(256)) * 40          # 10240 bytes, position-checkable
        with open(os.path.join(media, "clip.mkv"), "wb") as fh:
            fh.write(cls.body)
        cls.server = origin.Origin(cls.dir, port=0, bind="127.0.0.1")
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        import shutil

        cls.server.stop()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def fetch(self, name="clip.mkv", headers=None, method="GET"):
        req = self.request.Request(f"{self.server.local_url}/{name}",
                                   headers=headers or {}, method=method)
        try:
            with self.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except self.http_error as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_the_whole_file_comes_back_intact(self):
        status, headers, body = self.fetch()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.body)
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(headers.get("Content-Type"), "video/x-matroska")

    def test_a_range_gets_206_and_only_that_range(self):
        status, headers, body = self.fetch(headers={"Range": "bytes=1000-1099"})
        self.assertEqual(status, 206)
        self.assertEqual(headers.get("Content-Range"), "bytes 1000-1099/10240")
        self.assertEqual(headers.get("Content-Length"), "100")
        self.assertEqual(body, self.body[1000:1100])

    def test_an_open_ended_range_reaches_the_end(self):
        status, _headers, body = self.fetch(headers={"Range": "bytes=10200-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.body[10200:])

    def test_a_suffix_range_returns_the_tail(self):
        status, _headers, body = self.fetch(headers={"Range": "bytes=-64"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.body[-64:])

    def test_an_unsatisfiable_range_is_416_with_the_size(self):
        status, headers, _body = self.fetch(headers={"Range": "bytes=99999-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers.get("Content-Range"), "bytes */10240")

    def test_head_carries_the_length_and_no_body(self):
        status, headers, body = self.fetch(method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Length"), str(len(self.body)))
        self.assertEqual(body, b"")

    def test_a_missing_file_is_404(self):
        self.assertEqual(self.fetch("nope.mkv")[0], 404)

    def test_nothing_outside_the_origin_directory_is_reachable(self):
        """The origin sits under the library root, which holds the manifest and
        the download cache. A traversal out of it would serve those."""
        for name in ("../manifest.json", "..%2Fmanifest.json", "%2Fetc%2Fpasswd",
                     "sub/clip.mkv", "..\\clip.mkv"):
            with self.subTest(name=name):
                self.assertIn(self.fetch(name)[0], (400, 404))

    def test_files_lists_what_is_there(self):
        self.assertEqual(self.server.files(), ["clip.mkv"])

    def test_reachable_agrees_with_the_server(self):
        self.assertTrue(self.origin_mod().reachable(
            self.server.local_url, "clip.mkv"))
        self.assertFalse(self.origin_mod().reachable(
            self.server.local_url, "nope.mkv"))

    def origin_mod(self):
        from stdjflib import origin

        return origin


class TestOriginFixtures(unittest.TestCase):
    """How the origin and the `.strm` files that name it are kept in step."""

    def setUp(self):
        from stdjflib import libraries, origin

        self.libraries = libraries
        self.origin = origin

    def test_every_origin_fixture_points_at_a_file_that_gets_built(self):
        """A stream file naming an origin clip nobody generates is a fixture
        that resolves and 404s, which reads as a broken server."""
        cfg = config.BuildConfig(root="/tmp/whatever")
        for key, url in self.libraries.origin_targets(cfg.stream_origin).items():
            with self.subTest(key=key):
                self.assertIn(url.rsplit("/", 1)[-1], self.libraries.ORIGIN_CLIPS)

    def test_every_origin_clip_is_named_by_a_fixture(self):
        """The other direction: a clip nobody points at is build time spent on
        something no test can reach, and it would go unnoticed for exactly
        that reason."""
        named = {name for _lib, name in self.libraries.ORIGIN_FIXTURES.values()}
        for name in self.libraries.ORIGIN_CLIPS:
            with self.subTest(name=name):
                self.assertIn(name, named)

    def test_a_clip_may_be_shared_but_a_fixture_key_may_not(self):
        """Two `.strm` files naming one clip are still two items — the file is
        a stream target, not an item. Two fixtures sharing a *key* would be
        one overwriting the other."""
        keys = list(self.libraries.ORIGIN_FIXTURES)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertLess(len(self.libraries.ORIGIN_CLIPS), len(keys))

    def test_there_is_a_clip_long_enough_to_hold_a_resume_point(self):
        """`UserDataManager.UpdatePlayState` enforces
        MinResumeDurationSeconds, 300 by default: below it the position is
        zeroed and the item is marked played, so a shorter fixture cannot test
        resume at all."""
        longest = max(seconds for _ext, seconds, _video, _audios
                      in self.libraries.ORIGIN_CLIPS.values())
        self.assertGreater(longest, 300)

    def test_the_long_clip_is_not_allowed_to_dominate_the_tier(self):
        """400 seconds at the bitrate the other clips use would be some 80 MB
        in a minimal tier of 400. Nothing about a resume point needs to look
        good."""
        clips = self.libraries.ORIGIN_CLIPS
        longest = max(clips, key=lambda name: clips[name][1])
        _ext, seconds, video, audios = clips[longest]
        kbits = int(video.bitrate.rstrip("k"))
        kbits += sum(int((a.bitrate or "128k").rstrip("k")) for a in audios)
        self.assertLess(kbits * seconds / 8 / 1000, 8, "megabytes")

    def test_both_version_spellings_exist(self):
        """They test opposite things. A set whose primary is the local file
        leaves its shortcut alternate unprobed; a set whose primary *is* the
        shortcut gets both runtimes, because the probe gate reads the item's
        path. Losing either leaves a gap the other cannot cover."""
        shapes = {shape for _k, _t, shape, _p in self.libraries.NAMING_CASES}
        self.assertIn("strm-origin-versions", shapes)
        self.assertIn("strm-origin-primary-versions", shapes)

    def test_the_strm_primary_is_named_exactly_like_its_folder(self):
        """`OrganizeAlternateVersions` makes an exact-name file the primary
        outright. One character of drift and the local file wins instead, the
        item's path stops ending in .strm, and the fixture silently becomes a
        duplicate of the other one."""
        folder = "Origin Primary Versions (2020)"
        primary = folder + ".strm"
        self.assertEqual(os.path.splitext(primary)[0], folder)
        # And the alternate still has to be eligible for the set at all.
        alternate = self.libraries.version_path(folder, "Local File", "mkv")
        stem = os.path.splitext(alternate)[0]
        self.assertTrue(stem.startswith(folder))
        self.assertRegex(stem[len(folder):].strip(),
                         TestMultiVersionNaming.ELIGIBLE)

    def test_the_two_version_sets_do_not_share_a_local_length(self):
        """A runtime should say which fixture you are looking at as well as
        which source within it."""
        self.assertNotEqual(self.libraries.ORIGIN_VERSION_LOCAL_SECONDS,
                            self.libraries.ORIGIN_PRIMARY_LOCAL_SECONDS)
        clips = self.libraries.ORIGIN_CLIPS
        _lib, name = self.libraries.ORIGIN_FIXTURES["movie-strm-origin-primary"]
        self.assertNotEqual(clips[name][1],
                            self.libraries.ORIGIN_PRIMARY_LOCAL_SECONDS)

    def test_the_version_fixture_lengths_are_far_enough_apart(self):
        """A version picker whose two entries report the same runtime cannot
        say which one is playing, so a test asserting on the switch would pass
        without switching."""
        clips = self.libraries.ORIGIN_CLIPS
        _lib, name = self.libraries.ORIGIN_FIXTURES["movie-strm-origin-versions"]
        remote_seconds = clips[name][1]
        local_seconds = self.libraries.ORIGIN_VERSION_LOCAL_SECONDS
        self.assertGreaterEqual(max(remote_seconds, local_seconds),
                                2 * min(remote_seconds, local_seconds))

    def test_origin_urls_are_remote_as_far_as_jellyfin_is_concerned(self):
        from stdjflib import strm

        cfg = config.BuildConfig(root="/tmp/whatever")
        for url in self.libraries.origin_targets(cfg.stream_origin).values():
            self.assertTrue(strm.is_remote(url), url)

    def test_the_origin_lives_outside_every_library_folder(self):
        """Media inside a library folder is scanned, and the origin clips would
        become items — which is the one thing a stream target must not be."""
        path = self.origin.directory("/srv/qa")
        self.assertTrue(path.startswith("/srv/qa/" + config.STATE_DIR))
        for library in config.LIBRARIES:
            self.assertNotIn(f"/{library}/", path + "/")

    def test_the_default_origin_port_does_not_collide_with_faketvsource(self):
        from stdjflib import livetv

        self.assertNotEqual(self.origin.DEFAULT_PORT, livetv.DEFAULT_PORT)

    def test_port_is_read_back_out_of_the_recorded_url(self):
        self.assertEqual(self.origin.port_of("http://127.0.0.1:8410"), 8410)
        self.assertEqual(self.origin.port_of("http://host.containers.internal:9"), 9)

    def test_a_loopback_origin_is_reported_unreachable_from_a_container(self):
        """The trap `livetv.py` documents, arriving from the other side: the
        URL is already inside the files and cannot be fixed at startup."""
        ok, why = self.origin.describe_reachability(
            "http://127.0.0.1:8410", from_container="host.containers.internal")
        self.assertFalse(ok)
        self.assertIn("--stream-origin", why)
        self.assertIn("host.containers.internal", why)

    def test_a_named_host_is_left_alone(self):
        ok, why = self.origin.describe_reachability(
            "http://host.containers.internal:8410",
            from_container="host.containers.internal")
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_a_loopback_origin_is_fine_for_a_server_on_this_machine(self):
        ok, _why = self.origin.describe_reachability("http://127.0.0.1:8410")
        self.assertTrue(ok)

    def test_a_non_http_origin_is_refused_at_config_time(self):
        """Jellyfin drops a .strm naming anything but http/https/rtsp/rtp, so
        an origin that is not one produces a library of dead fixtures."""
        with self.assertRaises(ValueError):
            config.BuildConfig(root="/tmp/x", stream_origin="ftp://host/media")

    def test_a_trailing_slash_does_not_double_up_in_the_url(self):
        cfg = config.BuildConfig(root="/tmp/x",
                                 stream_origin="http://host:8410/")
        for url in self.libraries.origin_targets(cfg.stream_origin).values():
            self.assertNotIn("//", url.split("://", 1)[1])
