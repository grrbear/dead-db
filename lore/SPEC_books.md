# dead-db phase 3 — Books fetcher (corpus #4)

## Context

Fourth corpus for the phase 3 lore RAG: a curated personal library of ~39
Grateful Dead books in EPUB and PDF format, stored on the NAS at
`/datastore/mediacenter/Audio/AudioBooks/Books - EPUB/Grateful Dead Books/`.

Books are the heaviest, most edited, most authoritative lore source in the
corpus mix — 30+ distinct authorial voices (Phil Lesh's memoir vs. McNally's
history vs. *Skeleton Key* reference vs. the *Annotated Lyrics*) covering
the same events from radically different angles. This corpus is what makes
the RAG actually shine on lore questions.

Read `lore/SPEC.md`, `lore/SPEC_wikipedia.md`, and `lore/SPEC_lia.md` before
this file. This spec assumes all three are implemented and committed.
`lore/SPEC_deadcast_DEFERRED.md` is informational only; the Deadcast corpus
was deferred (Cloudflare WAF blocks fetch) and will return as a Whisper
project later.

---

## Locked decisions carried from earlier phases (do not relitigate)

sqlite-vec; bge-small-en-v1.5 @ 384 dim CPU; separate dead_lore.db; raw_text
stored in documents; code in dead-db/lore/; section hints via
RawDocument.sections; chunks.section for per-chunk section heading; stdlib
preferred. See prior specs.

## New decisions for this phase

- **Single `source="book"` value for all 39+ books.** Distinguishing voices
  is done by per-document metadata (author, genre, year), NOT by splitting
  the source value into book_memoir / book_history / book_reference. Genre
  classification has too many fuzzy cases (oral histories, ghostwritten
  memoirs, anthologies, single-show monographs) to freeze into the schema.
  Metadata is editable; source values are not.
- **One new schema column: `documents.metadata TEXT`** carrying a JSON
  object per document. Other corpora write `NULL` to it; books write
  `{"author": "...", "year": "...", "genre": "memoir|history|reference|...",
  "isbn": "..."}`. This is the third backward-compatible scaffolding contract
  change (after `RawDocument.sections` and `chunks.section`). The existing
  smoke test must still pass unchanged.
- **EPUBs are first-class, small text-PDFs are second-class, image-heavy
  PDFs are skipped + logged.** EPUB extraction via stdlib `zipfile` +
  `xml.etree.ElementTree` + the shared `_html.py` cleaner (chapter HTML
  inside EPUBs is the same HTML cleaner-target as Wikipedia/LIA bodies).
  Text-PDFs via `pypdf` (one new dependency, pure-Python, well-scoped).
  Large image-heavy PDFs (DeadBase 50, Live Dead - Bob Minkin, Ultimate
  Grateful Dead, Cornell 77 - The Music) are skipped this phase and logged
  to `books_skipped.log` — they're candidates for a future OCR pass on the
  iGPU (Tesseract), not for now.
- **Content-hash + EPUB-metadata dedup, both layers.** Two known near-dup
  cases (`Dead to the Core` x 2 nearly-identical files; `Grateful Dead and
  Philosophy` x 2 same-stem files of different sizes). The fetcher uses
  SHA-256 over the EPUB's *content stream* (after extraction, not over the
  zip bytes — same book re-zipped should still dedup) as the first filter,
  and EPUB OPF metadata (title + author) as the second filter for cases
  where the bytes differ but the content is the same edition.
- **Chapter structure is gold and we use it.** EPUBs have an OPF spine
  listing chapter files in order. The fetcher pre-splits into
  `Section(heading=chapter_title, text=...)` per chapter; the normalizer
  then paragraph-merges within each chapter (no cross-chapter chunks).
  Chapter titles come from the OPF's `<navMap>`/`<nav epub:type="toc">` or
  from `<h1>`/`<h2>` headings in the chapter HTML if the TOC is absent.
- **Copyright posture: personal use, chunks never leave the homelab.** Same
  posture as Plex transcoding purchased music. Documented here so it's
  explicit; the spec does not constrain ingest further on that basis.

---

