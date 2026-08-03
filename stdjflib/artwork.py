"""Generated artwork: one shape per Jellyfin image type.

Jellyfin does not have "an image" per item. It has Primary, Backdrop, Thumb,
Banner, Logo, Disc and Art, and each is expected at a particular shape: a
poster is 2:3, a backdrop and an episode still are 16:9, album art is square,
a banner is about 5.4:1, a logo is a transparent wordmark. The shape is not
decoration. Clients pick a row's layout from the *measured* aspect ratio of
what the server hands back — jellyfin-web's `getPrimaryImageAspectRatio`
rounds a row's median onto 2:3, 16:9, 1:1 or 4:3 and shapes every card in the
row from it, and the clients that copy it inherit the behaviour. Artwork of
the wrong shape therefore does not merely look wrong; it reshapes the row
around it, which hides the layout bug you were trying to find.

So each kind here is drawn at the shape Jellyfin expects, in a composition
only that kind uses, and stamped with its own name and ratio. A client showing
a banner where a poster belongs shows a picture with "BANNER 5.4:1" written
across it.

Filenames come from `MediaBrowser.LocalMetadata/Images/LocalImageProvider.cs`
and `EpisodeLocalImageProvider.cs` rather than from memory, because the three
naming schemes there do not agree with each other — see `FOLDER_STEM`,
`SIDECAR_STEM` and `SEASON_STEM`.

Drawn with ffmpeg rather than an imaging library, for the same reason the
media is: no dependency, and the font handling is already solved.

Colours derive from the item's key, so the same item is the same colour on
every build and two items are rarely the same colour. That makes artwork
useful for identification — if a client shows the wrong poster, you can see it.

Some logos are deliberately hostile. Real logo artwork is a transparent PNG
with no idea what it will be drawn on, and a good share of it is flat white or
flat black. A client that composites a logo onto its own theme colour shows an
invisible logo in one theme and a fine one in the other. Those cases are here
on purpose; making them all opaque would remove the test.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import tempfile
import textwrap
import threading

from . import ff, photos

# How many images this process has drawn. `stdjflib artwork` reports it, and
# it is the only number that run says anything about — every builder runs, so
# the item counts are the same whether or not anything was redrawn.
_drawn = 0
_drawn_lock = threading.Lock()


def drawn() -> int:
    return _drawn


def reset_drawn() -> None:
    global _drawn
    with _drawn_lock:
        _drawn = 0


@dataclasses.dataclass(frozen=True)
class Spec:
    """One image type: the shape Jellyfin expects it at, and what it stands for."""

    width: int
    height: int
    image_type: str        # the Jellyfin ImageType this file becomes
    ratio: str             # what the stamp says, and what a client will measure
    # How it is painted. "opaque" is a picture; "wordmark" is ink on nothing,
    # in one of the four LOGO_STYLES; "cutout" is a picture with a shape cut
    # out of it, which is what disc art is.
    mode: str = "opaque"

    @property
    def transparent(self) -> bool:
        return self.mode != "opaque"

    @property
    def ext(self) -> str:
        return "png" if self.transparent else "jpg"

    @property
    def aspect(self) -> float:
        return self.width / self.height


# Sizes are the ones the metadata providers hand out in practice: TMDB posters
# are 1000x1500, its clearlogos 800x310, fanart.tv banners 1000x185. Building
# at those sizes means the server's own downscaling is exercised too.
SPECS = {
    "poster":   Spec(1000, 1500, "Primary",  "2:3"),
    "square":   Spec(1000, 1000, "Primary",  "1:1"),
    "thumb":    Spec(1280,  720, "Thumb",    "16:9"),
    "backdrop": Spec(1920, 1080, "Backdrop", "16:9"),
    "banner":   Spec(1000,  185, "Banner",   "5.4:1"),
    "logo":     Spec( 800,  310, "Logo",     "2.6:1", mode="wordmark"),
    "disc":     Spec(1000, 1000, "Disc",     "1:1",   mode="cutout"),
    "art":      Spec(1000,  562, "Art",      "16:9",  mode="wordmark"),
    # Content rather than artwork: a photo library's items *are* images. No
    # stamp, and callers override the size to vary the shape.
    "photo":    Spec(1800, 1200, "Primary",  "3:2"),
}

# --- filenames -------------------------------------------------------------
#
# Three schemes, and the differences are the server's, not ours.
#
# In an item's own folder, `LocalImageProvider` prefers "landscape" over
# "thumb" for ImageType.Thumb, and music prefers "folder" for Primary.
FOLDER_STEM = {
    "poster": "poster", "square": "folder", "thumb": "landscape",
    "backdrop": "backdrop", "banner": "banner", "logo": "logo",
    "disc": "disc",  # "cdart" is the other spelling, preferred for albums
    "art": "clearart",
}

# Beside a loose media file, as `<name>-<stem>`. Backdrops are "fanart" here:
# `<name>-backdrop` is only matched for an item in its own folder, while
# `<name>-fanart` is matched either way. And an episode still is `-thumb`
# exactly — `EpisodeLocalImageProvider` has its own list and "landscape" is
# not on it. It registers the file as **Primary**, not Thumb, which is why
# episodes are 16:9 and a row of them lays out landscape.
#
# No entry for `square`: beside a loose file the only Primary spelling is
# `<name>-poster`, which is indistinguishable from an actual poster. Nothing
# needs one — an album or an artist always owns its folder.
SIDECAR_STEM = {
    "poster": "poster", "thumb": "thumb",
    "backdrop": "fanart", "banner": "banner", "logo": "logo",
    "disc": "disc", "art": "clearart",
}

# In the *series* folder, for a season: `season01-poster`, `season-specials-…`
# for season 0. Here Thumb is "landscape" again and Backdrop is "fanart" —
# neither agrees with the sidecar scheme. Logo and Disc are not read at all.
SEASON_STEM = {
    "poster": "poster", "thumb": "landscape", "backdrop": "fanart",
    "banner": "banner",
}

# What each kind of item gets by default. A season's Primary is a poster, an
# episode's is a still, an album's is square — the same ImageType at three
# different shapes, which is the whole reason this table exists.
SETS = {
    "movie":   ("poster", "backdrop", "logo"),
    "series":  ("poster", "backdrop", "logo", "banner", "thumb"),
    # A season gets a landscape as well as a poster and a backdrop. Jellyfin
    # reads all three for a season — `SEASON_STEM` is the proof, it is the
    # server's own list — and clients lean on the landscape wherever seasons
    # are laid out in a wide row rather than as posters.
    "season":  ("poster", "backdrop", "thumb"),
    "episode": ("thumb",),
    # Square on the server's own authority: `MusicAlbum` and `MusicArtist`
    # both override `GetDefaultPrimaryImageAspectRatio()` to return 1.
    "album":   ("square",),
    "artist":  ("square", "backdrop", "logo", "banner"),
    # One item per library gets the lot, so every image type Jellyfin has is
    # present somewhere and a client can be pointed at a single title to
    # exercise all of them.
    "everything": ("poster", "backdrop", "logo", "banner", "thumb", "disc",
                   "art"),
}

LOGO_STYLES = ("solid", "light-on-transparent", "dark-on-transparent",
               "translucent")

# Title size as a fraction of the image height, and how many characters to
# wrap at. A banner is 185px tall and a poster 1500, so neither a fixed size
# nor one rule for both survives contact.
LAYOUT = {
    "poster":   {"size": 0.075, "wrap": 14},
    "square":   {"size": 0.090, "wrap": 14},
    "thumb":    {"size": 0.110, "wrap": 20},
    "backdrop": {"size": 0.095, "wrap": 22},
    "banner":   {"size": 0.280, "wrap": 26},
    "logo":     {"size": 0.260, "wrap": 16},
    "disc":     {"size": 0.075, "wrap": 16},
    "art":      {"size": 0.190, "wrap": 16},
    "photo":    {"size": 0.130, "wrap": 18},
}


def filename(kind: str) -> str:
    """The file Jellyfin looks for in an item's own folder."""
    return f"{FOLDER_STEM[kind]}.{SPECS[kind].ext}"


