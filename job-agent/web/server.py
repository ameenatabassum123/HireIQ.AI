"""FastAPI web server for the AI Job Agent.

Serves HTML pages and JSON API endpoints that wrap the existing
db / ai modules without reimplementing scrapers or matchers.

Run via:
    python main.py --mode web
    uvicorn web.server:app --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import copy
import threading
import re
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ai.ats_scorer import compute_ats_score, format_ats_report
from ai.cover_letter import generate_cover_letter
from ai.jd_analyzer import analyze_jd, infer_seniority_level
from ai.match_explainer import get_or_create_match_explanation
from ai.matcher import compute_match
from ai.profile_store import (
    delete_profile,
    has_resume_pdf,
    import_disk_resume_to_db,
    import_user_legacy_resumes,
    list_profiles,
    load_profile,
    materialize_resume_pdf,
    profile_exists_in_db,
    profile_path,
    profile_skill_summary,
    resolve_active_slug,
    resolve_resume_pdf,
    save_profile,
    scoped_slug,
    slugify,
)
from ai.gemini_client import format_gemini_error, web_parse_max_retries
from ai.resume_customizer import (
    customize_resume,
    propose_resume_customization,
    propose_resume_customization_local,
)
from ai.resume_parser import is_local_fallback_profile, parse_resume
from db.database import Database
from scrapers.scheduler import JobScrapeScheduler, _parse_auto_scrape_config
from web.auth import (
    AuthMiddleware,
    get_current_user,
    get_session_secret,
    hash_password,
    login_user,
    logout_user,
    normalize_email,
    validate_email,
    validate_password,
    verify_password,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"

load_dotenv(PROJECT_ROOT / ".env")


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    """Start background job scraper on web app startup; stop on shutdown."""
    config = load_config()
    scheduler = JobScrapeScheduler.get_instance(config=config)
    # scheduler.start()
    app.state.scheduler = scheduler
    yield
    # scheduler.stop()


app = FastAPI(
    title="AI Job Agent",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=_app_lifespan,
)

# Custom HTTP middleware for security headers
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "frame-ancestors 'none';"
    )
    return response


app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    session_cookie="session",
    same_site="lax",
    https_only=False,  # Set to True if deploying behind HTTPS
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
(PROJECT_ROOT / "output").mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=str(PROJECT_ROOT / "output")), name="output")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# In-memory cache for resume-review diffs: (job_id, slug) -> payload
_review_cache: dict[tuple[int, str], dict[str, Any]] = {}

# In-memory search cache: (user_id, role, location) -> last_scraped_datetime
_search_cache: dict[tuple[int | None, str, str], datetime] = {}


def _infer_related_roles(role: str) -> list[str]:
    role_lower = role.lower().strip()
    related = _JOB_SEARCH_EXPANSIONS.get(role_lower, [])
    if not related:
        for k, v in _JOB_SEARCH_EXPANSIONS.items():
            if k in role_lower:
                related.extend(v)
    out = [role.strip()]
    for r in related:
        if r.strip().lower() not in [o.strip().lower() for o in out]:
            out.append(r.strip())
    return out


def _run_bg_scrape(
    user_id: int | None,
    search: str,
    location: str,
    active_slug: str | None,
) -> None:
    from scrapers.scraper_manager import ScraperManager
    
    related_roles = _infer_related_roles(search)
    print(f"[bg-scrape] Starting background scrape for search term '{search}' (expanded to: {related_roles}) location: '{location}'")
    scheduler = JobScrapeScheduler.get_instance(config=load_config())
    scheduler._is_running = True
    
    try:
        temp_cfg = copy.deepcopy(load_config())
        if "job_search" in temp_cfg:
            temp_cfg["job_search"]["roles"] = related_roles
            temp_cfg["job_search"]["locations"] = [location] if location else ["Remote", "India"]
            if "auto_scrape" in temp_cfg["job_search"]:
                temp_cfg["job_search"]["auto_scrape"]["enabled"] = False
        
        mgr = ScraperManager(config=temp_cfg, db=_db())
        print(f"[bg-scrape] Invoking ScraperManager.run...")
        mgr.run(parallel=True, max_workers=3, quiet=True)
        print(f"[bg-scrape] ScraperManager.run complete. Scoring new jobs...")
        
        profile = load_profile(
            slug=active_slug,
            config=temp_cfg,
            user_id=user_id,
            db=_db() if user_id else None,
        )
        if _profile_has_skills(profile):
            from automation.apply_agent import ApplyAgent
            agent = ApplyAgent(profile_slug=active_slug)
            agent._score_new_jobs()
            print(f"[bg-scrape] Match scoring complete for new jobs.")
            
        print(f"[bg-scrape] Background scrape successfully completed.")
    except Exception as exc:
        print(f"[bg-scrape] error: {exc}")
    finally:
        scheduler._is_running = False


JOBS_PAGE_SIZE = 50
JOB_PICKER_LIMIT = 100
JOB_LIST_DESC_MAX = 300

# Common UI search phrases → related job-title keywords (case-insensitive).
_JOB_SEARCH_EXPANSIONS: dict[str, list[str]] = {
    "data analysis": [
        "data analyst",
        "data analytics",
        "analytics",
        "business analyst",
        "data scientist",
    ],
    "data analyst": ["data analysis", "data analytics", "analytics", "business analyst"],
    "data analytics": ["data analyst", "data analysis", "analytics"],
    "machine learning": ["ml engineer", "machine learning engineer", "ai engineer"],
    "ai engineer": ["artificial intelligence", "machine learning engineer", "ml engineer"],
}


def _optional_int(value: str | int | None) -> int | None:
    """Parse query/form values; treat blank strings as unset."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_job_search(search: str) -> list[str] | None:
    """Expand a user keyword query into OR-matched search terms."""
    q = search.strip()
    if not q:
        return None
    terms: list[str] = [q]
    lower = q.lower()
    for extra in _JOB_SEARCH_EXPANSIONS.get(lower, ()):
        if extra not in terms:
            terms.append(extra)
    if " " in lower:
        for word in re.split(r"\s+", lower):
            if len(word) >= 3 and word not in terms:
                terms.append(word)
    return terms


# ---------------------------------------------------------------------------
# Config / helpers
# ---------------------------------------------------------------------------
def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _db() -> Database:
    return Database()


def _active_slug(
    config: dict[str, Any] | None = None,
    request: Request | None = None,
) -> str | None:
    user_id = None
    if request is not None:
        user = get_current_user(request)
        if user:
            user_id = int(user["id"])
    return resolve_active_slug(config or load_config(), user_id=user_id, db=_db())


def _profile_has_skills(profile: dict[str, Any]) -> bool:
    return any(
        profile.get(k)
        for k in ("skills", "tools", "programming_languages", "frameworks", "experience")
    )


