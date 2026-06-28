"""Scraper orchestrator.

Reads `config/config.yaml`, instantiates every scraper whose platform is
toggled on, fans out across the configured `roles x locations` matrix,
deduplicates by URL, and persists everything to SQLite via `db.Database`.

Run as a script:
    python -m scrapers.scraper_manager
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Type

import yaml

from db.database import Database
from .apify_scraper import scrape_platform_jobs
from .base_scraper import BaseScraper, Job
from .glassdoor import GlassdoorScraper
from .google_jobs import GoogleJobsScraper
from .indeed import IndeedScraper
from .internshala import InternshalaScraper
from .linkedin import LinkedInScraper
from .naukri import NaukriScraper
from .wellfound import WellfoundScraper


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


SCRAPER_REGISTRY: dict[str, Type[BaseScraper]] = {
    "internshala": InternshalaScraper,
    "indeed": IndeedScraper,
    "naukri": NaukriScraper,
    "wellfound": WellfoundScraper,
    "glassdoor": GlassdoorScraper,
    "google_jobs": GoogleJobsScraper,
    "linkedin": LinkedInScraper,
}

APIFY_PLATFORMS: frozenset[str] = frozenset(
    {"linkedin", "indeed", "glassdoor", "google_jobs", "naukri"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_scraper_config(global_cfg: dict[str, Any]) -> dict[str, Any]:
    """Pluck the runtime knobs scrapers care about out of the global config."""
    automation = global_cfg.get("automation", {}) or {}
    job_search = global_cfg.get("job_search", {}) or {}
    base_timeout = int(automation.get("timeout", 20))
    return {
        "headless": bool(automation.get("headless", True)),
        # Keep waits bounded so individual combo pages fail faster.
        "timeout": max(5, min(base_timeout, 20)),
        "max_results": int(job_search.get("max_results_per_platform", 25)),
        # Hard guards so one bad combo/platform cannot block the full run.
        "combo_timeout_seconds": int(automation.get("combo_timeout_seconds", 40)),
        "platform_timeout_seconds": int(
            automation.get("platform_timeout_seconds", 420)
        ),
    }


def _apify_platforms(global_cfg: dict[str, Any]) -> set[str]:
    """Platforms routed through Apify when toggled in ``platforms.apify``."""
    plats = global_cfg.get("platforms", {}) or {}
    apify_cfg = plats.get("apify") or {}
    if not isinstance(apify_cfg, dict):
        return set()
    out: set[str] = set()
    for name, enabled in apify_cfg.items():
        key = str(name).strip().lower()
        if enabled and key in APIFY_PLATFORMS:
            out.add(key)
    return out


def _enabled_platforms(global_cfg: dict[str, Any]) -> list[str]:
    plats = global_cfg.get("platforms", {}) or {}
    apify_enabled = _apify_platforms(global_cfg)
    # Lowercase + dedupe keys so YAML keys like 'Internshala' still match the
    # registry's canonical lowercase identifiers.
    out: list[str] = []
    seen: set[str] = set()
    for name, enabled in plats.items():
        if str(name).strip().lower() == "apify":
            continue
        key = str(name).strip().lower()
        if not enabled or key in seen:
            continue
        if key in apify_enabled or key in SCRAPER_REGISTRY:
            out.append(key)
            seen.add(key)
    return out


def _uses_apify(platform: str, global_cfg: dict[str, Any]) -> bool:
    return platform in _apify_platforms(global_cfg)


def _load_filters(global_cfg: dict[str, Any]) -> dict[str, list[str]]:
    """Read filters.* from config.yaml and lowercase every entry."""
    raw = global_cfg.get("filters") or {}

    def _norm(values: Any) -> list[str]:
        if not values:
            return []
        if not isinstance(values, list):
            values = [values]
        return [str(v).strip().lower() for v in values if str(v).strip()]

    return {
        "exclude_keywords": _norm(raw.get("exclude_keywords")),
        "exclude_companies": _norm(raw.get("exclude_companies")),
        "required_keywords": _norm(raw.get("required_keywords")),
    }


def _job_passes_filters(job: Job, filters: dict[str, list[str]]) -> bool:
    """Apply the config.filters.* rules. Matches are case-insensitive and
    checked against title + description (+ company for exclude_companies)."""
    haystack = " ".join(
        x for x in (job.title, job.description) if x
    ).lower()
    company = (job.company or "").lower()

    for term in filters.get("exclude_keywords", []):
        if term and term in haystack:
            return False
    for term in filters.get("exclude_companies", []):
        if term and term in company:
            return False
    required = filters.get("required_keywords", [])
    if required and not any(term in haystack for term in required if term):
        return False
    return True


def _is_valid_job(job: Job) -> bool:
    """Reject incomplete or obviously invalid postings before persistence."""
    title = (job.title or "").strip()
    company = (job.company or "").strip()
    url = (job.url or "").strip()
    if not title or not company or not url:
        return False
    if len(title) < 3 or len(company) < 2:
        return False
    if not url.startswith(("http://", "https://")):
        return False
    spam_markers = ("lorem ipsum", "click here to earn", "work from phone")
    hay = f"{title} {job.description or ''}".lower()
    if any(marker in hay for marker in spam_markers):
        return False
    return True


@dataclass
class PlatformScrapeStats:
    platform: str
    found: int = 0
    new: int = 0
    filtered: int = 0
    invalid: int = 0
    duplicates: int = 0
    exceptions: int = 0
    error: str | None = None


@dataclass
class ScrapeRunResult:
    total_found: int = 0
    new_jobs: int = 0
    platforms: dict[str, PlatformScrapeStats] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_found": self.total_found,
            "new_jobs": self.new_jobs,
            "platforms": {
                name: {
                    "platform": s.platform,
                    "found": s.found,
                    "new": s.new,
                    "filtered": s.filtered,
                    "invalid": s.invalid,
                    "duplicates": s.duplicates,
                    "exceptions": s.exceptions,
                    "error": s.error,
                }
                for name, s in self.platforms.items()
            },
            "errors": self.errors,
        }


def _query_grid(global_cfg: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(role, location, job_type) tuples to scrape per platform.

    Every entry in ``job_search.job_types`` is used (not only the first).
    """
    js = global_cfg.get("job_search", {}) or {}
    roles = js.get("roles") or [""]
    locations = js.get("locations") or [""]
    job_types = js.get("job_types") or [""]
    if not job_types:
        job_types = [""]
    grid: list[tuple[str, str, str]] = []
    for role in roles:
        for loc in locations:
            for jt in job_types:
                grid.append((role, loc, str(jt)))
    return grid


