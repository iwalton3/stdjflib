"""A VobSub (.idx/.sub) writer.

ffmpeg refuses to make image subtitles from text ones — "Subtitle encoding
currently only possible from text to text or bitmap to bitmap" — so there is no
way to reach the bitmap-subtitle code path with ffmpeg alone. That path matters:
bitmap subtitles cannot be converted to text without OCR, so Jellyfin has to
either burn them into the video (a full transcode) or hand the client bitmaps to
composite. A library with no bitmap subtitles never exercises either.

So this module builds the format directly. Text is rasterised by ffmpeg's
drawtext into a PGM — which keeps font handling in one place and needs no
imaging library — then run-length encoded into DVD sub-picture units and wrapped
in an MPEG-2 program stream.

Format references: the SPU control sequence and the 2-bit RLE are as described
in the DVD-Video specification; the .idx sidecar is MPlayer's, which is what
everything reads today.
"""

from __future__ import annotations

import os
import subprocess

SECTOR = 2048

# Sixteen RGB entries the SPU's palette command indexes into. Only the first
# four are used: transparent background, white fill, black outline, spare.
PALETTE = [
    "000000", "ffffff", "000000", "808080",
    "000000", "000000", "000000", "000000",
    "000000", "000000", "000000", "000000",
    "000000", "000000", "000000", "000000",
]


class TooLarge(RuntimeError):
    """A rasterised cue did not fit one 2048-byte sector."""


# --------------------------------------------------------------------------
# Rasterising
# --------------------------------------------------------------------------

def rasterise(text: str, width: int, height: int, font_file: str,
              ffmpeg: str = "ffmpeg", fontsize: int = 28) -> list[list[int]]:
    """Render `text` white-on-black and return rows of palette indices.

    Uses a PGM intermediate because it is the one raster format that can be
    parsed in a dozen lines: a short ASCII header then one byte per pixel.
    """
    tmp_txt = None
    try:
        import tempfile

        fd, tmp_txt = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)

        argv = [
            ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=1",
            "-vf",
            (f"drawtext=textfile='{tmp_txt}':fontfile='{font_file}'"
             f":fontcolor=white:fontsize={fontsize}"
             f":borderw=0:x=(w-text_w)/2:y=(h-text_h)/2"),
            "-frames:v", "1", "-pix_fmt", "gray", "-f", "image2pipe",
            "-vcodec", "pgm", "-",
        ]
        proc = subprocess.run(argv, capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-800:])
        return _parse_pgm(proc.stdout, width, height)
    finally:
        if tmp_txt and os.path.exists(tmp_txt):
            os.unlink(tmp_txt)


def _parse_pgm(data: bytes, width: int, height: int) -> list[list[int]]:
    """Binary PGM (P5) -> rows of palette indices 0 (bg), 1 (fill), 2 (edge)."""
    if not data.startswith(b"P5"):
        raise ValueError("not a binary PGM")
    # Header is three whitespace-separated integers after the magic, with
    # '#' comments legal anywhere between them.
    fields: list[int] = []
    pos = 2
    while len(fields) < 3:
        while pos < len(data) and data[pos : pos + 1].isspace():
            pos += 1
        if data[pos : pos + 1] == b"#":
            while pos < len(data) and data[pos] != 0x0A:
                pos += 1
            continue
        start = pos
        while pos < len(data) and not data[pos : pos + 1].isspace():
            pos += 1
        fields.append(int(data[start:pos]))
    pos += 1  # single whitespace byte before the raster
    w, h, _maxval = fields
    raster = data[pos : pos + w * h]

    rows = []
    for y in range(min(h, height)):
        line = raster[y * w : (y + 1) * w]
        # Three levels: the glyph body, its antialiased rim (which becomes the
        # outline colour so text stays legible over any picture), background.
        rows.append([2 if 40 <= v < 160 else (1 if v >= 160 else 0) for v in line])
    while len(rows) < height:
        rows.append([0] * w)
    return rows


# --------------------------------------------------------------------------
# RLE
# --------------------------------------------------------------------------