def _resolve_effective_slug(
    config: dict[str, Any],
    request: Request | None,
    override: str = "",
) -> str | None:
    """Explicit picker > active profile > first parsed profile > None (legacy file)."""
    raw = (override or "").strip()
    if raw:
        return slugify(raw)
    active = _active_slug(config, request)
    if active:
        return active
    user_id = _user_id(request)
    db = _db()
    for slug in list_profiles(user_id, db=db if user_id else None):
        prof = load_profile(
            slug=slug,
            config=config,
            user_id=user_id,
            db=db if user_id else None,
        )
        if _profile_has_skills(prof):
            return slug
    return None


def _fallback_jd_analysis(jd_text: str) -> dict[str, Any]:
    """Basic keyword list from raw JD text when AI analysis is unavailable."""
    tokens = re.findall(r"[a-zA-Z+#./][a-zA-Z0-9+#./-]*", jd_text.lower())
    seen: list[str] = []
    stop = {
        "and", "the", "for", "with", "job", "role", "position", "work", "team",
        "our", "you", "your", "will", "are", "has", "have", "this", "that",
    }
    for token in tokens:
        if len(token) < 3 or token in stop:
            continue
        display = token.title() if token.isalpha() else token.upper()
        if display not in seen:
            seen.append(display)
    return {
        "required_skills": seen[:12],
        "preferred_skills": seen[12:20],
        "tools": [],
        "keywords": seen,
        "experience_required": "",
        "seniority_level": "",
    }