def sidecar_name(stem: str, kind: str) -> str:
    """The file Jellyfin looks for beside a loose media file called `stem`."""
    if kind not in SIDECAR_STEM:
        raise ValueError(f"{kind} artwork has no sidecar spelling; it can "
                         f"only go in an item's own folder")
    return f"{stem}-{SIDECAR_STEM[kind]}.{SPECS[kind].ext}"


def season_name(season_no: int, kind: str) -> str:
    """The file Jellyfin looks for in the *series* folder, for one season.

    Season 0 is spelled `season-specials`; everything else is zero-padded to
    two digits, and a season numbered by year keeps all four.
    """
    marker = "-specials" if season_no == 0 else f"{season_no:02d}"
    return f"season{marker}-{SEASON_STEM[kind]}.{SPECS[kind].ext}"


def palette(key: str) -> tuple[str, str, str]:
    """Stable (background, accent, deep) hex triple derived from `key`.

    A dark, saturated background with a lighter accent keeps white text
    legible without having to measure anything. `deep` is the far end of the
    background gradient — a neighbouring hue, darker still, so the gradient
    reads as shading rather than as two colours meeting.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    return (_hsv_hex(hue, 0.55, 0.32),
            _hsv_hex((hue + 0.08) % 1.0, 0.70, 0.72),
            _hsv_hex((hue + 0.93) % 1.0, 0.65, 0.13))


def _background(key: str, w: int, h: int, bg: str, deep: str, cfg) -> str:
    """The source filter every opaque image starts from.

    A flat colour is the one thing artwork never is, and flatness hides
    problems: a client that stretches, crops or letterboxes an image wrongly
    looks fine on a solid rectangle. A gradient with a direction has an
    orientation you can see being mangled.

    `gradients` arrived in ffmpeg 4.4, so an older build falls back to the
    flat colour rather than losing the image.
    """
    if not ff.has_filter(cfg.ffmpeg, "gradients"):
        return f"color=c=0x{bg}:s={w}x{h}:d=1"
    # Four directions, chosen by the key, so neighbouring items in a row do
    # not all shade the same way.
    corner = hashlib.sha256(("grad" + key).encode("utf-8")).digest()[0] % 4
    x0, y0, x1, y1 = ((0, 0, w, h), (w, 0, 0, h), (0, h, w, 0),
                      (w // 2, 0, w // 2, h))[corner]
    return (f"gradients=s={w}x{h}:c0=0x{bg}:c1=0x{deep}"
            f":x0={x0}:y0={y0}:x1={x1}:y1={y1}:nb_colors=2:d=1:speed=0")


def logo_style_for(key: str) -> str:
    """Spread the four styles deterministically rather than picking one.

    Every logo in one style means a single theme colour passes the whole
    library, which is exactly the bug these are here to catch.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return LOGO_STYLES[int(digest, 16) % len(LOGO_STYLES)]


