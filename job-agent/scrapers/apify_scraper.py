"""Apify-backed job scrapers for platforms with broken native scrapers.

Reads ``APIFY_API_TOKEN`` from ``.env`` via python-dotenv. Each platform
function iterates ``roles x locations`` from config and returns normalized
job dicts. Failures on one combo are logged and skipped.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

# Import is delayed to avoid crashing if apify-client isn't installed
from dotenv import load_dotenv

from .base_scraper import Job

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAX_RESULTS = 25

ACTOR_LINKEDIN = "curious_coder/linkedin-jobs-search"
ACTOR_INDEED = "misceres/indeed-scraper"
ACTOR_GLASSDOOR = "bebity/glassdoor-jobs-scraper"
ACTOR_GOOGLE_JOBS = "jupri/google-jobs-scraper"
ACTOR_NAUKRI = "bebity/naukri-scraper"

# Verified via Apify API: the three IDs above return 404; these actors are used on retry.
ACTOR_FALLBACKS: dict[str, str] = {
    ACTOR_LINKEDIN: "curious_coder/linkedin-jobs-scraper",
    ACTOR_GOOGLE_JOBS: "automation-lab/google-jobs-scraper",
    ACTOR_NAUKRI: "automation-lab/naukri-scraper",
}

logger = logging.getLogger("scrapers.apify")


def _load_token() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    token = (os.getenv("APIFY_API_TOKEN") or "").strip()
    if not token or token == "your_token_here":
        raise RuntimeError(
            "APIFY_API_TOKEN is missing or unset in .env — add your Apify token."
        )
    return token


def _client() -> Any:
    try:
        from apify_client import ApifyClient
    except ImportError:
        raise RuntimeError("apify-client is not installed. run: pip install apify-client")
    return ApifyClient(_load_token())


def _first_str(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = item.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            text = val.strip()
            if text:
                return text
        elif isinstance(val, (int, float)):
            return str(val)
        elif isinstance(val, list):
            joined = ", ".join(str(v).strip() for v in val if str(v).strip())
            if joined:
                return joined
    return ""


def _normalize_item(item: dict[str, Any], platform: str) -> dict[str, str]:
    title = _first_str(
        item,
        "title",
        "positionName",
        "jobTitle",
        "job_title",
        "name",
        "headline",
    )
    company = _first_str(
        item,
        "company",
        "companyName",
        "company_name",
        "employer",
        "hiringOrganization",
    )
    location = _first_str(
        item,
        "location",
        "jobLocation",
        "formattedLocation",
        "city",
        "place",
    )
    url = _first_str(
        item,
        "url",
        "link",
        "jobUrl",
        "job_url",
        "applyUrl",
        "apply_url",
        "externalApplyLink",
        "jdURL",
    )
    if url and url.startswith("/"):
        if platform == "naukri":
            url = f"https://www.naukri.com{url}"
        elif platform == "indeed" and not url.startswith("//"):
            url = f"https://www.indeed.com{url}"

    description = _first_str(
        item,
        "description",
        "jobDescription",
        "snippet",
        "summary",
        "job_summary",
    )

    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "description": description,
        "platform": platform,
    }


def _to_jobs(raw: list[dict[str, Any]], platform: str) -> list[Job]:
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        norm = _normalize_item(item, platform)
        url = norm["url"]
        if not url or url in seen:
            continue
        seen.add(url)
        jobs.append(
            Job(
                title=norm["title"],
                company=norm["company"],
                location=norm["location"],
                job_type="",
                platform=platform,
                url=url,
                description=norm["description"],
            )
        )
    return jobs


def _dataset_id(run: Any) -> str | None:
    if isinstance(run, dict):
        return run.get("defaultDatasetId") or run.get("default_dataset_id")
    for attr in ("default_dataset_id", "defaultDatasetId"):
        val = getattr(run, attr, None)
        if val:
            return str(val)
    return None


def _is_actor_not_found(exc: Exception) -> bool:
    text = str(exc).lower()
    return "not found" in text or "record-not-found" in text


def _run_actor(
    actor_id: str,
    run_input: dict[str, Any],
    *,
    platform: str,
    role: str,
    location: str,
) -> list[dict[str, Any]]:
    """Call an Apify actor and return raw dataset items."""
    client = _client()
    actor_ids = [actor_id]
    fallback = ACTOR_FALLBACKS.get(actor_id)
    if fallback:
        actor_ids.append(fallback)

    run = None
    used_actor = actor_id
    last_exc: Exception | None = None
    for candidate in actor_ids:
        logger.info(
            "[%s] Apify actor %s for role=%r location=%r",
            platform,
            candidate,
            role,
            location,
        )
        try:
            run = client.actor(candidate).call(run_input=run_input)
            used_actor = candidate
            if candidate != actor_id:
                logger.warning(
                    "[%s] Primary actor %s unavailable; used fallback %s",
                    platform,
                    actor_id,
                    candidate,
                )
            break
        except Exception as exc:
            last_exc = exc
            if candidate != actor_ids[-1] and _is_actor_not_found(exc):
                logger.warning(
                    "[%s] Apify actor %s not found (%s); trying fallback",
                    platform,
                    candidate,
                    exc,
                )
                continue
            logger.error(
                "[%s] Apify actor %s failed for %r / %r: %s",
                platform,
                candidate,
                role,
                location,
                exc,
            )
            return []

    if run is None:
        if last_exc is not None:
            logger.error(
                "[%s] All Apify actors failed for %r / %r: %s",
                platform,
                role,
                location,
                last_exc,
            )
        return []

    dataset_id = _dataset_id(run)
    if not dataset_id:
        logger.warning(
            "[%s] Apify run for %r / %r returned no dataset",
            platform,
            role,
            location,
        )
        return []

    items = list(client.dataset(dataset_id).iterate_items())
    if not items:
        logger.warning(
            "[%s] 0 results from Apify for role=%r location=%r",
            platform,
            role,
            location,
        )
    else:
        logger.info(
            "[%s] Apify returned %d item(s) for role=%r location=%r",
            platform,
            len(items),
            role,
            location,
        )
    return items


def _linkedin_search_url(role: str, location: str) -> str:
    q = quote_plus(role)
    url = f"https://www.linkedin.com/jobs/search/?keywords={q}"
    if location:
        url += f"&location={quote_plus(location)}"
    return url


def _indeed_country(location: str) -> str:
    loc = (location or "").lower()
    if loc in {"india", "bengaluru", "bangalore", "hyderabad", "mumbai", "delhi"}:
        return "IN"
    return "IN"


def _google_country(location: str) -> str:
    loc = (location or "").lower()
    if loc in {"remote", ""}:
        return "in"
    if "india" in loc or loc in {"bengaluru", "bangalore", "hyderabad"}:
        return "in"
    return "in"


def _scrape_platform(
    platform: str,
    actor_id: str,
    roles: list[str],
    locations: list[str],
    *,
    build_input,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_combos: int | None = None,
) -> list[dict[str, str]]:
    """Generic role x location scraper returning normalized dicts."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    combo_count = 0
    for role in roles:
        for location in locations:
            if max_combos is not None and combo_count >= max_combos:
                return out
            combo_count += 1
            try:
                run_input = build_input(role, location, max_results)
                items = _run_actor(
                    actor_id,
                    run_input,
                    platform=platform,
                    role=role,
                    location=location,
                )
            except Exception as exc:
                logger.error(
                    "[%s] scrape failed for %r / %r: %s",
                    platform,
                    role,
                    location,
                    exc,
                )
                continue
            for item in items:
                norm = _normalize_item(item, platform)
                url = norm.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                out.append(norm)
    return out


