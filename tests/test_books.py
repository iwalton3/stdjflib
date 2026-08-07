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
import shutil
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
    add(libraries.EPUB2_FOLDER, f"{libraries.EPUB2_METADATA_STEM}.epub")
    add(libraries.EPUB2_FOLDER, f"{libraries.EPUB2_CREDITS_STEM}.epub")
    add(libraries.EPUB2_FOLDER, f"{libraries.EPUB2_CONFLICT_STEM}.epub")
    add(libraries.LONG_BOOKS_FOLDER, f"{libraries.LONG_BOOK_STEM}.epub")
    add(libraries.LONG_BOOKS_FOLDER, f"{libraries.LONG_PDF_STEM}.pdf")
    for comic in libraries.COMICS:
        add(libraries.COMICS_FOLDER, comic["file"])
        if comic["dialect"] in libraries.DIALECTS_BESIDE:
            add(libraries.COMICS_FOLDER,
                os.path.splitext(comic["file"])[0] + ".xml")
    by_key = {r.key: r for r in recipes.all_recipes()}
    for author, book, keys in libraries.audiobook_folders():
        for key in keys:
            rec = by_key[key]
            add(os.path.join(author, book), f"{rec.title}.{rec.container}")
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
                       libraries.UNOPENABLE_FOLDER,
                       libraries.LONG_BOOKS_FOLDER,
                       libraries.EPUB2_FOLDER):
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
            expect = libraries.comic_pages(comic)
            expect += comic["credits_page"] + comic["named_cover"]
            expect += "ComicInfo.xml" in entries
            with self.subTest(comic["file"]):
                self.assertEqual(len(entries), expect)


class TestComicInfoDialects(unittest.TestCase):
    def test_no_volume_is_written_because_nothing_reads_one(self):
        """`ComicInfo/Volume` is a real ComicRack field and Jellyfin never
        reads it — `ComicInfoReader` maps only `Number`, onto `IndexNumber`,
        and `ComicBookInfoProvider` deserializes `Volume` into its model and
        never uses it. Writing one would be `outline` all over again: a field
        that round-trips and looks like coverage.

        It also means no provider anywhere sets `ParentIndexNumber` on a Book,
        so a book's parent index is always its filename's.
        """
        xml = books.comicinfo_xml(
            title="T", series="S", number=1, year=2000, summary="x",
            publisher="P", genres=["A"], writer="W")
        self.assertNotIn("Volume", xml)


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

# `Emby.Naming/Common/NamingOptions.cs:AudioBookPartsExpressions`, the two
# entries every rip part here is named at. Restated rather than imported from
# anywhere of ours, because they are what decides that a file is part N of
# something rather than a book of its own.
PART_EXPRESSIONS = (r"ch(?:apter)?[\s_-]?(?P<chapter>[0-9]+)",
                    r"p(?:ar)?t[\s_-]?(?P<part>[0-9]+)")


def parses_a_part_number(name: str) -> bool:
    stem = os.path.splitext(name)[0]
    return any(re.search(expr, stem, re.IGNORECASE)
               for expr in PART_EXPRESSIONS)


def audiobook_play_state(position: float, runtime: float,
                         min_resume: int = recipes.AUDIOBOOK_MIN_RESUME_MINUTES,
                         max_resume: int = recipes.AUDIOBOOK_MAX_RESUME_MINUTES
                         ) -> tuple[float, bool]:
    """`UserDataManager.UpdatePlayState`'s **AudioBook arm**, ported.

    Seconds in, `(stored position, played)` out. The arm exists because an
    audiobook is measured in *minutes off each end* rather than in the
    percentages the video arm above it uses — under `MinAudiobookResume`
    minutes in the position is thrown away as just-started, and under
    `MaxAudiobookResume` minutes from the end it is thrown away *and* the item
    is marked played. Nothing there consults the runtime otherwise, which is
    the whole reason a short audiobook can hold no position at all.

    A second statement of the server rather than of `recipes.py`'s comment
    about it: a check that inherits its expectations from the thing it checks
    is not a check. Only the known-runtime case, which is the one every
    fixture here is in.
    """
    assert runtime > 0
    if position <= 0:
        return 0.0, False
    if position / 60 < min_resume:
        return 0.0, False
    if (runtime - position) / 60 < max_resume or position >= runtime:
        return 0.0, True
    return position, False