def _jd_analysis_for_matching(
    db: Database,
    job: dict[str, Any],
    analyses_cache: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Cached JD analysis or fast local keyword extract — never calls Gemini."""
    job_id = int(job["id"])
    analysis: dict[str, Any] | None = None
    if analyses_cache is not None:
        analysis = analyses_cache.get(job_id)
    if analysis is None:
        analysis = db.get_jd_analysis(job_id)
    if analysis:
        return analysis

    jd_text = (job.get("description") or job.get("title") or "").strip()
    if not jd_text:
        return None
    return _fallback_jd_analysis(jd_text)


def _batch_ensure_match_scores(
    db: Database,
    jobs: list[dict[str, Any]],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compute local match scores for jobs missing scores; persist to DB."""
    if not jobs or not _profile_has_skills(profile):
        return jobs

    job_ids = [int(j["id"]) for j in jobs]
    analyses = db.get_jd_analyses_for_jobs(job_ids)
    pending: dict[int, int] = {}

    for job in jobs:
        job_id = int(job["id"])
        if job.get("match_score") is not None:
            continue

        analysis = _jd_analysis_for_matching(db, job, analyses)
        if not analysis:
            score = 0
        else:
            try:
                result = compute_match(profile, analysis)
                score = int(result.get("match_score") or 0)
            except Exception:
                score = 0

        job["match_score"] = score
        pending[job_id] = score

    if pending:
        db.update_match_scores_batch(pending)

    return jobs


def _job_list_payload(job: dict[str, Any]) -> dict[str, Any]:
    """Trim heavy fields for list/API responses."""
    row = dict(job)
    desc = row.get("description") or ""
    if isinstance(desc, str) and len(desc) > JOB_LIST_DESC_MAX:
        row["description"] = desc[:JOB_LIST_DESC_MAX]
    return row


def _compute_ats_for_job(
    db: Database,
    job: dict[str, Any],
    profile: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (ats_result, error_message)."""
    if not _profile_has_skills(profile):
        return None, "Upload and parse a resume before calculating ATS scores."
    jd_text = (job.get("description") or job.get("title") or "").strip()
    if not jd_text:
        return None, "This job has no description or title — ATS scoring needs job text."
    job_id = int(job["id"])
    analysis = db.get_jd_analysis(job_id)
    used_fallback = False
    desc = (job.get("description") or "").strip()
    if not analysis and (not desc or len(desc) < 80):
        analysis = _fallback_jd_analysis(jd_text)
        used_fallback = True
    elif not analysis:
        analysis = _jd_analysis_for_matching(db, job)
        used_fallback = bool(analysis)
    if not analysis:
        return None, "Could not analyze this job description."
    try:
        result = compute_ats_score(profile, analysis)
        if used_fallback:
            result = dict(result)
            result["recommendation"] = (
                f"{result.get('recommendation', 'Match')} (basic title/text match — "
                "full JD analysis unavailable)"
            )
        return result, None
    except Exception as exc:
        return None, f"ATS scoring failed: {exc}"


def _user_id(request: Request | None) -> int | None:
    if request is None:
        return None
    user = get_current_user(request)
    return int(user["id"]) if user else None


def _ensure_legacy_resumes_imported(user_id: int | None) -> None:
    """Best-effort import of on-disk resumes into the database for a logged-in user."""
    if user_id is None:
        return
    try:
        import_user_legacy_resumes(user_id, _db())
    except Exception:
        pass


def _profile_json_exists(slug: str, user_id: int | None = None) -> bool:
    if user_id is not None:
        row = _db().get_resume(user_id, slug)
        if row and row.get("profile"):
            return True
    return profile_path(slug).exists()


def _profile_parse_status(
    slug: str,
    profile: dict[str, Any],
    *,
    has_pdf: bool,
    user_id: int | None = None,
) -> str:
    """One of: parsed, partial, failed, pdf_only, empty."""
    if user_id is not None:
        row = _db().get_resume(user_id, slug)
        if row and row.get("parse_status"):
            db_status = str(row["parse_status"])
            if db_status in ("parsed", "partial", "failed", "pdf_only"):
                if db_status == "pdf_only" and _profile_has_skills(profile):
                    return "parsed" if not is_local_fallback_profile(profile) else "partial"
                return db_status

    json_exists = _profile_json_exists(slug, user_id=user_id)
    if _profile_has_skills(profile):
        if is_local_fallback_profile(profile):
            return "partial"
        return "parsed"
    if json_exists:
        return "failed"
    if has_pdf:
        return "pdf_only"
    return "empty"


def _parse_status_label(status: str) -> str:
    return {
        "parsed": "Parsed",
        "partial": "Partially parsed",
        "failed": "Parse failed",
        "pdf_only": "PDF only — parse pending",
        "empty": "Empty",
    }.get(status, status)


def _run_parse_for_slug(
    slug: str,
    *,
    user_id: int | None,
    allow_local_fallback: bool = True,
) -> tuple[dict[str, Any], str | None, str | None]:
    """Parse existing PDF for slug. Returns (profile, message, error)."""
    db = _db()
    if user_id:
        import_disk_resume_to_db(user_id, slug, db)
    pdf = materialize_resume_pdf(slug, user_id=user_id, db=db)
    if not pdf or not pdf.exists():
        raise HTTPException(status_code=404, detail="Resume PDF not found")

    if user_id:
        out_path = Path(tempfile.gettempdir()) / f"job_agent_profile_{user_id}_{slug}.json"
    else:
        out_path = profile_path(slug)
        out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        profile = parse_resume(
            pdf,
            output_path=out_path,
            max_retries=web_parse_max_retries(),
            allow_local_fallback=allow_local_fallback,
            slug=slug,
        )
    except Exception as exc:
        if user_id:
            db.save_resume(user_id, slug, parse_status="failed")
        return {}, None, format_gemini_error(exc)

    parse_status = "partial" if is_local_fallback_profile(profile) else "parsed"
    if user_id:
        save_profile(
            slug,
            profile,
            user_id=user_id,
            db=db,
            parse_status=parse_status,
        )
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        msg = "Resume parsed and saved to your account."
    else:
        msg = f"Parsed profile saved to {out_path.relative_to(PROJECT_ROOT)}."

    if is_local_fallback_profile(profile):
        msg = (
            "Gemini parse unavailable — saved a basic local extract from PDF text. "
            "Use Re-parse with AI when quota resets for full parsing."
        )
    if user_id:
        db.set_user_active_profile(user_id, slug)
    return profile, msg, None


def _ensure_jd_analysis(db: Database, job: dict[str, Any]) -> dict[str, Any] | None:
    """Return JD analysis for a job, analyzing on demand if missing."""
    job_id = int(job["id"])
    analysis = db.get_jd_analysis(job_id)
    if analysis:
        return analysis

    jd_text = (job.get("description") or job.get("title") or "").strip()
    if not jd_text:
        return None

    try:
        analysis = analyze_jd(jd_text)
        analysis["seniority_level"] = infer_seniority_level(
            jd_text,
            job_title=job.get("title") or "",
            experience_required=analysis.get("experience_required") or "",
        )
        db.insert_jd_analysis(job_id, analysis)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"JD analysis failed: {exc}") from exc
    return analysis


def _score_job_local(
    db: Database,
    job: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Fast local match scoring — no Gemini."""
    job_id = int(job["id"])
    analysis = _jd_analysis_for_matching(db, job)
    if not analysis:
        return {}

    try:
        result = compute_match(profile, analysis)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Match scoring failed: {exc}") from exc

    score = int(result.get("match_score") or 0)
    if job.get("match_score") != score:
        db.update_match_score(job_id, score)
        job["match_score"] = score

    return result


def _score_job_if_needed(
    db: Database,
    job: dict[str, Any],
    profile: dict[str, Any],
    slug: str | None,
    *,
    explain: bool = False,
) -> dict[str, Any]:
    """Ensure job has match_score; return match result dict."""
    result = _score_job_local(db, job, profile)
    if not result or not explain:
        return result

    job_id = int(job["id"])
    analysis = _jd_analysis_for_matching(db, job)
    if not analysis:
        return result

    try:
        get_or_create_match_explanation(
            db,
            job_id=job_id,
            profile_slug=slug,
            job_title=job.get("title") or "",
            match_result=result,
            jd_analysis=analysis,
        )
    except Exception:
        pass

    return result


def _run_resume_propose(
    db: Database,
    job: dict[str, Any],
    profile: dict[str, Any],
    slug: str | None,
) -> tuple[list[dict[str, str]], str | None]:
    """Generate resume diffs; returns (diffs, optional_warning)."""
    analysis = _jd_analysis_for_matching(db, job)
    if not analysis:
        raise ValueError("No job description to analyze.")

    _score_job_local(db, job, profile)
    warning: str | None = None
    try:
        customized, diffs = propose_resume_customization(
            profile,
            analysis,
            allow_local_fallback=False,
        )
    except Exception as exc:
        customized, diffs = propose_resume_customization_local(profile, analysis)
        if not diffs:
            raise exc
        warning = (
            "Gemini unavailable — showing basic keyword-based suggestions. "
            "Retry later for full AI tailoring."
        )

    cache_key = (int(job["id"]), slug or "")
    _review_cache[cache_key] = {
        "original": copy.deepcopy(profile),
        "customized": customized,
        "diffs": diffs,
        "jd_analysis": analysis,
        "job": job,
    }
    return diffs, warning


def _filter_jobs(
    db: Database,
    *,
    search: str = "",
    location: str = "",
    min_score: int | None = None,
    platform: str = "",
    page: int = 1,
    page_size: int = JOBS_PAGE_SIZE,
) -> tuple[list[dict[str, Any]], int]:
    keyword_terms = _parse_job_search(search)
    locations = [location.strip()] if location.strip() else None
    filters = dict(
        min_score=min_score,
        locations=locations,
        keyword_terms=keyword_terms,
        platform=platform.strip() or None,
        sort_by_match=True,
    )
    total = db.count_jobs_filtered(**filters)
    page = max(1, page)
    offset = (page - 1) * page_size
    jobs = db.get_jobs_filtered(**filters, limit=page_size, offset=offset)
    return jobs, total


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _scrape_status_payload() -> dict[str, Any]:
    scheduler = JobScrapeScheduler.get_instance(config=load_config())
    return scheduler.get_status()


def _enrich_jobs_for_api(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``is_new`` flag based on scrape time and configured roles/locations."""
    config = load_config()
    auto_cfg = _parse_auto_scrape_config(config)
    badge_mins = auto_cfg["new_badge_minutes"]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=badge_mins)

    js = config.get("job_search") or {}
    roles = [str(r).strip().lower() for r in (js.get("roles") or []) if str(r).strip()]
    locations = [
        str(loc).strip().lower() for loc in (js.get("locations") or []) if str(loc).strip()
    ]

    enriched: list[dict[str, Any]] = []
    for job in jobs:
        row = dict(job)
        scraped_at = _parse_iso_datetime(row.get("date_scraped"))
        row["is_new"] = bool(scraped_at and scraped_at >= cutoff)

        title_loc = f"{row.get('title') or ''} {row.get('location') or ''}".lower()
        role_match = any(r in (row.get("title") or "").lower() for r in roles) if roles else True
        loc_match = any(loc in title_loc for loc in locations) if locations else True
        row["matches_search_criteria"] = role_match and loc_match
        enriched.append(row)
    return enriched


def _job_picker_list(db: Database) -> list[dict[str, Any]]:
    """Recent/top jobs for dropdowns (resume review, cover letter, ATS)."""
    jobs = db.get_jobs_filtered(sort_by_match=True, limit=JOB_PICKER_LIMIT)
    if jobs:
        return jobs
    all_jobs = db.get_jobs()
    return all_jobs[:JOB_PICKER_LIMIT] if all_jobs else []


def _prepare_jobs_for_list(
    db: Database,
    jobs: list[dict[str, Any]],
    request: Request | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Score missing matches locally, enrich metadata, trim payloads."""
    config = load_config()
    slug = _active_slug(config, request)
    profile = _load_user_profile(slug=slug, config=config, request=request)
    has_profile = _profile_has_skills(profile)
    if has_profile:
        jobs = _batch_ensure_match_scores(db, jobs, profile)
    enriched = _enrich_jobs_for_api(jobs)
    return [_job_list_payload(j) for j in enriched], has_profile


def _ctx(request: Request, active_page: str, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "active_page": active_page,
        "current_user": get_current_user(request),
        **extra,
    }


def _safe_next_url(next_url: str | None) -> str:
    """Allow only same-site relative redirects after login."""
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/home"
    return next_url


def _auth_ctx(request: Request, active_page: str, **extra: Any) -> dict[str, Any]:
    """Template context for public auth pages (no redirect loop)."""
    return {"request": request, "active_page": active_page, "current_user": None, **extra}


def _load_user_profile(
    slug: str | None,
    config: dict[str, Any],
    request: Request | None,
) -> dict[str, Any]:
    user_id = _user_id(request)
    return load_profile(
        slug=slug,
        config=config,
        user_id=user_id,
        db=_db() if user_id else None,
    )


def _profile_rows(
    config: dict[str, Any],
    request: Request | None = None,
) -> list[dict[str, Any]]:
    user_id = _user_id(request)
    db = _db()
    if user_id:
        _ensure_legacy_resumes_imported(user_id)
    active = _active_slug(config, request)
    rows: list[dict[str, Any]] = []
    for slug in list_profiles(user_id, db=db):
        prof = load_profile(slug=slug, config=config, user_id=user_id, db=db)
        has_pdf = has_resume_pdf(slug, user_id=user_id, db=db)
        parse_status = _profile_parse_status(
            slug, prof, has_pdf=has_pdf, user_id=user_id
        )
        display = prof.get("name") or slug.replace("_", " ").title()
        rows.append({
            "slug": slug,
            "name": prof.get("name") or "",
            "display_name": display,
            "has_pdf": has_pdf,
            "active": slug == active,
            "skills": profile_skill_summary(prof),
            "has_parsed": parse_status in ("parsed", "partial"),
            "parse_status": parse_status,
            "parse_status_label": _parse_status_label(parse_status),
            "summary": (prof.get("summary") or "")[:280],
        })
    return rows


def _profile_detail(
    config: dict[str, Any],
    slug: str | None,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    if not slug:
        return None
    db = _db()
    if user_id:
        _ensure_legacy_resumes_imported(user_id)
    prof = load_profile(slug=slug, config=config, user_id=user_id, db=db)
    has_pdf = has_resume_pdf(slug, user_id=user_id, db=db)
    parse_status = _profile_parse_status(
        slug, prof, has_pdf=has_pdf, user_id=user_id
    )
    has_parsed = parse_status in ("parsed", "partial")
    json_exists = _profile_json_exists(slug, user_id=user_id)
    display_name = prof.get("name") or slug
    if not json_exists and not has_parsed:
        display_name = slug.replace("_", " ").title()
    return {
        "slug": slug,
        "profile": prof,
        "display_name": display_name,
        "show_contact": has_parsed or json_exists,
        "has_pdf": has_pdf,
        "has_parsed": has_parsed,
        "parse_status": parse_status,
        "parse_status_label": _parse_status_label(parse_status),
        "is_local_fallback": is_local_fallback_profile(prof),
        "pdf_url": f"/api/resume-pdf/{slug}" if has_pdf else None,
        "skills": profile_skill_summary(prof, limit=20),
        "summary": prof.get("summary") or "",
    }


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def page_login_get(
    request: Request,
    next: str = "",
    registered: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        _auth_ctx(
            request,
            "login",
            next=next,
            message="Account created. Please sign in." if registered else None,
        ),
    )


@app.post("/login", response_class=HTMLResponse)
async def page_login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
) -> HTMLResponse:
    clean_email = normalize_email(email)
    db = _db()
    user = db.get_user_by_email(clean_email)

    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request,
            "login.html",
            _auth_ctx(
                request,
                "login",
                email=email,
                next=next,
                error="Invalid email or password.",
            ),
            status_code=401,
        )

    login_user(request, user)
    return RedirectResponse(url=_safe_next_url(next), status_code=303)


@app.get("/signup", response_class=HTMLResponse)
async def page_signup_get(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "signup.html",
        _auth_ctx(request, "signup"),
    )


@app.get("/register", response_class=HTMLResponse)
async def page_register_get() -> RedirectResponse:
    return RedirectResponse(url="/signup", status_code=303)


@app.post("/signup", response_class=HTMLResponse)
async def page_signup_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    name: str = Form(""),
) -> HTMLResponse:
    clean_email = normalize_email(email)

    if not validate_email(clean_email):
        return templates.TemplateResponse(
            request,
            "signup.html",
            _auth_ctx(
                request,
                "signup",
                email=email,
                name=name,
                error="Enter a valid email address.",
            ),
            status_code=400,
        )

    pwd_error = validate_password(password)
    if pwd_error:
        return templates.TemplateResponse(
            request,
            "signup.html",
            _auth_ctx(
                request,
                "signup",
                email=email,
                name=name,
                error=pwd_error,
            ),
            status_code=400,
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "signup.html",
            _auth_ctx(
                request,
                "signup",
                email=email,
                name=name,
                error="Passwords do not match.",
            ),
            status_code=400,
        )

    db = _db()
    if db.get_user_by_email(clean_email):
        return templates.TemplateResponse(
            request,
            "signup.html",
            _auth_ctx(
                request,
                "signup",
                email=email,
                name=name,
                error="An account with this email already exists.",
            ),
            status_code=409,
        )

    try:
        user_id = db.create_user(
            clean_email,
            hash_password(password),
            name=name.strip() or None,
        )
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(
            request,
            "signup.html",
            _auth_ctx(
                request,
                "signup",
                email=email,
                name=name,
                error="An account with this email already exists.",
            ),
            status_code=409,
        )

    user = db.get_user_by_id(user_id)
    if user:
        login_user(request, user)

    return RedirectResponse(url="/home", status_code=303)


@app.post("/register", response_class=HTMLResponse)
async def page_register_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    name: str = Form(""),
) -> HTMLResponse:
    return await page_signup_post(request, email, password, password_confirm, name)


