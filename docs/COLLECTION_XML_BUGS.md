# `collection.xml` — two server bugs, with reproductions

Found 2026-08-06 while building the collections fixtures in this repository.
Two separate defects in how Jellyfin reads a `collection.xml` from a library
on disk. Both are silent: the collection resolves, is named correctly, draws
its artwork, and is wrong in a way nothing logs.

Written to be filed against
[jellyfin/jellyfin](https://github.com/jellyfin/jellyfin). Everything under
"Measured" was run; everything under "Where it goes wrong" is source reading
and is marked where it stops being certain.

**Measured against:** a source build of `v12.0-rc3-87-g26261dbfe7`, Linux,
SQLite, database created fresh by that build (no migration from an older
install).

**Not measured:** 10.11. The 10.11 source stores linked children as JSON on
the item rather than in a table, which is why bug 1 is expected to be
12.0-only, but nobody has run it. Do not claim a 10.11 result in the issue
without running one. Bug 2's code is byte-identical on `v10.11.0` and
`master`, so that one is expected on both.

---

## Bug 1 — a collection's members are never persisted

A `<CollectionItems>` list in a `collection.xml` is parsed and applied to the
in-memory item, and never written to the `LinkedChildren` table. The
collection is therefore correct until the server restarts and empty
afterwards, and `ChildCount` reports `0` the entire time.

### Reproduce

Two libraries. **Movies**, with two ordinary films:

```
/srv/media/Movies/First Film (2019).mkv
/srv/media/Movies/Second Film (2020).mkv
```

**Collections**, added as content type `boxsets`, holding one folder:

```
/srv/media/Collections/Test Collection [boxset]/collection.xml
```

```xml
<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<Item>
  <LocalTitle>Test Collection</LocalTitle>
  <Overview>Two films named by path.</Overview>
  <CollectionItems>
    <CollectionItem>
      <Path>/srv/media/Movies/First Film (2019).mkv</Path>
    </CollectionItem>
    <CollectionItem>
      <Path>/srv/media/Movies/Second Film (2020).mkv</Path>
    </CollectionItem>
  </CollectionItems>
</Item>
```

Absolute paths are used here to keep the report simple. Relative ones behave
identically — see "Things that make no difference" below.

Scan both libraries, then:

```sh
# 1. the members are there
curl -H "Authorization: MediaBrowser Token=$TOKEN" \
  "$SERVER/Items?userId=$USER&parentId=$COLLECTION_ID"
#    -> TotalRecordCount: 2   ✅

# 2. ...but the count on the item itself says otherwise
curl -H "Authorization: MediaBrowser Token=$TOKEN" \
  "$SERVER/Items/$COLLECTION_ID?userId=$USER" | jq .ChildCount
#    -> 0                     ❌

# 3. ...and nothing was written
sqlite3 /var/lib/jellyfin/data/jellyfin.db \
  "select count(*) from LinkedChildren where ParentId = x'...';"
#    -> 0                     ❌
```

Now **restart the server** and do not rescan:

```sh
curl ".../Items?userId=$USER&parentId=$COLLECTION_ID"
#    -> TotalRecordCount: 0   ❌ the collection is empty
```

A library scan (or a `FullRefresh` of the collections library) brings the
members back, until the next restart.

> **Querying the database:** open it normally or copy `jellyfin.db`,
> `jellyfin.db-wal` and `jellyfin.db-shm` together. Do **not** open it with
> SQLite's `immutable=1`, which ignores the WAL and returns a stale snapshot.
> That cost this investigation an hour of wrong readings.

### Expected

Either the members are persisted like every other route's, or the item is
consistent about not having them. What happens now is neither: `ChildCount`
disagrees with the item's own children within a single request cycle.

### Measured

On a freshly scanned full library with eleven collections, from four
mechanisms:

| Collection | made by | after a scan | after a restart | rows in `LinkedChildren` |
| --- | --- | --- | --- | --- |
| `The Linked Collection` | `collection.xml` | 3 | **0** | 0 |
| `Collection Without The Marker` | `collection.xml` | 2 | **0** | 0 |
| `Two Libraries, One Collection` | `collection.xml` | 2 | **0** | 0 |
| `Versions Inside A Collection` | `collection.xml` | 2 | **0** | 0 |
| `Display Order Is Ignored` | `collection.xml` | 2 | **0** | 0 |
| `One Member Is Missing` | `collection.xml` | 1 | **0** | 0 |
| `Api Made Collection` | `POST /Collections` | 3 | 3 | 3 |
| `Api Mixed Types` | `POST /Collections` | 2 | 2 | 2 |
| `Api Multi Version Member` | `POST /Collections` | 2 | 2 | 2 |
| `The Automatic Set` | `<set>` + `AutomaticallyAddToCollection` | 2 | 2 | 2 |
| `The Legacy Shelf` | a folder of films, no XML | 2 | 2 | 0¹ |

¹ Expected: that one has no linked children at all. It is a "legacy box set"
— `BoxSet.IsLegacyBoxSet`, a path outside the data directory and no linked
children — so its children come from the filesystem on every load.

`Api Made Collection` holds **the same three films** as `The Linked
Collection`. The pair differs only in how it was created.

### Things that make no difference

Each of these was tried and changed nothing:

- **Relative vs absolute member paths.** `../../Movies/x.mkv` and
  `/srv/media/Movies/x.mkv` both resolve at read time and both persist
  nothing.
- **`<LockData>`.** Present or absent, no rows. (It does matter for
  *recovery*: a locked collection is never re-read, so its members cannot come
  back after a restart. `MetadataService.RefreshMetadata` returns at
  `if (item.IsLocked)` before the local providers run.)
- **Refresh mode.** `Default`, `FullRefresh`, and `FullRefresh` with
  `replaceAllMetadata=true` all leave the table empty.
- **Library scan order.** Adding the collections library *after* the movies
  library had been fully scanned made no difference, so "the members did not
  exist yet when the file was parsed" is not the explanation.
- **Whether the item is new.** Deleting the collection items and letting a
  scan recreate them changed nothing.

### Where it goes wrong

Confirmed by debug logging: the provider does run.

```
[DBG] MediaBrowser.Providers.BoxSets.BoxSetMetadataService:
      Running "BoxSetXmlProvider" for ".../Test Collection [boxset]"
```

`BoxSetXmlParser.FetchFromCollectionItemsNode` ends with
`item.Item.LinkedChildren = list.ToArray();`, and `BoxSetMetadataService.
MergeData` carries them onto the target for any collection outside the data
directory. `Overview` and `Tags` from the same file do reach the saved item,
so the parse and the merge are both working.

The write is in
`Jellyfin.Server.Implementations/Item/ItemPersistenceService.cs`, and it is
guarded twice:

```csharp
if (item.Item is Folder { LinkedChildrenLoaded: true } folder
    && folder.LinkedChildren.Length > 0)
{
    var pathsToResolve = folder.LinkedChildren
        .Where(lc => !string.IsNullOrEmpty(lc.Path) && ...)
        .Select(lc => lc.Path)
        .ToList();

    var pathToIdMap = context.BaseItems
        .Where(e => e.Path != null && pathsToResolve.Contains(e.Path))
        ...
```

Two candidate causes, and **this report does not establish which**:

1. **`LinkedChildrenLoaded` is false** on the object that reaches the save, so
   the whole block is skipped. The flag is set by the `LinkedChildren` setter,
   and the merge does assign it — but possibly not on the instance that is
   persisted.
2. **The lookup is an exact string match** on the raw `lc.Path` against
   `BaseItems.Path`, with no `MakeAbsolutePath`. That explains relative paths
   failing and does *not* explain absolute ones failing, which they do.

Neither explains the absence of the warning that sits immediately below:

```csharp
_logger.LogWarning(
    "Skipping LinkedChild for parent {ParentName} ({ParentId}): child item
     {ChildId} (path {ChildPath}) does not exist in database", ...);
```

It never fires for these collections, which means `resolvedChildren` is empty
— execution does not reach the resolve step at all. That points at cause 1.

Whoever files this should say plainly that the root cause is not pinned down;
the reproduction and the table above are the contribution.

---

## Bug 2 — `<DisplayOrder>` on a BoxSet can never take effect

Independent of bug 1, and simpler.

### Reproduce

Add to the `collection.xml` above:

```xml
  <DisplayOrder>SortName</DisplayOrder>
```

Give the two films premiere dates whose order is the reverse of their names,
so the two orderings are distinguishable. Scan, then:

```sh
curl ".../Items/$COLLECTION_ID?userId=$USER" | jq .DisplayOrder
#    -> "PremiereDate"   ❌ the file said SortName
```

The members come back in premiere-date order.

### Where it goes wrong

`MediaBrowser.Providers/Manager/MetadataService.cs`:

```csharp
private static void MergeDisplayOrder(BaseItem source, BaseItem target, bool replaceData)
{
    if (source is IHasDisplayOrder sourceHasDisplayOrder
        && target is IHasDisplayOrder targetHasDisplayOrder)
    {
        if (replaceData || string.IsNullOrEmpty(targetHasDisplayOrder.DisplayOrder))
        {
            ...
```

and `MediaBrowser.Controller/Entities/Movies/BoxSet.cs`:

```csharp
public BoxSet()
{
    DisplayOrder = "PremiereDate";
}
```

The target of the first merge is a fresh item from `CreateNew()`, whose
constructor has already filled `DisplayOrder` in. It is therefore never empty,
and that first merge runs with `replaceData: false`:

```csharp
MergeData(localItem, temp, [], false, true);
```

so the parsed value is discarded before any later merge could carry it. Note
that `BaseXmlSaver` *does* write `DisplayOrder` back out, so the field
round-trips through the file and changes nothing — the same shape as `outline`
in the Kodi NFO dialect.

This code is identical on `v10.11.0` and on `master`, so it is not a
regression; the field has presumably never worked for a box set read from
disk. A `Series` is unaffected because nothing pre-fills its `DisplayOrder`.

### Suggested fix

Either drop the constructor default and let `Sort` fall back to
`PremiereDate` when the value is empty (`BoxSet.Sort` already does exactly
that via `Enum.TryParse`), or pass `replaceData: true` for locally-parsed
metadata. The first is smaller and changes no other behaviour.

---

## Reproducing both from this repository

```sh
./stdjflib.py build /tmp/qa --tier minimal
./stdjflib.py serve /tmp/qa --fresh
```

`Box Sets/` then holds six `collection.xml` collections, `Movies/The Legacy
Shelf [boxset]/` is the filesystem-children shape, and provisioning creates
three more through `POST /Collections`. `Display Order Is Ignored` is bug 2.

To see bug 1, list the box sets and their children, restart with
`--no-scan`, and list them again:

```sh
./stdjflib.py serve /tmp/qa --no-scan --no-build
```

The ones whose names begin `Api` keep their members; the ones from
`collection.xml` come back empty.

## Why this repository ships the broken fixtures anyway

Both shapes stay. A client has to cope with a collection whose contents are
not what they were an hour ago, and a fixture that hides the case makes the
failure unreachable while the suite reports a pass. `boxsets.py` and the
"Collections" section of `README.md` state the behaviour so that nobody
reading the library mistakes it for a fault in their own client.