# ---------------------------------------------------------------------------
# Per-platform run
# ---------------------------------------------------------------------------
def _run_scraper(
    platform: str,
    scraper_cls: Type[BaseScraper],
    scraper_cfg: dict[str, Any],
    grid: list[tuple[str, str, str]],
) -> tuple[list[Job], int, str | None]:
    scraper = scraper_cls(scraper_cfg)
    found: list[Job] = []
    seen: set[str] = set()
    exception_count = 0
    combo_timeout = max(5, int(scraper_cfg.get("combo_timeout_seconds", 40)))
    platform_timeout = max(30, int(scraper_cfg.get("platform_timeout_seconds", 420)))
    platform_started = time.monotonic()
    platform_logger = logging.getLogger(f"manager.{platform}")
    platform_error: str | None = None
    try:
        # Run each combo search in its own thread so we can enforce a true
        # fail-fast timeout even if a scraper call blocks on IO.
        ex = ThreadPoolExecutor(max_workers=1)
        for role, location, job_type in grid:
            elapsed = time.monotonic() - platform_started
            if elapsed >= platform_timeout:
                exception_count += 1
                platform_error = (
                    f"platform timeout after {elapsed:.1f}s "
                    f"(limit {platform_timeout}s)"
                )
                platform_logger.error(
                    "platform timeout reached after %.1fs; stopping remaining combos",
                    elapsed,
                )
                break
            combo_started = time.monotonic()
            try:
                fut = ex.submit(scraper.search, role, location, job_type)
                batch = fut.result(timeout=combo_timeout)
            except TimeoutError:
                exception_count += 1
                platform_error = (
                    f"combo timeout after {combo_timeout}s "
                    f"on search({role!r}, {location!r}, {job_type!r})"
                )
                platform_logger.error("%s; aborting platform run", platform_error)
                break
            except Exception as exc:
                exception_count += 1
                platform_logger.error(
                    "search(%r, %r, %r) failed: %s", role, location, job_type, exc
                )
                continue
            combo_elapsed = time.monotonic() - combo_started
            if combo_elapsed > combo_timeout:
                exception_count += 1
                platform_logger.error(
                    "search(%r, %r, %r) exceeded combo timeout (%.1fs > %ss); "
                    "discarding this combo batch",
                    role,
                    location,
                    job_type,
                    combo_elapsed,
                    combo_timeout,
                )
                continue
            for j in batch:
                if j.url and j.url not in seen:
                    seen.add(j.url)
                    found.append(j)
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            scraper.close()
        except Exception:
            pass
    return found, exception_count, platform_error


