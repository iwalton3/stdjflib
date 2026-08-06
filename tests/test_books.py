"""The Books library: filenames, archive members, and the formats written by hand.

Books are the one library where the *files* are not media, so nothing here can
be checked by probing with ffmpeg. Three kinds of check take its place:

* the hand-written PDF is round-tripped through an independent reader written
  in this file, on the same terms `test_subs.py` decodes the VobSub RLE — a
  wrong byte in a format nobody validates produces a file that still opens and
  is silently wrong, and calling the writer back would find none of it;
* `BookFileNameParser` is **ported** here and pinned against Jellyfin's own
  test vectors, so the claim each fixture filename makes is checked against
  the parser it is aimed at rather than against a comment;
* the structural rules — how many supported files a folder may hold, which
  dialect an archive may carry — are asserted over the tables in
  `libraries.py`, because both are properties nothing at build time would
  notice going wrong.
"""

import os
import re
import unittest

from stdjflib import books, libraries, recipes


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

class ReferencePdf:
    """A PDF reader written against the spec, not against `books.py`.

    It walks the **cross-reference table** — byte offsets, trailer, /Root,
    /Pages, /Count — where `books.pdf_page_count` scans for page objects. The
    two agree only if the offsets and the object graph are both right, which
    is exactly the half of the format that a scanning reader recovers from
    silently. A real reader falls back to scanning a file whose xref is
    broken, so a writer with wrong offsets looks perfect everywhere except
    here.
    """

    def __init__(self, blob: bytes):
        self.blob = blob
        start = blob.rindex(b"startxref")
        self.xref_at = int(blob[start + 9:].split()[0])
        self.offsets = self._read_xref()
        self.trailer = self._read_trailer()

    def _read_xref(self) -> dict:
        body = self.blob[self.xref_at:]
        if not body.startswith(b"xref"):
            raise ValueError("startxref does not point at an xref table")
        header = body.split(b"\n", 2)
        first, count = (int(x) for x in header[1].split())
        rows = header[2]
        offsets = {}
        for i in range(count):
            # Every record is exactly twenty bytes. Reading them by width
            # rather than by splitting is what makes a short record a failure
            # here instead of a silent shift.
            record = rows[i * 20:(i + 1) * 20]
            if len(record) != 20:
                raise ValueError(f"xref record {i} is {len(record)} bytes")
            offset, _gen, kind = record.split()
            if kind == b"n":
                offsets[first + i] = int(offset)
        return offsets

    def _read_trailer(self) -> bytes:
        at = self.blob.index(b"trailer", self.xref_at)
        return self.blob[at:]

    def object_at(self, number: int) -> bytes:
        at = self.offsets[number]
        head = self.blob[at:at + 40].split(b"\n", 1)[0]
        if head.strip() != b"%d 0 obj" % number:
            raise ValueError(
                f"xref sends object {number} to {at}, which holds {head!r}")
        end = self.blob.index(b"endobj", at)
        return self.blob[at:end]

    def page_count(self) -> int:
        root = int(re.search(rb"/Root (\d+) 0 R", self.trailer).group(1))
        catalog = self.object_at(root)
        pages = int(re.search(rb"/Pages (\d+) 0 R", catalog).group(1))
        node = self.object_at(pages)
        count = int(re.search(rb"/Count (\d+)", node).group(1))
        kids = re.findall(rb"(\d+) 0 R", re.search(
            rb"/Kids \[([^\]]*)\]", node).group(1))
        if len(kids) != count:
            raise ValueError(f"/Count is {count} but there are {len(kids)} kids")
        for kid in kids:
            if b"/Type /Page" not in self.object_at(int(kid)):
                raise ValueError(f"object {int(kid)} is not a page")
        return count


