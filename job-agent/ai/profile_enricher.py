"""Conversational profile enricher (Jobsuit-style).

Scans a parsed profile for weak spots, asks the user clarifying questions,
and merges answers into the profile JSON without inventing facts.
"""

from __future__ import annotations

import re
import sys
from typing import Any

from .gemini_client import ask_gemini
from .profile_store import save_profile


_METRIC_RE = re.compile(
    r"\d|%|\$|₹|€|£|k\b|million|billion|x\b|fold|percent|users|customers|"
    r"requests|ms\b|sec\b|hours|days|weeks|months|years|reduced|increased|"
    r"improved|saved|grew|delivered|built|shipped|deployed",
    re.IGNORECASE,
)


def _bullet_lacks_metrics(text: str) -> bool:
    """True when a bullet has no obvious quantifiable outcome."""
    t = (text or "").strip()
    if len(t) < 20:
        return True
    return _METRIC_RE.search(t) is None


def scan_profile_weaknesses(profile: dict[str, Any]) -> list[dict[str, str]]:
    """Return a list of weakness records: {field, context, issue}."""
    issues: list[dict[str, str]] = []

    if not (profile.get("summary") or "").strip():
        issues.append({
            "field": "summary",
            "context": "Professional summary",
            "issue": "empty_summary",
        })
    elif len((profile.get("summary") or "").split()) < 15:
        issues.append({
            "field": "summary",
            "context": (profile.get("summary") or "")[:120],
            "issue": "short_summary",
        })

    for key in ("skills", "programming_languages", "frameworks", "tools"):
        if not profile.get(key):
            issues.append({
                "field": key,
                "context": key.replace("_", " ").title(),
                "issue": "empty_field",
            })

    for i, exp in enumerate(profile.get("experience") or []):
        role = exp.get("role") or f"Experience #{i + 1}"
        bullets = exp.get("description") or []
        if not bullets:
            issues.append({
                "field": f"experience[{i}].description",
                "context": f"{role} at {exp.get('company') or 'unknown'}",
                "issue": "no_bullets",
            })
            continue
        for j, bullet in enumerate(bullets):
            if _bullet_lacks_metrics(str(bullet)):
                issues.append({
                    "field": f"experience[{i}].description[{j}]",
                    "context": f"{role}: {str(bullet)[:100]}",
                    "issue": "no_metrics",
                })

    for i, proj in enumerate(profile.get("projects") or []):
        name = proj.get("name") or f"Project #{i + 1}"
        desc = proj.get("description") or ""
        if isinstance(desc, list):
            desc = " ".join(str(x) for x in desc)
        if not str(desc).strip():
            issues.append({
                "field": f"projects[{i}].description",
                "context": name,
                "issue": "empty_project",
            })
        elif _bullet_lacks_metrics(str(desc)):
            issues.append({
                "field": f"projects[{i}].description",
                "context": f"{name}: {str(desc)[:100]}",
                "issue": "vague_project",
            })

    if not profile.get("education"):
        issues.append({
            "field": "education",
            "context": "Education section",
            "issue": "empty_education",
        })

    return issues


def _template_question(issue: dict[str, str]) -> str:
    """Fallback question when Gemini is unavailable."""
    kind = issue.get("issue", "")
    ctx = issue.get("context", "")
    if kind == "no_metrics":
        return (
            f"Your bullet \"{ctx}\" has no numbers. "
            "What measurable outcome can you add (%, time saved, users, scale)?"
        )
    if kind == "empty_summary":
        return "Write a 2-3 sentence professional summary highlighting your top strengths."
    if kind == "short_summary":
        return f"Expand your summary. Current: \"{ctx}...\" What else should recruiters know?"
    if kind == "empty_field":
        return f"List items for {ctx} (comma-separated)."
    if kind == "no_bullets":
        return f"Add 2-3 accomplishment bullets for {ctx}."
    if kind == "empty_project":
        return f"Describe what you built in project \"{ctx}\" and the impact."
    if kind == "vague_project":
        return f"Add metrics or concrete outcomes for: {ctx}"
    if kind == "empty_education":
        return "Add your degree, institution, and graduation year."
    return f"Improve {issue.get('field', 'this field')}: {ctx}"


