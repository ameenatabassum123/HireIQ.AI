"""Base scraper + shared `Job` dataclass.

Every platform-specific scraper subclasses `BaseScraper` and implements
`search(role, location, job_type)`. The orchestrator (`scraper_manager`)
treats every scraper identically through this interface.
"""

from __future__ import annotations

import logging
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from fake_useragent import UserAgent  # type: ignore

    _UA: UserAgent | None = UserAgent()
except Exception:  # pragma: no cover - fake-useragent has its own quirks
    _UA = None


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------
@dataclass
class Job:
    """Normalized representation of a single scraped job posting."""

    title: str
    company: str
    location: str
    job_type: str
    platform: str
    url: str
    description: str = ""
    date_scraped: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Base scraper
# ---------------------------------------------------------------------------
class BaseScraper(ABC):
    """Abstract base for every job-board scraper.

    Subclasses must:
        - set `platform` (lowercase identifier matching `config.yaml`)
        - implement `search(role, location, job_type)` returning `list[Job]`
    """

    platform: str = "base"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config or {}
        self.headless: bool = bool(self.config.get("headless", True))
        self.timeout: int = int(self.config.get("timeout", 30))
        self.max_results: int = int(self.config.get("max_results", 25))
        self.user_agent: str = self._random_user_agent()
        self.logger: logging.Logger = logging.getLogger(
            f"scrapers.{self.platform}"
        )

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def _random_user_agent() -> str:
        """Pick a realistic UA via fake-useragent, falling back to a sane default."""
        if _UA is not None:
            try:
                ua = _UA.random
                if ua:
                    return ua
            except Exception:
                pass
        return DEFAULT_UA

    def _random_delay(self, min_seconds: float = 1, max_seconds: float = 3) -> None:
        """Sleep for a random interval to look more human."""
        lo, hi = (
            (min_seconds, max_seconds)
            if min_seconds <= max_seconds
            else (max_seconds, min_seconds)
        )
        time.sleep(random.uniform(lo, hi))

    @staticmethod
    def _clean_text(text: str | None) -> str:
        """Strip + collapse whitespace + drop control chars."""
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"[\u0000-\u001f\u007f]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _save_debug_screenshot(self, page, label: str = "") -> str | None:
        """Capture a diagnostic screenshot before extraction."""
        try:
            root = Path(__file__).resolve().parent.parent
            out_dir = root / "output" / "scrape_debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safe_label = f"_{label}" if label else ""
            path = out_dir / f"{self.platform}{safe_label}_{ts}.png"
            page.screenshot(path=str(path), full_page=True)
            self.logger.info("saved debug screenshot: %s", path)
            return str(path)
        except Exception as exc:
            self.logger.warning("failed to save debug screenshot: %s", exc)
            return None

    # ---- API ------------------------------------------------------------
    @abstractmethod
    def search(
        self, role: str, location: str, job_type: str = ""
    ) -> list[Job]:
        """Search the platform and return a list of `Job` objects."""

    # ---- lifecycle ------------------------------------------------------
    def close(self) -> None:
        """Release any held resources (sessions, browsers, etc.)."""

    def __enter__(self) -> "BaseScraper":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
