# stdjflib

Builds a standard Jellyfin QA library: a stable, reproducible set of media that
exercises the paths a Jellyfin client actually has to get right, so you can test
one against a real server programmatically instead of against whatever happens
to be in your own collection.

Everything is either **generated from scratch with ffmpeg** or **downloaded from
sources that declare a public-domain or Creative Commons licence**. Nothing in
here is anybody's film that you would rather not be redistributing, and
`ATTRIBUTION.md` credits what needs crediting.

Requirements: Python 3.10+ and `ffmpeg`. No Python packages to install — it is
all standard library.

```sh
./stdjflib.py doctor                       # what can this machine build?
./stdjflib.py list --tier standard         # what would it contain?
./stdjflib.py build /srv/qa-library        # build it
./stdjflib.py verify /srv/qa-library       # is it what it claims to be?
```

## What you get

```
Movies/          Blender open movies, public-domain shorts, and one fixture
                 per path convention Jellyfin resolves
Test Media/      the generated codec / container / subtitle / HDR / frame-rate /
                 scan-type / aspect-ratio matrix, 88 files
Shows/           six series covering season folders, absolute numbering,
                 date-based episodes, double episodes, gaps, flat layouts
Music/           FLAC, MP3, Opus, ALAC; embedded and folder art; multi-disc,
                 various-artists, and a completely untagged album
Music Videos/    artist/track layout with musicvideo NFOs
Photos/          all eight EXIF orientations, and several image formats
Home Videos/     dated folders of short clips
Books/           EPUB and CBZ

Bulk Movies/       at the full tier: ~1000 items per library, for scale
Bulk Shows/        rather than coverage — paging, virtualised scrolling,
Bulk Music/        thumbnail cache pressure, search and sort
Bulk Music Videos/
Bulk Photos/
Bulk Books/
```

Every item ships an NFO with `<lockdata>true</lockdata>` and its own artwork, so
Jellyfin never queries the internet and two builds present identical metadata.
That is what makes the library usable for automated testing rather than merely
convenient.

## The generated clips describe themselves

Each file in `Test Media/` burns its own stream layout into the picture, along
with a running timecode and frame counter:

```
┌──────────────────────────────────────────────┐
│  Six audio tracks                            │
│  libx264  854x480  24fps  yuv420p            │
│  A0: aac 5.1 eng (English 5.1)  FL FR FC ... │
│  A1: aac stereo eng (Commentary)  L R        │
│  A2: aac stereo deu (Deutsch) [default]  L R │
│  container: mkv   key: x-many-audio          │
│                                              │
│  00:00:03.000  frame 72                      │
└──────────────────────────────────────────────┘
```

When a client picks the wrong media source, or plays the wrong file, you can see
it without going back to the manifest. The same text is the item's plot in
Jellyfin, so browsing the library is also reading the documentation — every item
says what it exercises and what failing it looks like.

**Every audio channel gets its own pitch.** A 5.1 file is six distinguishable
notes rather than one tone copied six times, so "is the centre channel on the
centre speaker" and "did the downmix drop the surrounds" are things you can
hear.

## Tiers

| Tier | Downloads | Library | Items | Generate | What it adds |
| --- | --- | --- | --- | --- | --- |
| `minimal` | none | 0.3 GB | 172 | ~40 s | generated media only — the CI tier, works offline |
| `standard` | 2.4 GB | 3.7 GB | 216 | ~2.5 min | the Blender open movies, 24 real subtitle languages, public-domain shorts |
| `full` | 3.4 GB | 8.4 GB | 4737 | ~4 min | 4K, 60 fps, stereoscopic 3D, MPEG-2 and Cinepak derivatives, 8K, and the six `Bulk *` libraries |

Measured on a 16-core machine. "Library" excludes the download cache, which
`.stdjflib/cache/` keeps so a rebuild needs no network, and "Generate" excludes
download time — the full tier's downloads take considerably longer than its
encoding, because archive.org is slow. Tiers are cumulative.

They are much smaller than "a media library" suggests, and deliberately so —
the value here is coverage, not gigabytes. If you want volume for scale
testing (paging, virtualised scrolling, thumbnail caches), the Prelinger
collection that `catalog.py` draws from has 1,876 more items behind the same
licence filter, and adding them is a list entry each.

## What it covers

**Video** H.264 baseline/main/high/high10/4:2:2 · HEVC main and main10 · AV1 ·
VP9 · MPEG-2 · MPEG-4 ASP · Theora · WMV8 · Sorenson Spark · FFV1 · ProRes

**Audio** AAC 2.0/5.1/7.1 · AC-3 · E-AC-3 · DTS · TrueHD · FLAC 2.0/5.1/7.1 ·
Opus 2.0/5.1 · Vorbis · MP3 · MP2 · ALAC · PCM 16/24-bit · mono

**Containers** MKV · MP4 · MOV · TS · M2TS · WebM · AVI · FLV · Ogg · ASF · 3GP

**Subtitles** SRT, ASS and WebVTT embedded and as sidecars · ASS with styling,
positioning, karaoke and an attached font · MP4 timed text · **VobSub and DVB
bitmap subtitles** · forced and default flags · nine scripts including two
right-to-left · and, from Tears of Steel, twenty-four real translations
including Hebrew, Persian, Japanese and Greek