class TestAudiobooks(unittest.TestCase):
    def setUp(self):
        self.by_key = {r.key: r for r in recipes.all_recipes()}
        self.books = [r for r in recipes.all_recipes() if r.library == "Books"]
        self.folders = libraries.audiobook_folders()
        self.singles = [(author, book, keys[0])
                        for author, book, keys in self.folders
                        if len(keys) == 1]
        self.rips = [row for row in self.folders if len(row[2]) > 1]

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

    def test_both_shapes_exist_at_both_lengths(self):
        """Four folders, not two. The short pair is the "too short to resume"
        case and the long pair is the only one a resume position survives on;
        neither substitutes for the other."""
        self.assertEqual(len(self.singles), 2)
        self.assertEqual(len(self.rips), 2)

    def test_the_single_file_shape_carries_chapter_markers(self):
        """The only way to reach the server's chapter extraction, which is
        enabled for AudioBook and nothing else — and which does no more than
        add `-show_chapters` to ffprobe, so markers the container lacks are
        markers that do not exist."""
        for _author, _book, key in self.singles:
            with self.subTest(key):
                rec = self.by_key[key]
                self.assertGreater(rec.chapters, 1)
                self.assertEqual(rec.container, "m4b")

    def test_the_multi_file_shape_carries_none(self):
        """Here a chapter is a file. A rip that also had markers would let a
        client pass by reading the wrong one."""
        for _author, _book, keys in self.rips:
            for key in keys:
                with self.subTest(key):
                    self.assertEqual(self.by_key[key].chapters, 0)

    def test_every_audiobook_is_a_different_book(self):
        """Two spellings of one title would read as a duplicate rather than as
        a shape or a length — and these are looked up by name."""
        titles = [book for _author, book, _keys in self.folders]
        authors = [author for author, _book, _keys in self.folders]
        self.assertEqual(len(set(titles)), len(titles))
        self.assertEqual(len(set(authors)), len(authors))

    def test_every_audiobook_item_name_is_unique_across_the_library(self):
        """The single-file shape is named after its folder and a rip's parts
        after their files, and nothing dedupes across folders: two rips both
        spelling their parts `Chapter 01` would be four items sharing two
        names, which is unlookupable."""
        names = [book for _author, book, keys in self.folders if len(keys) == 1]
        names += [self.by_key[key].title
                  for _author, _book, keys in self.folders if len(keys) > 1
                  for key in keys]
        self.assertEqual(len(set(names)), len(names))

    def test_the_rips_are_joined_by_album_because_nothing_else_joins_them(self):
        """`AudioBook` implements `IHasSeries` and **nothing in the server
        ever sets `SeriesName` on one** — the only writers are `BookResolver`
        and the comic and OPF readers, all of which produce `Book`. So the
        tags are the whole of the relationship."""
        for author, book, keys in self.rips:
            albums = set()
            for key in keys:
                tags = dict(self.by_key[key].container_tags)
                albums.add(tags["album"])
                self.assertEqual(tags["album_artist"], author)
            self.assertEqual(albums, {book})

    def test_every_rip_part_has_its_own_track_number_and_title(self):
        for _author, _book, keys in self.rips:
            seen = set()
            for key in keys:
                tags = dict(self.by_key[key].container_tags)
                seen.add((tags["title"], tags["track"]))
            self.assertEqual(len(seen), len(keys))

    def test_a_rip_folder_holds_nothing_that_would_not_stack(self):
        """The subtle one, and it is load-bearing.

        `StackResolver.ResolveAudioBooks` groups by directory, so every part
        becomes one stack, which `AudioResolver` then drops. Zero items is
        what saves them: `ResolvePaths` only takes a multi-item resolver's
        answer `if (result?.Items.Count > 0)`, so it falls through and
        resolves each file on its own. Add an audio file that ends up as its
        own one-file stack and the folder yields one item — at which point the
        early return fires and the parts vanish from the library with nothing
        logged.
        """
        tree = book_tree()
        for author, book, keys in self.rips:
            names = tree[os.path.join(author, book)]
            with self.subTest(book):
                self.assertEqual(sorted(names),
                                 sorted(f"{self.by_key[k].title}."
                                        f"{self.by_key[k].container}"
                                        for k in keys))
                for name in names:
                    self.assertTrue(parses_a_part_number(name), name)

    def test_the_single_audiobooks_are_alone_in_their_folders(self):
        """One takes its name from the folder, which only happens when the
        directory branch resolves — one audio file, and one only."""
        tree = book_tree()
        for author, book, key in self.singles:
            rec = self.by_key[key]
            with self.subTest(key):
                self.assertEqual(tree[os.path.join(author, book)],
                                 [f"{rec.title}.{rec.container}"])
                # The folder is what names it, so the file agreeing with the
                # folder is what stops the two disagreeing invisibly.
                self.assertEqual(rec.title, book)

    def test_the_author_and_narrator_go_in_the_tags_the_prober_reads(self):
        """`album_artist` is the Author and `composer` the Narrator —
        Audiobookshelf's convention, which the server adopted."""
        narrators = set()
        for author, _book, keys in self.folders:
            for key in keys:
                tags = dict(self.by_key[key].container_tags)
                with self.subTest(key):
                    self.assertEqual(tags["album_artist"], author)
                    self.assertTrue(tags["composer"])
                narrators.add(tags["composer"])
        # One narrator per book, so a People list says which fixture it came
        # from rather than which shape.
        self.assertEqual(len(narrators), len(self.folders))

    def test_the_m4b_muxer_is_not_the_extension(self):
        """`-f m4b` fails the same way `-f mkv` does, and says nothing about
        muxers."""
        from stdjflib import generate

        self.assertNotEqual(generate.muxer_for("m4b"), "m4b")
        self.assertIn(generate.muxer_for("m4b"), ("mp4", "ipod"))


