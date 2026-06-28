# Legacy Streamlit dashboard — not launched by main.py.
# Primary UI: python main.py  (FastAPI web app)
# To run this file manually: pip install -r dashboard/requirements-legacy.txt
#                            streamlit run dashboard/app.py
"""AI Job Agent - Streamlit dashboard.

Five pages reachable from the sidebar:
    Overview       - hero stats + per-status pie + per-platform bar
    Job Browser    - filterable jobs with match badges + explanations
    Review Resume  - accept/reject tailored resume diffs before PDF
    Applications   - editable status + notes per application
    Analytics      - match-score histogram, top JD skills, success-rate over time
    Settings       - view / edit config.yaml + show user profile summary
    Profiles / Resumes - upload PDF, parse with Gemini, manage profile slugs

All data is read from `db/jobs.db` through `db.database.Database`. Charts use
Plotly with the dark template so the page is readable in either Streamlit
theme.
"""

from __future__ import annotations

import html
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Make sibling packages importable when invoked via `streamlit run`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import plotly.express as px
import streamlit as st
import yaml

from ai.profile_store import (
    RESUME_DIR,
    list_profiles,
    load_profile,
    profile_path,
    resolve_active_slug,
    resolve_resume_pdf,
    slugify,
)
from db.database import Database, APPLICATION_STATUSES


# =============================================================================
# Constants / config
# =============================================================================
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

PLATFORM_EMOJI: dict[str, str] = {
    "linkedin": "🔵",
    "naukri": "🟠",
    "indeed": "🔴",
    "internshala": "🟢",
    "wellfound": "⚪",
    "glassdoor": "🟡",
    "google_jobs": "🔍",
    "google": "🔍",
}

APP_STATUS_ORDER = ["Applied", "Interview", "Offer", "Rejected"]
APP_STATUS_COLOR = {
    "Applied": "#3b82f6",
    "Interview": "#f59e0b",
    "Offer": "#10b981",
    "Rejected": "#ef4444",
}

PLOTLY_TEMPLATE = "plotly_dark"

NAV_GROUPS: dict[str, list[str]] = {
    "Data": ["🏠 Overview", "🔍 Job Browser", "📊 Analytics"],
    "Actions": [
        "📄 Profiles / Resumes",
        "📝 Review Tailored Resume",
        "📋 Applications",
    ],
    "Settings": ["⚙️ Settings"],
}

