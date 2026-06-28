"""Resume parser: PDF -> structured profile JSON via Gemini.

Usage as a library:
    from ai.resume_parser import parse_resume
    profile = parse_resume("resume/master_resume.pdf")

Usage as a script:
    python -m ai.resume_parser
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pdfplumber

from .gemini_client import ask_gemini, resolve_parse_model, web_parse_max_retries


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESUME_PATH = PROJECT_ROOT / "resume" / "master_resume.pdf"
USER_PROFILE_PATH = PROJECT_ROOT / "config" / "user_profile.json"


PROMPT_TEMPLATE = """You are a resume parser. Extract structured information from the
resume below and return ONLY a single JSON object (no prose, no markdown fences)
with EXACTLY these top-level keys:

- name: string
- email: string
- phone: string
- location: string
- summary: string (1-3 sentence professional summary)
- skills: list of strings (general / soft / domain skills)
- tools: list of strings (software, platforms, IDEs)
- programming_languages: list of strings
- frameworks: list of strings (web / ML / app frameworks and libraries)
- databases: list of strings
- education: list of objects with keys: degree, institution, year, score
- experience: list of objects with keys: role, company, duration, description
  (description is a list of bullet strings)
- projects: list of objects with keys: name, description, technologies
  (technologies is a list of strings)
- certifications: list of strings

Rules:
- If a field is not present in the resume, use "" for strings, [] for lists.
- Do not invent information.
- Return ONLY the JSON. No commentary, no code fences.

Resume text:
\"\"\"
{resume_text}
\"\"\"
"""


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------
def extract_text_from_pdf(pdf_path: str | Path) -> str:
    """Extract concatenated text from every page of a PDF resume."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"Resume PDF not found: {path}")

    pages: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text.strip())

    if not pages:
        raise ValueError(f"No extractable text found in {path}")
    return "\n\n".join(pages)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
SCHEMA: dict[str, Any] = {
    "name": "",
    "email": "",
    "phone": "",
    "location": "",
    "summary": "",
    "skills": [],
    "tools": [],
    "programming_languages": [],
    "frameworks": [],
    "databases": [],
    "education": [],
    "experience": [],
    "projects": [],
    "certifications": [],
}


