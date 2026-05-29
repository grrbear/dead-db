"""Shared HTML -> (clean_text, sections) for Blogspot-style post bodies.
stdlib only: html.parser. No bs4, no lxml, no requests.
"""
from html.parser import HTMLParser
from ._base import Section

# Tags whose entire subtree is dropped (scripts, styles, and — critically —
# Blogger comment containers on the HTML fallback path).
_DROP_SUBTREE = {"script", "style", "noscript"}
# Block tags that force a paragraph break in output text.
_BLOCK = {"p", "div", "br", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
# Tags treated as section-heading boundaries for the sections hint.
_HEADING = {"h1", "h2", "h3", "h4", "b", "strong"}
# id/class substrings marking comment regions to drop on the HTML fallback.
_COMMENT_MARKERS = ("comment", "disqus", "comments")


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._drop_depth = 0
        self._in_comment_block = 0
        self._parts: list[str] = []
        # heading capture
        self._cap_heading = False
        self._heading_buf: list[str] = []
        # (heading, text) accumulation for sections
        self._cur_heading = ""
        self._cur_text: list[str] = []
        self.sections: list[Section] = []

    def _flush_section(self):
        text = _collapse("".join(self._cur_text))
        if text:
            self.sections.append(Section(heading=self._cur_heading, text=text))
        self._cur_text = []

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        ident = " ".join(filter(None, [ad.get("id", ""), ad.get("class", "")])).lower()
        if tag in _DROP_SUBTREE or any(m in ident for m in _COMMENT_MARKERS):
            self._drop_depth += 1
            self._in_comment_block += 1 if any(m in ident for m in _COMMENT_MARKERS) else 0
            return
        if self._drop_depth:
            return
        if tag in _HEADING:
            # a new heading boundary: close current section, start capturing
            self._flush_section()
            self._cap_heading = True
            self._heading_buf = []
        elif tag in _BLOCK:
            self._parts.append("\n")
            self._cur_text.append("\n")

    def handle_endtag(self, tag):
        ad_drop = tag in _DROP_SUBTREE
        if self._drop_depth and (ad_drop or self._in_comment_block):
            self._drop_depth = max(0, self._drop_depth - 1)
            if self._in_comment_block:
                self._in_comment_block = max(0, self._in_comment_block - 1)
            return
        if self._drop_depth:
            return
        if tag in _HEADING and self._cap_heading:
            self._cur_heading = _collapse("".join(self._heading_buf))
            self._cap_heading = False

    def handle_data(self, data):
        if self._drop_depth:
            return
        if self._cap_heading:
            self._heading_buf.append(data)
        self._parts.append(data)
        self._cur_text.append(data)

    def close(self):
        super().close()
        self._flush_section()


def _collapse(s: str) -> str:
    # collapse runs of whitespace, preserve paragraph breaks
    lines = [ln.strip() for ln in s.split("\n")]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln); blank = False
        elif not blank:
            out.append(""); blank = True
    return "\n".join(out).strip()


def html_to_text(html: str) -> tuple[str, list[Section]]:
    """Returns (clean_text, sections). sections may be empty if no headings."""
    ex = _Extractor()
    ex.feed(html)
    ex.close()
    text = _collapse("".join(ex._parts))
    # de-dupe: if only one section and it equals the whole text, treat as flat
    sections = ex.sections if len(ex.sections) > 1 else []
    return text, sections