class TestPdf(unittest.TestCase):
    def test_the_reference_reader_agrees_with_the_page_counter(self):
        for pages in (1, 2, 6, 17):
            with self.subTest(pages=pages):
                blob = books.pdf_bytes("A Title", "An Author", pages)
                self.assertEqual(ReferencePdf(blob).page_count(), pages)

    def test_every_xref_offset_lands_on_its_object(self):
        """The one part of the format that has to be exactly right.

        `object_at` raises if an offset points anywhere but the head of the
        object the table claims, and a reader that scans instead of trusting
        the table would never notice.
        """
        reader = ReferencePdf(books.pdf_bytes("A Title", "An Author", 4))
        for number in reader.offsets:
            reader.object_at(number)

    def test_the_pages_node_is_not_counted_as_a_page(self):
        """`/Type /Pages` starts with `/Type /Page`. Matching the short
        spelling first would count the tree node and report one page too many."""
        self.assertIn(b"/Type /Pages", books.pdf_bytes("T", "A", 3))
        blob = books.pdf_bytes("T", "A", 3)
        path = os.path.join(os.path.dirname(__file__), "_pdf_tmp.pdf")
        try:
            with open(path, "wb") as fh:
                fh.write(blob)
            self.assertEqual(books.pdf_page_count(path), 3)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_pdf_is_identifiable_and_binary_safe(self):
        blob = books.pdf_bytes("T", "A", 1)
        self.assertTrue(blob.startswith(b"%PDF-"))
        # The binary comment on line two is what stops a transfer agent
        # treating the file as text and translating its line endings.
        self.assertGreater(max(blob.split(b"\n")[1]), 127)
        self.assertTrue(blob.rstrip().endswith(b"%%EOF"))

    def test_parentheses_in_a_title_do_not_break_the_page_stream(self):
        """A PDF literal string ends at the first unescaped `)`."""
        blob = books.pdf_bytes("A (Bracketed) Title", r"O\Brien", 2)
        self.assertEqual(ReferencePdf(blob).page_count(), 2)

    def test_the_two_pdfs_have_different_page_counts(self):
        """The page count is the assertion, so two fixtures echoing one
        number would not show that it was read rather than assumed."""
        counts = [pages for _s, ext, _k, _t, pages, _w
                  in libraries.BOOK_SHELF_FILES if ext == "pdf"]
        self.assertEqual(len(counts), len(set(counts)))
        for pages in counts:
            self.assertGreater(pages, 1)


# --------------------------------------------------------------------------
# BookFileNameParser, ported
# --------------------------------------------------------------------------

# `Emby.Naming/Book/BookFileNameParser.cs`, restated. Deliberately a second
# statement and not a call into anything of ours: these regexes decide what a
# filename in `Books/` means, and the fixtures are named *at* them. A port
# that drifts is caught by the upstream vectors below, which are copied from
# `tests/Jellyfin.Naming.Tests/Book/BookResolverTests.cs`.
_NAME_MATCHES = [
    re.compile(r"^(?P<seriesName>.+?)((\s\((?P<seriesYear>[0-9]{4})\))?)"
               r"\s\#(?P<index>[0-9]+)(?:\.0)?"
               r"((\s\(of\s(?P<count>[0-9]+)\))?)((\s\((?P<year>[0-9]{4})\))?)$"),
    re.compile(r"^(?P<name>.+?)\s\((?P<seriesName>.+?),\s\#(?P<index>[0-9]+)\)"
               r"(?:\.0)?((\s\((?P<year>[0-9]{4})\))?)$"),
    re.compile(r"^(?P<index>[0-9]+)(?:\.0)?\s\-\s(?P<name>.+?)"
               r"((\s\((?P<year>[0-9]{4})\))?)$"),
    re.compile(r"(?P<name>.*)\((?P<year>[0-9]{4})\)"),
    re.compile(r"(?P<name>.*)"),
]

_COMIC = re.compile(r"^(?P<name>.+?)(\sv(?P<volume>[0-9]+))?"
                    r"(\sc(?P<chapter>[0-9]+))?$")


def parse_book_name(stem: str) -> dict:
    """What Jellyfin makes of a book filename (or a folder name)."""
    out = {"name": None, "series": None, "index": None,
           "parent_index": None, "year": None}
    for regex in _NAME_MATCHES:
        # .NET's Regex.Match searches rather than anchors, and the last two
        # patterns rely on it.
        match = regex.search(stem)
        if not match:
            continue
        groups = match.groupdict()
        if groups.get("name") is not None:
            comic = _COMIC.match(groups["name"].strip())
            if comic:
                if comic.group("volume"):
                    out["parent_index"] = int(comic.group("volume"))
                if comic.group("chapter"):
                    out["index"] = int(comic.group("chapter"))
            # The v/c suffix is *not* stripped from the name.
            out["name"] = groups["name"].strip()
        if groups.get("index") is not None:
            out["index"] = int(groups["index"])
        if groups.get("year") is not None:
            out["year"] = int(groups["year"])
        if groups.get("seriesName") is not None:
            out["series"] = groups["seriesName"].strip()
        break
    return out


def resolved_book_name(stem: str) -> str:
    """What the item is actually *called*, which is not what the parser returns.

    `ResolverHelper.EnsureName` fills a name in from the file (or folder) name
    whenever the resolver left one empty:

        if (string.IsNullOrEmpty(item.Name) && !string.IsNullOrEmpty(item.Path))
            item.Name = fileInfo.IsDirectory
                ? fileInfo.Name : Path.GetFileNameWithoutExtension(fileInfo.Name);

    So the `#index` spelling, whose regex has no `name` group at all, does not
    produce a nameless item — it produces one named after the raw filename.
    Measured on 12.0: `The Meridian Cycle #3 (2020).pdf` comes back as
    "The Meridian Cycle #3 (2020)", series "The Meridian Cycle", index 3.
    """
    return parse_book_name(stem)["name"] or stem


