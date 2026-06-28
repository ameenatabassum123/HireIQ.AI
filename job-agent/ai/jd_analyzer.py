"""Job-description analyzer.

Given a raw JD string, ask Gemini to extract structured requirements that
the matcher and resume-tailoring modules can consume.

Usage:
    from ai.jd_analyzer import analyze_jd
    analysis = analyze_jd(jd_text)
"""

from __future__ import annotations

import json
import re
from typing import Any

from .gemini_client import ask_gemini


_SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|director|head of|"
    r"architect|manager|vp\b|vice president|5\+?\s*years?|"
    r"7\+?\s*years?|8\+?\s*years?|10\+?\s*years?)\b",
    re.IGNORECASE,
)
_JUNIOR_RE = re.compile(
    r"\b(junior|jr\.?|entry[\s-]?level|graduate|fresher|intern|"
    r"0[\s-]?2\s*years?|0[\s-]?1\s*years?|no experience)\b",
    re.IGNORECASE,
)
_MID_RE = re.compile(
    r"\b(mid[\s-]?level|intermediate|2[\s-]?4\s*years?|"
    r"3[\s-]?5\s*years?|2\+?\s*years?)\b",
    re.IGNORECASE,
)


PROMPT_TEMPLATE = """You are a job-description analyzer. Read the JD below and
return ONLY a single JSON object (no prose, no markdown fences) with EXACTLY
these top-level keys:

- required_skills: list of strings (must-have technical/domain skills)
- preferred_skills: list of strings (nice-to-have skills)
- tools: list of strings (software / platforms / IDEs / cloud services mentioned)
- experience_required: string (e.g. "2-4 years", "Fresher", "5+ years" - empty
  string if not mentioned)
- responsibilities: list of strings (key duties / responsibilities)
- keywords: list of strings (other notable keywords: domains, methodologies,
  certifications, languages, frameworks, etc. that aren't already in
  required_skills/tools)

Rules:
- If a field cannot be inferred, use [] for lists and "" for strings.
- Each list item should be a short phrase, not a sentence.
- Do not invent information.
- Return ONLY the JSON. No commentary, no code fences.

Job description:
\"\"\"
{jd_text}
\"\"\"
"""


SCHEMA: dict[str, Any] = {
    "required_skills": [],
    "preferred_skills": [],
    "tools": [],
    "experience_required": "",
    "responsibilities": [],
    "keywords": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(value)]


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, default in SCHEMA.items():
        value = raw.get(key, default)
        if isinstance(default, list):
            out[key] = _listify(value)
        else:
            out[key] = value if isinstance(value, str) else ("" if value is None else str(value))
    return out


def infer_seniority_level(
    jd_text: str,
    *,
    job_title: str = "",
    experience_required: str = "",
) -> str:
    """Tag a JD as junior, mid, or senior from title/JD keywords.

    Returns one of ``junior``, ``mid``, ``senior``, or ``unknown``.
    """
    blob = " ".join(
        x for x in (job_title, experience_required, jd_text) if x
    ).lower()
    if _SENIOR_RE.search(blob):
        return "senior"
    if _JUNIOR_RE.search(blob):
        return "junior"
    if _MID_RE.search(blob):
        return "mid"
    return "unknown"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def analyze_jd(jd_text: str) -> dict[str, Any]:
    """Analyze a job description and return structured requirements.

    Returns a dict with keys matching SCHEMA. Raises on hard errors
    (missing key, API error, unparseable response).
    """
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise ValueError("jd_text must be a non-empty string.")

    prompt = PROMPT_TEMPLATE.format(jd_text=jd_text.strip())

    try:
        parsed = ask_gemini(prompt, expect_json=True, temperature=0)
    except Exception as exc:
        raise RuntimeError(f"Gemini JD analysis failed: {exc}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"Gemini returned a non-object JSON for JD analysis: {type(parsed).__name__}"
        )
    result = _normalize(parsed)
    result["seniority_level"] = infer_seniority_level(
        jd_text,
        experience_required=result.get("experience_required") or "",
    )
    return result


if __name__ == "__main__":
    sample = (
        "We are hiring a Machine Learning Engineer with 2+ years of experience. "
        "Required: Python, PyTorch, TensorFlow, SQL. Nice to have: AWS, Docker. "
        "You will build and deploy ML models, collaborate with data engineers, "
        "and monitor model performance in production."
    )
    try:
        result = analyze_jd(sample)
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"Error: {exc}")