def generate_enrichment_questions(
    profile: dict[str, Any],
    issues: list[dict[str, str]],
    *,
    max_questions: int = 8,
) -> list[dict[str, str]]:
    """Build user-facing questions for the top weaknesses."""
    subset = issues[:max_questions]
    if not subset:
        return []

    try:
        prompt = (
            "Given these resume weak spots, return ONLY a JSON array of objects "
            'with keys "field", "question". One question per item. '
            "Questions must ask for facts only — never invent answers.\n\n"
            f"Weak spots:\n{subset}\n\n"
            f"Profile name: {profile.get('name') or 'Candidate'}"
        )
        parsed = ask_gemini(prompt, expect_json=True, temperature=0.3)
        if isinstance(parsed, list) and parsed:
            out: list[dict[str, str]] = []
            for i, item in enumerate(parsed[:max_questions]):
                if not isinstance(item, dict):
                    continue
                field = str(item.get("field") or subset[i]["field"])
                question = str(item.get("question") or "").strip()
                if not question:
                    question = _template_question(subset[i])
                out.append({"field": field, "question": question})
            if out:
                return out
    except Exception as exc:
        print(f"[profile_enricher] Gemini questions failed ({exc}); using templates.",
              file=sys.stderr)

    return [
        {"field": iss["field"], "question": _template_question(iss)}
        for iss in subset
    ]


def _set_by_field_path(profile: dict[str, Any], field: str, answer: str) -> None:
    """Merge ``answer`` into ``profile`` at a dotted/bracket field path."""
    answer = answer.strip()
    if not answer:
        return

    if field == "summary":
        profile["summary"] = answer
        return

    if field in ("skills", "programming_languages", "frameworks", "tools", "databases"):
        items = [x.strip() for x in re.split(r"[,;\n]", answer) if x.strip()]
        profile[field] = items
        return

    if field == "education":
        profile.setdefault("education", [])
        profile["education"].append({"degree": answer, "institution": "", "year": ""})
        return

    m = re.match(r"experience\[(\d+)\]\.description(?:\[(\d+)\])?", field)
    if m:
        idx = int(m.group(1))
        bullet_idx = m.group(2)
        exp_list = profile.setdefault("experience", [])
        while len(exp_list) <= idx:
            exp_list.append({"role": "", "company": "", "description": []})
        entry = exp_list[idx]
        desc = entry.setdefault("description", [])
        if bullet_idx is not None:
            bi = int(bullet_idx)
            while len(desc) <= bi:
                desc.append("")
            desc[bi] = answer
        else:
            desc.append(answer)
        return

    m = re.match(r"projects\[(\d+)\]\.description", field)
    if m:
        idx = int(m.group(1))
        proj_list = profile.setdefault("projects", [])
        while len(proj_list) <= idx:
            proj_list.append({"name": "", "description": ""})
        proj_list[idx]["description"] = answer
        return


def merge_answers_into_profile(
    profile: dict[str, Any],
    qa_pairs: list[tuple[str, str]],
) -> dict[str, Any]:
    """Apply user answers to a copy of the profile (in-place on copy)."""
    import copy

    out = copy.deepcopy(profile)
    for field, answer in qa_pairs:
        if answer.strip():
            _set_by_field_path(out, field, answer)
    return out


def run_enrichment_cli(
    profile: dict[str, Any],
    slug: str | None,
    *,
    max_questions: int = 8,
) -> dict[str, Any]:
    """Interactive CLI loop: scan, ask, merge, save."""
    issues = scan_profile_weaknesses(profile)
    if not issues:
        print("[enrich-profile] Profile looks complete — no weak spots found.")
        return profile

    print(f"[enrich-profile] Found {len(issues)} area(s) to improve.")
    questions = generate_enrichment_questions(
        profile, issues, max_questions=max_questions
    )

    qa_pairs: list[tuple[str, str]] = []
    for i, q in enumerate(questions, 1):
        print(f"\n--- Question {i}/{len(questions)} ---")
        print(q["question"])
        try:
            answer = input("Your answer (Enter to skip): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[enrich-profile] Stopped early.")
            break
        if answer:
            qa_pairs.append((q["field"], answer))

    if not qa_pairs:
        print("[enrich-profile] No answers provided; profile unchanged.")
        return profile

    enriched = merge_answers_into_profile(profile, qa_pairs)
    out_path = save_profile(slug, enriched)
    print(f"[enrich-profile] Saved enriched profile -> {out_path}")
    return enriched
