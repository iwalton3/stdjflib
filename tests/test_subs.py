"""Subtitle text generation, and the VobSub binary format.

The VobSub tests matter more than they look: it is a format written by hand
here, and a wrong nibble produces a file that still parses and renders
garbage. `test_roundtrip_rle` is the one that would catch that.
"""

import unittest

from stdjflib import subs, vobsub


class TestCues(unittest.TestCase):
    def test_cues_are_ordered_and_within_the_clip(self):
        for duration in (10, 20, 30, 120):
            got = subs.cues(duration, "latin", "eng", "EN")
            with self.subTest(duration=duration):
                self.assertTrue(got)
                for start, end, _text in got:
                    self.assertLess(start, end)
                    self.assertLessEqual(end, duration + 0.01)
                for (s1, e1, _), (s2, _, _) in zip(got, got[1:]):
                    self.assertLessEqual(e1, s2 + 0.01,
                                         "cues must not overlap")

    def test_forced_track_is_shorter_than_the_full_one(self):
        """That asymmetry is what makes the forced flag observable."""
        full = subs.cues(30, "latin", "eng", "EN")
        forced = subs.cues(30, "latin", "eng", "EN", forced=True)
        self.assertLess(len(forced), len(full))
        self.assertIn("FORCED", forced[0][2])

    def test_scripts_are_actually_different(self):
        seen = {}
        for lang, script in (("eng", "latin"), ("rus", "cyrillic"),
                             ("jpn", "cjk"), ("heb", "rtl"), ("ara", "arabic")):
            text = subs.srt(20, script, lang, lang)
            self.assertNotIn(text, seen.values(),
                             f"{lang} produced the same text as another script")
            seen[lang] = text

    def test_hebrew_and_arabic_differ(self):
        """Both map to 'rtl' in the recipes; they must not collapse to one."""
        self.assertNotEqual(subs.sample_lines("rtl", "heb"),
                            subs.sample_lines("rtl", "ara"))


class TestFormats(unittest.TestCase):
    def test_srt_shape(self):
        text = subs.srt(20, "latin", "eng", "EN")
        self.assertTrue(text.startswith("1\n"))
        self.assertIn(" --> ", text)
        self.assertIn(",", text.split(" --> ")[0][-8:])  # comma, not dot

    def test_vtt_shape(self):
        text = subs.vtt(20, "latin", "eng", "EN")
        self.assertTrue(text.startswith("WEBVTT"))
        # WebVTT uses a dot for the fractional part, SubRip a comma.
        stamp = text.split(" --> ")[0].splitlines()[-1]
        self.assertIn(".", stamp)
        self.assertNotIn(",", stamp)

    def test_ass_has_styles_and_events(self):
        text = subs.ass(30, "latin", "eng", "EN")
        self.assertIn("[V4+ Styles]", text)
        self.assertIn("[Events]", text)
        self.assertIn("Dialogue:", text)
        # The three things plain-text rendering loses.
        self.assertIn("\\pos(", text)
        self.assertIn("\\k", text)
        self.assertIn("Style: Sign", text)

    def test_bitmap_codecs_fall_back_to_subrip_text(self):
        for codec in ("dvdsub", "dvbsub", "xsub"):
            text, ext = subs.render(codec, 20, "latin", "eng", "EN")
            self.assertEqual(ext, "srt")
            self.assertIn(" --> ", text)


