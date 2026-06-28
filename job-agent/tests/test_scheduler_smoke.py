"""Smoke tests for the background job scrape scheduler."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from db.database import Database
from scrapers.scheduler import JobScrapeScheduler, _parse_auto_scrape_config
from scrapers.scraper_manager import ScrapeRunResult, PlatformScrapeStats


class SchedulerConfigTests(unittest.TestCase):
    def test_parse_auto_scrape_defaults(self) -> None:
        cfg = _parse_auto_scrape_config({})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["interval_minutes"], 30)
        self.assertEqual(cfg["startup_delay_seconds"], 15)

    def test_parse_auto_scrape_disabled(self) -> None:
        cfg = _parse_auto_scrape_config({
            "job_search": {"auto_scrape": {"enabled": False}},
        })
        self.assertFalse(cfg["enabled"])


class SchedulerSmokeTests(unittest.TestCase):
    def test_import_scheduler(self) -> None:
        from scrapers.scheduler import JobScrapeScheduler  # noqa: F401

    def test_start_without_crash_when_disabled(self) -> None:
        config = {"job_search": {"auto_scrape": {"enabled": False}}}
        sched = JobScrapeScheduler(config=config)
        sched.start()
        self.assertIsNone(sched._thread)

    @patch("scrapers.scheduler.ScraperManager")
    def test_run_once_mock_scrape(self, mock_mgr_cls) -> None:
        tmp = Path(tempfile.gettempdir()) / "job_agent_scheduler_test.db"
        if tmp.exists():
            tmp.unlink()
        db = Database(tmp)

        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.run.return_value = ScrapeRunResult(
            total_found=3,
            new_jobs=2,
            platforms={
                "indeed": PlatformScrapeStats(platform="indeed", found=3, new=2),
            },
        )

        config = {
            "job_search": {
                "auto_scrape": {
                    "enabled": True,
                    "score_new_jobs": False,
                },
            },
        }
        sched = JobScrapeScheduler(config=config, db=db)
        result = sched.run_once(trigger="test")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["new_jobs"], 2)
        latest = db.get_latest_scrape_run()
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["status"], "completed")
        self.assertEqual(latest["new_jobs"], 2)
        tmp.unlink(missing_ok=True)

    @patch("scrapers.scheduler.ScraperManager")
    def test_scheduler_thread_starts(self, mock_mgr_cls) -> None:
        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.run.return_value = ScrapeRunResult()

        config = {
            "job_search": {
                "auto_scrape": {
                    "enabled": True,
                    "startup_delay_seconds": 0,
                    "interval_minutes": 60,
                    "score_new_jobs": False,
                },
            },
        }
        sched = JobScrapeScheduler(config=config)
        sched.start()
        self.assertIsNotNone(sched._thread)
        deadline = time.time() + 5
        while time.time() < deadline:
            if sched._last_status.get("trigger") == "startup":
                break
            time.sleep(0.1)
        sched.stop()
        self.assertIn(
            sched._last_status.get("trigger"),
            {"startup", "manual", "scheduled", "test", None},
        )


if __name__ == "__main__":
    unittest.main()
