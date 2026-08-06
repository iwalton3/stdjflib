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
Shows/           eight series covering season folders, absolute numbering,
                 date-based episodes, double episodes, gaps, flat layouts,
                 multi-version episodes, .strm stream files
Music/           FLAC, MP3, Opus, ALAC; embedded and folder art; multi-disc,
                 various-artists, an untagged album, and one of .strm tracks
Music Videos/    artist/track layout with musicvideo NFOs
Photos/          all eight EXIF orientations, and several image formats
Home Videos/     dated folders of short clips
Mixed Content/   videos and photographs in one tree, at uneven depths: folders
                 that hold one kind, the other, or both
Books/           EPUB and CBZ
Box Sets/        collections, whose members are paths into the libraries above
Auto Collections/
                 films whose NFOs carry a <set>, in the one library where the
                 server turns that into a box set of its own

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
| `minimal` | none | 0.4 GB | 268 | ~40 s | generated media only — the CI tier, works offline |
| `standard` | 2.4 GB | 3.8 GB | 278 | ~2.5 min | the Blender open movies, 24 real subtitle files, public-domain shorts |
| `full` | 3.4 GB | 8.5 GB | 4799 | ~4 min | 4K, 60 fps, stereoscopic 3D, MPEG-2 and Cinepak derivatives, 8K, and the six `Bulk *` libraries |

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

**Books** EPUB · PDF, whose page count is the only number a client can read
back off a book · CBZ and CBT · the three comic metadata dialects, two of
which the server reads **only** from a `.cbz` · both comic cover rules,
including the one that picks the wrong page · `.azw3` and `.mobi`, which
resolve with full metadata and which no client can open · **audiobooks in
both shapes**: one chaptered `.m4b` that comes back as a single item with
real chapter rows, and a six-part rip that comes back as six items · and one
book of every filename convention `BookFileNameParser` recognises

**Collections** both shapes a box set comes in — one whose members are paths
in a `collection.xml` and one that is simply a folder of films — and both
conditions `BoxSetResolver` accepts, the `[boxset]` suffix and the presence of
the file · one collection in the movies library, which is the only place
Jellyfin's own Movies → Collections tab looks · a non-default `DisplayOrder`
over members whose dates and names disagree · members drawn from two libraries
and of two item types · both multi-version films, named by the file that is
actually their item path · one member that deliberately resolves to nothing,
which 12.0 drops for good and 10.11 keeps · and a library where the *server*
builds the collections, from `<set>` tags, including a set of one that it
refuses to build at all

**Paths** loose files · folder/file name disagreement · multi-version films,
both spellings — resolution tags with no exact-name file, and named editions
behind one · **multi-version episodes**, tagged by resolution and by cut, in a
season folder and in a folder of their own · multi-part stacking · trailers
and extras folders · unicode and right-to-left titles · titles long enough to
overflow any column

