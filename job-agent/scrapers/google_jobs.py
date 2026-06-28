"""Google Jobs scraper (Playwright, no API keys).

Hits the public Google Jobs interface directly:
    https://www.google.com/search?q=<role>+jobs+<location>&ibp=htl;jobs

Google rotates DOM classes constantly, ships consent banners in some
regions, and will rate-limit aggressive scraping. We use a realistic UA,
random delays, and several selector fallbacks. Even so: expect to tune
the CSS selectors in `_parse_cards()` when Google reshuffles the page.
"""

from __future__ import annotations

import hashlib
import sys
from typing import Any
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from .base_scraper import BaseScraper, Job


BASE_URL = "https://www.google.com"
JOBS_PATH = "/search?q={query}&ibp=htl;jobs&hl=en"


def _stable_url(title: str, company: str, location: str) -> str:
    """Build a deterministic URL for dedup when Google doesn't expose a real apply link."""
    seed = f"{title}|{company}|{location}".lower().strip()
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()[:12]
    q = quote_plus(f"{title} {company}".strip()) or "jobs"
    return f"{BASE_URL}/search?q={q}&ibp=htl;jobs#{digest}"


class GoogleJobsScraper(BaseScraper):
    platform = "google_jobs"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    def _build_url(self, role: str, location: str) -> str:
        bits: list[str] = []
        if role.strip():
            bits.append(role.strip())
        bits.append("jobs")
        if location.strip():
            bits.append(location.strip())
        query = quote_plus(" ".join(bits))
        return BASE_URL + JOBS_PATH.format(query=query)

    @staticmethod
    def _dismiss_consent(page) -> None:
        """Click through Google's EU consent banner if it shows up."""
        for sel in [
            "button:has-text('I agree')",
            "button:has-text('Accept all')",
            "button:has-text('Reject all')",
            "form[action*='consent'] button",
            "div[role='dialog'] button",
        ]:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=1500)
                    return
            except Exception:
                continue

    # ------------------------------------------------------------------
    def _parse_cards(
        self, html: str, role: str, location: str, job_type: str
    ) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        cards = (
            soup.select("li[role='treeitem']")
            or soup.select("div[role='treeitem']")
            or soup.select("li.iFjolb")
            or soup.select("div.PwjeAc")
            or soup.select("[data-encoded-docid]")
            or soup.select("div.gws-plugins-horizon-jobs__li-ed")
        )

        jobs: list[Job] = []
        for card in cards:
            try:
                title_el = (
                    card.select_one("div[role='heading'] span")
                    or card.select_one("div[role='heading']")
                    or card.select_one("h2")
                    or card.select_one(".BjJfJf")
                    or card.select_one(".PUpOsf")
                )
                title = self._clean_text(title_el.get_text() if title_el else "")
                if not title:
                    continue

                # Company name - common patterns
                company_el = (
                    card.select_one(".vNEEBe")
                    or card.select_one("div.nJlQNd")
                    or card.select_one("div.wHYlTd:nth-of-type(1)")
                    or card.select_one(".oNwCmf div:nth-of-type(1)")
                )
                company = self._clean_text(
                    company_el.get_text() if company_el else ""
                )

                # Location
                loc_el = (
                    card.select_one(".Qk80Jf")
                    or card.select_one(".tj9XXd")
                    or card.select_one(".wHYlTd:nth-of-type(2)")
                )
                loc = self._clean_text(loc_el.get_text() if loc_el else "")

                # Snippet (sometimes shown inline)
                snippet_el = (
                    card.select_one(".HBvzbc")
                    or card.select_one(".YgLbBe")
                    or card.select_one(".YxiUEd")
                )
                snippet = self._clean_text(
                    snippet_el.get_text() if snippet_el else ""
                )

                # External apply link is hidden behind a side-panel click in
                # Google Jobs; build a deterministic dedup URL instead.
                url = _stable_url(title, company, loc or location)

                if not any(j.url == url for j in jobs):
                    jobs.append(
                        Job(
                            title=title,
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

    # ------------------------------------------------------------------
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
                        extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
                    )
                    page = context.new_page()
                    try:
                        page.goto(url, timeout=self.timeout * 1000)
                        page.wait_for_load_state("domcontentloaded")
                        self._random_delay(2, 4)
                        self._dismiss_consent(page)

                        # Google Jobs lazy-loads more results as you scroll.
                        for _ in range(4):
                            page.mouse.wheel(0, 2200)
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
            self.logger.error("google_jobs scrape aborted: %s", exc)

        return results[: self.max_results]


if __name__ == "__main__":
    scraper = GoogleJobsScraper({"max_results": 5, "headless": True})
    try:
        jobs = scraper.search("machine learning engineer", "Bengaluru, India", "")
        print(f"Found {len(jobs)} jobs:")
        for j in jobs:
            print(f"  - {j.title} @ {j.company} ({j.location}) -> {j.url}")
    except Exception as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