class TestBookFileNameParserPort(unittest.TestCase):
    """Jellyfin's own vectors, so a drifted port fails here and not in a
    library nobody re-reads."""

    # (filename stem, name, series, index, year) — BookResolverTests.cs
    VECTORS = [
        ("Sherlock Holmes (1887) #1 (of 4) (1887)", None, "Sherlock Holmes", 1, 1887),
        ("Sherlock Holmes #2", None, "Sherlock Holmes", 2, None),
        ("Sherlock Holmes (1887) #1", None, "Sherlock Holmes", 1, None),
        ("Sherlock Holmes #2 (1890)", None, "Sherlock Holmes", 2, 1890),
        ("A Study in Scarlet (Sherlock Holmes, #1) (1887)",
         "A Study in Scarlet", "Sherlock Holmes", 1, 1887),
        ("The Adventures of Sherlock Holmes (Sherlock Holmes, #5)",
         "The Adventures of Sherlock Holmes", "Sherlock Holmes", 5, None),
        ("The Sign of the Four (1890)", "The Sign of the Four", None, None, 1890),
        ("2 - The Sign of the Four (1890)", "The Sign of the Four", None, 2, 1890),
        ("4 - The Valley of Fear", "The Valley of Fear", None, 4, None),
        ("A Study in Scarlet", "A Study in Scarlet", None, None, None),
        ("00 - Dracula's Guest (1914)", "Dracula's Guest", None, 0, 1914),
        ("01 - Dracula (1897)", "Dracula", None, 1, 1897),
        ("2.0 - Twenty Thousand Leagues Under the Sea",
         "Twenty Thousand Leagues Under the Sea", None, 2, None),
        # A decimal that is not `.0` is not handled, and the whole string
        # becomes the name.
        ("2.1 - The Blockade Runners", "2.1 - The Blockade Runners", None, None, None),
    ]

    # (stem, name, chapter, volume, year)
    COMIC_VECTORS = [
        ("Captain Marvel Adventures v01 (1941)",
         "Captain Marvel Adventures v01", None, 1, 1941),
        ("Captain Marvel Adventures c120",
         "Captain Marvel Adventures c120", 120, None, None),
        ("Captain Marvel Adventures v01 c120",
         "Captain Marvel Adventures v01 c120", 120, 1, None),
    ]

    def test_upstream_vectors(self):
        for stem, name, series, index, year in self.VECTORS:
            with self.subTest(stem):
                got = parse_book_name(stem)
                self.assertEqual(got["name"], name)
                self.assertEqual(got["series"], series)
                self.assertEqual(got["index"], index)
                self.assertEqual(got["year"], year)

    def test_upstream_comic_vectors(self):
        for stem, name, chapter, volume, year in self.COMIC_VECTORS:
            with self.subTest(stem):
                got = parse_book_name(stem)
                self.assertEqual(got["name"], name)
                self.assertEqual(got["index"], chapter)
                self.assertEqual(got["parent_index"], volume)
                self.assertEqual(got["year"], year)


