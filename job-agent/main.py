"""AI Job Application Agent - master orchestrator.

Run with no args to launch the FastAPI web app, or pick a CLI phase via `--mode`.

    python main.py                            # web app on http://127.0.0.1:8765 (default)
    python main.py --mode web                 # same as above
    python main.py --mode parse-resume        # deprecated; use web app upload
    python main.py --mode scrape              # scrape + score every new job
    python main.py --mode analyze             # re-score every job, print top 10
    python main.py --mode enrich-profile      # interactive profile Q&A
    python main.py --mode apply               # browser-driven batch apply
    python main.py --mode dashboard           # deprecated; launches web app

Common options:
    --allow-cli-upload   allow parse-resume from CLI (default: web app only)
    --resume PATH        override resume PDF (parse-resume + --allow-cli-upload)
    --profile SLUG       use a named profile (config/profiles/<slug>.json)
    --min-score N        override config.job_search.min_match_score
    --limit N            cap how many jobs analyze / apply touches (default 10)
    --parallel           scrape platforms in parallel threads
    --workers N          parallel worker count (default 3)
    --port N             web app port (default 8765; see config web.port)
"""

from __future__ import annotations

import argparse
import socket
import sys
import textwrap
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from ai.profile_store import (
    LEGACY_PROFILE_PATH,
    load_profile,
    profile_path,
    resolve_active_slug,
    resolve_resume_pdf,
)


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

VALID_MODES = [
    "parse-resume", "scrape", "analyze", "enrich-profile", "apply", "dashboard", "web",
]
WEB_ALIASES = {"website": "web"}
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8765


# =============================================================================
# Helpers
# =============================================================================
def _banner(title: str, subtitle: str | None = None) -> None:
    line = "=" * 70
    print(f"\n{line}\n{title}")
    if subtitle:
        print(subtitle)
    print(line)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_user_profile(
    config: dict[str, Any], slug: str | None = None
) -> dict[str, Any]:
    """Merge the active parsed profile JSON with ``config.user.*`` contact fields.

    When ``slug`` is provided we read ``config/profiles/<slug>.json``; otherwise
    fall back to the legacy ``config/user_profile.json``.
    """
    return load_profile(slug=slug, config=config)


def _resolve_profile_slug(
    args: argparse.Namespace, config: dict[str, Any]
) -> str | None:
    """CLI override > config.user.active_profile > None (legacy single file)."""
    override = getattr(args, "profile", None)
    return resolve_active_slug(config, override=override)


def _web_settings(
    config: dict[str, Any], args: argparse.Namespace
) -> tuple[str, int]:
    """Resolve bind host/port: CLI --port > config web.* > defaults."""
    web_cfg = config.get("web") or {}
    host = str(web_cfg.get("host") or DEFAULT_WEB_HOST)
    port = getattr(args, "port", None) or web_cfg.get("port") or DEFAULT_WEB_PORT
    return host, int(port)


def _display_url(host: str, port: int) -> str:
    """User-facing URL (127.0.0.1 when bound to all interfaces)."""
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{display_host}:{port}"


