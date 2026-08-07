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

Run the tests with `python3 -m unittest discover -s tests -t .` (400 tests,
well under a second, no ffmpeg). Three of them validate the EPUB writers with
**epubcheck** and skip when it is absent — `apt install epubcheck`, which
costs about four seconds of JVM startup when present. It is a development
tool, never a dependency: the suite must keep passing on a machine that has
never heard of Java. To try it for real, build the minimal tier —
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
| `stdjflib/books.py` | the hand-written EPUB, PDF, CBZ/CBT and comic-metadata writers, and the derived filler text the long ones are filled with |
| `stdjflib/boxsets.py` | `collection.xml`, and the Emby-dialect parser it is written against |
| `stdjflib/strm.py` | `.strm` stream files, and the parsing rule they are written against |
| `stdjflib/origin.py` | the local HTTP origin those stream files can point at |
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
| `stdjflib/web.py` | building jellyfin-web in a container, so npm never runs here |
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

**`lockdata` also locks the NFO out, so changing one never reaches a server
that already scanned it.** `MetadataService.RefreshMetadata` returns at
`if (item.IsLocked) return refreshResult;` *before* it runs the local
providers, so an item already in the database is never re-read from its NFO —
not on a scan, and not on a full refresh with `replaceAllMetadata=true` either.
Measured: adding seven fields to every series NFO and rescanning changed
nothing on the six series already there, while the one series new to that scan
came back with all of them.

Images are the exception and the asymmetry is deliberate:
`ProviderManager.CanRefresh` returns true for an `ILocalImageProvider` *above*
the `IsLocked` check, so local artwork refreshes on an ordinary scan while
local metadata does not. That is why a redraw propagates to a running server
and an NFO edit does not.

So an incremental rebuild propagates media and artwork but not metadata, and
the item has to be new to the *database* before its NFO is read again.

**`--replace-libraries` does not achieve that, despite looking like it should.**
`LibraryManager.RemoveVirtualFolder` deletes the shortcut directory under
`DefaultUserViewsPath` and nothing else — the item rows survive, and because
Jellyfin derives an item's id from its path, re-adding the same folder adopts
every one of them back with `IsLocked` intact. `jfapi.remove_library` sends
`refreshLibrary=false`, so not even `ValidateTopLibraryFolders` runs to prune
the orphans. Measured: after `--replace-libraries` recreated all fifteen
libraries and rescanned 4774 items, every pre-existing item still had its old
metadata and only the two items whose *paths* were new had the new fields.

`serve --fresh` is the one that works: it deletes the state directory, so the
database starts empty and every NFO is read. It keeps the `-build` artifacts
directory, so there is no dotnet rebuild — the cost is the wizard, the twelve
accounts and a full scan, about five minutes here for the full tier.