def _hsv_hex(h: float, s: float, v: float) -> str:
    i = int(h * 6.0)
    f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r, g, b = [
        (v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)
    ][i % 6]
    return "%02X%02X%02X" % (int(r * 255), int(g * 255), int(b * 255))


def _wrap(text: str, width: int) -> str:
    # Newlines in the caller's text are kept: the EXIF photos use them to put
    # "TOP" on its own line, which is the whole point of that fixture.
    out = []
    for para in text.split("\n"):
        out.extend(textwrap.wrap(para, width=width) or [""])
    return "\n".join(out[:5]) or text


def _box(x, y, w, h, colour: str, alpha: float = 1.0) -> str:
    return (f"drawbox=x={int(x)}:y={int(y)}:w={max(1, int(w))}"
            f":h={max(1, int(h))}:color=0x{colour}@{alpha}:t=fill")


def _text(body_file: str | None, literal: str | None, *, font: str,
          colour: str, size: int, x: str, y: str, spacing: int = 0,
          shadow: int = 0) -> str:
    """One drawtext. `shadow` adds the treatment subtitles use over video.

    A photograph is not a background a fixed colour of text can survive:
    the next picture along is a snowfield. Darkening the picture until white
    text works is the obvious fix and the wrong one — it reads as a grey
    rectangle laid over a photo, which is what it is. A drop shadow plus a
    hairline outline keeps the photograph at full strength and is what every
    subtitle renderer does for the same problem.
    """
    source = (f"textfile='{body_file}'" if body_file
              else f"text='{ff.escape_drawtext(literal or '')}'")
    out = (f"drawtext={source}:fontfile='{font}':fontcolor={colour}"
           f":fontsize={size}:x={x}:y={y}:line_spacing={spacing}")
    if shadow:
        out += (f":shadowcolor=black@0.75:shadowx={shadow}:shadowy={shadow}"
                f":borderw={max(1, shadow // 2)}:bordercolor=black@0.45")
    return out


