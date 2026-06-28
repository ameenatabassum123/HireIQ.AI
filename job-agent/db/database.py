"""SQLite persistence layer for the AI Job Agent.

Schema
------
1. jobs              - one row per scraped job posting (URL is unique)
2. applications      - one row per application attempt (FK -> jobs.id)
3. jd_analysis       - structured JD requirements (FK -> jobs.id, 1:1)

All write operations go through a single context manager (`_conn`) that
commits on success and rolls back on exceptions. JSON list fields are
serialized to TEXT with `json.dumps` and re-hydrated with `json.loads`.

Usage:
    from db.database import Database
    db = Database()
    job_id = db.insert_job({...})
    db.update_match_score(job_id, 82)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "db" / "jobs.db"

APPLICATION_STATUSES = {"Applied", "Interview", "Offer", "Rejected"}

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    company       TEXT,
    location      TEXT,
    job_type      TEXT,
    platform      TEXT,
    url           TEXT NOT NULL UNIQUE,
    description   TEXT,
    date_scraped  TEXT NOT NULL,
    match_score   INTEGER,
    status        TEXT NOT NULL DEFAULT 'new'
);
"""

APPLICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             INTEGER NOT NULL,
    date_applied       TEXT NOT NULL,
    resume_version     TEXT,
    cover_letter_path  TEXT,
    status             TEXT NOT NULL DEFAULT 'Applied',
    notes              TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
"""

JD_ANALYSIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jd_analysis (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id               INTEGER NOT NULL UNIQUE,
    required_skills      TEXT NOT NULL DEFAULT '[]',
    preferred_skills     TEXT NOT NULL DEFAULT '[]',
    tools                TEXT NOT NULL DEFAULT '[]',
    keywords             TEXT NOT NULL DEFAULT '[]',
    experience_required  TEXT,
    seniority_level      TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
"""

MATCH_EXPLANATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS match_explanations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id               INTEGER NOT NULL,
    profile_slug         TEXT NOT NULL DEFAULT '',
    explanation          TEXT NOT NULL,
    match_score_snapshot INTEGER,
    updated_at           TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
    UNIQUE(job_id, profile_slug)
);
"""

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name          TEXT,
    created_at    TEXT NOT NULL
);
"""

USER_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id         INTEGER PRIMARY KEY,
    active_profile  TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""

SCRAPE_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    trigger       TEXT NOT NULL DEFAULT 'scheduled',
    status        TEXT NOT NULL DEFAULT 'running',
    new_jobs      INTEGER NOT NULL DEFAULT 0,
    total_found   INTEGER NOT NULL DEFAULT 0,
    platforms_json TEXT NOT NULL DEFAULT '{}',
    error         TEXT
);
"""

USER_RESUMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_resumes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    slug          TEXT NOT NULL,
    filename      TEXT,
    pdf_blob      BLOB,
    profile_json  TEXT,
    parse_status  TEXT NOT NULL DEFAULT 'pdf_only',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(user_id, slug)
);
"""

RESUME_PARSE_STATUSES = {"parsed", "pdf_only", "partial", "failed"}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_platform   ON jobs(platform);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_match      ON jobs(match_score);",
    "CREATE INDEX IF NOT EXISTS idx_apps_job_id     ON applications(job_id);",
    "CREATE INDEX IF NOT EXISTS idx_apps_status     ON applications(status);",
    "CREATE INDEX IF NOT EXISTS idx_match_exp_job   ON match_explanations(job_id);",
    "CREATE INDEX IF NOT EXISTS idx_jd_seniority    ON jd_analysis(seniority_level);",
    "CREATE INDEX IF NOT EXISTS idx_users_email     ON users(email);",
    "CREATE INDEX IF NOT EXISTS idx_user_resumes_user ON user_resumes(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_user_resumes_slug ON user_resumes(user_id, slug);",
    "CREATE INDEX IF NOT EXISTS idx_scrape_runs_started ON scrape_runs(started_at);",
]


