"""The declarative matrix of generated media.

Every entry here becomes one file in the library, built from ffmpeg's synthetic
sources — no downloads, no copyright, byte-stable across machines with the same
ffmpeg. Each clip burns its own description into the picture, so playing a file
tells you what it is without going back to the manifest, and `notes` becomes the
item's plot in Jellyfin — browse the library and every item explains what it
exercises.

Adding coverage means adding a Recipe here. Nothing else needs to change:
`generate.py` reads the dataclasses, `layout.py` decides the filename, and
`nfo.py` writes the metadata.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Video:
    encoder: str = "libx264"
    width: int = 1280
    height: int = 720
    fps: str = "24"
    pix_fmt: str = "yuv420p"
    args: tuple[str, ...] = ()
    sar: str | None = None          # forced sample aspect ratio, e.g. "16:11"
    interlace: str | None = None    # "tff" | "bff"
    telecine: bool = False
    hdr: str | None = None          # "hdr10" | "hlg"
    bitrate: str = "1500k"

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height} {self.fps}fps {self.pix_fmt}"


@dataclasses.dataclass(frozen=True)
class Audio:
    encoder: str = "aac"
    channels: int = 2
    rate: int = 48000
    lang: str = "eng"
    title: str = ""
    default: bool = False
    bitrate: str | None = None
    tone: int = 440  # Hz, so you can hear which track you are on


@dataclasses.dataclass(frozen=True)
class Sub:
    codec: str = "subrip"           # subrip|ass|webvtt|mov_text|dvdsub|dvbsub|xsub
    lang: str = "eng"
    title: str = ""
    external: bool = False
    forced: bool = False
    default: bool = False
    script: str = "latin"           # which sample text to use


@dataclasses.dataclass(frozen=True)
class Recipe:
    key: str                        # stable slug; drives filename and NFO id
    title: str
    group: str                      # subfolder / collection this belongs to
    notes: str                      # what it exercises -> becomes the plot
    container: str = "mkv"
    video: Video | None = Video()
    audios: tuple[Audio, ...] = (Audio(),)
    subs: tuple[Sub, ...] = ()
    duration: int = 20              # seconds
    chapters: int = 0
    attach_font: bool = False
    tier: str = "minimal"
    year: int = 2020
    broken: str | None = None       # "truncate" | "empty" | "zero"
    # Which library builder places this file. Everything in the matrix lands
    # in `Test Media/`; a recipe naming anything else is placed by that
    # library's own builder, because where it sits in the tree *is* what it
    # tests. It still belongs here rather than in `libraries.py` so that
    # `verify` re-probes it against a declared codec, channel count and
    # chapter count like everything else — `verify` looks a manifest entry up
    # by key in `all_recipes()`, and a recipe kept out of that list is a file
    # nothing ever checks.
    library: str = "Test Media"
    # Global container tags, written through the same FFMETADATA file the
    # chapters go in. Only the audiobooks use them, and they are not
    # decoration: `AudioFileProber` reads an audiobook's `album_artist` as its
    # Author, its `composer` as the Narrator and any remaining `artist` as
    # cast, and `album` is the only thing that groups the files of a
    # multi-file rip — the server sets no `SeriesName` on an AudioBook.
    container_tags: tuple[tuple[str, str], ...] = ()

    @property
    def encoders(self) -> tuple[str, ...]:
        names = []
        if self.video:
            names.append(self.video.encoder)
        names.extend(a.encoder for a in self.audios)
        return tuple(names)


# --------------------------------------------------------------------------
# Video codecs
# --------------------------------------------------------------------------

def _video_codecs() -> list[Recipe]:
    common = dict(group="Video Codecs", duration=20)
    out = [
        Recipe(
            key="v-h264-baseline", title="H.264 Baseline",
            notes="H.264 Constrained Baseline L3.0, no B-frames. The profile every "
                  "device claims to handle; a client that fails here has a decoder "
                  "problem, not a compatibility one.",
            video=Video(args=("-profile:v", "baseline", "-level", "3.0",
                              "-bf", "0", "-x264opts", "cabac=0")),
            **common,
        ),
        Recipe(
            key="v-h264-main", title="H.264 Main",
            notes="H.264 Main L3.1 with CABAC and B-frames.",
            video=Video(args=("-profile:v", "main", "-level", "3.1")),
            **common,
        ),
        Recipe(
            key="v-h264-high", title="H.264 High",
            notes="H.264 High L4.0 — the ordinary case for 1080p content.",
            video=Video(width=1920, height=1080,
                        args=("-profile:v", "high", "-level", "4.0")),
            **common,
        ),
        Recipe(
            key="v-h264-high10", title="H.264 High 10 (10-bit)",
            notes="10-bit H.264. Hardware decoders overwhelmingly do NOT support "
                  "this, so a correct client should fall back to software or ask "
                  "the server to transcode. Direct-playing it on a device that "
                  "reports h264 support is the bug this file catches.",
            video=Video(pix_fmt="yuv420p10le", args=("-profile:v", "high10")),
            **common,
        ),
        Recipe(
            key="v-h264-422", title="H.264 High 4:2:2",
            notes="4:2:2 chroma subsampling. Rare in the wild, routinely mishandled.",
            video=Video(pix_fmt="yuv422p", args=("-profile:v", "high422")),
            tier="full", **common,
        ),
        Recipe(
            key="v-hevc-main", title="HEVC Main (8-bit)",
            notes="H.265 Main. Exercises the second-most-common direct play path.",
            video=Video(encoder="libx265", args=("-x265-params", "log-level=error")),
            **common,
        ),
        Recipe(
            key="v-hevc-main10", title="HEVC Main 10 (10-bit)",
            notes="10-bit H.265, the normal container for HDR. Widely supported in "
                  "hardware, unlike 10-bit H.264.",
            video=Video(encoder="libx265", pix_fmt="yuv420p10le",
                        args=("-x265-params", "log-level=error")),
            **common,
        ),
        Recipe(
            key="v-av1", title="AV1",
            notes="AV1 via SVT-AV1. Newer devices decode it in hardware, most do "
                  "not; a good test of the codec-support negotiation.",
            video=Video(encoder="libsvtav1", args=("-preset", "10")),
            **common,
        ),
        Recipe(
            key="v-vp9", title="VP9",
            notes="VP9 in WebM — what a browser-based client is happiest with and "
                  "what most set-top boxes refuse.",
            container="webm",
            video=Video(encoder="libvpx-vp9", args=("-deadline", "realtime",
                                                    "-cpu-used", "8")),
            audios=(Audio(encoder="libopus"),),
            **common,
        ),
        Recipe(
            key="v-mpeg2", title="MPEG-2",
            notes="MPEG-2 video, as it arrives from DVD rips and broadcast capture.",
            container="ts",
            video=Video(encoder="mpeg2video", width=720, height=480, fps="29.97",
                        bitrate="4000k"),
            audios=(Audio(encoder="mp2"),),
            **common,
        ),
        Recipe(
            key="v-mpeg4-asp", title="MPEG-4 ASP (Xvid)",
            notes="MPEG-4 part 2 in AVI — the shape of a decade of downloads. "
                  "Jellyfin usually transcodes this rather than direct-playing it.",
            container="avi",
            video=Video(encoder="mpeg4", width=640, height=480,
                        args=("-vtag", "XVID")),
            audios=(Audio(encoder="libmp3lame"),),
            **common,
        ),
        Recipe(
            key="v-theora", title="Theora",
            notes="Theora in Ogg. Obsolete but still parsed; a decent negative test.",
            container="ogv",
            video=Video(encoder="libtheora", width=854, height=480),
            audios=(Audio(encoder="libvorbis"),),
            tier="standard", **common,
        ),
        Recipe(
            key="v-wmv2", title="WMV 8",
            notes="Windows Media Video 8 in ASF. Always transcoded; exercises the "
                  "path where the container itself is the blocker.",
            container="wmv",
            video=Video(encoder="wmv2", width=640, height=480),
            audios=(Audio(encoder="wmav2"),),
            tier="standard", **common,
        ),
        Recipe(
            key="v-flv1", title="Sorenson Spark (FLV)",
            notes="FLV1 in a Flash container.",
            container="flv",
            video=Video(encoder="flv", width=640, height=480),
            audios=(Audio(encoder="libmp3lame", rate=44100),),
            tier="standard", **common,
        ),
        Recipe(
            key="v-ffv1", title="FFV1 lossless",
            notes="Lossless FFV1 — huge bitrate for its resolution, which is the "
                  "point: it forces a bandwidth decision rather than a codec one.",
            video=Video(encoder="ffv1", width=640, height=480),
            audios=(Audio(encoder="flac"),),
            tier="full", **common,
        ),
        Recipe(
            key="v-prores", title="Apple ProRes",
            notes="ProRes 422 in MOV. Editorial intermediate; very high bitrate.",
            container="mov",
            # ProRes 422 is 10-bit 4:2:2 by definition; declaring yuv420p here
            # would be a lie the encoder silently corrects.
            video=Video(encoder="prores_ks", pix_fmt="yuv422p10le",
                        args=("-profile:v", "2")),
            audios=(Audio(encoder="pcm_s16le"),),
            tier="full", **common,
        ),
    ]
    return out


# --------------------------------------------------------------------------
# Audio codecs and channel layouts
# --------------------------------------------------------------------------

def _audio_codecs() -> list[Recipe]:
    common = dict(group="Audio Codecs", duration=20,
                  video=Video(width=854, height=480, bitrate="800k"))
    specs = [
        ("a-aac-stereo", "AAC 2.0", Audio(encoder="aac", channels=2), "minimal",
         "AAC-LC stereo — the baseline every client direct-plays."),
        ("a-aac-51", "AAC 5.1", Audio(encoder="aac", channels=6), "minimal",
         "AAC 5.1. Channel-count handling without a codec change."),
        ("a-ac3-51", "AC-3 5.1", Audio(encoder="ac3", channels=6), "minimal",
         "Dolby Digital 5.1 — the common case for passthrough to a receiver."),
        ("a-eac3-51", "E-AC-3 5.1", Audio(encoder="eac3", channels=6), "minimal",
         "Dolby Digital Plus 5.1. Streaming's default; many devices claim it and "
         "then stutter."),
        # Not E-AC-3: ffmpeg's encoder caps at 5.1 and rejects a 7.1 layout
        # outright, so the eight-channel cases use codecs that can carry them.
        ("a-aac-71", "AAC 7.1", Audio(encoder="aac", channels=8), "standard",
         "Eight channels of AAC. Exercises downmix when the sink has fewer, and "
         "the side/back channel distinction that gets flattened in reporting."),
        # TrueHD 7.1 is not here for the same reason E-AC-3 7.1 is not:
        # ffmpeg's encoder tops out at 5.1. FLAC carries the lossless-8-channel
        # case instead, which is the one a client can actually be handed.
        ("a-flac-71", "FLAC 7.1", Audio(encoder="flac", channels=8), "full",
         "Lossless eight-channel FLAC — large, and legal only in a few containers."),
        ("a-dts-51", "DTS 5.1", Audio(encoder="dca", channels=6), "standard",
         "DTS Coherent Acoustics. ffmpeg's encoder is experimental, which is why "
         "this file needs -strict -2 to build."),
        ("a-truehd-51", "TrueHD 5.1", Audio(encoder="truehd", channels=6), "standard",
         "Dolby TrueHD, lossless. Enormous, passthrough-only in practice."),
        ("a-flac-stereo", "FLAC 2.0", Audio(encoder="flac", channels=2), "minimal",
         "Lossless FLAC stereo in a video container."),
        ("a-flac-51", "FLAC 5.1", Audio(encoder="flac", channels=6), "standard",
         "Multichannel FLAC — legal, uncommon, and a frequent parser surprise."),
        ("a-opus-stereo", "Opus 2.0", Audio(encoder="libopus", channels=2), "minimal",
         "Opus stereo. Fine in MKV and WebM, illegal in most other containers."),
        ("a-opus-51", "Opus 5.1", Audio(encoder="libopus", channels=6), "standard",
         "Opus 5.1, with the channel mapping family that trips naive parsers."),
        ("a-vorbis", "Vorbis 2.0", Audio(encoder="libvorbis", channels=2), "standard",
         "Vorbis stereo."),
        ("a-mp3", "MP3 2.0", Audio(encoder="libmp3lame", channels=2, rate=44100),
         "minimal", "MP3 at 44.1 kHz — note the sample rate differs from the 48 kHz "
         "everything else here uses, which is itself worth testing."),
        ("a-mp2", "MP2 2.0", Audio(encoder="mp2", channels=2), "standard",
         "MPEG-1 Layer II, as broadcast delivers it."),
        ("a-alac", "ALAC 2.0", Audio(encoder="alac", channels=2), "standard",
         "Apple Lossless in MP4."),
        ("a-pcm16", "PCM 16-bit", Audio(encoder="pcm_s16le", channels=2), "standard",
         "Uncompressed 16-bit PCM."),
        ("a-pcm24", "PCM 24-bit", Audio(encoder="pcm_s24le", channels=2), "full",
         "24-bit PCM — bit depth beyond what many pipelines assume."),
        ("a-mono", "AAC Mono", Audio(encoder="aac", channels=1), "minimal",
         "Single channel. Upmix and the 'is this really mono' display question."),
    ]
    out = []
    for key, title, audio, tier, notes in specs:
        container = "mkv"
        if audio.encoder == "alac":
            container = "mp4"
        out.append(Recipe(key=key, title=title, notes=notes, tier=tier,
                          container=container, audios=(audio,), **common))
    return out


# --------------------------------------------------------------------------
# Containers
# --------------------------------------------------------------------------

def _containers() -> list[Recipe]:
    common = dict(group="Containers", duration=20,
                  video=Video(width=854, height=480, bitrate="800k"))
    specs = [
        ("mkv", "Matroska", "aac", "minimal",
         "Matroska. The permissive case — almost anything is legal inside."),
        ("mp4", "MP4", "aac", "minimal",
         "MP4 with a moov atom at the front, so it is seekable over HTTP."),
        ("mov", "QuickTime MOV", "aac", "minimal",
         "QuickTime. Structurally MP4's sibling, and routinely special-cased."),
        ("ts", "MPEG-TS", "aac", "minimal",
         "Transport stream. No global header and no duration in the container, so "
         "the client has to learn the length from the server rather than the file."),
        ("m2ts", "BDAV MPEG-TS (.m2ts)", "ac3", "standard",
         "Blu-ray flavoured transport stream. Same bytes as .ts, different "
         "extension — and Jellyfin routes the two differently."),
        ("webm", "WebM", "libopus", "minimal",
         "WebM. Only VP8/VP9/AV1 plus Vorbis/Opus are legal here."),
        ("3gp", "3GP", "aac", "full",
         "3GPP. Mobile-era container, still turns up in phone camera footage."),
    ]
    out = []
    for ext, title, aenc, tier, notes in specs:
        video = Video(width=854, height=480, bitrate="800k")
        if ext == "webm":
            video = dataclasses.replace(video, encoder="libvpx-vp9",
                                        args=("-deadline", "realtime", "-cpu-used", "8"))
        if ext == "3gp":
            video = dataclasses.replace(video, width=640, height=480,
                                        args=("-profile:v", "baseline", "-level", "3.0"))
        out.append(Recipe(
            key=f"c-{ext}", title=title, notes=notes, tier=tier, container=ext,
            audios=(Audio(encoder=aenc),),
            **{**common, "video": video},
        ))
    return out


# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------

def _subtitles() -> list[Recipe]:
    common = dict(group="Subtitles", duration=30,
                  video=Video(width=1280, height=720, bitrate="1200k"))
    out = [
        Recipe(
            key="s-srt-embedded", title="SRT embedded",
            notes="A single SubRip track muxed into MKV. The easy case.",
            subs=(Sub(codec="subrip", lang="eng", title="English", default=True),),
            **common,
        ),
        Recipe(
            key="s-srt-external", title="SRT external sidecar",
            notes="No subtitle track in the file; one .eng.srt beside it. Jellyfin "
                  "discovers it on scan, and the language comes from the filename.",
            subs=(Sub(codec="subrip", lang="eng", title="English", external=True),),
            **common,
        ),
        Recipe(
            key="s-ass-styled", title="ASS with styling",
            notes="Advanced SubStation Alpha using colour, position and karaoke "
                  "timing. A client that renders ASS as plain text loses all of it.",
            subs=(Sub(codec="ass", lang="eng", title="Styled", default=True),),
            **common,
        ),
        Recipe(
            key="s-ass-attached-font", title="ASS with attached font",
            notes="ASS referencing a font shipped as an MKV attachment. Correct "
                  "rendering requires reading the attachment; most clients quietly "
                  "substitute and the subtitles look almost right.",
            subs=(Sub(codec="ass", lang="eng", title="Styled", default=True),),
            attach_font=True, tier="standard", **common,
        ),
        Recipe(
            key="s-vobsub", title="VobSub (image subtitles)",
            notes="DVD bitmap subtitles. Cannot be converted to text without OCR, so "
                  "the server must burn them in or the client must render bitmaps — "
                  "there is no third option, and pretending otherwise is a common bug.",
            subs=(Sub(codec="dvdsub", lang="eng", title="English (bitmap)"),),
            tier="standard", **common,
        ),
        Recipe(
            key="s-vobsub-external", title="VobSub sidecar (.idx/.sub)",
            notes="A VobSub pair beside the video rather than muxed in — how a DVD "
                  "rip usually arrives. Two files that must be treated as one "
                  "track, and a client that lists the .sub as a subtitle of its own "
                  "shows a track that cannot be selected.",
            subs=(Sub(codec="dvdsub", lang="eng", title="English (bitmap)",
                      external=True),),
            tier="standard", **common,
        ),
        Recipe(
            key="s-dvbsub", title="DVB subtitles",
            notes="Broadcast bitmap subtitles in a transport stream.",
            container="ts",
            subs=(Sub(codec="dvbsub", lang="eng", title="DVB"),),
            tier="full", **common,
        ),
        Recipe(
            key="s-mov-text", title="MP4 timed text",
            notes="mov_text in MP4 — the only text subtitle codec MP4 allows.",
            container="mp4",
            subs=(Sub(codec="mov_text", lang="eng", title="English", default=True),),
            **common,
        ),
        Recipe(
            key="s-forced", title="Forced subtitle track",
            notes="A full English track plus a forced track covering only the "
                  "foreign-language lines. A client honouring the forced flag shows "
                  "the short one by default; one that does not shows everything.",
            subs=(
                Sub(codec="subrip", lang="eng", title="English", default=True),
                Sub(codec="subrip", lang="eng", title="Forced", forced=True),
            ),
            **common,
        ),
        Recipe(
            key="s-multilang", title="Nine subtitle languages",
            notes="Nine tracks covering Latin, Cyrillic, Greek, CJK and two "
                  "right-to-left scripts. Exercises language sorting, font fallback "
                  "and bidirectional text all at once.",
            subs=tuple(
                Sub(codec="subrip", lang=lang, title=name, script=script)
                for lang, name, script in (
                    ("eng", "English", "latin"),
                    ("fra", "Français", "latin"),
                    ("deu", "Deutsch", "latin"),
                    ("rus", "Русский", "cyrillic"),
                    ("ell", "Ελληνικά", "greek"),
                    ("jpn", "日本語", "cjk"),
                    ("kor", "한국어", "cjk"),
                    ("heb", "עברית", "rtl"),
                    ("ara", "العربية", "rtl"),
                )
            ),
            tier="standard", **common,
        ),
        Recipe(
            key="s-external-mixed", title="External sidecars, four formats",
            notes="Sidecars in SRT, ASS, VTT and SUB/IDX beside one video, with "
                  "language and 'forced' encoded in the filenames the way Jellyfin "
                  "expects to read them.",
            subs=(
                Sub(codec="subrip", lang="eng", title="English", external=True),
                Sub(codec="ass", lang="fra", title="Français", external=True),
                Sub(codec="webvtt", lang="deu", title="Deutsch", external=True),
                Sub(codec="subrip", lang="spa", title="Español", external=True,
                    forced=True),
            ),
            tier="standard", **common,
        ),
    ]
    return out


# --------------------------------------------------------------------------
# Colour, HDR, frame rate, scan type, aspect
# --------------------------------------------------------------------------

def _color_and_motion() -> list[Recipe]:
    common = dict(duration=20)
    hdr = [
        Recipe(
            key="h-sdr-bt709", title="SDR BT.709", group="HDR and Colour",
            notes="Ordinary SDR with explicit BT.709 tagging — the control for the "
                  "rest of this group.",
            video=Video(encoder="libx265", width=1920, height=1080,
                        args=("-x265-params", "log-level=error",
                              "-color_primaries", "bt709", "-color_trc", "bt709",
                              "-colorspace", "bt709")),
            **common,
        ),
        Recipe(
            key="h-hdr10", title="HDR10 (PQ, BT.2020)", group="HDR and Colour",
            notes="HDR10 with SMPTE ST 2084 transfer, BT.2020 primaries and both "
                  "mastering-display and content-light metadata. On an SDR screen a "
                  "client that ignores the transfer curve renders this washed out and "
                  "grey — which is the visible symptom to look for.",
            video=Video(encoder="libx265", width=1920, height=1080,
                        pix_fmt="yuv420p10le", hdr="hdr10"),
            tier="standard", **common,
        ),
        Recipe(
            key="h-hlg", title="HLG (Hybrid Log-Gamma)", group="HDR and Colour",
            notes="Broadcast HDR. Nominally SDR-compatible, which means a client that "
                  "does nothing at all is closer to right than for HDR10.",
            video=Video(encoder="libx265", width=1920, height=1080,
                        pix_fmt="yuv420p10le", hdr="hlg"),
            tier="standard", **common,
        ),
    ]

    rates = [
        Recipe(
            key=f"f-{slug}", title=f"{label} fps", group="Frame Rates",
            notes=notes, video=Video(fps=fps, width=1280, height=720), tier=tier,
            **common,
        )
        for slug, label, fps, tier, notes in (
            ("23976", "23.976", "24000/1001", "minimal",
             "Film rate as broadcast carries it. The classic source of judder when a "
             "client resamples to 60 Hz instead of matching the display."),
            ("24", "24", "24", "minimal", "True 24 fps."),
            ("25", "25", "25", "minimal", "PAL rate."),
            ("2997", "29.97", "30000/1001", "minimal", "NTSC rate."),
            ("30", "30", "30", "standard", "30 fps."),
            ("50", "50", "50", "standard", "PAL double rate."),
            ("5994", "59.94", "60000/1001", "standard", "NTSC double rate."),
            ("60", "60", "60", "standard", "60 fps."),
            ("120", "120", "120", "full", "120 fps — beyond most display refresh rates."),
        )
    ]

    scan = [
        Recipe(
            key="i-1080i25", title="1080i interlaced (TFF)", group="Scan Types",
            notes="True interlaced 1080i, top field first. Needs deinterlacing; a "
                  "client that skips it shows combing on every horizontal motion.",
            video=Video(encoder="mpeg2video", width=1920, height=1080, fps="25",
                        interlace="tff", bitrate="8000k"),
            audios=(Audio(encoder="ac3", channels=6),), container="ts",
            tier="standard", **common,
        ),
        Recipe(
            key="i-480i2997", title="480i interlaced (BFF)", group="Scan Types",
            notes="NTSC 480i, bottom field first — the field order DV uses, and the "
                  "one that gets assumed away.",
            video=Video(encoder="mpeg2video", width=720, height=480, fps="30000/1001",
                        interlace="bff", bitrate="6000k"),
            audios=(Audio(encoder="ac3", channels=2),), container="ts",
            tier="standard", **common,
        ),
        Recipe(
            key="i-telecine", title="3:2 pulldown (telecined)", group="Scan Types",
            notes="24 fps film telecined to 29.97. Correct handling is inverse "
                  "telecine back to 24, not deinterlacing — doing the latter throws "
                  "away a fifth of the frames.",
            video=Video(encoder="mpeg2video", width=720, height=480, fps="30000/1001",
                        telecine=True, bitrate="6000k"),
            container="ts", tier="full", **common,
        ),
    ]

    aspect = [
        Recipe(
            key="r-4x3", title="4:3", group="Aspect Ratios",
            notes="Square-ish frame. Pillarboxing on a 16:9 display.",
            video=Video(width=640, height=480), **common,
        ),
        Recipe(
            key="r-239", title="2.39:1 Scope", group="Aspect Ratios",
            notes="Anamorphic scope framing, letterboxed on 16:9.",
            video=Video(width=1920, height=804), **common,
        ),
        Recipe(
            key="r-anamorphic-pal", title="Anamorphic PAL (non-square pixels)",
            group="Aspect Ratios",
            notes="720x576 stored pixels displayed at 16:9, so the correct output "
                  "is 1024x576 and the coded resolution is a lie. A client that "
                  "trusts the coded resolution and ignores the sample aspect ratio "
                  "renders everyone too thin — the most common aspect-ratio bug "
                  "there is. This is the broadcast case: MPEG-2 in a transport "
                  "stream, where the aspect ratio is a four-value display code "
                  "rather than a ratio, which is why the sample aspect comes back "
                  "as 64:45 and not some rounder number.",
            video=Video(encoder="mpeg2video", width=720, height=576, fps="25",
                        sar="64:45", bitrate="5000k"),
            container="ts", tier="standard", **common,
        ),
        Recipe(
            key="r-anamorphic-arbitrary", title="Arbitrary sample aspect ratio",
            group="Aspect Ratios",
            notes="The same idea where the container can store any ratio it likes: "
                  "1024x576 coded pixels with a 3:2 sample aspect, displayed at "
                  "1536x576. MPEG-2 could not express this at all — it would round "
                  "to the nearest of its four codes — so this is the case that "
                  "catches a client which handles the handful of broadcast ratios "
                  "and assumes those are all of them.",
            video=Video(width=1024, height=576, fps="25", sar="3:2",
                        bitrate="3000k"),
            container="mkv", tier="standard", **common,
        ),
        Recipe(
            key="r-1x1", title="1:1 square", group="Aspect Ratios",
            notes="A square frame, as social video delivers it.",
            video=Video(width=720, height=720), tier="full", **common,
        ),
    ]

    res = [
        Recipe(
            key=f"z-{name}", title=label, group="Resolutions", notes=notes,
            video=Video(width=w, height=h, bitrate=br), tier=tier, **common,
        )
        for name, label, w, h, br, tier, notes in (
            ("240p", "240p", 426, 240, "300k", "minimal",
             "Very low resolution — upscaling quality, and whether the client bothers."),
            ("480p", "480p SD", 854, 480, "1000k", "minimal", "Standard definition."),
            ("720p", "720p HD", 1280, 720, "2500k", "minimal", "720p."),
            ("1080p", "1080p Full HD", 1920, 1080, "6000k", "minimal", "1080p."),
            ("1440p", "1440p QHD", 2560, 1440, "12000k", "standard",
             "An in-between resolution that bitrate ladders usually have no rung for."),
            ("2160p", "2160p 4K", 3840, 2160, "25000k", "standard",
             "4K. Direct play or a very expensive transcode, and nothing in between."),
            ("4320p", "4320p 8K", 7680, 4320, "60000k", "full",
             "8K. Almost nothing decodes this; the useful outcome is a clean refusal."),
        )
    ]

    return hdr + rates + scan + aspect + res


# --------------------------------------------------------------------------
# Structural and hostile cases
# --------------------------------------------------------------------------

def _structure() -> list[Recipe]:
    common = dict(group="Structure", video=Video(width=854, height=480, bitrate="800k"))
    return [
        Recipe(
            key="x-chapters", title="Twelve chapters", duration=240, chapters=12,
            notes="Twelve evenly spaced chapters with names. Exercises the chapter "
                  "menu, chapter skip, and the chapter-image extraction task.",
            **common,
        ),
        Recipe(
            key="x-many-audio", title="Six audio tracks", duration=30,
            notes="Six languages, each a different pitch so you can hear which one is "
                  "selected, with track three flagged default. Exercises audio track "
                  "selection, language preference and the default flag together.",
            audios=tuple(
                Audio(encoder="aac", channels=ch, lang=lang, title=name,
                      default=(lang == "deu"), tone=tone)
                for lang, name, ch, tone in (
                    ("eng", "English 5.1", 6, 440),
                    ("eng", "English Commentary", 2, 523),
                    ("deu", "Deutsch", 2, 587),
                    ("jpn", "日本語", 2, 659),
                    ("fra", "Français", 6, 698),
                    ("und", "Undetermined", 2, 784),
                )
            ),
            **common,
        ),
        Recipe(
            key="x-no-audio", title="Video with no audio track", duration=20,
            notes="A silent film in the literal sense — no audio stream at all. "
                  "Clients that assume at least one audio track crash or hang here.",
            audios=(), **common,
        ),
        Recipe(
            key="x-audio-only", title="Audio-only file in a video library",
            duration=30,
            notes="No video stream, sitting in the Movies library. The server should "
                  "still present it; the client has to draw something.",
            video=None, audios=(Audio(encoder="aac"),), container="mkv",
            group="Structure",
        ),
        Recipe(
            key="x-single-frame", title="One frame", duration=1,
            notes="A one-frame video. Duration rounds to zero in a lot of arithmetic, "
                  "and progress bars divide by it.",
            tier="standard", **common,
        ),
        Recipe(
            key="x-long", title="Three hours", duration=10800,
            notes="A long runtime, for resume points, seek accuracy far from the "
                  "start, and progress reporting over a session that outlives a token.",
            # `ultrafast` because this file exists to be long, not to be well
            # encoded — and at 259,200 frames it otherwise becomes the only
            # thing the build is still waiting on after everything else is done.
            video=Video(width=640, height=360, fps="24", bitrate="200k",
                        args=("-preset", "ultrafast")),
            chapters=36, tier="standard", group="Structure",
        ),
        Recipe(
            key="x-truncated", title="Truncated file", duration=20,
            notes="A valid header followed by a file that stops mid-stream. Playback "
                  "should fail cleanly and report an error, not hang.",
            broken="truncate", tier="standard", **common,
        ),
        Recipe(
            key="x-zero-byte", title="Zero-byte file", duration=1,
            notes="An empty file with a video extension. It should be rejected at "
                  "scan, and if it is not, it must fail gracefully at playback.",
            broken="zero", tier="standard", **common,
        ),
    ]


# --------------------------------------------------------------------------
# Audiobooks
# --------------------------------------------------------------------------
#
# These are declared here, and placed by `libraries.build_books`, because an
# audiobook is only an audiobook inside a `books` library: the *same* file in
# the Music library resolves as ordinary `Audio` and takes a different path
# through every client. They stay in this module so `verify` re-probes them
# against a declared codec, channel count and chapter count like everything
# else — a file with no recipe is a file nothing checks.
#
# Two shapes, because the server produces two and they are not variations on
# one another:
#
#   A single `.m4b` with embedded chapter markers is **one** item whose
#   chapters are rows in the database. Chapter extraction is enabled for this
#   type and no other (`AudioFileProber`: `ExtractChapters = item is
#   AudioBook`), and it does nothing more than add `-show_chapters` to
#   ffprobe — so markers the container does not carry are markers that do not
#   exist. There is no per-file, cue-sheet or filename fallback anywhere.
#
#   A multi-file rip is **N** items. `StackResolver.ResolveAudioBooks` groups
#   by directory, so all six parts become one stack of six files, and
#   `AudioResolver` then drops any stack holding more than one file ("For now,
#   until we sort out naming for multi-part books"). That leaves zero items,
#   which is exactly what saves them: `LibraryManager.ResolvePaths` only takes
#   a multi-item resolver's answer `if (result?.Items.Count > 0)`, so it falls
#   through and resolves each file on its own. Add a *seventh* file that does
#   not stack and the folder would produce one item and hide the other six.
#
# So "chapter 7" is a marker in the first case and item 7 in the second — two
# code paths for one gesture, and only the first reuses a client's chapter UI.

AUDIOBOOK_AUTHOR = "Elena Farrow"
AUDIOBOOK_NARRATOR = "Noa Nakamura"
AUDIOBOOK_TITLE = "The Lantern Keeper"
AUDIOBOOK_CHAPTERS = 8

# The multi-file rip: a different book, so the two shapes cannot be confused
# for two spellings of one.
RIP_AUTHOR = "Gus Gupta"
RIP_NARRATOR = "Mira Moreau"
RIP_TITLE = "The Divided Account"
RIP_PARTS = 6

# --- the two long ones, and the branch they exist for --------------------
#
# `UserDataManager.UpdatePlayState` has an arm of its own for `AudioBook`, and
# unlike the video arm above it its thresholds are **minutes off each end**,
# not percentages: `MinAudiobookResume` and `MaxAudiobookResume`, both 5 by
# default. Under 5 minutes in, the position is discarded as "just started";
# under 5 minutes from the end, it is discarded *and* the item is marked
# played. Nothing else in that arm looks at duration, so:
#
#   * an audiobook shorter than **10 minutes** can never hold a resume
#     position at all — every position is either <5 min in or <5 min from the
#     end;
#   * one shorter than **5 minutes** can never even be marked played by
#     playback, because the first test wins and returns before `Played` is
#     set.
#
# `The Lantern Keeper` is 240 s and the rip's parts are 20 s, so both sit
# under both thresholds and no amount of reporting moves either. Measured on
# 12.0: positions of 30 s, 120 s, 200 s and 235 s into the `.m4b` all stored
# 0 and left `Played` false. That is real coverage — it is the "too short to
# resume" case, and it is what makes playing one to its end cheap — so the
# long ones below are *additional* fixtures rather than those two lengthened.
# One fixture, one property.
#
# Both are mono at 32k. The content is a sine tone and the *length* is the
# fixture, so 24 minutes at the short ones' 64k stereo would be four times
# the bytes for nothing that is being tested.

LONG_AUDIOBOOK_AUTHOR = "Hana Halloran"
LONG_AUDIOBOOK_NARRATOR = "Ivo Ibarra"
LONG_AUDIOBOOK_TITLE = "The Overnight Vigil"
LONG_AUDIOBOOK_CHAPTERS = 6
LONG_AUDIOBOOK_SECONDS = 24 * 60

# The multi-file rip long enough for a *part* to be finished by playback,
# which is what makes folder-level resume ("the first part not yet finished")
# reachable at all. A different book again, so four audiobook folders are four
# titles rather than two titles at two lengths.
LONG_RIP_AUTHOR = "Kai Kowalski"
LONG_RIP_NARRATOR = "Lena Lindqvist"
LONG_RIP_TITLE = "The Slow Crossing"
LONG_RIP_PARTS = 3
LONG_RIP_SECONDS = 12 * 60

# The server's own two numbers, restated here because every position below is
# only meaningful against them (`ServerConfiguration.MinAudiobookResume` /
# `MaxAudiobookResume`, minutes).
AUDIOBOOK_MIN_RESUME_MINUTES = 5
AUDIOBOOK_MAX_RESUME_MINUTES = 5

# The three answers that arm can give, as positions into the long `.m4b`.
# Named because a test has to pick a number on the right side of both
# thresholds, and neither threshold is a fraction of the runtime — reading
# them off the percentages the video arm uses gives the wrong answer every
# time.
LONG_AUDIOBOOK_RESUME_SECONDS = 6 * 60     # >=5 min in, >=5 min left: kept
LONG_AUDIOBOOK_PLAYED_SECONDS = 21 * 60    # <5 min left: zeroed and Played
LONG_AUDIOBOOK_IGNORED_SECONDS = 2 * 60    # <5 min in: zeroed, not Played

# And on one part of the long rip. The finish position is what a client's
# "resume the folder" has to be able to produce; the short rip cannot reach
# it, because 20 s is under both thresholds at once.
LONG_RIP_RESUME_SECONDS = 6 * 60           # >=5 min in, >=5 min left: kept
LONG_RIP_PLAYED_SECONDS = 8 * 60           # 4 min left: zeroed and Played

# --- a folder holding SEVERAL books, which is the other thing a folder ---
#   means -------------------------------------------------------------------
#
# Every folder above holds the parts of *one* book, so the question a client
# has to answer about a books folder — one book, or several? — only ever had
# one answer here, and a client that never asks it looks right. The two
# renderings are not variations: chapters of one book are a **chapter list**
# and several books are a **gallery**, and picking the first for the second
# hides every book after the top one behind a row that plays it.
#
# `Album` is the only field that can tell them apart. Nothing sets
# `SeriesName` on an `AudioBook`, there is no album entity to point at, and
# the parts are N unrelated items — so **these must not share an album**, or
# they collapse into one book and the fixture becomes another copy of the rip.
#
# This is what a real library looks like when someone files audiobooks by
# author rather than by book: three single-file books loose in the author's
# own folder. Measured (see `docs/COVERAGE_GAPS.md` §10): all three come back
# as separate `AudioBook` items, because `AudioBookListResolver` makes one
# stack of the whole directory, `ResolveMultipleAudio` drops a stack of more
# than one file, and the per-file fall-through then resolves each on its own.
#
# They are two minutes each on purpose: they exist to be *listed*, not
# resumed, and both resume lengths are already fixtures four folders up. Two
# minutes is under `MinAudiobookResume`, so no position they report can ever
# be stored — which is the right answer for a fixture whose whole subject is
# the shape of the folder.
#
# No chapter markers either, which is a case of its own: every single-file
# audiobook here until now carried them, so "a book with nothing to expand"
# had no fixture.

SHELF_AUTHOR = "Lior Levy"
SHELF_NARRATOR = "Omar Okafor"
SHELF_SECONDS = 120
#: Three different books, and therefore three different `album` tags.
SHELF_BOOKS = ("The Copper Bell", "The Winter Ferry", "The Paper Bridge")

# --- and one folder holding both shapes at once ---------------------------
#
# An author folder with a multi-file rip in a subfolder *and* loose
# single-file books beside it. Two things live here that nothing else covers:
#
#   * a folder whose children are not all audiobooks — a `Folder` among the
#     `AudioBook`s — which cannot be drawn as a chapter list at all, because
#     no row in a track list can open a folder;
#   * the safe side of a resolver boundary nothing warns at. The author
#     folder survives as a folder because it holds **two** loose files. With
#     exactly one, `AudioResolver.FindAudioBook` gets a single item back and
#     the whole author directory becomes that one audiobook — measured, and
#     the rip in the subfolder disappears from the library with nothing
#     logged (`docs/COVERAGE_GAPS.md` §10).

MIXED_AUTHOR = "Mo Mensah"
MIXED_NARRATOR = "Pia Petrov"
MIXED_SECONDS = 120
#: The loose single-file books. Two, and the count is load-bearing: see above.
MIXED_LOOSE = ("The Glass Orchard", "The Quiet Ledger")
MIXED_RIP_TITLE = "The Falling Tide"
MIXED_RIP_PARTS = 2
MIXED_RIP_SECONDS = 60


# --------------------------------------------------------------------------
# Descriptions, and the tag each container has to carry one in
# --------------------------------------------------------------------------
#
# An audiobook's description lives in the **file**, not in the directory: a
# books library has no metadata sidecar the server reads (`MediaBrowser.XbmcMetadata`
# has no parser for `Book`, `AudioBook` or `Folder`), so the container tags
# are the only place one can be written from disk.
#
# `AudioFileProber` reads it in an arm of its own, for `AudioBook` and no
# other audio type:
#
#     var trackDescription = GetSanitizedStringTag(track.Description, ...);
#     var trackComment     = GetSanitizedStringTag(track.Comment, ...);
#     var overview = !string.IsNullOrWhiteSpace(trackDescription)
#                      ? trackDescription : trackComment;
#
# `track` is an ATL `Track`, and **which ffmpeg tag reaches which of those two
# fields is not the same in every container** — one of the obvious spellings
# is silently inert. Measured against ATL 7.15.3, the version the server
# builds with, and confirmed end to end against a running 12.0:
#
#   | container | ffmpeg `-metadata` | what is written        | ATL field   |
#   | --------- | ------------------ | ---------------------- | ----------- |
#   | m4b / mp4 | `description`      | the `desc` atom        | Description |
#   | m4b / mp4 | `comment`          | the `(c)cmt` atom      | Comment     |
#   | mp3       | `TIT3`             | an ID3v2 `TIT3` frame  | Description |
#   | mp3       | `description`      | `TXXX:description`     | **none**    |
#   | mp3       | `comment`          | `TXXX:comment`         | **none**    |
#
# So an MP3's description has to go in `TIT3`, and the spelling that works
# everywhere else does nothing at all there — no error, no warning, an item
# with no description. ffmpeg cannot write the `COMM` frame the wild puts one
# in: every route to it (`-metadata comment=`, `-metadata COMM=`) ends in a
# `TXXX` user frame. A real `COMM` *is* read — measured with one written by
# hand — so a rip tagged by anything but ffmpeg reaches the same place by the
# other branch.

#: Containers whose ffmpeg `comment` tag reaches ATL's `Track.Comment`, and
#: therefore the server's *fallback* source for an Overview. MP3 is not one:
#: see above. Passing `comment=` for anything else is refused rather than
#: written, because a tag nothing reads is a fixture that looks like coverage.
COMMENT_IS_READ = ("m4b", "m4a", "mp4")

#: Where a description goes, per container.
DESCRIPTION_TAG = {"m4b": "description", "m4a": "description",
                   "mp4": "description", "mp3": "TIT3"}

# The strings themselves. Every one of them says which tag it was written in
# and what it being on screen proves, because the whole point of having six
# of them is that a client can be told apart by which one it shows.

#: `The Lantern Keeper`, in `comment` and no `description` at all — the
#: fallback half of the rule above, which nothing else here reaches.
AUDIOBOOK_COMMENT = (
    "Written into this file's `comment` tag, with no `description` tag "
    "anywhere in it. That makes this book the fallback half of the server's "
    "rule: `AudioFileProber` prefers ATL's Description and falls back to "
    "Comment only when it is empty. This folder holds one audiobook, so the "
    "folder *is* this item — what you are reading is both.")

#: `The Overnight Vigil` carries both, saying different things, so which one
#: won is visible on screen. The description is the one that should win.
LONG_AUDIOBOOK_DESCRIPTION = (
    "The `description` tag. This file also carries a `comment` saying "
    "something else, and you should never see that one: ATL's Description is "
    "read first and Comment is only the fallback.")
LONG_AUDIOBOOK_COMMENT = (
    "The `comment` tag, which is the fallback and has lost here. Reading "
    "this on screen means a client (or a server) took Comment while a "
    "Description was sitting in the same file.")

#: `The Slow Crossing`'s parts, one string each, so which part a client reads
#: a folder's description off is visible. Its *folder* also carries one, and
#: that is the one that should win — see `LONG_RIP_FOLDER_OVERVIEW`.
def long_rip_description(part: int) -> str:
    return (f"Part {part} of {LONG_RIP_PARTS}, described in this file's own "
            f"ID3 `TIT3` frame — the only frame ffmpeg can write that the "
            f"server's tag reader reads a description out of. Every part "
            f"says a different number, so which one a client picks up is "
            f"visible. The folder above has a description of its own, and "
            f"that one wins: seeing this string where a book's description "
            f"goes means the folder's was not asked for.")

#: And the folder's own, which **cannot be written from disk**: a `Folder`
#: has no local metadata provider of any kind — no NFO parser, no XML
#: provider — so an `.nfo` beside the parts is read by nobody. Measured, not
#: assumed. It is set through the API after the scan (`provision.py`), the
#: same route a person editing metadata in the web client takes, because a
#: folder-level description that beats the per-file one is a rule a client
#: has and nothing here could otherwise exercise.
LONG_RIP_FOLDER_OVERVIEW = (
    "The folder's own description, set on the folder item and not in any "
    "file. It has to win over the parts' — each of those says which part it "
    "came from, so a client showing one of those here is reading the wrong "
    "level. No file on disk can carry this: a books library has no metadata "
    "sidecar the server reads for a folder.")


def shelf_description(title: str) -> str:
    return (f"`{title}`, one of {len(SHELF_BOOKS)} different books loose in "
            f"`{SHELF_AUTHOR}/`. Each has its own `album`, which is the only "
            f"field that says they are not chapters of one book — and its "
            f"own description, so a gallery of three shows three different "
            f"blurbs rather than one repeated.")


def mixed_description(title: str) -> str:
    return (f"`{title}`, loose in `{MIXED_AUTHOR}/` beside a multi-file rip "
            f"in a subfolder. A folder holding both shapes at once is not a "
            f"chapter list at any level: one of its children is a folder.")


def long_rip_folder_overviews() -> tuple[tuple[str, str, str], ...]:
    """`(author, folder, overview)` for every folder-level description.

    Read by `provision`, which is the only thing that can apply one. Kept
    here beside the strings it applies rather than in the provisioning code,
    so a fixture is declared in one place like every other.
    """
    return ((LONG_RIP_AUTHOR, LONG_RIP_TITLE, LONG_RIP_FOLDER_OVERVIEW),)


def _audiobook_tags(title: str, author: str, narrator: str, year: int,
                    *, container: str = "", overview: str = "",
                    comment: str = "", **extra: str
                    ) -> tuple[tuple[str, str], ...]:
    """The tags `AudioFileProber` reads off an audiobook.

    `album_artist` is the Author, `composer` the Narrator — Audiobookshelf's
    convention, which the server adopted — and `artist` is left as the author
    so a client that reads the ordinary music tags shows the same name rather
    than nothing.

    `overview` is the book's description and goes in whichever tag *this
    container* has one read out of (`DESCRIPTION_TAG`); `comment` writes the
    fallback source as well, and is refused for a container the server never
    reads it from, because a tag nothing reads is a fixture that looks like
    coverage. The table above says which is which and how it was measured.
    """
    tags = {
        "title": title, "album": title, "album_artist": author,
        "artist": author, "composer": narrator, "genre": "Audiobook",
        "date": str(year),
    }
    if overview:
        if container not in DESCRIPTION_TAG:
            raise ValueError(f"no description tag known for .{container}")
        tags[DESCRIPTION_TAG[container]] = overview
    if comment:
        if container not in COMMENT_IS_READ:
            raise ValueError(f"a .{container} `comment` never reaches "
                             f"ATL's Track.Comment")
        tags["comment"] = comment
    tags.update(extra)
    return tuple(tags.items())


def _audiobooks() -> list[Recipe]:
    out = [
        Recipe(
            key="book-m4b", title=AUDIOBOOK_TITLE, group="Audiobooks",
            library="Books", container="m4b",
            notes=f"One `.m4b` with {AUDIOBOOK_CHAPTERS} embedded chapter "
                  f"markers, alone in its own folder. The server returns a "
                  f"single AudioBook whose Chapters are real rows — the only "
                  f"way to reach the chapter-extraction path, which is "
                  f"switched on for this item type and no other. Its name "
                  f"comes from the *folder*, not the file — which holds only "
                  f"because the `title` tag agrees with it: "
                  f"`AudioFileProber` overwrites Name from that tag with no "
                  f"regard for `EnableEmbeddedTitles`. Its description is in "
                  f"`comment` and there is no `description` tag in it at "
                  f"all, which makes it the only fixture reaching the "
                  f"*fallback* half of the server's Overview rule.",
            video=None,
            audios=(Audio(encoder="aac", channels=2, rate=44100,
                          bitrate="64k", lang="eng"),),
            duration=240, chapters=AUDIOBOOK_CHAPTERS, year=2019,
            container_tags=_audiobook_tags(
                AUDIOBOOK_TITLE, AUDIOBOOK_AUTHOR, AUDIOBOOK_NARRATOR, 2019,
                container="m4b", comment=AUDIOBOOK_COMMENT),
        ),
    ]
    for part in range(1, RIP_PARTS + 1):
        out.append(Recipe(
            key=f"book-rip-{part:02d}",
            title=f"Chapter {part:02d}", group="Audiobooks",
            library="Books", container="mp3",
            notes=f"Part {part} of {RIP_PARTS} of a multi-file audiobook rip. "
                  f"Each part is its own AudioBook item — the parts stack at "
                  f"scan time and the stack is then dropped, so they survive "
                  f"only through the per-file fall-through. Nothing sets "
                  f"SeriesName on an AudioBook, so `album` is all that joins "
                  f"them. No chapter markers: here a chapter is a file. "
                  f"**Deliberately undescribed** — no description tag on any "
                  f"part and none on the folder — because "
                  f"\"this book has no blurb at all\" is a case a client "
                  f"draws differently, and every other audiobook here has "
                  f"one.",
            video=None,
            audios=(Audio(encoder="libmp3lame", channels=2, rate=44100,
                          bitrate="64k", lang="eng"),),
            duration=20, chapters=0, year=2016,
            container_tags=_audiobook_tags(
                f"Chapter {part:02d}", RIP_AUTHOR, RIP_NARRATOR, 2016,
                album=RIP_TITLE, track=f"{part}/{RIP_PARTS}"),
        ))
    out.append(Recipe(
        key="book-m4b-long", title=LONG_AUDIOBOOK_TITLE, group="Audiobooks",
        library="Books", container="m4b",
        notes=f"The same shape as `{AUDIOBOOK_TITLE}` and "
              f"{LONG_AUDIOBOOK_SECONDS // 60} minutes long, which is the "
              f"whole of the difference: `UpdatePlayState`'s AudioBook arm "
              f"measures {AUDIOBOOK_MIN_RESUME_MINUTES} minutes in and "
              f"{AUDIOBOOK_MAX_RESUME_MINUTES} minutes from the end, in "
              f"minutes rather than percentages, so nothing shorter than ten "
              f"minutes can hold a resume position at all. All three answers "
              f"that arm can give are reachable on this one item: "
              f"{LONG_AUDIOBOOK_RESUME_SECONDS // 60}:00 resumes, "
              f"{LONG_AUDIOBOOK_PLAYED_SECONDS // 60}:00 zeroes the position "
              f"and marks it played, "
              f"{LONG_AUDIOBOOK_IGNORED_SECONDS // 60}:00 is discarded as "
              f"just-started. {LONG_AUDIOBOOK_CHAPTERS} embedded chapter "
              f"markers of "
              f"{LONG_AUDIOBOOK_SECONDS // LONG_AUDIOBOOK_CHAPTERS // 60} "
              f"minutes each, so a chapter jump lands on either side of both "
              f"thresholds too. Alone in its folder, so it is named after "
              f"the folder like the short one. Mono at 32k: the length is "
              f"the fixture, not the fidelity. Carries a `description` "
              f"*and* a `comment` saying different things, which is the only "
              f"fixture where the server's preference between the two is "
              f"visible rather than assumed: the description must win.",
        video=None,
        audios=(Audio(encoder="aac", channels=1, rate=44100,
                      bitrate="32k", lang="eng"),),
        duration=LONG_AUDIOBOOK_SECONDS, chapters=LONG_AUDIOBOOK_CHAPTERS,
        year=2022,
        container_tags=_audiobook_tags(
            LONG_AUDIOBOOK_TITLE, LONG_AUDIOBOOK_AUTHOR,
            LONG_AUDIOBOOK_NARRATOR, 2022, container="m4b",
            overview=LONG_AUDIOBOOK_DESCRIPTION,
            comment=LONG_AUDIOBOOK_COMMENT),
    ))
    for part in range(1, LONG_RIP_PARTS + 1):
        out.append(Recipe(
            key=f"book-rip-long-{part:02d}",
            title=f"{LONG_RIP_TITLE} Part {part:02d}", group="Audiobooks",
            library="Books", container="mp3",
            notes=f"Part {part} of {LONG_RIP_PARTS} of a rip whose parts are "
                  f"{LONG_RIP_SECONDS // 60} minutes each — long enough for "
                  f"one part to be *finished by playback*, which is what "
                  f"makes folder-level resume (the first part not yet "
                  f"finished) reachable at all. That needs a position at "
                  f"least {AUDIOBOOK_MIN_RESUME_MINUTES} minutes in with "
                  f"under {AUDIOBOOK_MAX_RESUME_MINUTES} minutes left, and "
                  f"no such position exists on a 20 s part of "
                  f"`{RIP_TITLE}`; here {LONG_RIP_PLAYED_SECONDS // 60}:00 "
                  f"is one and {LONG_RIP_RESUME_SECONDS // 60}:00 resumes "
                  f"instead. Its own folder, holding nothing but parts, for "
                  f"the reason the short rip's folder does: every audio file "
                  f"in a directory becomes one stack, the stack is dropped "
                  f"for having more than one file, and each file then falls "
                  f"through and resolves on its own. `album` joins them and "
                  f"nothing else does. No chapter markers: here a chapter is "
                  f"a file. Mono at 32k. Each part carries a description of "
                  f"its own in an ID3 `TIT3` frame — the tag a description "
                  f"has to go in for an MP3, where the obvious `comment` "
                  f"spelling is silently inert — and the *folder* carries a "
                  f"different one, applied through the API because no file "
                  f"on disk can give a folder a description. The folder's "
                  f"has to win.",
            video=None,
            audios=(Audio(encoder="libmp3lame", channels=1, rate=44100,
                          bitrate="32k", lang="eng"),),
            duration=LONG_RIP_SECONDS, chapters=0, year=2023,
            container_tags=_audiobook_tags(
                f"{LONG_RIP_TITLE} Part {part:02d}", LONG_RIP_AUTHOR,
                LONG_RIP_NARRATOR, 2023, container="mp3",
                overview=long_rip_description(part),
                album=LONG_RIP_TITLE, track=f"{part}/{LONG_RIP_PARTS}"),
        ))
    out += _audiobook_shelf()
    out += _mixed_audiobook_folder()
    return out


def _audiobook_shelf() -> list[Recipe]:
    """Several different books loose in one author's folder."""
    out = []
    for n, title in enumerate(SHELF_BOOKS, 1):
        out.append(Recipe(
            key=f"book-shelf-{n:02d}", title=title, group="Audiobooks",
            library="Books", container="m4b",
            notes=f"Book {n} of {len(SHELF_BOOKS)} filed loose in "
                  f"`{SHELF_AUTHOR}/`, which is what a library looks like "
                  f"when audiobooks are filed by author rather than by book. "
                  f"Its `album` is **its own title and not the folder's**, "
                  f"and that is the whole fixture: `album` is the only field "
                  f"that can say these are three books rather than three "
                  f"chapters of one, so sharing it would collapse them and "
                  f"leave a client's \"one book or several\" rule with only "
                  f"one answer to give again. Measured: all "
                  f"{len(SHELF_BOOKS)} come back as separate AudioBook items "
                  f"named from their `title` tags. "
                  f"{SHELF_SECONDS // 60} minutes, and no chapter markers — "
                  f"these exist to be *listed*, not resumed (both resume "
                  f"lengths are already fixtures, and this is under "
                  f"`MinAudiobookResume`, so no position it reports can be "
                  f"stored), and a single-file audiobook with nothing to "
                  f"expand had no fixture either. Its own description, so a "
                  f"gallery of three shows three.",
            video=None,
            audios=(Audio(encoder="aac", channels=1, rate=44100,
                          bitrate="32k", lang="eng"),),
            duration=SHELF_SECONDS, chapters=0, year=2024,
            container_tags=_audiobook_tags(
                title, SHELF_AUTHOR, SHELF_NARRATOR, 2024, container="m4b",
                overview=shelf_description(title), album=title),
        ))
    return out


