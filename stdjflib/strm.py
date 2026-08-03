"""`.strm` shortcut files — a line of text standing in for a media file.

A `.strm` holds a URL. Jellyfin resolves the item from the *path*, exactly as
it would for an `.mkv`, and then plays the URL instead of the file. That split
is the whole reason these are worth testing: the item is ordinary, its media
source is not.

What the server does with one, all read out of `../jellyfin`:

**The extension decides the item type; the target does not.**
`Emby.Naming/Common/NamingOptions.cs` lists `.strm` in `VideoFileExtensions`
*and* in `AudioFileExtensions`, so which resolver claims it comes down to the
library's collection type. `MovieResolver` (priority Fourth) takes it in a
movies, musicvideos or homevideos library, `EpisodeResolver` in tvshows, and
`AudioResolver` (Fifth) gets what is left — so the same file is a Movie in one
library and an Audio in another. Nothing looks inside first.

**`IsShortcut` comes from the extension alone**, in
`BaseVideoResolver.SetVideoType` and `AudioResolver.Resolve`, and it turns off
every provider that would otherwise open the file: `FFProbeVideoInfo` and
`AudioFileProber` both gate on `!IsShortcut || EnableRemoteContentProbe`, and
`EmbeddedImageProvider`, `VideoImageProvider`, `AudioImageProvider`,
`ChapterManager` and `TrickplayManager` return false outright. So a scan reads
no streams, no embedded art, no chapters and no trickplay from a `.strm`:
everything a client shows before playback starts has to come from the NFO and
the artwork on disk. `MediaSourceManager.GetPlaybackMediaSources` turns remote
probing back on when playback is actually requested, which is why the stream
details appear late rather than never.

**The file format is one URL, and it is more forgiving than it looks.**
`ProbeProvider.FetchShortcutInfo` reads every line, strips tabs, CR and LF from
each and trims it, then takes the first that is neither blank nor starting with
`#`. So comments and blank lines are fine, and leading whitespace is fine.
Anything after the first URL is ignored — a `.strm` is one source, not a
playlist.

**Only remote URLs are honoured, and the check is made twice.**
`FetchShortcutInfo` accepts `http`, `https`, `rtsp` and `rtp` and logs anything
else as "invalid or non-remote"; `BaseItem.GetVersionInfo` then refuses a second
time for any shortcut whose protocol resolves to `File`. Both halves are
deliberate — a local path in a `.strm` would otherwise be a way to read
arbitrary files off the server — so a `.strm` naming a local file resolves to
an item with no usable media source rather than to the file it names.
"""

from __future__ import annotations

import os

# The four schemes `ProbeProvider.FetchShortcutInfo` accepts. Anything else —
# including `file://` and a bare path — is logged and dropped.
SCHEMES = ("http", "https", "rtsp", "rtp")


def is_remote(url: str) -> bool:
    """Whether Jellyfin will accept this line as a media source."""
    scheme, sep, rest = url.partition("://")
    return bool(sep) and rest != "" and scheme.lower() in SCHEMES


def write(path: str, url: str, *, header=(), trailing=()) -> None:
    """Write one `.strm`.

    `header` goes above the URL as `#` comments, `trailing` below it as extra
    lines. Both exist so a fixture can demonstrate that the parser skips
    comments and stops at the first URL, rather than a comment claiming it
    does.

    LF endings, no BOM: `File.ReadAllLines` copes with CRLF and a BOM alike,
    so writing either would be testing .NET rather than Jellyfin, and it would
    make the file differ between platforms for no gain.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [f"# {line}".rstrip() for line in header] + [url] + list(trailing)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def first_line(path: str) -> str | None:
    """The line `FetchShortcutInfo` would settle on, normalised as it does.

    A second, independent statement of that method — `verify` uses it to check
    that what is on disk is still what the server would read, and a check that
    called `write` back would only be checking itself.
    """
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.replace("\t", "").replace("\r", "").replace("\n", "").strip()
        if line and not line.startswith("#"):
            return line
    return None


def target(path: str) -> str | None:
    """The URL Jellyfin would play, or None if it would end up with none."""
    line = first_line(path)
    return line if line and is_remote(line) else None
