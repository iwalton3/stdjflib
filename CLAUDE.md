# stdjflib

Builds a standard Jellyfin QA library — generated media plus licence-checked
downloads — so a Jellyfin client can be tested against a stable, reproducible
library rather than someone's personal collection.

Read `README.md` first; it covers what this is and how to run it. This file is
about working on the code.

## Ground rules

Python standard library plus `ffmpeg`. Nothing else. This is a test tool; a
dependency that can rugpull is worse than a few hundred lines of code. Two
things that look like they need a package do not: image work goes through
ffmpeg (`artwork.py`), and the VobSub encoder is written by hand
(`vobsub.py`).

Run the tests with `python3 -m unittest discover -s tests -t .` (138 tests,
well under a second, no ffmpeg). To try it for real, build the minimal tier —
it downloads nothing and takes about 80 seconds on a 16-core machine:

```sh
./stdjflib.py build /tmp/qa --tier minimal && ./stdjflib.py verify /tmp/qa --tier minimal
```

`-v` prints every ffmpeg command line, which is the fastest way to debug a
filter or muxer problem.

## Layout

| File | What lives there |
| --- | --- |
| `stdjflib/config.py` | tiers, `BuildConfig`, library folders, font selection |
| `stdjflib/recipes.py` | the declarative codec/container matrix — **add coverage here** |
| `stdjflib/generate.py` | Recipe → ffmpeg invocation → file |
| `stdjflib/vobsub.py` | the hand-written VobSub (.idx/.sub) encoder |
| `stdjflib/subs.py` | subtitle sample text and the SRT/ASS/VTT writers |
| `stdjflib/catalog.py` | what gets downloaded, and under what licence |
| `stdjflib/fetch.py` | resumable downloads, the licence gate, unzip |
| `stdjflib/libraries.py` | per-library-type builders and the path conventions |
| `stdjflib/nfo.py` | Kodi-dialect NFO writers |
| `stdjflib/artwork.py` | one shape per Jellyfin image type, drawn by ffmpeg |
| `stdjflib/photos.py` | the opt-in photographic backgrounds (`--use-artwork`) |
| `stdjflib/picsum.py` | the pinned photo ids and their photographers |
| `stdjflib/build.py` | orchestration, manifest, ATTRIBUTION, library README |
| `stdjflib/verify.py` | re-probe everything and compare against the recipes |
| `stdjflib/jfapi.py` | the Jellyfin API client |
| `stdjflib/jfserver.py` | building and running a server from source |
| `stdjflib/provision.py` | library options, the test accounts, setup |
| `stdjflib/container.py` | running the official image under podman/docker |
| `stdjflib/livetv.py` | optional faketvsource tuner and XMLTV guide |
| `stdjflib/cli.py` | argument parsing and the subcommands |

Adding a test case usually means adding one `Recipe` to `recipes.py` and
nothing else: `generate.py` reads the dataclass, `libraries.py` decides the
filename, `nfo.py` writes the metadata, `verify.py` checks the result.

Jellyfin's own resolvers are at `../jellyfin/` — `Emby.Naming/` for the path
conventions (`Video/VideoResolver.cs`, `TV/EpisodeResolver.cs`) and
`MediaBrowser.XbmcMetadata/` for the NFO dialect. When in doubt about what
Jellyfin accepts, read it there rather than guessing.

## Invariants worth not breaking

**Every NFO sets `<lockdata>true`.** Without it Jellyfin queries TMDB and
friends on scan, and what the client sees then depends on the network, on the
day, and on whatever a stranger last edited. That single field is what makes
this a test fixture instead of a pile of files.

**Nothing may depend on wall-clock time or `hash()`.** Dates derive from
`config.EPOCH`; anything that needs a stable pseudo-random value derives it
from the item's key with SHA-256. Python salts string hashing per process, so
`hash("x")` differs between runs of the same program — it looks deterministic
in a single session and is not.

**The codec-matrix files are never hardware-encoded.** `--hwaccel` is opt-in
per file (`generate.build(..., allow_hw=True)`) and `libraries.py` asks for it
only on the large ones. Which encoder produced a matrix file is part of what
that file tests, and NVENC output is not byte-identical to libx264.