@app.post("/logout")
async def page_logout(request: Request) -> RedirectResponse:
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def page_home(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user:
        return templates.TemplateResponse(request, "intro.html", _ctx(request, "intro"))
    return templates.TemplateResponse(request, "intro.html", _auth_ctx(request, "intro"))


@app.get("/home", response_class=HTMLResponse)
async def page_home_logged_in(request: Request) -> HTMLResponse:
    db = _db()
    stats = db.get_stats()
    return templates.TemplateResponse(
        request,
        "home_logged_in.html",
        _ctx(
            request,
            "home",
            stats={
                "total_jobs": stats.get("total_jobs", 0),
                "applications": stats.get("total_applied", 0),
            },
        ),
    )


@app.get("/tailor-resume")
async def page_tailor_resume_redirect() -> RedirectResponse:
    return RedirectResponse(url="/resume-review", status_code=307)


@app.get("/about", response_class=HTMLResponse)
async def page_about(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "about.html",
        _ctx(request, "about") if user else _auth_ctx(request, "about"),
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def page_dashboard(request: Request) -> HTMLResponse:
    config = load_config()
    db = _db()
    stats = db.get_stats()
    avg_score, _scored = db.get_match_score_aggregate()
    high_priority = db.count_jobs_filtered(min_score=75)
    slug = _active_slug(config, request)
    profile = _load_user_profile(slug=slug, config=config, request=request)
    user = get_current_user(request)
    db = _db()
    profile_count = len(list_profiles(
        int(user["id"]) if user else None,
        db=db if user else None,
    ))
    recent_matches = db.get_jobs_filtered(
        min_score=50,
        sort_by_match=True,
        limit=8,
    )
    scrape_status = _scrape_status_payload()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            "dashboard",
            stats={
                "active_agents": profile_count or 1,
                "interviews": stats.get("total_applied", 0),
                "match_accuracy": avg_score,
                "total_jobs": stats.get("total_jobs", 0),
                "high_priority": high_priority,
            },
            active_slug=slug,
            profile_name=profile.get("name") or "",
            has_skills=_profile_has_skills(profile),
            profile_count=profile_count,
            recent_matches=recent_matches,
            scrape_status=scrape_status,
        ),
    )


