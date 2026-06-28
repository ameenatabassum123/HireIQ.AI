"""Internshala scraper (requests + BeautifulSoup).

Internshala renders job listings server-side, so a plain HTTP fetch + BS4 is
enough - no headless browser required.

Search URLs:
    https://internshala.com/jobs/keywords-<role>/
    https://internshala.com/internships/keywords-<role>/

Selectors are based on the current site layout (Q2 2026). Sites change;
expect to tune selectors over time.
"""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup

from .base_scraper import BaseScraper, Job


BASE_URL = "https://internshala.com"


class InternshalaScraper(BaseScraper):
    platform = "internshala"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/webp,*/*;q=0.8"
                ),
            }
        )

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _search_urls(self, role: str, job_type: str) -> list[str]:
        slug = role.strip().lower().replace(" ", "-")
        slug = quote_plus(slug).replace("%2D", "-")
        urls: list[str] = []
        # Choose section based on job_type. Default: cover both.
        jt = job_type.lower()
        if "intern" in jt:
            urls.append(f"{BASE_URL}/internships/keywords-{slug}/")
        elif jt and "intern" not in jt:
            urls.append(f"{BASE_URL}/jobs/keywords-{slug}/")
        else:
            urls.append(f"{BASE_URL}/jobs/keywords-{slug}/")
            urls.append(f"{BASE_URL}/internships/keywords-{slug}/")
        return urls

    def _parse_listing_page(
        self, html: str, role: str, location: str, job_type: str
    ) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(".individual_internship") or soup.select(
            "div[internshipid], div[jobid]"
        )
        jobs: list[Job] = []
        for card in cards:
            try:
                title_el = card.select_one(
                    ".job-internship-name a, .heading_4_5 a, .job-title-href"
                )
                if title_el is None:
                    continue
                title = self._clean_text(title_el.get_text())
                href = title_el.get("href") or card.get("data-href") or ""
                url = urljoin(BASE_URL, href) if href else ""

                company_el = card.select_one(
                    ".company-name, .company_name, p.company h3, .company a"
                )
                company = self._clean_text(
                    company_el.get_text() if company_el else ""
                )

                loc_el = card.select_one(
                    ".locations a, .location_link, .row-1-item.locations"
                )
                loc = self._clean_text(loc_el.get_text() if loc_el else "")

                # Heuristic: type = "internship" if URL has /internship/ in path
                guessed_type = (
                    "internship"
                    if "/internship/" in url or "internship" in href.lower()
                    else "job"
                )
                effective_type = job_type or guessed_type

                if url and not any(j.url == url for j in jobs):
                    jobs.append(
                        Job(
                            title=title or "(no title)",
                            company=company,
                            location=loc or location,
                            job_type=effective_type,
                            platform=self.platform,
                            url=url,
                        )
                    )
            except Exception as exc:  # individual card failure shouldn't kill the run
                self.logger.warning("card parse failed: %s", exc)
                continue
        return jobs

    def _fetch_details(self, job: Job) -> None:
        """Visit the job page and fill in `description`."""
        try:
            resp = self.session.get(job.url, timeout=self.timeout)
            resp.raise_for_status()
        except Exception as exc:
            self.logger.warning("detail fetch failed for %s: %s", job.url, exc)
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        desc_el = (
            soup.select_one(".internship_details, .text-container")
            or soup.select_one(".about-the-internship")
            or soup.select_one(".detail_view")
        )
        if desc_el:
            job.description = self._clean_text(desc_el.get_text(" "))

    # ------------------------------------------------------------------
    @staticmethod
    def _paginate_url(url: str, page_num: int) -> str:
        if page_num <= 1:
            return url
        return f"{url}page-{page_num}/"

    def search(self, role: str, location: str, job_type: str = "") -> list[Job]:
        results: list[Job] = []
        for url in self._search_urls(role, job_type):
            max_pages = max(1, (self.max_results + 24) // 25)
            for page_num in range(1, max_pages + 1):
                page_url = self._paginate_url(url, page_num)
                try:
                    resp = self.session.get(page_url, timeout=self.timeout)
                    resp.raise_for_status()
                except Exception as exc:
                    self.logger.warning("listing fetch failed for %s: %s", page_url, exc)
                    break

                page_jobs = self._parse_listing_page(
                    resp.text, role=role, location=location, job_type=job_type
                )
                self.logger.info("%s -> %d cards", page_url, len(page_jobs))
                if not page_jobs:
                    break

                for j in page_jobs:
                    if any(existing.url == j.url for existing in results):
                        continue
                    self._fetch_details(j)
                    results.append(j)
                    if len(results) >= self.max_results:
                        break
                    self._random_delay(1, 2)

                if len(results) >= self.max_results:
                    break
                self._random_delay(1, 3)
            if len(results) >= self.max_results:
                break

        # Filter by location loosely (substring, case-insensitive) if provided.
        if location:
            loc_l = location.lower()
            results = [
                j for j in results
                if not j.location or loc_l in j.location.lower()
                or j.location.lower() in ("remote", "work from home")
            ]
        return results[: self.max_results]


if __name__ == "__main__":
    scraper = InternshalaScraper({"max_results": 5, "timeout": 20})
    try:
        jobs = scraper.search("python developer", "Remote", "")
        print(f"Found {len(jobs)} jobs:")
        for j in jobs:
            print(f"  - {j.title} @ {j.company} ({j.location}) -> {j.url}")
    except Exception as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
    finally:
        scraper.close()
