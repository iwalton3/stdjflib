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
./stdjflib.py artwork /srv/qa-library      # redraw every image, media untouched
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

Every item ships an NFO with `<lockdata>true</lockdata>` and its own artwork —
posters, backdrops, banners, logos, season posters, episode stills and square
album covers, each at the shape Jellyfin expects that image type to be — so
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
| `standard` | 2.4 GB | 3.7 GB | 216 | ~2.5 min | the Blender open movies, 24 real subtitle files, public-domain shorts |
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
right-to-left · and, from Tears of Steel, twenty-four real community
translations including Hebrew, Persian, Japanese and Greek — **four of which
are malformed upstream** and are kept deliberately, because a sidecar the
server refuses to parse is a case worth having

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

**Artwork** every image type Jellyfin has, at the shape it expects it —
Primary as a 2:3 poster, as a 1:1 album cover and as a 16:9 episode still ·
Backdrop · Banner at 5.4:1 · transparent Logo and clearart · Disc · season
posters in both places the server looks for them · sidecar naming beside
loose files as well as folder naming · photos in a spread of aspect ratios

## Artwork

An image's shape is not decoration. jellyfin-web takes the **median** aspect
ratio of a row, rounds it onto 2:3, 16:9, 1:1 or 4:3, and lays every card in
that row out from the result; the clients that copy it inherit the behaviour.
So a poster that is secretly 16:9 does not look wrong on its own — it reshapes
the row it is in, and the layout bug you were hunting hides behind it.

| Type | Shape | In a folder | Beside a loose file |
| --- | --- | --- | --- |
| Primary (poster) | 2:3 | `poster.jpg` | `<name>-poster.jpg` |
| Primary (album, artist) | 1:1 | `folder.jpg` | — |
| Primary (episode) | 16:9 | — | `<episode>-thumb.jpg` |
| Backdrop | 16:9 | `backdrop.jpg` | `<name>-fanart.jpg` |
| Thumb | 16:9 | `landscape.jpg` | `<name>-thumb.jpg` |
| Banner | 5.4:1 | `banner.jpg` | `<name>-banner.jpg` |
| Logo | 2.6:1, transparent | `logo.png` | `<name>-logo.png` |
| Art (clearart) | 16:9, transparent | `clearart.png` | `<name>-clearart.png` |
| Disc | 1:1, transparent | `disc.png` | `<name>-disc.png` |

Season posters go in the *series* folder as `season01-poster.jpg`, and season
zero is spelled `season-specials-poster.jpg`. `Season 01/poster.jpg` also
resolves, so the Standard Show uses one spelling for season one and the other
for season two — a client that handles only one shows half the posters.

Each image is drawn as the thing it stands for — a poster has a spine, a
still has sprocket holes, a banner has an art block and a wordmark, disc art
is round — and stamped with its own type and ratio. A client showing a banner
where a poster belongs shows a picture with `BANNER 5.4:1` written across it.

**`stdjflib artwork <root>` redraws every image in a library that is already
built.** It reads the tier and bulk count out of the manifest, runs every
builder with the media steps switched off, and touches nothing else — so the
images land wherever a build would have put them, *including image types
added since that library was built*, which a pass over the files on disk
could not discover. Roughly 15 seconds for a minimal library. Album art
embedded inside FLAC and MP3 files is swapped with `-c:a copy`, so the audio
stays bit-identical to everyone else's.

`stdjflib verify` re-probes the artwork too, and compares each image's actual
shape against the type its filename claims.

### Photographic artwork, for screenshots

The drawn artwork is built to be *read*: every image says which type it is and
what shape a client should have laid it out at. That is what you want when you
are chasing a layout bug and exactly what you do not want in a screenshot — a
wall of stamped colour blocks looks like a test fixture, because it is one.

**`--use-artwork`** puts photographs behind the posters, backdrops, banners
and covers instead, and takes the type stamp off. The text stays, over a drop
shadow rather than a darkened picture, so the photograph keeps its full
strength. A photo library becomes actual photographs with no label at all.

```sh
./stdjflib.py build /srv/qa-library --use-artwork      # at build time
./stdjflib.py artwork /srv/qa-library --use-artwork    # or swap an existing one
./stdjflib.py artwork /srv/qa-library --drawn-artwork  # and back again
```

