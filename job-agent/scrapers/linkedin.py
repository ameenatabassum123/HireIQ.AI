"""LinkedIn Jobs scraper (Playwright, no login).

Public LinkedIn job search renders some cards without authentication:
    https://www.linkedin.com/jobs/search/?keywords=<role>&location=<location>

NOTE: Full job descriptions, salary, applicant counts, and "Easy Apply"
flows all REQUIRE a logged-in session. This scraper deliberately stays
unauthenticated to keep the user's account safe from rate-limiting / bans.
Only fields visible on the public search page are populated.
"""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from .base_scraper import BaseScraper, Job


BASE_URL = "https://www.linkedin.com"


class LinkedInScraper(BaseScraper):
    platform = "linkedin"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    def _build_url(self, role: str, location: str) -> str:
        q = quote_plus(role)
        l = quote_plus(location) if location else ""
        return (
            f"{BASE_URL}/jobs/search/"
            f"?keywords={q}"
            + (f"&location={l}" if l else "")
            + "&f_TPR=r604800"
        )

    def _parse_cards(
        self, html: str, role: str, location: str, job_type: str
    ) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        cards = (
            soup.select("div.base-card")
            or soup.select("li div.job-search-card")
            or soup.select("[data-entity-urn*='job']")
        )

        jobs: list[Job] = []
        for card in cards:
            try:
                link_el = card.select_one(
                    "a.base-card__full-link, a.job-card-list__title, a[href*='/jobs/view/']"
                )
                if link_el is None:
                    continue
                href = link_el.get("href") or ""
                url = urljoin(BASE_URL, href).split("?")[0]

                title_el = card.select_one(
                    "h3.base-search-card__title, .base-search-card__title, span.sr-only"
                )
                title = self._clean_text(
                    title_el.get_text() if title_el else link_el.get_text()
                )

                company_el = card.select_one(
                    "h4.base-search-card__subtitle, .base-search-card__subtitle a, .base-search-card__subtitle"
                )
                company = self._clean_text(
                    company_el.get_text() if company_el else ""
                )

                loc_el = card.select_one(
                    "span.job-search-card__location, .job-search-card__location"
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
                        locale="en-US",
                    )
                    page = context.new_page()
                    try:
                        page.goto(url, timeout=self.timeout * 1000)
                        page.wait_for_load_state("domcontentloaded")
                        self._random_delay(2, 4)
                        # LinkedIn lazy-loads more cards on scroll
                        for _ in range(4):
                            page.mouse.wheel(0, 2500)
                            self._random_delay(1, 2)
                    except Exception as exc:
                        self.logger.warning("page load failed: %s", exc)

                    self._save_debug_screenshot(page, "pre_extract")
                    html = page.content()
                    cards = self._parse_cards(html, role, location, job_type)
                    self.logger.info("%d cards on %s", len(cards), url)
                    results.extend(cards)
                finally:
                    browser.close()
        except Exception as exc:
            self.logger.error("linkedin scrape aborted: %s", exc)

        return results[: self.max_results]


if __name__ == "__main__":
    scraper = LinkedInScraper({"max_results": 5, "headless": True})
    try:
        jobs = scraper.search("machine learning engineer", "Bengaluru", "")
        print(f"Found {len(jobs)} jobs:")
        for j in jobs:
            print(f"  - {j.title} @ {j.company} ({j.location}) -> {j.url}")
        print(
            "\nNote: full job descriptions require a logged-in LinkedIn "
            "session and are not scraped here."
        )
    except Exception as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