def _run_apify_scraper(
    platform: str,
    global_cfg: dict[str, Any],
    scraper_cfg: dict[str, Any],
) -> tuple[list[Job], int, str | None]:
    js = global_cfg.get("job_search", {}) or {}
    roles = [str(r) for r in (js.get("roles") or [""])]
    locations = [str(loc) for loc in (js.get("locations") or [""])]
    max_results = min(
        25,
        int(scraper_cfg.get("max_results", 25)),
    )
    max_combos_raw = js.get("apify_max_combos")
    max_combos: int | None = None
    if max_combos_raw is not None:
        try:
            cap = int(max_combos_raw)
            if cap > 0:
                max_combos = cap
        except (TypeError, ValueError):
            max_combos = None
    platform_logger = logging.getLogger(f"manager.{platform}")
    exception_count = 0
    platform_error: str | None = None
    try:
        jobs = scrape_platform_jobs(
            platform,
            roles,
            locations,
            max_results=max_results,
            max_combos=max_combos,
        )
    except Exception as exc:
        exception_count = 1
        platform_error = str(exc)
        platform_logger.error("[%s] Apify scrape failed: %s", platform, exc)
        jobs = []
    if not jobs and platform_error is None:
        platform_logger.warning("[%s] Apify returned 0 jobs", platform)
    return jobs, exception_count, platform_error


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class ScraperManager:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        db: Database | None = None,
    ) -> None:
        self.config = config if config is not None else load_config()
        self.scraper_cfg = _build_scraper_config(self.config)
        self.filters = _load_filters(self.config)
        self.db = db or Database()
        self.logger = logging.getLogger("scraper_manager")
        self._platform_stats: dict[str, PlatformScrapeStats] = {}

    def run(
        self,
        parallel: bool = False,
        max_workers: int = 3,
        *,
        quiet: bool = False,
    ) -> ScrapeRunResult:
        """Run every enabled scraper, dedupe, persist. Returns scrape statistics."""
        platforms = _enabled_platforms(self.config)
        result = ScrapeRunResult()
        self._platform_stats = {
            plat: PlatformScrapeStats(platform=plat) for plat in platforms
        }

        if not platforms:
            msg = "No platforms enabled in config.yaml -> platforms."
            if not quiet:
                print(msg)
            result.errors.append(msg)
            return result

        grid = _query_grid(self.config)
        if not quiet:
            print(
                f"Enabled platforms ({len(platforms)}): {', '.join(platforms)}\n"
                f"Query grid: {len(grid)} (role, location) pairs per platform\n"
                f"Parallel: {parallel}\n" + "-" * 60
            )

        all_jobs: dict[str, Job] = {}

        if parallel:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures: dict[Any, str] = {}
                for plat in platforms:
                    if _uses_apify(plat, self.config):
                        futures[
                            ex.submit(
                                _run_apify_scraper,
                                plat,
                                self.config,
                                self.scraper_cfg,
                            )
                        ] = plat
                    else:
                        futures[
                            ex.submit(
                                _run_scraper,
                                plat,
                                SCRAPER_REGISTRY[plat],
                                self.scraper_cfg,
                                grid,
                            )
                        ] = plat
                for fut in as_completed(futures):
                    plat = futures[fut]
                    try:
                        jobs, exception_count, platform_error = fut.result()
                    except Exception as exc:
                        err = f"[{plat}] FAILED: {exc}"
                        if not quiet:
                            print(f"  {err}")
                        self.logger.error(err)
                        self._platform_stats[plat].error = str(exc)
                        result.errors.append(err)
                        continue
                    self._platform_stats[plat].exceptions = exception_count
                    if platform_error:
                        self._platform_stats[plat].error = platform_error
                        result.errors.append(f"[{plat}] {platform_error}")
                    self._after_platform(all_jobs, jobs, plat, result, quiet=quiet)
        else:
            for plat in platforms:
                try:
                    if _uses_apify(plat, self.config):
                        jobs, exception_count, platform_error = _run_apify_scraper(
                            plat, self.config, self.scraper_cfg
                        )
                    else:
                        jobs, exception_count, platform_error = _run_scraper(
                            plat, SCRAPER_REGISTRY[plat], self.scraper_cfg, grid
                        )
                except Exception as exc:
                    err = f"[{plat}] FAILED: {exc}"
                    if not quiet:
                        print(f"  {err}")
                    self.logger.error(err)
                    self._platform_stats[plat].error = str(exc)
                    result.errors.append(err)
                    continue
                self._platform_stats[plat].exceptions = exception_count
                if platform_error:
                    self._platform_stats[plat].error = platform_error
                    result.errors.append(f"[{plat}] {platform_error}")
                self._after_platform(all_jobs, jobs, plat, result, quiet=quiet)

        result.total_found = len(all_jobs)
        result.platforms = dict(self._platform_stats)

        if not quiet:
            print("-" * 60)
            print(f"Total unique jobs found across all platforms: {len(all_jobs)}")
            print(f"New jobs added to DB: {result.new_jobs}")
            print("Per-platform summary:")
            print(f"  {'platform':<12} | {'found':>5} | {'new':>5} | {'duplicates':>10} | errors")
            print(f"  {'-' * 12}-+-{'-' * 5}-+-{'-' * 5}-+-{'-' * 10}-+-------")
            for plat in platforms:
                s = self._platform_stats.get(plat) or PlatformScrapeStats(platform=plat)
                err = s.error or "-"
                via = "apify" if _uses_apify(plat, self.config) else "native"
                print(
                    f"  {plat:<12} | {s.found:>5} | {s.new:>5} | "
                    f"{s.duplicates:>10} | {s.exceptions} ({via}) err={err}"
                )
        return result

    def _merge(
        self,
        sink: dict[str, Job],
        jobs: list[Job],
        platform: str,
        *,
        quiet: bool = False,
    ) -> list[Job]:
        stats = self._platform_stats.setdefault(
            platform, PlatformScrapeStats(platform=platform)
        )
        stats.found = len(jobs)
        before = len(sink)
        filtered_out = 0
        invalid_out = 0
        duplicates_out = 0
        added_jobs: list[Job] = []
        for j in jobs:
            if not j.url or j.url in sink:
                duplicates_out += 1
                continue
            if not _is_valid_job(j):
                invalid_out += 1
                continue
            if not _job_passes_filters(j, self.filters):
                filtered_out += 1
                continue
            sink[j.url] = j
            added_jobs.append(j)
        stats.filtered = filtered_out
        stats.invalid = invalid_out
        stats.duplicates = duplicates_out
        added = len(sink) - before
        suffix_parts = []
        if duplicates_out:
            suffix_parts.append(f"{duplicates_out} duplicates")
        if filtered_out:
            suffix_parts.append(f"{filtered_out} dropped by filters")
        if invalid_out:
            suffix_parts.append(f"{invalid_out} invalid/skipped")
        if stats.exceptions:
            suffix_parts.append(f"{stats.exceptions} errors")
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        if not quiet:
            print(
                f"  [{platform:<12}] Found {len(jobs):>3} jobs from "
                f"{platform.title()} ({added} new after global dedup{suffix})"
            )
        return added_jobs

    def _after_platform(
        self,
        sink: dict[str, Job],
        jobs: list[Job],
        platform: str,
        result: ScrapeRunResult,
        *,
        quiet: bool = False,
    ) -> None:
        added = self._merge(sink, jobs, platform, quiet=quiet)
        if added:
            new_count = self._persist(added, quiet=quiet)
            result.new_jobs += new_count
            if not quiet:
                print(f"  [{platform:<12}] Persisted {new_count} new job(s) to DB")

    def _persist(self, jobs, *, quiet: bool = False) -> int:
        new_count = 0
        platform_new: dict[str, int] = {}
        for j in jobs:
            try:
                _job_id, inserted = self.db.insert_job_with_status(j.to_dict())
                if inserted:
                    new_count += 1
                    plat = (j.platform or "unknown").lower()
                    platform_new[plat] = platform_new.get(plat, 0) + 1
            except Exception as exc:
                self.logger.warning("DB insert failed for %s: %s", j.url, exc)
        for plat, count in platform_new.items():
            if plat in self._platform_stats:
                self._platform_stats[plat].new = count
        if not quiet:
            print(f"Persisted to DB: {new_count} new job(s)")
        return new_count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Run all enabled job scrapers.")
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Run scrapers in parallel threads (1 per platform).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Max parallel workers when --parallel is set.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable INFO-level logging from scrapers.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    mgr = ScraperManager()
    result = mgr.run(parallel=args.parallel, max_workers=args.workers)
    print(f"Scrape complete: {result.new_jobs} new / {result.total_found} unique.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