class TestAudiobookResume(unittest.TestCase):
    """The lengths, and the server arm that makes them the fixture.

    An audiobook's resume window is measured in minutes off each end, so it is
    the *runtime* that decides whether any position at all can be stored — and
    below ten minutes the answer is none. Both lengths are shipped on purpose:
    the short pair is the case a client meets on a rip of a short story, and
    the long pair is the only one where resume, "finished by playback" and
    "ignored as just started" can be told apart.
    """

    def setUp(self):
        self.by_key = {r.key: r for r in recipes.all_recipes()}

    def _seconds(self, key):
        return self.by_key[key].duration

    def test_the_short_fixtures_can_hold_no_resume_position_at_all(self):
        """Deliberate, not an oversight. Under twice the threshold every
        position is either <5 minutes in or <5 minutes from the end, so the
        arm zeroes all of them — measured against 12.0 at 30 s, 120 s, 200 s
        and 235 s into the 240 s `.m4b`, all stored 0."""
        short = ["book-m4b"] + [f"book-rip-{n:02d}"
                                for n in range(1, recipes.RIP_PARTS + 1)]
        for key in short:
            runtime = self._seconds(key)
            with self.subTest(key):
                self.assertLess(runtime,
                                (recipes.AUDIOBOOK_MIN_RESUME_MINUTES
                                 + recipes.AUDIOBOOK_MAX_RESUME_MINUTES) * 60)
                for position in range(1, int(runtime)):
                    stored, _played = audiobook_play_state(position, runtime)
                    self.assertEqual(stored, 0.0)

    def test_the_short_single_can_not_even_be_marked_played_by_playback(self):
        """Under `MinAudiobookResume` the *first* test wins and returns before
        `Played` is ever set, so playing one to its end leaves it unplayed —
        a second case worth having, and a second reason not to lengthen it."""
        runtime = self._seconds("book-m4b")
        self.assertLess(runtime, recipes.AUDIOBOOK_MIN_RESUME_MINUTES * 60)
        for position in range(1, int(runtime) + 1):
            _stored, played = audiobook_play_state(position, runtime)
            self.assertFalse(played, position)

    def test_the_long_single_reaches_all_three_answers(self):
        runtime = self._seconds("book-m4b-long")
        self.assertEqual(runtime, recipes.LONG_AUDIOBOOK_SECONDS)

        stored, played = audiobook_play_state(
            recipes.LONG_AUDIOBOOK_RESUME_SECONDS, runtime)
        self.assertEqual(stored, recipes.LONG_AUDIOBOOK_RESUME_SECONDS)
        self.assertFalse(played)

        stored, played = audiobook_play_state(
            recipes.LONG_AUDIOBOOK_PLAYED_SECONDS, runtime)
        self.assertEqual(stored, 0.0)
        self.assertTrue(played)

        stored, played = audiobook_play_state(
            recipes.LONG_AUDIOBOOK_IGNORED_SECONDS, runtime)
        self.assertEqual(stored, 0.0)
        self.assertFalse(played)

    def test_the_long_single_has_a_resumable_chapter_boundary_on_each_side(self):
        """A chapter jump is the other way a client produces a position, so
        the markers are placed where they land on both sides of both
        thresholds rather than all inside one band."""
        runtime = self._seconds("book-m4b-long")
        chapters = self.by_key["book-m4b-long"].chapters
        starts = [i * runtime / chapters for i in range(chapters)][1:]
        answers = [audiobook_play_state(s, runtime) for s in starts]
        self.assertTrue([a for a in answers if a[0]], "no chapter resumes")
        self.assertTrue([a for a in answers if a == (0.0, False)],
                        "no chapter is discarded as just-started")
        self.assertTrue([a for a in answers if a[1]],
                        "no chapter marks it played")

    def test_a_long_rip_part_can_be_finished_by_playback(self):
        """Which is what makes folder-level resume — "the first part not yet
        finished" — reachable at all. The short rip's 20 s parts have no such
        position, so a client that resumes a rip by finding the first unplayed
        part had nothing to be tested against."""
        for n in range(1, recipes.LONG_RIP_PARTS + 1):
            runtime = self._seconds(f"book-rip-long-{n:02d}")
            with self.subTest(n):
                self.assertEqual(runtime, recipes.LONG_RIP_SECONDS)
                stored, played = audiobook_play_state(
                    recipes.LONG_RIP_PLAYED_SECONDS, runtime)
                self.assertEqual(stored, 0.0)
                self.assertTrue(played)
                stored, played = audiobook_play_state(
                    recipes.LONG_RIP_RESUME_SECONDS, runtime)
                self.assertEqual(stored, recipes.LONG_RIP_RESUME_SECONDS)
                self.assertFalse(played)

    def test_the_long_ones_stay_cheap(self):
        """Mono at 32k, because the length is the fixture and the content is a
        sine tone. Stereo at the short ones' 64k would be four times the bytes
        for nothing that is being tested."""
        for key in ["book-m4b-long"] + [f"book-rip-long-{n:02d}"
                                        for n in range(1,
                                                       recipes.LONG_RIP_PARTS
                                                       + 1)]:
            audio = self.by_key[key].audios[0]
            with self.subTest(key):
                self.assertEqual(audio.channels, 1)
                self.assertEqual(audio.bitrate, "32k")


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