## Contract change 3: `documents.metadata`

Add one column to the `documents` table in `lore/schema.sql`:

```sql
    metadata TEXT,   -- JSON: per-doc metadata (e.g. book author/year/genre). NULL for corpora that don't use it.
```

Place it after `raw_text`. Update the INSERT in `build_lore_db.py` to carry
it. The existing Wikipedia/LIA fetchers continue to write `NULL` here — no
fetcher changes required for prior corpora.

Note: `init_schema` uses `CREATE TABLE IF NOT EXISTS`. An EXISTING dead_lore.db
will NOT gain the column automatically. As with the previous contract
change, do NOT write a migration — just ensure schema.sql has the column
and rebuild. The user will purge + re-ingest Wikipedia and LIA after this
phase lands.

Update the `Chunk`/orchestrator path in `build_lore_db.py` to accept an
optional `metadata: dict | None` per RawDocument, JSON-encode it on insert.
Extend `RawDocument`:

```python
@dataclass
class RawDocument:
    source: str
    source_id: str
    title: str
    url: str
    published: str | None
    raw_text: str
    sections: list[Section] | None = None
    metadata: dict | None = None      # NEW — JSON-serializable per-doc data
```

Backward compatibility: existing fetchers don't set the field; orchestrator
treats `None` as "store NULL." Smoke test must continue to pass unchanged.

---

## EPUB structure (reference for implementer)

An EPUB is a ZIP file with:
- `META-INF/container.xml` — points to the OPF file
- `<book>.opf` — the package document. Contains:
  - `<metadata>` — Dublin Core: `<dc:title>`, `<dc:creator>` (author),
    `<dc:date>`, `<dc:identifier>` (ISBN sometimes)
  - `<manifest>` — list of all files in the EPUB by id
  - `<spine>` — ordered list of `<itemref idref="...">` pointing to the
    chapter files in reading order
- Chapter files (XHTML), referenced by spine

For chapter titles, prefer in this order:
1. The EPUB's `nav.xhtml` (EPUB 3) with `<nav epub:type="toc">` — clean
   list of chapter title + href.
2. The `toc.ncx` file (EPUB 2) — older but very common, has `<navMap>`
   with `<navPoint>` entries.
3. Fallback: extract first `<h1>` or `<h2>` from each chapter file.
4. Final fallback: `"Chapter N"` indexed by spine position.

Use stdlib only: `zipfile`, `xml.etree.ElementTree`, `html.parser` (via the
existing `_html.py`). No `ebooklib`, no `beautifulsoup`.

---

## `lore/fetchers/books.py` — the fetcher