def _emit(nibbles: list[int], count: int, color: int) -> None:
    """Append one run in the DVD 2-bit RLE.

    Four widths, chosen by run length: 1 nibble for 1-3, 2 for 4-15, 3 for
    16-63, 4 for 64-255. The value is always (count << 2) | colour, zero-padded
    up to the chosen width.
    """
    value = (count << 2) | color
    if count < 4:
        nibbles.append(value & 0xF)
    elif count < 16:
        nibbles += [(value >> 4) & 0xF, value & 0xF]
    elif count < 64:
        nibbles += [(value >> 8) & 0xF, (value >> 4) & 0xF, value & 0xF]
    else:
        nibbles += [(value >> 12) & 0xF, (value >> 8) & 0xF,
                    (value >> 4) & 0xF, value & 0xF]


def encode_field(rows: list[list[int]]) -> bytes:
    """RLE one field. Each row is padded to a whole byte, as decoders expect."""
    nibbles: list[int] = []
    for row in rows:
        start = len(nibbles)
        run_color = row[0] if row else 0
        run_len = 0
        for px in row:
            if px == run_color and run_len < 255:
                run_len += 1
            else:
                _emit(nibbles, run_len, run_color)
                run_color, run_len = px, 1
        if run_len:
            _emit(nibbles, run_len, run_color)
        if (len(nibbles) - start) % 2:
            nibbles.append(0)
    if len(nibbles) % 2:
        nibbles.append(0)
    out = bytearray()
    for i in range(0, len(nibbles), 2):
        out.append((nibbles[i] << 4) | nibbles[i + 1])
    return bytes(out)


# --------------------------------------------------------------------------
# Sub-picture unit
# --------------------------------------------------------------------------

def build_spu(rows: list[list[int]], x: int, y: int, duration: float) -> bytes:
    """One sub-picture unit: RLE for both fields plus the control sequences."""
    height = len(rows)
    width = len(rows[0]) if rows else 0
    top = encode_field(rows[0::2])
    bottom = encode_field(rows[1::2])

    data_len = 4 + len(top) + len(bottom)
    top_off = 4
    bottom_off = 4 + len(top)

    x2, y2 = x + width - 1, y + height - 1
    # Display area: four 12-bit values packed into six bytes.
    area = bytes([
        (x >> 4) & 0xFF, ((x & 0xF) << 4) | ((x2 >> 8) & 0xF), x2 & 0xFF,
        (y >> 4) & 0xFF, ((y & 0xF) << 4) | ((y2 >> 8) & 0xF), y2 & 0xFF,
    ])

    # Colours and alpha are given for palette entries 3,2,1,0 in that order.
    ctrl0_cmds = (
        b"\x03\x32\x10"          # colours -> palette idx 3,2,1,0
        + b"\x04\x0f\xf0"        # alpha: 3 off, 2 and 1 opaque, 0 off
        + b"\x05" + area
        + b"\x06" + top_off.to_bytes(2, "big") + bottom_off.to_bytes(2, "big")
        + b"\x01"                # start displaying
        + b"\xff"                # end of this command set
    )
    ctrl1_cmds = b"\x02\xff"     # stop displaying

    ctrl0_off = data_len
    ctrl0_len = 4 + len(ctrl0_cmds)
    ctrl1_off = ctrl0_off + ctrl0_len
    # The delay unit is 1024/90000 s, and it is measured from the SPU's PTS.
    delay = min(0xFFFF, int(duration * 90000 / 1024))

    ctrl0 = (b"\x00\x00" + ctrl1_off.to_bytes(2, "big") + ctrl0_cmds)
    # A terminal control sequence points at itself.
    ctrl1 = (delay.to_bytes(2, "big") + ctrl1_off.to_bytes(2, "big") + ctrl1_cmds)

    total = data_len + len(ctrl0) + len(ctrl1)
    if total % 2:
        ctrl1 += b"\xff"
        total += 1

    return (total.to_bytes(2, "big") + ctrl0_off.to_bytes(2, "big")
            + top + bottom + ctrl0 + ctrl1)


# --------------------------------------------------------------------------
# Program stream framing
# --------------------------------------------------------------------------

# SCR 0, mux rate 25200, no stuffing. Constant because nothing reading a .sub
# looks at it — the .idx byte offsets are the real index.
_PACK_HEADER = bytes.fromhex("000001BA4400040004010189C3F8")


