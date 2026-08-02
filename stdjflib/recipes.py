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


def all_recipes() -> list[Recipe]:
    out: list[Recipe] = []
    out += _video_codecs()
    out += _audio_codecs()
    out += _containers()
    out += _subtitles()
    out += _color_and_motion()
    out += _structure()
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
