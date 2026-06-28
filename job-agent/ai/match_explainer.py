"""Human-readable match explanations (Jobsuit-style).

Given a match result and job context, ask Gemini for a short 2-3 sentence
explanation. Results are cached per (job_id, profile_slug) in SQLite.
"""

from __future__ import annotations

from typing import Any

from .gemini_client import ask_gemini
from db.database import Database


PROMPT_TEMPLATE = """You explain job-resume match scores in plain English.

Write exactly 2-3 short sentences (no bullet points, no markdown).
Be specific about matched skills and gaps. Mention the job title once.
Do not invent skills the candidate does not have.

Job title: {job_title}
Match score: {match_score}/100
Recommendation: {recommendation}
Matched skills: {matched_skills}
Missing skills: {missing_skills}
JD experience required: {experience_required}
"""


def generate_match_explanation(
    *,
    job_title: str,
    match_score: int,
    matched_skills: list[str],
    missing_skills: list[str],
    jd_analysis: dict[str, Any],
    recommendation: str = "",
) -> str:
    """Ask Gemini for a concise match explanation.

    Returns the explanation text (never empty on success).
    """
    prompt = PROMPT_TEMPLATE.format(
        job_title=(job_title or "Unknown role").strip(),
        match_score=int(match_score),
        recommendation=recommendation or "N/A",
        matched_skills=", ".join(matched_skills[:12]) or "none",
        missing_skills=", ".join(missing_skills[:12]) or "none",
        experience_required=jd_analysis.get("experience_required") or "not specified",
    )
    text = ask_gemini(prompt, temperature=0.3, max_output_tokens=256)
    return str(text).strip()


def get_or_create_match_explanation(
    db: Database,
    *,
    job_id: int,
    profile_slug: str | None,
    job_title: str,
    match_result: dict[str, Any],
    jd_analysis: dict[str, Any],
    force: bool = False,
) -> str:
    """Return a cached explanation or generate and store a new one.

    Regenerates when ``force`` is True or no cached row exists for this
    job/profile pair.
    """
    slug = profile_slug or ""
    score = int(match_result.get("match_score") or 0)

    if not force:
        cached = db.get_match_explanation(job_id, slug)
        if cached and cached.get("explanation"):
            return str(cached["explanation"])

    explanation = generate_match_explanation(
        job_title=job_title,
        match_score=score,
        matched_skills=list(match_result.get("matched_skills") or []),
        missing_skills=list(match_result.get("missing_skills") or []),
        jd_analysis=jd_analysis,
        recommendation=str(match_result.get("recommendation") or ""),
    )
    db.upsert_match_explanation(
        job_id=job_id,
        profile_slug=slug,
        explanation=explanation,
        match_score_snapshot=score,
    )
    return explanation
