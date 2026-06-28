"""Indeed India scraper (Playwright + BeautifulSoup).

Indeed runs Cloudflare + bot detection, so a real browser is required.
We paginate up to 3 result pages (start=0, 10, 20) and visit each job
detail page to extract the full description.

Selectors as of Q2 2026; expect periodic tuning.
"""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from .base_scraper import BaseScraper, Job


BASE_URL = "https://in.indeed.com"
MAX_PAGES = 3
RESULTS_PER_PAGE = 10


class IndeedScraper(BaseScraper):
    platform = "indeed"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    def _build_url(self, role: str, location: str, page: int) -> str:
        q = quote_plus(role)
        l = quote_plus(location)
        start = page * RESULTS_PER_PAGE
        return f"{BASE_URL}/jobs?q={q}&l={l}&start={start}"

    def _parse_cards(
        self, html: str, role: str, location: str, job_type: str
    ) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        cards = (
            soup.select("div.job_seen_beacon")
            or soup.select("[data-testid='result-card']")
            or soup.select("td.resultContent")
        )

        jobs: list[Job] = []
        for card in cards:
            try:
                title_el = card.select_one(
                    "h2.jobTitle span[title], h2.jobTitle a span, h2.jobTitle"
                )
                link_el = card.select_one("h2.jobTitle a, a.jcs-JobTitle")
                if not (title_el and link_el):
                    continue
                title = self._clean_text(
                    title_el.get("title") or title_el.get_text()
                )
                href = link_el.get("href") or ""
                url = urljoin(BASE_URL, href) if href else ""

                company_el = card.select_one(
                    "[data-testid='company-name'], span.companyName, .companyName"
                )
                company = self._clean_text(
                    company_el.get_text() if company_el else ""
                )

                loc_el = card.select_one(
                    "[data-testid='text-location'], div.companyLocation, .companyLocation"
                )
                loc = self._clean_text(loc_el.get_text() if loc_el else "")

                type_el = card.select_one(
                    "[data-testid='attribute_snippet_testid'], div.metadata"
                )
                detected_type = self._clean_text(
                    type_el.get_text() if type_el else ""
                )

                snippet_el = card.select_one(
                    ".job-snippet, [data-testid='belowJobSnippet']"
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
                            job_type=job_type or detected_type,
                            platform=self.platform,
                            url=url,
                            description=snippet,
                        )
                    )
            except Exception as exc:
                self.logger.warning("card parse failed: %s", exc)
                continue
        return jobs

    # ------------------------------------------------------------------
    def search(self, role: str, location: str, job_type: str = "") -> list[Job]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.error(
                "playwright is not installed. run: pip install playwright && playwright install"
            )
            return []

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

                    # Listing pages
                    for page_idx in range(MAX_PAGES):
                        url = self._build_url(role, location, page_idx)
                        try:
                            page.goto(url, timeout=self.timeout * 1000)
                            page.wait_for_load_state("domcontentloaded")
                        except Exception as exc:
                            self.logger.warning("page %d failed: %s", page_idx, exc)
                            continue

                        self._random_delay(2, 4)
                        self._save_debug_screenshot(page, f"pre_extract_p{page_idx + 1}")
                        html = page.content()
                        cards = self._parse_cards(html, role, location, job_type)
                        self.logger.info(
                            "page %d -> %d cards (%s)", page_idx, len(cards), url
                        )

                        for j in cards:
                            if not any(r.url == j.url for r in results):
                                results.append(j)
                            if len(results) >= self.max_results:
                                break

                        if len(results) >= self.max_results or not cards:
                            break
                        self._random_delay(2, 5)

                    # Detail pages
                    for j in results:
                        try:
                            page.goto(j.url, timeout=self.timeout * 1000)
                            page.wait_for_selector(
                                "#jobDescriptionText, .jobsearch-jobDescriptionText",
                                timeout=8000,
                            )
                            detail_html = page.content()
                            detail_soup = BeautifulSoup(detail_html, "html.parser")
                            desc_el = detail_soup.select_one(
                                "#jobDescriptionText, .jobsearch-jobDescriptionText"
                            )
                            if desc_el:
                                j.description = self._clean_text(
                                    desc_el.get_text(" ")
                                )
                        except Exception as exc:
                            self.logger.warning(
                                "detail page failed for %s: %s", j.url, exc
                            )
                        self._random_delay(1, 3)

                finally:
                    browser.close()
        except Exception as exc:
            self.logger.error("indeed scrape aborted: %s", exc)

        return results[: self.max_results]


if __name__ == "__main__":
    scraper = IndeedScraper({"max_results": 5, "headless": True})
    try:
        jobs = scraper.search("python developer", "Bengaluru", "full-time")
        print(f"Found {len(jobs)} jobs:")
        for j in jobs:
            print(f"  - {j.title} @ {j.company} ({j.location}) -> {j.url}")
    except Exception as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