@app.get("/profiles", response_class=HTMLResponse)
async def page_profiles_get(
    request: Request,
    slug: str = "",
) -> HTMLResponse:
    config = load_config()
    user_id = _user_id(request)
    db = _db()
    selected = slug.strip() or _active_slug(config, request) or ""
    if selected and selected not in list_profiles(user_id, db=db):
        selected = _active_slug(config, request) or ""
    return templates.TemplateResponse(
        request,
        "profiles.html",
        _ctx(
            request,
            "profiles",
            profiles=_profile_rows(config, request),
            selected_slug=selected,
            selected_profile=_profile_detail(config, selected, user_id=user_id)
            if selected
            else None,
        ),
    )


@app.post("/profiles", response_class=HTMLResponse)
def page_profiles_post(
    request: Request,
    slug: str = Form(...),
    resume: UploadFile = File(...),
    parse: str | None = Form(None),
) -> HTMLResponse:
    config = load_config()
    user = get_current_user(request)
    user_id = int(user["id"]) if user else None
    clean_slug = scoped_slug(user_id, slug)
    if not clean_slug:
        return templates.TemplateResponse(
            request,
            "profiles.html",
            _ctx(
                request,
                "profiles",
                profiles=_profile_rows(config, request),
                error="Invalid profile slug.",
            ),
            status_code=400,
        )

    if not resume.filename or not resume.filename.lower().endswith(".pdf"):
        return templates.TemplateResponse(
            request,
            "profiles.html",
            _ctx(
                request,
                "profiles",
                profiles=_profile_rows(config, request),
                error="Please upload a PDF file.",
            ),
            status_code=400,
        )

    content = resume.file.read()
    db = _db()
    message = "Resume saved to your account."
    error = None

    if user_id:
        db.save_resume(
            user_id,
            clean_slug,
            pdf_bytes=content,
            filename=resume.filename or f"{clean_slug}.pdf",
            parse_status="pdf_only",
        )
    else:
        from ai.profile_store import RESUME_DIR

        RESUME_DIR.mkdir(parents=True, exist_ok=True)
        dest = RESUME_DIR / f"{clean_slug}.pdf"
        dest.write_bytes(content)
        message = f"Uploaded resume to {dest.relative_to(PROJECT_ROOT)}."

    if parse:
        if user_id:
            out_path = Path(tempfile.gettempdir()) / (
                f"job_agent_profile_{user_id}_{clean_slug}.json"
            )
        else:
            out_path = profile_path(clean_slug)
            out_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_source = materialize_resume_pdf(
            clean_slug, user_id=user_id, db=db
        )
        if not pdf_source:
            from ai.profile_store import RESUME_DIR

            pdf_source = RESUME_DIR / f"{clean_slug}.pdf"
        try:
            profile = parse_resume(
                pdf_source,
                output_path=out_path,
                max_retries=web_parse_max_retries(),
                allow_local_fallback=True,
                slug=clean_slug,
            )
            parse_status = "partial" if is_local_fallback_profile(profile) else "parsed"
            if user_id:
                save_profile(
                    clean_slug,
                    profile,
                    user_id=user_id,
                    db=db,
                    parse_status=parse_status,
                )
                if out_path.exists():
                    out_path.unlink(missing_ok=True)
                message = "Resume saved to your account and parsed with AI."
                if is_local_fallback_profile(profile):
                    message = (
                        "Resume saved to your account. Gemini quota hit — "
                        "saved a basic local extract from PDF text. "
                        "Use Re-parse with AI when quota resets."
                    )
                db.set_user_active_profile(user_id, clean_slug)
            else:
                message += f" Parsed profile saved to {out_path.relative_to(PROJECT_ROOT)}."
                if is_local_fallback_profile(profile):
                    message = (
                        f"Uploaded resume to {pdf_source.relative_to(PROJECT_ROOT)}. "
                        "Gemini quota hit — saved a basic local extract from PDF text. "
                        "Use Re-parse with AI when quota resets."
                    )
        except Exception as exc:
            if user_id:
                db.save_resume(user_id, clean_slug, parse_status="failed")
            error = f"Parse failed: {format_gemini_error(exc)}"

    return templates.TemplateResponse(
        request,
        "profiles.html",
        _ctx(
            request,
            "profiles",
            profiles=_profile_rows(config, request),
            selected_slug=clean_slug,
            selected_profile=_profile_detail(config, clean_slug, user_id=user_id),
            message=message,
            error=error,
        ),
    )