# --------------------------------------------------------------------------
# EPUB structure — the reader fixtures
# --------------------------------------------------------------------------


class TestEpubValidity(unittest.TestCase):
    """EPUB 3 requires a navigation document and a `dcterms:modified`.

    Jellyfin requires neither — `EpubProvider` reads `dc:title` out of the OPF
    and stops — so every EPUB this tool wrote before these existed resolved,
    displayed and verified perfectly while being an invalid EPUB with nothing
    for a reader to page through. Nothing but a check like this notices.

    Read back with `books.epub_structure`, which parses the OPF rather than
    string-matching it, so a well-formedness regression fails here.
    """

    def _written(self, **kw):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "t.epub")
        books.write_epub(path, kw.pop("title", "T"), kw.pop("author", "A"), **kw)
        return path, books.epub_structure(path)

    def test_the_default_single_chapter_epub_is_valid(self):
        _path, got = self._written()
        self.assertTrue(got["nav"], "EPUB 3 requires a nav document")
        self.assertTrue(got["modified"], "EPUB 3 requires dcterms:modified")
        self.assertEqual(got["spine"], 1)

    def test_the_spine_is_the_chapter_count_exactly(self):
        """The nav document is deliberately not in the spine, so the spine
        length is the chapter count and nothing else."""
        chapters = [(f"Chapter {n}", ["one", "two"]) for n in range(1, 8)]
        _path, got = self._written(chapters=chapters)
        self.assertEqual(got["spine"], 7)

    def test_every_chapter_is_reachable_from_the_table_of_contents(self):
        """A TOC entry pointing at a file that is not in the archive is a
        chapter a reader cannot navigate to."""
        import zipfile
        chapters = [(f"Chapter {n}", ["body"]) for n in range(1, 5)]
        path, _got = self._written(chapters=chapters)
        with zipfile.ZipFile(path) as zf:
            nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
            names = set(zf.namelist())
        for href in re.findall(r'<a href="([^"]+)"', nav):
            with self.subTest(href):
                self.assertIn(f"OEBPS/{href}", names)
        self.assertEqual(len(re.findall(r'<a href="', nav)), 4)

    def test_every_xml_member_is_well_formed(self):
        import xml.etree.ElementTree as ET
        import zipfile
        path, _got = self._written(
            title="Cloak & Dagger <b>", author="A & B",
            chapters=[("Chapter & One", ["a < b", 'quote " here'])])
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith((".xhtml", ".opf", ".xml")):
                    with self.subTest(name):
                        ET.fromstring(zf.read(name))

    def test_the_opf_three_cover_uses_the_properties_spelling(self):
        """`ReadCoverPath` tries `properties="cover-image"` first and
        `<meta name="cover">` last. The two writers must carry one each — if
        this one drifted to the OPF 2 spelling, the first branch would lose
        its only fixture and nothing would fail."""
        import zipfile
        path, got = self._written(cover=b"pretend-jpeg")
        self.assertEqual(got["cover"], "OEBPS/cover.jpg")
        with zipfile.ZipFile(path) as zf:
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
        self.assertIn('properties="cover-image"', opf)
        self.assertNotIn('<meta name="cover"', opf)

    def test_an_epub_without_a_cover_declares_none(self):
        """Most of the shelf has no cover, and a writer that invented one
        would give every book artwork it was never asked for."""
        _path, got = self._written()
        self.assertIsNone(got["cover"])

    def test_the_modified_date_is_the_epoch_and_not_the_clock(self):
        """Two builds of one library must produce identical bytes, and a
        `dcterms:modified` taken from `datetime.now()` is the easiest way to
        break that without noticing."""
        from stdjflib import config
        _path, got = self._written()
        self.assertEqual(got["modified"], config.EPOCH)

    def test_two_builds_produce_identical_bytes(self):
        import hashlib
        chapters = libraries.long_book_chapters()
        digests = set()
        for _ in range(2):
            path, _got = self._written(chapters=chapters)
            with open(path, "rb") as fh:
                digests.add(hashlib.sha256(fh.read()).hexdigest())
        self.assertEqual(len(digests), 1)


