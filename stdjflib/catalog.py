"""The catalog of downloadable source material.

Two families, and nothing else:

**Blender open movies** — CC-BY. Real films with real production values, real
grain, real 5.1 mixes, and in Tears of Steel's case twenty-four genuine subtitle
translations covering scripts no synthetic sample would produce convincingly.
Attribution is required and `ATTRIBUTION.md` provides it.

**Prelinger Archives ephemeral film** — donated to the public domain by Rick
Prelinger and labelled as such in each item's own metadata, which `fetch.py`
re-checks at download time. These bring what the Blender films cannot: 4:3
framing, mono optical-track audio, telecine artefacts, and encodings
(Cinepak AVI, MPEG-2, Ogg Theora) that no one would produce today but that
still turn up in real libraries.

Deliberately absent: the popular "public domain classics" — the well-known
horror and serial titles people reach for first. Their archive.org items are
either dark, unlabelled, or user re-uploads whose provenance cannot be checked
mechanically. A QA library is not worth a copyright argument.

Sizes are recorded so `--dry-run` can total a build without touching the
network, and so a truncated download is caught rather than muxed.
"""

from __future__ import annotations

import dataclasses

BLENDER = "https://download.blender.org"
ARCHIVE = "https://archive.org/download"

CC_BY_3 = "CC-BY-3.0"
CC_BY_25 = "CC-BY-3.0"  # Elephants Dream is 2.5; treated the same here.
PD = "public-domain"


@dataclasses.dataclass(frozen=True)
class Source:
    key: str
    title: str
    url: str
    licence: str
    attribution: str
    tier: str = "standard"
    kind: str = "movie"          # movie | episode | subtitle | audio | extra
    size: int = 0                # expected bytes, 0 when unknown
    unzip: bool = False
    member: str | None = None    # which zip member, if not the largest
    filename: str = ""           # name inside the cache
    year: int = 2000
    plot: str = ""
    archive_id: str | None = None
    parent: str | None = None    # for subtitles/extras: the movie key
    lang: str | None = None
    runtime: int = 0             # minutes, for NFO

    @property
    def cache_name(self) -> str:
        return self.filename or self.url.rsplit("/", 1)[-1]


BLENDER_CREDIT = ("(CC BY 3.0) Blender Foundation | "
                  "https://studio.blender.org")

MOVIES = [
    Source(
        key="big-buck-bunny",
        title="Big Buck Bunny",
        url=f"{BLENDER}/peach/bigbuckbunny_movies/big_buck_bunny_1080p_h264.mov.zip",
        licence=CC_BY_3,
        attribution=f"Big Buck Bunny © 2008 {BLENDER_CREDIT}/projects/big-buck-bunny/",
        size=725106348, unzip=True, filename="big_buck_bunny_1080p_h264.mov",
        year=2008, runtime=10,
        plot="A large rabbit takes revenge on three rodents. The Blender "
             "Foundation's second open movie, and the most widely used test clip "
             "in existence — 1080p H.264 in a QuickTime container with stereo AAC.",
    ),
    Source(
        key="sintel",
        title="Sintel",
        url=f"{BLENDER}/durian/movies/Sintel.2010.1080p.mkv",
        licence=CC_BY_3,
        attribution=f"Sintel © 2010 {BLENDER_CREDIT}/projects/sintel/",
        size=1180090590, filename="Sintel.2010.1080p.mkv",
        year=2010, runtime=15,
        plot="A girl searches for her lost dragon. Ships as Matroska with 5.1 "
             "audio and embedded subtitles, so it exercises the container most "
             "real libraries are full of.",
    ),
    Source(
        key="tears-of-steel",
        title="Tears of Steel",
        url=f"{BLENDER}/demo/movies/ToS/tears_of_steel_720p.mov",
        licence=CC_BY_3,
        attribution=f"Tears of Steel © 2012 {BLENDER_CREDIT}/projects/tears-of-steel/",
        size=372178639, filename="tears_of_steel_720p.mov",
        year=2012, runtime=12,
        plot="Live action and CGI, shot to test Blender's compositing. Its "
             "twenty-four community subtitle translations are the reason it is "
             "here: real sentences in Hebrew, Persian, Japanese and Greek beat "
             "any generated sample for testing font fallback and bidirectional text.",
    ),
    Source(
        key="elephants-dream",
        title="Elephants Dream",
        url=f"{BLENDER}/ED/elephantsdream-1024-h264-st-aac.mov",
        licence=CC_BY_25,
        attribution=("Elephants Dream © 2006 (CC BY 2.5) Blender Foundation | "
                     "https://orange.blender.org"),
        size=326632467, filename="elephantsdream-1024-h264-st-aac.mov",
        year=2006, runtime=11,
        plot="The first Blender open movie. 1024x576 — a resolution that is "
             "neither 720p nor SD, which is exactly the sort of thing that "
             "breaks a bitrate ladder.",
    ),
]

