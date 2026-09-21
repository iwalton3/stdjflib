"""Missing and unaired episodes, injected into a disposable server.

A **virtual** episode is one Jellyfin lists out of a series' metadata with no
file behind it -- what every client draws as "Missing", or "Unaired" when the
air date is still ahead. A QA library built from generated files has none, so
the clients' handling of them could not be tested against a real server at all.

**There is no gentler route than writing to the database, and that is a refuted
alternative rather than an unexplored one.**

* **The API cannot create one.** `POST /Items/{itemId}`
  (`ItemUpdateController.cs`) *updates* an existing item and never assigns
  `Path` or `IsVirtualItem`; the only creating POSTs in the API are Collections
  and Playlists.
* **Nothing in the server makes one except the TMDb provider.**
  `IsVirtualItem = true` is assigned for an Episode in exactly two files, both
  of it (`TmdbMissingEpisodeProvider.cs`, `TmdbUpcomingEpisodesTask.cs`), and
  `provision.disable_remote_providers` turns off everything that talks to the
  internet -- which is the whole premise of this tool.

So: a direct insert, into a server whose entire state is disposable and
rebuilt in about ten minutes.

## Three things, not one, and each failed silently

Copying a real episode's `BaseItems` row and flipping `Path`/`IsVirtualItem` is
**not enough**. Two more are required, and the two failure modes are
indistinguishable from each other -- in both cases the row is visible to
`/Items?isMissing=true` and invisible to `/Shows/{id}/Episodes` -- so anyone
who fixes one without knowing about the other concludes their fix did not work.

1. **The `BaseItems` row**, copied from a real episode of the same season with
   `Id`, `Name`, `IndexNumber`, `PremiereDate` and `SortName` overridden,
   `Path` NULL and `IsVirtualItem` 1.
2. **Matching `AncestorIds` rows.** A real episode has five; an insert has
   none, and the season query filters on ancestry.
3. **Its own `PresentationUniqueKey`** -- the item's id, lowercased with dashes
   stripped. Copying the template's makes every clone share one key and the
   server **dedupes them away**.

`Series.GetSeasonEpisodes` keys on `AncestorWithPresentationUniqueKey` /
`SeriesPresentationUniqueKey`, which is what makes 2 and 3 load-bearing rather
than housekeeping.

## What this is coupled to

One code path serves Jellyfin 10.11 and 12.0 alike, because both are on the EF
Core `jellyfin.db` schema. What it *is* coupled to is that schema, so
:func:`check_schema` runs first and says so out loud: a future migration that
renames a column here should fail loudly rather than insert something the
server ignores.

Measured, before this module existed, by the spike this is written from: 8 → 10
episodes on 12.0.0 and 5 → 7 on 10.11.11, two Virtual in each case, the same
insert on both.
"""

import datetime
import os
import sqlite3
import uuid

#: Where a server keeps its database inside its state directory, in the two
#: layouts this tool produces: ``serve --on-host`` passes ``--datadir
#: <state>/data``, and the container mounts ``<state>/config`` at ``/config``
#: with the data directory under it.
DB_CANDIDATES = (os.path.join("data", "jellyfin.db"),
                 os.path.join("config", "data", "jellyfin.db"))

#: The columns this module writes or reads by name. Checked before anything is
#: inserted, because the failure mode of a renamed column here is a row the
#: server quietly ignores -- and this fixture exists to be *noticed*.
REQUIRED_COLUMNS = (
    "Id", "Type", "Name", "SortName", "Path", "IsVirtualItem",
    "PresentationUniqueKey", "IndexNumber", "ParentIndexNumber",
    "PremiereDate", "SeasonId", "SeriesId", "SeriesPresentationUniqueKey",
)

#: The two episodes this injects, as (name, index, is in the future).
#: A pair rather than one, because the pair is the point: a client has to tell
#: "we do not have this" from "this has not aired yet", and those differ only
#: by the air date.
EPISODES = (
    ("Missing Episode", 97, False),
    ("Unaired Episode", 98, True),
)


class SchemaMismatch(RuntimeError):
    """The database is not the one this module was written against."""


def database_path(state_dir: str) -> str:
    """The server database under a state directory.

    Probed rather than assumed, because the two run modes put it in different
    places (:data:`DB_CANDIDATES`). Falls back to the on-host layout when
    neither exists, so the error a caller gets names a path rather than None.
    """
    for relative in DB_CANDIDATES:
        candidate = os.path.join(state_dir, relative)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(state_dir, DB_CANDIDATES[0])


def connect(db_path: str) -> sqlite3.Connection:
    """Open the server's database for writing.

    A long `busy_timeout` because the server is usually **running**: the rows
    are read on demand rather than cached, which is what makes injecting into
    a live server work at all, but its own writes still hold the lock now and
    then.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def check_schema(conn: sqlite3.Connection) -> None:
    """Raise :class:`SchemaMismatch` unless every column used is present."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(BaseItems)")}
    if not have:
        raise SchemaMismatch("no BaseItems table; this is not a Jellyfin "
                             "database")
    missing = [name for name in REQUIRED_COLUMNS if name not in have]
    if missing:
        raise SchemaMismatch(
            "BaseItems has no %s -- the schema has moved under this module, "
            "and a row inserted anyway would be ignored by the server rather "
            "than reported" % ", ".join(missing))
    ancestors = {row["name"] for row in conn.execute(
        "PRAGMA table_info(AncestorIds)")}
    for name in ("ItemId", "ParentItemId"):
        if name not in ancestors:
            raise SchemaMismatch("AncestorIds has no %s" % name)