class TestShelfFilenames(unittest.TestCase):
    """What each file on the shelf is *for*.

    The filenames are the entire fixture, so each one is asserted to parse to
    the fields its row claims. Rename one to look tidier and the case it
    covers is gone with no other symptom.
    """

    def setUp(self):
        self.rows = {row[2]: row for row in libraries.BOOK_SHELF_FILES}
        self.by_key = {row[2]: parse_book_name(row[0])
                       for row in libraries.BOOK_SHELF_FILES}

    def test_the_goodreads_spelling_yields_all_four_fields(self):
        for key, index, year in (("book-shelf-vol1", 1, 2018),
                                 ("book-shelf-vol2", 2, 2019)):
            got = self.by_key[key]
            with self.subTest(key):
                self.assertTrue(got["name"])
                self.assertEqual(got["series"], "The Meridian Cycle")
                self.assertEqual(got["index"], index)
                self.assertEqual(got["year"], year)

    def test_the_hash_spelling_yields_no_name_at_all(self):
        """Regex 1 has no `name` group, so `BookResolver` sets Name to the
        empty string. An item with a series, an index, a year and no title is
        a real state a client has to draw."""
        stem, ext, _key, title, _pages, _why = self.rows["book-shelf-vol3"]
        got = self.by_key["book-shelf-vol3"]
        self.assertIsNone(got["name"])
        # But the item is not nameless: `EnsureName` backfills the filename.
        # So what a client is handed is an item whose *title is a filename*,
        # complete with the `#` and the bracketed year — which is the case
        # worth having, and is not what "no name group" sounds like.
        self.assertEqual(resolved_book_name(stem), stem)
        # It has to be a format no provider reads, or the backfill is invisible
        # behind a `dc:title`. Measured: as an EPUB this fixture reported its
        # own OPF title and told you nothing.
        self.assertEqual(ext, "pdf")
        self.assertIsNone(title)
        self.assertEqual(got["series"], "The Meridian Cycle")
        self.assertEqual(got["index"], 3)
        self.assertEqual(got["year"], 2020)

    def test_the_numbered_spelling_has_no_series_so_the_folder_supplies_one(self):
        got = self.by_key["book-shelf-numbered"]
        self.assertEqual(got["name"], "The Early Years")
        self.assertIsNone(got["series"])
        self.assertEqual(got["index"], 1)
        # `BookResolver` then falls back to the parent directory, which only
        # happens for a loose file — see TestFolderShapes.
        self.assertTrue(libraries.BOOK_SHELF)

    def test_the_volume_chapter_suffix_sets_both_index_numbers(self):
        got = self.by_key["book-shelf-volume-chapter"]
        self.assertEqual(got["parent_index"], 2)
        self.assertEqual(got["index"], 15)
        # Not stripped: the suffix stays in the name, which is what a client
        # will display.
        self.assertIn("v02 c015", got["name"])

    def test_the_pdf_parses_to_a_name_and_a_year(self):
        got = self.by_key["book-pdf"]
        self.assertEqual(got["name"], "The Standard Manual")
        self.assertEqual(got["year"], 1994)

    def test_every_epub_embeds_the_name_its_filename_parses_to(self):
        """The bug this is here to stop coming back.

        `EpubProvider` reads the OPF and overwrites `Name`, so an EPUB whose
        `dc:title` is its filename shows the filename and the whole parse is
        invisible. Every one of these fixtures was written that way and was
        measured doing exactly that against a live 12.0 before it was fixed —
        `Ascent (The Meridian Cycle, #1) (2018).epub` came back named
        "Ascent (The Meridian Cycle, #1) (2018)".

        The author folders are the deliberate version of the same behaviour,
        and they are elsewhere.
        """
        for stem, ext, key, title, _pages, _why in libraries.BOOK_SHELF_FILES:
            if ext != "epub":
                continue
            with self.subTest(key):
                self.assertIsNotNone(title, "an EPUB must carry a dc:title")
                self.assertEqual(title, self.by_key[key]["name"])

    def test_no_fixture_relies_on_an_item_having_no_name(self):
        """`EnsureName` means an item is never nameless, however little the
        parser found. A fixture written against "Name comes back empty" would
        be asserting something the server never does."""
        for stem, _ext, _key, _title, _pages, _why in libraries.BOOK_SHELF_FILES:
            self.assertTrue(resolved_book_name(stem))

    def test_the_lone_book_embeds_the_name_its_folder_parses_to(self):
        self.assertEqual(
            libraries.BOOK_ALONE_PARSED_NAME,
            parse_book_name(libraries.BOOK_ALONE)["name"])

    def test_the_author_folders_are_where_the_opf_deliberately_wins(self):
        """`Ada Alvarez/The Standard Reference.epub` is one supported file in
        a folder, so the resolver names it after the *folder* — and it comes
        back as "The Standard Reference" because the OPF overrides that. The
        case is worth having; it just must not be everywhere."""
        self.assertEqual(parse_book_name("Ada Alvarez")["name"], "Ada Alvarez")

    def test_the_shelf_covers_every_regex_that_yields_fields(self):
        """A shelf that exercised one spelling four times would look full."""
        shapes = {(bool(v["name"]), bool(v["series"]), v["index"] is not None,
                   v["parent_index"] is not None, v["year"] is not None)
                  for v in self.by_key.values()}
        self.assertGreaterEqual(len(shapes), 5)


# --------------------------------------------------------------------------
# Folder shapes — the directory rule
# --------------------------------------------------------------------------

# `BookResolver._validExtensions`. Restated rather than imported, because it
# decides how every folder below resolves and it is the list the doc for these
# fixtures got wrong: `.cba` is *not* on it and `.cb7`, `.cbr` and `.cbt` are.
VALID_BOOK_EXTENSIONS = (".azw", ".azw3", ".cb7", ".cbr", ".cbt", ".cbz",
                         ".epub", ".mobi", ".pdf")