def scrape_linkedin(
    roles: list[str],
    locations: list[str],
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_combos: int | None = None,
) -> list[dict[str, str]]:
    """Scrape LinkedIn Jobs via Apify."""

    def build_input(role: str, location: str, limit: int) -> dict[str, Any]:
        search_url = _linkedin_search_url(role, location)
        return {
            "searchUrl": search_url,
            "urls": [search_url],
            "count": limit,
            "scrapeJobDetails": False,
            "scrapeCompany": False,
        }

    return _scrape_platform(
        "linkedin",
        ACTOR_LINKEDIN,
        roles,
        locations,
        build_input=build_input,
        max_results=max_results,
        max_combos=max_combos,
    )


def scrape_indeed(
    roles: list[str],
    locations: list[str],
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_combos: int | None = None,
) -> list[dict[str, str]]:
    """Scrape Indeed via Apify."""

    def build_input(role: str, location: str, limit: int) -> dict[str, Any]:
        return {
            "position": role,
            "location": location,
            "country": _indeed_country(location),
            "maxItemsPerSearch": limit,
            "saveOnlyUniqueItems": True,
            "parseCompanyDetails": False,
        }

    return _scrape_platform(
        "indeed",
        ACTOR_INDEED,
        roles,
        locations,
        build_input=build_input,
        max_results=max_results,
        max_combos=max_combos,
    )


