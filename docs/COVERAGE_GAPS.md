# Library coverage gaps — work items

What a client has to get right that this library does not yet exercise. Same
shape as jellyfin-mpv-shim's `docs/PERMISSION_GAPS.md`, and for the same
reason: a gap is only actionable if the write-up says what is missing, what
client path it leaves untested, and how you would know it was fixed.

Append **— FIXED** to a heading and a "What shipped" paragraph when one
lands. Keep the section: the record of what was wrong is worth more than a
tidy file.

The principle: **a fixture that omits a case does not leave it untested, it
makes the failure unreachable while the suite reports a pass.** A client
whose book support is exercised only against three EPUBs and one CBZ will
look correct right up until someone points it at a real library.

---

# Books

Raised 2026-08-06 while researching Jellyfin's book support for the shim,
which is about to implement audiobooks and download-and-open. `build_books`
(`stdjflib/libraries.py:1534`) currently emits **three EPUBs** under
`<author>/` folders and **one four-page CBZ** under `Comics/` (both since
superseded; comics now hold fifteen pages, and `Long Form/` holds a
twenty-four chapter EPUB and a 240-page PDF). That is the
whole Books library.

Two things already in place that these items build on, so none of this is
new machinery:

* `config.py:47` maps `Books` → collection type **`books`**, which is what
  makes Jellyfin's `BookResolver` and the audiobook branch of
  `AudioResolver` fire at all. Nothing below works without it, and it is
  already correct.
* **Chapters are solved.** `Recipe.chapters` (`recipes.py:73`),
  `generate.py:_chapter_metadata` writing FFMETADATA, and `verify.py`
  re-probing chapter counts all exist and are used by the `x-chapters`
  recipe. The audiobook item below needs them pointed at an audio-only
  recipe, not built.

## 1. There are no audiobooks anywhere in the Books library — FIXED

The largest gap by far, and the one most likely to be built against first.

Jellyfin models an audiobook as `AudioBook : Audio` — an ordinary audio item
with real `MediaSources` and real duration — and it only resolves that way
**inside a `books` collection** (`AudioResolver.cs:64`). The Music library
cannot stand in for it: the same file there resolves as `Audio` and takes a
different path through every client.

**Two shapes are needed, because the server produces two and clients must
handle both:**

| Shape | What the server returns |
| --- | --- |
| A single `.m4b` with embedded chapters | **one** item, with real Chapter rows |
| A multi-file rip (`Chapter 01.mp3`, …) | **N** separate items joined only by `SeriesName`/`SeriesId` |

For the second, `AudioBookListResolver` stacks the files at scan time for
*naming*, but the API still hands back N items. So "chapter 7" means a
chapter marker in one case and item 7 in the other — two code paths for one
user gesture, and only the first reuses a client's existing chapter UI. A
fixture with only one shape leaves the other silently untested.

Chapter extraction is enabled specifically for this type
(`AudioFileProber.cs:107`, `ExtractChapters = item is AudioBook`), so the
single-file case is also the only way to exercise that server path at all.

**Shape of the work.** An audio-only recipe (`video=None`) with `chapters`
set, in an MP4-family container, plus a `build_books` section that writes
one of each shape. Formats the resolver accepts include `.m4b`, `.m4a`,
`.mp3`, `.opus`, `.flac` (`995d56d5ff` upstream). `.m4b` is the one worth
having, since it is what carries chapters in the wild; note that ffmpeg
wants an MP4 muxer and the `.m4b` extension, which is a muxer/extension
mismatch worth checking rather than assuming.

**How you would know it worked.** `verify` re-probes the chapter count.
Against a server: the single file comes back as one item with populated
`Chapters`, the multi-file rip as several sharing a `SeriesName`.

**What shipped.** Seven recipes carrying `library="Books"` — declared in
`recipes.py` so `verify` re-probes them, placed by `build_books` because an
audiobook only resolves as one inside a `books` library — plus
`Recipe.container_tags`, written into the FFMETADATA file the chapters
already go through. `Elena Farrow/The Lantern Keeper/The Lantern Keeper.m4b`
is the single chaptered file, alone in a folder so the server's directory
branch fires; `Gus Gupta/The Divided Account/Chapter 01-06.mp3` is the rip.

Measured on 12.0: the `.m4b` comes back as **one** `AudioBook` with **8
chapter rows**, 240 s, named after the folder. The rip comes back as **six**
`AudioBook` items. Both shapes are as this section predicted.

