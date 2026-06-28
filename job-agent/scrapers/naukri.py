"""Naukri.com scraper (Playwright + BeautifulSoup).

Naukri is JS-heavy and aggressively fingerprints bots. We launch headless
Chromium with a realistic UA, set common browser headers, and sleep between
requests. Even so: VPN / residential proxy may be required in practice.

Search URL pattern:
    https://www.naukri.com/<role>-jobs-in-<location>
"""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base_scraper import BaseScraper, Job


BASE_URL = "https://www.naukri.com"


def _slugify(value: str) -> str:
    return "-".join(value.strip().lower().split())


class NaukriScraper(BaseScraper):
    platform = "naukri"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    def _build_url(self, role: str, location: str) -> str:
        role_slug = _slugify(role) or "jobs"
        if location:
            loc_slug = _slugify(location)
            return f"{BASE_URL}/{role_slug}-jobs-in-{loc_slug}"
        return f"{BASE_URL}/{role_slug}-jobs"

    def _parse_cards(
        self, html: str, role: str, location: str, job_type: str
    ) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        cards = (
            soup.select("div.srp-jobtuple-wrapper")
            or soup.select("article.jobTuple")
            or soup.select("div.jobTuple")
            or soup.select("[class*='jobTuple']")
        )

        jobs: list[Job] = []
        for card in cards:
            try:
                title_el = card.select_one("a.title, a.jobTitle, a[class*='title']")
                if title_el is None:
                    continue
                title = self._clean_text(title_el.get_text())
                href = title_el.get("href") or ""
                url = urljoin(BASE_URL, href) if href else ""

                company_el = card.select_one(
                    "a.comp-name, .companyInfo .compName, .subTitle, a.compName"
                )
                company = self._clean_text(
                    company_el.get_text() if company_el else ""
                )

                loc_el = card.select_one(
                    "span.locWdth, .loc, .location, [class*='locWdth']"
                )
                loc = self._clean_text(loc_el.get_text() if loc_el else "")

                exp_el = card.select_one(
                    "span.expwdth, .exp, [class*='expwdth']"
                )
                exp = self._clean_text(exp_el.get_text() if exp_el else "")

                desc_el = card.select_one(
                    ".job-description, .job-desc, [class*='jobDesc']"
                )
                desc = self._clean_text(desc_el.get_text() if desc_el else "")

                if url and not any(j.url == url for j in jobs):
                    jobs.append(
                        Job(
                            title=title or "(no title)",
                            company=company,
                            location=loc or location,
                            job_type=job_type or exp,
                            platform=self.platform,
                            url=url,
                            description=desc,
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
                        extra_http_headers={
                            "Accept-Language": "en-IN,en;q=0.9",
                            "Sec-Ch-Ua-Platform": '"Windows"',
                        },
                    )
                    page = context.new_page()
                    try:
                        page.goto(url, timeout=self.timeout * 1000)
                        page.wait_for_load_state("networkidle", timeout=self.timeout * 1000)
                        self._random_delay(2, 4)
                        # Naukri lazy-loads cards as you scroll
                        for _ in range(3):
                            page.mouse.wheel(0, 2000)
                            self._random_delay(1, 2)
                    except Exception as exc:
                        self.logger.warning("page load failed: %s", exc)

                    html = page.content()
                    cards = self._parse_cards(html, role, location, job_type)
                    self.logger.info("%d cards on %s", len(cards), url)
                    results.extend(cards)

                    # Try to enrich with full descriptions for the top N
                    for j in results[: self.max_results]:
                        if j.description:
                            continue
                        try:
                            page.goto(j.url, timeout=self.timeout * 1000)
                            page.wait_for_load_state("domcontentloaded")
                            self._random_delay(1, 3)
                            detail_html = page.content()
                            detail_soup = BeautifulSoup(detail_html, "html.parser")
                            desc_el = (
                                detail_soup.select_one(".job-desc")
                                or detail_soup.select_one("section.styles_JDC__dang-inner-html__h0K4t")
                                or detail_soup.select_one("[class*='JDC__']")
                            )
                            if desc_el:
                                j.description = self._clean_text(
                                    desc_el.get_text(" ")
                                )
                        except Exception as exc:
                            self.logger.warning(
                                "detail fetch failed for %s: %s", j.url, exc
                            )
                finally:
                    browser.close()
        except Exception as exc:
            self.logger.error("naukri scrape aborted: %s", exc)

        return results[: self.max_results]


if __name__ == "__main__":
    scraper = NaukriScraper({"max_results": 5, "headless": True})
    try:
        jobs = scraper.search("python developer", "Bengaluru", "")
        print(f"Found {len(jobs)} jobs:")
        for j in jobs:
            print(f"  - {j.title} @ {j.company} ({j.location}) -> {j.url}")
    except Exception as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