def scrape_glassdoor(
    roles: list[str],
    locations: list[str],
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_combos: int | None = None,
) -> list[dict[str, str]]:
    """Scrape Glassdoor via Apify."""

    def build_input(role: str, location: str, limit: int) -> dict[str, Any]:
        base_url = "https://www.glassdoor.co.in"
        if location and location.lower() not in {"india", "remote"}:
            base_url = "https://www.glassdoor.co.in"
        return {
            "keyword": role,
            "location": location,
            "maxItems": limit,
            "baseUrl": base_url,
            "includeNoSalaryJob": True,
            "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
        }

    return _scrape_platform(
        "glassdoor",
        ACTOR_GLASSDOOR,
        roles,
        locations,
        build_input=build_input,
        max_results=max_results,
        max_combos=max_combos,
    )


def scrape_google_jobs(
    roles: list[str],
    locations: list[str],
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_combos: int | None = None,
) -> list[dict[str, str]]:
    """Scrape Google Jobs via Apify."""

    def build_input(role: str, location: str, limit: int) -> dict[str, Any]:
        query = role if not location else f"{role} {location}"
        return {
            "queries": [query],
            "query": query,
            "location": location,
            "maxResults": limit,
            "maxItems": limit,
            "country": _google_country(location),
            "language": "en",
            "includeDetails": True,
        }

    return _scrape_platform(
        "google_jobs",
        ACTOR_GOOGLE_JOBS,
        roles,
        locations,
        build_input=build_input,
        max_results=max_results,
        max_combos=max_combos,
    )


def scrape_naukri(
    roles: list[str],
    locations: list[str],
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_combos: int | None = None,
) -> list[dict[str, str]]:
    """Scrape Naukri via Apify."""

    def build_input(role: str, location: str, limit: int) -> dict[str, Any]:
        loc = location
        if loc and loc.lower() == "india":
            loc = ""
        return {
            "keyword": role,
            "location": loc,
            "maxJobs": limit,
            "maxItems": limit,
            "fetchDetails": True,
            "proxyConfiguration": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
            },
        }

    return _scrape_platform(
        "naukri",
        ACTOR_NAUKRI,
        roles,
        locations,
        build_input=build_input,
        max_results=max_results,
        max_combos=max_combos,
    )


def scrape_platform_jobs(
    platform: str,
    roles: list[str],
    locations: list[str],
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_combos: int | None = None,
) -> list[Job]:
    """Dispatch to the correct Apify scraper and return ``Job`` objects."""
    dispatch = {
        "linkedin": scrape_linkedin,
        "indeed": scrape_indeed,
        "glassdoor": scrape_glassdoor,
        "google_jobs": scrape_google_jobs,
        "naukri": scrape_naukri,
    }
    fn = dispatch.get(platform)
    if fn is None:
        raise ValueError(f"No Apify scraper registered for platform: {platform}")
    raw = fn(roles, locations, max_results=max_results, max_combos=max_combos)
    return _to_jobs(raw, platform)
