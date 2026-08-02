"""Photographic artwork, for a library that has to look like a real one.

The generated artwork is built to be *read*: every image says which type it is
and what shape a client should have laid it out at. That is what you want when
you are chasing a layout bug, and exactly what you do not want in a
screenshot — a wall of stamped colour blocks looks like a test fixture,
because it is one.

`--use-artwork` swaps the drawn backgrounds for photographs, so a screenshot
of the library looks like a screenshot of somebody's library. The text still
goes on top; only the background changes, and the type stamp comes off.

**Where the pictures come from.** Lorem Picsum (picsum.photos), which serves
photographs from Unsplash. The Unsplash licence grants free use, commercial
and not, with no permission required and no attribution obliged; the one thing
it forbids is compiling the photos to replicate a competing photo service,
which a QA fixture is not. Every photographer is credited in ATTRIBUTION.md
anyway, with a link to the original.

This does **not** go through `catalog.py`'s licence gate, and the difference
is deliberate rather than an oversight. That gate answers "may this film be
redistributed", by checking a claim the catalogue makes against what the
archive says right now. There is no equivalent per-image endpoint here — the
licence is one blanket statement covering the service — so there is nothing
for the gate to check. Hence: off by default, named in the flag, stated in the
README, and credited in the file that exists for crediting.

**Reproducibility.** The ids are pinned in `picsum.py`, not fetched, and an
item is assigned a photo by position rather than by hash — so two builds of
the same library get the same picture for the same item, and no two items
within one screenful share one.
"""

from __future__ import annotations

import concurrent.futures as futures
import functools
import hashlib
import os

from . import fetch
from .picsum import PHOTOS

# Square, and big enough that no target shape has to be upscaled much: a
# poster wants 1500 of height and a backdrop 1920 of width, and everything is
# cropped from this one file rather than fetched once per shape.
SIZE = 1600
URL = "https://picsum.photos/id/{id}/{size}/{size}"

# How much of the pool a tier pulls. The whole point is that a screenful of
# thumbnails holds no repeats, and a screenful is a few dozen — so even the
# smallest of these has room to spare, and a minimal build does not spend
# 100 MB to prove it.
POOL_FOR_TIER = {"minimal": 150, "standard": 250, "full": len(PHOTOS)}


def wanted(cfg) -> int:
    """How many photographs this build will use."""
    if cfg.bulk:
        return len(PHOTOS)
    return min(len(PHOTOS), POOL_FOR_TIER.get(cfg.tier, len(PHOTOS)))


def path_for(cfg, ident: int) -> str:
    return cfg.cache("artwork", f"picsum-{ident:04d}-{SIZE}.jpg")


def download(cfg, count: int | None = None, say=print) -> int:
    """Fetch the photographs this build needs. Returns how many are ready.

    Downloads are resumable and skipped when already cached, like every other
    download here — the pool is ~100 MB at the full tier and nobody should pay
    for it twice.
    """
    count = wanted(cfg) if count is None else count
    todo = [ident for ident, _a, _s in PHOTOS[:count]
            if not (os.path.exists(path_for(cfg, ident))
                    and os.path.getsize(path_for(cfg, ident)) > 0)]
    if todo:
        say(f"  {len(todo)} photographs to fetch "
            f"(~{len(todo) * 270 // 1024} MB, cached for next time)")

    def one(ident):
        try:
            fetch.download(URL.format(id=ident, size=SIZE), path_for(cfg, ident))
            return None
        except Exception as exc:  # noqa: BLE001 - one missing photo is not fatal
            return f"picsum {ident}: {exc}"

    # Small files, latency-bound, so several at once — but not so many that
    # this reads as a scrape.
    failures = []
    if todo:
        with futures.ThreadPoolExecutor(8) as pool_:
            for i, problem in enumerate(pool_.map(one, todo), 1):
                if problem:
                    failures.append(problem)
                if i % 50 == 0:
                    say(f"    {i}/{len(todo)}")
    for problem in failures[:3]:
        say(f"  ! {problem}")
    if failures:
        say(f"  ! {len(failures)} photograph(s) could not be fetched; those "
            f"items fall back to drawn artwork")
    _pool.cache_clear()
    return len(pool(cfg))


@functools.lru_cache(maxsize=4)
def _pool(root: str, count: int) -> tuple[str, ...]:
    """The photographs actually on disk, in pinned order.

    Cached per library root: this is read once per `draw`, thousands of times
    a build, and it is a directory full of stat calls on a network mount.
    """
    paths = []
    for ident, _author, _slug in PHOTOS[:count]:
        path = os.path.join(root, "picsum-%04d-%d.jpg" % (ident, SIZE))
        if os.path.exists(path):
            paths.append(path)
    return tuple(paths)


def pool(cfg) -> tuple[str, ...]:
    return _pool(cfg.cache("artwork"), wanted(cfg))


def pick(cfg, seq: int | None, key: str) -> str | None:
    """The photograph for one item, or None if the pool is empty.

    `seq` is the item's position in its library, and position is what makes
    the guarantee: consecutive items get consecutive photographs, so a run of
    fewer items than the pool holds cannot repeat one. A hash would be tidier
    to plumb and would collide constantly — 40 items drawn from 400 by hash
    repeat about 86% of the time, which is most screenfuls.
    """
    available = pool(cfg)
    if not available:
        return None
    if seq is None:
        # A fixture with no natural index. There are a few dozen of these and
        # they are never on screen together in a grid, so a collision here
        # costs nothing.
        seq = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
    return available[seq % len(available)]


def credits(cfg) -> list[tuple[int, str, str]]:
    """(id, photographer, unsplash url) for every photograph in use."""
    return [(ident, author, f"https://unsplash.com/photos/{slug}")
            for ident, author, slug in PHOTOS[:wanted(cfg)]
            if os.path.exists(path_for(cfg, ident))]
