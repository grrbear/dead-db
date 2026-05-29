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
    "/hddpool/datastore/mediacenter/Audio/AudioBooks/Books - EPUB/Grateful Dead Books",
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