def book_tree() -> dict:
    """Every folder `build_books` writes, and the files it puts there.

    Derived from the tables rather than from a build, so this runs without
    ffmpeg and fails when a table changes rather than when a library is
    rebuilt.
    """
    tree: dict[str, list[str]] = {}

    def add(folder, name):
        tree.setdefault(folder, []).append(name)

    for title, author in (("The Standard Reference", "Ada Alvarez"),
                          ("A Second Volume", "Bo Brandt"),
                          ("日本語の本", "Cai Chen")):
        add(author, f"{title}.epub")
    for stem, ext, _key, _title, _pages, _why in libraries.BOOK_SHELF_FILES:
        add(libraries.BOOK_SHELF, f"{stem}.{ext}")
    add(os.path.join(libraries.BOOK_ALONE_AUTHOR, libraries.BOOK_ALONE),
        f"{libraries.BOOK_ALONE}.epub")
    for stem, ext, _key in libraries.UNOPENABLE_FILES:
        add(libraries.UNOPENABLE_FOLDER, f"{stem}.{ext}")
    for comic in libraries.COMICS:
        add(libraries.COMICS_FOLDER, comic["file"])
        if comic["dialect"] in libraries.DIALECTS_BESIDE:
            add(libraries.COMICS_FOLDER,
                os.path.splitext(comic["file"])[0] + ".xml")
    single = {r.key: r for r in recipes.all_recipes()}["book-m4b"]
    add(os.path.join(recipes.AUDIOBOOK_AUTHOR, single.title),
        f"{single.title}.{single.container}")
    for part in range(1, recipes.RIP_PARTS + 1):
        add(os.path.join(recipes.RIP_AUTHOR, recipes.RIP_TITLE),
            f"Chapter {part:02d}.mp3")
    return tree


def supported(names: list[str]) -> list[str]:
    return [n for n in names
            if os.path.splitext(n)[1].lower() in VALID_BOOK_EXTENSIONS]


class TestFolderShapes(unittest.TestCase):
    """`BookResolver` counts the supported files in a folder and changes
    behaviour at exactly one.

    One supported file makes the *folder* a book, named after the folder, with
    SeriesName empty. Two makes every file in it a book of its own, named
    after the file, with SeriesName falling back to the folder. Nothing warns
    when a folder crosses that line — adding a second book to a
    single-book folder silently deletes the case it was covering.
    """

    def setUp(self):
        self.tree = book_tree()

    # Folders that must stay one-supported-file, i.e. resolve as one book
    # named after the directory.
    DIRECTORY_BOOKS = ("Ada Alvarez", "Bo Brandt", "Cai Chen",
                       os.path.join(libraries.BOOK_ALONE_AUTHOR,
                                    libraries.BOOK_ALONE))

    def test_the_directory_book_folders_hold_exactly_one_supported_file(self):
        for folder in self.DIRECTORY_BOOKS:
            with self.subTest(folder):
                self.assertEqual(len(supported(self.tree[folder])), 1)

    def test_the_shelf_holds_more_than_one_so_filenames_are_parsed(self):
        """The filename parser is unreachable in a one-book folder, so a shelf
        that shrank to one file would take every naming case with it."""
        for folder in (libraries.BOOK_SHELF, libraries.COMICS_FOLDER,
                       libraries.UNOPENABLE_FOLDER):
            with self.subTest(folder):
                self.assertGreater(len(supported(self.tree[folder])), 1)

    def test_the_sidecar_xml_does_not_count_towards_the_tally(self):
        """Only the nine book extensions count, which is what lets a comic
        keep its `ComicInfo.xml` beside it without changing how it resolves."""
        names = self.tree[libraries.COMICS_FOLDER]
        self.assertIn("The Signal Archive 002.xml", names)
        self.assertNotIn("The Signal Archive 002.xml", supported(names))

    def test_no_fixture_uses_an_extension_the_resolver_rejects(self):
        """`.cba` looks like it belongs and resolves to nothing at all."""
        for folder, names in self.tree.items():
            for name in names:
                ext = os.path.splitext(name)[1].lower()
                if ext in (".xml", ".m4b", ".mp3"):
                    continue
                with self.subTest(os.path.join(folder, name)):
                    self.assertIn(ext, VALID_BOOK_EXTENSIONS)

    def test_the_unopenable_formats_are_ones_no_client_can_open(self):
        opens_in_web = (".epub", ".pdf", ".cbz", ".cbr", ".cb7", ".cbt")
        for _stem, ext, _key in libraries.UNOPENABLE_FILES:
            with self.subTest(ext):
                self.assertIn("." + ext, VALID_BOOK_EXTENSIONS)
                self.assertNotIn("." + ext, opens_in_web)


# --------------------------------------------------------------------------
# Comics
# --------------------------------------------------------------------------