FULL_TIER_MOVIES = [
    Source(
        key="bbb-4k60",
        title="Big Buck Bunny (4K 60fps)",
        url=f"{BLENDER}/demo/movies/BBB/bbb_sunflower_2160p_60fps_normal.mp4.zip",
        licence=CC_BY_3,
        attribution=f"Big Buck Bunny © 2008 {BLENDER_CREDIT}/projects/big-buck-bunny/",
        tier="full", size=671868845, unzip=True,
        filename="bbb_sunflower_2160p_60fps_normal.mp4",
        year=2008, runtime=10,
        plot="The 2160p60 grade. Direct play or a very expensive transcode, with "
             "nothing sensible in between — which is the decision being tested.",
    ),
    Source(
        key="bbb-1080p60",
        title="Big Buck Bunny (1080p 60fps)",
        url=f"{BLENDER}/demo/movies/BBB/bbb_sunflower_1080p_60fps_normal.mp4.zip",
        licence=CC_BY_3,
        attribution=f"Big Buck Bunny © 2008 {BLENDER_CREDIT}/projects/big-buck-bunny/",
        tier="full", size=355019001, unzip=True,
        filename="bbb_sunflower_1080p_60fps_normal.mp4",
        year=2008, runtime=10,
        plot="1080p at 60 fps, with 5.1 and stereo tracks.",
    ),
    Source(
        key="bbb-3d-abl",
        title="Big Buck Bunny (Stereoscopic 3D)",
        url=f"{BLENDER}/demo/movies/BBB/bbb_sunflower_1080p_30fps_stereo_abl.mp4.zip",
        licence=CC_BY_3,
        attribution=f"Big Buck Bunny © 2008 {BLENDER_CREDIT}/projects/big-buck-bunny/",
        tier="full", unzip=True,
        filename="bbb_sunflower_1080p_30fps_stereo_abl.mp4",
        year=2008, runtime=10,
        plot="Above-below stereoscopic 3D. A client that does not recognise the "
             "layout shows a squashed picture stacked on itself, which is a "
             "clearer failure than most.",
    ),
    Source(
        key="ed-3d-sbs",
        title="Elephants Dream (Side-by-Side 3D)",
        url=f"{BLENDER}/ED/ed3d_sidebyside-RL-2x1920x1038_24fps.mkv",
        licence=CC_BY_25,
        attribution=("Elephants Dream © 2006 (CC BY 2.5) Blender Foundation | "
                     "https://orange.blender.org"),
        tier="full", filename="ed3d_sidebyside-RL-2x1920x1038_24fps.mkv",
        year=2006, runtime=11,
        plot="Side-by-side 3D, right eye first — the less common ordering, and "
             "the one that gets assumed away.",
    ),
    Source(
        key="sintel-720p",
        title="Sintel (720p)",
        url=f"{BLENDER}/demo/movies/Sintel.2010.720p.mkv.zip",
        licence=CC_BY_3,
        attribution=f"Sintel © 2010 {BLENDER_CREDIT}/projects/sintel/",
        tier="full", unzip=True, filename="Sintel.2010.720p.mkv",
        year=2010, runtime=15,
        plot="A second, lower resolution of the same film — so the two can sit "
             "in one folder as alternate versions of one item.",
    ),
]

# The 24 Tears of Steel translations. `lang` is the ISO 639-2 code Jellyfin
# wants in the sidecar filename; the source filenames use their own scheme.
TOS_SUBTITLES = [
    ("TOS-en.srt", "eng", "English"),
    ("TOS-de.srt", "deu", "Deutsch"),
    ("TOS-es.srt", "spa", "Español"),
    ("TOS-fr-orig.srt", "fra", "Français"),
    ("TOS-it.srt", "ita", "Italiano"),
    ("TOS-nl.srt", "nld", "Nederlands"),
    ("TOS-no.srt", "nor", "Norsk"),
    ("TOS-PL-please-review.srt", "pol", "Polski"),
    ("TOS-PT-BR.srt", "por", "Português (Brasil)"),
    ("TOS-PT-PT.srt", "por", "Português"),
    ("TOS-CZ.srt", "ces", "Čeština"),
    ("TOS-HU.srt", "hun", "Magyar"),
    ("TOS-Croatian.srt", "hrv", "Hrvatski"),
    ("TOS-Dansk.srt", "dan", "Dansk"),
    ("TOS-ru.srt", "rus", "Русский"),
    ("TOS-GK.srt", "ell", "Ελληνικά"),
    ("TOS-HE.srt", "heb", "עברית"),
    ("TOS-Persian.srt", "fas", "فارسی"),
    ("TOS-CH.srt", "zho", "中文"),
    ("TOS-CH-traditional.srt", "zho", "中文 (繁體)"),
    ("TOS-JP.srt", "jpn", "日本語"),
    ("TOS-Indonesian.srt", "ind", "Bahasa Indonesia"),
    ("TOS-fr-Goofy.srt", "fra", "Français (alt)"),
    ("TOS-fr-OMenor.srt", "fra", "Français (alt 2)"),
]

