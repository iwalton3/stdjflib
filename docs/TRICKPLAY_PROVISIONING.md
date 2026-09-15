# Trickplay provisioning — work items

Raised 2026-09-12 from jf-offline, which downloads trickplay tiles for offline
scrubbing and needs a server that has some. Same shape as `COVERAGE_GAPS.md`:
what is missing, what client path it leaves untested, and how you would know it
was fixed.

Everything below was configured by hand against a running QA server to get a
suite passing, which is the argument for putting it in the provisioner. Jellyfin
references are to `../jellyfin/` at `v12.0`.

---

## 1. `--trickplay` is all-or-nothing, and uses the settings that make the tests unfalsifiable

`--trickplay` (`stdjflib/cli.py:125`) sets `EnableTrickplayImageExtraction` and
`ExtractTrickplayImagesDuringLibraryScan` in `library_options`
(`stdjflib/provision.py:252`), for **every** library. It never touches the
server-wide `TrickplayOptions`, so generation runs at Jellyfin's defaults:
`WidthResolutions` `[320]`, `TileWidth` and `TileHeight` `10`, `Interval`
`10000` (`MediaBrowser.Model/Configuration/TrickplayOptions.cs`).

Those defaults are the problem, not the scope:

- **One width means a client that keeps one width cannot be shown to.** A
  downloading client stores a single resolution and must stop advertising the
  others. With `[320]` generated there are no others, so the assertion passes
  against a client that advertises every width the source had.
- **A 10x10 grid means one sheet, and one sheet makes every coverage assertion
  vacuous.** A 30-second Test Media clip at a 5000 ms interval is six
  thumbnails, which is one tile image out of a possible hundred. "Every sheet
  the player can ask for is held" is then true of a client that holds nothing
  but the first.
- **Generating over the whole library is the "very slow" in the help text.**
  Test Media is 88 short clips, minutes rather than hours, and it is where a
  matrix fixture belongs anyway.

What is wanted is the option carrying the settings rather than only the switch:
which libraries, and the width list, tile grid and interval to use. What was set
by hand, and is now a documented dependency of another project's suite:
`EnableTrickplayImageExtraction` on **Test Media** only, `WidthResolutions`
`[320, 480, 640]`, `TileWidth` and `TileHeight` `2`, `Interval` `5000`.

**How you would know it was fixed:** a freshly provisioned server has at least
one item with more than one trickplay width, and at least one item whose tiles
at a given width span more than one sheet. Both are readable from
`/Items/{id}/PlaybackInfo` and `/Videos/{id}/Trickplay/{width}/{index}.jpg`
without a client.

One trap that cost time by hand: the **Generate Trickplay Images** task skips an
item that already has tiles at a width, so changing the tile grid or the
interval alone regenerates nothing. Adding a width is what makes it re-run.
A provisioner that writes the options before the first scan does not meet this,
but one that reconfigures an existing server does.

---

## 2. Tiles do not survive a re-create, and the mechanism that would is already there

Trickplay tiles are the most expensive thing a scan produces and the state
directory is disposable by design (`stdjflib/jfserver.py`), so `--fresh` throws
away every tile and the next run regenerates them.

The server already has the answer: **`LibraryOptions.SaveTrickplayWithMedia`**
(`MediaBrowser.Model/Configuration/LibraryOptions.cs:118`) writes tiles beside
the media instead of under the server's data directory — so they land in the
built library, which is exactly the thing that outlives the server. And a later
scan does not regenerate them: `TrickplayManager` finds the existing files and
imports them
(`Jellyfin.Server.Implementations/Trickplay/TrickplayManager.cs:449`).

So the request is small: set `SaveTrickplayWithMedia` alongside the extraction
flags, and let the tiles be part of the built library the way artwork is.

**But verify rather than trust it, because the import path is buggy.** On
generation, `ThumbnailCount` is the number of thumbnails
(`TrickplayManager.cs:568`, `images.Count`). On import it is set to the number
of **tile sheets** (`TrickplayManager.cs:457`, `existingFiles.Length`). With a
2x2 grid and six thumbnails that is 6 the first time and 2 after a re-create,
for the same files on disk.

That matters to any client that derives a sheet count from `ThumbnailCount`,
which is the natural thing to do and what jf-offline does. It also means a
cached library and a freshly generated one do not describe themselves the same
way, so a suite that passes on one can fail on the other with nothing having
changed.

**How you would know it was fixed:** build, provision, note `ThumbnailCount` for
an item, re-create the server against the same library, and read it again. It
should be the same number, and it should be the thumbnail count rather than the
sheet count. If `verify` grows a trickplay check, that comparison is the check
worth having — and it is worth reporting upstream regardless of what stdjflib
does about it.