def _is_port_free(host: str, port: int) -> bool:
    """Return True if we can bind to host:port (best-effort on Windows)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _resolve_web_port(host: str, preferred: int, *, max_tries: int = 20) -> int:
    """Pick preferred port or the next free port in range."""
    for offset in range(max_tries):
        candidate = preferred + offset
        if _is_port_free(host, candidate):
            return candidate
    raise OSError(
        f"No free port in range {preferred}-{preferred + max_tries - 1} on {host}"
    )


def _profile_has_skills(profile: dict[str, Any]) -> bool:
    return any(
        profile.get(k)
        for k in ("skills", "tools", "programming_languages", "frameworks", "experience")
    )


def _print_top_matches(scored: list[dict[str, Any]], n: int = 10) -> None:
    if not scored:
        print("No scored jobs to display.")
        return
    top = sorted(
        scored, key=lambda x: (x.get("match_score") or 0), reverse=True
    )[:n]
    print(f"\n----- Top {len(top)} matches " + "-" * (60 - len(f"Top {len(top)} matches ")))
    print(f"{'#':<3} {'Score':<6} {'Title':<38} {'Company':<22} {'Platform':<12}")
    print("-" * 70)
    for i, j in enumerate(top, 1):
        title = (j.get("title") or "")[:38]
        company = (j.get("company") or "")[:22]
        platform = (j.get("platform") or "")[:12]
        score = j.get("match_score") or 0
        print(f"{i:<3} {score:<6} {title:<38} {company:<22} {platform:<12}")
    print("-" * 70)


# =============================================================================
# Mode handlers
# =============================================================================
def cmd_parse_resume(args: argparse.Namespace, config: dict[str, Any]) -> None:
    """Parse a resume PDF into structured JSON (CLI fallback for power users).

    Primary flow: upload and parse via the web app
    (``python main.py`` -> Profiles page at /profiles).
    """
    if not getattr(args, "allow_cli_upload", False):
        print("[parse-resume] Resume upload and parsing is done via the web app.")
        print("              Launch:  python main.py")
        _, port = _web_settings(config, args)
        print(f"              Then open **Profiles** at http://127.0.0.1:{port}/profiles")
        print("              Power users: pass --allow-cli-upload to use this CLI mode.")
        sys.exit(1)

    from ai.resume_parser import parse_resume  # local import keeps startup fast

    slug = _resolve_profile_slug(args, config)

    if args.resume:
        resume_path = Path(args.resume).expanduser().resolve()
    else:
        resume_path = resolve_resume_pdf(slug, config=config)

    if not resume_path.exists():
        print(f"[parse-resume] Resume PDF not found: {resume_path}")
        hint = (
            f"resume/{slug}.pdf" if slug else "resume/master_resume.pdf"
        )
        print(
            f"              Upload via the web app (Profiles) or "
            f"place a PDF at {hint}, or pass --resume PATH."
        )
        sys.exit(1)

    if slug:
        out_path = profile_path(slug)
        subtitle = f"  source: {resume_path}\n  profile: {slug} -> {out_path}"
    else:
        out_path = LEGACY_PROFILE_PATH
        subtitle = f"  source: {resume_path}\n  profile: (default) -> {out_path}"

    _banner("Parsing resume", subtitle)
    parse_resume(resume_path, output_path=out_path)


def cmd_scrape(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Scrape every enabled platform, then score every still-unscored job.

    Returns the number of unique jobs in the DB after the scrape (not just
    newly added rows).
    """
    from scrapers.scraper_manager import ScraperManager
    from automation.apply_agent import ApplyAgent

    slug = _resolve_profile_slug(args, config)
    _banner(
        "Scraping enabled platforms",
        f"  parallel={args.parallel}  workers={args.workers}  "
        f"profile={slug or '(default)'}",
    )
    mgr = ScraperManager(config=config)
    result = mgr.run(parallel=args.parallel, max_workers=args.workers)

    # Score any 'new' jobs that came back without a match_score yet.
    _banner("Analyzing JDs and scoring matches")
    agent = ApplyAgent(profile_slug=slug)
    if not _profile_has_skills(agent.user_profile):
        print(
            "[scrape] user_profile has no skills - matcher won't have much to "
            "compare against. Upload and parse a resume in the web app "
            "(Profiles page) first."
        )
    scored = agent._score_new_jobs()
    print(f"[scrape] scored {scored} new job(s).")
    print(
        f"[scrape] {result.new_jobs} new job(s) added "
        f"({result.total_found} unique across platforms)."
    )
    return result.total_found