class TestComicDialects(unittest.TestCase):
    def test_each_archive_carries_exactly_one_dialect(self):
        """`ComicProvider` returns the first provider that finds anything, in
        a fixed order, so an archive carrying two would only ever demonstrate
        which one is first."""
        for comic in libraries.COMICS:
            entries = libraries.comic_entries(comic)
            inside = "ComicInfo.xml" in entries
            beside = comic["dialect"] in libraries.DIALECTS_BESIDE
            comment = comic["dialect"] in libraries.DIALECTS_IN_COMMENT
            with self.subTest(comic["file"]):
                self.assertLessEqual(sum((inside, beside, comment)), 1)

    def test_every_dialect_is_covered(self):
        got = {c["dialect"] for c in libraries.COMICS}
        self.assertEqual(got, {"none", "external", "internal", "bookinfo",
                               "ignored"})

    def test_the_two_cbz_only_dialects_are_in_a_cbz(self):
        for comic in libraries.COMICS:
            if comic["dialect"] in ("internal", "bookinfo"):
                with self.subTest(comic["file"]):
                    self.assertTrue(comic["file"].endswith(".cbz"))

    def test_the_ignored_dialect_is_deliberately_not_a_cbz(self):
        """The whole fixture is the extension: the same bytes in a `.cbz` are
        metadata, and here they are a page."""
        ignored = [c for c in libraries.COMICS if c["dialect"] == "ignored"]
        self.assertTrue(ignored)
        for comic in ignored:
            self.assertFalse(comic["file"].endswith(".cbz"))
            self.assertIn("ComicInfo.xml", libraries.comic_entries(comic))

    def test_a_failure_of_the_ignored_case_is_visible_without_opening_anything(self):
        """Its title must be one that could only come from the metadata."""
        for comic in libraries.COMICS:
            if comic["dialect"] == "ignored":
                self.assertNotIn(comic["title"],
                                 os.path.splitext(comic["file"])[0])

    def test_the_metadata_titles_never_repeat_the_filename(self):
        """A comic whose name on screen is its filename is a comic whose
        metadata was not read — which is only legible if the two differ."""
        for comic in libraries.COMICS:
            if comic["dialect"] == "none":
                continue
            with self.subTest(comic["file"]):
                self.assertNotEqual(comic["title"],
                                    os.path.splitext(comic["file"])[0])


class TestComicCovers(unittest.TestCase):
    """`ComicImageProvider`: an exact `cover.<ext>` at the root, else the
    alphabetically first image by full entry key."""

    def test_the_named_cover_wins_over_the_sort(self):
        named = [c for c in libraries.COMICS if c["named_cover"]]
        self.assertTrue(named)
        for comic in named:
            entries = libraries.comic_entries(comic)
            with self.subTest(comic["file"]):
                self.assertEqual(books.archive_cover(entries), "cover.jpg")

    def test_without_one_the_sort_picks_the_wrong_page(self):
        """The point of the fixture: page one is not the alphabetically first
        entry, so a client that assumes the cover is page one is wrong here.
        `A Test Comic 001.cbz` cannot show this — its pages sort right."""
        wrong = [c for c in libraries.COMICS
                 if c["credits_page"] and not c["named_cover"]]
        self.assertTrue(wrong)
        for comic in wrong:
            entries = libraries.comic_entries(comic)
            with self.subTest(comic["file"]):
                cover = books.archive_cover(entries)
                self.assertEqual(cover, libraries.SCAN_CREDITS_PAGE)
                self.assertNotEqual(cover, "001.jpg")

    def test_the_pair_differs_only_in_the_cover_entry(self):
        """Anything else different between them and the comparison stops
        being about the rule."""
        pair = [c for c in libraries.COMICS if c["credits_page"]]
        self.assertEqual(len(pair), 2)
        a, b = (libraries.comic_entries(c) for c in pair)
        self.assertEqual(set(a) ^ set(b), {"cover.jpg"})

    def test_the_exact_match_takes_no_prefix_and_no_capital(self):
        """`Key` is compared to `"cover" + ext` outright, so neither of these
        is a cover and both fall through to the sort."""
        self.assertEqual(books.archive_cover(["images/cover.jpg", "z.jpg"]),
                         "images/cover.jpg")   # by sort, not by name
        self.assertEqual(books.archive_cover(["Cover.jpg", "a.jpg"]), "Cover.jpg")
        self.assertEqual(books.archive_cover(["b.jpg", "Cover.jpg"]), "Cover.jpg")

    def test_png_is_preferred_over_jpg(self):
        """The extensions are tried in `_coverExtensions` order, which is not
        alphabetical and not the order they appear in the archive."""
        self.assertEqual(books.archive_cover(["cover.jpg", "cover.png"]),
                         "cover.png")

    def test_a_non_image_entry_is_never_a_cover(self):
        self.assertIsNone(books.archive_cover(["ComicInfo.xml", "notes.txt"]))

    def test_the_declared_entry_count_includes_the_metadata_member(self):
        """The server counts every non-directory entry as a page, so a
        `ComicInfo.xml` inside one inflates its page count. Stated rather than
        corrected — a client showing five pages for a four-page comic is
        reading the server correctly."""
        for comic in libraries.COMICS:
            entries = libraries.comic_entries(comic)
            expect = libraries.COMIC_PAGES
            expect += comic["credits_page"] + comic["named_cover"]
            expect += "ComicInfo.xml" in entries
            with self.subTest(comic["file"]):
                self.assertEqual(len(entries), expect)