**`verify` must actually re-probe.** A build exiting 0 is not evidence: ffmpeg
returns 0 on plenty of partial failures. The ProRes recipe originally declared
`yuv420p` and ffmpeg silently produced `yuv422p10le`, which only `verify`
caught.

**Server state never lives beside the library.** `config.runtime_dir()` puts
the server's data, the dotnet artifacts and the faketvsource log under the
system temp directory, keyed by a SHA-256 of the library root so two libraries
cannot share one database. The library root is routinely a network mount —
sshfs here — and SQLite over sshfs corrupts while `dotnet build` over it is
unusable. Only `.stdjflib/cache/`, the manifest and ATTRIBUTION stay next to
the library, because those are worth keeping and a rebuild needs them.

**The licence gate is two-sided.** `ALLOWED_LICENCES` is the catalog's claim;
`archive_licence()` is what the item says right now. Both have to pass. Do not
add an NC or ND licence to the allowed set — those cannot be redistributed
freely, which is the whole question being answered.

**Bulk items share media and never share artwork.** That split is the whole
design of the `build_bulk_*` functions. Media is hard-linked from a twelve-clip
pool in the cache, because a bulk item exists to be listed rather than played
and a thousand encodes would buy nothing. Artwork is per item, because a
thousand identical posters would never evict anything from a thumbnail cache —
which is one of the main things a library that size is for. Do not "optimise"
by sharing posters, and do not "fix" the shared media by giving every item its
own encode.

**Logo artwork is deliberately hostile.** Transparent PNGs, some white ink and
some black, so no single background colour renders them all. A client that
composites logos badly is supposed to fail here. Do not make them all opaque.

**An image's aspect ratio is the test, not its decoration.** jellyfin-web
takes a row's *median* `PrimaryImageAspectRatio`, snaps it onto 2:3, 16:9, 1:1
or 4:3, and shapes every card in the row from the result — so a poster that is
secretly 16:9 does not look wrong on its own, it reshapes the row and hides
the layout bug being hunted. `artwork.SPECS` is the table; `verify` re-probes
every image and compares. The same ImageType is a different shape depending on
what owns it: Primary is 2:3 for a movie, 1:1 for an album or artist, and 16:9
for an episode. Album art is square — do not "fix" it back to a poster.

**The three artwork naming schemes disagree, and the disagreements are the
server's.** In an item's folder, Thumb is `landscape.jpg` and music's Primary
is `folder.jpg`; beside a loose file, Backdrop is `<name>-fanart.jpg` because
`<name>-backdrop` is only matched for an item in its own folder, and an
episode still is `<name>-thumb.jpg` exactly — `EpisodeLocalImageProvider` has
its own list, `landscape` is not on it, and it registers the file as
**Primary**. Seasons are `season01-poster.jpg` in the *series* folder, with
season zero spelled `season-specials-`. All of it is read out of
`MediaBrowser.LocalMetadata/Images/`; `verify._ARTWORK_STEMS` deliberately
spells the mapping out a second time rather than inverting `artwork.py`'s
tables, because a check that inherits its expectations from the thing it
checks is not a check. `test_artwork.py` ties the two together.

**`--use-artwork` is opt-in and does not go through the licence gate.** There
is nothing for the gate to check — Unsplash's licence is one blanket statement
covering the service, not a per-image claim like archive.org's. So the terms
are met the only other way available: off by default, named in the flag, and
every photographer credited in ATTRIBUTION.md. Do not make it the default, and
do not add the Unsplash licence to `ALLOWED_LICENCES` — that set is the film
catalogue's, and it means something narrower.

**Photographs are assigned by position, never by hash.** The requirement is
that a screenful of thumbnails holds no repeats, and `seq % len(pool)` gives
exactly that for any run shorter than the pool. A hash looks tidier and fails:
40 items drawn from 400 by hash repeat about 86% of the time. That is why
`seq` is plumbed through `folder_images`/`sidecar_images` into `draw`, and why
the bulk builders pass their loop index.

**Text over a photograph gets a shadow, not a scrim.** Darkening the picture
until white text works is the obvious fix and the wrong one: it reads as a
grey rectangle laid over a photo. `drawtext`'s `shadowcolor` plus a hairline
`borderw` is what subtitle renderers do about the same problem, and it leaves
the photograph alone. If a gradient is ever needed again, `_ramp` tiles its
slices **exactly** — a one-pixel overlap is painted twice and shows as a line
at every boundary, which is worse than the hard edge it was replacing.

