"""Check a built library against what it claims to be.

This is the part that makes the library trustworthy rather than merely present.
ffmpeg exits 0 on a surprising number of partial failures, so "the build
succeeded" is not evidence that a file has the streams its recipe describes.
Every generated file is probed and compared against its recipe: codec,
resolution, pixel format, channel counts, track counts and chapter counts.

A mismatch here means the library is lying about itself, which is worse than a
missing file — a client tested against it would be tested against the wrong
thing.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import os

from . import artwork, config, ff, recipes

# Beyond this many artwork files in one library, only a sample is probed. A
# bulk library is thousands of images of half a dozen shapes, and probing all
# of them costs minutes to learn what the first hundred already said. What
# was skipped is printed rather than passed over in silence.
ARTWORK_SAMPLE = 120

# ffmpeg names encoders and codecs differently; probing reports the codec.
CODEC_OF = {
    "libx264": "h264", "libx265": "hevc", "libsvtav1": "av1",
    "libvpx-vp9": "vp9", "libtheora": "theora", "libxvid": "mpeg4",
    "flv": "flv1", "prores_ks": "prores", "libmp3lame": "mp3",
    "libopus": "opus", "libvorbis": "vorbis", "dca": "dts",
    "libfdk_aac": "aac",
}


def codec_of(encoder: str) -> str:
    return CODEC_OF.get(encoder, encoder)


def _check_recipe(rec, path: str, cfg) -> list[str]:
    if rec.broken == "zero":
        if not os.path.exists(path):
            return [f"{rec.key}: missing"]
        if os.path.getsize(path) != 0:
            return [f"{rec.key}: expected a zero-byte file"]
        return []
    if rec.broken == "truncate":
        # Deliberately damaged; existence is all that can be asserted.
        return [] if os.path.exists(path) else [f"{rec.key}: missing"]

    if not os.path.exists(path):
        return [f"{rec.key}: missing ({path})"]

    data = ff.probe(path, cfg.ffprobe)
    if not data:
        return [f"{rec.key}: unreadable by ffprobe"]

    issues = []
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"
             and s.get("disposition", {}).get("attached_pic") != 1]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    if rec.video:
        if not video:
            issues.append(f"{rec.key}: no video stream")
        else:
            v = video[0]
            want = codec_of(rec.video.encoder)
            # A hardware encode is a different encoder for the same codec, so
            # the codec still has to match even when the build used NVENC.
            if v.get("codec_name") != want:
                issues.append(f"{rec.key}: video is {v.get('codec_name')}, "
                              f"expected {want}")
            if (v.get("width"), v.get("height")) != (rec.video.width,
                                                     rec.video.height):
                issues.append(
                    f"{rec.key}: {v.get('width')}x{v.get('height')}, "
                    f"expected {rec.video.width}x{rec.video.height}")
            if rec.video.sar and v.get("sample_aspect_ratio") not in (
                    rec.video.sar, rec.video.sar.replace(":", "/")):
                issues.append(f"{rec.key}: SAR is {v.get('sample_aspect_ratio')}, "
                              f"expected {rec.video.sar}")
    elif video:
        issues.append(f"{rec.key}: has a video stream but should not")

    if len(audio) != len(rec.audios):
        issues.append(f"{rec.key}: {len(audio)} audio tracks, "
                      f"expected {len(rec.audios)}")
    for i, want in enumerate(rec.audios):
        if i >= len(audio):
            break
        got = audio[i]
        if got.get("codec_name") != codec_of(want.encoder):
            issues.append(f"{rec.key}: audio {i} is {got.get('codec_name')}, "
                          f"expected {codec_of(want.encoder)}")
        if got.get("channels") != want.channels:
            issues.append(f"{rec.key}: audio {i} has {got.get('channels')} "
                          f"channels, expected {want.channels}")

    embedded = [s for s in rec.subs if not s.external]
    if len(subs) != len(embedded):
        issues.append(f"{rec.key}: {len(subs)} subtitle tracks, "
                      f"expected {len(embedded)}")

    if rec.chapters and len(data.get("chapters", [])) != rec.chapters:
        issues.append(f"{rec.key}: {len(data.get('chapters', []))} chapters, "
                      f"expected {rec.chapters}")

    return issues


def _artwork_kind(name: str) -> str | None:
    """Which image type a filename claims to be, or None if it is not artwork.

    Names, not folders: the same `poster.jpg` rule has to work for an item in
    its own folder, for `<film>-poster.jpg` beside a loose file and for
    `season01-poster.jpg` up in the series folder.
    """
    stem, ext = os.path.splitext(name.lower())
    if ext not in (".jpg", ".png"):
        return None
    for kind, spelling in _ARTWORK_STEMS:
        if stem == spelling or stem.endswith("-" + spelling):
            return kind
    return None


# Every spelling `artwork.py` writes, mapped back to the shape it should be.
# Spelled out rather than derived from that module's three naming tables,
# because those are keyed the other way and collide when inverted — and a
# check that inherits its expectations from the thing it is checking is not a
# check. Longest first: `clearart` must not be read as `art`.
_ARTWORK_STEMS = (
    ("art", "clearart"),
    ("thumb", "landscape"),
    ("backdrop", "backdrop"),
    ("backdrop", "fanart"),
    ("banner", "banner"),
    ("poster", "poster"),
    ("square", "folder"),
    ("square", "cover"),
    ("thumb", "thumb"),
    ("logo", "logo"),
    ("disc", "disc"),
)


def _check_artwork(path: str, kind: str, cfg) -> list[str]:
    """Probe one image and compare its shape against the type it claims to be.

    The shape is the whole point. A client rounds a row's median aspect ratio
    onto 2:3, 16:9 or 1:1 and lays every card in the row out from it, so a
    poster that is secretly 16:9 does not look wrong on its own — it quietly
    reshapes the row it is in, and the layout bug you were hunting hides
    behind it.
    """
    data = ff.probe(path, cfg.ffprobe)
    if not data:
        return [f"{path}: unreadable"]
    streams = [s for s in data.get("streams", []) if s.get("width")]
    if not streams:
        return [f"{path}: no image stream"]
    width, height = streams[0]["width"], streams[0]["height"]
    if not height:
        return [f"{path}: zero height"]
    want = artwork.SPECS[kind].aspect
    got = width / height
    # 1% — enough for rounding at these sizes, nowhere near the tolerance
    # jellyfin-web snaps with, which would let a 4:3 pass as 16:9.
    if abs(got - want) > want * 0.01:
        return [f"{path}: {width}x{height} is {got:.2f}:1, but a "
                f"{kind} is {artwork.SPECS[kind].ratio} ({want:.2f}:1)"]
    return []


def check_artwork(cfg, libraries) -> tuple[int, list[str]]:
    """Probe the artwork under each library folder. Returns (checked, problems)."""
    targets: list[tuple[str, str]] = []
    problems: list[str] = []
    for library in libraries:
        found = []
        for folder, _dirs, files in os.walk(cfg.path(library)):
            for name in files:
                kind = _artwork_kind(name)
                if kind:
                    found.append((os.path.join(folder, name), kind))
        found.sort()
        if len(found) > ARTWORK_SAMPLE:
            step = len(found) // ARTWORK_SAMPLE
            problems.append(
                f"{library}: {len(found)} images, probing every {step}th "
                f"({ARTWORK_SAMPLE} of them) — this is a note, not a fault")
            found = found[::step][:ARTWORK_SAMPLE]
        targets += found

    if not targets:
        return 0, problems
    with futures.ThreadPoolExecutor(cfg.workers * 2) as pool:
        for issues in pool.map(lambda t: _check_artwork(t[0], t[1], cfg),
                               targets):
            problems.extend(issues)
    return len(targets), problems


def run(cfg) -> tuple[int, int, list[str]]:
    """Verify the library at cfg.root. Returns (files, images, problems)."""
    manifest_path = cfg.path(config.MANIFEST)
    if not os.path.exists(manifest_path):
        return 0, 0, [f"no manifest at {manifest_path} — "
                      f"was this built by stdjflib?"]
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    problems: list[str] = []
    if manifest.get("build_version") != config.BUILD_VERSION:
        problems.append(
            f"manifest was written by build version "
            f"{manifest.get('build_version')}, this is {config.BUILD_VERSION} "
            f"— rebuild before trusting the result")
    if manifest.get("hwaccel"):
        problems.append(
            f"built with {manifest['hwaccel']} hardware encoding, so it is not "
            f"byte-identical to a software build (this is a note, not a fault)")

    by_key = {r.key: r for r in recipes.all_recipes()}
    paths = {i["key"]: i["path"] for i in manifest.get("items", [])}

    targets = [(by_key[k], p) for k, p in paths.items() if k in by_key]
    checked = 0
    with futures.ThreadPoolExecutor(cfg.workers) as pool:
        for issues in pool.map(lambda t: _check_recipe(t[0], t[1], cfg), targets):
            checked += 1
            problems.extend(issues)

    # Everything the manifest lists should still be on disk, recipe or not.
    # Pooled because a bulk build puts thousands of entries here and these are
    # latency-bound stat calls, usually over a network filesystem.
    listed = manifest.get("items", [])
    with futures.ThreadPoolExecutor(cfg.workers * 4) as pool:
        present = list(pool.map(lambda i: os.path.exists(i["path"]), listed))
    problems += [f"{item['key']}: missing ({item['path']})"
                 for item, ok in zip(listed, present) if not ok]

    # The artwork is checked against the manifest's library list rather than
    # this invocation's: `verify` is usually run without the --bulk the build
    # had, and a library that is on disk should be checked whether or not
    # today's arguments would have produced it.
    images, image_problems = check_artwork(
        cfg, manifest.get("libraries") or cfg.libraries())
    problems += image_problems

    return checked, images, problems