class TestLongForm(unittest.TestCase):
    """The two fixtures that exist so a *reader* can be tested rather than a
    resolver. Everything else on these shelves is a few sentences long."""

    def test_the_long_book_embeds_the_name_its_filename_parses_to(self):
        """Same rule as the shelf: `dc:title` beats the filename, so an EPUB
        whose internal title disagreed would hide the parse."""
        self.assertEqual(
            libraries.LONG_BOOK_NAME,
            parse_book_name(libraries.LONG_BOOK_STEM)["name"])

    def test_the_long_pdf_parses_to_a_name_because_nothing_reads_a_pdf(self):
        """No provider reads a PDF's title, so the filename is the only thing
        that can name it — which is why both live in a folder holding two
        books rather than one each."""
        parsed = parse_book_name(libraries.LONG_PDF_STEM)
        self.assertEqual(parsed["name"], libraries.LONG_PDF_NAME)
        self.assertEqual(parsed["year"], 1998)

    def test_the_chapter_count_matches_the_declared_one(self):
        self.assertEqual(len(libraries.long_book_chapters()),
                         libraries.LONG_BOOK_CHAPTERS)

    def test_the_last_chapters_are_the_scripts_they_say_they_are(self):
        """Font fallback in a reader is the same failure the subtitle fixtures
        exist for. A chapter titled "Japanese" holding Latin text would look
        like a passing test."""
        from stdjflib import subs
        chapters = libraries.long_book_chapters()
        tail = chapters[-len(libraries.LONG_BOOK_SCRIPTS):]
        for (script, english, native), (title, body) in zip(
                libraries.LONG_BOOK_SCRIPTS, tail):
            with self.subTest(script):
                self.assertIn(english, title)
                self.assertIn(native, title)
                # Every sample line for that script must appear in the body.
                for line in subs.sample_lines(script, script):
                    self.assertIn(line.replace("\n", " "), " ".join(body))

    def test_the_bulk_of_the_book_is_latin_so_pagination_is_measurable(self):
        """A reader that cannot draw CJK would otherwise contaminate the one
        thing the long book is mostly for."""
        chapters = libraries.long_book_chapters()
        latin = chapters[:-len(libraries.LONG_BOOK_SCRIPTS)]
        self.assertGreater(len(latin), len(libraries.LONG_BOOK_SCRIPTS) * 3)
        for title, body in latin:
            with self.subTest(title):
                self.assertTrue(" ".join(body).isascii())

    def test_the_prose_is_derived_from_the_key_and_nothing_else(self):
        """`random` seeded from the clock and `hash()` both look deterministic
        within one run. Python salts string hashing per process, so `hash()`
        differs between runs of the same program."""
        self.assertEqual(books.paragraphs("k", 3), books.paragraphs("k", 3))
        self.assertNotEqual(books.paragraphs("k", 3), books.paragraphs("j", 3))

    def test_the_long_pdf_is_far_longer_than_the_short_one(self):
        """The pair is the fixture: same folder shape, same parse, a page
        count large enough that a client's paging costs something."""
        short = [pages for _s, ext, _k, _t, pages, _w
                 in libraries.BOOK_SHELF_FILES if ext == "pdf"]
        self.assertTrue(short)
        self.assertGreater(libraries.LONG_PDF_PAGES, max(short) * 10)


