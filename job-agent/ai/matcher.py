"""Job-match scorer.

Given a parsed user profile (from `ai.resume_parser`) and a JD analysis
(from `ai.jd_analyzer`), compute a 0-100 match score plus matched/missing
skill lists and a human-readable recommendation.

The scoring is intentionally simple (case-insensitive keyword overlap) so
results are predictable and explainable. AI-based semantic scoring can be
layered on later.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# Weight applied to each JD section when computing the overall match score.
# Weights are normalized at runtime, so absolute magnitudes don't matter -
# only their ratios.
SECTION_WEIGHTS: dict[str, float] = {
    "required_skills": 0.60,
    "tools": 0.20,
    "keywords": 0.10,
    "preferred_skills": 0.10,
}

RECOMMENDATION_THRESHOLDS: list[tuple[int, str]] = [
    (80, "Strong Match"),
    (60, "Good Match"),
    (40, "Partial Match"),
    (0, "Low Match"),
]


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^a-z0-9+#./\s-]")
_WS_RE = re.compile(r"\s+")


def _normalize_term(term: Any) -> str:
    """Lowercase + trim + collapse whitespace + strip junk punctuation."""
    if term is None:
        return ""
    s = str(term).strip().lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _normalize_set(values: Any) -> set[str]:
    """Turn a list (or scalar) of skill strings into a set of normalized terms."""
    if not values:
        return set()
    if not isinstance(values, list):
        values = [values]
    out: set[str] = set()
    for v in values:
        n = _normalize_term(v)
        if n:
            out.add(n)
    return out


def _extract_user_terms(user_profile: dict[str, Any]) -> set[str]:
    """Collect every technical term the user knows into one normalized set."""
    fields = (
        "skills",
        "tools",
        "programming_languages",
        "frameworks",
        "databases",
    )
    pool: set[str] = set()
    for field in fields:
        pool |= _normalize_set(user_profile.get(field))
    return pool


def _match_terms(user_terms: set[str], jd_terms: set[str]) -> set[str]:
    """Return the JD terms the user has, with substring fallback.

    A JD term counts as matched if:
      1. It appears verbatim in the user's term set, OR
      2. It is a substring of a user term (or vice versa) and >= 3 chars
         (e.g. JD asks for "pytorch", user has "pytorch lightning").
    """
    matched: set[str] = set()
    for jd_term in jd_terms:
        if not jd_term:
            continue
        if jd_term in user_terms:
            matched.add(jd_term)
            continue
        if len(jd_term) < 3:
            continue
        for user_term in user_terms:
            if len(user_term) < 3:
                continue
            if jd_term in user_term or user_term in jd_term:
                matched.add(jd_term)
                break
    return matched


def _recommendation_for(score: int) -> str:
    for threshold, label in RECOMMENDATION_THRESHOLDS:
        if score >= threshold:
            return label
    return "Low Match"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_match(
    user_profile: dict[str, Any],
    jd_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Compute a match score between a user profile and a JD analysis.

    Args:
        user_profile: dict from `ai.resume_parser.parse_resume`.
        jd_analysis:  dict from `ai.jd_analyzer.analyze_jd`.

    Returns:
        {
            "match_score":      int 0-100,
            "matched_skills":   list[str],   sorted, deduped
            "missing_skills":   list[str],   from JD required_skills + tools
            "recommendation":   "Strong Match" | "Good Match" | "Partial Match" | "Low Match",
        }
    """
    if not isinstance(user_profile, dict):
        raise TypeError("user_profile must be a dict.")
    if not isinstance(jd_analysis, dict):
        raise TypeError("jd_analysis must be a dict.")

    user_terms = _extract_user_terms(user_profile)

    jd_sections: dict[str, set[str]] = {
        "required_skills": _normalize_set(jd_analysis.get("required_skills")),
        "tools": _normalize_set(jd_analysis.get("tools")),
        "keywords": _normalize_set(jd_analysis.get("keywords")),
        "preferred_skills": _normalize_set(jd_analysis.get("preferred_skills")),
    }

    # Per-section overlap ratios, then weighted average.
    weighted_sum = 0.0
    weight_total = 0.0
    all_matched: set[str] = set()

    for section, terms in jd_sections.items():
        weight = SECTION_WEIGHTS.get(section, 0.0)
        if not terms:
            continue
        matched = _match_terms(user_terms, terms)
        all_matched |= matched
        ratio = len(matched) / len(terms)
        weighted_sum += ratio * weight
        weight_total += weight

    if weight_total == 0:
        score = 0
    else:
        score = round((weighted_sum / weight_total) * 100)
    score = max(0, min(100, int(score)))

    # "Missing" focuses on the JD's hard requirements (required + tools).
    hard_required = jd_sections["required_skills"] | jd_sections["tools"]
    missing = sorted(hard_required - all_matched)
    matched_sorted = sorted(all_matched)

    return {
        "match_score": score,
        "matched_skills": matched_sorted,
        "missing_skills": missing,
        "recommendation": _recommendation_for(score),
    }


# Backwards-compatible alias for callers that prefer a shorter name.
match = compute_match


if __name__ == "__main__":
    demo_profile = {
        "skills": ["Machine Learning", "Deep Learning", "NLP"],
        "tools": ["Git", "Docker", "VS Code"],
        "programming_languages": ["Python", "SQL"],
        "frameworks": ["PyTorch", "FastAPI"],
        "databases": ["PostgreSQL"],
    }
    demo_jd = {
        "required_skills": ["Python", "PyTorch", "TensorFlow", "SQL"],
        "preferred_skills": ["AWS"],
        "tools": ["Docker", "Git"],
        "experience_required": "2+ years",
        "responsibilities": ["Build and deploy ML models"],
        "keywords": ["Machine Learning", "MLOps"],
    }
    import json as _json

    print(_json.dumps(compute_match(demo_profile, demo_jd), indent=2))