@app.get("/jobs", response_class=HTMLResponse)
async def page_jobs(
    request: Request,
    search: str = "",
    location: str = "",
    min_score: str = "",
    platform: str = "",
    page: int = 1,
) -> HTMLResponse:
    db = _db()
    parsed_min_score = _optional_int(min_score)
    jobs, total = _filter_jobs(
        db,
        search=search,
        location=location,
        min_score=parsed_min_score,
        platform=platform,
        page=page,
    )
    locations = db.get_distinct_locations()
    platforms = db.get_distinct_platforms()
    total_pages = max(1, (total + JOBS_PAGE_SIZE - 1) // JOBS_PAGE_SIZE)
    page = min(max(1, page), total_pages)
    jobs, has_profile = _prepare_jobs_for_list(db, jobs, request)
    scrape_status = _scrape_status_payload()
    return templates.TemplateResponse(
        request,
        "jobs.html",
        _ctx(
            request,
            "jobs",
            jobs=jobs,
            job_count=total,
            page_jobs=len(jobs),
            locations=locations,
            platforms=platforms,
            search=search,
            selected_location=location,
            min_score=parsed_min_score,
            selected_platform=platform,
            page=page,
            total_pages=total_pages,
            page_size=JOBS_PAGE_SIZE,
            scrape_status=scrape_status,
            has_profile=has_profile,
        ),
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def page_job_detail(request: Request, job_id: int) -> HTMLResponse:
    config = load_config()
    db = _db()
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    slug = _active_slug(config, request)
    profile = _load_user_profile(slug=slug, config=config, request=request)
    profiles = _profile_rows(config, request)

    jd_analysis = db.get_jd_analysis(job_id)
    analyze_now = request.query_params.get("analyze") == "1"
    if analyze_now and not jd_analysis:
        try:
            jd_analysis = _ensure_jd_analysis(db, job)
        except Exception:
            jd_analysis = db.get_jd_analysis(job_id)

    explanation = None
    ats_result: dict[str, Any] | None = None
    matched_skills: list[str] = []
    missing_skills: list[str] = []

    if _profile_has_skills(profile) and jd_analysis:
        try:
            ats_result = compute_ats_score(profile, jd_analysis)
            matched_skills = ats_result.get("matched_keywords") or []
            missing_skills = ats_result.get("missing_keywords") or []
            if job.get("match_score") != ats_result.get("ats_score"):
                db.update_match_score(job_id, int(ats_result.get("ats_score") or 0))
                job = db.get_job(job_id) or job
        except Exception:
            pass

        cached = db.get_match_explanation(job_id, slug)
        if cached:
            explanation = cached.get("explanation")

    return templates.TemplateResponse(
        request,
        "job_detail.html",
        _ctx(
            request,
            "jobs",
            job=job,
            jd_analysis=jd_analysis,
            explanation=explanation,
            matched_skills=matched_skills,
            missing_skills=missing_skills,
            ats_result=ats_result,
            active_slug=slug,
            profiles=profiles,
            has_profile=_profile_has_skills(profile),
            needs_analysis=not jd_analysis and bool((job.get("description") or "").strip()),
        ),
    )


@app.get("/resume-review", response_class=HTMLResponse)
async def page_resume_review_get(
    request: Request,
    job_id: int | None = None,
) -> HTMLResponse:
    db = _db()
    jobs = _job_picker_list(db)
    config = load_config()
    profiles = _profile_rows(config, request)
    active_slug = _active_slug(config, request)
    has_parsed = any(p.get("has_parsed") for p in profiles)
    return templates.TemplateResponse(
        request,
        "resume_review.html",
        _ctx(
            request,
            "resume-review",
            jobs=jobs,
            selected_job_id=job_id,
            profiles=profiles,
            active_slug=active_slug,
            has_parsed_profile=has_parsed or _profile_has_skills(
                _load_user_profile(slug=active_slug, config=config, request=request)
            ),
        ),
    )


@app.post("/resume-review/propose", response_class=HTMLResponse)
def page_resume_review_propose(
    request: Request,
    job_id: int = Form(...),
    profile_slug: str = Form(""),
) -> HTMLResponse:
    """No-JS fallback for generating resume diffs."""
    config = load_config()
    db = _db()
    slug = _resolve_effective_slug(config, request, override=profile_slug)
    profile = _load_user_profile(slug=slug, config=config, request=request)
    job = db.get_job(job_id)
    jobs = _job_picker_list(db)
    profiles = _profile_rows(config, request)

    if not job:
        return templates.TemplateResponse(
            request,
            "resume_review.html",
            _ctx(request, "resume-review", jobs=jobs, profiles=profiles, error="Job not found."),
            status_code=404,
        )
    if not _profile_has_skills(profile):
        return templates.TemplateResponse(
            request,
            "resume_review.html",
            _ctx(
                request,
                "resume-review",
                jobs=jobs,
                profiles=profiles,
                error="Upload and parse a resume first.",
            ),
            status_code=400,
        )

    try:
        diffs, warning = _run_resume_propose(db, job, profile, slug)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "resume_review.html",
            _ctx(
                request,
                "resume-review",
                jobs=jobs,
                profiles=profiles,
                selected_job_id=job_id,
                error=str(exc),
            ),
            status_code=502,
        )

    return templates.TemplateResponse(
        request,
        "resume_review.html",
        _ctx(
            request,
            "resume-review",
            jobs=jobs,
            profiles=profiles,
            selected_job_id=job_id,
            selected_profile_slug=slug,
            diffs=diffs,
            message=warning,
        ),
    )


@app.post("/resume-review/accept", response_class=HTMLResponse)
def page_resume_review_accept(
    request: Request,
    job_id: int = Form(...),
    profile_slug: str = Form(""),
    accepted: Annotated[list[int], Form()] = [],
) -> HTMLResponse:
    config = load_config()
    db = _db()
    slug = _resolve_effective_slug(config, request, override=profile_slug)
    cache_key = (job_id, slug or "")
    cached = _review_cache.get(cache_key)
    jobs = _job_picker_list(db)
    profiles = _profile_rows(config, request)

    if not cached:
        return templates.TemplateResponse(
            request,
            "resume_review.html",
            _ctx(
                request,
                "resume-review",
                jobs=jobs,
                profiles=profiles,
                selected_job_id=job_id,
                selected_profile_slug=slug,
                error="Session expired — generate suggestions again.",
            ),
            status_code=400,
        )

    try:
        profile = cached["original"]
        diffs = cached["diffs"]
        analysis = cached["jd_analysis"]
        job = cached["job"]
        indices = [int(i) for i in accepted]
        _, pdf_path, _ = customize_resume(
            profile,
            analysis,
            company=job.get("company") or "",
            role=job.get("title") or "",
            job=job,
            diffs=diffs,
            accepted_diff_indices=indices,
        )
        message = f"Resume PDF saved to {pdf_path.relative_to(PROJECT_ROOT)}"
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "resume_review.html",
            _ctx(
                request,
                "resume-review",
                jobs=jobs,
                profiles=profiles,
                error=str(exc),
            ),
            status_code=502,
        )

    return templates.TemplateResponse(
        request,
        "resume_review.html",
        _ctx(request, "resume-review", jobs=jobs, profiles=profiles, message=message),
    )


@app.get("/cover-letter", response_class=HTMLResponse)
async def page_cover_letter_get(
    request: Request,
    job_id: int | None = None,
) -> HTMLResponse:
    db = _db()
    config = load_config()
    jobs = _job_picker_list(db)
    return templates.TemplateResponse(
        request,
        "cover_letter.html",
        _ctx(
            request,
            "cover-letter",
            jobs=jobs,
            selected_job_id=job_id,
            profiles=_profile_rows(config, request),
            active_slug=_active_slug(config, request),
        ),
    )