**What an NFO writes is what `MediaBrowser.XbmcMetadata/Parsers/` reads.**
Not what Kodi documents and not what `BaseNfoSaver` writes — the two disagree.
`outline` is the standing example: the saver writes it, the parser has no
`case` for it, so it round-trips and changes nothing. Four fields the parser
*does* read are left out deliberately and `nfo.py`'s docstring says why —
`watched`/`playcount`/`lastplayed` (they set one named user's view history),
`trailer` (a URL, in an otherwise offline library), `lockedfields` (redundant
under `lockdata`), and `namedseason` on a series (Jellyfin parses it with
`reader.Skip()`; season names come from `season.nfo`'s `seasonname`). Adding a
field means finding its `case` first.

**Books are the exception to every NFO rule here, because the server has no
Book NFO parser at all.** `MediaBrowser.XbmcMetadata` has no `BookNfoParser`
and no `BookNfoSaver`, and `BaseNfoProvider<T>` is subclassed for Movie,
Video, MusicVideo, Series, Season, Episode, MusicAlbum and MusicArtist —
nothing else. A `.nfo` beside a `.epub` is read by nobody, so `lockdata` is
never set on a Book, `IsLocked` is never true, and an NFO written there would
be a file that looks like coverage and is inert. `build_books` writes none.

What keeps that library off the internet instead is the two provider layers
that are already there — the per-library `TypeOptions` entry and the
server-wide `MetadataOptions`, both of which already list `Book` and
`AudioBook`. And it means the usual "an NFO change never reaches an item the
server already scanned" does *not* apply to Books: with nothing setting
`IsLocked`, a book's local providers re-run on an ordinary scan.

**`ImageFetchers` is not the internet's list alone, and Books need theirs.**
`ProviderManager.CanRefreshImages` returns early for an `ILocalImageProvider`
and *nothing else*, so an `IDynamicImageProvider` — one that derives a picture
from the file itself, with no network anywhere — is gated by the same array
that keeps TMDB out. Emptying it for every type therefore switched off
`ComicImageProvider` and `EpubImageProvider`, and every book in the library
came back with no artwork at all; measured before and after. For every other
type the empty list is still right, because this library ships its own drawn
artwork and an embedded thumbnail winning over it would be a fixture quietly
replaced. `provision.LOCAL_IMAGE_EXTRACTORS` is the exception and it names
`Book` only; `test_server.py` forbids a remote provider ever appearing there.

**A Books folder changes meaning at its second book.** `BookResolver` counts
the files whose extension is on its own list — `.azw .azw3 .cb7 .cbr .cbt
.cbz .epub .mobi .pdf`, and an NFO, a `.xml` sidecar or a poster does not
count. Exactly one and the *folder* is the book, named after the folder, with
`SeriesName` empty. Two and every file resolves on its own, named after
itself, with `SeriesName` falling back to the parent directory. Nothing warns
at the boundary, so adding a book to a one-book folder silently deletes the
case that folder was covering — `test_books.py:TestFolderShapes` is what
catches it. It also means `BookFileNameParser` is unreachable in a library of
one-book folders, which is what `Ines Imani/` exists to fix.

**An EPUB's `dc:title` beats whatever the filename parsed to.**
`EpubProvider` reads the OPF and overwrites `Name`. That is why the three
author folders come back named after their books rather than their folders,
and it is worth having — but it silently hid the entire filename-parsing
fixture until it was measured against a running server. Every EPUB on the
shelf therefore embeds *the name the parser should produce*, and the one row
the parser gives no name to is a PDF, because no provider reads one.
`test_books.py:test_every_epub_embeds_the_name_its_filename_parses_to` holds
the two to each other.

**An EPUB needs a nav document and a `dcterms:modified`, and Jellyfin will
never tell you they are missing.** EPUB 3 requires both — a manifest item
carrying `properties="nav"`, and the one mandatory `<meta>` in the package
metadata. Every EPUB this tool wrote had neither, and nothing caught it:
`EpubProvider` reads `dc:title` out of the OPF and stops, so the files
resolved, displayed, and passed `verify`. What was lost was the *client* side
— one spine item and no table of contents means nothing to page through, no
chapter to jump to and no TOC to draw, so three of the four things an ebook
reader does had no fixture. `books.epub_structure` re-reads spine length and
the nav declaration and `verify` checks them, because that is the only place
their loss would ever be reported. The nav document is deliberately **not** in
the spine (EPUB 3 allows either), so spine length is the chapter count exactly
and the check has one number to compare.

**`OpfReader` never checks the package version, and most of it is only
reachable from an EPUB 2.** It is a bag of XPaths, and roughly two thirds are
OPF 2 spellings an EPUB 3 file has no way to express: `calibre:series`,
`calibre:series_index`, `calibre:rating` and `calibre:title_sort`, the
`dc:identifier[@opf:scheme=...]` provider ids, `opf:role` on `dc:creator`, and
the `<meta name="cover">` form of the cover. An all-EPUB-3 shelf reaches
`dc:title`, `dc:creator` and `dc:language` and leaves the rest of that file
unexecuted — which is what `Epub Two Dialect/` is for. It is also the other
half of the client story: jellyfin-web pins `epubjs 0.3.93`, whose
`book.navigation` resolves a **nav document** for EPUB 3 and an **NCX** for
EPUB 2, two separate parsers.

Measured on 12.0, and all of it works: `calibre:series` → SeriesName,
`series_index` → IndexNumber 2, `calibre:rating` → CommunityRating 8, all
three `opf:scheme` ids → `{Amazon, GoogleBooks, ISBN}`, `dc:publisher` →
Studios, `dc:date` → PremiereDate, and `dc:subject` "Fiction / Adventure"
splitting into **two** genres. Every `case` in `GetRole` came back with its
own PersonKind, `ctb` came back as **Author** — the contributor silently
recorded as an author, as predicted — `J.R.R. Nakamura` came back respaced to
`J. R. R. Nakamura`, and the one semicolon element came back as two people.

**`calibre:series` beats the filename, and the guard that looks like it should
stop it never sees the filename.** `SeriesName` on a Book has two unrelated
sources — the path, via `BookFileNameParser`, and `calibre:series`, via the
OPF. Measured on 12.0, the OPF wins **both** `SeriesName` and `IndexNumber`,
with no series in the filename and with a conflicting one, and the result is
stable across a second `FullRefresh`:

| Fixture | filename says | OPF says | comes back |
| --- | --- | --- | --- |
| `The Older Format (2004)` | (nothing; folder is `Epub Two Dialect`) | `The Archive Editions` / 2 | `The Archive Editions` / 2 |
| `The Contributors (2006)` | (nothing) | (nothing) | `Epub Two Dialect` / — |
| `The Contested Field (Filename Series, #4) (2011)` | `Filename Series` / 4 | `Opf Series` / 9 | **`Opf Series` / 9** |

Reading `BookMetadataService.MergeData` alone predicts the opposite — it is
guarded by `replaceData || string.IsNullOrEmpty(target.Item.SeriesName)`, and
`BookResolver` really does set `SeriesName` from the parse (or the parent
folder) at resolve time. That prediction was wrong, and the reason is that
`MetadataService.RefreshMetadata` merges **three** times, not once:

1. `var temp = new MetadataResult<T> { Item = CreateNew() }` — a fresh item
   carrying only `Path`, `Id`, `ParentIndexNumber` and the two
   metadata-language fields.
2. every local provider's result is merged **into `temp`**, whose fields are
   still empty, so each guard passes and the OPF value lands.
3. `MergeData(metadata, temp, [], false, false)` — the **backfill**: the
   existing item's own values are merged into `temp` with `replaceData: false`,
   filling only what the providers did not set.
4. `MergeData(temp, metadata, …, shouldReplace, true)` — `temp` is written
   back over the item, and on an ordinary scan `shouldReplace` is *true*
   (`MetadataRefreshMode.Default && !ReplaceAllMetadata`).

So the rule is **first writer into `temp` wins**, and providers always write
before the backfill. A resolver value survives only where no provider spoke —
which is exactly what the shelf shows: `Adrift v02 c015.epub` comes back
`ParentIndexNumber` 2 and `IndexNumber` 15, both straight off the filename,
because no provider touches either.

**`ParentIndexNumber` cannot be put in conflict at all, and the asymmetry that
looked like it mattered does not exist.** An earlier note here reasoned that
because `ParentIndexNumber` is in the step-1 copy list and `IndexNumber` is
not, the two would behave oppositely. They do not: step 3 backfills every
field the providers left alone, so the copy list is belt-and-braces rather
than the thing that preserves the value — measured, `IndexNumber` is *not*
copied and survives anyway.

And there is nothing to conflict with regardless. **No provider in the tree
sets `ParentIndexNumber` on a Book.** `OpfReader` maps `calibre:series_index`
to `IndexNumber`; `ComicInfoReader` maps `ComicInfo/Number` to `IndexNumber`
and never reads `ComicInfo/Volume` at all; `ComicBookInfoProvider` maps
`Issue` to `IndexNumber` and deserializes `Volume` into its model without ever
using it. `BookResolver` is the only writer, so a book's parent index is
always its filename's. `books.comicinfo_xml` writes no `Volume` for the usual
reason — write what has a `case`.

The lesson is the one this file keeps relearning: a guard read in isolation
says what it does, not what it sees.

**A sort title has to sort somewhere the name would not.** `calibre:title_sort`
was `"Older Format, The"` and it *worked* — `ForcedSortName` was set — and it
was indistinguishable from doing nothing, because `BaseItem.GetSortName`
strips `SortRemoveWords` ("the", "a", "an") from the start, the middle **and
the end**, then deletes `SortRemoveCharacters` (`, & - { } '`). So
`"Older Format, The"` and the name-derived `"The Older Format"` both normalise
to `older format`. It now asks for `"Zzz Sorted Last By Title Sort"`, measured
as `SortName` `zzz sorted last by title sort`. Same trap as `Ordered By Name`
in the collections, and it was only caught by putting it in front of a running
server — a fixture has to be able to tell "honoured" from "ignored and the two
agreed".

**A `dc:creator`'s `opf:role` must be a real MARC relator.** epubcheck rejects
anything else with OPF-052, which is how the first draft of `EPUB2_CREATORS`
was caught — it used `zzz` to reach `GetRole`'s `default` arm and produced an
invalid EPUB instead of a fixture. The `default` arm is reached with **`ctb`**
instead: a real relator the server has no `case` for, so a *contributor is
silently recorded as an author*. The table covers every `case` in `GetRole`
plus the default, and `test_the_table_covers_every_case_in_get_role` forbids
it quietly collapsing to eleven rows of Author.

**Two branches of `ReadCoverPath` can never match.**
`//opf:item[@id='cover' and @media-type='image/*']` and
`//opf:item[@id='*cover-image']` are XPath *string literals*, not globs — and
an `id` of `*cover-image` is not a well-formed NCName, so the second is
unmatchable in any valid EPUB. Read from source, **not** measured against a
running server. `books.epub_structure` restates the two branches that do work
and leaves these out, because no fixture can reach them.

**No artwork is drawn beside a book, so an OPF cover is the only image one can
ever have.** `build_books` writes no posters — `provision.LOCAL_IMAGE_EXTRACTORS`
names `Book` alone precisely so `EpubImageProvider` and `ComicImageProvider`
can supply them from the files themselves. Every EPUB here declared no cover
at all until this pair, so every book in the library was artwork-less.

Measured: both come back with `ImageTags: ['Primary']`, so both live branches
of `ReadCoverPath` work. `ReadCoverPath` has two live branches and they are
covered one each, which is the arrangement to keep: `Long Form/The Long Novel (2016).epub` carries the
OPF 3 spelling (`properties="cover-image"`, the first branch) and
`Epub Two Dialect/The Older Format (2004).epub` the OPF 2 one
(`<meta name="cover">`, the last). A writer drifting onto the other's spelling
would leave a branch with no fixture and nothing would fail, so
`test_books.py` asserts each writer emits its own and *not* the other's.

Both are also the reason those two builders are **not** skipped under
`artwork_only`: unlike every other EPUB here they contain artwork, so a redraw
has something to change in them. `_book_cover` draws at 1200x1800 — 2:3, the
poster shape, because a book's Primary image is shaped like a movie's and not
like an album's.

**The long book is mostly Latin on purpose, and its tail deliberately is
not.** `Long Form/The Long Novel (2016).epub` is 24 chapters; the last five
are Cyrillic, Greek, Japanese, Hebrew and Arabic, taken from `subs.SCRIPTS`
rather than a second table that could drift from it. Font fallback in a reader
fails exactly as it does in a subtitle renderer, so the coverage is worth
having — but chapters 1-19 stay Latin because pagination is what the bulk of
the book measures, and a reader that cannot draw CJK would contaminate that.
Each script chapter's title carries the English name *and* the script, so the
TOC is one more place the fallback shows and stays diagnosable when it fails.

**The long fixtures live in a folder of two, and the count is the reason.**
One supported file makes the folder the book, named after the folder — and
nothing in the server reads a PDF's title, so a lone long PDF would come back
named after its directory with no way to say what it is. Two files means each
is named from its own filename. The EPUB still embeds the name its filename
parses to, for the same reason every EPUB on the shelf does.

**Filler text is derived, never random.** `books.paragraphs` expands SHA-256
of the item key into a byte stream and picks words from it, so two builds of
one library produce identical bytes. `random` seeded from the clock and
`hash()` both fail that, and `hash()` fails it invisibly — Python salts string
hashing per process, so it looks deterministic within a single run.

**Comic page counts are a tier decision, not a constant.** A page is one
ffmpeg invocation at about 130 ms and 53 KB. `COMIC_PAGES` is 15 — four was
enough to check the page *count*, which is all `verify` reads, but not enough
for a reader to page through or a jump to land on wrongly. `LONG_COMIC_PAGES`
is 300 and gated to `standard`, because 17 MB is real and minimal is the tier
that promises to be quick. The draws go through `_run_all`, which is what
keeps 405 pages at a few seconds instead of a minute — and which preserves
submission order, so `comic_entries` still predicts the archive's entry order
and the cover rule still applies to it.

**Nothing in the server sets `SeriesName` on an `AudioBook`.** It implements
`IHasSeries`, so the field is there and a client will find it, and the only
writers in the tree are `BookResolver` and the comic and OPF readers — all of
which produce `Book`. Measured: null on all seven audiobook items. A
multi-file audiobook is joined by its `album` tag and by nothing else, which
is why the rip's parts carry one, and why `docs/COVERAGE_GAPS.md` is wrong
where it says otherwise.

**Which makes `album` the only field that says whether a folder is one book
or several, and both answers are now fixtures.** `Lior Levy/` holds three
different books loose in one folder, each with **its own** album; `Mo Mensah/`
holds a rip in a subfolder *and* loose books beside it. Giving any two books
in one folder the same album collapses them into one and deletes the case —
`test_books.py:TestAudiobooks` is what catches it, and it classifies the
folders off the tags rather than off the table row they came from, so a
fixture that stopped answering the question is not still counted under the
shape it was written as.

**A folder holding one audiobook is not a folder in the library at all — it
*is* the item.** `AudioResolver.Resolve` runs on the directory, `FindAudioBook`
finds a single audiobook in it, and the directory resolves as that audiobook:
the file's `Path`, `Path.GetFileName(item.ContainingFolderPath)` as the `Name`,
and the *folder's* parent as the parent. So `The Lantern Keeper` comes back
under `Elena Farrow` and the per-book directory never exists — which reads as
the server flattening something and is not. Only a rip leaves a real `Folder`.

**Every audio file in a directory becomes one stack, whatever it is called.**
`StackResolver.ResolveAudioBooks` groups by directory and by nothing else, so
the "a non-stacking file would produce one item and hide the rest" worry in
earlier notes was wrong about the mechanism: what hides things is a folder
with **exactly one** audio file, which resolves the *whole directory* into one
audiobook and never descends into its subdirectories. Measured — a two-part
rip in a subfolder vanished with nothing logged. That is why `Mo Mensah/`
holds two loose books and not one, and why three unrelated books in
`Lior Levy/` are safe.

**An audio item's `Name` is its `title` tag, not its filename.**
`AudioFileProber` ends with `audio.Name = trackTitle` guarded by nothing but
`LockedFields` — `EnableEmbeddedTitles` is not consulted, unlike video. Every
"named after the folder" claim here holds only because the tag agrees, which
`test_books.py` pins. Same shape as `dc:title` beating a filename for an EPUB.

**A description goes in a different tag per container, and one obvious
spelling is inert.** `AudioFileProber` reads ATL's `Track.Description` and
falls back to `Track.Comment`. For an `.m4b` those are `description` and
`comment`; for an `.mp3` the description is an ID3 **`TIT3`** frame, and
`-metadata comment=` / `-metadata description=` both land in a `TXXX` user
frame ATL files under additional fields — so the file probes, resolves, plays
and comes back with no Overview, silently. ffmpeg has no route to a `COMM`
frame at all (a hand-written one *is* read). `recipes.DESCRIPTION_TAG` and
`COMMENT_IS_READ` are the table, `_audiobook_tags` refuses a comment for a
container that never reads one, and `docs/COVERAGE_GAPS.md` §11 has the
measurements.

**A `Folder` has no local metadata provider of any kind, so no file can give
one a description.** `BaseNfoProvider<T>` is subclassed for eight item types
and `Folder` is not one; `MediaBrowser.LocalMetadata` covers `BoxSet` and
`Playlist` only. Measured: `folder.nfo` and `album.nfo` beside a rip's parts
both left Overview null, so none is shipped — the same rule that keeps NFOs
out of Books entirely. `provision.apply_folder_overviews` sets the one
folder-level description through the API after the scan and reads it back to
check it stuck. It is the only fixture in the library that lives in the
database rather than in a file, and it is there because "the folder's
description beats the files'" is a client rule that otherwise has nothing to
be tested against.

**Four of the seven audiobook folders are a pair of pairs, because an
audiobook's resume window is measured in minutes, not percentages, and the
two lengths are separate fixtures.** (The other three are the folder-shape
fixtures above, and are short on purpose.) `UpdatePlayState`'s `AudioBook`
arm discards a position under
`MinAudiobookResume` (5) minutes in and discards-and-marks-played one under
`MaxAudiobookResume` (5) minutes from the end, consulting the runtime nowhere
else. So under ten minutes **no** position can be stored, and under five
minutes the item cannot even be marked played by playback — the first test
wins and returns before `Played` is set. The 240 s `.m4b` and the 20 s rip
parts are that case on purpose: do not lengthen them, and do not shorten
`The Overnight Vigil` (24 min, 6 chapters) or `The Slow Crossing`'s parts
(12 min), which are the only fixtures where resume, finished-by-playback and
ignored-as-just-started can be told apart. `recipes.py` names the three
positions that reach each answer, because reading them off the video arm's
percentages gives the wrong number every time. Same shape as
`origin-long.mp4` being 400 seconds, and a different cliff.

Both comparisons are strict, so a position exactly five minutes in or exactly
five minutes from the end is *kept* — measured on 12.0, along with everything
else in `docs/COVERAGE_GAPS.md` §9. Two neighbours of that arm: a `Book` is
excluded from *both* arms (`item is not AudioBook && item is not Book`, then
`is AudioBook`), so an EPUB's or a PDF's position is stored verbatim with no
threshold at all — measured; and plain `Audio` does not override
`SupportsPositionTicksResume` while `AudioBook` does, so the same file in
`Music/` can never resume however long it is — read from source, and no
fixture here can reach it, because every track in `Music/` is under
`MinResumeDurationSeconds` and would be zeroed by the video arm first. And
"cannot be marked played" means *by playback*: `POST /UserPlayedItems` sets
`Played` on the 240 s `.m4b` perfectly well.

**A collection is not an NFO and not the Kodi dialect.** `collection.xml` is
read by `BoxSetXmlParser`, which subclasses `BaseItemXmlParser` — the older
Emby XML, rooted at `<Item>`, PascalCase, with its own vocabulary. The element
that sets the name is **`<LocalTitle>`**; `<title>` is not a case in that
parser and does nothing. So the rule that governs `nfo.py` governs `boxsets.py`
too, against a different parser: write what has a `case`. Two fields that do
have one are still left out — `Shares` and `OwnerUserId` are `IHasShares`, and
`Playlist` is the only class in the tree that implements it (the comment at
`Folder.cs:934` claiming BoxSets have per-user visibility is wrong; theirs is
`IsVisibleStandalone` plus the parental check, neither of which is per-user);
and there is no `uniqueid` equivalent, because provider ids here are matched
against `ProviderManager.GetExternalIdInfos`, so a made-up namespace is
dropped rather than round-tripped. The fixture key travels in `<Tags>`.

`v10.11.0` and `master` parse an identical field set — the `case` lists diff
clean — so nothing in that file needs a version guard.

**`BoxSetResolver` takes a folder on either of two conditions, and both are
fixtures.** `[boxset]` in the directory name **or** a `collection.xml` inside
it; either alone is enough, and the suffix is stripped from the resolved name
before `<LocalTitle>` overrides it anyway. It also runs at `ResolverPriority.
First` with no collection-type gate, so it beats `MovieResolver` (Fourth) in
*any* library — which is what puts a working box set inside `Movies/`.

**The two shapes a collection comes in cannot live in the same library.**
`MovieResolver._validCollectionTypes` is movies, homevideos, musicvideos,
tvshows, photos; `IsInvalid` returns true for everything else. So in a
`boxsets` library **no media file resolves to anything at all**, and a
collection whose children come from the disk is empty there. (The comment
above the file branch in `MovieResolver.Resolve` says "the collection type
must be movies or boxsets". The code under it tests only for movies. Believe
the code.) Hence the split, which is not a preference:

| | where | children from |
| --- | --- | --- |
| `collection.xml`, members by path | `Box Sets/` | `LinkedChildren` |
| a folder of films, no XML | `Movies/` | the filesystem |

`BoxSet.IsLegacyBoxSet` is what tells them apart: a path outside the server's
data directory **and** no linked children. Adding a `collection.xml` to
`The Legacy Shelf [boxset]` converts it into the other case and deletes the
one it covers, so `test_collections.py` forbids the movies builder writing
one. Being in the movies library is also what puts it on jellyfin-web's Movies
→ Collections tab, whose query is `itemType: [BoxSet]` parented to that
library — a collection in `Box Sets/` is not in scope there and does not
appear.

**Member paths are relative, and that is the whole reason they survive a
container.** `BaseItem.GetLinkedChild` resolves one through
`FileSystem.MakeAbsolutePath(ContainingFolderPath, path)`, which for anything
unrooted is `Path.GetFullPath(Path.Join(...))` — identical code in 10.11 and
12.0. So `../../Movies/Foo.mkv` is resolved against the collection's own
folder and the library works at a host path and at `/media` both. An absolute
path would bake this machine's mount point in and fail *silently*: the
collection still resolves, it is simply empty. The path has to be the item's
path as Jellyfin recorded it — the media file, not the folder that names the
movie — and a multi-version item's path is its **primary version's** file, not
the folder the manifest records. `build_movies` records that file as
`primary` alongside the folder it calls `path`, and `build._member_paths`
prefers it, which is what lets `Versions Inside A Collection` name both
multi-version films — one whose primary is its exact-name file and one whose
primary is its highest resolution — rather than avoiding them.

**A `collection.xml`'s members are never written to the database, and that
governs everything about these fixtures.** Measured on 12.0: zero
`LinkedChildren` rows for all six on-disk collections, while the API-made and
auto-made ones have theirs. The parse happens — `BoxSetXmlProvider` runs, and
name, overview and tags all arrive — but the members live only on the
in-memory item. So:

| | after a scan | after a restart | after another scan |
| --- | --- | --- | --- |
| `collection.xml` | correct | **empty** | correct again |
| `POST /Collections` | correct | correct | correct |
| `<set>` + auto | correct | correct | correct |
| a folder of films | correct | correct | correct |

`ChildCount` is read off the database, so it reports **0 the whole time**,
including while a `GET /Items?parentId=` on the same item returns every
member. Do not measure a collection with `ChildCount` — that mistake is what
made this look broken when it was not.

The recovery in the third column is the reason **`collection.xml` is the one
metadata file here that must not set `lockdata`**. `RefreshMetadata` returns
at `if (item.IsLocked)` before the local providers run, so a locked
collection can never be re-read and its members never come back. Unlocked, a
scan re-parses and repopulates them. Nothing is lost by leaving it out:
remote fetchers are already off for `BoxSet` in both provider layers, the
same argument that applies to Books.

**So both mechanisms are shipped, and neither is the spare.** The on-disk
files cover `BoxSetResolver`, the XML dialect and this bug; `POST /Collections`
covers what a client's own "new collection" button does and is the only route
whose membership survives a restart. `boxsets.API_COLLECTIONS` is that table,
created by `provision.create_api_collections` after the scan, because a
collection is made of item ids and the items have to exist first.
`Api Made Collection` deliberately holds the same three films as
`The Linked Collection`, so the pair differs only in how it was made.

Both of these are server bugs rather than facts of life, and
`docs/COLLECTION_XML_BUGS.md` is the write-up: reproductions, what was
measured against what was inferred, and the two candidate causes for the
first one, whose root cause is **not** pinned down.

**`<DisplayOrder>` is parsed, saved and can never take effect on a BoxSet.**
`MergeDisplayOrder` copies the value only when `replaceData ||
string.IsNullOrEmpty(target.DisplayOrder)`, and `BoxSet`'s constructor sets
`DisplayOrder = "PremiereDate"` — so the target is never empty, the first
merge runs with `replaceData: false`, and the parsed value is gone before
anything else sees it. `BaseXmlSaver` writes it back out, so it round-trips
and does nothing, exactly like `outline` in the NFO world. Identical code in
`v10.11.0` and on `master`; measured as `PremiereDate` on a file asking for
`SortName`. `Display Order Is Ignored` is that fixture, and it is named for
what it does.

Four smaller differences in the same area, all measured off the same diff:
`CollectionPostScanTask` skips items with a `PrimaryVersionId` on 12.0 and
adds them on 10.11; `AddToCollectionAsync` deduplicates by ItemId only on 12.0
and by ItemId *or Path* on 10.11; `MarkPlayed` on a box set cascades to its
children on 12.0 and marks the box set itself on 10.11; and
`linkedChildAncestorIds` — the `GET /Items` parameter that filters collections
by which library their members came from — exists on 12.0 and nowhere in
10.11.

**A collection's artwork is drawn or absent; there is no third option.**
`CollectionImageProvider` builds a collage from an item's members, and it is a
`BaseDynamicImageProvider`, so the empty `ImageFetchers` that keeps TMDB out
switches it off with everything else — the same asymmetry documented above for
Books, landing the other way. A collection has no media to grab a frame from
either, so the drawn `poster.jpg`/`backdrop.jpg`/`logo.png` beside it are the
only images the item can ever have.

**The library is called `Box Sets`, not `Collections`, and the name is load
bearing.** `CollectionManager.EnsureLibraryFolder` adds a `boxsets` library of
its own, under the localized name **"Collections"**, pointed at
`<data>/collections`, the first time anything creates a server-owned
collection. Two libraries wanting one name is not a fixture. Provisioning
`Auto Collections` makes that happen on the first scan, so the server's
"Collections" library is expected to appear and is not ours.

**A `<set>` does nothing until a library asks for it, and `Auto Collections`
is the only one that does.** `CollectionPostScanTask` skips every library
whose `AutomaticallyAddToCollection` is false — the first thing it tests — so
everywhere else a `<set>` is a field a client can read off a movie and nothing
more. That distinction matters here because `build_test_media` has passed
`collection=rec.group` since the first commit: switch the option on for
`Test Media` and eighty-seven matrix files become eight box sets built in the
server's data directory, which is a different fixture wearing the matrix's
clothes. `test_collections.py` forbids it.

Two rules inside that task have fixtures, and the second is a trap:

- **A set naming one movie creates nothing.** `if (movieIds.Count >= 2)`,
  with the server's own comment above it. `The Set Of One` is that case: the
  field is read, stored as `CollectionName`, and produces nothing to navigate
  to.
- **An existing box set of the same name is added to, not created.** The
  lookup is `boxSets.FirstOrDefault(b => b.Name == collectionName)` over every
  box set on the server, with **no scope of any kind** — so a `<set>` named
  after a collection in `Box Sets/` pours movies into that fixture instead.
  None of the names collide and a test says so. (`SaveLocalMetadata` is false,
  so the fixture's `collection.xml` is not rewritten — only the database
  diverges from it, which is worse, because the file still reads correctly.)

What the task creates never lands in the library it read from, so this is the
one collection fixture `verify` cannot check: it lives in the server's data
directory, `--fresh` deletes it, and the next scan rebuilds it.

**`DisplayOrder` fails to the default rather than failing.** `BoxSet.Sort`
does `Enum.TryParse<ItemSortBy>(DisplayOrder, out var sortBy)` and falls back
to `PremiereDate` when that fails, so a misspelled order is not an error — it
is the default wearing the label of whatever was meant. `Ordered By Name`
therefore asks for `SortName` over two films whose years run opposite to their
names, which is the only way to tell "the client honoured the field" from "the
client ignored it and the two happened to agree". Those two films are also the
filesystem children of `The Legacy Shelf`, which makes them the one pair here
in two collections at once — the case `GetCollectionsContainingItem`, new in
12.0, exists to answer.

**Every NFO sets `<lockdata>true`.** Without it Jellyfin queries TMDB and
friends on scan, and what the client sees then depends on the network, on the
day, and on whatever a stranger last edited. That single field is what makes
this a test fixture instead of a pile of files.

**npm never runs on this machine, and that is what `web.py` is for.**
Building jellyfin-web is `npm ci` over a hundred and thirty packages whose
install scripts run as you — the one place this tool would execute somebody
else's code, in a project whose entire dependency policy exists to avoid that.
So it happens in a rootless podman container: the checkout is mounted **`:ro`**
and copied to scratch inside, the only writable mount is the output directory
under `config.runtime_dir()`, capabilities are dropped, `no-new-privileges` is
set, and `npm ci` runs `--ignore-scripts`. The network cannot be closed —
`npm ci` needs the registry — so it is contained rather than removed.

**Podman only, and Docker is not a fallback.** They are interchangeable in
`container.py`, where the job is running a published image. Here they are not:
rootless podman builds in a user namespace as an unprivileged user, and a
rootful Docker daemon would run the same build as **root on the host**, which
is worse than running npm normally. With no podman, nothing is built and the
server runs `--nowebclient`. Downloading a prebuilt bundle from jellyfin-web
CI is deliberately not implemented either — it is the same trust decision
without the ability to see what went in.

Three things the container turns up that read as tooling faults:

- **`-w /build` fails before anything runs.** podman refuses to start when the
  working directory is absent from the image, exit **126**, "workdir does not
  exist". The script makes its own directory and `cd`s there.
- **Root cannot write to `/`.** The image's root is `dr-xr-xr-x` and
  `--cap-drop=ALL` takes `CAP_DAC_OVERRIDE` with everything else, so
  `mkdir /build` is "Permission denied" *because the hardening is working*.
  The build lives in `/tmp/build`.
- **The bundle is moved into place, never written in place.** An interrupted
  build that left a half-populated `dist/` would satisfy `is_current` on the
  next run and be served.

The build is cached on the jellyfin-web commit, so it costs about two minutes
once and nothing after. A checkout with local edits reports its revision as
`<sha>-dirty` and therefore always rebuilds — serving yesterday's bundle from
an edited checkout looks exactly like an edit that did nothing. A `dist/`
already in the checkout is still used when there is no container build, but
loses to one: its provenance is an npm run nobody here can see.

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

**A command that starts a child must go through `cli._stop_on_signals`.**
Python's default SIGTERM action kills the interpreter where it stands, so no
`finally` runs. Every child here is started with `start_new_session=True` —
deliberately, so stopping one can signal a whole process group instead of
leaving the dotnet host behind — and that isolation also means a signal aimed
at the parent never reaches them. Together: `kill` on a `serve` left a
Jellyfin on 8096 and a faketvsource on 8409, still scanning, and the next run
failed with "port is busy" and nothing pointing at why. Ctrl-C was fine and a
kill was not, which is a difference nobody finds until a script is doing the
killing. `serve`, `container` and `provision` each wrap their body;
`test_every_command_that_starts_a_child_installs_them` is what catches a
fourth being added without it.

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

**A `.strm` is decided by its extension, and never opened.** `.strm` is in
`NamingOptions`' `VideoFileExtensions` *and* its `AudioFileExtensions`, so the
item type comes from the library's collection type and nothing else — Movie in
movies, Episode in tvshows, Audio in music, and `MovieResolver` (priority
Fourth) beats `AudioResolver` (Fifth) everywhere it is eligible. Then
`IsShortcut`, set from the extension in `BaseVideoResolver.SetVideoType` and
`AudioResolver.Resolve`, switches off every provider that would open the file:
`FFProbeVideoInfo` and `AudioFileProber` gate on
`!IsShortcut || EnableRemoteContentProbe`, and `EmbeddedImageProvider`,
`VideoImageProvider`, `AudioImageProvider`, `ChapterManager` and
`TrickplayManager` all return false outright. So a scan reads no streams, no
runtime, no embedded cover, no chapters and no trickplay from one, and
everything a client shows before playback has to be in the NFO and the images
beside it. `MediaSourceManager.GetPlaybackMediaSources` turns remote probing
back on when playback is requested, which is why the stream details arrive
late rather than never.

Two consequences for the fixtures: `music-strm`'s album carries no codec and
no embedded art, because there is nothing to encode into and nothing that
would ever read a cover out of it; and the `.strm` movies carry the full
sidecar artwork set, because those files are not the *preferred* artwork,
they are the only artwork the item can have.

**The NFO does not fill in what the probe skipped.** Measured on 12.0 after a
`--fresh` scan: a `.strm` movie comes back with every NFO field set — tagline,
countries, critic rating, genres, plot — and `RunTimeTicks` **null**, despite
`<runtime>11</runtime>` sitting in that same file as a direct child of
`<movie>` and `BaseNfoParser` having a `case "runtime"` that reads it. So "the
metadata comes from the NFO" is true of everything except duration. Do not
write a fixture plot that promises a runtime; the show and movie plots say
there is none, and that is the measurement.

**An audio `.strm` resolves but never becomes remote.**
`BaseItem.GetVersionInfo` does the shortcut substitution inside
`var video = item as Video; if (video is not null) { … }`, and there is no
matching branch for `Audio`. So a `.strm` in a music library resolves as a
track — `AudioResolver` sets `IsShortcut`, `ProbeProvider.FetchAudioInfo`
even calls `FetchShortcutInfo` and stores the `ShortcutPath` — and its media
source still comes back protocol `File`, `IsRemote` false, path pointing at
the `.strm` itself. Measured: all three tracks of `strm-album`. The fixture is
kept because the resolver half genuinely works and a client is handed the item
either way; its note says plainly that it does not play. Do not "fix" it here
— there is nothing on this side to fix.

**Only `http`, `https`, `rtsp` and `rtp` are honoured in a `.strm`, and both
halves of the check matter.** `ProbeProvider.FetchShortcutInfo` sets
`ShortcutPath` only for those four; `BaseItem.GetVersionInfo` then refuses
again for any shortcut whose protocol resolves to `File`. A local path
therefore yields an item with no usable media source rather than the file it
names — the comment in the server says why, and it is not a subtle reason.
`movie-strm-local-path` is that case on purpose. Do not "fix" it, and do not
add a scheme to `strm.SCHEMES` without finding it in `FetchShortcutInfo`
first.

**The local origin exists so a playback test needs no network, and its URL is
baked in at build time.** The archive.org fixtures are the better test — a
real host, real TLS, real redirects — and unusable in CI or offline, so
`origin.py` serves two generated clips from `.stdjflib/origin/` over HTTP and
two fixtures point at those instead. Three things about it are load-bearing:

- **Range requests must return 206.** `SimpleHTTPRequestHandler` ignores
  `Range` and answers 200 with the whole body; ffmpeg reads that as a server
  that cannot seek, so playback starts and every seek silently does nothing.
  That is why `origin.py` is a hand-written handler and not four lines of
  `http.server`. Verified end to end: `ffmpeg -ss 20` against the origin
  produces a frame, and the request log is all 206.
- **The origin is under `.stdjflib/`, never inside a library folder.** Media
  in a library folder gets scanned, and the clips would become items — the one
  thing a stream *target* must not be.
- **`--stream-origin` is a build flag, not a startup one.** faketvsource is
  told at startup how the server will reach it; a `.strm` was written earlier
  and cannot be told anything, so a container or a remote server needs the
  library **rebuilt** with the right base URL.
  `origin.describe_reachability` is what says so before a scan turns it into
  items that resolve and never play, and `cli._start_origin` prints it.

It is a daemon thread rather than a subprocess, which is why it is the one
long-lived thing here that does *not* need the `_stop_on_signals` treatment: a
thread cannot outlive the interpreter, so no `kill` can leave port 8410 held.

**`origin-long.mp4` is 400 seconds because 300 is a cliff.**
`UserDataManager.UpdatePlayState` enforces
`ServerConfiguration.MinResumeDurationSeconds`, default **300**: anything
shorter has `positionTicks` zeroed and `Played` set outright. Every other clip
here is 12-30 seconds, so before this one there was no item in the library
that could hold a resume point at all, and a resume test against any of them
would have been testing the cliff. The same method keeps a position only
between `MinResumePct` 5 and `MaxResumePct` 90, so the usable window is 20s to
360s. Do not shorten it below 300, and do not raise its bitrate: 400 seconds
at the 1500k the other clips use is ~80 MB against a whole minimal tier of
400, which is why it is 640x360 at 90k with a mono 48k track and lands at 7 MB.

**A `.strm` inside a version set is never remote-probed, so it reports no
runtime.** The trigger in `MediaSourceManager.GetPlaybackMediaSources` is
`item.Path.EndsWith(".strm")` — the *item's* path, and a version set's path is
its **primary's**. So `Local Origin Stream Movie`, whose own path is the
`.strm`, gets probed on PlaybackInfo and comes back with 30s and its streams;
`Local Origin Versions`, whose primary is the local `.mkv`, leaves its
shortcut alternate at `RunTimeTicks` null. Measured both ways, including with
`mediaSourceId` pinned to the alternate — `MediaInfoHelper` passes
`allowMediaProbe: true`, so that is not the gate; the path test is.

Consequence for anyone writing against `Local Origin Versions`: tell its
sources apart by `Path`, `IsRemote` or `Id`, never by runtime. The *media*
still differs threefold — 10 seconds local against 30 remote, which is what a
player sees once it starts — and `ORIGIN_VERSION_LOCAL_SECONDS` names the 10
so the ratio is stated once.

**So there are two version sets, and they are a pair.** `Origin Primary
Versions` names its `.strm` exactly like its folder, which
`OrganizeAlternateVersions` makes the primary outright — so the item's path
*does* end in `.strm`, the gate fires, and both sources come back with their
own runtime and streams. Measured on 12.0:

| Fixture | primary | alternate |
| --- | --- | --- |
| `Local Origin Versions` | File, 10.0s | Http, **runtime None, no streams** |
| `Origin Primary Versions` | Http, 30.0s | File, 20.0s |

One says whether a client reads a *version's own* duration; the other says
what it does when there is none to read. Neither substitutes for the other,
and renaming either one's files to match the other destroys the case it
covers — in particular, making `Local Origin Versions`' shortcut the primary
would flip which side is unprobed and lose the absence entirely.
`ORIGIN_PRIMARY_LOCAL_SECONDS` is 20 rather than 10 so a runtime alone says
which of the two fixtures is on screen.

`ORIGIN_CLIPS` is keyed by filename and `ORIGIN_FIXTURES` maps fixtures onto
it many-to-one, because a clip is a stream *target*: two `.strm` files naming
one file are still two separate items.

`strm.first_line` restates that parser — first line that is neither blank nor
`#`, after tabs and CR are stripped and it is trimmed — and `verify` compares
what it reads against the URL the manifest recorded. It is deliberately a
second statement rather than a call back into `strm.write`, for the same
reason `verify._ARTWORK_STEMS` spells the artwork mapping out again.

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

**In a photos library, a filename decides whether an image is an item.**
`PhotoResolver` drops an image two ways, both silently and both by prefix.
`_ignoreFiles` — folder, thumb, landscape, fanart, backdrop, poster, cover,
logo, default — is matched with `StartsWith` against the filename, so
`cover story.jpg` is not a photograph. And `IsOwnedByResolvedMedia` drops any
image whose name starts with a *video's* name in the same folder, which is
what makes `<clip>-thumb.jpg` artwork rather than an item. The result is a
folder that holds fewer items than it has files, with nothing logged.
`Mixed Content/` puts videos and photographs in one folder on purpose, so it
lives closest to both rules; `test_mixed_content` restates them.

**Multi-version has no setting, and episodes have a version gate anyway.**
Nothing in `LibraryOptions` or the server configuration turns it on or off:
`MovieResolver` passes `SupportsMultiVersion = true` unconditionally, and
`VideoListResolver.Resolve` then picks `GetEpisodesGroupedByVersion` or
`GetVideosGroupedByVersion` purely on the collection type. So the only switch
that exists is the one already being set — a library added as **tvshows** gets
episode grouping and one added as anything else does not. The version gate is
real though: episode grouping is commit `d5bb7756f1`, which is in `v12.0-rc*`
and in no 10.11 tag, so on the container image the same eight files are eight
episodes. Movie versions work on both. `versions-show` says so in its plot,
because a fixture that silently means two different things on two servers is
worse than no fixture.

**The two multi-version spellings pick their primary differently.** For a
movie, a file named exactly like its folder is the primary source outright;
with no such file the resolution in the name decides, matched as
`[0-9]{2}[0-9]+[ip]` and sorted numerically descending. Episodes have no
exact-name rule at all — they are grouped on the parsed season and episode
number, so their primary is the resolution or, failing that, the filename
sort. `libraries.py` carries one fixture per answer, and the tables are
ordered so that reading them tells you which file wins.

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

**`-f m4b` fails exactly like `-f mkv`.** "Error initializing the muxer for
x.m4b: Invalid argument", which says nothing about muxers. Both `mp4` and
`ipod` write a `.m4b` with working chapters; `MUXERS` maps it to **`ipod`**,
which is what ffmpeg itself picks from the extension and which stamps the
MPEG-4 audio brand rather than `isom`. `ipod` had to be added to the
`+faststart` list at the same time — it is not one of `mp4`/`mov`/`3gp`.

**Global container tags go through the same FFMETADATA file as the chapters.**
`-map_metadata` takes one input and a second would replace the first rather
than merge with it, so `Recipe.container_tags` is written into the chapter
file. Values are escaped: `= ; # \` and a newline are FFMETADATA syntax, and
a title containing one would not fail the build — it would produce a file
tagged with something other than what the recipe asked for.

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

## User policies, and the two sets of defaults

`jfapi.default_policy()` sends a **full** policy object because `set_policy`
is a replacement, not a patch — anything omitted is blanked. Its values mirror
`UserEntityExtensions.AddDefaultPermissions` in the server, which is what a
newly created user actually gets.

**There is a second set of defaults and it disagrees.** The `UserPolicy` DTO
constructor has its own values, and both are live on different paths: the DTO's
apply to accounts *migrated* from an older install, because `MigrateUserDb`
deserializes the old policy file into a `UserPolicy` and any field absent from
it keeps the constructor's value. So the answer to "what is the default" is
"which user are you asking about".

`EnableLiveTvManagement` is where this bites: `true` for a migrated account,
`false` for one created today, and there is **no administrator bypass** —
`UserPermissionHandler` asks `HasPermission` and nothing else. A server built
from nothing therefore has no account that can schedule a recording, so the
entire DVR surface (timers, series rules, the Schedule screen) is unreachable
and every client looks broken there in the same way. `qa-user` is granted it
explicitly for that reason; every other account still lacks it, which is a
state a client has to render too.

**`ACCOUNTS[0]` is the account the provisioner is signed in as**, and its
policy *is* applied — which is safe in exactly one direction. `UpdatePolicyAsync`
writes permissions and revokes no session, so a policy that only ever grants
cannot lock the run out; one that takes a right away, or sets `IsDisabled`,
would break the Live TV setup and the scan that follow, and would surface as a
failure somewhere unrelated. `test_the_admin_policy_only_grants` forbids any
`False` there, and `provision` re-authenticates if the session does not survive.

It carries every management permission on purpose. The wizard makes the first
user an administrator and nothing more — `IsAdministrator` is its own
permission and gates none of the others — so without them qa-admin could not
delete an item, manage a collection or schedule a recording. An earlier version
of this entry declared four of them while `provision` skipped the account
entirely, so they were never sent and only read as though they had been.
