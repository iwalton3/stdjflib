"""Generated artwork: posters, backdrops, logos, thumbs and person images.

Drawn with ffmpeg rather than an imaging library, for the same reason the media
is: no dependency, and the font handling is already solved.

Colours are derived from the item's key, so the same item is the same colour on
every build and two items are rarely the same colour. That makes artwork useful
for identification — if a client shows the wrong poster, you can see it.

Some logos are deliberately hostile. Real logo artwork is a transparent PNG with
no idea what it will be drawn on, and a good share of it is flat white or flat
black. A client that composites a logo onto its own theme colour shows an
invisible logo in one theme and a fine one in the other. Those cases are here on
purpose; making them all opaque would remove the test.
"""

from __future__ import annotations

import hashlib
import os
import textwrap

from . import ff

# Poster is 2:3, backdrop 16:9, banner ~5.4:1, thumb 16:9 — the shapes Jellyfin
# asks for and the ones a client's row-shape heuristics switch on.
SIZES = {
    "poster": (600, 900),
    "backdrop": (1920, 1080),
    "banner": (1000, 185),
    "thumb": (640, 360),
    "logo": (800, 310),
    "person": (400, 600),
}

LOGO_STYLES = ("solid", "light-on-transparent", "dark-on-transparent", "translucent")


def palette(key: str) -> tuple[str, str, str]:
    """Stable (background, accent, ink) hex triple derived from `key`."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    # A dark, saturated background with a lighter accent keeps white text legible
    # without having to measure anything.
    bg = _hsv_hex(hue, 0.55, 0.32)
    accent = _hsv_hex((hue + 0.08) % 1.0, 0.70, 0.72)
    ink = "FFFFFF"
    return bg, accent, ink


def _hsv_hex(h: float, s: float, v: float) -> str:
    i = int(h * 6.0)
    f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r, g, b = [
        (v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)
    ][i % 6]
    return "%02X%02X%02X" % (int(r * 255), int(g * 255), int(b * 255))


def _wrap(text: str, width: int) -> str:
    return "\n".join(textwrap.wrap(text, width=width)[:4]) or text


def draw(kind: str, key: str, lines: list[str], out_path: str, cfg,
         *, style: str = "solid", subtitle: str = "") -> str | None:
    """Render one image. Returns the path, or None if it could not be drawn."""
    if kind not in SIZES:
        raise ValueError(f"unknown artwork kind {kind!r}")
    if not cfg.font_file:
        return None
    width, height = SIZES[kind]
    bg, accent, _ink = palette(key)

    transparent = kind == "logo" and style != "solid"
    if transparent:
        base = f"color=c=black@0:s={width}x{height}:d=1"
        text_color = {
            "light-on-transparent": "white@1.0",
            "dark-on-transparent": "black@1.0",
            "translucent": "white@0.35",
        }[style]
    else:
        base = f"color=c=0x{bg}:s={width}x{height}:d=1"
        text_color = "white@1.0"

    filters = []
    if not transparent:
        # A diagonal band of the accent colour, so posters are distinguishable
        # from across a room and from each other at thumbnail size.
        filters.append(
            f"drawbox=x=0:y={int(height * 0.60)}:w={width}:h={max(4, height // 90)}"
            f":color=0x{accent}@0.95:t=fill"
        )
        filters.append(
            f"drawbox=x=0:y=0:w={max(6, width // 60)}:h={height}"
            f":color=0x{accent}@0.8:t=fill"
        )

    body = _wrap(lines[0] if lines else key, 18 if kind == "poster" else 30)
    label_path = os.path.join(os.path.dirname(out_path) or ".",
                              f".{os.path.basename(out_path)}.txt")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(label_path, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")

    size = max(18, int(height / (6 if kind in ("poster", "person") else 5)))
    if kind in ("banner", "logo"):
        size = max(24, height // 3)
    filters.append(
        f"drawtext=textfile='{label_path}':fontfile='{cfg.font_file}'"
        f":fontcolor={text_color}:fontsize={size}"
        f":x=(w-text_w)/2:y=(h-text_h)/2:line_spacing={size // 5}"
    )
    if subtitle and kind in ("poster", "backdrop", "thumb"):
        small = max(14, size // 3)
        filters.append(
            f"drawtext=text='{ff.escape_drawtext(subtitle)}'"
            f":fontfile='{cfg.font_file}':fontcolor=white@0.75:fontsize={small}"
            f":x=(w-text_w)/2:y=h-{small * 3}"
        )
    if transparent:
        # Both ends are needed: the colour source negotiates its own pixel
        # format, so `black@0` alone gets flattened onto opaque black without a
        # word of complaint — and the result looks perfectly fine, which is how
        # it goes unnoticed.
        filters.append("format=rgba")

    tmp = ff.temp_path(out_path)
    argv = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", base,
            "-vf", ",".join(filters), "-frames:v", "1"]
    if transparent:
        argv += ["-pix_fmt", "rgba"]
    argv += [tmp]
    try:
        ff.run(argv, verbose=cfg.verbose, timeout=120)
        os.replace(tmp, out_path)
        return out_path
    except (ff.FFmpegError, OSError):
        return None
    finally:
        if os.path.exists(label_path):
            os.unlink(label_path)
        if os.path.exists(tmp):
            os.unlink(tmp)


def item_images(folder: str, key: str, title: str, cfg, *,
                subtitle: str = "", logo_style: str | None = None,
                kinds: tuple[str, ...] = ("poster", "backdrop", "logo")) -> list[str]:
    """Write the standard image set for one library item.

    Filenames are the ones Jellyfin's image resolver looks for in a folder:
    `poster.jpg`, `backdrop.jpg`, `logo.png`, `banner.jpg`.
    """
    names = {"poster": "poster.jpg", "backdrop": "backdrop.jpg",
             "logo": "logo.png", "banner": "banner.jpg", "thumb": "thumb.jpg"}
    if logo_style is None:
        # Spread the four styles deterministically across the library rather
        # than making every logo the same, so one theme cannot pass them all.
        logo_style = LOGO_STYLES[
            int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(LOGO_STYLES)
        ]
    written = []
    for kind in kinds:
        path = os.path.join(folder, names[kind])
        got = draw(kind, key, [title], path, cfg,
                   style=logo_style if kind == "logo" else "solid",
                   subtitle=subtitle)
        if got:
            written.append(got)
    return written