@app.post("/cover-letter", response_class=HTMLResponse)
def page_cover_letter_post(
    request: Request,
    job_id: int = Form(...),
    profile_slug: str = Form(""),
) -> HTMLResponse:
    config = load_config()
    db = _db()
    slug = profile_slug.strip() or _active_slug(config, request)
    profile = _load_user_profile(slug=slug, config=config, request=request)
    job = db.get_job(job_id)
    jobs = _job_picker_list(db)

    if not job:
        return templates.TemplateResponse(
            request,
            "cover_letter.html",
            _ctx(
                request,
                "cover-letter",
                jobs=jobs,
                profiles=_profile_rows(config, request),
                error="Job not found.",
            ),
            status_code=404,
        )
    if not _profile_has_skills(profile):
        return templates.TemplateResponse(
            request,
            "cover_letter.html",
            _ctx(
                request,
                "cover-letter",
                jobs=jobs,
                profiles=_profile_rows(config, request),
                error="Upload and parse a resume first.",
            ),
            status_code=400,
        )

    try:
        analysis = _ensure_jd_analysis(db, job)
        if not analysis:
            raise ValueError("No job description to analyze.")
        letter_text, pdf_path = generate_cover_letter(profile, job, analysis)
        message = f"Cover letter PDF saved to {pdf_path.relative_to(PROJECT_ROOT)}"
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "cover_letter.html",
            _ctx(
                request,
                "cover-letter",
                jobs=jobs,
                profiles=_profile_rows(config, request),
                error=str(exc),
            ),
            status_code=502,
        )

    return templates.TemplateResponse(
        request,
        "cover_letter.html",
        _ctx(
            request,
            "cover-letter",
            jobs=jobs,
            selected_job_id=job_id,
            profiles=_profile_rows(config, request),
            active_slug=slug,
            letter_text=letter_text,
            pdf_path=str(pdf_path.relative_to(PROJECT_ROOT)),
            message=message,
        ),
    )