def cmd_analyze(
    args: argparse.Namespace, config: dict[str, Any], *, print_top: bool = True
) -> list[dict[str, Any]]:
    """Re-score every job in the DB. Returns the enriched job rows."""
    from ai.jd_analyzer import analyze_jd, infer_seniority_level
    from ai.matcher import compute_match
    from db.database import Database

    db = Database()
    slug = _resolve_profile_slug(args, config)
    profile = load_user_profile(config, slug=slug)
    explain_matches = bool(
        config.get("ai", {}).get("explain_matches")
        or config.get("job_search", {}).get("explain_matches")
    )
    if not _profile_has_skills(profile):
        print(
            "[analyze] user_profile has no skills. "
            "Upload and parse a resume in the web app (Profiles page)."
        )
        return []

    jobs = db.get_jobs()
    if not jobs:
        print("[analyze] no jobs in DB yet. Run `python main.py --mode scrape` first.")
        return []

    _banner(f"Re-scoring {len(jobs)} job(s) against current profile")
    enriched: list[dict[str, Any]] = []
    for i, j in enumerate(jobs, start=1):
        job_id = int(j["id"])
        # Use cached JD analysis when present; analyze otherwise.
        analysis = db.get_jd_analysis(job_id)
        if not analysis:
            jd_text = (j.get("description") or j.get("title") or "").strip()
            if not jd_text:
                continue
            try:
                analysis = analyze_jd(jd_text)
                analysis["seniority_level"] = infer_seniority_level(
                    jd_text,
                    job_title=j.get("title") or "",
                    experience_required=analysis.get("experience_required") or "",
                )
                db.insert_jd_analysis(job_id, analysis)
            except Exception as exc:
                print(f"  [{i}/{len(jobs)}] analyze failed for "
                      f"{j.get('title')!r}: {exc}")
                continue
        elif not analysis.get("seniority_level"):
            jd_text = (j.get("description") or j.get("title") or "").strip()
            analysis["seniority_level"] = infer_seniority_level(
                jd_text,
                job_title=j.get("title") or "",
                experience_required=analysis.get("experience_required") or "",
            )
            db.insert_jd_analysis(job_id, analysis)

        try:
            result = compute_match(profile, analysis)
        except Exception as exc:
            print(f"  [{i}/{len(jobs)}] match failed for "
                  f"{j.get('title')!r}: {exc}")
            continue

        score = int(result["match_score"])
        db.update_match_score(job_id, score)

        if explain_matches:
            try:
                from ai.match_explainer import get_or_create_match_explanation

                get_or_create_match_explanation(
                    db,
                    job_id=job_id,
                    profile_slug=slug,
                    job_title=j.get("title") or "",
                    match_result=result,
                    jd_analysis=analysis,
                )
            except Exception as exc:
                print(f"  [{i}/{len(jobs)}] explanation failed for "
                      f"{j.get('title')!r}: {exc}")

        enriched.append({**j, **result, "match_score": score})

        if i % 10 == 0 or i == len(jobs):
            print(f"  scored {i}/{len(jobs)}")

    if print_top:
        _print_top_matches(enriched, n=args.limit or 10)
    return enriched


def cmd_enrich_profile(args: argparse.Namespace, config: dict[str, Any]) -> None:
    """Interactive CLI to enrich a parsed profile with user-provided facts."""
    from ai.profile_enricher import run_enrichment_cli

    slug = _resolve_profile_slug(args, config)
    profile = load_user_profile(config, slug=slug)
    if not profile:
        print(
            "[enrich-profile] No profile found. "
            "Upload and parse a resume in the web app (Profiles page)."
        )
        sys.exit(1)

    enrichment_cfg = config.get("profile_enrichment") or {}
    if enrichment_cfg.get("enabled") is False:
        print("[enrich-profile] Disabled in config (profile_enrichment.enabled: false).")
        return

    _banner(
        "Profile enrichment",
        f"  profile={slug or '(default)'}\n"
        "  Answer questions to strengthen your profile (facts only).",
    )
    run_enrichment_cli(
        profile,
        slug,
        max_questions=int(enrichment_cfg.get("max_questions", 8)),
    )


def cmd_apply(args: argparse.Namespace, config: dict[str, Any]) -> None:
    """Run the apply agent in batch mode (browser-driven, with human pauses)."""
    from automation.apply_agent import ApplyAgent

    slug = _resolve_profile_slug(args, config)
    _banner(
        "Batch apply",
        f"  limit={args.limit}  min_score={args.min_score or 'config default'}  "
        f"profile={slug or '(default)'}",
    )
    agent = ApplyAgent(profile_slug=slug)
    agent.run_batch(min_score=args.min_score, limit=args.limit)


def cmd_web(args: argparse.Namespace, config: dict[str, Any]) -> None:
    """Launch the FastAPI browser web app (works in any modern browser)."""
    import threading
    import time
    import webbrowser

    host, preferred_port = _web_settings(config, args)
    try:
        port = _resolve_web_port(host, preferred_port)
    except OSError as exc:
        print(
            f"\n[web] Could not find a free port near {preferred_port} — {exc}\n"
            f"      Close other copies of this app or pass --port {preferred_port + 1}",
            file=sys.stderr,
        )
        sys.exit(1)

    url = _display_url(host, port)
    slug = _resolve_profile_slug(args, config)

    port_note = ""
    if port != preferred_port:
        port_note = (
            f"\n  Note: port {preferred_port} was busy; using {port} instead."
        )

    _banner(
        "Launching web app",
        f"  profile={slug or '(default)'}\n"
        f"  bind: {host}:{port}{port_note}\n"
        "  Ctrl+C to stop.",
    )
    print(f"\n  AI Job Agent running at {url}\n")

    def _open_browser() -> None:
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        import uvicorn
    except ImportError:
        print(
            "[web] uvicorn not installed. Run: pip install fastapi uvicorn python-multipart"
        )
        sys.exit(1)

    uvicorn.run("web.server:app", host=host, port=port, reload=False)