class TestComicTiers(unittest.TestCase):
    def test_only_the_long_comic_is_held_back_from_the_minimal_tier(self):
        """Minimal promises to be quick and to download nothing. Three hundred
        1200x1800 pages is sixteen megabytes and the one archive here big
        enough to be worth gating."""
        from stdjflib import config
        for comic in libraries.COMICS:
            with self.subTest(comic["file"]):
                tier = libraries.comic_tier(comic)
                self.assertEqual(
                    tier != "minimal",
                    libraries.comic_pages(comic) > libraries.COMIC_PAGES)
                self.assertIn(tier, config.TIERS)

    def test_the_long_comic_declares_the_entries_it_will_hold(self):
        long = [c for c in libraries.COMICS if c["key"] == "cbz-long"][0]
        entries = libraries.comic_entries(long)
        self.assertEqual(len(entries), libraries.LONG_COMIC_PAGES + 1)
        self.assertIn("cover.jpg", entries)
        self.assertEqual(books.archive_cover(entries), "cover.jpg")


# --------------------------------------------------------------------------
# EPUB 2 — the dialect that reaches the other two thirds of OpfReader
# --------------------------------------------------------------------------

# `OpfReader.GetRole`, ported. Restated here rather than imported from
# anywhere, on the same terms `BookFileNameParser` is ported above: the claim
# each fixture row makes is checked against the switch it is aimed at rather
# than against a comment. `default` is Author, which is why `ctb` — a real
# MARC relator with no case — lands there.
GET_ROLE = {
    "arr": "Arranger", "art": "Artist",
    "aut": "Author", "aqt": "Author", "aft": "Author", "aui": "Author",
    "edt": "Editor", "ill": "Illustrator", "lyr": "Lyricist",
    "mus": "AlbumArtist", "nrt": "Narrator", "oth": "Unknown",
    "trl": "Translator",
}


def find_authors(text: str) -> list[str]:
    r"""`OpfReader.FindAuthors`' name normalisation, ported.

    Split on `;`, flip "Lastname, Firstname", then respace initials with
    `(?<=\p{L})\.(?!\s|$)` — so `J.R.R. Nakamura` becomes
    `J. R. R. Nakamura` and the final period, which is followed by a space,
    is left alone.
    """
    out = []
    for full in (part.strip() for part in text.split(";") if part.strip()):
        parts = [p.strip() for p in full.split(",", 1) if p.strip()]
        if len(parts) == 2:
            full = f"{parts[1]} {parts[0]}"
        out.append(re.sub(r"(?<=[^\W\d_])\.(?!\s|$)", ". ", full))
    return out