SUBTITLES = [
    Source(
        key=f"tos-sub-{code}-{i}",
        title=f"Tears of Steel subtitles — {name}",
        url=f"{BLENDER}/demo/movies/ToS/subtitles/{fname}",
        licence=CC_BY_3,
        attribution=f"Tears of Steel © 2012 {BLENDER_CREDIT}/projects/tears-of-steel/",
        kind="subtitle", parent="tears-of-steel", lang=code, filename=fname,
        tier="standard",
    )
    for i, (fname, code, name) in enumerate(TOS_SUBTITLES)
]

# Prelinger ephemeral films. `_512kb.mp4` is the small H.264 derivative that
# every item has; `.mpeg` and `.avi` are the period-accurate ones and are
# pulled at the full tier because they are an order of magnitude larger.
_PRELINGER = [
    ("AboutBan1935", "About Bananas", 1935, 22,
     "A United Fruit Company educational short on the banana trade. 4:3, mono "
     "optical-track audio, and the telecine artefacts of a film scan."),
    ("Sniffles1955", "Sniffles and Sneezes", 1955, 11,
     "A classroom hygiene film. Short enough to watch through while testing, "
     "which is more than can be said for most test content."),
    ("Careofth1949", "Care of the Skin", 1949, 11,
     "Postwar health education, black and white."),
    ("ParkCons1938", "Park Conscious", 1938, 10,
     "A civic short on public parks. This item has no MPEG-2 derivative, which "
     "makes it the case where the expected format is simply absent."),
    ("Sleepfor1950", "Sleep for Health", 1950, 10,
     "Another classroom short from the same era."),
    ("EatforHe1954", "Eat for Health", 1954, 11,
     "Nutrition instruction, 1954."),
]

PRELINGER = [
    Source(
        key=f"prelinger-{ident.lower()}",
        title=title,
        url=f"{ARCHIVE}/{ident}/{ident}_512kb.mp4",
        licence=PD,
        attribution=(f"{title} ({year}) — Prelinger Archives, public domain. "
                     f"https://archive.org/details/{ident}"),
        archive_id=ident, filename=f"{ident}_512kb.mp4",
        year=year, runtime=runtime, plot=plot, tier="standard",
    )
    for ident, title, year, runtime, plot in _PRELINGER
]

PRELINGER_FULL = [
    Source(
        key=f"prelinger-{ident.lower()}-mpeg2",
        title=f"{title} (MPEG-2)",
        url=f"{ARCHIVE}/{ident}/{ident}.mpeg",
        licence=PD,
        attribution=(f"{title} ({year}) — Prelinger Archives, public domain. "
                     f"https://archive.org/details/{ident}"),
        archive_id=ident, filename=f"{ident}.mpeg", tier="full",
        year=year, runtime=runtime,
        plot=f"{plot} This is the MPEG-2 program stream derivative — "
             f"interlaced, and far larger than the film is long.",
    )
    for ident, title, year, runtime, plot in _PRELINGER[:3]
] + [
    Source(
        key=f"prelinger-{ident.lower()}-cinepak",
        title=f"{title} (Cinepak AVI)",
        url=f"{ARCHIVE}/{ident}/{ident}.avi",
        licence=PD,
        attribution=(f"{title} ({year}) — Prelinger Archives, public domain. "
                     f"https://archive.org/details/{ident}"),
        archive_id=ident, filename=f"{ident}.avi", tier="full",
        year=year, runtime=runtime,
        plot=f"{plot} Cinepak in AVI — a codec from 1992 that nothing decodes "
             f"in hardware and few clients handle gracefully.",
    )
    for ident, title, year, runtime, plot in _PRELINGER[:2]
]


def all_sources() -> list[Source]:
    out = (MOVIES + FULL_TIER_MOVIES + SUBTITLES + PRELINGER + PRELINGER_FULL)
    seen = set()
    for s in out:
        if s.key in seen:
            raise ValueError(f"duplicate source key {s.key!r}")
        seen.add(s.key)
    return out


def for_tier(tier: str) -> list[Source]:
    from .config import tier_includes

    # The minimal tier downloads nothing at all — that is what makes it usable
    # in CI and on a metered connection.
    if tier == "minimal":
        return []
    return [s for s in all_sources() if tier_includes(tier, s.tier)]


def movies_for_tier(tier: str) -> list[Source]:
    return [s for s in for_tier(tier) if s.kind == "movie"]


def estimated_bytes(tier: str) -> int:
    return sum(s.size for s in for_tier(tier))
