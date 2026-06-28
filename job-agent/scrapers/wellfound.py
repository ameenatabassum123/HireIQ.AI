"""Wellfound (formerly AngelList) scraper (Playwright + BeautifulSoup).

Wellfound is fully client-rendered React. Public listings are visible at:
    https://wellfound.com/jobs?q=<role>&l=<location>

Full company / role details usually require a login. We capture what's
publicly visible: title, company, location, URL.
"""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from .base_scraper import BaseScraper, Job


BASE_URL = "https://wellfound.com"


class WellfoundScraper(BaseScraper):
    platform = "wellfound"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    def _build_url(self, role: str, location: str) -> str:
        q = quote_plus(role)
        l = quote_plus(location)
        return f"{BASE_URL}/jobs?q={q}&l={l}"

    def _parse_cards(
        self, html: str, role: str, location: str, job_type: str
    ) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        cards = (
            soup.select("div[data-test='JobSearchResult']")
            or soup.select("div[data-test='StartupResult']")
            or soup.select("a[href*='/jobs/']")
        )

        jobs: list[Job] = []
        for card in cards:
            try:
                # The card itself may be a job link or contain one
                link_el = (
                    card if (card.name == "a" and card.get("href")) else None
                ) or card.select_one("a[href*='/jobs/']")
                if link_el is None:
                    continue
                href = link_el.get("href") or ""
                if "/jobs/" not in href:
                    continue
                url = urljoin(BASE_URL, href)

                title_el = card.select_one(
                    "h2, h3, [class*='styles_title'], a[href*='/jobs/'] span"
                )
                title = self._clean_text(
                    title_el.get_text() if title_el else link_el.get_text()
                )

                company_el = card.select_one(
                    "h4, [class*='startupName'], a[href*='/company/']"
                )
                company = self._clean_text(
                    company_el.get_text() if company_el else ""
                )

                loc_el = card.select_one(
                    "[class*='location'], [data-test='LocationsList']"
                )
                loc = self._clean_text(loc_el.get_text() if loc_el else "")

                if url and not any(j.url == url for j in jobs):
                    jobs.append(
                        Job(
                            title=title or "(no title)",
                            company=company,
                            location=loc or location,
                            job_type=job_type,
                            platform=self.platform,
                            url=url,
                        )
                    )
            except Exception as exc:
                self.logger.warning("card parse failed: %s", exc)
                continue
        return jobs

    def search(self, role: str, location: str, job_type: str = "") -> list[Job]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.error(
                "playwright is not installed. run: pip install playwright && playwright install"
            )
            return []

        url = self._build_url(role, location)
        results: list[Job] = []

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                try:
                    context = browser.new_context(
                        user_agent=self.user_agent,
                        viewport={"width": 1366, "height": 768},
                    )
                    page = context.new_page()
                    try:
                        page.goto(url, timeout=self.timeout * 1000)
                        page.wait_for_load_state("networkidle", timeout=self.timeout * 1000)
                        # Trigger lazy loaders
                        for _ in range(3):
                            page.mouse.wheel(0, 2000)
                            self._random_delay(1, 2)
                    except Exception as exc:
                        self.logger.warning("page load failed: %s", exc)

                    html = page.content()
                    cards = self._parse_cards(html, role, location, job_type)
                    self.logger.info("%d cards on %s", len(cards), url)
                    results.extend(cards)
                finally:
                    browser.close()
        except Exception as exc:
            self.logger.error("wellfound scrape aborted: %s", exc)

        return results[: self.max_results]


if __name__ == "__main__":
    scraper = WellfoundScraper({"max_results": 5, "headless": True})
    try:
        jobs = scraper.search("python", "Remote", "")
        print(f"Found {len(jobs)} jobs:")
        for j in jobs:
            print(f"  - {j.title} @ {j.company} ({j.location}) -> {j.url}")
    except Exception as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