def _mixed_audiobook_folder() -> list[Recipe]:
    """One author folder holding a rip in a subfolder and loose books."""
    out = []
    for n, title in enumerate(MIXED_LOOSE, 1):
        out.append(Recipe(
            key=f"book-mixed-{n:02d}", title=title, group="Audiobooks",
            library="Books", container="m4b",
            notes=f"Loose book {n} of {len(MIXED_LOOSE)} in "
                  f"`{MIXED_AUTHOR}/`, which also holds `{MIXED_RIP_TITLE}/` "
                  f"— a multi-file rip in a subfolder. A folder whose "
                  f"children are not all audiobooks cannot be drawn as a "
                  f"chapter list at any level, and nothing else here is one. "
                  f"There are **two** loose files rather than one because "
                  f"the count is a resolver boundary: with exactly one, "
                  f"`FindAudioBook` gets a single item back and the whole "
                  f"author directory becomes that one audiobook, taking the "
                  f"subfolder's rip out of the library with nothing logged. "
                  f"{MIXED_SECONDS // 60} minutes, no markers, its own "
                  f"`album` and its own description.",
            video=None,
            audios=(Audio(encoder="aac", channels=1, rate=44100,
                          bitrate="32k", lang="eng"),),
            duration=MIXED_SECONDS, chapters=0, year=2025,
            container_tags=_audiobook_tags(
                title, MIXED_AUTHOR, MIXED_NARRATOR, 2025, container="m4b",
                overview=mixed_description(title), album=title),
        ))
    for part in range(1, MIXED_RIP_PARTS + 1):
        out.append(Recipe(
            key=f"book-mixed-rip-{part:02d}",
            title=f"{MIXED_RIP_TITLE} Part {part:02d}", group="Audiobooks",
            library="Books", container="mp3",
            notes=f"Part {part} of {MIXED_RIP_PARTS} of the rip inside "
                  f"`{MIXED_AUTHOR}/{MIXED_RIP_TITLE}/`. The subfolder is an "
                  f"ordinary rip and resolves like the other two; what it is "
                  f"here for is its *parent*, which holds loose books beside "
                  f"it. {MIXED_RIP_SECONDS} seconds a part — a rip that "
                  f"exists to be found, not listened to.",
            video=None,
            audios=(Audio(encoder="libmp3lame", channels=1, rate=44100,
                          bitrate="32k", lang="eng"),),
            duration=MIXED_RIP_SECONDS, chapters=0, year=2025,
            container_tags=_audiobook_tags(
                f"{MIXED_RIP_TITLE} Part {part:02d}", MIXED_AUTHOR,
                MIXED_NARRATOR, 2025, container="mp3",
                overview=f"Part {part} of the rip that shares "
                         f"`{MIXED_AUTHOR}/` with two loose books.",
                album=MIXED_RIP_TITLE, track=f"{part}/{MIXED_RIP_PARTS}"),
        ))
    return out


def all_recipes() -> list[Recipe]:
    out: list[Recipe] = []
    out += _video_codecs()
    out += _audio_codecs()
    out += _containers()
    out += _subtitles()
    out += _color_and_motion()
    out += _structure()
    out += _audiobooks()
    _check_unique(out)
    return out


def for_tier(tier: str) -> list[Recipe]:
    from .config import tier_includes

    return [r for r in all_recipes() if tier_includes(tier, r.tier)]


def _check_unique(recipes: list[Recipe]) -> None:
    seen: dict[str, Recipe] = {}
    for r in recipes:
        if r.key in seen:
            raise ValueError(f"duplicate recipe key {r.key!r}")
        seen[r.key] = r