```python
"""Books fetcher (EPUB + small text-PDFs). Reads from a single library
directory, extracts chapter-structured text, captures per-book metadata.
Image-heavy PDFs are skipped + logged. stdlib + pypdf only.
"""
import hashlib
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

from ._base import Fetcher, RawDocument, Section
from ._html import html_to_text

BOOKS_DIR = Path(os.environ.get(
    "BOOKS_DIR",
    "/datastore/mediacenter/Audio/AudioBooks/Books - EPUB/Grateful Dead Books",
))
# image-heavy PDFs we know to skip this phase (size-based + name-based heuristic)
PDF_SIZE_SKIP_BYTES = 50 * 1024 * 1024   # >50MB PDFs are presumed image-heavy
SKIP_PATTERNS = (
    r"deadbase", r"live dead - bob minkin", r"ultimate grateful dead",
    r"cornell 77.*music",
)
# OPF/EPUB namespaces
NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}


# ---------- discovery ----------

def _is_macos_dot(p: Path) -> bool:
    return p.name.startswith("._") or p.name == ".DS_Store"


def _should_skip_pdf(p: Path) -> tuple[bool, str]:
    name_l = p.name.lower()
    for pat in SKIP_PATTERNS:
        if re.search(pat, name_l):
            return True, f"name-pattern: {pat}"
    if p.stat().st_size > PDF_SIZE_SKIP_BYTES:
        return True, f"size>{PDF_SIZE_SKIP_BYTES} bytes (image-heavy heuristic)"
    return False, ""


def _enumerate_files(root: Path) -> list[Path]:
    """Recurse one level deep. Skip macOS dotfiles."""
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if _is_macos_dot(p):
            continue
        if p.suffix.lower() in (".epub", ".pdf"):
            files.append(p)
    return files


# ---------- EPUB extraction ----------

def _epub_opf_path(zf: zipfile.ZipFile) -> str:
    with zf.open("META-INF/container.xml") as f:
        root = ET.parse(f).getroot()
    rootfile = root.find(".//container:rootfile", NS)
    return rootfile.get("full-path")


def _epub_metadata(opf_root: ET.Element) -> dict:
    md = opf_root.find("opf:metadata", NS)
    if md is None:
        return {}
    def first(tag: str) -> str | None:
        el = md.find(f"dc:{tag}", NS)
        return (el.text or "").strip() if el is not None and el.text else None
    isbn = None
    for el in md.findall("dc:identifier", NS):
        text = (el.text or "").strip()
        if text and ("isbn" in text.lower() or re.fullmatch(r"[\d\-Xx]{10,17}", text)):
            isbn = text.replace("urn:isbn:", "").strip()
            break
    return {
        "title": first("title"),
        "author": first("creator"),
        "year": (first("date") or "")[:4] or None,
        "isbn": isbn,
        "genre": None,   # user-curated, not in EPUB metadata; left for manual edit
    }


def _epub_spine_items(opf_root: ET.Element, opf_dir: str) -> list[tuple[str, str]]:
    """Return [(idref, href)] in spine order, hrefs resolved to zip paths."""
    manifest = {}
    for item in opf_root.findall("opf:manifest/opf:item", NS):
        manifest[item.get("id")] = item.get("href")
    spine = []
    for ref in opf_root.findall("opf:spine/opf:itemref", NS):
        idref = ref.get("idref")
        href = manifest.get(idref)
        if href:
            full = os.path.normpath(os.path.join(opf_dir, href))
            spine.append((idref, full.replace("\\", "/")))
    return spine


def _epub_toc_titles(zf: zipfile.ZipFile, opf_root: ET.Element, opf_dir: str) -> dict[str, str]:
    """Map chapter-file-path -> chapter title from nav.xhtml or toc.ncx. Best effort."""
    titles: dict[str, str] = {}

    # EPUB 3: nav.xhtml — item with properties="nav"
    for item in opf_root.findall("opf:manifest/opf:item", NS):
        if "nav" in (item.get("properties") or ""):
            nav_path = os.path.normpath(os.path.join(opf_dir, item.get("href"))).replace("\\", "/")
            try:
                with zf.open(nav_path) as f:
                    nav_root = ET.parse(f).getroot()
                for a in nav_root.iter("{http://www.w3.org/1999/xhtml}a"):
                    href = (a.get("href") or "").split("#", 1)[0]
                    if not href:
                        continue
                    chap_path = os.path.normpath(os.path.join(os.path.dirname(nav_path), href))
                    titles[chap_path.replace("\\", "/")] = "".join(a.itertext()).strip()
                if titles:
                    return titles
            except (KeyError, ET.ParseError):
                pass

    # EPUB 2: toc.ncx
    for item in opf_root.findall("opf:manifest/opf:item", NS):
        if item.get("media-type") == "application/x-dtbncx+xml":
            ncx_path = os.path.normpath(os.path.join(opf_dir, item.get("href"))).replace("\\", "/")
            try:
                with zf.open(ncx_path) as f:
                    ncx_root = ET.parse(f).getroot()
                for nav in ncx_root.iter("{%s}navPoint" % NS["ncx"]):
                    label = nav.find("ncx:navLabel/ncx:text", NS)
                    content = nav.find("ncx:content", NS)
                    if label is not None and content is not None:
                        href = (content.get("src") or "").split("#", 1)[0]
                        chap_path = os.path.normpath(
                            os.path.join(os.path.dirname(ncx_path), href)
                        ).replace("\\", "/")
                        titles[chap_path] = (label.text or "").strip()
            except (KeyError, ET.ParseError):
                pass

    return titles


def _extract_epub(path: Path) -> tuple[dict, list[Section]]:
    """Return (metadata_dict, [Section(chapter_title, text), ...])."""
    with zipfile.ZipFile(path) as zf:
        opf_path = _epub_opf_path(zf)
        opf_dir = os.path.dirname(opf_path)
        with zf.open(opf_path) as f:
            opf_root = ET.parse(f).getroot()
        meta = _epub_metadata(opf_root)
        spine = _epub_spine_items(opf_root, opf_dir)
        toc_titles = _epub_toc_titles(zf, opf_root, opf_dir)

        sections: list[Section] = []
        for idx, (idref, chap_path) in enumerate(spine):
            try:
                with zf.open(chap_path) as f:
                    html = f.read().decode("utf-8", "replace")
            except KeyError:
                continue
            text, inner_sections = html_to_text(html)
            if not text.strip():
                continue
            heading = toc_titles.get(chap_path) or ""
            if not heading and inner_sections:
                heading = inner_sections[0].heading
            if not heading:
                heading = f"Chapter {idx + 1}"
            sections.append(Section(heading=heading, text=text))
    return meta, sections


# ---------- PDF extraction (text-PDFs only) ----------

def _extract_pdf(path: Path) -> tuple[dict, list[Section]]:
    """Best-effort: each page becomes one Section. Metadata best-effort."""
    from pypdf import PdfReader   # local import: only loaded if a PDF is processed
    reader = PdfReader(str(path))
    docinfo = reader.metadata or {}
    meta = {
        "title": (docinfo.get("/Title") or path.stem).strip() if docinfo else path.stem,
        "author": (docinfo.get("/Author") or "").strip() or None if docinfo else None,
        "year": None,
        "isbn": None,
        "genre": None,
    }
    sections: list[Section] = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = text.strip()
        if text:
            sections.append(Section(heading=f"Page {i + 1}", text=text))
    return meta, sections


# ---------- dedup ----------

def _content_hash(sections: list[Section]) -> str:
    h = hashlib.sha256()
    for s in sections:
        h.update(s.text.encode("utf-8"))
    return h.hexdigest()


def _metadata_key(meta: dict) -> str | None:
    t = (meta.get("title") or "").strip().lower()
    a = (meta.get("author") or "").strip().lower()
    if t and a:
        return f"{t}||{a}"
    return None


# ---------- fetcher ----------

class BooksFetcher(Fetcher):
    name = "book"

    def discover(self) -> list[str]:
        return [str(p) for p in _enumerate_files(BOOKS_DIR)]

    def fetch(self, source_ids: list[str] | None = None) -> Iterator[RawDocument]:
        files = [Path(p) for p in (source_ids or self.discover())]
        seen_hashes: set[str] = set()
        seen_metakeys: set[str] = set()
        skipped: list[str] = []

        for p in files:
            suffix = p.suffix.lower()
            try:
                if suffix == ".epub":
                    meta, sections = _extract_epub(p)
                elif suffix == ".pdf":
                    skip, reason = _should_skip_pdf(p)
                    if skip:
                        skipped.append(f"{p.name} :: skipped ({reason})")
                        continue
                    meta, sections = _extract_pdf(p)
                else:
                    skipped.append(f"{p.name} :: skipped (unknown suffix)")
                    continue
            except Exception as e:
                skipped.append(f"{p.name} :: extract failed: {type(e).__name__}: {e}")
                continue

            if not sections:
                skipped.append(f"{p.name} :: no extractable text")
                continue

            # dedup pass 1: content hash
            ch = _content_hash(sections)
            if ch in seen_hashes:
                skipped.append(f"{p.name} :: dedup by content hash")
                continue
            # dedup pass 2: metadata key
            mk = _metadata_key(meta)
            if mk and mk in seen_metakeys:
                skipped.append(f"{p.name} :: dedup by title+author ({mk})")
                continue
            seen_hashes.add(ch)
            if mk:
                seen_metakeys.add(mk)

            title = meta.get("title") or p.stem
            raw_text = "\n\n".join(s.text for s in sections)
            yield RawDocument(
                source="book",
                source_id=p.name,             # filename = stable per-file id
                title=title,
                url=f"file://{p}",            # local; no http
                published=meta.get("year"),   # year only
                raw_text=raw_text,
                sections=sections,
                metadata=meta,                 # author/year/isbn/genre
            )

        if skipped:
            log = BOOKS_DIR.parent / "books_skipped.log"
            try:
                log.write_text("\n".join(skipped) + "\n", encoding="utf-8")
                print(f"[books] {len(skipped)} skipped/dedup -> {log}")
            except OSError:
                # NAS may be read-only from the build host; fall back to dead-db dir
                fallback = Path(__file__).parent.parent / "books_skipped.log"
                fallback.write_text("\n".join(skipped) + "\n", encoding="utf-8")
                print(f"[books] {len(skipped)} skipped/dedup -> {fallback}")
```