class TestVobsubRLE(unittest.TestCase):
    def _decode(self, data: bytes, width: int, rows: int) -> list[list[int]]:
        """A reference decoder, so the encoder is checked against something."""
        nibbles = []
        for byte in data:
            nibbles += [byte >> 4, byte & 0xF]
        out, i = [], 0
        for _ in range(rows):
            line, x = [], 0
            while x < width and i < len(nibbles):
                value, taken = 0, 0
                while taken < 4:
                    value = (value << 4) | nibbles[i]
                    i += 1
                    taken += 1
                    if value >= 0x4 << (0 if taken == 1 else 0) and taken == 1 \
                            and value >= 0x4:
                        break
                    if taken == 2 and value >= 0x10:
                        break
                    if taken == 3 and value >= 0x40:
                        break
                count, color = value >> 2, value & 0x3
                if count == 0:
                    count = width - x
                count = min(count, width - x)
                line += [color] * count
                x += count
            if i % 2:
                i += 1  # rows are padded to a byte
            out.append(line)
        return out

    def test_emit_widths(self):
        """Run length picks the encoding width; the boundaries are the bugs."""
        cases = [(1, 1), (3, 1), (4, 2), (15, 2), (16, 3), (63, 3), (64, 4),
                 (255, 4)]
        for count, expected in cases:
            nibbles = []
            vobsub._emit(nibbles, count, 1)
            with self.subTest(count=count):
                self.assertEqual(len(nibbles), expected)

    def test_roundtrip_rle(self):
        """Encode a known bitmap and decode it back to the same pixels."""
        rows = [
            [0] * 40,
            [0] * 10 + [1] * 20 + [0] * 10,
            [2] * 40,
            [0, 1, 2, 1] * 10,
            [1] * 40,
            [0] * 39 + [2],
        ]
        encoded = vobsub.encode_field(rows)
        decoded = self._decode(encoded, 40, len(rows))
        self.assertEqual(decoded, rows)

    def test_long_runs_are_split(self):
        """A run wider than 255 needs more than one code."""
        rows = [[1] * 700]
        encoded = vobsub.encode_field(rows)
        self.assertEqual(self._decode(encoded, 700, 1), rows)

    def test_each_row_ends_byte_aligned(self):
        # One pixel per row is a single nibble, so each row needs a pad nibble.
        encoded = vobsub.encode_field([[1], [1], [1]])
        self.assertEqual(len(encoded), 3)


class TestVobsubFraming(unittest.TestCase):
    def _spu(self, rows=None):
        rows = rows or [[0] * 64 for _ in range(8)]
        return vobsub.build_spu(rows, 0, 400, 3.0)

    def test_spu_header_is_self_consistent(self):
        spu = self._spu()
        total = int.from_bytes(spu[0:2], "big")
        ctrl = int.from_bytes(spu[2:4], "big")
        self.assertEqual(total, len(spu))
        self.assertLess(ctrl, total)
        # The control block must start with the palette command.
        self.assertEqual(spu[ctrl + 4], 0x03)

    def test_sectors_are_whole_and_aligned(self):
        data = vobsub._sectors(self._spu(), 90000, 0)
        self.assertEqual(len(data) % vobsub.SECTOR, 0)
        for off in range(0, len(data), vobsub.SECTOR):
            with self.subTest(off=off):
                self.assertEqual(data[off:off + 4], b"\x00\x00\x01\xba")

    def test_large_spu_spans_several_sectors(self):
        """Real text rasterises to 2-4 KB, so splitting is the normal path."""
        rows = [[(x // 2) % 4 for x in range(720)] for _ in range(48)]
        spu = vobsub.build_spu(rows, 0, 400, 3.0)
        self.assertGreater(len(spu), vobsub.SECTOR)
        data = vobsub._sectors(spu, 90000, 0)
        self.assertGreater(len(data) // vobsub.SECTOR, 1)
        # Only the first packet carries a PTS.
        self.assertEqual(data[14 + 6:14 + 9], b"\x81\x80\x05")
        second = vobsub.SECTOR
        self.assertEqual(data[second + 14 + 6:second + 14 + 9], b"\x81\x00\x00")

    def test_pts_encoding(self):
        got = vobsub._pts_bytes(90000)
        self.assertEqual(got[0] & 0xF0, 0x20)      # '0010' prefix
        for i in (0, 2, 4):
            self.assertEqual(got[i] & 0x01, 0x01)  # marker bits


if __name__ == "__main__":
    unittest.main()
