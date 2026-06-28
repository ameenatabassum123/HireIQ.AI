"""ATS compatibility scorer — detailed resume vs JD analysis.

Wraps ``ai.matcher.compute_match`` and adds per-section keyword breakdown
for the web ATS calculator and job detail pages.
"""

from __future__ import annotations

from typing import Any

from .matcher import (
    SECTION_WEIGHTS,
    _extract_user_terms,
    _match_terms,
    _normalize_set,
    compute_match,
)

JD_SECTIONS = ("required_skills", "preferred_skills", "tools", "keywords")


def _section_breakdown(
    user_terms: set[str],
    jd_analysis: dict[str, Any],
) -> dict[str, dict[str, list[str]]]:
    """Per-JD-section matched vs missing terms (display-form)."""
    out: dict[str, dict[str, list[str]]] = {}
    for section in JD_SECTIONS:
        raw_terms = jd_analysis.get(section) or []
        if not isinstance(raw_terms, list):
            raw_terms = [raw_terms]
        display_terms = [str(t).strip() for t in raw_terms if str(t).strip()]
        if not display_terms:
            continue
        normalized = _normalize_set(display_terms)
        matched_norm = _match_terms(user_terms, normalized)
        # Map normalized matches back to original display strings where possible
        matched_display: list[str] = []
        missing_display: list[str] = []
        for term in display_terms:
            norm = _normalize_set([term])
            term_norm = next(iter(norm), "")
            if term_norm in matched_norm:
                matched_display.append(term)
            else:
                missing_display.append(term)
        out[section] = {
            "all": display_terms,
            "matched": matched_display,
            "missing": missing_display,
        }
    return out


def compute_ats_score(
    user_profile: dict[str, Any],
    jd_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Return ATS score (0-100) plus keyword breakdown for tailoring."""
    base = compute_match(user_profile, jd_analysis)
    user_terms = _extract_user_terms(user_profile)
    sections = _section_breakdown(user_terms, jd_analysis)

    all_keywords: list[str] = []
    matched_keywords: list[str] = []
    missing_keywords: list[str] = []
    seen: set[str] = set()

    for section in JD_SECTIONS:
        block = sections.get(section) or {}
        for term in block.get("all") or []:
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            all_keywords.append(term)
            if term in (block.get("matched") or []):
                matched_keywords.append(term)
            else:
                missing_keywords.append(term)

    hard_missing = []
    for section in ("required_skills", "tools"):
        block = sections.get(section) or {}
        hard_missing.extend(block.get("missing") or [])

    weighted: list[dict[str, Any]] = []
    for section, weight in SECTION_WEIGHTS.items():
        block = sections.get(section)
        if not block or not block.get("all"):
            continue
        total = len(block["all"])
        hit = len(block["matched"])
        weighted.append({
            "section": section,
            "weight": weight,
            "matched": hit,
            "total": total,
            "percent": round((hit / total) * 100) if total else 0,
        })

    return {
        "ats_score": base["match_score"],
        "match_score": base["match_score"],
        "recommendation": base["recommendation"],
        "matched_skills": base["matched_skills"],
        "missing_skills": base["missing_skills"],
        "sections": sections,
        "all_keywords": all_keywords,
        "matched_keywords": matched_keywords,
        "missing_keywords": missing_keywords,
        "tailoring_suggestions": hard_missing,
        "weighted_sections": weighted,
    }


def format_ats_report(result: dict[str, Any]) -> str:
    """Plain-text ATS report for the dedicated score output panel."""
    score = int(result.get("ats_score") or 0)
    lines = [
        f"ATS Score: {score}/100",
        f"Recommendation: {result.get('recommendation') or '—'}",
        "",
    ]
    weighted = result.get("weighted_sections") or []
    if weighted:
        lines.append("Score breakdown:")
        for sec in weighted:
            name = str(sec.get("section") or "").replace("_", " ").title()
            lines.append(
                f"  • {name}: {sec.get('matched', 0)}/{sec.get('total', 0)} "
                f"({sec.get('percent', 0)}%)"
            )
        lines.append("")

    matched = result.get("matched_keywords") or []
    missing = result.get("missing_keywords") or []
    if matched:
        lines.append(f"Matched keywords ({len(matched)}):")
        lines.append("  " + ", ".join(matched))
        lines.append("")
    if missing:
        lines.append(f"Missing keywords ({len(missing)}):")
        lines.append("  " + ", ".join(missing))
        lines.append("")

    suggestions = result.get("tailoring_suggestions") or []
    if suggestions:
        lines.append("Tailoring suggestions (required skills / tools to highlight):")
        lines.append("  " + ", ".join(suggestions))

    return "\n".join(lines).strip()