Implementer notes:
- The EPUB extraction is the bulk of the complexity. Test on ONE well-formed
  EPUB first (recommend: `Searching for the Sound - Phil Lesh.epub` — small,
  clean, modern format) before running the full ingest. Print the parsed
  metadata dict + the list of chapter headings + the byte count of the first
  chapter's extracted text. If it looks wrong, fix it before processing 38
  more books.
- `pypdf` is a single pure-Python dependency, no native code. Pin
  `pypdf>=4.0`. Do not add `pdfminer`, `pdfplumber`, `PyPDF2` (deprecated),
  or `pymupdf` (requires native libs).
- PDF skip heuristic: >50MB OR matches SKIP_PATTERNS. The four currently
  skipped: DeadBase 50, Live Dead - Bob Minkin, Ultimate Grateful Dead,
  Cornell 77 - The Music. If `Outtakes from Garcia` (1.13 MB) and
  `The Grateful Dead Reader` (23 MB) extract OK, great. If they come back
  with garbage text (scanned without OCR), the validation criterion below
  will catch it and we'll add them to the skip list.
- `source_id` is the filename, not the full path — this keeps re-ingest
  idempotent even if the library moves.
- Metadata's `genre` field is intentionally left None on ingest. The user
  populates it later via a simple SQL update or a small curation script —
  this is the right tradeoff for the fuzzy classification cases (oral
  histories, anthologies, etc.).