def presentation_key(item_id: str) -> str:
    """An item's own key, which is its id lowercased with dashes stripped.

    Its own, never the template's: a shared key is how the server decides two
    rows are the same item and drops one of them.
    """
    return item_id.replace("-", "").lower()


def choose_season(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """A season with real episodes to hang virtual ones off, or None.

    Deterministic -- ordered by the season's own id -- so a rebuild puts the
    fixture in the same place and a test written against it keeps working.
    Seasons with at least two episodes only, because the episode this copies
    is the template for everything the server needs and a one-episode season
    is more likely to be an oddity of the build.
    """
    rows = conn.execute(
        "SELECT SeasonId, SeriesId, COUNT(*) AS episodes FROM BaseItems"
        " WHERE Type LIKE '%.Episode' AND SeasonId IS NOT NULL"
        "   AND (IsVirtualItem IS NULL OR IsVirtualItem = 0)"
        " GROUP BY SeasonId HAVING episodes >= 2"
        " ORDER BY SeasonId").fetchall()
    return rows[0] if rows else None


def template_episode(conn: sqlite3.Connection, season_id: str):
    """A real episode of this season, to copy everything else from.

    The columns a virtual episode needs and this module does not name are
    numerous and dull (`DateCreated`, `TopParentId`, `ParentId`, the inherited
    parental ratings...). Copying a sibling is what keeps the list of things
    this module has to know about down to the three that matter.
    """
    return conn.execute(
        "SELECT * FROM BaseItems WHERE SeasonId = ? AND Type LIKE '%.Episode'"
        "  AND (IsVirtualItem IS NULL OR IsVirtualItem = 0)"
        " ORDER BY IndexNumber LIMIT 1", (season_id,)).fetchone()


def _iso(when: datetime.datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _already_there(conn, season_id: str, name: str) -> str | None:
    row = conn.execute(
        "SELECT Id FROM BaseItems WHERE SeasonId = ? AND Name = ?",
        (season_id, name)).fetchone()
    return row["Id"] if row else None


def inject_one(conn: sqlite3.Connection, template: sqlite3.Row, *, name: str,
               index_number: int, premiere: datetime.datetime) -> str:
    """Insert one virtual episode beside ``template``. Returns its id."""
    columns = list(template.keys())
    values = {key: template[key] for key in columns}
    item_id = str(uuid.uuid4())
    values["Id"] = item_id
    values["Name"] = name
    values["IndexNumber"] = index_number
    values["PremiereDate"] = _iso(premiere)
    # The shape the server writes for a real one, read off a real row:
    # `001 - 0003 - Something Happens`, season then episode then name. Without
    # it the episode sorts as if it had no number at all.
    values["SortName"] = "%03d - %04d - %s" % (
        template["ParentIndexNumber"] or 0, index_number, name)
    values["Path"] = None
    values["IsVirtualItem"] = 1
    values["PresentationUniqueKey"] = presentation_key(item_id)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        'INSERT INTO BaseItems (%s) VALUES (%s)'
        % (", ".join('"%s"' % c for c in columns), placeholders),
        [values[c] for c in columns])
    # The ancestry, copied from the template: without it the row exists and
    # the season query cannot see it.
    ancestors = conn.execute(
        "SELECT ParentItemId FROM AncestorIds WHERE ItemId = ?",
        (template["Id"],)).fetchall()
    conn.executemany(
        "INSERT OR IGNORE INTO AncestorIds (ItemId, ParentItemId)"
        " VALUES (?, ?)",
        [(item_id, row["ParentItemId"]) for row in ancestors])
    return item_id


def inject(db_path: str, *, season_id: str | None = None,
           now: datetime.datetime | None = None) -> dict:
    """Give one season a missing episode and an unaired one.

    Idempotent by name: run twice and the second run finds them and changes
    nothing, so a re-provision of an existing server does not accumulate them.

    Returns ``{"season_id", "series_id", "episodes": {name: id}, "created":
    [ids]}``; ``season_id`` is None when the library has no season with real
    episodes in it, which is what a books-only build looks like.
    """
    now = now or datetime.datetime.now()
    with connect(db_path) as conn:
        check_schema(conn)
        if season_id is None:
            chosen = choose_season(conn)
            if chosen is None:
                return {"season_id": None, "series_id": None,
                        "episodes": {}, "created": []}
            season_id = chosen["SeasonId"]
        template = template_episode(conn, season_id)
        if template is None:
            raise RuntimeError("season %s has no real episode to copy"
                               % season_id)
        episodes, created = {}, []
        for name, index_number, future in EPISODES:
            existing = _already_there(conn, season_id, name)
            if existing:
                episodes[name] = existing
                continue
            delta = datetime.timedelta(days=365 if future else -365)
            new_id = inject_one(conn, template, name=name,
                                index_number=index_number,
                                premiere=now + delta)
            episodes[name] = new_id
            created.append(new_id)
        conn.commit()
        return {"season_id": season_id, "series_id": template["SeriesId"],
                "episodes": episodes, "created": created}