def cmd_dashboard(args: argparse.Namespace, config: dict[str, Any]) -> None:
    """Deprecated alias — redirects to the FastAPI web app."""
    print(
        "[dashboard] Streamlit dashboard removed/deprecated. "
        "Use: python main.py --mode web"
    )
    cmd_web(args, config)


def cmd_pipeline(args: argparse.Namespace, config: dict[str, Any]) -> None:
    """Full interactive pipeline: scrape -> analyze -> confirm -> apply."""
    slug = _resolve_profile_slug(args, config)
    profile = load_user_profile(config, slug=slug)

    enabled = [k for k, v in (config.get("platforms") or {}).items() if v]
    _banner(
        "AI Job Agent - Full Pipeline",
        f"  candidate : {profile.get('name') or '(unknown)'}\n"
        f"  profile   : {slug or '(default)'}\n"
        f"  platforms : {', '.join(enabled) if enabled else '(none enabled)'}\n"
        f"  min score : {config.get('job_search', {}).get('min_match_score', '?')}",
    )

    if not _profile_has_skills(profile):
        print(
            "\nYour parsed profile has no skills/experience yet.\n"
            "Open the web app (python main.py), go to Profiles, upload your PDF, "
            "and click Parse resume.\n"
            "Then re-run this pipeline."
        )
        return

    # 1. Scrape + score new jobs
    cmd_scrape(args, config)

    # 2. Re-analyze everything (cheap if JD analyses are cached) and show top
    scored = cmd_analyze(args, config, print_top=True)
    if not scored:
        return

    top = [j for j in scored if (j.get("match_score") or 0) >= (
        args.min_score or config.get("job_search", {}).get("min_match_score", 70)
    )]

    if not top:
        print(
            "\nNo jobs cleared the min match score. "
            "Lower it in config.yaml (job_search.min_match_score) "
            "or pass --min-score 50."
        )
        return

    # 3. Ask before applying
    print(f"\n{len(top)} job(s) cleared the score threshold.")
    try:
        answer = input("Apply to top matches? (y/n): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()  # newline after ^C
        answer = "n"

    if answer in {"y", "yes"}:
        cmd_apply(args, config)
    else:
        print(
            "\nSkipping apply step. Run later with:\n"
            "  python main.py --mode apply"
        )


# =============================================================================
# Entry point
# =============================================================================
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="job-agent",
        description="AI Job Application Agent - master orchestrator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              python main.py                       # web app (default)
              python main.py --mode web            # same as above
              python main.py --mode scrape --parallel
              python main.py --mode analyze --limit 20
              python main.py --mode enrich-profile --profile data_science
              python main.py --mode apply --min-score 60 --limit 5
              python main.py --mode parse-resume --allow-cli-upload  # power users
            """
        ),
    )
    p.add_argument(
        "--mode",
        choices=VALID_MODES + list(WEB_ALIASES.keys()),
        default=None,
        help="Run one CLI phase. Omit to launch the web app (default).",
    )
    p.add_argument("--resume", type=str, default=None,
                   help="Path to a resume PDF (parse-resume + --allow-cli-upload).")
    p.add_argument(
        "--allow-cli-upload",
        action="store_true",
        help=(
            "Allow parse-resume from the CLI. By default, upload and parse "
            "via the web app (Profiles page)."
        ),
    )
    p.add_argument(
        "--profile", type=str, default=None,
        help=(
            "Named profile slug. parse-resume writes to "
            "config/profiles/<slug>.json; other modes load the matching "
            "profile for matching + apply. Defaults to config.user.active_profile."
        ),
    )
    p.add_argument("--min-score", type=int, default=None,
                   help="Override config.job_search.min_match_score.")
    p.add_argument("--limit", type=int, default=10,
                   help="Cap analyze/apply to N jobs (default 10).")
    p.add_argument("--parallel", action="store_true",
                   help="Run scrapers in parallel threads.")
    p.add_argument("--workers", type=int, default=3,
                   help="Parallel worker count when --parallel is set.")
    p.add_argument(
        "--port", type=int, default=None,
        help=f"Web app port (default {DEFAULT_WEB_PORT}; overrides config web.port).",
    )
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    config = load_config()

    handlers = {
        "parse-resume": cmd_parse_resume,
        "scrape": cmd_scrape,
        "analyze": cmd_analyze,
        "enrich-profile": cmd_enrich_profile,
        "apply": cmd_apply,
        "dashboard": cmd_dashboard,
        "web": cmd_web,
    }

    try:
        if args.mode is None:
            cmd_web(args, config)
        else:
            mode = WEB_ALIASES.get(args.mode, args.mode)
            handlers[mode](args, config)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        print(f"\n[fatal] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
