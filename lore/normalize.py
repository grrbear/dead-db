"""RawDocument -> list[Chunk]. Dumb-but-reasonable chunking for v1.

mentioned_songs stays empty in this phase; song matching against
dead.db.songs lands in a later phase along with the real fetchers.
"""
import re
from dataclasses import dataclass
from datetime import datetime

from .config import CHUNK_SIZE, CHUNK_OVERLAP
from .fetchers._base import RawDocument

DATE_RE = re.compile(r"\b(19[6-9]\d|20\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")


@dataclass
class Chunk:
    chunk_index: int
    text: str
    mentioned_dates: list[str]
    mentioned_songs: list[str]
    era: str | None


def _approx_tokens(s: str) -> int:
    return len(s) // 4  # cheap proxy; good enough for sizing


def _era_for(dates: list[str]) -> str | None:
    """Coarse era label from the earliest in-band date in the chunk."""
    if not dates:
        return None
    try:
        d = min(datetime.fromisoformat(x) for x in dates)
    except ValueError:
        return None
    y = d.year
    if y < 1972: return "pigpen"
    if y < 1979: return "keith"
    if y < 1990: return "brent"
    if y <= 1995: return "bruce"
    return None


def normalize(doc: RawDocument) -> list[Chunk]:
    """Split on paragraph boundaries, then merge to ~CHUNK_SIZE tokens with overlap."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", doc.raw_text) if p.strip()]
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    idx = 0

    def flush():
        nonlocal buf, buf_tokens, idx
        if not buf:
            return
        text = "\n\n".join(buf)
        dates = sorted({m.group(0) for m in DATE_RE.finditer(text)})
        chunks.append(Chunk(
            chunk_index=idx, text=text,
            mentioned_dates=dates, mentioned_songs=[],
            era=_era_for(dates),
        ))
        idx += 1
        # overlap: keep tail paragraphs ~CHUNK_OVERLAP tokens
        tail, tail_tokens = [], 0
        for p in reversed(buf):
            tail.insert(0, p); tail_tokens += _approx_tokens(p)
            if tail_tokens >= CHUNK_OVERLAP:
                break
        buf = tail
        buf_tokens = tail_tokens

    for p in paras:
        pt = _approx_tokens(p)
        if buf_tokens + pt > CHUNK_SIZE and buf:
            flush()
        buf.append(p); buf_tokens += pt

    flush()
    return chunks