class TestEpub2Dialect(unittest.TestCase):
    """`OpfReader` never checks the package version — it is a bag of XPaths,
    and most of them are OPF 2 spellings an EPUB 3 file cannot express."""

    def test_both_books_embed_the_name_their_filename_parses_to(self):
        for stem, name in ((libraries.EPUB2_METADATA_STEM,
                            libraries.EPUB2_METADATA_NAME),
                           (libraries.EPUB2_CREDITS_STEM,
                            libraries.EPUB2_CREDITS_NAME)):
            with self.subTest(stem):
                self.assertEqual(parse_book_name(stem)["name"], name)

    def test_the_conflict_fixture_disagrees_in_both_fields(self):
        """A conflict where the two sources happened to agree would look like
        an answer and be none — the sort-title lesson, one field over."""
        parsed = parse_book_name(libraries.EPUB2_CONFLICT_STEM)
        self.assertEqual(parsed["name"], libraries.EPUB2_CONFLICT_NAME)
        self.assertEqual(parsed["series"],
                         libraries.EPUB2_CONFLICT_FILENAME_SERIES)
        self.assertEqual(parsed["index"],
                         libraries.EPUB2_CONFLICT_FILENAME_INDEX)
        self.assertNotEqual(libraries.EPUB2_CONFLICT_FILENAME_SERIES,
                            libraries.EPUB2_CONFLICT_OPF_SERIES)
        self.assertNotEqual(libraries.EPUB2_CONFLICT_FILENAME_INDEX,
                            libraries.EPUB2_CONFLICT_OPF_INDEX)

    def test_the_filenames_carry_no_series_so_the_opf_is_the_only_source(self):
        """`calibre:series` has no OPF 3 spelling at all. If the filename also
        carried a series there would be no way to tell which one a client
        read."""
        # Deliberately not EPUB2_CONFLICT_STEM, which carries one on purpose.
        for stem in (libraries.EPUB2_METADATA_STEM,
                     libraries.EPUB2_CREDITS_STEM):
            with self.subTest(stem):
                self.assertFalse(parse_book_name(stem)["series"])

    def test_every_relator_code_is_one_the_server_maps(self):
        roles = [role for role, _w, _s, _k in libraries.EPUB2_CREATORS]
        roles.append(libraries.EPUB2_JOINT_CREATOR[0])
        for role in roles:
            with self.subTest(role):
                self.assertEqual(GET_ROLE.get(role, "Author"),
                                 self._expected_kind(role))

    def _expected_kind(self, role):
        for r, _w, _s, kind in libraries.EPUB2_CREATORS:
            if r == role:
                return kind
        return "Author"

    def test_the_table_covers_every_case_in_get_role(self):
        """A table that exercised Author eleven times would look full."""
        covered = {GET_ROLE.get(role, "Author")
                   for role, _w, _s, _k in libraries.EPUB2_CREATORS}
        self.assertEqual(covered, set(GET_ROLE.values()))

    def test_one_row_is_a_valid_relator_the_server_has_no_case_for(self):
        """The `default` arm. It has to be a *real* MARC relator: epubcheck
        rejects an invalid one with OPF-052, so a nonsense code would make an
        invalid EPUB rather than a fixture for the fallthrough."""
        fallthrough = [role for role, _w, _s, _k in libraries.EPUB2_CREATORS
                       if role not in GET_ROLE]
        self.assertTrue(fallthrough)
        for role in fallthrough:
            with self.subTest(role):
                self.assertEqual(self._expected_kind(role), "Author")

    def test_the_declared_display_names_are_what_the_server_would_produce(self):
        for role, written, shown, _kind in libraries.EPUB2_CREATORS:
            with self.subTest(written):
                self.assertEqual(find_authors(written), [shown])

    def test_one_creator_element_becomes_two_people(self):
        _role, written, expected = libraries.EPUB2_JOINT_CREATOR
        self.assertEqual(find_authors(written), list(expected))

    def test_the_initials_row_is_actually_respaced(self):
        """A row whose written and shown forms were identical would pass
        `test_the_declared_display_names...` while testing nothing."""
        rows = [(w, s) for _r, w, s, _k in libraries.EPUB2_CREATORS if w != s]
        self.assertTrue(any("." in w for w, _s in rows))

    def test_the_sort_title_cannot_collapse_onto_the_name_derived_one(self):
        """`BaseItem.GetSortName`, ported far enough to answer one question:
        does `calibre:title_sort` produce a different sort key from the one
        the Name would have produced anyway?

        "Older Format, The" did not — the trailing article is stripped from
        the *end* too, and then the comma goes — so the field was honoured and
        looked exactly like it being ignored. Measured on 12.0 before this
        test existed.
        """
        def sort_key(name):
            s = name.strip().lower()
            for word in ("the", "a", "an"):
                if s.startswith(word + " "):
                    s = s[len(word) + 1:]
                s = s.replace(" " + word + " ", " ")
                if s.endswith(" " + word):
                    s = s[:-(len(word) + 1)]
            for ch in (",", "&", "-", "{", "}", "'"):
                s = s.replace(ch, "")
            for ch in (".", "+", "%"):
                s = s.replace(ch, " ")
            return " ".join(s.split())

        self.assertNotEqual(sort_key(libraries.EPUB2_SORT_TITLE),
                            sort_key(libraries.EPUB2_METADATA_NAME),
                            "a sort title that normalises onto the name's own "
                            "sort key proves nothing either way")

    def test_a_subject_that_splits_into_two_genres_is_present(self):
        """The server splits `dc:subject` on `/ & , ; -`, so one element can
        become two genres. A table of single-word subjects would not show
        that happening."""
        self.assertTrue(any(any(sep in subject for sep in "/&,;")
                            for subject in libraries.EPUB2_SUBJECTS))

    def test_the_written_file_is_an_epub_two_with_an_ncx_and_no_nav(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "t.epub")
        books.write_epub2(path, "T", "A",
                          chapters=[("C1", ["x"]), ("C2", ["y"])])
        got = books.epub_structure(path)
        self.assertEqual(got["version"], "2.0")
        self.assertEqual(got["spine"], 2)
        self.assertTrue(got["ncx"], "EPUB 2's contents are an NCX")
        self.assertFalse(got["nav"], "a nav document is the EPUB 3 spelling")

    def test_the_opf_two_cover_is_found_the_way_the_server_finds_it(self):
        """`<meta name="cover">` naming a manifest id — the OPF 2 spelling,
        and the only reason any book in this library has artwork."""
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "t.epub")
        books.write_epub2(path, "T", "A", cover=b"not-really-a-jpeg")
        self.assertEqual(books.epub_structure(path)["cover"], "OEBPS/cover.jpg")
        import zipfile
        with zipfile.ZipFile(path) as zf:
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
        self.assertIn('<meta name="cover" content="cover-image"/>', opf)
        self.assertNotIn('properties="cover-image"', opf,
                         "that is the OPF 3 spelling and the other writer's")