**`artwork` regenerates by running the builders, not by walking the files.**
`cfg.artwork_only` switches the media steps off (`_emit`, `_link_or_copy`,
`_audio_track`, `_write_epub`, the downloads and the manifest write) and lets
everything else run. That is what lets a redraw add an image type the library
was built without — a pass over what is on disk can only refresh what is
already there. Embedded album art is re-muxed with `-c:a copy`; re-encoding
the audio to change a cover would make the library differ from everyone
else's.

**A truncated download raises no exception.** The server ends the body early
and `read()` just returns empty, so without the explicit
`got != Content-Length` check the build happily muxes a 7% file. This is not
hypothetical: the first real run lost Sintel at exactly that point. The check
plus resume plus `attempts=5` is the whole mechanism — keep all three. Do not
"simplify" it by trusting `urlopen` to raise.

## Jellyfin server gotchas

All of these were read out of `../jellyfin` and then confirmed against both a
source build (12.0) and the official container image (10.11). Each fails
*silently* — the call succeeds and the
setting simply has no effect.

**The auth token goes inside the `Authorization` header.** `X-Emby-Token` is
still read by `AuthorizationContext`, but only as a fallback when the
Authorization header carries no token — and on 12.0 a request with
`X-Emby-Token` and a token-less Authorization header comes back 401. Measured:
token-in-Authorization 200, `X-Emby-Token` 401 with or without the other
header.

**`LibraryOptions.EnableInternetProviders` is vestigial.** It exists in the DTO
and is referenced nowhere else in the server. Setting it false changes nothing.
`provision.library_options` sets it anyway, for honesty, and does the real work
with `TypeOptions`.

**An empty `MetadataFetchers` disables remote fetchers; a missing `TypeOptions`
enables them.** `BaseItemManager.IsMetadataFetcherEnabled` branches on
`libraryTypeOptions is not null`, so a type with no entry falls through to the
server-wide defaults. Hence an entry for every type in `ITEM_TYPES`. Local
providers never reach that check — `CanRefreshMetadata` returns early for
anything that is not an `IRemoteMetadataProvider` — which is why the NFO reader
still runs.

**Per-library options do not cover items with no path.** A `MusicArtist` comes
from tags, has no folder, so `GetLibraryOptions` returns null and
`ProviderManager` deliberately allows every provider through. This is not
hypothetical: the first working run leaked live MusicBrainz lookups for every
artist. The fix is the server-wide `MetadataOptions`, which is the other branch
of the same check. **Both layers are required**; do not remove either.

**The wizard needs `GET /Startup/User` before the POST.** The GET initialises
the default user record; without it the POST has nothing to rename.

**Never build inside the Jellyfin checkout.** `dotnet build` writes `obj/`, and
a checkout that was ever built as root has root-owned ones (42 in the tree this
was written against). It fails with "Permission denied" and a temp-file path
that explains nothing. `--artifacts-path` keeps every output elsewhere.

**The container's media path is not the host's.** Inside the container the
library lives at `/media`, and `provision(media_root=...)` must be told so.
Sending the host path creates the libraries without error and scans them to
zero items, which reads as a Jellyfin fault. `Container.check_library_visible`
runs before provisioning for the same reason — a mount the container cannot
traverse should fail loudly and early, not look like an empty library. FUSE
mounts are the usual culprit; rootless podman handles sshfs here, rootful
Docker may not see it at all if the mount postdates the daemon.

**There is no `GET /LiveTv/TunerHosts`.** It answers POST and DELETE only,
and a GET returns 405 with an empty body — which reads as an auth or routing
problem rather than a wrong verb. What already exists is in
`GET /System/Configuration/livetv`, under `TunerHosts` and `ListingProviders`.

**Live TV needs the guide refreshed before anything appears.** Adding a tuner
and a listings provider leaves the Live TV section empty until the
`RefreshGuide` scheduled task has run, which looks like a broken tuner.