def _decor(kind: str, w: int, h: int, accent: str) -> list[str]:
    """The shapes that make one image type recognisable as itself.

    Opaque kinds only — the transparent ones are wordmarks on nothing, and
    drawing furniture on them would defeat what they test.
    """
    if kind == "poster":
        # A spine down the left edge and a title band low on the face: box art.
        return [_box(0, 0, w * 0.07, h, accent, 0.9),
                _box(0, h * 0.70, w, h * 0.010, accent, 0.95)]
    if kind == "square":
        # Concentric frames, the way a sleeve is laid out.
        return [_box(w * 0.06, h * 0.06, w * 0.88, h * 0.88, accent, 0.35),
                _box(w * 0.10, h * 0.10, w * 0.80, h * 0.80, "000000", 0.45),
                _box(0, h * 0.78, w, h * 0.010, accent, 0.9)]
    if kind == "backdrop":
        # A horizon in the lower third, so a backdrop reads as a scene rather
        # than as a very wide poster.
        return [_box(0, h * 0.62, w, h * 0.38, accent, 0.22),
                _box(0, h * 0.62, w, h * 0.006, accent, 0.9)]
    if kind == "thumb":
        # Sprocket holes: a still out of a strip of film.
        holes = []
        for i in range(8):
            x = w * (0.02 + i * 0.123)
            holes.append(_box(x, h * 0.02, w * 0.05, h * 0.06, "000000", 0.55))
            holes.append(_box(x, h * 0.92, w * 0.05, h * 0.06, "000000", 0.55))
        return holes
    if kind == "banner":
        # An art block on the left, the wordmark to the right of it — the
        # layout every real banner uses.
        return [_box(0, 0, w * 0.22, h, accent, 0.85),
                _box(w * 0.22, 0, w * 0.006, h, "000000", 0.6)]
    if kind == "disc":
        # Printed bands across the face. The ring cutout clips them later,
        # which is what makes the result read as a disc rather than a square.
        return [_box(0, h * 0.30, w, h * 0.025, accent, 0.85),
                _box(0, h * 0.72, w, h * 0.025, accent, 0.85)]
    if kind == "photo":
        return [_box(0, h * 0.70, w, h * 0.012, accent, 0.9)]
    return []


