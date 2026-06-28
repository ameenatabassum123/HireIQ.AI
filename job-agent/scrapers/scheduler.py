"""Background job-scrape scheduler for the web app.

Runs enabled platform scrapers on a configurable interval (default 30 minutes),
logs results to SQLite, and optionally scores new jobs against the active profile.

Some platforms (LinkedIn, Indeed, Glassdoor) may block or rate-limit headless
scraping — failures are logged per platform without stopping the scheduler.

Usage (started automatically via ``web.server`` lifespan):
    from scrapers.scheduler import JobScrapeScheduler
    scheduler = JobScrapeScheduler()
    scheduler.start()
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ai.profile_store import load_profile, resolve_active_slug
from db.database import Database
from scrapers.scraper_manager import ScraperManager, ScrapeRunResult, load_config


PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("job_scrape_scheduler")

_scheduler_instance: "JobScrapeScheduler | None" = None
_instance_lock = threading.Lock()


def _parse_auto_scrape_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize auto-scrape settings from ``config.yaml``."""
    js = config.get("job_search") or {}
    auto = js.get("auto_scrape") or {}

    def _bool(val: Any, default: bool) -> bool:
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in {"1", "true", "yes", "on"}

    interval = auto.get("interval_minutes")
    if interval is None:
        interval = js.get("auto_scrape_interval_minutes", 30)

    return {
        "enabled": _bool(auto.get("enabled", js.get("auto_scrape_enabled")), True),
        "interval_minutes": max(1, int(interval or 30)),
        "startup_delay_seconds": max(0, int(auto.get("startup_delay_seconds", 15))),
        "parallel": _bool(auto.get("parallel"), True),
        "max_workers": max(1, int(auto.get("max_workers", 3))),
        "score_new_jobs": _bool(auto.get("score_new_jobs"), True),
        "manual_cooldown_minutes": max(1, int(auto.get("manual_cooldown_minutes", 5))),
        "new_badge_minutes": max(1, int(auto.get("new_badge_minutes", 30))),
    }