**faketvsource needs `--public-url` whenever the server is not local.** It
builds its stream URLs from the request's Host header otherwise, so a
containerised Jellyfin gets URLs pointing at itself. Both halves are required:
the tuner URL handed to Jellyfin *and* the URLs inside the playlist. Fixing
only the first gives a tuner that saves, lists channels, and plays nothing.

**`wait_until_up` must require a real payload.** A socket that accepts and
closes — a previous server still shutting down on the same port — reads as an
empty body, and treating that as "up" makes the next call fail with a
connection error pointing nowhere near the cause.

## ffmpeg gotchas, all learned the hard way

**The muxer for `.mkv` is `matroska`.** `-f mkv` fails with "Requested output
format 'mkv' is not known" — which says nothing about muxers and sends you
looking at the inputs. `MUXERS` in `generate.py` maps every extension whose
muxer name differs.

**Temp files must keep the real extension.** ffmpeg picks its muxer from the
extension when `-f` is absent, so writing to `album.flac.part` fails with
"Error opening output files: Invalid argument". `ff.temp_path` returns a hidden
sibling with the extension intact. This cost two libraries silently building
nothing.

**Text goes through files, never the filter string.** Labels are written to a
file and read with `textfile=`, so a title containing `:` or `%` cannot corrupt
the filter graph. The whole graph is passed with `-filter_complex_script` for
the same reason — argv escaping and filter escaping do not compose.

**drawtext needs an explicit `fontfile=`.** Without one it takes whatever
freetype picks, which usually has no CJK — and these labels name audio tracks
in their own language, so the Japanese one renders as tofu boxes while
everything else looks perfect. `config.find_font()` prefers a CJK-capable font
for exactly this reason, and `font_for_lang()` asks fontconfig per script where
the language is known.

**`-attach` must come after every `-i`.** Put an input after it and ffmpeg
fails with "Error opening input files: Invalid argument", blaming the input.

**Encoder channel limits are not discoverable from the encoder list.** ffmpeg's
`eac3` and `truehd` encoders both refuse 7.1 — they report "Specified channel
layout '7.1' is not supported" and then fail the conversion with a generic -22
several lines later. `MAX_CHANNELS` in `generate.py` catches this at recipe
level with a message that names the actual problem; `test_recipes.py` checks
every recipe against it.

**ffmpeg cannot encode text subtitles into bitmap ones.** This is why
`vobsub.py` exists. Once a VobSub exists it is a bitmap source, so dvbsub and
xsub are reachable from it by normal transcoding — that is how those recipes
are built.

**A transparent background needs `format=rgba` *and* `-pix_fmt rgba`.** The
`color` source negotiates its own pixel format, so `black@0` alone gets
flattened onto opaque black silently — and the result looks fine, which is how
it goes unnoticed.

**`testsrc2` draws its own counter in the top-left.** Labels are offset below
it rather than over it.

## VobSub, specifically

The one place with real binary-format risk. A wrong nibble produces a file that
still parses and renders garbage, so `test_subs.py` round-trips the RLE through
an independent reference decoder written in the test rather than asserting on
byte patterns. Keep that test honest — do not reimplement it by calling the
encoder.

Points that were not obvious:

- Run length picks the code width: 1 nibble for 1-3, 2 for 4-15, 3 for 16-63,
  4 for 64-255, value always `(count << 2) | colour`. The boundaries are the
  bugs; `test_emit_widths` pins all four.
- Each row is padded to a whole byte.
- A rasterised line of text is 2-4 KB, so **splitting across sectors is the
  normal path, not an edge case**. Only the first PES packet carries a PTS;
  every packet repeats the substream id.
- The terminal control sequence points at itself. That is how a decoder knows
  it has reached the end.

## Deliberately not implemented

- **PGS subtitles.** ffmpeg can decode `hdmv_pgs_subtitle` but not encode it,
  and unlike VobSub the format is not worth hand-writing for one test case.
  VobSub reaches the same burn-in code path in Jellyfin.
- **Dolby Vision.** No encoder exists outside licensed tooling.
- **Checksums in the catalog.** Sizes are recorded for estimating a build, but
  completeness is checked against the `Content-Length` of the actual request.
  Treating a recorded size as a checksum makes the tool fail whenever a mirror
  re-encodes something by a few hundred bytes, which is not corruption.
