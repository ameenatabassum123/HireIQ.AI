"""Glassdoor India scraper (Playwright + BeautifulSoup).

Glassdoor pops up a "Create an account / Sign in" modal a few seconds after
load. We click it away (multiple known close-button selectors) before
parsing the listings.

Search URL:
    https://www.glassdoor.co.in/Job/jobs.htm?sc.keyword=<role>&locKeyword=<location>
"""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from .base_scraper import BaseScraper, Job


BASE_URL = "https://www.glassdoor.co.in"

CLOSE_POPUP_SELECTORS = [
    "[data-test='hardsell-modal-close']",
    "button[alt='Close']",
    "button[aria-label='Close']",
    ".modal_closeIcon",
    "span.SVGInline.modal_closeIcon",
    "button[data-test='close-button']",
]


class GlassdoorScraper(BaseScraper):
    platform = "glassdoor"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    def _build_url(self, role: str, location: str) -> str:
        return (
            f"{BASE_URL}/Job/jobs.htm"
            f"?sc.keyword={quote_plus(role)}"
            f"&locKeyword={quote_plus(location)}"
        )

    def _dismiss_login_popup(self, page) -> None:
        for sel in CLOSE_POPUP_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=2000)
                    self.logger.info("dismissed login popup via %s", sel)
                    self._random_delay(0.5, 1.5)
                    return
            except Exception:
                continue
        # Last-resort: press Escape
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    def _parse_cards(
        self, html: str, role: str, location: str, job_type: str
    ) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        cards = (
            soup.select("li[data-test='jobListing']")
            or soup.select("div.JobsList_jobListItem__wjTHv")
            or soup.select("[data-test='job-link']")
        )

        jobs: list[Job] = []
        for card in cards:
            try:
                link_el = card.select_one(
                    "a[data-test='job-link'], a.JobCard_jobTitle, a.jobLink"
                )
                if link_el is None:
                    continue
                title = self._clean_text(link_el.get_text())
                href = link_el.get("href") or ""
                url = urljoin(BASE_URL, href) if href else ""

                company_el = card.select_one(
                    "[data-test='employer-name'], .EmployerProfile_compactEmployerName, .employerName"
                )
                company = self._clean_text(
                    company_el.get_text() if company_el else ""
                )

                loc_el = card.select_one(
                    "[data-test='emp-location'], .JobCard_location, .location"
                )
                loc = self._clean_text(loc_el.get_text() if loc_el else "")

                snippet_el = card.select_one(
                    "[data-test='descSnippet'], .jobDescriptionContent"
                )
                snippet = self._clean_text(
                    snippet_el.get_text() if snippet_el else ""
                )

                if url and not any(j.url == url for j in jobs):
                    jobs.append(
                        Job(
                            title=title or "(no title)",
                            company=company,
                            location=loc or location,
                            job_type=job_type,
                            platform=self.platform,
                            url=url,
                            description=snippet,
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
                        locale="en-IN",
                    )
                    page = context.new_page()

                    try:
                        page.goto(url, timeout=self.timeout * 1000)
                        page.wait_for_load_state("domcontentloaded")
                        self._random_delay(3, 5)
                        self._dismiss_login_popup(page)

                        # Scroll to load more cards; dismiss popup again if it reappears.
                        for _ in range(3):
                            page.mouse.wheel(0, 2000)
                            self._random_delay(1, 2)
                            self._dismiss_login_popup(page)
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
            self.logger.error("glassdoor scrape aborted: %s", exc)

        return results[: self.max_results]


if __name__ == "__main__":
    scraper = GlassdoorScraper({"max_results": 5, "headless": True})
    try:
        jobs = scraper.search("python developer", "Bengaluru", "")
        print(f"Found {len(jobs)} jobs:")
        for j in jobs:
            print(f"  - {j.title} @ {j.company} ({j.location}) -> {j.url}")
    except Exception as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