class JobScrapeScheduler:
    """Daemon thread that scrapes enabled platforms on a fixed interval."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        db: Database | None = None,
    ) -> None:
        self.config = config if config is not None else load_config()
        self.db = db or Database()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._config_lock = threading.Lock()
        self._is_running = False
        self._last_manual_at: float = 0.0
        self._next_scrape_at: float | None = None
        self._last_status: dict[str, Any] = {}

    @classmethod
    def get_instance(cls, config: dict[str, Any] | None = None) -> "JobScrapeScheduler":
        global _scheduler_instance
        with _instance_lock:
            if _scheduler_instance is None:
                _scheduler_instance = cls(config=config)
            elif config is not None:
                with _scheduler_instance._config_lock:
                    _scheduler_instance.config = config
            return _scheduler_instance

    def _cfg(self) -> dict[str, Any]:
        with self._config_lock:
            return _parse_auto_scrape_config(self.config)

    def start(self) -> None:
        """Start the background scheduler thread (no-op if disabled or already running)."""
        cfg = self._cfg()
        if not cfg["enabled"]:
            logger.info("Auto-scrape is disabled in config (job_search.auto_scrape.enabled: false)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="job-scrape-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Job scrape scheduler started (every %s min, initial delay %ss)",
            cfg["interval_minutes"],
            cfg["startup_delay_seconds"],
        )

    def stop(self) -> None:
        """Signal the scheduler thread to stop."""
        self._stop.set()

    def _loop(self) -> None:
        cfg = self._cfg()
        delay = cfg["startup_delay_seconds"]
        if delay and self._stop.wait(delay):
            return
        # self.run_once(trigger="startup")
        interval_sec = cfg["interval_minutes"] * 60
        while not self._stop.is_set():
            self._next_scrape_at = time.time() + interval_sec
            if self._stop.wait(interval_sec):
                break
            self.run_once(trigger="scheduled")

    def run_once(self, trigger: str = "manual") -> dict[str, Any]:
        """Execute one scrape cycle. Returns status payload."""
        if not self._run_lock.acquire(blocking=False):
            payload = {
                "status": "skipped",
                "reason": "scrape already in progress",
                "is_running": True,
            }
            self._last_status = payload
            return payload

        self._is_running = True
        run_id = 0
        cfg = self._cfg()
        try:
            run_id = self.db.insert_scrape_run(trigger=trigger)
            logger.info("Scrape run #%s started (trigger=%s)", run_id, trigger)

            mgr = ScraperManager(config=self.config, db=self.db)
            result = mgr.run(
                parallel=cfg["parallel"],
                max_workers=cfg["max_workers"],
                quiet=True,
            )

            scored = 0
            if cfg["score_new_jobs"] and result.new_jobs > 0:
                scored = self._score_new_jobs()

            status = "completed" if not result.errors else "completed_with_errors"
            self.db.finish_scrape_run(
                run_id,
                status=status,
                new_jobs=result.new_jobs,
                total_found=result.total_found,
                platforms=result.to_dict()["platforms"],
                error="; ".join(result.errors) if result.errors else None,
            )

            for plat, stats in result.platforms.items():
                if stats.error:
                    logger.error(
                        "[%s] scrape error: %s", plat, stats.error
                    )
                else:
                    logger.info(
                        "[%s] found=%s new=%s duplicates=%s filtered=%s invalid=%s errors=%s",
                        plat,
                        stats.found,
                        stats.new,
                        stats.duplicates,
                        stats.filtered,
                        stats.invalid,
                        stats.exceptions,
                    )

            payload = {
                "status": status,
                "run_id": run_id,
                "trigger": trigger,
                "new_jobs": result.new_jobs,
                "total_found": result.total_found,
                "scored": scored,
                "platforms": result.to_dict()["platforms"],
                "errors": result.errors,
                "is_running": False,
            }
            logger.info(
                "Scrape run #%s finished: %s new / %s unique (scored %s)",
                run_id,
                result.new_jobs,
                result.total_found,
                scored,
            )
            self._last_status = payload
            return payload

        except Exception as exc:
            logger.exception("Scrape run failed: %s", exc)
            if run_id:
                self.db.finish_scrape_run(
                    run_id,
                    status="failed",
                    error=str(exc),
                )
            payload = {
                "status": "failed",
                "run_id": run_id,
                "trigger": trigger,
                "error": str(exc),
                "is_running": False,
            }
            self._last_status = payload
            return payload
        finally:
            self._is_running = False
            self._run_lock.release()
            cfg = self._cfg()
            self._next_scrape_at = time.time() + cfg["interval_minutes"] * 60

    def _score_new_jobs(self) -> int:
        """Score unscored jobs using the default/active profile."""
        slug = resolve_active_slug(self.config)
        profile = load_profile(slug=slug, config=self.config)
        has_skills = any(
            profile.get(k)
            for k in ("skills", "tools", "programming_languages", "frameworks", "experience")
        )
        if not has_skills:
            logger.info("Skipping match scoring — no parsed profile with skills")
            return 0
        try:
            from automation.apply_agent import ApplyAgent

            agent = ApplyAgent(profile_slug=slug)
            return agent._score_new_jobs()
        except Exception as exc:
            logger.warning("Match scoring after scrape failed: %s", exc)
            return 0

    def can_manual_trigger(self) -> tuple[bool, str | None]:
        """Return whether a manual scrape is allowed (rate limit)."""
        cfg = self._cfg()
        if self._is_running:
            return False, "A scrape is already running."
        cooldown = cfg["manual_cooldown_minutes"] * 60
        elapsed = time.time() - self._last_manual_at
        if self._last_manual_at and elapsed < cooldown:
            wait = int(cooldown - elapsed)
            return False, f"Please wait {wait // 60}m {wait % 60}s before refreshing again."
        return True, None

    def trigger_manual(self) -> dict[str, Any]:
        """Run a manual scrape if rate limit allows."""
        ok, reason = self.can_manual_trigger()
        if not ok:
            return {"status": "rate_limited", "error": reason, "is_running": self._is_running}
        self._last_manual_at = time.time()
        return self.run_once(trigger="manual")

    def get_status(self) -> dict[str, Any]:
        """Return scheduler status for API / UI."""
        cfg = self._cfg()
        last_run = self.db.get_last_completed_scrape_run()
        latest = self.db.get_latest_scrape_run()
        now = time.time()

        last_scrape_at = None
        minutes_since = None
        if last_run and last_run.get("finished_at"):
            last_scrape_at = last_run["finished_at"]
            try:
                finished = datetime.fromisoformat(last_scrape_at)
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
                minutes_since = int((datetime.now(timezone.utc) - finished).total_seconds() // 60)
            except ValueError:
                minutes_since = None

        next_in_minutes = None
        if self._next_scrape_at and cfg["enabled"]:
            next_in_minutes = max(0, int((self._next_scrape_at - now) // 60))

        return {
            "enabled": cfg["enabled"],
            "is_running": self._is_running,
            "interval_minutes": cfg["interval_minutes"],
            "last_scrape_at": last_scrape_at,
            "minutes_since_last_scrape": minutes_since,
            "next_scrape_in_minutes": next_in_minutes,
            "new_badge_minutes": cfg["new_badge_minutes"],
            "latest_run": {
                "id": latest.get("id") if latest else None,
                "status": latest.get("status") if latest else None,
                "trigger": latest.get("trigger") if latest else None,
                "new_jobs": latest.get("new_jobs") if latest else 0,
                "started_at": latest.get("started_at") if latest else None,
                "finished_at": latest.get("finished_at") if latest else None,
            },
            "last_result": self._last_status,
        }


def reload_scheduler_config(config: dict[str, Any]) -> JobScrapeScheduler:
    """Update the singleton scheduler config (e.g. after config reload)."""
    sched = JobScrapeScheduler.get_instance(config)
    sched.config = config
    return sched