@app.get("/ats", response_class=HTMLResponse)
def page_ats_get(
    request: Request,
    job_id: int | None = None,
    profile: str = "",
) -> HTMLResponse:
    config = load_config()
    db = _db()
    jobs = _job_picker_list(db)
    slug = _resolve_effective_slug(config, request, override=profile)
    profiles = _profile_rows(config, request)
    prof = _load_user_profile(slug=slug, config=config, request=request)
    ats_result = None
    ats_error: str | None = None
    ats_report = ""
    job = db.get_job(job_id) if job_id else None

    if job_id and not job:
        ats_error = "Job not found."
    elif job:
        ats_result, ats_error = _compute_ats_for_job(db, job, prof)
        if ats_result:
            db.update_match_score(job_id, int(ats_result.get("ats_score") or 0))
            ats_report = format_ats_report(ats_result)
        elif not ats_error and job_id:
            ats_error = "Could not compute ATS score."

    return templates.TemplateResponse(
        request,
        "ats.html",
        _ctx(
            request,
            "ats",
            jobs=jobs,
            profiles=profiles,
            selected_job_id=job_id,
            selected_profile_slug=slug,
            ats_result=ats_result,
            ats_error=ats_error,
            ats_report=ats_report,
            job=job,
            has_profile=_profile_has_skills(prof),
        ),
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    return JSONResponse(_db().get_stats())


@app.get("/api/profiles")
async def api_profiles(request: Request) -> JSONResponse:
    config = load_config()
    return JSONResponse({"profiles": _profile_rows(config, request)})


@app.post("/api/profiles/{slug}/activate")
async def api_activate_profile(request: Request, slug: str) -> JSONResponse:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    clean = slugify(slug)
    user_id = int(user["id"])
    db = _db()
    allowed = list_profiles(user_id, db=db)
    if clean not in allowed:
        raise HTTPException(status_code=404, detail="Profile not found")
    if profile_exists_in_db(user_id, clean, db):
        db.set_active_resume(user_id, clean)
    else:
        db.set_user_active_profile(user_id, clean)
    return JSONResponse({"ok": True, "active_profile": clean})


@app.delete("/api/profiles/{slug}")
async def api_delete_profile(request: Request, slug: str) -> JSONResponse:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    clean = slugify(slug)
    user_id = int(user["id"])
    db = _db()
    allowed = list_profiles(user_id, db=db)
    if clean not in allowed:
        raise HTTPException(status_code=404, detail="Profile not found")
    delete_profile(clean, user_id=user_id, db=db)
    settings = db.get_user_settings(user_id)
    if settings and settings.get("active_profile") == clean:
        db.set_user_active_profile(user_id, None)
    return JSONResponse({"ok": True})


@app.get("/api/profiles/{slug}")
async def api_profile_detail(request: Request, slug: str) -> JSONResponse:
    config = load_config()
    clean = slugify(slug)
    user_id = _user_id(request)
    db = _db()
    if user_id and clean not in list_profiles(user_id, db=db):
        raise HTTPException(status_code=404, detail="Profile not found")
    detail = _profile_detail(config, clean, user_id=user_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Profile not found")
    return JSONResponse(detail)


@app.get("/api/resume-pdf/{slug}")
async def api_resume_pdf(request: Request, slug: str) -> Response:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    clean = slugify(slug)
    user_id = int(user["id"])
    db = _db()
    allowed = list_profiles(user_id, db=db)
    if clean not in allowed:
        raise HTTPException(status_code=404, detail="Resume PDF not found")

    pdf_bytes = db.get_resume_pdf_bytes(user_id, clean)
    if not pdf_bytes:
        import_disk_resume_to_db(user_id, clean, db)
        pdf_bytes = db.get_resume_pdf_bytes(user_id, clean)
    if not pdf_bytes:
        config = load_config()
        pdf_path = resolve_resume_pdf(clean, config=config)
        if not pdf_path.exists():
            raise HTTPException(status_code=404, detail="Resume PDF not found")
        pdf_bytes = pdf_path.read_bytes()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{clean}.pdf"'},
    )


@app.get("/api/jobs")
def api_jobs(
    request: Request,
    search: str = "",
    location: str = "",
    min_score: str = Query(""),
    platform: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(JOBS_PAGE_SIZE, ge=1, le=200),
) -> JSONResponse:
    db = _db()
    
    # Redesign Step 4 & 5: On-demand Scraping Trigger
    if search.strip():
        user_id = _user_id(request)
        cache_key = (user_id, search.strip().lower(), location.strip().lower())
        now = datetime.now(timezone.utc)
        cached_time = _search_cache.get(cache_key)
        
        if not cached_time or (now - cached_time) > timedelta(minutes=30):
            print(f"[api_jobs] Cache miss for {cache_key}. Triggering background scrape.")
            _search_cache[cache_key] = now
            active_slug = _active_slug(load_config(), request)
            threading.Thread(
                target=_run_bg_scrape,
                args=(user_id, search, location, active_slug),
                daemon=True,
            ).start()
        else:
            print(f"[api_jobs] Cache hit for {cache_key} (last run at {cached_time}). Skipping background scrape.")
            
    parsed_min_score = _optional_int(min_score)
    jobs, total = _filter_jobs(
        db,
        search=search,
        location=location,
        min_score=parsed_min_score,
        platform=platform,
        page=page,
        page_size=page_size,
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    jobs, has_profile = _prepare_jobs_for_list(db, jobs, request)
    return JSONResponse({
        "jobs": jobs,
        "count": len(jobs),
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_profile": has_profile,
        "scrape": _scrape_status_payload(),
    })


@app.get("/api/scrape/status")
async def api_scrape_status() -> JSONResponse:
    return JSONResponse(_scrape_status_payload())


@app.post("/api/scrape/trigger")
async def api_scrape_trigger(request: Request) -> JSONResponse:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    scheduler = JobScrapeScheduler.get_instance(config=load_config())
    result = scheduler.trigger_manual()
    if result.get("status") == "rate_limited":
        raise HTTPException(status_code=429, detail=result.get("error") or "Rate limited")
    return JSONResponse(result)


@app.post("/api/jobs/{job_id}/analyze")
def api_analyze_job(job_id: int) -> JSONResponse:
    db = _db()
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        analysis = _ensure_jd_analysis(db, job)
    except HTTPException as exc:
        raise exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse({"jd_analysis": analysis})


@app.get("/api/jobs/{job_id}")
def api_job_detail(request: Request, job_id: int) -> JSONResponse:
    db = _db()
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    config = load_config()
    slug = _active_slug(config, request)
    jd_analysis = db.get_jd_analysis(job_id)
    explanation = db.get_match_explanation(job_id, slug)
    ats_result = None
    profile = _load_user_profile(slug=slug, config=config, request=request)
    if _profile_has_skills(profile) and jd_analysis:
        try:
            ats_result = compute_ats_score(profile, jd_analysis)
        except Exception:
            pass
    return JSONResponse({
        "job": job,
        "jd_analysis": jd_analysis,
        "explanation": explanation,
        "ats": ats_result,
    })


@app.post("/api/ats/{job_id}")
def api_ats_score(
    request: Request,
    job_id: int,
    profile_slug: str = Form(""),
) -> JSONResponse:
    config = load_config()
    db = _db()
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    slug = _resolve_effective_slug(config, request, override=profile_slug)
    profile = _load_user_profile(slug=slug, config=config, request=request)
    result, err = _compute_ats_for_job(db, job, profile)
    if err:
        raise HTTPException(status_code=400, detail=err)
    assert result is not None
    db.update_match_score(job_id, int(result.get("ats_score") or 0))
    payload = dict(result)
    payload["report_text"] = format_ats_report(result)
    payload["profile_slug"] = slug
    return JSONResponse(payload)


@app.post("/api/profiles/parse")
def api_parse_profile(
    request: Request,
    slug: str = Form(...),
    resume: UploadFile = File(...),
) -> JSONResponse:
    user = get_current_user(request)
    user_id = int(user["id"]) if user else None
    clean_slug = scoped_slug(user_id, slug)
    if not clean_slug:
        raise HTTPException(status_code=400, detail="Invalid slug")

    content = resume.file.read()
    db = _db()

    if user_id:
        db.save_resume(
            user_id,
            clean_slug,
            pdf_bytes=content,
            filename=resume.filename or f"{clean_slug}.pdf",
            parse_status="pdf_only",
        )
        out_path = Path(tempfile.gettempdir()) / (
            f"job_agent_profile_{user_id}_{clean_slug}.json"
        )
        pdf_source = materialize_resume_pdf(clean_slug, user_id=user_id, db=db)
    else:
        from ai.profile_store import RESUME_DIR

        RESUME_DIR.mkdir(parents=True, exist_ok=True)
        dest = RESUME_DIR / f"{clean_slug}.pdf"
        dest.write_bytes(content)
        out_path = profile_path(clean_slug)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_source = dest

    if not pdf_source:
        raise HTTPException(status_code=500, detail="Failed to store resume PDF")

    try:
        profile = parse_resume(
            pdf_source,
            output_path=out_path,
            max_retries=web_parse_max_retries(),
            allow_local_fallback=True,
            slug=clean_slug,
        )
    except Exception as exc:
        if user_id:
            db.save_resume(user_id, clean_slug, parse_status="failed")
        raise HTTPException(
            status_code=502,
            detail=format_gemini_error(exc),
        ) from exc

    parse_status = "partial" if is_local_fallback_profile(profile) else "parsed"
    if user_id:
        save_profile(
            clean_slug,
            profile,
            user_id=user_id,
            db=db,
            parse_status=parse_status,
        )
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        db.set_user_active_profile(user_id, clean_slug)
        msg = "Resume saved to your account and parsed with AI."
    else:
        msg = "Profile parsed."

    if is_local_fallback_profile(profile):
        msg = (
            "Resume saved to your account. Saved basic local extract — Gemini unavailable."
            if user_id
            else "Saved basic local extract — Gemini unavailable."
        )
    return JSONResponse({
        "slug": clean_slug,
        "profile": profile,
        "message": msg,
    })


@app.post("/api/profiles/{slug}/parse")
def api_reparse_profile(request: Request, slug: str) -> JSONResponse:
    """Re-parse an existing uploaded PDF without re-upload."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    clean = slugify(slug)
    user_id = int(user["id"])
    db = _db()
    allowed = list_profiles(user_id, db=db)
    if clean not in allowed:
        raise HTTPException(status_code=404, detail="Profile not found")
    profile, msg, err = _run_parse_for_slug(clean, user_id=user_id)
    if err:
        raise HTTPException(status_code=502, detail=err)
    detail = _profile_detail(load_config(), clean, user_id=user_id)
    return JSONResponse({
        "ok": True,
        "message": msg,
        "slug": clean,
        "profile": profile,
        "detail": detail,
    })


@app.post("/api/resume-review/{job_id}/propose")
def api_resume_propose(
    request: Request,
    job_id: int,
    profile_slug: str = Form(""),
) -> JSONResponse:
    config = load_config()
    db = _db()
    slug = _resolve_effective_slug(config, request, override=profile_slug)
    profile = _load_user_profile(slug=slug, config=config, request=request)
    job = db.get_job(job_id)

    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if not _profile_has_skills(profile):
        return JSONResponse({"error": "Upload and parse a resume first."}, status_code=400)

    try:
        diffs, warning = _run_resume_propose(db, job, profile, slug)
        payload: dict[str, Any] = {
            "diffs": diffs,
            "count": len(diffs),
            "profile_slug": slug,
        }
        if warning:
            payload["warning"] = warning
        return JSONResponse(payload)
    except HTTPException as exc:
        detail = exc.detail
        return JSONResponse(
            {"error": detail if isinstance(detail, str) else str(detail)},
            status_code=exc.status_code,
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/cover-letter/{job_id}")
def api_cover_letter(
    request: Request,
    job_id: int,
    profile_slug: str = Form(""),
) -> JSONResponse:
    config = load_config()
    db = _db()
    slug = profile_slug.strip() or _active_slug(config, request)
    profile = _load_user_profile(slug=slug, config=config, request=request)
    job = db.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _profile_has_skills(profile):
        raise HTTPException(status_code=400, detail="Upload and parse a resume first.")

    analysis = _ensure_jd_analysis(db, job)
    if not analysis:
        raise HTTPException(status_code=400, detail="No job description available.")

    letter_text, pdf_path = generate_cover_letter(profile, job, analysis)
    return JSONResponse({
        "letter_text": letter_text,
        "pdf_path": str(pdf_path.relative_to(PROJECT_ROOT)),
    })
