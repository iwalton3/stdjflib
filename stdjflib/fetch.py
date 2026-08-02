"""Downloading, with resume, verification and a licence gate.

Two rules this module enforces, because the library is meant to be shared:

**Nothing is downloaded without a declared licence.** Every catalog entry names
one, and archive.org entries are checked against the item's own metadata at
fetch time rather than trusted from the catalog — if an item's licence has
changed or been withdrawn since the catalog was written, the fetch stops.

**Everything downloaded is recorded.** `ATTRIBUTION.md` lists every file, where
it came from and under what terms, which is what a CC-BY attribution
requirement actually asks for.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

USER_AGENT = "stdjflib/1.0 (Jellyfin QA library builder)"
TIMEOUT = 60

# Licences considered acceptable to build a shareable library from. Anything
# else is refused rather than guessed at.
ALLOWED_LICENCES = {
    "CC-BY-3.0", "CC-BY-4.0", "CC-BY-SA-3.0", "CC-BY-SA-4.0",
    "CC0-1.0", "public-domain",
}

# Substrings that mark an archive.org `licenseurl` as one of the above.
_ARCHIVE_OK = ("publicdomain", "creativecommons.org/licenses/by/",
               "creativecommons.org/licenses/by-sa/",
               "creativecommons.org/publicdomain/", "/cc0/", "mark/1.0")


class LicenceRefused(RuntimeError):
    pass


def head(url: str) -> tuple[int, int]:
    """(status, content-length). Length is 0 when the server will not say."""
    req = urllib.request.Request(url, method="HEAD")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, int(resp.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as exc:
        return exc.code, 0
    except (urllib.error.URLError, OSError, ValueError):
        return 0, 0


def download(url: str, dest: str, *, progress=None,
             attempts: int = 5) -> str:
    """Fetch `url` to `dest`, resuming a partial `.part` if one is there.

    Completeness is checked against the Content-Length *this* request reported,
    not against `expect_bytes`. The catalog's sizes are for estimating a build
    total; treating them as a checksum makes the tool fail whenever a mirror
    re-encodes something by a few hundred bytes, which is not corruption and
    should not stop a build.

    **A short read is retried, not raised.** A large download over a long build
    gets its connection dropped sooner or later, and the server ends the body
    early without an error — `read()` simply returns empty. The first real run
    of this tool lost Sintel at 7% that way. Each attempt resumes from what is
    already on disk, so a file that keeps dropping still converges instead of
    starting over.
    """
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return _download_once(url, dest, progress=progress)
        except (IOError, OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt < attempts:
                # Back off a little, then resume from whatever landed.
                time.sleep(min(30, 2 ** attempt))
    raise IOError(f"{url}: gave up after {attempts} attempts ({last_error})")


def _download_once(url: str, dest: str, *, progress=None) -> str:
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    if have:
        req.add_header("Range", f"bytes={have}-")

    mode = "ab" if have else "wb"
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if have and resp.status != 206:
                # Server ignored the range; start over rather than corrupt.
                have, mode = 0, "wb"
            total = int(resp.headers.get("Content-Length") or 0) + have
            done = have
            with open(tmp, mode) as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except urllib.error.HTTPError as exc:
        if have and exc.code == 416:
            # Already complete; the range started past the end.
            os.replace(tmp, dest)
            return dest
        raise

    got = os.path.getsize(tmp)
    if total and got != total:
        raise IOError(f"{url}: got {got} bytes, server said {total}. "
                      f"Partial file kept at {tmp} — rerun to resume.")
    os.replace(tmp, dest)
    return dest


def archive_licence(identifier: str) -> tuple[str | None, dict]:
    """Ask archive.org what an item's licence actually is, right now.

    The catalog records what it was when written; this is what it is today. A
    dark or relicensed item answers with neither, and the fetch stops.
    """
    url = f"https://archive.org/metadata/{urllib.parse.quote(identifier)}"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None, {}
    meta = data.get("metadata") or {}
    licence = meta.get("licenseurl") or meta.get("rights") or ""
    return (licence or None), data


def archive_ok(licence: str | None) -> bool:
    if not licence:
        return False
    low = licence.lower()
    return any(token in low for token in _ARCHIVE_OK)


def check_licence(entry) -> None:
    """Raise unless this catalog entry may be redistributed as declared."""
    if entry.licence not in ALLOWED_LICENCES:
        raise LicenceRefused(
            f"{entry.key}: licence {entry.licence!r} is not in the allowed set. "
            f"Add it to ALLOWED_LICENCES only if you are certain."
        )
    if entry.archive_id:
        live, _meta = archive_licence(entry.archive_id)
        if not archive_ok(live):
            raise LicenceRefused(
                f"{entry.key}: archive.org item {entry.archive_id!r} does not "
                f"currently declare a public-domain or CC licence "
                f"(got {live!r}). Refusing to download it."
            )


def unzip_one(archive: str, dest: str, member: str | None = None) -> str:
    """Extract a single member from a zip.

    Most of the Blender downloads are a zip containing exactly one movie, so
    the default is 'the biggest file in there'.
    """
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if member:
            pick = member
        else:
            pick = max(names, key=lambda n: zf.getinfo(n).file_size)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".part"
        with zf.open(pick) as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        os.replace(tmp, dest)
    return dest


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