st.set_page_config(
    page_title="AI Job Agent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =============================================================================
# Styling & UI helpers
# =============================================================================
def inject_css() -> None:
    st.markdown(
        """
        <style>
        /* ---- Typography & page chrome ---- */
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        html, body, [class*="css"] { font-family: 'Inter', system-ui, sans-serif; }
        .block-container {
            padding-top: 1.2rem; padding-bottom: 2.5rem; max-width: 1400px;
        }
        h1, h2, h3 { letter-spacing: -0.02em; }
        .page-title {
            font-size: 1.75rem; font-weight: 700; margin-bottom: 0.15rem;
            background: linear-gradient(90deg, #f8fafc 0%, #94a3b8 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .page-subtitle { color: #94a3b8; font-size: 0.92rem; margin-bottom: 1.25rem; }

        /* ---- Metrics & cards ---- */
        [data-testid="stMetric"] {
            background: linear-gradient(135deg, rgba(31,41,55,0.72) 0%, rgba(17,24,39,0.72) 100%);
            border: 1px solid rgba(148, 163, 184, 0.16);
            padding: 16px 18px; border-radius: 14px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.18);
        }
        [data-testid="stMetricLabel"] p {
            font-size: 0.72rem; text-transform: uppercase;
            letter-spacing: 0.08em; color: #94a3b8; font-weight: 600;
        }
        [data-testid="stMetricValue"] { font-weight: 700; font-size: 1.65rem; }

        .dash-card {
            background: linear-gradient(145deg, rgba(30,41,59,0.55), rgba(15,23,42,0.55));
            border: 1px solid rgba(148,163,184,0.14);
            border-radius: 14px; padding: 1.1rem 1.25rem; margin-bottom: 0.75rem;
        }
        .hero-banner {
            background: linear-gradient(135deg, rgba(59,130,246,0.12) 0%, rgba(16,185,129,0.08) 100%);
            border: 1px solid rgba(59,130,246,0.22);
            border-radius: 16px; padding: 1.35rem 1.5rem; margin-bottom: 1.25rem;
        }
        .hero-banner h3 { margin: 0 0 0.35rem 0; font-size: 1.15rem; color: #e2e8f0; }
        .hero-banner p { margin: 0; color: #94a3b8; font-size: 0.88rem; }

        /* ---- Pills & badges ---- */
        .pill {
            display: inline-block; padding: 3px 11px; border-radius: 999px;
            font-size: 0.78rem; font-weight: 600; line-height: 1.6;
        }
        .pill-green  { background: rgba(16,185,129,0.18); color: #34d399; }
        .pill-amber  { background: rgba(245,158,11,0.2); color: #fbbf24; }
        .pill-red    { background: rgba(239,68,68,0.2); color: #f87171; }
        .pill-gray   { background: rgba(148,163,184,0.18); color: #94a3b8; }
        .pill-blue   { background: rgba(59,130,246,0.2); color: #60a5fa; }

        .job-card-title { font-size: 1.02rem; font-weight: 600; color: #f1f5f9; line-height: 1.4; }
        .job-card-meta { color: #64748b; font-size: 0.82rem; }

        /* ---- Empty states ---- */
        .empty-state {
            text-align: center; padding: 2.5rem 1.5rem;
            background: rgba(15,23,42,0.45);
            border: 1px dashed rgba(148,163,184,0.25);
            border-radius: 16px; margin: 1rem 0;
        }
        .empty-state .icon { font-size: 2.5rem; margin-bottom: 0.5rem; }
        .empty-state h4 { color: #e2e8f0; margin: 0.25rem 0; font-size: 1.05rem; }
        .empty-state p { color: #94a3b8; font-size: 0.88rem; margin: 0.35rem 0 0.75rem; }
        .empty-state code {
            background: rgba(30,41,59,0.8); padding: 0.2rem 0.45rem;
            border-radius: 6px; font-size: 0.82rem;
        }

        /* ---- Wizard steps ---- */
        .wizard-row { display: flex; gap: 0.5rem; margin-bottom: 1.25rem; flex-wrap: wrap; }
        .wizard-step {
            flex: 1; min-width: 140px; text-align: center; padding: 0.75rem 0.5rem;
            border-radius: 12px; border: 1px solid rgba(148,163,184,0.18);
            background: rgba(15,23,42,0.4); transition: all 0.2s;
        }
        .wizard-step.active {
            border-color: rgba(59,130,246,0.55);
            background: rgba(59,130,246,0.12);
        }
        .wizard-step.done {
            border-color: rgba(16,185,129,0.45);
            background: rgba(16,185,129,0.08);
        }
        .wizard-step .num {
            display: inline-block; width: 1.6rem; height: 1.6rem; line-height: 1.6rem;
            border-radius: 50%; background: rgba(148,163,184,0.2);
            font-size: 0.78rem; font-weight: 700; margin-bottom: 0.25rem;
        }
        .wizard-step.active .num { background: #3b82f6; color: white; }
        .wizard-step.done .num { background: #10b981; color: white; }
        .wizard-step .label { font-size: 0.78rem; color: #94a3b8; font-weight: 500; }
        .wizard-step.active .label, .wizard-step.done .label { color: #e2e8f0; }

        /* ---- Upload zone ---- */
        [data-testid="stFileUploader"] {
            border: 2px dashed rgba(59,130,246,0.35) !important;
            border-radius: 14px !important;
            padding: 0.5rem !important;
            background: rgba(59,130,246,0.04) !important;
        }
        [data-testid="stFileUploader"]:hover {
            border-color: rgba(59,130,246,0.6) !important;
            background: rgba(59,130,246,0.08) !important;
        }

        /* ---- Diff columns ---- */
        .diff-col {
            border-radius: 10px; padding: 0.85rem 1rem; min-height: 120px;
            font-family: ui-monospace, 'Cascadia Code', monospace;
            font-size: 0.82rem; line-height: 1.55; white-space: pre-wrap;
            word-break: break-word;
        }
        .diff-original {
            background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.22);
            color: #fca5a5;
        }
        .diff-suggested {
            background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.22);
            color: #6ee7b7;
        }
        .diff-label {
            font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
            font-weight: 600; margin-bottom: 0.4rem;
        }
        .diff-label-original { color: #f87171; }
        .diff-label-suggested { color: #34d399; }

        /* ---- Sidebar ---- */
        section[data-testid="stSidebar"] {
            border-right: 1px solid rgba(148,163,184,0.1);
            background: linear-gradient(180deg, rgba(15,23,42,0.95), rgba(17,24,39,0.98));
        }
        section[data-testid="stSidebar"] .nav-group-label {
            font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.1em;
            color: #64748b; font-weight: 700; margin: 0.65rem 0 0.25rem 0;
        }
        .profile-badge {
            background: linear-gradient(135deg, rgba(59,130,246,0.15), rgba(16,185,129,0.1));
            border: 1px solid rgba(59,130,246,0.3);
            border-radius: 12px; padding: 0.65rem 0.85rem; margin: 0.5rem 0;
        }
        .profile-badge .name { font-weight: 600; color: #e2e8f0; font-size: 0.92rem; }
        .profile-badge .slug { color: #64748b; font-size: 0.75rem; margin-top: 0.15rem; }
        .profile-badge.missing {
            border-color: rgba(245,158,11,0.35);
            background: rgba(245,158,11,0.08);
        }

        /* ---- Section headers ---- */
        .section-header {
            font-size: 0.95rem; font-weight: 600; color: #cbd5e1;
            margin: 1.25rem 0 0.65rem 0; padding-bottom: 0.35rem;
            border-bottom: 1px solid rgba(148,163,184,0.12);
        }

        /* ---- Quick action chips ---- */
        .action-chip {
            background: rgba(30,41,59,0.6); border: 1px solid rgba(148,163,184,0.15);
            border-radius: 10px; padding: 0.75rem; text-align: center;
        }
        .action-chip .cmd { font-size: 0.78rem; color: #94a3b8; margin-top: 0.35rem; }

        /* ---- Tables ---- */
        .stDataFrame, .stDataEditor { font-size: 0.92rem; }

        /* ---- Mobile tweaks ---- */
        @media (max-width: 768px) {
            .block-container { padding-left: 1rem; padding-right: 1rem; }
            [data-testid="stMetricValue"] { font-size: 1.35rem; }
            .wizard-step { min-width: 100px; }
        }

        /* ---- Top matches pin ---- */
        .top-match-pin {
            border-left: 3px solid #10b981;
            padding-left: 0.25rem;
        }

        /* ---- Error banner ---- */
        .gemini-error-banner {
            background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35);
            border-radius: 10px; padding: 0.85rem 1rem; margin: 0.75rem 0;
            color: #fca5a5; font-size: 0.88rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def page_header(title: str, subtitle: str = "") -> None:
    st.markdown(f'<p class="page-title">{title}</p>', unsafe_allow_html=True)
    if subtitle:
        st.markdown(f'<p class="page-subtitle">{subtitle}</p>', unsafe_allow_html=True)


def empty_state(
    icon: str,
    title: str,
    message: str,
    command: str | None = None,
) -> None:
    cmd_html = f"<p>Run: <code>{command}</code></p>" if command else ""
    st.markdown(
        f"""
        <div class="empty-state">
            <div class="icon">{icon}</div>
            <h4>{title}</h4>
            <p>{message}</p>
            {cmd_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def section_header(text: str) -> None:
    st.markdown(f'<p class="section-header">{text}</p>', unsafe_allow_html=True)


def render_wizard_steps(step: int, has_upload: bool, has_parsed: bool) -> None:
    steps = [
        (1, "Upload PDF", step >= 1),
        (2, "Parse with Gemini", step >= 2),
        (3, "Select profile", step >= 3),
    ]
    parts = ['<div class="wizard-row">']
    for num, label, done in steps:
        active = num == step and not done
        cls = "wizard-step"
        if done:
            cls += " done"
        elif active:
            cls += " active"
        parts.append(
            f'<div class="{cls}">'
            f'<div class="num">{num if not done else "✓"}</div>'
            f'<div class="label">{label}</div></div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def render_profile_badge(slug: str | None, config: dict[str, Any]) -> None:
    profile = load_active_profile(config, slug)
    name = profile.get("name") or (slug or "Default")
    has_data = bool(profile.get("skills") or profile.get("experience"))
    if slug and has_data:
        st.markdown(
            f"""
            <div class="profile-badge">
                <div class="name">👤 {name}</div>
                <div class="slug">Active · {slug}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    elif slug:
        st.markdown(
            f"""
            <div class="profile-badge missing">
                <div class="name">⚠️ {slug}</div>
                <div class="slug">Not parsed — upload & parse resume</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div class="profile-badge missing">
                <div class="name">👤 No profile selected</div>
                <div class="slug">Upload a resume to get started</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def badge_thresholds(config: dict[str, Any]) -> tuple[int, int]:
    """Return (green_min, yellow_min) from config."""
    cfg = (config.get("job_search") or {}).get("badge_thresholds") or {}
    green = int(cfg.get("green", 80))
    yellow = int(cfg.get("yellow", 50))
    return green, yellow


def score_pill(score: float | int | None, config: dict[str, Any] | None = None) -> str:
    if score is None or (isinstance(score, float) and pd.isna(score)):
        return '<span class="pill pill-gray">—</span>'
    s = int(score)
    green_min, yellow_min = badge_thresholds(config or {})
    if s >= green_min:
        cls = "pill-green"
    elif s >= yellow_min:
        cls = "pill-amber"
    else:
        cls = "pill-red"
    return f'<span class="pill {cls}">{s}</span>'


def status_pill(status: str | None) -> str:
    s = status or "new"
    color_map = {
        "new": "pill-blue",
        "applied": "pill-amber",
        "skipped_low_score": "pill-gray",
        "Applied": "pill-amber",
        "Interview": "pill-amber",
        "Offer": "pill-green",
        "Rejected": "pill-red",
    }
    return f'<span class="pill {color_map.get(s, "pill-gray")}">{s}</span>'


def platform_label(p: str | None) -> str:
    key = (p or "").lower()
    return f"{PLATFORM_EMOJI.get(key, '•')} {p or '—'}"


def render_job_card(
    row: pd.Series,
    config: dict[str, Any],
    explanations: dict[int, str],
    *,
    pinned: bool = False,
) -> None:
    """Render a single job listing card."""
    job_id = int(row["id"])
    score = row.get("match_score")
    pin_cls = " top-match-pin" if pinned else ""
    with st.container(border=True):
        st.markdown(f'<div class="{pin_cls.strip()}">', unsafe_allow_html=True)
        c1, c2, c3 = st.columns([5, 1, 1])
        title_line = (
            f'<span class="job-card-title">{row["title"]}</span>'
            f'<br><span class="job-card-meta">'
            f'{row.get("company") or "—"} · {platform_label(row.get("platform"))}'
            f'</span>'
        )
        c1.markdown(title_line, unsafe_allow_html=True)
        c2.markdown(
            f"**Match**<br>{score_pill(score, config)}",
            unsafe_allow_html=True,
        )
        c3.markdown(
            f"**Status**<br>{status_pill(row.get('status'))}",
            unsafe_allow_html=True,
        )
        meta = (
            f"📍 {row.get('location') or '—'} · "
            f"scraped {row.get('date_scraped') or '—'}"
        )
        st.caption(meta)
        if pd.notna(score):
            st.progress(int(score) / 100.0)
        expl = explanations.get(job_id)
        if expl:
            with st.expander("Why this match score?", expanded=pinned):
                st.write(expl)
        elif config.get("ai", {}).get("explain_matches"):
            st.caption(
                "No cached explanation — run "
                "`python main.py --mode analyze` to generate."
            )
        if row.get("url"):
            st.markdown(f"[Open posting ↗]({row['url']})")
        st.markdown("</div>", unsafe_allow_html=True)


def gemini_error_banner(message: str) -> None:
    st.markdown(
        f'<div class="gemini-error-banner">⚠️ {message}</div>',
        unsafe_allow_html=True,
    )


# =============================================================================
# Data access
# =============================================================================
@st.cache_resource
def get_db() -> Database:
    return Database()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)


def load_active_profile(
    config: dict[str, Any], slug: str | None
) -> dict[str, Any]:
    """Read the parsed profile JSON for ``slug`` (or legacy fallback)."""
    return load_profile(slug=slug, config=config)


def _initial_profile_slug(config: dict[str, Any]) -> str | None:
    """Pick the slug to seed the sidebar selector with."""
    env_slug = os.environ.get("JOB_AGENT_PROFILE")
    if env_slug:
        return resolve_active_slug(config, override=env_slug)
    return resolve_active_slug(config)


def jobs_dataframe(db: Database) -> pd.DataFrame:
    rows = db.get_jobs()
    if not rows:
        return pd.DataFrame(
            columns=[
                "id", "title", "company", "location", "platform", "job_type",
                "url", "description", "date_scraped", "match_score", "status",
            ]
        )
    df = pd.DataFrame(rows)
    if "match_score" in df.columns:
        df["match_score"] = pd.to_numeric(df["match_score"], errors="coerce")
    if "date_scraped" in df.columns:
        df["date_scraped_dt"] = pd.to_datetime(df["date_scraped"], errors="coerce")
    return df


def applications_dataframe(db: Database, jobs_df: pd.DataFrame) -> pd.DataFrame:
    apps = db.get_applications()
    apps_df = pd.DataFrame(apps)
    if apps_df.empty:
        return pd.DataFrame(
            columns=[
                "id", "job_id", "title", "company", "platform", "date_applied",
                "status", "notes", "resume_version", "cover_letter_path", "url",
            ]
        )
    join_cols = ["id", "title", "company", "platform", "url"]
    have = [c for c in join_cols if c in jobs_df.columns]
    apps_df = apps_df.merge(
        jobs_df[have].rename(columns={"id": "job_id"}),
        on="job_id",
        how="left",
        suffixes=("", "_job"),
    )
    apps_df["date_applied_dt"] = pd.to_datetime(apps_df["date_applied"], errors="coerce")
    return apps_df


# =============================================================================
# Pages
# =============================================================================
def page_overview(db: Database) -> None:
    page_header("🏠 Overview", "Pipeline snapshot and quick actions")

    stats = db.get_stats()
    apps_by = stats.get("applications_by_status", {}) or {}
    total_jobs = stats.get("total_jobs", 0)

    st.markdown(
        """
        <div class="hero-banner">
            <h3>Your job search command center</h3>
            <p>Track scraped listings, match scores, and application outcomes in one place.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Jobs Found", total_jobs)
    c2.metric("Applied", apps_by.get("Applied", 0))
    c3.metric("Interviews", apps_by.get("Interview", 0))
    c4.metric("Offers", apps_by.get("Offer", 0))
    c5.metric("Rejected", apps_by.get("Rejected", 0))

    section_header("Quick actions")
    qa1, qa2, qa3 = st.columns(3)
    with qa1:
        st.markdown(
            """
            <div class="action-chip">
                <strong>🔍 Scrape jobs</strong>
                <div class="cmd">python main.py --mode scrape</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Copy scrape command", key="qa_scrape", use_container_width=True):
            st.toast("Run: python main.py --mode scrape", icon="📋")
    with qa2:
        st.markdown(
            """
            <div class="action-chip">
                <strong>🧠 Analyze matches</strong>
                <div class="cmd">python main.py --mode analyze --limit 25</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Copy analyze command", key="qa_analyze", use_container_width=True):
            st.toast("Run: python main.py --mode analyze --limit 25", icon="📋")
    with qa3:
        st.markdown(
            """
            <div class="action-chip">
                <strong>🚀 Apply guide</strong>
                <div class="cmd">python main.py --mode apply --min-score 70</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Copy apply command", key="qa_apply", use_container_width=True):
            st.toast("Run: python main.py --mode apply --min-score 70", icon="📋")

    st.markdown("&nbsp;")

    left, right = st.columns([1, 1.2])

    with left:
        section_header("Applications by status")
        if apps_by:
            pie_df = pd.DataFrame(
                [{"status": s, "count": apps_by.get(s, 0)} for s in APP_STATUS_ORDER]
            )
            pie_df = pie_df[pie_df["count"] > 0]
            if pie_df.empty:
                empty_state(
                    "📋",
                    "No applications yet",
                    "Run the apply agent to start tracking outcomes.",
                    "python main.py --mode apply --min-score 70",
                )
            else:
                fig = px.pie(
                    pie_df,
                    values="count",
                    names="status",
                    hole=0.5,
                    color="status",
                    color_discrete_map=APP_STATUS_COLOR,
                    template=PLOTLY_TEMPLATE,
                )
                fig.update_traces(textposition="outside", textinfo="label+percent")
                fig.update_layout(
                    height=360, margin=dict(t=10, b=10, l=10, r=10),
                    legend=dict(orientation="h", y=-0.05),
                )
                st.plotly_chart(fig, use_container_width=True)
        else:
            empty_state(
                "📋",
                "No applications yet",
                "Run the apply agent to start tracking outcomes.",
                "python main.py --mode apply --min-score 70",
            )

    with right:
        section_header("Jobs by platform")
        by_platform = stats.get("by_platform", {}) or {}
        if by_platform:
            bar_df = pd.DataFrame(
                [{"platform": platform_label(k), "count": v} for k, v in by_platform.items()]
            ).sort_values("count", ascending=True)
            fig = px.bar(
                bar_df,
                x="count", y="platform", orientation="h",
                template=PLOTLY_TEMPLATE,
                color="count",
                color_continuous_scale="Tealgrn",
                text="count",
            )
            fig.update_traces(textposition="outside")
            fig.update_layout(
                height=360, margin=dict(t=10, b=10, l=10, r=10),
                xaxis_title=None, yaxis_title=None,
                coloraxis_showscale=False,
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            empty_state(
                "🔍",
                "No jobs scraped yet",
                "Scrape job boards to populate the database.",
                "python main.py --mode scrape",
            )


def page_job_browser(db: Database) -> None:
    page_header(
        "🔍 Job Browser",
        "Filter listings, view match scores, and read AI explanations",
    )
    config = load_config()
    slug = st.session_state.get("profile_slug")
    green_min, yellow_min = badge_thresholds(config)

    total_jobs = len(db.get_jobs())
    if total_jobs == 0:
        empty_state(
            "📭",
            "No jobs in the database",
            "Run a scrape to discover listings across platforms.",
            "python main.py --mode scrape",
        )
        return

    filter_keys = [
        "jb_date_preset", "jb_custom_start", "jb_custom_end",
        "jb_locations", "jb_seniority", "jb_title_terms", "jb_desc_terms",
        "jb_platforms", "jb_score_low", "jb_status", "jb_sort_match", "jb_search",
    ]

    fc1, fc2 = st.columns([5, 1])
    with fc1:
        section_header("Filters")
    with fc2:
        st.markdown("&nbsp;")
        if st.button("Clear filters", use_container_width=True, key="jb_clear"):
            for k in filter_keys:
                st.session_state.pop(k, None)
            st.rerun()

    with st.expander("Filter panel", expanded=True):
        r1c1, r1c2, r1c3 = st.columns(3)
        with r1c1:
            date_preset = st.selectbox(
                "Posted within",
                ["All time", "Last 7 days", "Last 14 days", "Last 30 days", "Custom range"],
                index=0,
                key="jb_date_preset",
            )
        with r1c2:
            use_custom = date_preset == "Custom range"
            custom_start = st.date_input(
                "From",
                value=datetime.now().date() - timedelta(days=30),
                disabled=not use_custom,
                key="jb_custom_start",
            )
        with r1c3:
            custom_end = st.date_input(
                "To",
                value=datetime.now().date(),
                disabled=not use_custom,
                key="jb_custom_end",
            )

        r2c1, r2c2 = st.columns(2)
        with r2c1:
            loc_options = db.get_distinct_locations()
            picked_locations = st.multiselect(
                "Location",
                loc_options,
                default=[],
                placeholder="Any location",
                key="jb_locations",
            )
        with r2c2:
            seniority_options = ["junior", "mid", "senior", "unknown"]
            picked_seniority = st.multiselect(
                "Seniority",
                seniority_options,
                default=[],
                placeholder="Any seniority",
                key="jb_seniority",
            )

        r3c1, r3c2 = st.columns(2)
        with r3c1:
            title_terms_raw = st.text_input(
                "Title contains (comma-separated, OR)",
                placeholder="ML Engineer, Data Scientist",
                key="jb_title_terms",
            )
        with r3c2:
            desc_terms_raw = st.text_input(
                "Description contains (comma-separated, OR)",
                placeholder="Python, PyTorch, remote",
                key="jb_desc_terms",
            )

        r4c1, r4c2, r4c3, r4c4 = st.columns([1.2, 1.4, 1.2, 1.4])
        with r4c1:
            platforms = sorted(
                p for p in {j.get("platform") for j in db.get_jobs()} if p
            )
            picked_platforms = st.multiselect(
                "Platform", platforms, default=platforms, key="jb_platforms"
            )
        with r4c2:
            score_low = st.slider(
                "Min match score",
                min_value=0, max_value=100, value=0, step=5,
                help="Jobs without a score are excluded when min > 0.",
                key="jb_score_low",
            )
        with r4c3:
            statuses = sorted(
                s for s in {j.get("status") for j in db.get_jobs()} if s
            )
            status_pick = st.multiselect(
                "Status", statuses, default=statuses, key="jb_status"
            )
        with r4c4:
            sort_by_match = st.checkbox(
                "Top resume matches first",
                value=False,
                help="Sort by match_score descending (nulls last).",
                key="jb_sort_match",
            )

        q = st.text_input(
            "Search title / company", placeholder="ML, Acme, ...", key="jb_search"
        )

    posted_after: str | None = None
    posted_before: str | None = None
    if date_preset == "Last 7 days":
        posted_after = (datetime.now() - timedelta(days=7)).isoformat()
    elif date_preset == "Last 14 days":
        posted_after = (datetime.now() - timedelta(days=14)).isoformat()
    elif date_preset == "Last 30 days":
        posted_after = (datetime.now() - timedelta(days=30)).isoformat()
    elif date_preset == "Custom range":
        posted_after = datetime.combine(custom_start, datetime.min.time()).isoformat()
        posted_before = datetime.combine(
            custom_end, datetime.max.time()
        ).isoformat()

    title_terms = [
        t.strip() for t in title_terms_raw.split(",") if t.strip()
    ] if title_terms_raw else None
    desc_terms = [
        t.strip() for t in desc_terms_raw.split(",") if t.strip()
    ] if desc_terms_raw else None

    rows = db.get_jobs_filtered(
        platforms=picked_platforms or None,
        min_score=score_low if score_low > 0 else None,
        locations=picked_locations or None,
        title_terms=title_terms,
        description_terms=desc_terms,
        seniority_levels=picked_seniority or None,
        posted_after=posted_after,
        posted_before=posted_before,
        sort_by_match=sort_by_match,
    )

    fdf = pd.DataFrame(rows) if rows else pd.DataFrame()
    if not fdf.empty:
        if "match_score" in fdf.columns:
            fdf["match_score"] = pd.to_numeric(fdf["match_score"], errors="coerce")
        if status_pick:
            fdf = fdf[fdf["status"].isin(status_pick)]
        if q:
            ql = q.lower()
            fdf = fdf[
                fdf["title"].fillna("").str.lower().str.contains(ql)
                | fdf["company"].fillna("").str.lower().str.contains(ql)
            ]

    st.caption(f"**{len(fdf)}** / {total_jobs} jobs after filters")

    if fdf.empty:
        empty_state(
            "🔎",
            "Nothing matches your filters",
            "Try clearing filters or broadening your search criteria.",
        )
        return

    explanations = db.get_match_explanations_for_jobs(
        [int(x) for x in fdf["id"].tolist()],
        profile_slug=slug,
    )

    # ---- pinned top matches -------------------------------------------
    scored_df = fdf[fdf["match_score"].notna()].copy()
    if not scored_df.empty:
        top_matches = scored_df[scored_df["match_score"] >= green_min].head(5)
        if not top_matches.empty:
            section_header(f"⭐ Top matches (score ≥ {green_min})")
            for _, row in top_matches.iterrows():
                render_job_card(row, config, explanations, pinned=True)
            st.divider()

    # ---- all results --------------------------------------------------
    section_header("All results")
    for _, row in fdf.iterrows():
        render_job_card(row, config, explanations)

    # ---- detail expander (legacy deep-dive) -------------------------
    section_header("Job details")
    if fdf.empty:
        empty_state("🔎", "Nothing matches your filters", "Adjust filters above.")
        return

    def _row_label(row):
        return (
            f"#{row['id']} · {row['title']} @ {row['company']} "
            f"({platform_label(row['platform'])})"
        )

    options = [_row_label(r) for _, r in fdf.iterrows()]
    idx = st.selectbox(
        "Pick a job to inspect",
        range(len(options)),
        format_func=lambda i: options[i],
    )
    row = fdf.iloc[idx]

    head_a, head_b, head_c = st.columns([3, 1, 1])
    head_a.markdown(f"### {row['title']}")
    head_b.markdown(
        f"**Match**<br>{score_pill(row['match_score'], config)}",
        unsafe_allow_html=True,
    )
    head_c.markdown(f"**Status**<br>{status_pill(row['status'])}", unsafe_allow_html=True)

    meta_a, meta_b, meta_c, meta_d = st.columns(4)
    meta_a.markdown(f"**Company**<br>{row['company'] or '—'}", unsafe_allow_html=True)
    meta_b.markdown(f"**Platform**<br>{platform_label(row['platform'])}", unsafe_allow_html=True)
    meta_c.markdown(f"**Location**<br>{row.get('location') or '—'}", unsafe_allow_html=True)
    meta_d.markdown(f"**Type**<br>{row.get('job_type') or '—'}", unsafe_allow_html=True)

    if row.get("url"):
        st.markdown(f"[Open original posting ↗]({row['url']})")

    with st.expander("📄 Full job description", expanded=True):
        desc = row.get("description") or ""
        if desc.strip():
            st.write(desc)
        else:
            st.caption("No description was scraped for this job.")

    cached = db.get_jd_analysis(int(row["id"]))
    if cached:
        with st.expander("🧠 JD analysis (cached)", expanded=False):
            st.json(cached)

    expl_detail = explanations.get(int(row["id"]))
    if expl_detail:
        with st.expander("💬 Match explanation", expanded=False):
            st.write(expl_detail)


def page_resume_review(db: Database) -> None:
    """Review tailored resume diffs before PDF generation."""
    page_header(
        "📝 Review Tailored Resume",
        "Accept or reject AI-suggested edits before generating your PDF",
    )
    config = load_config()
    if not (config.get("resume_review") or {}).get("enabled", True):
        st.info("Resume review is disabled in config (resume_review.enabled: false).")
        return

    slug = st.session_state.get("profile_slug")
    profile = load_active_profile(config, slug)
    if not profile.get("skills") and not profile.get("experience"):
        hint = f"profile `{slug}`" if slug else "a profile"
        empty_state(
            "📄",
            "No parsed profile",
            f"Upload and parse a resume for {hint} first.",
        )
        if st.button("Go to Profiles / Resumes", key="rr_goto_profiles"):
            st.session_state["nav_page"] = "📄 Profiles / Resumes"
            st.rerun()
        return

    jobs_df = jobs_dataframe(db)
    if jobs_df.empty:
        empty_state(
            "📭",
            "No jobs in the database",
            "Scrape jobs before tailoring resumes.",
            "python main.py --mode scrape",
        )
        return

    scored = jobs_df[jobs_df["match_score"].notna()].sort_values(
        "match_score", ascending=False
    )
    if scored.empty:
        empty_state(
            "🧠",
            "No scored jobs",
            "Run analyze to compute match scores.",
            "python main.py --mode analyze --limit 25",
        )
        return

    def _job_label(r: pd.Series) -> str:
        return (
            f"#{int(r['id'])} · {r['title']} @ {r.get('company') or '—'} "
            f"({int(r['match_score'])}%)"
        )

    pick = st.selectbox(
        "Select job to tailor resume for",
        scored.index.tolist(),
        format_func=lambda i: _job_label(scored.loc[i]),
    )
    job_row = scored.loc[pick]
    job_id = int(job_row["id"])

    if st.button("Generate proposed changes", type="primary"):
        from ai.resume_customizer import propose_resume_customization

        analysis = db.get_jd_analysis(job_id)
        if not analysis:
            from ai.jd_analyzer import analyze_jd, infer_seniority_level

            jd_text = (job_row.get("description") or job_row.get("title") or "").strip()
            if not jd_text:
                st.error("Job has no description to analyze.")
                return
            try:
                analysis = analyze_jd(jd_text)
                analysis["seniority_level"] = infer_seniority_level(
                    jd_text,
                    job_title=job_row.get("title") or "",
                    experience_required=analysis.get("experience_required") or "",
                )
                db.insert_jd_analysis(job_id, analysis)
            except Exception as exc:
                gemini_error_banner(f"JD analysis failed: {exc}")
                st.error(f"JD analysis failed: {exc}")
                return

        try:
            _, diffs = propose_resume_customization(profile, analysis)
        except Exception as exc:
            err = str(exc).lower()
            if "api" in err or "key" in err or "quota" in err or "429" in err:
                gemini_error_banner(
                    "Gemini API error — check GEMINI_API_KEY in .env and your quota."
                )
            st.error(f"Resume customization failed: {exc}")
            return

        st.session_state["resume_review_diffs"] = diffs
        st.session_state["resume_review_job_id"] = job_id
        st.session_state["resume_review_analysis"] = analysis
        st.session_state["resume_review_accepted"] = list(range(len(diffs)))
        st.toast(f"Generated {len(diffs)} proposed change(s)", icon="✨")

    diffs = st.session_state.get("resume_review_diffs")
    if not diffs:
        st.info(
            "Click **Generate proposed changes** to see AI-suggested edits "
            "side-by-side. Accept or reject each change, then render PDF."
        )
        return

    if st.session_state.get("resume_review_job_id") != job_id:
        st.warning("Selected job changed — regenerate proposed changes.")
        return

    section_header(f"{len(diffs)} proposed change(s)")

    bulk1, bulk2, bulk3 = st.columns([1, 1, 3])
    with bulk1:
        if st.button("Accept all", use_container_width=True, key="rr_accept_all"):
            for i in range(len(diffs)):
                st.session_state[f"resume_diff_{i}"] = True
            st.toast("All changes accepted", icon="✅")
            st.rerun()
    with bulk2:
        if st.button("Reject all", use_container_width=True, key="rr_reject_all"):
            for i in range(len(diffs)):
                st.session_state[f"resume_diff_{i}"] = False
            st.toast("All changes rejected", icon="❌")
            st.rerun()

    accepted: list[int] = []
    for i, diff in enumerate(diffs):
        default_accept = st.session_state.get(f"resume_diff_{i}", True)
        with st.expander(
            f"Change {i + 1}: {diff.get('section', 'section')}",
            expanded=i == 0,
        ):
            col_orig, col_sugg = st.columns(2)
            orig_text = html.escape(diff.get("original_text") or "(empty)")
            sugg_text = html.escape(diff.get("suggested_text") or "")
            with col_orig:
                st.markdown(
                    '<p class="diff-label diff-label-original">Original</p>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div class="diff-col diff-original">{orig_text}</div>',
                    unsafe_allow_html=True,
                )
            with col_sugg:
                st.markdown(
                    '<p class="diff-label diff-label-suggested">Suggested</p>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div class="diff-col diff-suggested">{sugg_text}</div>',
                    unsafe_allow_html=True,
                )
            if st.checkbox(
                "Accept this change",
                value=default_accept,
                key=f"resume_diff_{i}",
            ):
                accepted.append(i)

    if st.button("Render PDF with accepted changes", type="primary"):
        from ai.resume_customizer import customize_resume

        analysis = st.session_state.get("resume_review_analysis")
        out_dir = (config.get("output") or {}).get("resumes_dir") or "output/resumes"
        try:
            _, pdf_path, _ = customize_resume(
                profile,
                analysis,
                company=str(job_row.get("company") or ""),
                role=str(job_row.get("title") or ""),
                output_dir=PROJECT_ROOT / out_dir,
                accepted_diff_indices=accepted,
                diffs=diffs,
            )
            st.success(f"PDF saved: {pdf_path}")
            st.toast("Tailored resume PDF ready!", icon="📄")
            with pdf_path.open("rb") as f:
                st.download_button(
                    "Download tailored resume",
                    data=f.read(),
                    file_name=pdf_path.name,
                    mime="application/pdf",
                )
        except Exception as exc:
            st.error(f"PDF render failed: {exc}")


def page_applications(db: Database) -> None:
    page_header("📋 Applications", "Track status and notes for every submission")

    jobs_df = jobs_dataframe(db)
    apps_df = applications_dataframe(db, jobs_df)

    if apps_df.empty:
        empty_state(
            "📋",
            "No applications yet",
            "Run the apply agent to record submissions.",
            "python main.py --mode apply --min-score 70",
        )
        return

    a1, a2, a3, a4 = st.columns(4)
    counts = apps_df["status"].value_counts().to_dict()
    a1.metric("Applied", counts.get("Applied", 0))
    a2.metric("Interview", counts.get("Interview", 0))
    a3.metric("Offer", counts.get("Offer", 0))
    a4.metric("Rejected", counts.get("Rejected", 0))

    st.markdown("&nbsp;")
    section_header("Application tracker")

    edit_cols = [
        "id", "job_id", "title", "company", "platform",
        "date_applied", "status", "notes", "url",
    ]
    for c in edit_cols:
        if c not in apps_df.columns:
            apps_df[c] = None
    display_df = apps_df[edit_cols].copy()
    display_df["platform"] = display_df["platform"].apply(platform_label)
    display_df["notes"] = display_df["notes"].fillna("")

    edited = st.data_editor(
        display_df,
        hide_index=True,
        use_container_width=True,
        height=460,
        key="apps_editor",
        column_config={
            "id": st.column_config.NumberColumn("App ID", width="small", disabled=True),
            "job_id": st.column_config.NumberColumn("Job ID", width="small", disabled=True),
            "title": st.column_config.TextColumn("Title", disabled=True),
            "company": st.column_config.TextColumn("Company", disabled=True),
            "platform": st.column_config.TextColumn("Platform", disabled=True),
            "date_applied": st.column_config.TextColumn("Applied", disabled=True),
            "status": st.column_config.SelectboxColumn(
                "Status",
                options=APP_STATUS_ORDER,
                required=True,
            ),
            "notes": st.column_config.TextColumn("Notes", width="large"),
            "url": st.column_config.LinkColumn("URL", disabled=True),
        },
    )

    save = st.button("Save status / notes changes", type="primary")
    if save:
        changes = 0
        for _, new in edited.iterrows():
            orig = apps_df.loc[apps_df["id"] == new["id"]]
            if orig.empty:
                continue
            o = orig.iloc[0]
            new_status = new.get("status") or o.get("status")
            new_notes = new.get("notes") or ""
            if new_status not in APPLICATION_STATUSES:
                continue
            if new_status != o.get("status") or new_notes != (o.get("notes") or ""):
                try:
                    db.update_application_status(
                        int(new["id"]),
                        new_status,
                        notes=new_notes if new_notes else None,
                    )
                    changes += 1
                except Exception as exc:
                    st.error(f"Failed to update app id={new['id']}: {exc}")
        if changes:
            st.success(f"Saved {changes} change(s).")
            st.toast(f"Updated {changes} application(s)", icon="💾")
            st.rerun()
        else:
            st.info("No changes detected.")


def page_analytics(db: Database) -> None:
    page_header("📊 Analytics", "Match scores, in-demand skills, and application trends")
    config = load_config()
    green_min, yellow_min = badge_thresholds(config)

    jobs_df = jobs_dataframe(db)
    apps_df = applications_dataframe(db, jobs_df)

    section_header("Match score distribution")
    scored = jobs_df[jobs_df["match_score"].notna()] if not jobs_df.empty else jobs_df
    if scored.empty:
        empty_state(
            "📈",
            "No match scores yet",
            "Run analyze to score jobs against your profile.",
            "python main.py --mode analyze --limit 25",
        )
    else:
        fig = px.histogram(
            scored, x="match_score", nbins=20,
            template=PLOTLY_TEMPLATE,
            color_discrete_sequence=["#3b82f6"],
        )
        fig.add_vrect(x0=green_min, x1=100, fillcolor="#10b981", opacity=0.08, line_width=0)
        fig.add_vrect(x0=yellow_min, x1=green_min, fillcolor="#f59e0b", opacity=0.08, line_width=0)
        fig.add_vrect(x0=0, x1=yellow_min, fillcolor="#ef4444", opacity=0.08, line_width=0)
        fig.update_layout(
            height=320, margin=dict(t=10, b=10, l=10, r=10),
            xaxis_title="Match score", yaxis_title="Jobs",
            bargap=0.05,
        )
        st.plotly_chart(fig, use_container_width=True)

    section_header("Top skills in demand (from cached JD analyses)")
    skill_counter: Counter[str] = Counter()
    tool_counter: Counter[str] = Counter()
    all_analyses = db.get_all_jd_analyses()
    analyzed = len(all_analyses)
    for analysis in all_analyses.values():
        for s in (analysis.get("required_skills") or []):
            if isinstance(s, str) and s.strip():
                skill_counter[s.strip()] += 1
        for s in (analysis.get("preferred_skills") or []):
            if isinstance(s, str) and s.strip():
                skill_counter[s.strip()] += 1
        for t in (analysis.get("tools") or []):
            if isinstance(t, str) and t.strip():
                tool_counter[t.strip()] += 1

    if analyzed == 0:
        empty_state(
            "🧠",
            "No JD analyses cached",
            "Run analyze or apply to populate JD analysis data.",
            "python main.py --mode analyze --limit 25",
        )
    else:
        col_s, col_t = st.columns(2)
        with col_s:
            top_skills = skill_counter.most_common(15)
            if top_skills:
                sk_df = pd.DataFrame(top_skills, columns=["skill", "count"]).iloc[::-1]
                fig = px.bar(
                    sk_df, x="count", y="skill", orientation="h",
                    template=PLOTLY_TEMPLATE,
                    color="count", color_continuous_scale="Tealgrn",
                    text="count",
                )
                fig.update_traces(textposition="outside")
                fig.update_layout(
                    height=420, margin=dict(t=10, b=10, l=10, r=10),
                    xaxis_title=None, yaxis_title=None, coloraxis_showscale=False,
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.caption("No skills extracted yet.")
        with col_t:
            top_tools = tool_counter.most_common(15)
            if top_tools:
                tl_df = pd.DataFrame(top_tools, columns=["tool", "count"]).iloc[::-1]
                fig = px.bar(
                    tl_df, x="count", y="tool", orientation="h",
                    template=PLOTLY_TEMPLATE,
                    color="count", color_continuous_scale="Sunsetdark",
                    text="count",
                )
                fig.update_traces(textposition="outside")
                fig.update_layout(
                    height=420, margin=dict(t=10, b=10, l=10, r=10),
                    xaxis_title=None, yaxis_title=None, coloraxis_showscale=False,
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.caption("No tools extracted yet.")

    section_header("Application activity over time")
    if apps_df.empty or apps_df["date_applied_dt"].isna().all():
        empty_state(
            "📅",
            "No applications recorded yet",
            "Applications will appear here once you start applying.",
        )
        return

    daily = (
        apps_df.assign(day=apps_df["date_applied_dt"].dt.date)
        .groupby(["day", "status"])
        .size()
        .reset_index(name="count")
    )
    pivot = daily.pivot(index="day", columns="status", values="count").fillna(0).sort_index()
    for s in APP_STATUS_ORDER:
        if s not in pivot.columns:
            pivot[s] = 0
    pivot = pivot[APP_STATUS_ORDER]
    cumulative = pivot.cumsum().reset_index().melt(id_vars="day", var_name="status", value_name="count")
    cumulative["day"] = pd.to_datetime(cumulative["day"])

    fig = px.line(
        cumulative, x="day", y="count", color="status",
        markers=True,
        template=PLOTLY_TEMPLATE,
        color_discrete_map=APP_STATUS_COLOR,
    )
    fig.update_layout(
        height=320, margin=dict(t=10, b=10, l=10, r=10),
        xaxis_title=None, yaxis_title="Cumulative",
        legend=dict(orientation="h", y=-0.15),
    )
    st.plotly_chart(fig, use_container_width=True)

    applied = int(pivot["Applied"].sum() + pivot["Interview"].sum()
                  + pivot["Offer"].sum() + pivot["Rejected"].sum())
    interviews = int(pivot["Interview"].sum() + pivot["Offer"].sum())
    offers = int(pivot["Offer"].sum())
    cr1, cr2, cr3 = st.columns(3)
    cr1.metric(
        "Interview rate",
        f"{(interviews / applied * 100):.1f}%" if applied else "—",
        help="(# applications that reached Interview or Offer) / total",
    )
    cr2.metric(
        "Offer rate",
        f"{(offers / applied * 100):.1f}%" if applied else "—",
        help="# offers / total applications",
    )
    cr3.metric(
        "Interview -> Offer",
        f"{(offers / interviews * 100):.1f}%" if interviews else "—",
        help="# offers / # interview-stage applications",
    )


def page_profiles_resumes(db: Database) -> None:
    """Upload resume PDFs and parse them into named profiles."""
    page_header(
        "📄 Profiles / Resumes",
        "Upload PDFs, parse with Gemini, and manage profile slugs",
    )

    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()
    slugs = list_profiles()

    st.markdown("##### Upload & parse")
    mode = st.radio(
        "Profile",
        ["Create new profile", "Update existing profile"],
        horizontal=True,
    )

    slug: str = ""
    if mode == "Update existing profile":
        if not slugs:
            st.info("No existing profiles yet — create one below.")
        else:
            slug = st.selectbox("Existing profile slug", slugs)
    else:
        name_input = st.text_input(
            "Profile name",
            placeholder="e.g. data_science or Full Stack",
            help="Used as the slug for resume/<slug>.pdf and config/profiles/<slug>.json",
        )
        if name_input.strip():
            slug = slugify(name_input)
            st.caption(f"Slug: `{slug}`")

    uploaded = st.file_uploader(
        "Resume PDF — drag & drop or browse",
        type=["pdf"],
        help="Required for new profiles; optional when re-parsing an existing upload.",
    )

    pdf_path = resolve_resume_pdf(slug or None, config=config) if slug else None
    has_pdf_on_disk = bool(slug and pdf_path and pdf_path.exists())
    has_parsed = bool(slug and profile_path(slug).exists())

    if slug and has_pdf_on_disk and pdf_path:
        st.caption(f"On disk: `{pdf_path.relative_to(PROJECT_ROOT)}`")

    if not slug:
        wizard_step = 1
    elif uploaded is not None or has_pdf_on_disk:
        wizard_step = 2 if not has_parsed else 3
    else:
        wizard_step = 1

    render_wizard_steps(
        wizard_step,
        has_upload=uploaded is not None or has_pdf_on_disk,
        has_parsed=has_parsed,
    )

    parse_clicked = st.button("Parse resume", type="primary")
    if parse_clicked:
        if not slug:
            st.error("Enter a profile name or pick an existing profile.")
            return

        if uploaded is not None:
            dest = RESUME_DIR / f"{slug}.pdf"
            try:
                dest.write_bytes(uploaded.getvalue())
                pdf_path = dest
                st.success(f"Saved PDF → `{dest.relative_to(PROJECT_ROOT)}`")
                st.toast("Resume PDF uploaded", icon="📄")
            except OSError as exc:
                st.error(f"Could not save PDF: {exc}")
                return
        else:
            pdf_path = resolve_resume_pdf(slug, config=load_config())
            if not pdf_path.exists():
                st.error(
                    f"No PDF uploaded and `{pdf_path.relative_to(PROJECT_ROOT)}` "
                    "not found. Upload a PDF first."
                )
                return

        out_path = profile_path(slug)
        with st.spinner("Parsing resume with Gemini…"):
            try:
                from ai.resume_parser import parse_resume

                profile = parse_resume(pdf_path, output_path=out_path)
            except RuntimeError as exc:
                gemini_error_banner(str(exc))
                st.error(f"Resume parsing failed: {exc}")
                return
            except Exception as exc:
                err = str(exc).lower()
                if "api" in err or "key" in err or "quota" in err or "429" in err:
                    gemini_error_banner(
                        "Gemini API error — check GEMINI_API_KEY in .env and your quota."
                    )
                    st.error(
                        "Gemini API error — check `GEMINI_API_KEY` in `.env` and "
                        f"your quota. Details: {exc}"
                    )
                else:
                    st.error(f"Unexpected error while parsing: {exc}")
                return

        st.session_state["profile_slug"] = slug
        name = profile.get("name") or slug
        st.success(
            f"Parsed **{name}** → `{out_path.relative_to(PROJECT_ROOT)}`. "
            "Select this profile in the sidebar for Job Browser and apply."
        )
        st.toast(f"Profile '{slug}' ready!", icon="✅")
        st.rerun()

    st.divider()
    section_header("Existing profiles")

    all_slugs = sorted(set(slugs) | {p.stem for p in RESUME_DIR.glob("*.pdf")})
    if not all_slugs:
        empty_state(
            "📄",
            "No profiles yet",
            "Upload a PDF above to create your first profile.",
        )
        return

    for s in all_slugs:
        prof = load_profile(slug=s, config=load_config())
        pdf = resolve_resume_pdf(s, config=load_config())
        has_json = profile_path(s).exists()
        has_pdf = pdf.exists()
        is_active = st.session_state.get("profile_slug") == s
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 1, 1])
            active_badge = " · **active**" if is_active else ""
            c1.markdown(f"**`{s}`**{active_badge}")
            if prof.get("name"):
                c1.caption(prof.get("name"))
            c2.markdown(
                f"JSON: {'✅' if has_json else '—'} · PDF: {'✅' if has_pdf else '—'}"
            )
            if has_pdf:
                with pdf.open("rb") as f:
                    c3.download_button(
                        "Download PDF",
                        data=f.read(),
                        file_name=pdf.name,
                        mime="application/pdf",
                        key=f"dl_pdf_{s}",
                    )
            if has_json:
                skills_n = len(prof.get("skills") or [])
                exp_n = len(prof.get("experience") or [])
                st.caption(f"{skills_n} skills · {exp_n} experience entries")
            if has_json and not is_active:
                if st.button(f"Set as active profile", key=f"activate_{s}"):
                    st.session_state["profile_slug"] = s
                    st.toast(f"Active profile: {s}", icon="👤")
                    st.rerun()


def page_settings(db: Database) -> None:
    page_header("⚙️ Settings", "Job-search targets, config, and profile summary")

    config = load_config()
    slug = st.session_state.get("profile_slug")
    profile = load_active_profile(config, slug)

    section_header("Job-search targets")
    job_search = (config.get("job_search") or {}).copy()
    roles_default = "\n".join(job_search.get("roles") or [])
    locations_default = "\n".join(job_search.get("locations") or [])

    c1, c2 = st.columns(2)
    with c1:
        roles_text = st.text_area(
            "Target roles (one per line)",
            value=roles_default,
            height=180,
            help="The roles your scrapers will search for.",
        )
    with c2:
        locations_text = st.text_area(
            "Target locations (one per line)",
            value=locations_default,
            height=180,
            help="Pass 'Remote' to include WFH listings.",
        )

    c3, c4 = st.columns(2)
    with c3:
        min_score_new = st.slider(
            "Minimum match score",
            min_value=0, max_value=100,
            value=int(job_search.get("min_match_score", 70)),
            step=5,
            help="Jobs scoring below this are not auto-applied.",
        )
    with c4:
        max_per_platform = st.number_input(
            "Max results per platform",
            min_value=1, max_value=500,
            value=int(job_search.get("max_results_per_platform", 25)),
            step=5,
        )

    if st.button("Save settings", type="primary"):
        new_cfg = dict(config)
        new_cfg.setdefault("job_search", {})
        new_cfg["job_search"]["roles"] = [
            r.strip() for r in roles_text.splitlines() if r.strip()
        ]
        new_cfg["job_search"]["locations"] = [
            l.strip() for l in locations_text.splitlines() if l.strip()
        ]
        new_cfg["job_search"]["min_match_score"] = int(min_score_new)
        new_cfg["job_search"]["max_results_per_platform"] = int(max_per_platform)
        try:
            save_config(new_cfg)
            st.success(f"Saved -> {CONFIG_PATH.relative_to(PROJECT_ROOT)}")
            st.toast("Settings saved", icon="💾")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not save config: {exc}")

    st.divider()

    section_header("Full config.yaml")
    with st.expander("View raw YAML", expanded=False):
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            st.code(raw, language="yaml")
        except Exception as exc:
            st.error(f"Could not read config: {exc}")

    st.divider()

    slug_label = slug or "(default)"
    profile_src = (
        f"config/profiles/{slug}.json" if slug else "config/user_profile.json"
    )
    section_header(f"User profile · `{slug_label}` (from `{profile_src}`)")
    if not profile:
        empty_state(
            "👤",
            "No parsed profile for this slug",
            "Upload and parse a resume under Profiles / Resumes.",
        )
        return

    p1, p2, p3 = st.columns(3)
    p1.metric("Name", profile.get("name") or "—")
    p2.metric("Skills", len(profile.get("skills") or []))
    p3.metric("Experience entries", len(profile.get("experience") or []))

    q1, q2, q3 = st.columns(3)
    q1.metric("Programming langs", len(profile.get("programming_languages") or []))
    q2.metric("Frameworks", len(profile.get("frameworks") or []))
    q3.metric("Projects", len(profile.get("projects") or []))

    with st.expander("Summary"):
        st.write(profile.get("summary") or "(no summary)")
    with st.expander("Skills"):
        st.write(", ".join(profile.get("skills") or []) or "—")
    with st.expander("Experience"):
        for e in profile.get("experience") or []:
            head = f"**{e.get('role', '')}** · {e.get('company', '')}"
            if e.get("duration"):
                head += f" _{e['duration']}_"
            st.markdown(head)
            for b in e.get("description") or []:
                st.markdown(f"- {b}")
    with st.expander("Projects"):
        for p in profile.get("projects") or []:
            techs = ", ".join(p.get("technologies") or [])
            st.markdown(f"**{p.get('name', '')}** _({techs})_")
            desc = p.get("description")
            if isinstance(desc, list):
                for b in desc:
                    st.markdown(f"- {b}")
            elif desc:
                st.write(desc)


# =============================================================================
# Main
# =============================================================================
PAGES: dict[str, Any] = {
    "🏠 Overview": page_overview,
    "🔍 Job Browser": page_job_browser,
    "📄 Profiles / Resumes": page_profiles_resumes,
    "📝 Review Tailored Resume": page_resume_review,
    "📋 Applications": page_applications,
    "📊 Analytics": page_analytics,
    "⚙️ Settings": page_settings,
}


def _flat_nav_pages() -> list[str]:
    return [p for pages in NAV_GROUPS.values() for p in pages]


def main() -> None:
    inject_css()
    db = get_db()
    config = load_config()

    flat_pages = _flat_nav_pages()
    if "nav_page" not in st.session_state or st.session_state["nav_page"] not in flat_pages:
        st.session_state["nav_page"] = flat_pages[0]

    with st.sidebar:
        st.markdown("# 🤖 AI Job Agent")
        st.caption("Local dashboard")

        slug = st.session_state.get("profile_slug")
        render_profile_badge(slug, config)

        st.divider()

        for group_name, pages in NAV_GROUPS.items():
            st.markdown(
                f'<p class="nav-group-label">{group_name}</p>',
                unsafe_allow_html=True,
            )
            for page in pages:
                is_active = st.session_state.get("nav_page") == page
                btn_type = "primary" if is_active else "secondary"
                if st.button(
                    page,
                    key=f"nav_{page}",
                    use_container_width=True,
                    type=btn_type,
                ):
                    st.session_state["nav_page"] = page
                    st.rerun()

        st.divider()

        slugs = list_profiles()
        if slugs:
            default_slug = _initial_profile_slug(config)
            options = ["(default)"] + slugs
            try:
                default_idx = options.index(default_slug) if default_slug else 0
            except ValueError:
                default_idx = 0
            picked = st.selectbox(
                "Active profile",
                options,
                index=default_idx,
                help=(
                    "Pick which parsed resume profile to use for matching "
                    "and apply. Upload profiles under Profiles / Resumes."
                ),
            )
            st.session_state["profile_slug"] = (
                None if picked == "(default)" else picked
            )
        else:
            st.session_state["profile_slug"] = _initial_profile_slug(config)

        st.divider()
        auto = st.checkbox(
            "Auto-refresh every 60s",
            value=False,
            help="Reloads the page every minute via an HTML meta refresh "
                 "(non-blocking).",
        )
        if auto:
            st.markdown(
                '<meta http-equiv="refresh" content="60">',
                unsafe_allow_html=True,
            )

        st.divider()
        stats = db.get_stats()
        st.caption(
            f"**Jobs**: {stats.get('total_jobs', 0)}  ·  "
            f"**Applications**: {stats.get('total_applied', 0)}"
        )
        st.caption(f"Updated {datetime.now().strftime('%H:%M:%S')}")

    choice = st.session_state["nav_page"]
    PAGES[choice](db)


main()