---

## `lore/build_books.py` — runnable entry

```python
"""Build the books corpus into dead_lore.db. Run: python3 -m lore.build_books"""
from .build_lore_db import ingest
from .fetchers.books import BooksFetcher


def main() -> int:
    n_docs, n_chunks = ingest(BooksFetcher())
    print(f"[books] ingested {n_docs} docs, {n_chunks} chunks")
    assert n_docs >= 25, f"expected >=25 books after dedup, got {n_docs}"
    assert n_chunks >= n_docs * 5, (
        f"expected >=5 chunks per book on average, got {n_chunks} for {n_docs} docs"
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
```

---

## Genre tagging — a follow-on, not part of this phase

After the build runs, you'll have ~25-35 book documents with `metadata.genre
= NULL`. The intended workflow for filling those in:

1. Run the build.
2. Open dead_lore.db, `SELECT id, title, json_extract(metadata, '$.author')
   FROM documents WHERE source = 'book';`
3. Hand-curate a small tagging script or do it via direct SQL updates:
   ```sql
   UPDATE documents
   SET metadata = json_set(metadata, '$.genre', 'memoir')
   WHERE source = 'book' AND title LIKE '%Searching for the Sound%';
   ```
4. No re-ingest needed. Metadata edits don't invalidate chunks or vectors.

This is the payoff for keeping the genre in metadata-JSON instead of the
source value. Reclassification is one UPDATE, not a rebuild.

---

## Success criteria

1. New column `documents.metadata` exists. `RawDocument.metadata` field
   added with default `None`. `build_lore_db.ingest()` carries it through.
2. The existing smoke test still passes UNCHANGED
   (`python3 -m lore.smoke_test` -> "OK: 3 docs..."). The
   smoke test's FakeFetcher doesn't set `metadata`; verify it stores NULL.
3. `lore/fetchers/books.py` and `lore/build_books.py` exist.
4. `pypdf>=4.0` added to requirements.txt (one new line; don't remove
   anything else).
5. Single-EPUB test passes: extract `Searching for the Sound - Phil
   Lesh.epub` and print metadata dict + chapter heading list + first
   chapter byte count. Sanity check before running full ingest.
6. `python3 -m lore.build_books` ingests >=25 books, prints doc/chunk
   counts, writes `books_skipped.log` listing dedup hits + skipped PDFs.
   Skip log MUST include:
   - 4 large image-heavy PDFs (DeadBase 50, Live Dead - Bob Minkin,
     Ultimate Grateful Dead, Cornell 77 - The Music)
   - 1-2 dedup'd EPUBs (one of the Dead-to-the-Core variants, one of the
     Grateful Dead and Philosophy variants if same)
7. Spot-check retrieval (sanity, not asserts):
   ```python
   from lore.query import search
   search("Phil Lesh's first time on LSD", k=3)
   search("how did Kreutzmann and Mickey Hart's drumming differ", k=3)
   search("the Wall of Sound's engineering challenges", k=3)
   ```
   Book chunks (source="book") should surface, ideally from the right
   memoirs (Lesh / Kreutzmann / Scully respectively). Verify chunks have
   real chapter headings in `chunks.section`, not `Chapter 1` everywhere.
8. Spot-check metadata column:
   ```sql
   SELECT title,
          json_extract(metadata, '$.author') AS author,
          json_extract(metadata, '$.year') AS year
   FROM documents WHERE source = 'book' LIMIT 10;
   ```
   Most rows should have a real author. A few EPUBs have garbage metadata
   (the OPF is sometimes wrong) — that's OK, log a count of NULL authors.
9. No git commit — user reviews diff and commits.

---

## Out of scope (do not build this phase)

- OCR of image-heavy PDFs (DeadBase, Live Dead, Ultimate GD, Cornell 77 -
  The Music). Future iGPU project, like Whisper for Deadcast.
- MOBI extraction. There are no .mobi files in the current corpus inventory
  (despite the user mentioning MOBI as a possible format). If any appear
  later, that's a follow-on.
- Genre auto-classification. The metadata.genre field is populated manually
  post-ingest, as documented above.
- Per-book chunk-quality tuning. Some books (lyrics annotations, almanacks,
  reference works) chunk less well than narrative prose; live with it.
- Song matching for chunks.mentioned_songs — that's the next phase.
- MCP tools (dead_lore, dead_ask) — later phase.
- ebooklib, beautifulsoup, pdfminer, pdfplumber, PyPDF2, pymupdf — banned.
  Stdlib + pypdf only.

---

## Notes for the implementer

- Read this file + lore/SPEC.md before coding. The contract change
  (documents.metadata + RawDocument.metadata) MUST be backward compatible
  with the existing smoke test. If your changes break it, you've done it
  wrong; stop and reconsider.
- Test EPUB extraction on one book before running the full ingest. EPUB
  spec is loose and books vary — some have malformed OPF, missing TOC,
  inconsistent encoding. The fetcher's per-file try/except + skip-log
  pattern handles these gracefully; don't try to "rescue" malformed files.
- Test PDF extraction on `Outtakes from Garcia` (1.13 MB) first — if it
  returns clean text, the small text-PDFs work and `The Grateful Dead
  Reader` (23 MB) should too. If it returns garbage, both small PDFs are
  scanned and the spec's PDF coverage is effectively EPUB-only this phase.
- The NAS path is read from `BOOKS_DIR` env var with a default. The default
  works on arrstack assuming the NAS is mounted at that path; if not, the
  user will set `BOOKS_DIR` before running.
- `books_skipped.log` is the workflow file: user reads it after each run,
  fixes filenames or moves files as needed, re-runs. Same loop as
  wikipedia_unresolved.log.
- Be polite to the local NAS too — sequential file reads, no parallelism.
  This isn't a network etiquette thing, it's a "don't thrash the disk while
  Plex is also reading" thing.
- Match existing dead-db style: terse docstrings, stdlib over deps.
