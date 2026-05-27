"""Common interface every source-specific fetcher implements."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass
class RawDocument:
    source: str            # 'lia', 'wikipedia', ...
    source_id: str         # stable id within source (URL slug, article title)
    title: str
    url: str
    published: str | None  # ISO date if known, else None
    raw_text: str          # cleaned plain text, no HTML


class Fetcher(ABC):
    """Source-specific scraper. Each source = one subclass.

    The discover/fetch split exists so we can list source_ids cheaply
    (for incremental sync planning) before committing to body downloads.
    """
    name: str  # short identifier used as documents.source

    @abstractmethod
    def discover(self) -> list[str]:
        """Return source_ids to fetch. Cheap — no document body downloads."""
        ...

    @abstractmethod
    def fetch(self, source_ids: list[str] | None = None) -> Iterator[RawDocument]:
        """Yield RawDocument for each id (or all from discover() if None)."""
        ...