class TestComicInfoDialects(unittest.TestCase):
    def test_comicinfo_uses_the_spellings_the_server_reads(self):
        xml = books.comicinfo_xml(
            title="T", series="S", number=1, year=2000, summary="x",
            publisher="P", genres=["A", "B"], writer="W", colourist="C")
        for tag in ("Title", "Series", "Number", "Summary", "Year", "Month",
                    "Day", "Genre", "Publisher", "Writer", "LanguageISO"):
            self.assertIn(f"<{tag}>", xml)
        # The server spells it the British way and has no `Colorist` case.
        self.assertIn("<Colourist>", xml)
        self.assertNotIn("<Colorist>", xml)
        # `Genre` is comma-split by the reader, so several go in one element.
        self.assertIn("<Genre>A, B</Genre>", xml)

    def test_comicbookinfo_uses_the_container_key_the_server_deserializes(self):
        import json

        blob = books.comicbookinfo_json(
            title="T", series="S", issue=1, year=2000, month=2, publisher="P",
            genre="G", comments="c", credits=[("Ada Alvarez", "Writer")],
            tags=["t"])
        payload = json.loads(blob)
        self.assertIn("ComicBookInfo/1.0", payload)
        body = payload["ComicBookInfo/1.0"]
        for field in ("series", "title", "issue", "publisher",
                      "publicationMonth", "publicationYear", "credits", "tags"):
            self.assertIn(field, body)
        self.assertEqual(body["credits"][0],
                         {"person": "Ada Alvarez", "role": "Writer"})

    def test_nothing_written_here_depends_on_the_clock(self):
        """Two builds of the same library must be identical, and a zip comment
        holding today's date is the easiest way to lose that."""
        import json

        blob = books.comicbookinfo_json(
            title="T", series="S", issue=1, year=2000, month=2, publisher="P",
            genre="G", comments="c", credits=[], tags=[])
        self.assertEqual(json.loads(blob)["lastModified"],
                         "2020-01-01 00:00:00 +0000")
        self.assertEqual(books.palmdb_bytes("T", "A"),
                         books.palmdb_bytes("T", "A"))


# --------------------------------------------------------------------------
# Audiobooks
# --------------------------------------------------------------------------