400 photographs from [Lorem Picsum](https://picsum.photos), which serves
photographs from Unsplash. The [Unsplash
licence](https://unsplash.com/license) grants free use, commercial and not,
with no permission required and no attribution obliged; the one thing it
forbids is compiling the photos to replicate a competing photo service, which
this is not. Every photographer is credited in `ATTRIBUTION.md` anyway, with a
link to the original.

Unlike the film catalogue, this does not pass through the licence gate —
there is no per-image claim to check, only one blanket statement covering the
service. Hence: off by default, named in the flag, and credited.

The ids are pinned in `picsum.py` rather than fetched, so the same item gets
the same picture on every build, and photographs are assigned **by position,
not by hash** — consecutive items get consecutive photographs, so no screenful
of thumbnails repeats one. (By hash, 40 items drawn from 400 would repeat
about 86% of the time.) Up to ~105 MB, cached under `.stdjflib/cache/artwork/`,
so a rebuild or a redraw needs no network. The minimal and standard tiers take
a 150- and 250-photograph slice of it; anything with bulk libraries takes all
400.

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

## Running a server against it

```sh
./stdjflib.py serve /srv/qa-library          # build Jellyfin, run it, set it up
./stdjflib.py serve /srv/qa-library --fresh  # ...from a factory-fresh server
./stdjflib.py container /srv/qa-library      # or run the official image instead
./stdjflib.py provision /srv/qa-library --server http://host:8096
./stdjflib.py accounts                       # what each test account is for
```

`serve` compiles Jellyfin from a source checkout (`--source`, default
`~/Desktop/jellyfin`), runs it against a disposable state directory, completes
the first-run wizard, creates a library per folder, creates the test accounts,
triggers a scan and waits for it. Roughly 30 seconds to build and a few minutes
to scan 4,700 items. `provision` does everything except the running, against a
server you already have.

Nothing is written into the Jellyfin checkout — the build goes to a separate
`--artifacts-path`. That is not just tidiness: a checkout that was ever built
as root has root-owned `obj/` directories, and an in-tree build then dies with
"Permission denied" on a path that does not explain itself. Deleting the state
directory gives a factory-fresh server, which is the state most worth being
able to reach on demand.

The server's state, its build and the container's config live under the system
temp directory, not beside the library — the path is derived from the library
root, printed on start, and overridable with `--state` and `--artifacts`. A
library often sits on a network mount, and neither SQLite nor `dotnet build`
tolerates one; the instances are disposable in any case, which is what
`--fresh` is for. A reboot costs a rebuild and a factory-fresh server.

The web UI is optional and off unless a built `jellyfin-web/dist` is found —
a client talks to the API, and building the web UI needs an npm toolchain that
has nothing to do with testing one.

### In a container instead

```sh
./stdjflib.py container /srv/qa-library                     # podman or docker
./stdjflib.py container /srv/qa-library --runtime docker
./stdjflib.py container /srv/qa-library --keep-running      # set up, then return
./stdjflib.py container-stop
```

Runs the official `jellyfin/jellyfin` image with the library bind-mounted
read-only at `/media`, then provisions it exactly as `serve` does. Podman and
Docker take the same arguments for all of this, so `--runtime` only chooses the
binary; Docker generally needs root on a stock install, podman does not.

Between this and `serve` you get both major versions for free — the image is
Jellyfin 10.11 stable, a source build of `master` is 12.0, and the provisioning
works unchanged against both.

**The server's path is not your path.** Inside the container the library is at
`/media`, and that is what gets sent when the libraries are created. Getting
this wrong is quiet: the libraries are created without complaint and then scan
to nothing. `provision --media-root` exposes the same knob for a server running
somewhere else entirely.

**Bind mounts are checked before anything is provisioned.** The container is
asked to list `/media` first, and an empty or unreadable mount stops the run
with an explanation. This matters most for FUSE filesystems — sshfs, rclone,
network mounts — where whether it works depends on the runtime, the rootless
mapping, and whether the mount was made with `allow_other`. Rootless podman
reads an sshfs mount here without complaint; rootful Docker may not see it at
all if the mount was made after the daemon started, because it never enters the
daemon's mount namespace. If the check fails, build a library on local disk
(`stdjflib build /var/tmp/qa --tier minimal --bulk 30` takes about 40 seconds)
and point at that instead.

### Live TV (optional)

```sh
./stdjflib.py serve /srv/qa-library --live-tv
./stdjflib.py serve /srv/qa-library --live-tv --tuner-type hdhomerun
./stdjflib.py container /srv/qa-library --live-tv --tuner-count 2
```

Off by default. With `--live-tv` the run also starts faketvsource and wires it
in as a tuner plus an XMLTV guide, then refreshes the listings and waits — six
channels and around 970 programmes. It needs a faketvsource checkout
(`--faketv-source`; found automatically at `~/Desktop/faketvsource`) and
ffmpeg, and adds no other dependency.

This reaches a large part of a client that a media library cannot touch at
all: guide grids, channel logos, now/next, timers and recordings. It is also
what makes `qa-kid` meaningful — that account has Live TV access revoked,
which tests nothing until there is Live TV to be denied.

**`--tuner-type` matters.** M3U and HDHomeRun are separate tuner host
implementations in Jellyfin, with separate discovery and separate stream URLs,
so testing one says nothing about the other. Both are verified working.

**`--tuner-count N`** caps the simulated tuners, so tuning more channels than
that returns `503` — the same answer a real tuner out of capacity gives.

Two things are handled that otherwise fail quietly:

- **A containerised server cannot reach `127.0.0.1`.** That address is the
  container, not the host running faketvsource. The tuner URL becomes
  `host.containers.internal` under podman or `host.docker.internal` under
  Docker, *and* faketvsource is started with `--public-url` so the stream URLs
  inside the playlist are rewritten to match. Getting only the first half right
  gives a tuner that saves cleanly, lists all six channels, and plays none of
  them.
- **A server elsewhere has to be told how to reach this machine**, which is
  what `provision --live-tv-host` is for. The `127.0.0.1` default is correct
  only when the server is local.

### The test accounts

Twelve, each reaching a client path that is otherwise tedious to set up by
hand. Password `stdjflib` throughout, except where the point is not having one.

| Account | What it makes reachable |
| --- | --- |
| `qa-admin` | administrator — dashboard, tasks, library management |
| `qa-user` | everything allowed; the control |
| `qa-nopassword` | no password at all, as home setups often have |
| `qa-restricted` | only Movies and Shows; the rest must be *absent*, not merely unplayable |
| `qa-notranscode` | transcoding and remuxing refused — finds the spinner where an error belongs |
| `qa-nodownload` | downloads and sync refused |
| `qa-noplayback` | can browse, cannot play — separates "can list" from "can play" |
| `qa-kid` | parental cap plus unrated blocked, so most of the library vanishes; empty rows and empty libraries |
| `qa-nosyncplay` | SyncPlay refused |
| `qa-onesession` | one session; a second login must evict the first |
| `qa-hidden` | not in the public user list, but can still sign in by name |
| `qa-disabled` | authentication must fail cleanly, not hang |

### Two things that fail silently

Both were found by reading the server source and confirmed against a running
one, and both look like they work if you do not check.

**`LibraryOptions.EnableInternetProviders` does nothing.** It is in the DTO and
referenced nowhere else in the server. What actually gates a remote provider is
a `TypeOptions` entry with an empty `MetadataFetchers` array — and the check is
"does this library have a TypeOptions", so a type with *no* entry falls back to
the server defaults, which have the internet providers on.

**Per-library options cannot reach every item.** A `MusicArtist` is derived
from tags rather than a folder, so it has no library path, so its options come
back null, and `ProviderManager` then allows every provider through by design.
The first run of this leaked live MusicBrainz lookups for every artist despite
the per-library switch being set correctly. Closing it needs the *server-wide*
`MetadataOptions` to list each remote provider as disabled as well. Both layers
are applied; either alone is insufficient.

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

138 tests, well under a second, needing no ffmpeg, server or container runtime. They cover the matrix's
internal consistency, the licence rules, NFO output and determinism, ffmpeg
argument assembly, and the VobSub encoder — including a round-trip of the RLE
against an independent reference decoder written in the test, which is the only
thing that would catch a wrong nibble producing a file that still parses and
renders garbage.