**Two things here were wrong.**

*`SeriesName` does not join the rip — `Album` does.* `AudioBook` implements
`IHasSeries`, so the field is there, but **nothing in the server ever sets
it**: the only writers are `BookResolver` and the comic and OPF readers, all
of which produce `Book`. Measured: `SeriesName` is null on all seven items.
What actually groups them is the `album` tag, and `AudioFileProber` has a
dedicated audiobook branch that reads `album_artist` as the **Author** and
`composer` as the **Narrator** (Audiobookshelf's convention). The fixtures tag
all of it, and both people arrive with the right `PersonKind`. A client
grouping a multi-file audiobook must use `Album`/`AlbumArtist`; `SeriesName`
is a field that exists and is always empty.

*The mechanism behind "N items" is a fall-through, not stacking.*
`StackResolver.ResolveAudioBooks` groups **by directory**, so the six parts
become one stack of six, and `AudioResolver` drops any stack with more than
one file outright. Zero items is what saves them:
`LibraryManager.ResolvePaths` only takes a multi-item resolver's answer
`if (result?.Items.Count > 0)`, so it falls through and resolves each file on
its own. This is fragile in a way worth writing down — a *seventh* audio file
in that folder that does not stack with the rest would make the folder yield
one item, the early return would fire, and the six parts would vanish from the
library with nothing logged. `test_books.py` asserts that folder holds only
rip parts.

*And on the muxer:* `-f m4b` does fail, exactly as this section suspected —
"Error initializing the muxer for x.m4b: Invalid argument". Both `mp4` and
`ipod` work and write the same chapters. `MUXERS` maps `m4b` to **`ipod`**,
which is what ffmpeg picks from the extension itself and which stamps the
MPEG-4 audio brand rather than `isom`.

## 2. There is no PDF — FIXED

`BookResolver` accepts `.pdf`, the server counts its pages with PDFium and
stores `pageCount * 10000` as `RunTimeTicks`, and **no cover image is
extracted at all** — there is no PDF image provider, so a PDF is the one
book type that renders with blank artwork unless a sidecar supplies it.

That "no artwork" case is worth having *because* it is ugly: a client's
placeholder path for a book-shaped item with no poster is otherwise never
drawn.

**Shape of the work.** A minimal valid PDF of a few pages. This must not add
a dependency — a small PDF is writable by hand as bytes, the same line
`vobsub.py` already takes for a hand-written encoder. Page count is the
assertion (`RunTimeTicks / 10000`).

**What shipped.** `books.pdf_bytes` writes one by hand: numbered objects, a
cross-reference table of byte offsets, a trailer. Validated with `qpdf
--check` (no syntax or stream errors), `pdfinfo` and `mutool`, and
round-tripped in `test_books.py` through a reference reader that walks the
**xref** where `books.pdf_page_count` scans for page objects — the two agree
only if the offsets and the object graph are both right, and a real reader
falls back to scanning a file whose xref is broken, so a writer with wrong
offsets looks perfect everywhere else.

**Two** of them, at 6 and 3 pages, so the number is visibly read rather than
echoed. Measured on 12.0: `RunTimeTicks` 60000 and 30000, and **no
`ImageTags` at all** — the no-artwork case is real and is the only book in
the library without a cover, because turning the extractors back on (below)
gave every comic one.

**One thing this section did not say:** the page count is **new in 12.0**.
`ProbeProvider`'s `Book` overload arrived in commit `9e996d612c`, which
`git tag --contains` puts in `v12.0-rc3` and no 10.11 tag; `PDFtoImage`
(PDFium) is a hard NuGet dependency of `MediaBrowser.Providers`. On the
official 10.11 container image a PDF resolves with **no runtime at all**, and
so does a comic archive. The fixture is right either way; the assertion is
only available on 12.0.

## 3. Nothing exercises the resolves-but-unplayable formats — FIXED

`BookResolver` accepts `.azw`, `.azw3`, `.mobi` and `.cba`, so the server
catalogs them as ordinary Books with metadata and artwork — but no client
can open any of them. jellyfin-web's three players claim only epub, pdf and
the four comic archives; everything else browses and then dead-ends.

A client has to decide what to do at that point (offer download, say the
format is unsupported, hide the play button) and today has nothing to decide
it against.

**Shape of the work.** One file of a couple of these extensions. Content can
be trivial — the point is the resolve and the absent player, not the format.
Worth a note in the library README so it does not read as a broken file.

**What shipped.** `Unopenable Formats/A Kindle Format Book (2011).azw3` and
`A Mobipocket Book (2005).mobi`, each a genuine PalmDB header — which is what
both formats actually are — with a plain-text trailer saying what it is and
why it is empty. Two files, so the folder is not itself a one-book directory.
Measured: both resolve as `Book` with the year and `SeriesName` parsed, and
`RunTimeTicks` **null**, because neither extension is in the page-count
switch.

**`.cba` is not accepted and never was.** `BookResolver._validExtensions` is
exactly `.azw .azw3 .cb7 .cbr .cbt .cbz .epub .mobi .pdf`. A `.cba` resolves
to nothing at all and would have sat in the library as a file with no item —
the worst possible fixture, since it looks like coverage and is invisible.
The list is restated in `test_books.py` and every fixture is checked against
it.

**`.cbt` earned its place instead of a fake `.cbr`.** A RAR cannot be written
from the standard library, and a zip renamed `.cbr` would be a lie about the
format. A `.cbt` is a tar, `tarfile` writes one, and SharpCompress reads it —
so `Ignored Internal Info 005.cbt` is a real archive of its claimed format
that answers the same question, and it does double duty in item 4.

## 4. The three comic metadata dialects are untested — FIXED

Jellyfin reads comic metadata three different ways and the single CBZ
exercises none of them:

| Provider | Where it reads from | Restriction |
| --- | --- | --- |
| `ExternalComicInfoProvider` | `ComicInfo.xml` **beside** the archive | any archive type |
| `InternalComicInfoProvider` | `ComicInfo.xml` **inside** the archive | **`.cbz` only** |
| `ComicBookInfoProvider` | ComicBookInfo JSON in the **zip comment** | **`.cbz` only** |

The two `.cbz`-only restrictions are the interesting part: the same metadata
in a `.cbr` is ignored, which is a real behaviour a client may have to
explain.

Also untested: **cover selection**. `ComicImageProvider` looks for an entry
literally named `cover.<ext>` and otherwise takes the first image by sorted
key. A comic whose pages sort such that page one is *not* first, or one that
names a cover explicitly, are different paths — and the current fixture's
`001.jpg`-style naming exercises only the lucky case.

**Shape of the work.** A CBZ per dialect, plus one with an explicit
`cover.jpg` and one whose sort order would pick the wrong page without it.
Only `.cbz` needs writing by hand; `zipfile` already does it, and note the
existing builder writes **stored** (`ZipFile(path, "w")` defaults to
`ZIP_STORED`), which is realistic and worth keeping.

**What shipped.** Seven archives in `Books/Comics/`, driven by a `COMICS`
table so the isolation property is testable without building anything. Each
carries **exactly one** dialect, which matters more than this section said:
`ComicProvider` asks its providers in a fixed order — ComicBookInfo, external,
internal — and takes the **first** that finds anything, so an archive carrying
two would only ever demonstrate which one is first. Every dialect sets a
`Title` naming itself, so a comic showing its filename is a comic whose
metadata was not read and you can see that without opening anything.

Measured on 12.0: "Sidecar ComicInfo Dialect", "Internal ComicInfo Dialect"
and "ComicBookInfo Dialect" all came back with their series, index and year.
`Ignored Internal Info 005.cbt` came back named after its **filename** with
`SeriesName` "Comics" — the `.cbz`-only restriction, demonstrated with the
same bytes in an archive the server otherwise reads perfectly well (it still
extracted a cover and counted the entries).

Cover selection: the served `Primary` image was downloaded for all seven and
**byte-matched against the archive entries**. Every one matched the entry
`books.archive_cover` predicts — `cover.jpg` for `Named Cover 007.cbz`, and
`000 - Scan Credits.jpg` for `Scan Credits Cover 006.cbz`, which is the
realistic way the sort rule goes wrong (a scanlator credit page filed ahead of
page one). The two are otherwise identical archives, so the difference between
their covers is the rule and not the artwork.

**A page count is an entry count.** `ProbeProvider` counts every non-directory
entry, so an internal `ComicInfo.xml` makes a fifteen-page comic report
sixteen.
Measured (40000 vs 50000 ticks). Stated rather than corrected: a client
showing five is reading the server correctly.

**And the reason none of this was visible at first:** `provision.py` set
`"ImageFetchers": []` for every type. `ProviderManager.CanRefreshImages`
returns early for an `ILocalImageProvider` and *nothing else*, so an
`IDynamicImageProvider` — one that derives a picture from the file itself,
with no network anywhere — was gated by the same array that keeps TMDB out.
Every book in the library came back with no artwork, and the whole of this
item was unreachable. `LOCAL_IMAGE_EXTRACTORS` now names the two local book
extractors for `Book` only; `test_server.py` forbids a remote provider ever
appearing in that list.

## 5. No book uses the filename conventions the resolver parses — FIXED

`Emby.Naming/Book/BookFileNameParser` pulls **name, index number, parent
index, year and series name** out of the filename, and `SeriesName` falls
back to the parent directory. `Book` implements `IHasSeries`, so a client can
group books into series — but there is no real `Series` entity behind it,
only denormalized `SeriesName`/`SeriesId` fields, which is exactly the kind
of half-relationship that gets a client's grouping wrong.

The current three EPUBs are `<Author>/<Title>.epub`, which parses to a name
and nothing else. Untested: numbered volumes, a year in the filename, a
series folder, and the **single-file-in-a-directory** case (`BookResolver`
treats a directory holding exactly one supported file as one book, and
*only* if it holds exactly one).

**Shape of the work.** A series folder of numbered volumes, at least one
with a year, and one book alone in its own directory. Filenames are the
whole test, so keep them ordinary rather than clever.

**What shipped.** `Ines Imani/` holds six books — and the *number* is the
point, which this section missed. `BookResolver` treats a folder holding
**exactly one** supported file as one book named after the **folder**, and
only stops doing so at the second. So before this change the entire library
was one-book folders and `BookFileNameParser` **never ran at all**: measured
on the pre-existing library, all three EPUBs and the lone CBZ resolved as
directory-books, and `Books/Comics/A Test Comic 001.cbz` came back as an item
called "Comics".

The shelf covers regex 2 (`Ascent (The Meridian Cycle, #1) (2018)` — name,
series, index and year), regex 1 (`The Meridian Cycle #3 (2020)`), regex 3
(`01 - The Early Years (2015)`, whose `SeriesName` falls back to the parent
folder), the `v02 c015` comic convention (`ParentIndexNumber` 2,
`IndexNumber` 15, suffix not stripped) and regex 4. `Jo Jansen/The Solitary
Volume (2001)/` is the directory case, and it comes back with `SeriesName`
**empty** where the same filename loose would inherit the folder — the two
paths disagree, which is the half of this that is easiest to get wrong.

The parser is **ported into `test_books.py` and pinned against Jellyfin's own
vectors** from `BookResolverTests.cs`, so a drifted port fails there rather
than in a library nobody re-reads.

**Two corrections.**

*There is no "empty name" case.* Regex 1 has no `name` group, so
`BookResolver` sets `Name` to the empty string — and
`ResolverHelper.EnsureName` then backfills it from the filename. Measured: the
item is called "The Meridian Cycle #3 (2020)", not nothing. What a client is
actually handed is an item whose *title is a filename*, `#` and bracketed year
and all, in a correctly parsed series. Still worth having; not what it sounds
like.

*An EPUB's `dc:title` beats the filename parse, and nearly destroyed this
item.* `EpubProvider` reads the OPF and overwrites `Name`. Every shelf EPUB
originally embedded its own filename as its title, so the first live scan
reported "Ascent (The Meridian Cycle, #1) (2018)" and told you nothing about
the parser. Each EPUB now embeds *the name the parser should produce*, the row
the parser gives no name to is a PDF because no provider reads one, and
`test_books.py` holds the two to each other. The three author folders are the
deliberate version of the same behaviour and are left as they were.

---

---

# Books — what is still missing

Raised 2026-08-06 while closing the five above.

## 6. Books ship no NFO, and cannot

There is no `BookNfoParser` and no `BookNfoSaver` in
`MediaBrowser.XbmcMetadata`, and `BaseNfoProvider<T>` is subclassed for Movie,
Video, MusicVideo, Series, Season, Episode, MusicAlbum and MusicArtist and
nothing else. So the project-wide invariant that **every item ships an NFO
with `<lockdata>true`** has an exception it cannot avoid: an NFO beside a
`.epub` is read by nobody, and writing one would be a file that looks like
coverage and is inert.

Two consequences worth someone's attention rather than a fixture:

* `IsLocked` is never true for a Book, so the usual "an NFO change never
  reaches an item the server already scanned" does not hold here — a book's
  local providers re-run on an ordinary scan. What keeps the library off the
  internet is the two provider layers, both of which already list `Book` and
  `AudioBook`.
* Every field this library sets through an NFO elsewhere — tagline, critic
  rating, custom rating, countries, cast — is simply **absent** on every book.
  A client's book detail page is therefore tested against nearly empty
  metadata. The only ways in are `ComicInfo.xml`, ComicBookInfo and the OPF,
  and only the comics use them.

## 7. Two more local metadata dialects are untested

* **`OpfProvider`** reads a loose `.opf` beside *any* book —
  `<name>.opf`, then `content.opf`, then `metadata.opf`, which is Calibre's
  spelling and therefore extremely common in real libraries. Nothing here has
  one, so the whole Calibre-shaped case is untested, including
  `calibre:series`/`calibre:series_index` (the only route by which a
  non-comic book gets a `SeriesName` from metadata rather than from its path)
  and `calibre:rating`.
* **`EpubImageProvider`** finds a cover through four different OPF spellings
  in order. Every EPUB here has none, so no book in the library exercises it
  and the EPUBs are the only items left with no artwork besides the PDFs.

## 8. `.cbr` and `.cb7` resolve and nothing here is one

Both are on `BookResolver`'s list and both are read by SharpCompress for
covers and page counts. Neither can be written from the standard library, so
`.cbt` stands in for the archive-type question. That is honest for the
metadata restriction — which is about the extension — but it leaves RAR and
7z decoding themselves untested, and `.cbr` is by far the most common comic
archive in the wild. Closing it means either a checked-in fixture or a
hand-written RAR store-mode writer; the first breaks "everything is generated
or licence-checked", the second is a real piece of work for one file.

## The calibre:series conflict — SETTLED

A Book gets a `SeriesName` from its path via `BookFileNameParser` and another
from `calibre:series` via the OPF. **The OPF wins, and so does its
`series_index`.** Measured on 12.0 against
`The Contested Field (Filename Series, #4) (2011).epub`, whose filename parses
`Filename Series` / 4 and whose OPF names `Opf Series` / 9: the item comes
back `Opf Series` / 9, and stays there across a second `FullRefresh`.

**The earlier prediction here was wrong.** This document previously reasoned
from `BookMetadataService.MergeData`'s guard —
`replaceData || string.IsNullOrEmpty(target.Item.SeriesName)` — that a
filename-parsed series would block the OPF. The guard is real and
`BookResolver` really does set `SeriesName` at resolve time, but
`MetadataService.RefreshMetadata` never merges into the resolved item: it
merges into `new MetadataResult<T> { Item = CreateNew() }`, copying across
only `Path`, `Id`, `ParentIndexNumber` and the two metadata-language fields.
The target is therefore empty of both fields no matter what the filename said,
and the OPF always lands.

**What that leaves open.** `ParentIndexNumber` *is* in that copy list and
`IndexNumber` is not, so the two should behave oppositely — a filename-parsed
parent index ought to block an OPF one. Nothing here combines the two: the
`v02 c015` fixture that produces a `ParentIndexNumber` is an EPUB 3 with no
calibre metadata. Closing it is one more EPUB 2 whose filename carries a
volume/chapter suffix and whose OPF names a different `calibre:series_index`.

---

## Working on these

Read `CLAUDE.md` first — the invariants there all apply, in particular:
nothing may depend on wall-clock time or `hash()`, no new dependencies, and
`verify` must actually re-probe rather than trust that the build exited 0.
**The NFO invariant is the one exception, and only for Books** — see item 6:
the server has no Book NFO parser, so there is nothing there to write to.

Jellyfin's own resolvers are at `../jellyfin/`:
`Emby.Server.Implementations/Library/Resolvers/Books/BookResolver.cs`,
`Emby.Naming/Book/`, `Emby.Naming/AudioBook/`, and
`MediaBrowser.Providers/Books/` for every metadata provider named above.
When in doubt about what Jellyfin accepts, read it there rather than
guessing.
