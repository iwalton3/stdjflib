"""Turn a Recipe into a media file.

Everything is synthesised from lavfi sources, so a build needs no network and
no source material. Two decisions shape this module:

**The clip describes itself.** Each file burns its title, its stream layout and
a running timecode into the picture. When a client plays the wrong file, or
picks the wrong media source out of several, you can see it without going back
to the manifest.

**Each audio channel gets its own pitch.** `aevalsrc` generates one sine per
channel rather than upmixing a mono tone, so a 5.1 file is six distinguishable
notes. That turns "is the centre channel actually on the centre speaker" and
"did the downmix drop the surrounds" into things you can hear.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile

from . import ff, subs, vobsub
from .recipes import Recipe, Sub

# Distinct pitch per channel, in the order ffmpeg lays each layout out.
# A major scale, so a missing channel is audible as a gap rather than a
# subtly different chord.
CHANNEL_TONES = {
    1: [440],
    2: [440, 554],
    6: [440, 554, 659, 110, 784, 880],       # FL FR FC LFE BL BR
    8: [440, 554, 659, 110, 784, 880, 988, 1047],
}
CHANNEL_LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}
CHANNEL_NAMES = {
    1: ["C"],
    2: ["L", "R"],
    6: ["FL", "FR", "FC", "LFE", "BL", "BR"],
    8: ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
}

# Containers whose ffmpeg muxer name differs from the extension we want on disk.
# `.mkv` is the one that bites: the muxer is `matroska`, and `-f mkv` fails with
# "Requested output format 'mkv' is not known" rather than anything about muxers.
MUXERS = {
    "mkv": "matroska",
    "ts": "mpegts",
    "m2ts": "mpegts",
    "wmv": "asf",
    "ogv": "ogg",
    "3gp": "3gp",
    # An audiobook. `-f m4b` fails the same way `-f mkv` does — "Error
    # initializing the muxer for x.m4b: Invalid argument", which says nothing
    # about muxers — and `ipod` is what ffmpeg picks for the extension when
    # `-f` is absent. `mp4` also works and writes the same chapters; `ipod` is
    # here because it stamps the MPEG-4 audio brand rather than `isom`, which
    # is what a real `.m4b` carries.
    "m4b": "ipod",
    "m4a": "ipod",
}

# Codecs that ffmpeg refuses without an explicit opt-in.
EXPERIMENTAL = {"dca", "truehd", "opus"}

# Highest channel count each encoder will actually accept. ffmpeg reports the
# refusal as "Specified channel layout '7.1' is not supported", then fails the
# whole conversion with a generic -22 several lines later — so without this
# table an over-wide recipe looks like a build bug rather than a codec limit.
MAX_CHANNELS = {
    "ac3": 6,
    "eac3": 6,
    "truehd": 6,
    "dca": 6,
    "mp2": 2,
    "libmp3lame": 2,
    "wmav2": 2,
    "libvorbis": 8,
    "libopus": 8,
    "aac": 8,
    "libfdk_aac": 8,
    "flac": 8,
    "alac": 8,
}


def channel_limit(recipe) -> str | None:
    """Why this recipe cannot be built, if an audio track is too wide."""
    for a in recipe.audios:
        cap = MAX_CHANNELS.get(a.encoder)
        if cap is not None and a.channels > cap:
            return (f"{a.encoder} accepts at most {cap} channels, "
                    f"recipe asks for {a.channels}")
    return None


@dataclasses.dataclass
class Result:
    recipe: Recipe
    path: str | None
    skipped: str | None = None
    sidecars: tuple[str, ...] = ()
    bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.path is not None and self.skipped is None


def muxer_for(container: str) -> str:
    return MUXERS.get(container, container)


def _fps_float(fps: str) -> float:
    if "/" in fps:
        num, den = fps.split("/")
        return float(num) / float(den)
    return float(fps)


# --------------------------------------------------------------------------
# Filter graph
# --------------------------------------------------------------------------

def _video_filters(rec: Recipe, label_file: str | None,
                   font_file: str | None = None) -> list[str]:
    """The chain applied to the synthetic video source."""
    v = rec.video
    assert v is not None
    chain: list[str] = []

    if v.telecine:
        # Source runs at film rate; telecine takes it to 29.97 with 3:2 pulldown.
        chain.append("telecine=pattern=23")
    elif v.interlace:
        mode = "interleave_top" if v.interlace == "tff" else "interleave_bottom"
        chain.append(f"tinterlace=mode={mode}")

    if label_file:
        # Static description, read from a file so a title containing ':' or '%'
        # cannot corrupt the graph. Sized relative to the frame so it stays
        # legible from 240p to 8K.
        size = max(14, v.height // 22)
        # An explicit fontfile is not optional. Without it drawtext falls back
        # to whatever freetype picks, which on most systems has no CJK — and
        # these labels name audio tracks in their own language, so the
        # Japanese one comes out as tofu boxes while everything else looks fine.
        font = f":fontfile='{font_file}'" if font_file else ""
        # Offset below testsrc2's own burned-in counter, which sits in the
        # top-left corner and is worth keeping legible rather than covering.
        chain.append(
            f"drawtext=textfile='{label_file}'{font}:fontcolor=white"
            f":fontsize={size}"
            f":box=1:boxcolor=black@0.55:boxborderw={max(4, size // 4)}"
            f":x={size // 2}:y={size * 2}:line_spacing={max(2, size // 6)}"
        )
        # Running timecode and frame number. Written here rather than in the
        # label file because it has to be re-evaluated per frame.
        chain.append(
            f"drawtext=text='%{{pts\\:hms}}  frame %{{n}}'{font}"
            f":fontcolor=yellow"
            f":fontsize={size}:box=1:boxcolor=black@0.55"
            f":boxborderw={max(4, size // 4)}:x={size // 2}:y=h-{size * 2}"
        )

    if v.sar:
        chain.append(f"setsar={v.sar.replace(':', '/')}")

    chain.append(f"format={v.pix_fmt}")
    return chain


def _audio_source(a, duration: int) -> tuple[str, str]:
    """An `aevalsrc` giving each channel its own pitch. Returns (spec, layout)."""
    layout = CHANNEL_LAYOUTS.get(a.channels, "stereo")
    tones = CHANNEL_TONES.get(a.channels, CHANNEL_TONES[2])
    # Offset every track's pitches so two tracks are never the same note.
    shift = a.tone / 440.0
    exprs = "|".join(
        f"0.25*sin(2*PI*{int(t * shift)}*t)" for t in tones[: a.channels]
    )
    return (f"aevalsrc=exprs={exprs}:c={layout}:s={a.rate}:d={duration}", layout)


# --------------------------------------------------------------------------
# Label text
# --------------------------------------------------------------------------

def describe(rec: Recipe) -> str:
    """The block burned into the picture and written beside the file."""
    lines = [rec.title]
    if rec.video:
        v = rec.video
        bits = [v.encoder, f"{v.width}x{v.height}", f"{_fps_float(v.fps):g}fps",
                v.pix_fmt]
        if v.sar:
            bits.append(f"SAR {v.sar}")
        if v.interlace:
            bits.append(f"interlaced {v.interlace.upper()}")
        if v.telecine:
            bits.append("3:2 pulldown")
        if v.hdr:
            bits.append(v.hdr.upper())
        lines.append("  ".join(bits))
    else:
        lines.append("no video stream")

    for i, a in enumerate(rec.audios):
        names = CHANNEL_NAMES.get(a.channels, [])
        chans = CHANNEL_LAYOUTS.get(a.channels, f"{a.channels}ch")
        tag = f"A{i}: {a.encoder} {chans} {a.lang}"
        if a.title:
            tag += f" ({a.title})"
        if a.default:
            tag += " [default]"
        if names:
            tag += "  " + " ".join(names)
        lines.append(tag)
    if not rec.audios:
        lines.append("no audio stream")

    for i, s in enumerate(rec.subs):
        tag = f"S{i}: {s.codec} {s.lang}"
        if s.external:
            tag += " (external)"
        if s.forced:
            tag += " [forced]"
        if s.default:
            tag += " [default]"
        lines.append(tag)

    lines.append(f"container: {rec.container}   key: {rec.key}")
    return "\n".join(lines)


def _ffmetadata_escape(value: str) -> str:
    """FFMETADATA treats `= ; # \\` and a newline as syntax.

    Nothing here contains any of them today, and a title that did would not
    fail — it would produce a file tagged with something subtly different from
    what the recipe asked for, which is the failure this whole module is
    written to avoid.
    """
    for ch in ("\\", "=", ";", "#", "\n"):
        value = value.replace(ch, "\\" + ch)
    return value


def _chapter_metadata(rec: Recipe) -> str:
    out = [";FFMETADATA1", f"title={_ffmetadata_escape(rec.title)}"]
    # Global container tags. The same file carries these and the chapters
    # because `-map_metadata` takes one input, and a second metadata input
    # would replace the first rather than merge with it.
    for name, value in rec.container_tags:
        if name == "title":
            out[1] = f"title={_ffmetadata_escape(value)}"
        else:
            out.append(f"{name}={_ffmetadata_escape(str(value))}")
    if not rec.chapters:
        return "\n".join(out) + "\n"
    per = (rec.duration * 1000) // rec.chapters
    for i in range(rec.chapters):
        start = i * per
        end = (i + 1) * per - 1 if i < rec.chapters - 1 else rec.duration * 1000
        out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                f"title=Chapter {i + 1}"]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Subtitle handling
# --------------------------------------------------------------------------

def sidecar_name(base: str, s: Sub, ext: str) -> str:
    """Jellyfin reads language and flags out of the sidecar filename.

    `Movie.eng.forced.srt` — the tokens between the stem and the extension are
    the language and any of default/forced/sdh/cc. Getting this shape right is
    the whole point of the external-subtitle cases.
    """
    parts = [base, s.lang]
    if s.forced:
        parts.append("forced")
    if s.default:
        parts.append("default")
    return ".".join(parts) + "." + ext


BITMAP_SUBS = {"dvdsub", "dvbsub", "xsub"}


def _write_subs(rec: Recipe, workdir: str, base: str, outdir: str, cfg
                ) -> tuple[list[tuple[Sub, str]], list[str]]:
    """Write every subtitle track. Embedded ones go to workdir, sidecars to outdir.

    Bitmap tracks go through `vobsub`, because ffmpeg cannot encode text into
    an image format at all. Once a VobSub exists it *is* a bitmap source, so
    dvbsub and xsub are reachable from it by ordinary transcoding.
    """
    embedded: list[tuple[Sub, str]] = []
    sidecars: list[str] = []
    for i, s in enumerate(rec.subs):
        label = s.title or s.lang

        if s.codec in BITMAP_SUBS:
            if not cfg.font_file:
                continue  # nothing to rasterise with; skip rather than fail
            cues = subs.cues(rec.duration, s.script, s.lang, label,
                             forced=s.forced)
            width = rec.video.width if rec.video else 1280
            height = rec.video.height if rec.video else 720
            target = (os.path.join(outdir, f"{base}.{s.lang}")
                      if s.external else os.path.join(workdir, f"sub{i}"))
            # The track is in one known language, so ask for a font that
            # covers that script rather than hoping the general one does.
            from .config import font_for_lang

            font = font_for_lang(s.lang) or cfg.font_file
            idx, sub_file = vobsub.write(
                cues, target, width, height, font,
                lang=s.lang[:2], ffmpeg=cfg.ffmpeg,
                fontsize=max(20, height // 26),
            )
            if s.external:
                sidecars += [idx, sub_file]
            else:
                embedded.append((s, idx))
            continue

        text, ext = subs.render(s.codec, rec.duration, s.script, s.lang, label,
                                forced=s.forced)
        if s.external:
            path = os.path.join(outdir, sidecar_name(base, s, ext))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            sidecars.append(path)
        else:
            path = os.path.join(workdir, f"sub{i}.{ext}")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            embedded.append((s, path))
    return embedded, sidecars


# --------------------------------------------------------------------------
# Command assembly
# --------------------------------------------------------------------------

def build_command(rec: Recipe, out_path: str, workdir: str, *,
                  font_file: str | None = None,
                  video_encoder: str | None = None) -> list[str]:
    """Assemble the whole ffmpeg invocation for one recipe.

    `video_encoder` overrides the recipe's choice, which is how the optional
    NVENC path gets in without the recipes knowing anything about it.
    """
    argv = ["-hide_banner", "-nostdin", "-y"]
    filter_parts: list[str] = []
    maps: list[str] = []
    codec_args: list[str] = []
    idx = 0
    v = rec.video

    # --- video input ---
    if v is not None:
        src_fps = v.fps
        if v.telecine:
            src_fps = "24000/1001"
        elif v.interlace:
            # tinterlace consumes two frames per output frame.
            src_fps = f"2*({v.fps})" if "/" not in v.fps else \
                f"{int(v.fps.split('/')[0]) * 2}/{v.fps.split('/')[1]}"
        label_file = None
        if font_file:
            label_file = os.path.join(workdir, "label.txt").replace("\\", "/")
            with open(label_file, "w", encoding="utf-8") as fh:
                fh.write(describe(rec) + "\n")
        argv += ["-f", "lavfi", "-i",
                 f"testsrc2=size={v.width}x{v.height}:rate={src_fps}"]
        chain = _video_filters(rec, label_file, font_file)
        filter_parts.append(f"[{idx}:v]{','.join(chain)}[vout]")
        maps += ["-map", "[vout]"]
        idx += 1

    # --- audio inputs ---
    audio_idx = []
    for a in rec.audios:
        spec, _layout = _audio_source(a, rec.duration)
        argv += ["-f", "lavfi", "-i", spec]
        audio_idx.append(idx)
        maps += ["-map", f"{idx}:a"]
        idx += 1

    # --- subtitle inputs ---
    # `build` has already written these and parked them on the recipe; this
    # function only maps them, so it stays a pure function of (recipe, paths)
    # and the tests can call it without touching the filesystem.
    for s, path in getattr(rec, "_embedded", []):
        argv += ["-i", path]
        maps += ["-map", f"{idx}:s"]
        idx += 1

    # --- chapters / global metadata ---
    meta_path = os.path.join(workdir, "meta.ini")
    with open(meta_path, "w", encoding="utf-8") as fh:
        fh.write(_chapter_metadata(rec))
    argv += ["-i", meta_path]
    meta_idx = idx
    idx += 1

    # --- attachment ---
    # `-attach` has to come after every `-i`. Put an input after it and ffmpeg
    # fails with "Error opening input files: Invalid argument", which points at
    # the input rather than at the ordering.
    if rec.attach_font and font_file and os.path.exists(font_file):
        argv += ["-attach", font_file]
        codec_args += ["-metadata:s:t:0", "mimetype=application/x-truetype-font",
                       "-metadata:s:t:0",
                       f"filename={os.path.basename(font_file)}"]

    if filter_parts:
        script = os.path.join(workdir, "filter.txt")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(";".join(filter_parts))
        argv += ["-filter_complex_script", script]

    argv += maps
    argv += ["-map_metadata", str(meta_idx)]
    argv += ["-t", str(rec.duration)]

    # --- video codec ---
    if v is not None:
        encoder = video_encoder or v.encoder
        codec_args += ["-c:v", encoder, "-b:v", v.bitrate, "-r", v.fps]
        # Encoder-specific args (x265-params, profiles) only make sense for the
        # encoder they were written for.
        if encoder == v.encoder:
            codec_args += list(v.args)
            if v.hdr:
                codec_args += _hdr_args(v)
        elif v.hdr:
            codec_args += ["-color_primaries", "bt2020", "-colorspace", "bt2020nc",
                           "-color_trc",
                           "smpte2084" if v.hdr == "hdr10" else "arib-std-b67"]
        if v.interlace:
            codec_args += ["-flags", "+ilme+ildct",
                           "-top", "1" if v.interlace == "tff" else "0"]
            if encoder == "libx264":
                codec_args += ["-x264opts",
                               "tff=1" if v.interlace == "tff" else "bff=1"]
        if encoder in ("libx264", "libx265"):
            # Short GOPs keep seeking honest on files this small.
            codec_args += ["-g", str(max(12, int(_fps_float(v.fps) * 2)))]

    # --- audio codecs ---
    for n, a in enumerate(rec.audios):
        codec_args += [f"-c:a:{n}", a.encoder, f"-ac:a:{n}", str(a.channels),
                       f"-ar:a:{n}", str(a.rate)]
        if a.bitrate:
            codec_args += [f"-b:a:{n}", a.bitrate]
        elif a.encoder in ("aac", "ac3", "eac3", "libopus", "libvorbis",
                           "libmp3lame", "mp2", "wmav2"):
            codec_args += [f"-b:a:{n}", _default_audio_bitrate(a)]
        codec_args += [f"-metadata:s:a:{n}", f"language={a.lang}"]
        if a.title:
            codec_args += [f"-metadata:s:a:{n}", f"title={a.title}"]
        codec_args += [f"-disposition:a:{n}", "default" if a.default else "0"]

    # --- subtitle codecs ---
    for n, (s, _path) in enumerate(getattr(rec, "_embedded", [])):
        if s.codec == "dvdsub":
            # The source already *is* dvd_subtitle, so copy rather than
            # re-encode; a round trip through the encoder gains nothing.
            codec_args += [f"-c:s:{n}", "copy"]
        else:
            codec_args += [f"-c:s:{n}", s.codec]
        codec_args += [f"-metadata:s:s:{n}", f"language={s.lang}"]
        if s.title:
            codec_args += [f"-metadata:s:s:{n}", f"title={s.title}"]
        flags = []
        if s.default:
            flags.append("default")
        if s.forced:
            flags.append("forced")
        codec_args += [f"-disposition:s:{n}", "+".join(flags) if flags else "0"]

    argv += codec_args

    if any(a.encoder in EXPERIMENTAL for a in rec.audios):
        argv += ["-strict", "-2"]

    muxer = muxer_for(rec.container)
    argv += ["-f", muxer]
    if muxer in ("mp4", "mov", "3gp", "ipod"):
        # Put the index at the front so the file is seekable without a range
        # request for the tail.
        argv += ["-movflags", "+faststart"]
    if muxer == "mpegts":
        argv += ["-mpegts_flags", "+resend_headers"]

    argv += [out_path]
    return argv


def _default_audio_bitrate(a) -> str:
    per_channel = {"aac": 64, "libopus": 48, "libvorbis": 64, "libmp3lame": 96,
                   "mp2": 96, "wmav2": 96, "ac3": 96, "eac3": 96}
    kb = per_channel.get(a.encoder, 64) * max(1, a.channels)
    return f"{min(kb, 1536)}k"


def _hdr_args(v) -> list[str]:
    if v.hdr == "hdr10":
        params = (
            "log-level=error:hdr10=1:hdr10-opt=1:repeat-headers=1"
            ":colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"
            ":master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
            "WP(15635,16450)L(10000000,1):max-cll=1000,400"
        )
        return ["-color_primaries", "bt2020", "-color_trc", "smpte2084",
                "-colorspace", "bt2020nc", "-x265-params", params]
    if v.hdr == "hlg":
        return ["-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
                "-colorspace", "bt2020nc",
                "-x265-params", ("log-level=error:colorprim=bt2020"
                                 ":transfer=arib-std-b67:colormatrix=bt2020nc")]
    return []


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build(rec: Recipe, out_path: str, cfg, *, allow_hw: bool = False) -> Result:
    """Produce one file. Missing encoders skip rather than fail the build.

    `allow_hw` is opt-in per file rather than global: the codec-matrix entries
    must come out of the software encoder they name, because *which encoder
    produced them* is part of what they test.
    """
    gaps = ff.missing(cfg.ffmpeg, *rec.encoders)
    if gaps:
        return Result(rec, None, skipped=f"ffmpeg lacks {', '.join(gaps)}")

    limit = channel_limit(rec)
    if limit:
        raise ValueError(f"{rec.key}: {limit}")

    hw_encoder = None
    if allow_hw and rec.video:
        candidate = cfg.hw_encoder(rec.video.encoder)
        if candidate and ff.have(cfg.ffmpeg, candidate):
            hw_encoder = candidate

    if rec.broken == "zero":
        if not cfg.dry_run:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").close()
        return Result(rec, out_path, bytes=0)

    outdir = os.path.dirname(out_path)
    base = os.path.splitext(os.path.basename(out_path))[0]
    workdir = tempfile.mkdtemp(prefix="stdjflib-")
    try:
        if not cfg.dry_run:
            os.makedirs(outdir, exist_ok=True)
        embedded, sidecars = _write_subs(rec, workdir, base,
                                         outdir if not cfg.dry_run else workdir,
                                         cfg)
        # build_command reads the written paths off the recipe; a frozen
        # dataclass will not take an attribute, so pass it through object.
        object.__setattr__(rec, "_embedded", embedded)

        tmp = ff.temp_path(out_path)
        argv = [cfg.ffmpeg] + build_command(rec, tmp, workdir,
                                            font_file=cfg.font_file,
                                            video_encoder=hw_encoder)
        if cfg.dry_run:
            return Result(rec, out_path, sidecars=tuple(sidecars))

        ff.run(argv, verbose=cfg.verbose)

        if rec.broken == "truncate":
            size = os.path.getsize(tmp)
            with open(tmp, "r+b") as fh:
                fh.truncate(int(size * 0.4))

        os.replace(tmp, out_path)
        return Result(rec, out_path, sidecars=tuple(sidecars),
                      bytes=os.path.getsize(out_path))
    finally:
        object.__setattr__(rec, "_embedded", [])
        shutil.rmtree(workdir, ignore_errors=True)
