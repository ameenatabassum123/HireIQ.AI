"""Apply Agent: end-to-end orchestrator for a single job application.

Flow per job:
    1. Fetch the job row from SQLite.
    2. Run / cache JD analysis (`ai.jd_analyzer`).
    3. Score the match against the user profile (`ai.matcher`); skip if below
       the configured `min_match_score`.
    4. Generate a tailored resume PDF (`ai.resume_customizer`).
    5. Generate a tailored cover letter PDF + body text (`ai.cover_letter`).
    6. Open a visible Playwright browser, best-effort fill common form fields,
       pause for human review, submit on ENTER.
    7. Record the application in SQLite.

Anti-foot-gun defaults:
    - Headless = False (you can see what's happening).
    - The submit click only happens AFTER you press ENTER. Ctrl+C aborts.
    - Auto-applies are throttled by `automation.apply_delay_seconds` from config.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from ai.cover_letter import generate_cover_letter
from ai.jd_analyzer import analyze_jd
from ai.matcher import compute_match
from ai.profile_store import load_profile, resolve_active_slug
from ai.resume_customizer import customize_resume
from db.database import Database
from scrapers.base_scraper import Job


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

# ----- form field selectors --------------------------------------------------
# Best-effort patterns that cover most job-board apply forms. They are tried
# in order; the first match wins. Selectors are case-insensitive via [name*=].
TEXT_FIELD_SELECTORS: dict[str, list[str]] = {
    "name": [
        "input[name*='name' i]:not([name*='user' i]):not([name*='company' i])",
        "input[id*='name' i]:not([id*='user' i]):not([id*='company' i])",
        "input[placeholder*='name' i]",
    ],
    "email": [
        "input[type='email']",
        "input[name*='email' i]",
        "input[id*='email' i]",
    ],
    "phone": [
        "input[type='tel']",
        "input[name*='phone' i]",
        "input[name*='mobile' i]",
        "input[id*='phone' i]",
    ],
    "experience": [
        "input[name*='experience' i]",
        "input[name*='years' i]",
        "input[id*='experience' i]",
    ],
    "location": [
        "input[name*='location' i]",
        "input[name*='city' i]",
    ],
    "linkedin": [
        "input[name*='linkedin' i]",
        "input[name*='profile' i]",
    ],
}

COVER_LETTER_TEXTAREA_SELECTORS = [
    "textarea[name*='cover' i]",
    "textarea[id*='cover' i]",
    "textarea[name*='letter' i]",
    "textarea[name*='message' i]",
    "textarea[placeholder*='cover' i]",
    "textarea[placeholder*='why' i]",
]

RESUME_FILE_SELECTORS = [
    "input[type='file'][name*='resume' i]",
    "input[type='file'][name*='cv' i]",
    "input[type='file'][id*='resume' i]",
    "input[type='file'][id*='cv' i]",
]

COVER_LETTER_FILE_SELECTORS = [
    "input[type='file'][name*='cover' i]",
    "input[type='file'][name*='letter' i]",
    "input[type='file'][id*='cover' i]",
]

GENERIC_FILE_SELECTOR = "input[type='file']"

SUBMIT_BUTTON_SELECTORS = [
    "button[type='submit']:has-text('Submit application')",
    "button[type='submit']:has-text('Submit Application')",
    "button[type='submit']:has-text('Apply now')",
    "button[type='submit']:has-text('Apply Now')",
    "button[type='submit']:has-text('Submit')",
    "button[type='submit']:has-text('Apply')",
    "button:has-text('Submit application')",
    "button:has-text('Apply now')",
    "button:has-text('Submit')",
    "button:has-text('Apply')",
    "input[type='submit']",
]


# =============================================================================
# Helpers
# =============================================================================
def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _row_to_job(row: dict[str, Any]) -> Job:
    """Turn a `db.jobs` row dict into our normalized `Job` dataclass."""
    return Job(
        title=row.get("title") or "",
        company=row.get("company") or "",
        location=row.get("location") or "",
        job_type=row.get("job_type") or "",
        platform=row.get("platform") or "",
        url=row.get("url") or "",
        description=row.get("description") or "",
        date_scraped=row.get("date_scraped") or "",
    )


def _estimate_experience_years(profile: dict[str, Any]) -> str:
    """Cheap heuristic so we can populate a 'years of experience' field.

    If config doesn't carry a number, count the experience entries. Returns
    a stringified integer (forms usually want a plain number).
    """
    explicit = profile.get("experience_years") or profile.get("years_of_experience")
    if explicit:
        return str(explicit)
    exp = profile.get("experience") or []
    return str(len(exp)) if exp else "1"


# =============================================================================
# Apply Agent
# =============================================================================
class ApplyAgent:
    """Orchestrates the full per-job apply flow."""

    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        profile_slug: str | None = None,
    ) -> None:
        load_dotenv(PROJECT_ROOT / ".env")

        self.config_path = Path(config_path)
        self.config: dict[str, Any] = _load_yaml(self.config_path)

        # CLI override > config.user.active_profile > legacy single-file mode
        self.profile_slug: str | None = resolve_active_slug(
            self.config, override=profile_slug
        )
        self.user_profile: dict[str, Any] = load_profile(
            slug=self.profile_slug, config=self.config
        )

        self.min_score: int = int(
            (self.config.get("job_search") or {}).get("min_match_score", 60)
        )
        automation_cfg = self.config.get("automation") or {}
        # NOTE: the apply flow always shows the browser so the user can review
        # before submitting. We expose `self.headless` for completeness but it
        # is intentionally NOT honored during `_fill_and_submit`.
        self.headless: bool = bool(automation_cfg.get("headless", False))
        self.apply_delay: int = int(automation_cfg.get("apply_delay_seconds", 30))
        self.auto_apply_enabled: bool = bool(automation_cfg.get("auto_apply", False))

        self.db = Database()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def apply_to_job(self, job_id: int) -> int | None:
        """Run the full apply pipeline for a single job. Returns the new
        application row id (or None if skipped / failed)."""

        # ---- 1. Fetch the job row -------------------------------------
        job_row = self.db.get_job(job_id)
        if not job_row:
            print(f"[apply] job_id={job_id} not found in DB; skipping.")
            return None
        job = _row_to_job(job_row)
        print(f"\n{'=' * 70}\n[apply] {job.title} @ {job.company} ({job.platform})")
        print(f"        {job.url}")
        print(f"        auto_apply={self.auto_apply_enabled}")
        print("=" * 70)

        # ---- 2. JD analysis (use cache if we've seen this job) --------
        cached = self.db.get_jd_analysis(job_id)
        if cached:
            jd_analysis = cached
            print("[apply] reusing cached JD analysis from DB.")
        else:
            print("[apply] analyzing JD via Gemini...")
            try:
                jd_analysis = analyze_jd(job.description or job.title)
            except Exception as exc:
                print(f"[apply] JD analysis failed: {exc}; skipping job.")
                return None
            self.db.insert_jd_analysis(job_id, jd_analysis)

        # ---- 3. Match scoring -----------------------------------------
        match = compute_match(self.user_profile, jd_analysis)
        score = int(match["match_score"])
        self.db.update_match_score(job_id, score)
        print(
            f"[apply] match score: {score}/100 ({match['recommendation']})\n"
            f"        matched : {match['matched_skills']}\n"
            f"        missing : {match['missing_skills']}"
        )
        if score < self.min_score:
            print(
                f"[apply] score {score} < min_match_score "
                f"({self.min_score}); skipping job."
            )
            self.db.update_job_status(job_id, "skipped_low_score")
            return None

        # ---- 4. Tailor the resume -------------------------------------
        print("[apply] generating tailored resume PDF...")
        try:
            _, resume_pdf, _ = customize_resume(
                self.user_profile,
                jd_analysis,
                company=job.company,
                role=job.title,
                job=job,
            )
        except Exception as exc:
            print(f"[apply] resume generation failed: {exc}; skipping job.")
            return None
        print(f"        -> {resume_pdf}")

        # ---- 5. Generate cover letter ---------------------------------
        print("[apply] generating cover letter PDF...")
        try:
            letter_text, letter_pdf = generate_cover_letter(
                self.user_profile, job, jd_analysis
            )
        except Exception as exc:
            print(f"[apply] cover letter generation failed: {exc}; skipping job.")
            return None
        print(f"        -> {letter_pdf}")

        # ---- 6. Drive the browser, fill the form, wait for human ------
        submit_outcome = self._fill_and_submit(
            job, resume_pdf, letter_pdf, letter_text
        )

        # ---- 7. Record application ------------------------------------
        if submit_outcome == "skipped":
            print("[apply] user skipped this job; not recording an application.")
            return None
        if submit_outcome == "aborted":
            print("[apply] browser pipeline aborted; not recording an application.")
            return None

        app_id = self.db.insert_application(
            {
                "job_id": job_id,
                "resume_version": str(resume_pdf),
                "cover_letter_path": str(letter_pdf),
                "status": "Applied",
                "notes": (
                    f"Match score: {score}/100; "
                    f"submit={submit_outcome}; "
                    f"matched: {', '.join(match['matched_skills'])}"
                ),
            }
        )
        print(f"[apply] recorded application id={app_id} (submit={submit_outcome}).")
        return app_id

    def run_batch(
        self, min_score: int | None = None, limit: int = 10
    ) -> dict[str, int]:
        """Apply to up to `limit` jobs whose match_score >= min_score and
        whose status == 'new'. Returns a small summary dict."""

        score_floor = self.min_score if min_score is None else int(min_score)
        # Score any unscored 'new' jobs first so we have something to filter on.
        self._score_new_jobs()

        candidates = [
            j for j in self.db.get_jobs(status="new", min_score=score_floor)
        ][:limit]

        print(
            f"\n[batch] {len(candidates)} job(s) queued "
            f"(min_score={score_floor}, limit={limit}, "
            f"delay={self.apply_delay}s)"
        )

        applied = 0
        skipped = 0
        failed = 0
        for idx, row in enumerate(candidates, start=1):
            print(f"\n[batch] ---- {idx}/{len(candidates)} ----")
            try:
                app_id = self.apply_to_job(int(row["id"]))
            except KeyboardInterrupt:
                print("\n[batch] interrupted by user; stopping batch.")
                break
            except Exception as exc:
                print(f"[batch] unexpected error on job {row['id']}: {exc}")
                failed += 1
                continue

            if app_id is None:
                skipped += 1
            else:
                applied += 1

            if idx < len(candidates):
                print(f"[batch] sleeping {self.apply_delay}s before next job...")
                try:
                    time.sleep(self.apply_delay)
                except KeyboardInterrupt:
                    print("\n[batch] interrupted during delay; stopping batch.")
                    break

        summary = {"applied": applied, "skipped": skipped, "failed": failed}
        print(f"\n[batch] done. summary={summary}")
        return summary

    # ------------------------------------------------------------------
    # Score-only pre-pass (so run_batch has match_score values to filter on)
    # ------------------------------------------------------------------
    def _score_new_jobs(self) -> int:
        """Run JD analysis + matcher for any 'new' job that has no match_score yet."""
        new_jobs = [
            j for j in self.db.get_jobs(status="new")
            if j.get("match_score") in (None, "")
        ]
        if not new_jobs:
            return 0
        print(f"[score] pre-scoring {len(new_jobs)} unscored job(s)...")
        scored = 0
        for row in new_jobs:
            job_id = int(row["id"])
            job = _row_to_job(row)
            try:
                cached = self.db.get_jd_analysis(job_id)
                jd_analysis = cached or analyze_jd(job.description or job.title)
                if not cached:
                    self.db.insert_jd_analysis(job_id, jd_analysis)
                match = compute_match(self.user_profile, jd_analysis)
                self.db.update_match_score(job_id, int(match["match_score"]))
                scored += 1
            except Exception as exc:
                print(f"[score] job_id={job_id} failed: {exc}")
        return scored

    # ------------------------------------------------------------------
    # The browser piece
    # ------------------------------------------------------------------
    def _fill_and_submit(
        self,
        job: Job,
        resume_path: Path,
        cover_letter_path: Path,
        cover_letter_text: str,
    ) -> str:
        """Open Playwright, best-effort fill the form, pause for review, submit.

        Returns one of:
            "auto_submitted" - auto_apply=True and the submit button click landed.
            "user_confirmed" - user confirmed they submitted manually.
            "skipped"        - user explicitly skipped this job.
            "aborted"        - browser/automation error or missing dependency.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(
                "[apply] playwright is not installed. "
                "Run: pip install playwright && playwright install"
            )
            return "aborted"

        resume_path = Path(resume_path)
        cover_letter_path = Path(cover_letter_path)

        try:
            with sync_playwright() as p:
                # Hard-coded headless=False so the user can see the form and
                # review every field before pressing ENTER to submit.
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(
                    viewport={"width": 1366, "height": 800},
                    accept_downloads=True,
                )
                page = context.new_page()

                # ----- navigate ---------------------------------------
                print(f"[browser] opening {job.url}")
                try:
                    page.goto(job.url, timeout=45_000)
                    page.wait_for_load_state("domcontentloaded")
                except Exception as exc:
                    print(f"[browser] navigation error: {exc}")

                # Many job boards open a separate "Apply" pane / modal.
                # Click the most obvious apply trigger if one is visible.
                self._click_first_visible(
                    page,
                    [
                        "button:has-text('Easy Apply')",
                        "a:has-text('Easy Apply')",
                        "button:has-text('Apply now')",
                        "button:has-text('Apply Now')",
                        "a:has-text('Apply now')",
                        "button:has-text('Apply')",
                        "a:has-text('Apply')",
                    ],
                    description="apply trigger",
                )
                time.sleep(2)

                # ----- fill text fields -------------------------------
                fill_values: dict[str, str] = {
                    "name": str(self.user_profile.get("name") or ""),
                    "email": str(self.user_profile.get("email") or ""),
                    "phone": str(self.user_profile.get("phone") or ""),
                    "experience": _estimate_experience_years(self.user_profile),
                    "location": str(self.user_profile.get("location") or ""),
                    "linkedin": str(self.user_profile.get("linkedin") or ""),
                }
                for field_name, value in fill_values.items():
                    if not value:
                        continue
                    selectors = TEXT_FIELD_SELECTORS.get(field_name, [])
                    if self._fill_first_visible(page, selectors, value):
                        print(f"[browser] filled {field_name}={value!r}")
                    else:
                        print(f"[browser] no selector matched for {field_name}")

                # ----- cover letter textarea --------------------------
                if self._fill_first_visible(
                    page, COVER_LETTER_TEXTAREA_SELECTORS, cover_letter_text
                ):
                    print("[browser] pasted cover-letter text into textarea.")

                # ----- file inputs (resume + cover letter) ------------
                # Try the specific selectors first; fall back to the first
                # generic file input for the resume.
                if not self._upload_first_visible(
                    page, RESUME_FILE_SELECTORS, resume_path
                ):
                    if self._upload_first_visible(
                        page, [GENERIC_FILE_SELECTOR], resume_path
                    ):
                        print("[browser] uploaded resume via generic file input.")
                else:
                    print("[browser] uploaded resume via named file input.")

                if self._upload_first_visible(
                    page, COVER_LETTER_FILE_SELECTORS, cover_letter_path
                ):
                    print("[browser] uploaded cover letter via named file input.")

                # ----- pause for human review --------------------------
                print("\n" + "=" * 70)
                print("[browser] Form pre-filled. Switch to the browser window,")
                print("          review every field, fix anything the agent missed,")
                print("          then come back here.")
                print("=" * 70)

                try:
                    if self.auto_apply_enabled:
                        input(
                            "auto_apply=True. Press ENTER to let the agent click "
                            "Submit (or Ctrl+C to skip)  "
                        )
                    else:
                        input(
                            "auto_apply=False. Submit MANUALLY in the browser, then "
                            "press ENTER (or Ctrl+C to skip)  "
                        )
                except KeyboardInterrupt:
                    print("\n[browser] user skipped this job.")
                    return "skipped"

                outcome: str
                if self.auto_apply_enabled:
                    clicked = self._click_first_visible(
                        page,
                        SUBMIT_BUTTON_SELECTORS,
                        description="submit button",
                    )
                    if clicked:
                        print("[browser] submit clicked.")
                        time.sleep(5)
                        outcome = "auto_submitted"
                    else:
                        print(
                            "[browser] could not find a submit button to click.\n"
                            "          Submit the form manually in the browser "
                            "if you want this run recorded."
                        )
                        outcome = self._prompt_user_confirmed_submit()
                else:
                    outcome = self._prompt_user_confirmed_submit()

                browser.close()
                return outcome
        except Exception as exc:
            print(f"[browser] aborted: {exc}")
            return "aborted"

    @staticmethod
    def _prompt_user_confirmed_submit() -> str:
        """Ask the user whether they actually clicked submit. Returns
        'user_confirmed' on yes, 'skipped' on anything else / Ctrl+C."""
        try:
            answer = input(
                "Did you submit the application? (y/N)  "
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return "skipped"
        return "user_confirmed" if answer in {"y", "yes"} else "skipped"

    # ---- Playwright helpers ------------------------------------------
    @staticmethod
    def _first_visible(page, selectors: list[str]):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                # Use wait_for(state='visible') with a short timeout instead of
                # the deprecated is_visible(timeout=...) kwarg.
                try:
                    loc.wait_for(state="visible", timeout=1000)
                except Exception:
                    continue
                return loc, sel
            except Exception:
                continue
        return None, None

    def _fill_first_visible(self, page, selectors: list[str], value: str) -> bool:
        if not value:
            return False
        loc, _ = self._first_visible(page, selectors)
        if loc is None:
            return False
        try:
            loc.fill(str(value), timeout=3000)
            return True
        except Exception:
            return False

    def _upload_first_visible(self, page, selectors: list[str], path: Path) -> bool:
        if not Path(path).exists():
            return False
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.set_input_files(str(path), timeout=3000)
                return True
            except Exception:
                continue
        return False

    def _click_first_visible(
        self, page, selectors: list[str], description: str = "element"
    ) -> bool:
        loc, matched = self._first_visible(page, selectors)
        if loc is None:
            return False
        try:
            loc.click(timeout=3000)
            print(f"[browser] clicked {description} via {matched!r}")
            return True
        except Exception as exc:
            print(f"[browser] click failed on {description}: {exc}")
            return False


# =============================================================================
# CLI
# =============================================================================
def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the AI job apply agent.")
    parser.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="Apply to a single job by DB id (skips batch mode).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Override config min_match_score for this run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max jobs to attempt in batch mode.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Named profile slug (config/profiles/<slug>.json).",
    )
    args = parser.parse_args()

    agent = ApplyAgent(profile_slug=args.profile)

    print(
        f"\nApplyAgent ready.\n"
        f"  Candidate : {agent.user_profile.get('name') or '(unknown)'}\n"
        f"  Profile   : {agent.profile_slug or '(default)'}\n"
        f"  Min score : {agent.min_score}\n"
        f"  Headless  : {agent.headless}\n"
        f"  Delay     : {agent.apply_delay}s between jobs\n"
    )

    if args.job_id is not None:
        agent.apply_to_job(args.job_id)
        return 0

    agent.run_batch(min_score=args.min_score, limit=args.limit)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