**Colour and motion** HDR10 with mastering metadata · HLG · BT.709 SDR ·
23.976 / 24 / 25 / 29.97 / 30 / 50 / 59.94 / 60 / 120 fps · true interlaced
1080i TFF and 480i BFF · 3:2 pulldown · anamorphic PAL (SAR 64:45, the
broadcast case) and an arbitrary 3:2 sample aspect MPEG-2 cannot express ·
4:3, 16:9, 2.39:1, 1:1 · 240p through 8K

**Structure** chapters · six audio tracks with language and default flags ·
video with no audio · audio with no video · one-frame and three-hour runtimes ·
a truncated file and a zero-byte file

**Paths** loose files · folder/file name disagreement · multi-version ·
multi-part stacking · trailers and extras folders · unicode and
right-to-left titles · titles long enough to overflow any column

## The bulk libraries

`--bulk N` builds six `Bulk *` libraries of roughly N items each — on by
default at the full tier (`--bulk 0` opts out), and available at any tier
(`--bulk 500 --tier minimal` works fine). They answer a different question
from everything else here: not "does the client handle this format" but "does
it still work with a thousand of them".

Defaults to 1000 because that is chosen against the client under test —
jellyfin-mpv-shim pages at 100 and fetches 300 at a time, so a thousand
crosses both boundaries several times rather than sitting just past the first.

What they are built to stress:

- **Sort and jump-to-letter.** Titles spread evenly across A–Z rather than
  clumping, because an index that is 80% "A" tests nothing.
- **Text handling mid-list.** Every 37th title is non-Latin (Japanese, Greek,
  Cyrillic, Arabic, Hebrew), leading-punctuation, leading-digit, or far too
  long — so they land in the middle of a long list, not only at the edges
  where they are easy to notice.
- **Filters.** Years span 1950–2025 and ratings span 1–10.
- **Placeholder artwork.** One item in eight ships without a poster; three
  episodes in four ship without a still. A client that only ever draws real
  artwork looks fine until a real library hands it an item without any.
- **Uneven collections.** Bulk shows have 1, 6, 13, 24, 60 and 120 episodes,
  splitting into seasons of 24 — a client that pages a season list correctly
  at twenty episodes can still break at two hundred.

**Media is shared between bulk items by hard link.** A bulk item exists to be
listed, scrolled past and searched, not played; a thousand separate encodes
would cost minutes to produce files nobody opens. Artwork and metadata *are*
per item, because those are what a thumbnail cache and a sort actually touch.
The result is ~4500 bulk items in about two minutes.

Not every filesystem cooperates: **sshfs and SMB accept the link call and give
you a copy anyway**, silently, so the sharing does not happen and bulk media
costs real bytes per item. The pool clips are therefore deliberately tiny
(three seconds at 256x144, five seconds of 48 kbit audio) — on a filesystem
that links, their size is irrelevant; on one that does not, it is the only
thing that matters. Expect roughly 100 MB of bulk media where linking works
and around 250 MB where it does not, rather than the several GB the naive
version would cost.

## Bitmap subtitles

ffmpeg cannot produce image subtitles from text ones — it refuses with
"Subtitle encoding currently only possible from text to text or bitmap to
bitmap" — so there is no way to reach that code path with ffmpeg alone. It
matters, because bitmap subtitles cannot be converted to text without OCR:
Jellyfin must either burn them into the video (a full transcode) or hand the
client bitmaps to composite, and there is no third option.

So `vobsub.py` writes the format directly: ffmpeg's drawtext rasterises the
text to a PGM, and that is run-length encoded into DVD sub-picture units and
wrapped in an MPEG-2 program stream. Once a VobSub exists it *is* a bitmap
source, so DVB and XSUB are reachable from it by ordinary transcoding.

## Reproducibility

Same tier, same ffmpeg build, same output. Concretely:

- No wall-clock time anywhere. Dates come from a fixed epoch.
- Nothing is seeded from Python's `hash()` — string hashing is salted per
  process, so it differs between runs of the same program.
- NFO field values are derived from each item's stable key by SHA-256.
- `verify` re-probes every generated file and compares codec, resolution, pixel
  format, channel counts, track counts and chapter counts against the recipe
  that asked for them. ffmpeg exits 0 on a surprising number of partial
  failures, so a successful build is not evidence that a file is correct.

`--hwaccel nvenc` uses the GPU for the large files. It is much faster and the
output is **not** byte-identical to a software build, so it is opt-in, recorded
in the manifest, and never used for the codec-matrix files whose exact encoding
is the thing under test.

## Adding it to Jellyfin

The build writes a `README.md` into the library root with the exact steps. The
short version: add each top-level folder as its own library with the content
type it names, then **turn off every metadata and image downloader** on all of
them. Leaving one on reintroduces exactly the variability the library exists to
remove.

## Licensing

Downloads are gated twice. Every catalog entry names a licence, and only
public-domain, CC0 and CC-BY / CC-BY-SA are accepted — NC and ND are not, since
they cannot be redistributed freely. For archive.org items the licence is
re-checked against the item's own metadata *at download time*, so an item that
has been withdrawn or relicensed since the catalog was written stops the fetch
rather than being pulled on the strength of a stale note.

The popular "public domain classics" are deliberately absent. Their archive.org
items are largely dark, unlabelled, or user re-uploads whose provenance cannot
be checked mechanically. A QA library is not worth a copyright argument.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

55 tests, well under a second, no ffmpeg required. They cover the matrix's
internal consistency, the licence rules, NFO output and determinism, ffmpeg
argument assembly, and the VobSub encoder — including a round-trip of the RLE
against an independent reference decoder written in the test, which is the only
thing that would catch a wrong nibble producing a file that still parses and
renders garbage.