class TestAudiobooks(unittest.TestCase):
    def setUp(self):
        self.by_key = {r.key: r for r in recipes.all_recipes()}
        self.books = [r for r in recipes.all_recipes() if r.library == "Books"]

    def test_they_are_recipes_so_verify_re_probes_them(self):
        """A file with no recipe is a file nothing checks: `verify` looks a
        manifest entry up by key in `all_recipes()`."""
        self.assertTrue(self.books)
        for rec in self.books:
            with self.subTest(rec.key):
                self.assertIsNone(rec.video)
                self.assertEqual(len(rec.audios), 1)

    def test_they_are_kept_out_of_the_test_media_matrix(self):
        """An audiobook only resolves as one inside a `books` library — the
        same file in Movies or Music is something else entirely."""
        for rec in self.books:
            self.assertNotEqual(rec.library, "Test Media")

    def test_the_single_file_shape_carries_chapter_markers(self):
        """The only way to reach the server's chapter extraction, which is
        enabled for AudioBook and nothing else — and which does no more than
        add `-show_chapters` to ffprobe, so markers the container lacks are
        markers that do not exist."""
        rec = self.by_key["book-m4b"]
        self.assertGreater(rec.chapters, 1)
        self.assertEqual(rec.container, "m4b")

    def test_the_multi_file_shape_carries_none(self):
        """Here a chapter is a file. A rip that also had markers would let a
        client pass by reading the wrong one."""
        for part in range(1, recipes.RIP_PARTS + 1):
            self.assertEqual(self.by_key[f"book-rip-{part:02d}"].chapters, 0)

    def test_the_two_shapes_are_different_books(self):
        """Two spellings of one title would read as a duplicate rather than as
        two shapes."""
        self.assertNotEqual(recipes.AUDIOBOOK_TITLE, recipes.RIP_TITLE)
        self.assertNotEqual(recipes.AUDIOBOOK_AUTHOR, recipes.RIP_AUTHOR)

    def test_the_rip_is_joined_by_album_because_nothing_else_joins_it(self):
        """`AudioBook` implements `IHasSeries` and **nothing in the server
        ever sets `SeriesName` on one** — the only writers are `BookResolver`
        and the comic and OPF readers, all of which produce `Book`. So the
        tags are the whole of the relationship."""
        albums = set()
        for part in range(1, recipes.RIP_PARTS + 1):
            tags = dict(self.by_key[f"book-rip-{part:02d}"].container_tags)
            albums.add(tags["album"])
            self.assertEqual(tags["album_artist"], recipes.RIP_AUTHOR)
        self.assertEqual(albums, {recipes.RIP_TITLE})

    def test_every_rip_part_has_its_own_track_number_and_title(self):
        seen = set()
        for part in range(1, recipes.RIP_PARTS + 1):
            tags = dict(self.by_key[f"book-rip-{part:02d}"].container_tags)
            seen.add((tags["title"], tags["track"]))
        self.assertEqual(len(seen), recipes.RIP_PARTS)

    def test_the_rip_folder_holds_nothing_that_would_not_stack(self):
        """The subtle one, and it is load-bearing.

        `StackResolver` groups by directory, so all six parts become one stack
        of six, which `AudioResolver` then drops. Zero items is what saves
        them: `ResolvePaths` only takes a multi-item resolver's answer
        `if (result?.Items.Count > 0)`, so it falls through and resolves each
        file on its own. Add a *seventh* audio file that ends up as its own
        one-file stack and the folder yields one item — at which point the
        early return fires and the six parts vanish from the library with
        nothing logged.
        """
        tree = book_tree()
        folder = os.path.join(recipes.RIP_AUTHOR, recipes.RIP_TITLE)
        names = tree[folder]
        self.assertEqual(len(names), recipes.RIP_PARTS)
        for name in names:
            self.assertTrue(name.startswith("Chapter "), name)

    def test_the_single_audiobook_is_alone_in_its_folder(self):
        """It takes its name from the folder, which only happens when the
        directory branch resolves — one audio file, and one only."""
        tree = book_tree()
        single = self.by_key["book-m4b"]
        folder = os.path.join(recipes.AUDIOBOOK_AUTHOR, single.title)
        self.assertEqual(tree[folder], [f"{single.title}.{single.container}"])

    def test_the_author_and_narrator_go_in_the_tags_the_prober_reads(self):
        """`album_artist` is the Author and `composer` the Narrator —
        Audiobookshelf's convention, which the server adopted."""
        tags = dict(self.by_key["book-m4b"].container_tags)
        self.assertEqual(tags["album_artist"], recipes.AUDIOBOOK_AUTHOR)
        self.assertEqual(tags["composer"], recipes.AUDIOBOOK_NARRATOR)

    def test_the_m4b_muxer_is_not_the_extension(self):
        """`-f m4b` fails the same way `-f mkv` does, and says nothing about
        muxers."""
        from stdjflib import generate

        self.assertNotEqual(generate.muxer_for("m4b"), "m4b")
        self.assertIn(generate.muxer_for("m4b"), ("mp4", "ipod"))


class TestContainerTags(unittest.TestCase):
    def test_tags_and_chapters_share_one_metadata_file(self):
        """`-map_metadata` takes one input, and a second would replace the
        first rather than merge with it."""
        from stdjflib import generate

        rec = {r.key: r for r in recipes.all_recipes()}["book-m4b"]
        text = generate._chapter_metadata(rec)
        self.assertTrue(text.startswith(";FFMETADATA1\n"))
        self.assertIn("album_artist=Elena Farrow", text)
        self.assertIn("[CHAPTER]", text)
        self.assertEqual(text.count("[CHAPTER]"), rec.chapters)

    def test_the_title_is_written_once(self):
        """`title` is both the recipe's own field and a legal container tag."""
        from stdjflib import generate
        import dataclasses

        rec = dataclasses.replace(
            {r.key: r for r in recipes.all_recipes()}["book-m4b"],
            container_tags=(("title", "Overridden"),))
        text = generate._chapter_metadata(rec)
        self.assertEqual(text.count("title=Overridden"), 1)
        self.assertNotIn(f"title={rec.title}\n", text)

    def test_ffmetadata_syntax_in_a_value_is_escaped(self):
        """`= ; # \\` and a newline are syntax in an FFMETADATA file. A title
        containing one would not fail the build — it would produce a file
        tagged with something other than what the recipe asked for."""
        from stdjflib import generate
        import dataclasses

        rec = dataclasses.replace(
            {r.key: r for r in recipes.all_recipes()}["book-rip-01"],
            title="A=B; #C", container_tags=(("album", "X\\Y"),))
        text = generate._chapter_metadata(rec)
        self.assertIn(r"title=A\=B\; \#C", text)
        self.assertIn(r"album=X\\Y", text)


if __name__ == "__main__":
    unittest.main()