def _listify(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def normalize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure every schema key exists with the right type."""
    out: dict[str, Any] = {}
    for key, default in SCHEMA.items():
        value = raw.get(key, default)
        if isinstance(default, list):
            out[key] = _listify(value)
        elif isinstance(default, str):
            out[key] = value if isinstance(value, str) else ("" if value is None else str(value))
        else:
            out[key] = value
    for extra in ("raw_text", "_parse_meta"):
        if extra in raw:
            out[extra] = raw[extra]
    return out


# ---------------------------------------------------------------------------
# Local fallback (no Gemini)
# ---------------------------------------------------------------------------
_COMMON_KEYWORDS = (
    "python", "java", "javascript", "typescript", "sql", "r", "scala", "go",
    "react", "node", "django", "flask", "fastapi", "pandas", "numpy",
    "scikit-learn", "tensorflow", "pytorch", "spark", "hadoop", "aws", "azure",
    "gcp", "docker", "kubernetes", "git", "linux", "tableau", "power bi",
    "excel", "machine learning", "deep learning", "data analysis",
    "data analytics", "statistics", "nlp", "computer vision", "etl",
    "postgresql", "mysql", "mongodb", "redis", "html", "css", "rest", "api",
)


def is_local_fallback_profile(profile: dict[str, Any]) -> bool:
    meta = profile.get("_parse_meta") or {}
    return meta.get("source") == "local_fallback"


def _slug_display_name(slug: str | None) -> str:
    if not slug:
        return ""
    name = re.sub(r"^u\d+_", "", slug)
    return name.replace("_", " ").strip().title()


def _extract_email(text: str) -> str:
    match = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    return match.group(0) if match else ""


def _extract_phone(text: str) -> str:
    match = re.search(
        r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}",
        text,
    )
    return match.group(0).strip() if match else ""


def _guess_name(text: str, slug: str | None) -> str:
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) > 80:
            continue
        if "@" in line or re.search(r"\d{3}.*\d{3}.*\d{4}", line):
            continue
        if len(line.split()) <= 6:
            return line
    return _slug_display_name(slug)


def _keyword_skills(text: str, limit: int = 24) -> list[str]:
    low = text.lower()
    found: list[str] = []
    for kw in _COMMON_KEYWORDS:
        if kw in low and kw not in found:
            found.append(kw.title() if " " not in kw else kw.title())
        if len(found) >= limit:
            break
    return found


def local_fallback_profile(
    pdf_path: str | Path,
    *,
    slug: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Build a minimal profile from PDF text when Gemini is unavailable."""
    resume_text = extract_text_from_pdf(pdf_path)
    profile = normalize_profile({
        "name": _guess_name(resume_text, slug),
        "email": _extract_email(resume_text),
        "phone": _extract_phone(resume_text),
        "summary": resume_text[:600].strip(),
        "skills": _keyword_skills(resume_text),
        "raw_text": resume_text[:12000],
        "_parse_meta": {
            "source": "local_fallback",
            "reason": reason[:500],
            "parsed_at": datetime.now(timezone.utc).isoformat(),
        },
    })
    return profile


def _save_profile(profile: dict[str, Any], output_path: str | Path) -> Path:
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return out_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_resume(
    pdf_path: str | Path = DEFAULT_RESUME_PATH,
    output_path: str | Path = USER_PROFILE_PATH,
    *,
    model: str | None = None,
    max_retries: int | None = None,
    allow_local_fallback: bool = False,
    slug: str | None = None,
) -> dict[str, Any]:
    """Parse a resume PDF into structured JSON and save to user_profile.json.

    When ``allow_local_fallback`` is True and Gemini fails, saves a minimal
    profile extracted locally from PDF text instead of raising.

    Returns the parsed profile dict.
    """
    resume_text = extract_text_from_pdf(pdf_path)
    try:
        prompt = PROMPT_TEMPLATE.format(resume_text=resume_text)
        raw = ask_gemini(
            prompt,
            expect_json=True,
            temperature=0,
            model=model or resolve_parse_model(),
            max_retries=max_retries,
        )
        if not isinstance(raw, dict):
            raise RuntimeError(
                "Gemini returned a non-object JSON for resume parsing: "
                f"{type(raw).__name__}"
            )
        profile = normalize_profile(raw)
        profile["_parse_meta"] = {
            "source": "gemini",
            "parsed_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        if not allow_local_fallback:
            raise
        profile = local_fallback_profile(
            pdf_path,
            slug=slug or Path(pdf_path).stem,
            reason=str(exc),
        )

    out_path = _save_profile(profile, output_path)
    _print_summary(profile, out_path)
    return profile


def _print_summary(profile: dict[str, Any], out_path: Path) -> None:
    name = profile.get("name") or "(unknown)"
    n_skills = len(profile.get("skills", []))
    n_tools = len(profile.get("tools", []))
    n_langs = len(profile.get("programming_languages", []))
    n_projects = len(profile.get("projects", []))
    n_exp = len(profile.get("experience", []))
    n_edu = len(profile.get("education", []))
    n_certs = len(profile.get("certifications", []))

    print("=" * 60)
    print(f"Resume parsed for: {name}")
    print("-" * 60)
    print(f"  Skills:                {n_skills}")
    print(f"  Tools:                 {n_tools}")
    print(f"  Programming languages: {n_langs}")
    print(f"  Experience entries:    {n_exp}")
    print(f"  Projects:              {n_projects}")
    print(f"  Education entries:     {n_edu}")
    print(f"  Certifications:        {n_certs}")
    print("-" * 60)
    print(f"Saved profile to: {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        parse_resume(DEFAULT_RESUME_PATH)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