def _now() -> str:
    """Current UTC time as an ISO-8601 string with explicit timezone (seconds resolution)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dumps(value: Any) -> str:
    """JSON-encode a list/dict, accepting None or scalar inputs."""
    if value is None:
        return "[]"
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            return json.dumps([value])
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class Database:
    """Thin SQLite wrapper for jobs / applications / jd_analysis."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ #
    # Connection / schema bootstrap
    # ------------------------------------------------------------------ #
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection that commits on success, rolls back on error."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(JOBS_SCHEMA)
            conn.execute(APPLICATIONS_SCHEMA)
            conn.execute(JD_ANALYSIS_SCHEMA)
            conn.execute(MATCH_EXPLANATIONS_SCHEMA)
            conn.execute(USERS_SCHEMA)
            conn.execute(USER_SETTINGS_SCHEMA)
            conn.execute(USER_RESUMES_SCHEMA)
            conn.execute(SCRAPE_RUNS_SCHEMA)
            self._migrate_legacy_jobs(conn)
            self._migrate_jd_analysis(conn)
            for stmt in INDEXES:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    # Older legacy `jobs` schemas may lack indexed columns even
                    # after migration; skip rather than crash.
                    pass

    @staticmethod
    def _migrate_legacy_jobs(conn: sqlite3.Connection) -> None:
        """Upgrade an older `jobs` schema (from the standalone `scrape.py`)
        in place: add any missing columns, backfill from legacy column names,
        and add a UNIQUE INDEX on `url`. Idempotent."""
        cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(jobs);").fetchall()
        }

        adds: list[tuple[str, str]] = [
            ("url", "TEXT"),
            ("job_type", "TEXT"),
            ("platform", "TEXT"),
            ("description", "TEXT"),
            ("date_scraped", "TEXT"),
            ("match_score", "INTEGER"),
            ("status", "TEXT NOT NULL DEFAULT 'new'"),
        ]
        for name, decl in adds:
            if name not in cols:
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl};")
                except sqlite3.OperationalError:
                    pass

        legacy_backfills = [
            ("url", "apply_url"),
            ("platform", "source"),
            ("description", "jd_text"),
            ("date_scraped", "scraped_at"),
        ]
        for new_col, old_col in legacy_backfills:
            if old_col in cols:
                try:
                    conn.execute(
                        f"UPDATE jobs SET {new_col} = {old_col} "
                        f"WHERE ({new_col} IS NULL OR {new_col} = '') "
                        f"AND {old_col} IS NOT NULL;"
                    )
                except sqlite3.OperationalError:
                    pass

        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_url "
                "ON jobs(url) WHERE url IS NOT NULL;"
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _migrate_jd_analysis(conn: sqlite3.Connection) -> None:
        """Add columns/tables introduced after initial schema. Idempotent."""
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(jd_analysis);").fetchall()
        }
        if "seniority_level" not in cols:
            try:
                conn.execute(
                    "ALTER TABLE jd_analysis ADD COLUMN seniority_level TEXT;"
                )
            except sqlite3.OperationalError:
                pass

        conn.execute(MATCH_EXPLANATIONS_SCHEMA)

    # ------------------------------------------------------------------ #
    # jobs
    # ------------------------------------------------------------------ #
    def insert_job(self, job: dict[str, Any]) -> int:
        """Insert a job. Returns the job's id; if the URL already exists,
        returns the id of the existing row instead (no error)."""
        job_id, _inserted = self.insert_job_with_status(job)
        return job_id

    @staticmethod
    def _jobs_table_columns(conn: sqlite3.Connection) -> set[str]:
        return {
            row["name"]
            for row in conn.execute("PRAGMA table_info(jobs);").fetchall()
        }

    @staticmethod
    def _build_job_insert_payload(job: dict[str, Any]) -> dict[str, Any]:
        """Map a normalized job dict to INSERT columns (incl. legacy schema)."""
        url = str(job.get("url") or "").strip()
        platform = str(job.get("platform") or "unknown").strip() or "unknown"
        scraped = job.get("date_scraped") or _now()
        description = job.get("description") or ""
        payload: dict[str, Any] = {
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
            "job_type": job.get("job_type"),
            "platform": platform,
            "url": url,
            "description": description,
            "date_scraped": scraped,
            "match_score": job.get("match_score"),
            "status": job.get("status", "new"),
            # Legacy scrape.py columns (NOT NULL on older DBs).
            "source": platform,
            "apply_url": url,
            "jd_text": description,
            "scraped_at": scraped,
            "role_query": job.get("role_query") or job.get("title") or "",
            "location_query": job.get("location_query") or job.get("location") or "",
            "job_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
        }
        return payload

    def insert_job_with_status(self, job: dict[str, Any]) -> tuple[int, bool]:
        """Insert a job. Returns ``(job_id, was_newly_inserted)``."""
        if not job.get("url"):
            raise ValueError("job_dict must include a non-empty 'url'.")
        if not job.get("title"):
            raise ValueError("job_dict must include a non-empty 'title'.")

        payload = self._build_job_insert_payload(job)

        with self._conn() as conn:
            cols = self._jobs_table_columns(conn)
            insert_cols = [c for c in payload if c in cols]
            placeholders = ", ".join(f":{c}" for c in insert_cols)
            col_list = ", ".join(insert_cols)
            cur = conn.execute(
                f"INSERT OR IGNORE INTO jobs ({col_list}) VALUES ({placeholders});",
                {k: payload[k] for k in insert_cols},
            )
            if cur.lastrowid and cur.rowcount > 0:
                return int(cur.lastrowid), True
            row = conn.execute(
                "SELECT id FROM jobs WHERE url = ?;", (payload["url"],)
            ).fetchone()
            if row is None and "apply_url" in cols:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE apply_url = ?;",
                    (payload["url"],),
                ).fetchone()
            return (int(row["id"]) if row else 0), False

    def get_jobs(
        self,
        status: str | None = None,
        min_score: int | None = None,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if min_score is not None:
            clauses.append("(match_score IS NOT NULL AND match_score >= ?)")
            params.append(min_score)
        if platform is not None:
            clauses.append("platform = ?")
            params.append(platform)

        sql = "SELECT * FROM jobs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY date_scraped DESC, id DESC;"

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_distinct_locations(self) -> list[str]:
        """Return sorted distinct non-empty job locations."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT location FROM jobs
                WHERE location IS NOT NULL AND TRIM(location) != ''
                ORDER BY location COLLATE NOCASE;
                """
            ).fetchall()
        return [str(r["location"]).strip() for r in rows]

    def _build_jobs_filter_sql(
        self,
        *,
        status: str | None = None,
        platform: str | None = None,
        platforms: list[str] | None = None,
        min_score: int | None = None,
        locations: list[str] | None = None,
        title_terms: list[str] | None = None,
        description_terms: list[str] | None = None,
        keyword_terms: list[str] | None = None,
        seniority_levels: list[str] | None = None,
        posted_after: str | None = None,
        posted_before: str | None = None,
        sort_by_match: bool = False,
    ) -> tuple[str, list[Any], bool]:
        """Build SELECT SQL + params for job filters. Returns (sql, params, join_jd)."""
        clauses: list[str] = []
        params: list[Any] = []
        join_jd = bool(seniority_levels)

        if status is not None:
            clauses.append("j.status = ?")
            params.append(status)
        if platform is not None:
            clauses.append("j.platform = ?")
            params.append(platform)
        if platforms:
            placeholders = ", ".join("?" * len(platforms))
            clauses.append(f"j.platform IN ({placeholders})")
            params.extend(platforms)
        if min_score is not None:
            clauses.append("(j.match_score IS NOT NULL AND j.match_score >= ?)")
            params.append(min_score)
        if locations:
            placeholders = ", ".join("?" * len(locations))
            clauses.append(f"j.location IN ({placeholders})")
            params.extend(locations)
        if posted_after:
            clauses.append("j.date_scraped >= ?")
            params.append(posted_after)
        if posted_before:
            clauses.append("j.date_scraped <= ?")
            params.append(posted_before)
        if title_terms:
            or_parts = []
            for term in title_terms:
                or_parts.append("LOWER(j.title) LIKE ?")
                params.append(f"%{term.strip().lower()}%")
            clauses.append("(" + " OR ".join(or_parts) + ")")
        if description_terms:
            or_parts = []
            for term in description_terms:
                or_parts.append("LOWER(COALESCE(j.description, '')) LIKE ?")
                params.append(f"%{term.strip().lower()}%")
            clauses.append("(" + " OR ".join(or_parts) + ")")
        if keyword_terms:
            field_exprs = (
                "LOWER(j.title)",
                "LOWER(COALESCE(j.company, ''))",
                "LOWER(COALESCE(j.description, ''))",
                "LOWER(COALESCE(j.platform, ''))",
            )
            or_parts = []
            for term in keyword_terms:
                needle = term.strip().lower()
                if not needle:
                    continue
                field_or = " OR ".join(f"{expr} LIKE ?" for expr in field_exprs)
                or_parts.append(f"({field_or})")
                params.extend([f"%{needle}%"] * len(field_exprs))
            if or_parts:
                clauses.append("(" + " OR ".join(or_parts) + ")")
        if seniority_levels:
            placeholders = ", ".join("?" * len(seniority_levels))
            clauses.append(f"jd.seniority_level IN ({placeholders})")
            params.extend(seniority_levels)

        sql = "SELECT j.* FROM jobs j"
        if join_jd:
            sql += " LEFT JOIN jd_analysis jd ON jd.job_id = j.id"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if sort_by_match:
            sql += (
                " ORDER BY (j.match_score IS NULL), j.match_score DESC, "
                "j.date_scraped DESC, j.id DESC"
            )
        else:
            sql += " ORDER BY j.date_scraped DESC, j.id DESC"
        return sql, params, join_jd

    def count_jobs_filtered(
        self,
        *,
        status: str | None = None,
        platform: str | None = None,
        platforms: list[str] | None = None,
        min_score: int | None = None,
        locations: list[str] | None = None,
        title_terms: list[str] | None = None,
        description_terms: list[str] | None = None,
        keyword_terms: list[str] | None = None,
        seniority_levels: list[str] | None = None,
        posted_after: str | None = None,
        posted_before: str | None = None,
        sort_by_match: bool = False,
    ) -> int:
        """Count jobs matching the same filters as ``get_jobs_filtered``."""
        sql, params, join_jd = self._build_jobs_filter_sql(
            status=status,
            platform=platform,
            platforms=platforms,
            min_score=min_score,
            locations=locations,
            title_terms=title_terms,
            description_terms=description_terms,
            keyword_terms=keyword_terms,
            seniority_levels=seniority_levels,
            posted_after=posted_after,
            posted_before=posted_before,
            sort_by_match=sort_by_match,
        )
        count_sql = sql.replace("SELECT j.*", "SELECT COUNT(*) AS c", 1)
        count_sql = count_sql.split(" ORDER BY ")[0]
        with self._conn() as conn:
            row = conn.execute(count_sql, params).fetchone()
        return int(row["c"]) if row else 0

    def get_jobs_filtered(
        self,
        *,
        status: str | None = None,
        platform: str | None = None,
        platforms: list[str] | None = None,
        min_score: int | None = None,
        locations: list[str] | None = None,
        title_terms: list[str] | None = None,
        description_terms: list[str] | None = None,
        keyword_terms: list[str] | None = None,
        seniority_levels: list[str] | None = None,
        posted_after: str | None = None,
        posted_before: str | None = None,
        sort_by_match: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return jobs matching dynamic filter criteria.

        ``title_terms`` / ``description_terms`` / ``keyword_terms`` use OR logic
        within each list. ``keyword_terms`` matches title, company, description,
        and platform (case-insensitive substring).
        ``seniority_levels`` filters via ``jd_analysis.seniority_level``.
        When ``sort_by_match`` is True, order by match_score DESC (nulls last).
        """
        sql, params, _join_jd = self._build_jobs_filter_sql(
            status=status,
            platform=platform,
            platforms=platforms,
            min_score=min_score,
            locations=locations,
            title_terms=title_terms,
            description_terms=description_terms,
            keyword_terms=keyword_terms,
            seniority_levels=seniority_levels,
            posted_after=posted_after,
            posted_before=posted_before,
            sort_by_match=sort_by_match,
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])

        with self._conn() as conn:
            rows = conn.execute(sql + ";", params).fetchall()
        return [dict(r) for r in rows]

    def get_distinct_platforms(self) -> list[str]:
        """Return sorted distinct non-empty job platforms."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT platform FROM jobs
                WHERE platform IS NOT NULL AND TRIM(platform) != ''
                ORDER BY platform COLLATE NOCASE;
                """
            ).fetchall()
        return [str(r["platform"]).strip() for r in rows]

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?;", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_job_status(self, job_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?;", (status, job_id)
            )

    def update_match_score(self, job_id: int, score: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET match_score = ? WHERE id = ?;",
                (int(score), job_id),
            )

    def update_match_scores_batch(self, scores: dict[int, int]) -> None:
        """Persist many match scores in one transaction."""
        if not scores:
            return
        rows = [(int(score), int(job_id)) for job_id, score in scores.items()]
        with self._conn() as conn:
            conn.executemany(
                "UPDATE jobs SET match_score = ? WHERE id = ?;",
                rows,
            )

    def get_match_score_aggregate(self) -> tuple[float, int]:
        """Return (average match_score, count of scored jobs)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT AVG(match_score) AS avg_score,
                       COUNT(*) AS scored
                FROM jobs
                WHERE match_score IS NOT NULL;
                """
            ).fetchone()
        if not row or not row["scored"]:
            return 0.0, 0
        return round(float(row["avg_score"] or 0), 1), int(row["scored"])

    # ------------------------------------------------------------------ #
    # applications
    # ------------------------------------------------------------------ #
    def insert_application(self, application: dict[str, Any]) -> int:
        if not application.get("job_id"):
            raise ValueError("application_dict must include 'job_id'.")
        status = application.get("status", "Applied")
        if status not in APPLICATION_STATUSES:
            raise ValueError(
                f"Invalid application status {status!r}; "
                f"must be one of {sorted(APPLICATION_STATUSES)}"
            )

        payload = {
            "job_id": int(application["job_id"]),
            "date_applied": application.get("date_applied") or _now(),
            "resume_version": application.get("resume_version"),
            "cover_letter_path": application.get("cover_letter_path"),
            "status": status,
            "notes": application.get("notes"),
        }
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO applications (
                    job_id, date_applied, resume_version,
                    cover_letter_path, status, notes
                ) VALUES (
                    :job_id, :date_applied, :resume_version,
                    :cover_letter_path, :status, :notes
                );
                """,
                payload,
            )
            app_id = int(cur.lastrowid or 0)
            conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?;",
                ("applied", payload["job_id"]),
            )
            return app_id

    def update_application_status(
        self,
        application_id: int,
        status: str,
        notes: str | None = None,
    ) -> None:
        if status not in APPLICATION_STATUSES:
            raise ValueError(
                f"Invalid application status {status!r}; "
                f"must be one of {sorted(APPLICATION_STATUSES)}"
            )
        with self._conn() as conn:
            if notes is None:
                conn.execute(
                    "UPDATE applications SET status = ? WHERE id = ?;",
                    (status, application_id),
                )
            else:
                conn.execute(
                    "UPDATE applications SET status = ?, notes = ? WHERE id = ?;",
                    (status, notes, application_id),
                )

    def get_applications(
        self, status: str | None = None, job_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)

        sql = "SELECT * FROM applications"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY date_applied DESC, id DESC;"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # jd_analysis
    # ------------------------------------------------------------------ #
    def insert_jd_analysis(self, job_id: int, analysis: dict[str, Any]) -> int:
        payload = {
            "job_id": int(job_id),
            "required_skills": _dumps(analysis.get("required_skills")),
            "preferred_skills": _dumps(analysis.get("preferred_skills")),
            "tools": _dumps(analysis.get("tools")),
            "keywords": _dumps(analysis.get("keywords")),
            "experience_required": analysis.get("experience_required") or "",
            "seniority_level": analysis.get("seniority_level") or "",
        }
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO jd_analysis (
                    job_id, required_skills, preferred_skills,
                    tools, keywords, experience_required, seniority_level
                ) VALUES (
                    :job_id, :required_skills, :preferred_skills,
                    :tools, :keywords, :experience_required, :seniority_level
                )
                ON CONFLICT(job_id) DO UPDATE SET
                    required_skills      = excluded.required_skills,
                    preferred_skills     = excluded.preferred_skills,
                    tools                = excluded.tools,
                    keywords             = excluded.keywords,
                    experience_required  = excluded.experience_required,
                    seniority_level      = excluded.seniority_level;
                """,
                payload,
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute(
                "SELECT id FROM jd_analysis WHERE job_id = ?;", (payload["job_id"],)
            ).fetchone()
            return int(row["id"]) if row else 0

    def get_jd_analysis(self, job_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jd_analysis WHERE job_id = ?;", (job_id,)
            ).fetchone()
        if not row:
            return None
        return self._hydrate_jd_row(row)

    def get_all_jd_analyses(self) -> dict[int, dict[str, Any]]:
        """Return every cached JD analysis keyed by job_id (one round-trip)."""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM jd_analysis;").fetchall()
        return {int(row["job_id"]): self._hydrate_jd_row(row) for row in rows}

    def get_jd_analyses_for_jobs(
        self, job_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Bulk-fetch JD analyses for a list of job ids."""
        ids = [int(i) for i in job_ids if i is not None]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM jd_analysis WHERE job_id IN ({placeholders});",
                ids,
            ).fetchall()
        return {int(row["job_id"]): self._hydrate_jd_row(row) for row in rows}

    @staticmethod
    def _hydrate_jd_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "required_skills": _loads(row["required_skills"], []),
            "preferred_skills": _loads(row["preferred_skills"], []),
            "tools": _loads(row["tools"], []),
            "keywords": _loads(row["keywords"], []),
            "experience_required": row["experience_required"] or "",
            "seniority_level": row["seniority_level"] or "",
        }

    # ------------------------------------------------------------------ #
    # match_explanations
    # ------------------------------------------------------------------ #
    def get_match_explanation(
        self, job_id: int, profile_slug: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch cached match explanation for a job/profile pair."""
        slug = profile_slug or ""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM match_explanations
                WHERE job_id = ? AND profile_slug = ?;
                """,
                (int(job_id), slug),
            ).fetchone()
        return dict(row) if row else None

    def upsert_match_explanation(
        self,
        job_id: int,
        profile_slug: str | None,
        explanation: str,
        match_score_snapshot: int | None = None,
    ) -> None:
        """Insert or update a cached match explanation."""
        slug = profile_slug or ""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO match_explanations (
                    job_id, profile_slug, explanation,
                    match_score_snapshot, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id, profile_slug) DO UPDATE SET
                    explanation = excluded.explanation,
                    match_score_snapshot = excluded.match_score_snapshot,
                    updated_at = excluded.updated_at;
                """,
                (
                    int(job_id),
                    slug,
                    explanation,
                    match_score_snapshot,
                    _now(),
                ),
            )

    def get_match_explanations_for_jobs(
        self,
        job_ids: list[int],
        profile_slug: str | None = None,
    ) -> dict[int, str]:
        """Bulk-fetch explanations keyed by job_id."""
        if not job_ids:
            return {}
        slug = profile_slug or ""
        placeholders = ", ".join("?" * len(job_ids))
        params: list[Any] = [slug, *job_ids]
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT job_id, explanation FROM match_explanations
                WHERE profile_slug = ? AND job_id IN ({placeholders});
                """,
                params,
            ).fetchall()
        return {int(r["job_id"]): str(r["explanation"]) for r in rows}

    # ------------------------------------------------------------------ #
    # users
    # ------------------------------------------------------------------ #
    def create_user(
        self,
        email: str,
        password_hash: str,
        name: str | None = None,
    ) -> int:
        """Insert a new user. Raises sqlite3.IntegrityError if email exists."""
        payload = {
            "email": email.strip().lower(),
            "password_hash": password_hash,
            "name": (name or "").strip() or None,
            "created_at": _now(),
        }
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO users (email, password_hash, name, created_at)
                VALUES (:email, :password_hash, :name, :created_at);
                """,
                payload,
            )
            return int(cur.lastrowid or 0)

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, name, created_at FROM users WHERE email = ?;",
                (email.strip().lower(),),
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id, email, password_hash, name, created_at
                FROM users WHERE id = ?;
                """,
                (int(user_id),),
            ).fetchone()
        if not row:
            return None
        user = dict(row)
        user.pop("password_hash", None)
        return user

    def get_user_settings(self, user_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id, active_profile FROM user_settings WHERE user_id = ?;",
                (int(user_id),),
            ).fetchone()
        return dict(row) if row else None

    def set_user_active_profile(self, user_id: int, slug: str | None) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO user_settings (user_id, active_profile)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET active_profile = excluded.active_profile;
                """,
                (int(user_id), slug),
            )

    # ------------------------------------------------------------------ #
    # user_resumes
    # ------------------------------------------------------------------ #
    def save_resume(
        self,
        user_id: int,
        slug: str,
        *,
        pdf_bytes: bytes | None = None,
        filename: str | None = None,
        profile_json: dict[str, Any] | str | None = None,
        parse_status: str | None = None,
        set_active: bool = False,
    ) -> int:
        """Insert or update a user's resume (PDF blob + optional parsed profile)."""
        uid = int(user_id)
        clean_slug = (slug or "").strip()
        if not clean_slug:
            raise ValueError("slug must be non-empty")

        profile_text: str | None = None
        if profile_json is not None:
            profile_text = (
                profile_json
                if isinstance(profile_json, str)
                else json.dumps(profile_json, ensure_ascii=False)
            )

        status = parse_status or "pdf_only"
        if status not in RESUME_PARSE_STATUSES:
            raise ValueError(
                f"Invalid parse_status {status!r}; "
                f"must be one of {sorted(RESUME_PARSE_STATUSES)}"
            )

        now = _now()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id, pdf_blob, profile_json, parse_status FROM user_resumes "
                "WHERE user_id = ? AND slug = ?;",
                (uid, clean_slug),
            ).fetchone()

            if existing:
                blob = pdf_bytes if pdf_bytes is not None else existing["pdf_blob"]
                prof = profile_text if profile_text is not None else existing["profile_json"]
                st = status if parse_status is not None else existing["parse_status"]
                conn.execute(
                    """
                    UPDATE user_resumes SET
                        filename = COALESCE(?, filename),
                        pdf_blob = ?,
                        profile_json = ?,
                        parse_status = ?,
                        updated_at = ?
                    WHERE user_id = ? AND slug = ?;
                    """,
                    (filename, blob, prof, st, now, uid, clean_slug),
                )
                resume_id = int(existing["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO user_resumes (
                        user_id, slug, filename, pdf_blob, profile_json,
                        parse_status, created_at, updated_at, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0);
                    """,
                    (
                        uid,
                        clean_slug,
                        filename,
                        pdf_bytes,
                        profile_text,
                        status,
                        now,
                        now,
                    ),
                )
                resume_id = int(cur.lastrowid or 0)

            if set_active:
                conn.execute(
                    "UPDATE user_resumes SET is_active = 0 WHERE user_id = ?;",
                    (uid,),
                )
                conn.execute(
                    """
                    UPDATE user_resumes SET is_active = 1, updated_at = ?
                    WHERE user_id = ? AND slug = ?;
                    """,
                    (now, uid, clean_slug),
                )
                conn.execute(
                    """
                    INSERT INTO user_settings (user_id, active_profile)
                    VALUES (?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET active_profile = excluded.active_profile;
                    """,
                    (uid, clean_slug),
                )
        return resume_id

    @staticmethod
    def _hydrate_resume_row(row: sqlite3.Row, *, include_blob: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": row["id"],
            "user_id": row["user_id"],
            "slug": row["slug"],
            "filename": row["filename"],
            "has_pdf": row["pdf_blob"] is not None and len(row["pdf_blob"]) > 0,
            "parse_status": row["parse_status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "is_active": bool(row["is_active"]),
        }
        if row["profile_json"]:
            data["profile"] = _loads(row["profile_json"], {})
        else:
            data["profile"] = {}
        if include_blob:
            data["pdf_blob"] = row["pdf_blob"]
        return data

    def get_resume(self, user_id: int, slug: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM user_resumes WHERE user_id = ? AND slug = ?;",
                (int(user_id), slug),
            ).fetchone()
        return self._hydrate_resume_row(row) if row else None

    def list_resumes(self, user_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM user_resumes
                WHERE user_id = ?
                ORDER BY is_active DESC, updated_at DESC, slug COLLATE NOCASE;
                """,
                (int(user_id),),
            ).fetchall()
        return [self._hydrate_resume_row(r) for r in rows]

    def get_resume_pdf_bytes(self, user_id: int, slug: str) -> bytes | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT pdf_blob FROM user_resumes WHERE user_id = ? AND slug = ?;",
                (int(user_id), slug),
            ).fetchone()
        if not row or row["pdf_blob"] is None:
            return None
        return bytes(row["pdf_blob"])

    def delete_resume(self, user_id: int, slug: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM user_resumes WHERE user_id = ? AND slug = ?;",
                (int(user_id), slug),
            )
            return cur.rowcount > 0

    def cleanup_old_scrape_runs(self, keep_days: int = 30) -> int:
        """Delete scrape runs older than `keep_days`."""
        with self._conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
            res = conn.execute(
                "DELETE FROM scrape_runs WHERE started_at < ?;", (cutoff,)
            )
            return res.rowcount

    # ------------------------------------------------------------------ #
    # Users & Auth
    # ------------------------------------------------------------------ #
    def create_user(self, email: str, password_hash: str, name: str = "") -> int:
        """Create a new user. Raises sqlite3.IntegrityError if email exists."""
        with self._conn() as conn:
            res = conn.execute(
                """
                INSERT INTO users (email, password_hash, name, created_at)
                VALUES (?, ?, ?, ?);
                """,
                (email, password_hash, name, _now()),
            )
            return res.lastrowid

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        """Fetch a user by email."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?;", (email,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Fetch a user by ID."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?;", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_active_resume(self, user_id: int, slug: str) -> None:
        uid = int(user_id)
        now = _now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM user_resumes WHERE user_id = ? AND slug = ?;",
                (uid, slug),
            ).fetchone()
            if not row:
                raise ValueError(f"Resume {slug!r} not found for user {uid}")
            conn.execute(
                "UPDATE user_resumes SET is_active = 0 WHERE user_id = ?;",
                (uid,),
            )
            conn.execute(
                """
                UPDATE user_resumes SET is_active = 1, updated_at = ?
                WHERE user_id = ? AND slug = ?;
                """,
                (now, uid, slug),
            )
            conn.execute(
                """
                INSERT INTO user_settings (user_id, active_profile)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET active_profile = excluded.active_profile;
                """,
                (uid, slug),
            )

    # ------------------------------------------------------------------ #
    # scrape_runs
    # ------------------------------------------------------------------ #
    def insert_scrape_run(self, trigger: str = "scheduled") -> int:
        """Start a scrape run log row; returns its id."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO scrape_runs (started_at, trigger, status)
                VALUES (?, ?, 'running');
                """,
                (_now(), trigger),
            )
            return int(cur.lastrowid or 0)

    def finish_scrape_run(
        self,
        run_id: int,
        *,
        status: str,
        new_jobs: int = 0,
        total_found: int = 0,
        platforms: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Mark a scrape run complete (or failed)."""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE scrape_runs SET
                    finished_at = ?,
                    status = ?,
                    new_jobs = ?,
                    total_found = ?,
                    platforms_json = ?,
                    error = ?
                WHERE id = ?;
                """,
                (
                    _now(),
                    status,
                    int(new_jobs),
                    int(total_found),
                    json.dumps(platforms or {}, ensure_ascii=False),
                    error,
                    int(run_id),
                ),
            )

    def get_latest_scrape_run(self) -> dict[str, Any] | None:
        """Return the most recent scrape run (any status)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM scrape_runs
                ORDER BY started_at DESC, id DESC
                LIMIT 1;
                """
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["platforms"] = _loads(data.get("platforms_json"), {})
        return data

    def get_last_completed_scrape_run(self) -> dict[str, Any] | None:
        """Return the most recent successfully completed scrape run."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM scrape_runs
                WHERE status IN ('completed', 'completed_with_errors')
                ORDER BY finished_at DESC, id DESC
                LIMIT 1;
                """
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["platforms"] = _loads(data.get("platforms_json"), {})
        return data

    # ------------------------------------------------------------------ #
    # stats
    # ------------------------------------------------------------------ #
    def get_stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            total_jobs = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs;"
            ).fetchone()["c"]
            total_applied = conn.execute(
                "SELECT COUNT(*) AS c FROM applications;"
            ).fetchone()["c"]
            by_platform_rows = conn.execute(
                """
                SELECT COALESCE(platform, 'unknown') AS platform,
                       COUNT(*) AS c
                FROM jobs
                GROUP BY platform
                ORDER BY c DESC;
                """
            ).fetchall()
            by_status_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM jobs
                GROUP BY status
                ORDER BY c DESC;
                """
            ).fetchall()
            apps_by_status_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM applications
                GROUP BY status
                ORDER BY c DESC;
                """
            ).fetchall()

        return {
            "total_jobs": int(total_jobs),
            "total_applied": int(total_applied),
            "by_platform": {r["platform"]: int(r["c"]) for r in by_platform_rows},
            "by_status": {r["status"]: int(r["c"]) for r in by_status_rows},
            "applications_by_status": {
                r["status"]: int(r["c"]) for r in apps_by_status_rows
            },
        }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
def _run_smoke_test() -> None:
    """Exercise every public method against a throwaway DB file."""
    import os
    import tempfile

    tmp = Path(tempfile.gettempdir()) / "job_agent_smoketest.db"
    if tmp.exists():
        tmp.unlink()
    db = Database(tmp)

    print(f"[1] Using temp DB: {tmp}")

    # -- insert_job --
    job_a = {
        "title": "ML Engineer",
        "company": "Acme AI",
        "location": "Remote",
        "job_type": "full-time",
        "platform": "internshala",
        "url": "https://example.com/jobs/ml-1",
        "description": "Build models. PyTorch + AWS.",
    }
    job_b = {
        "title": "Data Scientist",
        "company": "Globex",
        "location": "Bengaluru",
        "job_type": "full-time",
        "platform": "naukri",
        "url": "https://example.com/jobs/ds-2",
        "description": "Analytics + SQL + Python.",
    }
    id_a = db.insert_job(job_a)
    id_b = db.insert_job(job_b)
    assert id_a > 0 and id_b > 0
    print(f"[2] insert_job: job_a id={id_a}, job_b id={id_b}")

    id_dup = db.insert_job(job_a)
    assert id_dup == id_a, f"Expected duplicate URL to return {id_a}, got {id_dup}"
    print(f"[3] insert_job duplicate URL returned existing id={id_dup} (ok)")

    # -- update_match_score / update_job_status --
    db.update_match_score(id_a, 88)
    db.update_match_score(id_b, 55)
    db.update_job_status(id_b, "skipped")
    print("[4] update_match_score + update_job_status: ok")

    # -- get_jobs filters --
    high = db.get_jobs(min_score=70)
    assert len(high) == 1 and high[0]["id"] == id_a, high
    skipped = db.get_jobs(status="skipped")
    assert len(skipped) == 1 and skipped[0]["id"] == id_b, skipped
    naukri = db.get_jobs(platform="naukri")
    assert len(naukri) == 1 and naukri[0]["platform"] == "naukri"
    print(
        f"[5] get_jobs filters ok "
        f"(min_score=70 -> {len(high)}, status=skipped -> {len(skipped)}, "
        f"platform=naukri -> {len(naukri)})"
    )

    # -- jd_analysis insert + upsert + get --
    analysis_a = {
        "required_skills": ["Python", "PyTorch"],
        "preferred_skills": ["AWS"],
        "tools": ["Docker", "Git"],
        "keywords": ["MLOps", "deep learning"],
        "experience_required": "2+ years",
    }
    jd_id = db.insert_jd_analysis(id_a, analysis_a)
    assert jd_id > 0
    fetched = db.get_jd_analysis(id_a)
    assert fetched is not None
    assert fetched["required_skills"] == ["Python", "PyTorch"]
    assert fetched["tools"] == ["Docker", "Git"]
    print(f"[6] insert_jd_analysis + get_jd_analysis: id={jd_id}, "
          f"required={fetched['required_skills']}")

    # upsert (same job_id -> updates, doesn't dup)
    analysis_a_v2 = {**analysis_a, "preferred_skills": ["AWS", "GCP"]}
    db.insert_jd_analysis(id_a, analysis_a_v2)
    refetched = db.get_jd_analysis(id_a)
    assert refetched is not None
    assert refetched["preferred_skills"] == ["AWS", "GCP"]
    print("[7] insert_jd_analysis upsert: ok")

    # -- applications --
    app_id = db.insert_application({
        "job_id": id_a,
        "resume_version": "v1.pdf",
        "cover_letter_path": "output/cover_letters/1.pdf",
        "status": "Applied",
        "notes": "First-round screen scheduled.",
    })
    assert app_id > 0
    # side effect: job status flipped to 'applied'
    job_a_now = db.get_job(id_a)
    assert job_a_now and job_a_now["status"] == "applied"
    print(f"[8] insert_application id={app_id}, job status -> applied")

    db.update_application_status(app_id, "Interview", notes="Phone screen on Friday")
    apps_iv = db.get_applications(status="Interview")
    assert len(apps_iv) == 1 and apps_iv[0]["notes"] == "Phone screen on Friday"
    print(f"[9] update_application_status: status=Interview, notes updated")

    # -- invalid inputs --
    try:
        db.insert_application({"job_id": id_a, "status": "Bogus"})
    except ValueError as e:
        print(f"[10] invalid status raises ValueError as expected: {e}")
    else:
        raise AssertionError("Expected ValueError for invalid status")

    # -- get_stats --
    stats = db.get_stats()
    assert stats["total_jobs"] == 2
    assert stats["total_applied"] == 1
    assert stats["by_platform"].get("internshala") == 1
    assert stats["by_platform"].get("naukri") == 1
    assert stats["applications_by_status"].get("Interview") == 1
    print("[11] get_stats:")
    print(json.dumps(stats, indent=2))

    try:
        tmp.unlink()
        print(f"[12] cleaned up {tmp}")
    except OSError:
        pass

    print("\nAll database smoke tests passed.")


if __name__ == "__main__":
    _run_smoke_test()
