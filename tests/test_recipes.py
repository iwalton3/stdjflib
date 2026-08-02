"""The matrix itself: uniqueness, tier nesting, and internal consistency.

These run without ffmpeg and without touching the filesystem.
"""

import unittest

from stdjflib import config, generate, recipes


class TestMatrix(unittest.TestCase):
    def test_keys_are_unique(self):
        # all_recipes() raises on a duplicate; this pins that it is checked.
        keys = [r.key for r in recipes.all_recipes()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_tiers_nest(self):
        minimal = {r.key for r in recipes.for_tier("minimal")}
        standard = {r.key for r in recipes.for_tier("standard")}
        full = {r.key for r in recipes.for_tier("full")}
        self.assertLess(minimal, standard)
        self.assertLess(standard, full)

    def test_every_recipe_has_notes(self):
        """The notes become the item's plot, so an empty one is a blank item."""
        for r in recipes.all_recipes():
            with self.subTest(r.key):
                self.assertTrue(r.notes.strip(), f"{r.key} has no notes")
                self.assertTrue(r.title.strip())

    def test_tier_values_are_legal(self):
        for r in recipes.all_recipes():
            self.assertIn(r.tier, config.TIERS)

    def test_containers_have_a_known_muxer(self):
        for r in recipes.all_recipes():
            with self.subTest(r.key):
                self.assertTrue(generate.muxer_for(r.container))
                # `-f mkv` is not a thing; this is the mapping that catches it.
                self.assertNotEqual(generate.muxer_for(r.container), "mkv")

    def test_no_recipe_exceeds_its_encoder_channel_limit(self):
        """ffmpeg reports an over-wide layout confusingly, so catch it here."""
        for r in recipes.all_recipes():
            with self.subTest(r.key):
                self.assertIsNone(generate.channel_limit(r))

    def test_webm_only_carries_legal_codecs(self):
        legal_v = {"libvpx-vp9", "libvpx", "libsvtav1", "libaom-av1"}
        legal_a = {"libopus", "libvorbis"}
        for r in recipes.all_recipes():
            if r.container != "webm":
                continue
            with self.subTest(r.key):
                if r.video:
                    self.assertIn(r.video.encoder, legal_v)
                for a in r.audios:
                    self.assertIn(a.encoder, legal_a)

    def test_mp4_subtitles_are_mov_text(self):
        """MP4 permits no other text subtitle codec."""
        for r in recipes.all_recipes():
            if r.container not in ("mp4", "3gp"):
                continue
            for s in r.subs:
                if s.external:
                    continue
                with self.subTest(r.key):
                    self.assertEqual(s.codec, "mov_text")

    def test_coverage_of_the_things_this_library_exists_for(self):
        """A guard against quietly losing a whole category."""
        keys = {r.key for r in recipes.all_recipes()}
        for required in ("v-h264-high10", "v-hevc-main10", "v-av1",
                         "a-truehd-51", "a-dts-51", "s-vobsub", "h-hdr10",
                         "i-1080i25", "r-anamorphic-pal", "x-chapters"):
            self.assertIn(required, keys)

        groups = {r.group for r in recipes.all_recipes()}
        for required in ("Video Codecs", "Audio Codecs", "Containers",
                         "Subtitles", "HDR and Colour", "Frame Rates",
                         "Scan Types", "Aspect Ratios", "Structure"):
            self.assertIn(required, groups)

    def test_bitmap_subtitle_cases_exist(self):
        """ffmpeg cannot make these, so losing them loses the burn-in path."""
        bitmap = [r for r in recipes.all_recipes()
                  if any(s.codec in generate.BITMAP_SUBS for s in r.subs)]
        self.assertTrue(bitmap)
        self.assertTrue(any(s.external for r in bitmap for s in r.subs))


class TestDescribe(unittest.TestCase):
    def test_describe_mentions_every_stream(self):
        rec = {r.key: r for r in recipes.all_recipes()}["x-many-audio"]
        text = generate.describe(rec)
        self.assertIn(rec.title, text)
        for a in rec.audios:
            self.assertIn(a.lang, text)
        self.assertIn(rec.key, text)

    def test_describe_handles_no_audio_and_no_video(self):
        by = {r.key: r for r in recipes.all_recipes()}
        self.assertIn("no audio stream", generate.describe(by["x-no-audio"]))
        self.assertIn("no video stream", generate.describe(by["x-audio-only"]))


if __name__ == "__main__":
    unittest.main()