@unittest.skipUnless(shutil.which("epubcheck"),
                     "epubcheck is not installed (apt install epubcheck)")
class TestEpubCheck(unittest.TestCase):
    """Validate what the writers produce against the official W3C validator.

    Gated on the binary being present, because the suite is standard-library
    only and must keep passing on a machine that has never heard of Java.
    epubcheck is a *development* tool here, never a dependency.

    This is what caught both of the writers' real bugs: an EPUB 3 with no nav
    document and no `dcterms:modified` (RSC-005, twice), and an `opf:role` of
    `zzz` (OPF-052) in the first draft of the creators table.
    """

    def _check(self, path):
        import subprocess
        result = subprocess.run(["epubcheck", "-e", path],
                                capture_output=True, text=True)
        errors = [line for line in
                  (result.stdout + result.stderr).splitlines()
                  if line.startswith(("ERROR", "FATAL"))]
        self.assertEqual(errors, [], f"{os.path.basename(path)} is not valid")

    def test_the_epub_three_writer_produces_a_valid_epub(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "three.epub")
        books.write_epub(path, "The Long Novel", "Long Form",
                         chapters=libraries.long_book_chapters())
        self._check(path)

    def test_the_epub_two_writer_produces_a_valid_epub(self):
        """Every OPF 2 field the fixtures use, so an invalid spelling is
        caught here rather than in a library nobody validated. No cover: a
        real JPEG needs ffmpeg, and this suite runs without it."""
        import tempfile
        creators = [(role, written)
                    for role, written, _s, _k in libraries.EPUB2_CREATORS]
        creators.append(libraries.EPUB2_JOINT_CREATOR[:2])
        path = os.path.join(tempfile.mkdtemp(), "two.epub")
        books.write_epub2(
            path, libraries.EPUB2_METADATA_NAME, "Adeyemi, Ada",
            chapters=[("Chapter 1", ["body"]), ("Chapter 2", ["body"])],
            scheme_ids=libraries.EPUB2_SCHEME_IDS,
            series=libraries.EPUB2_SERIES,
            series_index=libraries.EPUB2_SERIES_INDEX,
            sort_title=libraries.EPUB2_SORT_TITLE,
            rating=libraries.EPUB2_RATING,
            description="d", publisher="p",
            subjects=libraries.EPUB2_SUBJECTS, date=libraries.EPUB2_DATE,
            creators=tuple(creators))
        self._check(path)

    def test_the_default_one_chapter_epub_is_valid(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "d.epub")
        books.write_epub(path, "The Standard Reference", "Ada Alvarez")
        self._check(path)


if __name__ == "__main__":
    unittest.main()