**Remote sources** `.strm` stream files, which are a line of text where a media
file would be: as a loose movie, in a folder of its own, as an episode, as
`.strm` tracks in a music library, and as one alternate version of a film whose
other version is on disk. Plus the two the server is *supposed* to refuse — a
scheme it does not accept, and a filesystem path, which it declines twice over
because honouring it would make a `.strm` a way to read any file on the server.
Five more play from an HTTP origin on this machine — among them both
spellings of a version set and a 400-second item — so an end-to-end playback
test needs no network. Nothing is downloaded for any of them at any tier; see
[Stream files](#stream-files)

**Metadata** every field `MediaBrowser.XbmcMetadata`'s parsers actually read —
taglines (including one absent and one far too long) · critic rating out of
100 beside a community rating out of 10 · custom rating, which is parental
control's field and not `mpaa` · production countries · per-episode cast and
guest stars · specials that declare where in watch order they belong ·
`displayorder` absolute, which changes how the *server* numbers episodes ·
air day and time · series end dates

**Artwork** every image type Jellyfin has, at the shape it expects it —
Primary as a 2:3 poster, as a 1:1 album cover and as a 16:9 episode still ·
Backdrop · Banner at 5.4:1 · transparent Logo and clearart · Disc · season
posters in both places the server looks for them, on every season including
the ones with no folder of their own · a landscape and a backdrop on every
season as well as a poster · sidecar naming beside loose files as well as
folder naming · photos in a spread of aspect ratios

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

## Stream files

A `.strm` is one line of text holding a URL. Jellyfin resolves the item from
the *path*, exactly as it would for an `.mkv`, then plays the URL instead of a
file — which makes it the one case where an ordinary-looking item has a media
source that is not local. There are fifteen of them here, and none of them
downloads anything at any tier, because a stream file is a line of text.

The behaviour worth knowing, all of it read out of the server:

- **The extension decides the item type; the target does not.** `.strm` is in
  `NamingOptions`' video extension list *and* its audio one, so the same file
  is a Movie in a movies library, an Episode in a tvshows one and a track in a
  music one. Nothing looks inside first. That is why there is a `.strm` album
  as well as `.strm` films.
- **A shortcut is never probed on a scan.** `IsShortcut` turns off ffprobe,
  embedded image extraction, chapters and trickplay alike, so everything a
  client shows before playback has to come from the NFO and the images on
  disk. Remote probing is switched back on only when playback is actually
  requested, which is why the stream details arrive late rather than never.
  Measured on 12.0: every NFO field lands — tagline, genres, countries,
  ratings, plot — and the **runtime does not**, despite `<runtime>` being in
  the same file. A `.strm` item has no duration and no resolution until
  something plays it.
- **The file format tolerates more than it looks like it does.** `#` comments,
  blank lines and leading tabs are all skipped, and everything after the first
  URL is ignored — a `.strm` is one source, not a playlist. One fixture is
  built out of exactly that: a comment header with a decoy URL inside it, an
  indented real URL, and a second URL underneath that must not be read.
- **Only `http`, `https`, `rtsp` and `rtp` are honoured**, and the check is
  made twice — once in the parser, again in `BaseItem.GetVersionInfo`. A
  `.strm` naming a local path resolves to an item with *no usable media
  source* rather than to the file it names, because honouring it would make a
  stream file a way to read any file on the server. That fixture is here, and
  so is an `rtsp://` one, whose point is the protocol field rather than the
  stream.
- **A `.strm` groups as a version beside real media.** Multi-version matching
  compares filenames without their extensions, so `Local And Remote Versions`
  is one film with a local primary and a remote alternate, and the stream show's
  episode three is one episode with both.
- **Five of them need no network at all**, including both spellings of a
  version set and a 400-second item for resume tests — see
  [the local origin](#the-local-origin-for-tests-with-no-network).
- **The audio case resolves but does not play, and that is the server's.**
  `BaseItem.GetVersionInfo` swaps the URL in for the file only inside
  `if (item is Video)`, with no branch for `Audio` — so a `.strm` track comes
  back with protocol `File` and its media source pointing at the text file.
  The album is here because the resolver half genuinely works and a client is
  handed the item regardless; its note in the library says so.

Most of the playable ones point at public-domain Prelinger shorts already named
in `catalog.py`, so a stream file cannot reference something the licence gate
has never had an opinion about; they are credited in `ATTRIBUTION.md` whether
or not the tier downloaded a copy. The two unplayable ones are deliberately
local — an unroutable loopback port and a filesystem path — so pressing play on
either fetches nothing from anybody.

### The local origin, for tests with no network

A real remote host is the better test and the wrong one for CI, a metered
connection, or a machine that is deliberately offline. So two fixtures —
**Local Origin Stream Movie** and the stream show's **S01E04** — point at an
HTTP server running on this machine instead:

```
.stdjflib/origin/origin-movie.mkv     30s, h264 + aac, in Matroska
.stdjflib/origin/origin-episode.mp4   30s, h264 + aac, in MP4
.stdjflib/origin/origin-long.mp4      400s, 640x360 at 90k + mono aac — 7 MB
```

Five fixtures point at those three clips — a clip is a stream *target*, not an
item, so two `.strm` files naming one are still two items:

| Fixture | Why it exists |
| --- | --- |
| `Local Origin Stream Movie` | the plain case: a loose `.strm`, played from this machine |
| `Remote Stream Show` S01E04 | the same, as an episode |
| `Local Origin Versions` | **a version set with no network**: a 10s local primary beside the 30s origin clip, so switching version switches between a local file and a URL |
| `Origin Primary Versions` | the same set built the other way up — the `.strm` is named exactly like its folder, so it is the *primary* and a 20s local file is the alternate |
| `Long Origin Stream Movie` | 400 seconds, for resume and progress |

**The two version sets are a pair, and the difference between them is the
server's.** `MediaSourceManager` forces the remote probe only when the
*item's* path ends in `.strm` — and a version set's path is its **primary's**.
Measured on 12.0 from `PlaybackInfo`:

| Fixture | primary | alternate |
| --- | --- | --- |
| `Local Origin Versions` | File, 10.0s | Http, **no runtime, no streams** |
| `Origin Primary Versions` | Http, 30.0s | File, 20.0s |

So a shortcut sitting *inside* a set is never probed, however you ask —
pinning `mediaSourceId` to it does not help. Naming the `.strm` exactly like
its folder makes it the primary, puts a `.strm` back in the item's path, and
the gate fires. One fixture tells you whether a client reads a version's own
duration; the other tells you what it does when there is none to read. In
`Local Origin Versions`, tell the sources apart by `Path`, `IsRemote` or `Id`
— the media really is 10s against 30s once playing.

The 400-second one is the only item in the library a resume test can use.
`UserDataManager.UpdatePlayState` enforces `MinResumeDurationSeconds`, 300 by
default: below it the position is zeroed and the item is marked played
outright, and every other clip here is 12–30 seconds. The position is only
kept between `MinResumePct` 5 and `MaxResumePct` 90, so the window that holds
one is 20s–360s. Its bitrate is deliberately poor — 400 seconds at the rate
the other clips use would be some 80 MB in a minimal tier of 400, and nothing
about a resume point needs to look good.

`stdjflib serve` starts that server alongside Jellyfin, on port 8410. As far
as Jellyfin is concerned the source is exactly as remote as archive.org —
protocol `Http`, `IsRemote` true, no probe until playback — but nothing leaves
the machine. Measured: `PlaybackInfo` on both comes back with the real 30s
runtime, `Video:h264, Audio:aac`, and direct play.

It is a file server with one thing that is not optional: **byte ranges**.
Python's `SimpleHTTPRequestHandler` ignores `Range` and answers 200 with the
whole body, which ffmpeg reads as a server that cannot seek — so playback would
start and every seek would silently do nothing. Hence a hand-written handler,
206 responses, and `ffmpeg -ss 20` against it as the check.

The clips live under `.stdjflib/` rather than in a library folder, because
media inside a library folder is scanned and the origin would become items in
its own right — the one thing a stream target must not be.

**The URL is written into the `.strm` files at build time**, which is the same
trap `--public-url` covers for faketvsource, arriving from the other side:
faketvsource is told at startup how the server will reach it, and a stream file
cannot be told anything. A server that is not on this machine needs the library
rebuilt:

```sh
./stdjflib.py build /srv/qa-library \
    --stream-origin http://host.containers.internal:8410
```

`stdjflib container` says so rather than letting the scan produce items that
resolve and never play. `--no-stream-origin` leaves the server unstarted, which
is a state worth being able to reach on purpose.

`stdjflib verify` re-reads every stream file and checks that the line Jellyfin
would take from it is still the URL the build recorded — and, for the local
ones, that the file they name is actually there to serve.

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
| `qa-admin` | administrator — everything allowed: dashboard, tasks, library management, recordings, deletion |
| `qa-user` | everything a non-admin can have; the control. Includes recording management |
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

## Books

The Books library is the one place where the *files* are not media, and where
almost everything a client has to get right is decided before a byte is read.
Six things are worth knowing, all of them read out of the server and then
measured against a running one.

**A folder holding exactly one book *is* the book.** `BookResolver` counts the
files whose extension is on its list — `.azw .azw3 .cb7 .cbr .cbt .cbz .epub
.mobi .pdf`, and nothing else counts, so an NFO or a sidecar XML is invisible
to the tally. Exactly one, and the folder resolves as a single book named
after the **folder**. Two, and the rule stops applying: every file is resolved
on its own and named after **itself**. Nothing warns at the boundary, and the
two paths disagree about `SeriesName` — a loose file falls back to its parent
directory's name, a directory-book to the empty string.

So `Ines Imani/` holds six books precisely so that the filename parser runs at
all; in a library of one-book folders it never does.

**Books read no NFO.** There is no `BookNfoParser` and no `BookNfoSaver` in
`MediaBrowser.XbmcMetadata` — nothing parses one for a `Book` or an
`AudioBook`. This is the one library here whose metadata does not come from an
NFO and cannot; it comes from the formats themselves. What keeps it off the
internet is the per-library `TypeOptions` and the server-wide `MetadataOptions`
instead, which is why both are set for `Book` and `AudioBook`.

**`.cba` is not a book.** It looks like it belongs beside `.cbz` and `.cbr`,
and the server has never accepted it. A `.cba` in a books library resolves to
nothing at all and sits there as a file with no item.

**The three comic dialects, and which of them your archive gets.**

| Where the metadata is | Restriction | Fixture |
| --- | --- | --- |
| `ComicInfo.xml` **beside** the archive | any archive type | `The Signal Archive 002.cbz` |
| `ComicInfo.xml` **inside** the archive | **`.cbz` only** | `The Signal Archive 003.cbz` |
| ComicBookInfo JSON in the **zip comment** | **`.cbz` only** | `The Signal Archive 004.cbz` |

The server asks them in a fixed order — ComicBookInfo, external, internal —
and takes the **first** that finds anything, so each fixture carries exactly
one dialect and every one of them sets a `Title` naming the dialect it came
from. A comic showing its filename is a comic whose metadata was not read, and
you can see that without opening anything.

`Ignored Internal Info 005.cbt` is the restriction itself: the same
`ComicInfo.xml`, in an archive the server reads perfectly well — it still
extracts a cover and counts the pages — and ignores as metadata purely because
the extension is not `.cbz`.

**The cover is an exact name or an alphabetical accident.** `ComicImageProvider`
looks for an entry called exactly `cover.<ext>` at the archive root (tried
`.png`, `.jpeg`, `.jpg`, `.webp`, `.bmp`, `.gif`, so `.png` beats `.jpg`; no
path prefix, and `Cover.jpg` does not match), and failing that takes the
**alphabetically first image by entry key**. A comic whose pages are `001.jpg`
onwards passes that by luck. `Scan Credits Cover 006.cbz` is the realistic way
it goes wrong — a scanlator credit page filed as `000 - ` sorts ahead of page
one and becomes the cover — and `Named Cover 007.cbz` is the identical archive
with `cover.jpg` added and nothing else changed, so the difference between the
two covers is the rule rather than the artwork.

**Page counts are entry counts.** For a comic archive the server counts every
non-directory entry, so an internal `ComicInfo.xml` makes a four-page comic
report five. That is stated rather than corrected: a client showing five is
reading the server correctly. A PDF is counted properly with PDFium, and an
EPUB gets a flat `TimeSpan.TicksPerSecond` — page position in an EPUB is a
percentage, not a page.

**A PDF is the one book type with no artwork at all.** There is no PDF image
provider, so `The Standard Manual (1994).pdf` renders with whatever a client
draws when there is no poster — which is otherwise never exercised, because
every comic here has a cover extracted from its own pages.

### Audiobooks

`AudioBook` is `Audio` with a different resolve, and it only happens inside a
**books** library: the same file in `Music/` is an ordinary track and takes a
different path through every client. The server produces two shapes and they
are not variations on one another.

| Fixture | What comes back |
| --- | --- |
| `Elena Farrow/The Lantern Keeper/The Lantern Keeper.m4b` | **one** AudioBook, 8 real chapter rows, named after the *folder* |
| `Gus Gupta/The Divided Account/Chapter 01–06.mp3` | **six** AudioBooks, one per file |

So "chapter 7" is a marker in the first case and item 7 in the second — two
code paths for one gesture, and only the first reuses a client's existing
chapter UI.

Chapter extraction is enabled for this item type and no other
(`ExtractChapters = item is AudioBook`), and it does nothing more than add
`-show_chapters` to ffprobe. There is no per-file, cue-sheet or filename
fallback anywhere, so markers the container does not carry are markers that do
not exist — which is why the six-part rip reports no chapters at all.

**The six parts are joined by their `album` tag and by nothing else.**
`AudioBook` implements `IHasSeries` and **nothing in the server ever sets
`SeriesName` on one** — measured, it comes back null. Group a multi-file
audiobook by `Album`/`AlbumArtist`; `SeriesName` is a field that exists and is
always empty. The author is the `album_artist` tag and the narrator is
`composer` (Audiobookshelf's convention, which the server adopted), and they
arrive as `Author` and `Narrator` people.

**Why six files become six items is worth knowing, because it is fragile.**
The parts stack at scan time into one six-file audiobook, which the server
then drops outright — "until we sort out naming for multi-part books". Zero
items is what saves them: the library manager only takes a multi-item
resolver's answer when it produced at least one, so it falls through and
resolves each file on its own. Put a *seventh* audio file in that folder that
does not stack with the rest and the folder yields one item, the early return
fires, and the six parts vanish from the library with nothing logged.

## Collections

A collection is the one item here made entirely of references. It owns no
media, its content is a list of other items, and every way it can be wrong is
a way that produces a collection which resolves, renders, and is quietly
missing things.

**There are two shapes, and they are in different libraries because they have
to be.** `Box Sets/` holds collections that name their members in a
`collection.xml`; `Movies/The Legacy Shelf [boxset]/` is a collection that is
simply a folder of films, with its children read off the disk. The split is
not tidiness: `MovieResolver` refuses a `boxsets` library outright — its list
of valid collection types is movies, homevideos, musicvideos, tvshows, photos
— so a film inside a box set in `Box Sets/` would resolve to nothing at all.
Jellyfin tells the two shapes apart by absence, in `BoxSet.IsLegacyBoxSet`: no
linked children, and a path outside the server's own data directory.

Being in the movies library is also what puts the legacy one on jellyfin-web's
**Movies → Collections** tab, which queries box sets parented to that library.
A collection in `Box Sets/` appears in its own library's tabs and not there.

**A folder becomes a collection two ways, and both are covered.** Either the
directory name contains `[boxset]`, or it holds a `collection.xml` — one
fixture each, which is why one of them is called `Collection Without The
Marker`. `BoxSetResolver` runs ahead of every media resolver and applies in
any library type, not just a `boxsets` one.

**`collection.xml` is not an NFO.** It is the older Emby XML — rooted at
`<Item>`, PascalCase — and the element that sets the name is `<LocalTitle>`.
A `<title>` written in the Kodi dialect the rest of this library uses is read
by nobody. The whole file is written against `BaseItemXmlParser`'s case list
in the same way the NFOs are written against theirs.

**Members are relative paths, deliberately.** Jellyfin resolves a member
against the collection's own folder, so `../../Movies/Loose File Movie
(2020).mkv` works at whatever path the library is mounted at — including
`/media` inside a container, where an absolute host path would resolve to
nothing. That failure is silent: the collection is still there, still named,
simply empty. `verify` resolves every member the way the server does for
exactly that reason.

**One difference between server versions is worth knowing before you file a
bug.** On 12.0 a member that resolves to nothing is dropped permanently — the
linked children live in a database table whose child column is a non-nullable
id, and a path has nowhere to survive. On 10.11 the same link is kept as JSON
on the item and starts working on a later scan. So an identical library can
show a collection short an item on one server and complete on the other, and
neither logs anything a user would see. `MarkPlayed` on a collection also
differs — 12.0 marks the members, 10.11 marks the collection — and
`linkedChildAncestorIds`, which filters collections by the library their
members came from, exists on 12.0 only.

**A collection's artwork is drawn here or it has none.** The provider that
builds a collage out of a collection's members is a dynamic image provider,
and this library switches those off along with the internet ones, so the
poster, backdrop and logo beside each `collection.xml` are the only images the
item can have.

**`Ordered By Name` overrides the order its members appear in**, which is a
field that fails to the default rather than failing. Jellyfin parses
`DisplayOrder` as a sort key and falls back to `PremiereDate` when it cannot,
so a typo is indistinguishable from a deliberate default. The two films in it
have years running opposite to their names, so "the client honoured the field"
and "the client ignored it" cannot look the same. Those two are also the
contents of `The Legacy Shelf`, making them the one pair here that belongs to
two collections at once.

**`Two Libraries, One Collection`** holds a series and a film. A linked child
can be any item, and a client that assumes a box set is a list of movies fails
on it. It is also what `linkedChildAncestorIds` is for — the `/Items`
parameter that filters collections by which library their members came from,
which exists on 12.0 and in no 10.11.

**`Versions Inside A Collection` names files, not folders.** A multi-version
film's item path is its *primary version's* file, and the two spellings pick
that differently — an exact-name file wins outright, and with none the highest
resolution does. Naming the folder instead gives a member that resolves to
nothing, so both films are in here named the way the server records them.

**`One Member Is Missing` is broken on purpose**, and it is where the two
server versions part company. One member resolves and one names a file that
has never existed. On 12.0 the missing one is gone for good; on 10.11 it is
kept and would start working the moment the file appeared. `verify` reports it
as a note rather than a problem, and fails if something ever *does* turn up at
that path.

## Collections the server builds

`Auto Collections/` is an ordinary movies library. Nothing in it says
"collection" — the films are loose `.mkv` files with NFOs, and the only thing
that distinguishes it is that three of its four NFOs carry a `<set>`, and that
it is the one library provisioned with `AutomaticallyAddToCollection` on.

After a scan, the server has built a box set called **The Automatic Set** out
of the two films that name it. That box set is not in this library or in any
folder on disk: it lives in the server's own data directory, inside a
`boxsets` library the server adds for itself and calls "Collections". Deleting
the database deletes it, and `serve --fresh` rebuilds it on the next scan.
It is the one collection fixture here that cannot be verified offline.

Two more of the films are there to cover what the server *does not* do.
**The Set Of One** names a set that only one film belongs to, and Jellyfin
refuses to create a collection for fewer than two — so that field is read,
stored, and produces nothing a client can navigate to. **In No Set At All**
carries no `<set>`, and must end up in no collection whatsoever.

The option is off for every other library, deliberately. `Test Media/` has
carried a `<set>` per codec group since the first commit; switching it on
there would silently turn the codec matrix into eight box sets.

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

### Rebuilding over an existing library

`build` on a directory that already holds a library rewrites it in place, and
`--only <folder>` limits it to the libraries you changed — the manifest carries
the rest forward, so a partial rebuild does not forget what it did not touch.

One thing to know if a server has already scanned it: **NFO changes do not
reach an item Jellyfin already has.** `<lockdata>true</lockdata>` is what keeps
this library reproducible, and the same flag makes
`MetadataService.RefreshMetadata` return before it reads the NFO — so a rescan,
and even a full refresh with "replace all metadata", both leave the old values
in place. Artwork is the exception and refreshes normally. To pick up metadata
changes the item has to be new to the *database*, which means starting the
server from an empty one:

```sh
./stdjflib.py serve /srv/qa-library --fresh
```

`--replace-libraries` is not enough, however much it looks like it should be:
removing a library deletes Jellyfin's shortcut folder and leaves the items,
and since an item's id comes from its path, re-adding the same folder adopts
them all back — still locked, still holding the old metadata. `--fresh`
deletes the server state instead, so the database starts empty. It keeps the
compiled server, so the cost is the setup wizard and one full scan.

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

299 tests, well under a second, needing no ffmpeg, server or container
runtime. They cover the matrix's internal consistency, the licence rules, NFO
output and determinism, ffmpeg argument assembly, the Books library's naming
and archive rules, and the VobSub encoder.

Two of them are round-trips against a reference implementation written in the
test rather than a call back into the writer, because both formats are ones a
wrong byte leaves *parseable*: the VobSub RLE, where a wrong nibble produces a
file that still renders — as garbage — and the hand-written PDF, whose
cross-reference table a real reader silently recovers from by scanning, so
wrong offsets look perfect everywhere else.

`BookFileNameParser` is ported into the tests and pinned against Jellyfin's
own vectors, so a drift in what this library believes about filenames fails
here rather than in a fixture nobody re-reads.