def _pts_bytes(pts_90k: int) -> bytes:
    """The five-byte PTS field: '0010' then 33 bits split by marker bits."""
    p = pts_90k & 0x1FFFFFFFF
    return bytes([
        0x21 | ((p >> 29) & 0x0E),
        (p >> 22) & 0xFF,
        0x01 | ((p >> 14) & 0xFE),
        (p >> 7) & 0xFF,
        0x01 | ((p << 1) & 0xFE),
    ])


def _sectors(spu: bytes, pts_90k: int, stream_index: int) -> bytes:
    """Wrap one SPU in as many 2048-byte sectors as it needs.

    A rasterised line of text RLEs to 2-4 KB, so splitting is the normal case,
    not an edge case. Only the first packet carries a PTS; every packet repeats
    the substream id, which is what tells a demuxer which subtitle track the
    continuation belongs to.
    """
    substream = 0x20 | (stream_index & 0x1F)
    # 14 pack + 6 PES start/length + 3 flag bytes + 5 PTS + 1 substream.
    first_capacity = SECTOR - (14 + 6 + 3 + 5 + 1)
    rest_capacity = SECTOR - (14 + 6 + 3 + 1)

    out = bytearray()
    offset = 0
    first = True
    while offset < len(spu):
        capacity = first_capacity if first else rest_capacity
        chunk = spu[offset : offset + capacity]
        offset += len(chunk)

        if first:
            header = b"\x81\x80\x05" + _pts_bytes(pts_90k)
        else:
            header = b"\x81\x00\x00"
        payload = bytes([substream]) + chunk
        pes_len = len(header) + len(payload)
        pes = (b"\x00\x00\x01\xbd" + pes_len.to_bytes(2, "big")
               + header + payload)

        sector = bytearray(_PACK_HEADER + pes)
        pad = SECTOR - len(sector)
        if pad >= 6:
            sector += (b"\x00\x00\x01\xbe" + (pad - 6).to_bytes(2, "big")
                       + b"\xff" * (pad - 6))
        elif pad:
            # Too small for a padding packet's own header; a demuxer scans to
            # the next start code regardless, so zero-fill is safe.
            sector += b"\x00" * pad
        out += sector
        first = False
    return bytes(out)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def write(cues, out_base: str, width: int, height: int, font_file: str,
          *, lang: str = "en", ffmpeg: str = "ffmpeg",
          fontsize: int = 28) -> tuple[str, str]:
    """Write `out_base.idx` and `out_base.sub`.

    `cues` is an iterable of (start_seconds, end_seconds, text). Returns the
    two paths. Cues that do not fit a sector are dropped with a warning rather
    than aborting — one oversized line should not cost the whole track.
    """
    # A strip across the lower third, which is where subtitles live and which
    # keeps each RLE well inside one sector.
    strip_h = max(72, height // 6)
    strip_y = height - strip_h - max(16, height // 24)

    sub_path = out_base + ".sub"
    idx_path = out_base + ".idx"
    entries = []

    with open(sub_path, "wb") as fh:
        for start, end, text in cues:
            rows = rasterise(text, width, strip_h, font_file, ffmpeg=ffmpeg,
                             fontsize=fontsize)
            spu = build_spu(rows, 0, strip_y, max(0.5, end - start))
            entries.append((start, fh.tell()))
            fh.write(_sectors(spu, int(start * 90000), 0))

    with open(idx_path, "w", encoding="utf-8") as fh:
        fh.write("# VobSub index file, v7\n")
        fh.write(f"size: {width}x{height}\n")
        fh.write("palette: " + ", ".join(PALETTE) + "\n")
        fh.write("langidx: 0\n\n")
        fh.write(f"id: {lang}, index: 0\n")
        for start, pos in entries:
            ms = int(start * 1000)
            h_, ms = divmod(ms, 3_600_000)
            m_, ms = divmod(ms, 60_000)
            s_, ms = divmod(ms, 1000)
            fh.write(f"timestamp: {h_:02d}:{m_:02d}:{s_:02d}:{ms:03d}, "
                     f"filepos: {pos:09x}\n")

    return idx_path, sub_path