def _ramp(w: int, top: float, bottom: float, a0: float, a1: float,
          steps: int = 24) -> list[str]:
    """A vertical fade in black, as a stack of thin boxes.

    ffmpeg has no gradient-fill filter that composites onto an existing frame,
    and a hard-edged rectangle of 40% black over a photograph reads as exactly
    what it is: a rectangle.

    Two things make the stack read as a gradient rather than as stripes. The
    slices must **tile exactly** — an overlap of even one pixel is painted
    twice and shows up as a dark line at every boundary, which is worse than
    the hard edge being avoided. And there have to be enough of them that one
    step is invisible: 24 across a 0.45 fade is a step of 5/255, under the
    threshold at which JPEG's own blocking is more noticeable.
    """
    top, bottom = int(top), int(bottom)
    if bottom <= top:
        return []
    edges = [top + round((bottom - top) * i / steps) for i in range(steps + 1)]
    return [_box(0, edges[i], w, edges[i + 1] - edges[i], "000000",
                 round(a0 + (a1 - a0) * (i / max(1, steps - 1)), 3))
            for i in range(steps) if edges[i + 1] > edges[i]]


def _scrim(kind: str, w: int, h: int) -> list[str]:
    """Darkening laid over a photograph so white text stays readable on it.

    Not a flat wash over the whole picture — that is what makes an overlay
    look like an overlay. A light overall knock-back, then a gradient behind
    the text, which is what a real poster does; most of the photograph is left
    at full strength.

    It has to hold for *every* photograph, not the one being looked at. The
    next picture along is a snowfield, and white text on a snowfield with no
    scrim is invisible.
    """
    if kind == "photo":
        # Content, not artwork: the photograph is the item. Nothing on top.
        return []
    if kind == "banner":
        # 185px tall and almost entirely text, so this one is a wash — there
        # is no room for a gradient and nothing much of the picture to lose.
        return [_box(0, 0, w, h, "000000", 0.40)]
    if kind == "backdrop":
        # A backdrop is displayed *behind* a client's own UI, and every one of
        # them darkens the bottom for its own text. Matching that is honest
        # about what the image is for, and it is where the title sits.
        return _ramp(w, h * 0.55, h, 0.0, 0.55)
    # Everything else leans on the text's own shadow. A light wash only, to
    # take the edge off a blown-out sky without flattening the picture.
    return [_box(0, 0, w, h, "000000", 0.12)]


def _title_position(kind: str, w: int, h: int, size: int) -> tuple[str, str]:
    """Where the title goes, per kind, as ffmpeg x/y expressions."""
    if kind == "backdrop":
        # Low and left, where a backdrop's title sits when it has one at all.
        return f"{int(w * 0.05)}", f"h-{int(h * 0.30)}"
    if kind == "banner":
        return f"{int(w * 0.26)}", "(h-text_h)/2"
    if kind == "disc":
        # Above the centre hole, which is where a disc's label is readable.
        return "(w-text_w)/2", f"{int(h * 0.16)}"
    if kind == "poster":
        return "(w-text_w)/2", f"(h-text_h)/2-{int(h * 0.04)}"
    return "(w-text_w)/2", "(h-text_h)/2"


def _stamp_position(kind: str, w: int, h: int, tag: int) -> tuple[str, str]:
    """Where the type stamp goes, staying clear of that kind's furniture."""
    if kind == "banner":
        # Inside the art block, where a long title cannot run into it.
        return f"{tag // 2}", f"h-{int(tag * 1.6)}"
    if kind == "thumb":
        # Below the top row of sprocket holes.
        return f"w-text_w-{tag}", f"{int(h * 0.11)}"
    if kind == "disc":
        # Below the centre hole, still inside the ring.
        return "(w-text_w)/2", f"{int(h * 0.66)}"
    return f"w-text_w-{tag}", f"{tag // 2}"


