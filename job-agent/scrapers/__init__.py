"""Scrapers package.

Re-exports the shared `BaseScraper` ABC and the normalized `Job` dataclass
so callers can do:

    from scrapers import BaseScraper, Job
"""

from .base_scraper import BaseScraper, Job

__all__ = ["BaseScraper", "Job"]