def draw(kind: str, key: str, title: str, out_path: str, cfg, *,
         style: str | None = None, subtitle: str = "",
         size: tuple[int, int] | None = None, stamp: bool = True,
         seq: int | None = None, text: bool = True) -> str | None:
    """Render one image. Returns the path, or None if it could not be drawn.

    `size` overrides the spec's dimensions, which only photos use — a photo
    library wants a spread of shapes, because a client picks its row layout
    from the median of them.

    `seq` is the item's position in its library. Under `--use-artwork` it
    decides which photograph backs the image, and position rather than hash is
    what keeps a screenful free of repeats.
    """
    if kind not in SPECS:
        raise ValueError(f"unknown artwork kind {kind!r}")
    if not cfg.font_file:
        return None
    spec = SPECS[kind]
    width, height = size or (spec.width, spec.height)
    bg, accent, deep = palette(key)
    layout = LAYOUT[kind]

    if spec.mode == "wordmark":
        style = style or logo_style_for(key)
        ink = {
            "solid": "white@1.0",
            "light-on-transparent": "white@1.0",
            "dark-on-transparent": "black@1.0",
            "translucent": "white@0.35",
        }[style]
    else:
        # Disc art is transparent because of the hole in the middle, not
        # because it is ink on nothing — it gets a painted face like any
        # other picture.
        style, ink = "solid", "white@1.0"

    # A wordmark drawn "solid" still gets a background; that is the one style
    # in four where a client cannot fail on compositing.
    opaque = spec.mode != "wordmark" or style == "solid"

    # A photograph replaces the drawn background, never the wordmarks: a logo
    # is ink on nothing, and a photograph of a logo is not a logo.
    photo = None
    if opaque and spec.mode != "wordmark" and getattr(cfg, "use_artwork", False):
        photo = photos.pick(cfg, seq, key)

    base = (_background(key, width, height, bg, deep, cfg) if opaque
            else f"color=c=black@0:s={width}x{height}:d=1")
    if photo:
        # Cover, not fit: `increase` then crop is what every client does to
        # artwork, and doing it here means the shape on disk is already the
        # shape that is wanted.
        base = (f"[0:v]scale={width}:{height}"
                f":force_original_aspect_ratio=increase,crop={width}:{height}")

    workdir = tempfile.mkdtemp(prefix="stdjflib-art-")
    tmp = ff.temp_path(out_path)
    try:
        body = os.path.join(workdir, "title.txt").replace("\\", "/")
        with open(body, "w", encoding="utf-8") as fh:
            fh.write(_wrap(title or key, layout["wrap"]) + "\n")

        font = cfg.font_file.replace("\\", "/")
        title_size = max(14, int(height * layout["size"]))
        x, y = _title_position(kind, width, height, title_size)

        chain = [base]
        if photo:
            # A photograph is not a background until something makes the text
            # survivable on it — the next picture along is a snowfield.
            chain += _scrim(kind, width, height)
        elif opaque:
            chain += _decor(kind, width, height, accent)
            # Before the text, so the corners darken and the title does not.
            if ff.has_filter(cfg.ffmpeg, "vignette"):
                chain.append("vignette=angle=PI/4.2")
        # Sized off the type: a shadow that reads on a 1500px poster is a
        # smear on a 185px banner.
        shadow = max(2, title_size // 12) if photo else 0
        if text:
            chain.append(_text(body, None, font=font, colour=ink,
                               size=title_size, x=x, y=y,
                               spacing=title_size // 5, shadow=shadow))

        if text and subtitle and kind not in ("logo", "art", "disc"):
            small = max(12, title_size // 3)
            sub_y = (f"h-{int(height * 0.16)}" if kind == "backdrop"
                     else f"h-{small * 3}")
            sub_x = (f"{int(width * 0.05)}" if kind == "backdrop"
                     else "(w-text_w)/2")
            chain.append(_text(None, subtitle, font=font,
                               colour="white@0.9" if photo else
                               ("white@0.75" if opaque else ink),
                               size=small, x=sub_x, y=sub_y,
                               shadow=max(1, small // 10) if photo else 0))

        # The stamp is what makes a misplaced image obvious at a glance: the
        # picture says which type it is and what shape a client should have
        # laid it out at.
        # Off under --use-artwork: the whole point there is a library that
        # photographs like a library, and a wall of "POSTER 2:3" does not.
        if stamp and kind != "photo" and not photo:
            tag = max(11, int(height * 0.040))
            stamp_x, stamp_y = _stamp_position(kind, width, height, tag)
            chain.append(_text(None, f"{kind.upper()} {spec.ratio}", font=font,
                               colour="white@0.55" if opaque else ink,
                               size=tag, x=stamp_x, y=stamp_y))

        if kind == "disc":
            # Cut the ring out last, so everything above is clipped by it.
            # `between` returns 1/0, hence the multiply.
            radius, hole = min(width, height) / 2.0, min(width, height) * 0.11
            chain.append("format=rgba")
            chain.append(
                f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)'"
                f":a='255*between(hypot(X-{width / 2:.1f},Y-{height / 2:.1f})"
                f",{hole:.1f},{radius:.1f})'")
        elif spec.transparent:
            # Both ends are needed: the colour source negotiates its own pixel
            # format, so `black@0` alone gets flattened onto opaque black
            # without a word of complaint — and the result looks perfectly
            # fine, which is how it goes unnoticed.
            chain.append("format=rgba")

        # The whole graph goes through a script file rather than argv: filter
        # escaping and shell escaping do not compose, and a library path is
        # allowed to contain the characters that break both.
        graph = os.path.join(workdir, "graph.txt")
        with open(graph, "w", encoding="utf-8") as fh:
            fh.write(",".join(chain) + "[vout]\n")

        argv = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-y"]
        if photo:
            argv += ["-i", photo]
        argv += ["-filter_complex_script", graph, "-map", "[vout]",
                 "-frames:v", "1"]
        argv += ["-pix_fmt", "rgba"] if spec.transparent else ["-q:v", "4"]
        argv += [tmp]

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        ff.run(argv, verbose=cfg.verbose, timeout=180)
        os.replace(tmp, out_path)
        global _drawn
        with _drawn_lock:
            _drawn += 1
        return out_path
    except (ff.FFmpegError, OSError):
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if os.path.exists(tmp):
            os.unlink(tmp)


def folder_images(folder: str, key: str, title: str, cfg, *,
                  kinds: tuple[str, ...] = SETS["movie"],
                  subtitle: str = "", logo_style: str | None = None,
                  seq: int | None = None) -> list[str]:
    """The image set for an item that owns a folder: `poster.jpg` and friends."""
    written = []
    for kind in kinds:
        got = draw(kind, key, title, os.path.join(folder, filename(kind)), cfg,
                   style=logo_style, subtitle=subtitle, seq=seq)
        if got:
            written.append(got)
    return written


def sidecar_images(media_path: str, key: str, title: str, cfg, *,
                   kinds: tuple[str, ...] = SETS["movie"],
                   subtitle: str = "", logo_style: str | None = None,
                   seq: int | None = None) -> list[str]:
    """The image set for an item that is a loose file: `<name>-poster.jpg`.

    The only shape available to an item sharing a folder with others, and the
    one a client is most likely to have never been tested against.
    """
    folder = os.path.dirname(media_path) or "."
    stem = os.path.splitext(os.path.basename(media_path))[0]
    written = []
    for kind in kinds:
        got = draw(kind, key, title,
                   os.path.join(folder, sidecar_name(stem, kind)), cfg,
                   style=logo_style, subtitle=subtitle, seq=seq)
        if got:
            written.append(got)
    return written


def season_images(series_folder: str, season_no: int, key: str, title: str,
                  cfg, *, kinds: tuple[str, ...] = SETS["season"],
                  subtitle: str = "", seq: int | None = None) -> list[str]:
    """Season artwork in the *series* folder, which is where Jellyfin looks first.

    The other spelling — `Season 01/poster.jpg` — also resolves, and both are
    worth having in the library. `libraries.py` picks one per show so each
    path through `PopulateSeasonImagesFromSeriesFolder` gets exercised.
    """
    written = []
    for kind in kinds:
        if kind not in SEASON_STEM:
            continue
        got = draw(kind, key, title,
                   os.path.join(series_folder, season_name(season_no, kind)),
                   cfg, subtitle=subtitle, seq=seq)
        if got:
            written.append(got)
    return written
