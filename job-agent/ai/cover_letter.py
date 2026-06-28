"""Cover-letter generator.

Asks Gemini for a 3-paragraph cover letter that mentions the company,
role, 2-3 matching skills, one relevant project, and genuine enthusiasm
for the role, then renders the result as a clean business-letter PDF.

Public entry point:
    generate_cover_letter(user_profile, job, jd_analysis, output_dir=None)
        -> (letter_text: str, pdf_path: Path)
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from .gemini_client import ask_gemini


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "cover_letters"


PROMPT_TEMPLATE = """You are writing a personalized cover letter on behalf of a
job candidate. Output EXACTLY 3 paragraphs of body text - nothing else (no
salutation, no signature, no markdown, no headers, no bullet points).

Style rules:
- Professional but human; specific, not generic.
- Mention the company name and the role title at least once in the first paragraph.
- Naturally reference 2-3 skills from the JD's required_skills/tools that the
  candidate genuinely has (cross-check against the candidate's skills, tools,
  programming_languages, and frameworks - do NOT name skills the candidate
  lacks).
- Refer to ONE specific project from the candidate's projects list by name
  and explain in one sentence why it is relevant to this role.
- Show real enthusiasm in the closing paragraph and end with a clear call to
  action (interview / conversation).
- Avoid cliches: "I am writing to apply...", "I am a passionate...", "team player".
- 200-280 words total across the 3 paragraphs.
- Plain text only. Separate paragraphs with a single blank line.

COMPANY: {company}
ROLE: {role}
LOCATION: {location}

JOB DESCRIPTION (raw):
\"\"\"
{job_description}
\"\"\"

JD ANALYSIS (structured):
{jd_analysis_json}

CANDIDATE PROFILE (use only what is here, do not invent):
{candidate_json}
"""


# =============================================================================
# Helpers
# =============================================================================
def _safe_filename(value: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")
    return cleaned[:max_len] or "unknown"


def _today_stamp() -> str:
    """Date-only stamp; kept for the smoke-test PDF name."""
    return datetime.now().strftime("%Y%m%d")


def _run_stamp() -> str:
    """Timestamp down to the second so back-to-back runs don't overwrite."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _formatted_date() -> str:
    return datetime.now().strftime("%B %d, %Y")


def _candidate_payload(user_profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": user_profile.get("name", ""),
        "summary": user_profile.get("summary", ""),
        "skills": user_profile.get("skills", []),
        "tools": user_profile.get("tools", []),
        "programming_languages": user_profile.get("programming_languages", []),
        "frameworks": user_profile.get("frameworks", []),
        "databases": user_profile.get("databases", []),
        "experience": [
            {
                "role": e.get("role", ""),
                "company": e.get("company", ""),
                "duration": e.get("duration", ""),
            }
            for e in user_profile.get("experience", []) or []
        ],
        "projects": [
            {
                "name": p.get("name", ""),
                "technologies": p.get("technologies", []),
                "description": p.get("description", ""),
            }
            for p in user_profile.get("projects", []) or []
        ],
    }


def _job_attr(job: Any, key: str, default: str = "") -> str:
    if isinstance(job, dict):
        return str(job.get(key, default) or default)
    return str(getattr(job, key, default) or default)


# =============================================================================
# Letter generation
# =============================================================================
def _fallback_letter(
    user_profile: dict[str, Any],
    company: str,
    role: str,
    jd_analysis: dict[str, Any],
) -> str:
    """A safe (if generic) letter used when Gemini can't be reached."""
    name = user_profile.get("name", "the candidate")
    user_skills_pool = (
        list(user_profile.get("skills") or [])
        + list(user_profile.get("tools") or [])
        + list(user_profile.get("programming_languages") or [])
        + list(user_profile.get("frameworks") or [])
    )
    user_skill_set = {s.lower() for s in user_skills_pool if isinstance(s, str)}
    jd_skills = (jd_analysis.get("required_skills") or []) + (
        jd_analysis.get("tools") or []
    )
    overlap = [s for s in jd_skills if isinstance(s, str) and s.lower() in user_skill_set][:3]
    skill_phrase = ", ".join(overlap) if overlap else "the technologies your team uses"

    projects = user_profile.get("projects") or []
    proj_name = projects[0].get("name", "a recent project") if projects else "a recent project"

    p1 = (
        f"I am applying for the {role or 'open role'} at {company or 'your team'}. "
        f"The combination of technical depth and product impact described in your posting "
        f"is exactly the kind of work I want to be doing next, and I believe my background "
        f"is a strong fit."
    )
    p2 = (
        f"My experience with {skill_phrase} maps directly to the requirements you outlined. "
        f"In {proj_name}, I built and shipped something that exercised these skills in "
        f"production - happy to walk you through the details and the trade-offs I made."
    )
    p3 = (
        f"I would welcome a conversation about how I can contribute to {company or 'your team'}. "
        f"Thank you for considering my application; I look forward to hearing from you. "
        f"- {name}"
    )
    return "\n\n".join([p1, p2, p3])


def generate_letter_text(
    user_profile: dict[str, Any],
    job: Any,
    jd_analysis: dict[str, Any],
) -> str:
    """Return the 3-paragraph letter body (string, no salutation/signature)."""
    company = _job_attr(job, "company", "")
    role = _job_attr(job, "title", "")
    location = _job_attr(job, "location", "")
    job_description = _job_attr(job, "description", "")

    prompt = PROMPT_TEMPLATE.format(
        company=company or "the company",
        role=role or "the role",
        location=location or "",
        job_description=(job_description or "")[:4000],
        jd_analysis_json=json.dumps(jd_analysis, ensure_ascii=False, indent=2),
        candidate_json=json.dumps(
            _candidate_payload(user_profile), ensure_ascii=False, indent=2
        ),
    )

    try:
        raw = ask_gemini(prompt, expect_json=False, temperature=0.6, max_output_tokens=1200)
    except Exception as exc:
        print(
            f"[cover_letter] Gemini call failed ({exc}); using fallback template.",
            file=sys.stderr,
        )
        return _fallback_letter(user_profile, company, role, jd_analysis)

    # Strip any accidental markdown fences or stray headers Gemini might add.
    cleaned = raw.strip()
    cleaned = re.sub(r"^```[A-Za-z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r"^(Dear[^\n]*)\n+", "", cleaned, flags=re.IGNORECASE)
    # Only strip a sign-off when it's clearly the last paragraph: a sign-off
    # phrase followed by a comma or newline, then an optional short name line,
    # all at the end of the string. Previously this regex matched a paragraph
    # body starting with "Thank you ..." and nuked everything after it.
    cleaned = re.sub(
        r"\n{2,}\s*(Sincerely|Best regards|Kind regards|Warm regards|Regards)"
        r"\s*[,.]?\s*(\n[^\n]{0,80})?\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip() or _fallback_letter(user_profile, company, role, jd_analysis)


# =============================================================================
# PDF rendering
# =============================================================================
def _build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "name": ParagraphStyle(
            "Name",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#111111"),
            spaceAfter=1,
        ),
        "contact": ParagraphStyle(
            "Contact",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=12,
            textColor=colors.HexColor("#555555"),
            spaceAfter=4,
        ),
        "date": ParagraphStyle(
            "Date",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#222222"),
            alignment=TA_LEFT,
            spaceBefore=8,
            spaceAfter=8,
        ),
        "recipient": ParagraphStyle(
            "Recipient",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=13,
            textColor=colors.HexColor("#222222"),
            alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#222222"),
            alignment=TA_JUSTIFY,
            spaceAfter=10,
            firstLineIndent=0,
        ),
        "signature": ParagraphStyle(
            "Signature",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=13,
            textColor=colors.HexColor("#111111"),
            spaceBefore=18,
        ),
    }


def render_cover_letter_pdf(
    user_profile: dict[str, Any],
    company: str,
    role: str,
    letter_body: str,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = _build_styles()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=2.0 * cm,
        rightMargin=2.0 * cm,
        topMargin=1.8 * cm,
        bottomMargin=1.8 * cm,
        title=f"Cover Letter - {company} - {role}",
        author=user_profile.get("name", ""),
    )

    story: list = []

    # Sender block
    name = user_profile.get("name") or "Your Name"
    story.append(Paragraph(name, styles["name"]))
    contact_bits: list[str] = []
    for key in ("email", "phone", "location"):
        v = user_profile.get(key)
        if v:
            contact_bits.append(str(v))
    for key in ("linkedin", "github"):
        v = user_profile.get(key)
        if v:
            contact_bits.append(str(v))
    if contact_bits:
        story.append(Paragraph("  •  ".join(contact_bits), styles["contact"]))
    story.append(
        HRFlowable(
            width="100%",
            thickness=0.6,
            color=colors.HexColor("#cccccc"),
            spaceBefore=2,
            spaceAfter=6,
        )
    )

    # Date
    story.append(Paragraph(_formatted_date(), styles["date"]))

    # Recipient
    recipient_lines = ["Hiring Manager"]
    if company:
        recipient_lines.append(company)
    for line in recipient_lines:
        story.append(Paragraph(line, styles["recipient"]))
    story.append(Spacer(1, 8))

    # Salutation
    story.append(
        Paragraph(
            f"Dear {company or 'Hiring'} Team,"
            if company
            else "Dear Hiring Manager,",
            styles["body"],
        )
    )

    # Body paragraphs
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", letter_body) if p.strip()]
    if not paragraphs:
        paragraphs = [letter_body.strip()]
    for para in paragraphs:
        # Escape HTML-sensitive chars; ReportLab Paragraph supports HTML-like tags.
        safe = (
            para.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        story.append(Paragraph(safe, styles["body"]))

    # Closing
    story.append(Paragraph("Sincerely,", styles["body"]))
    story.append(Paragraph(name, styles["signature"]))

    doc.build(story)
    return output_path


# =============================================================================
# Public entry point
# =============================================================================
def generate_cover_letter(
    user_profile: dict[str, Any],
    job: Any,
    jd_analysis: dict[str, Any],
    *,
    output_dir: str | Path | None = None,
) -> tuple[str, Path]:
    """Generate a cover-letter PDF tailored to the given job.

    Returns (letter_text, pdf_path).
    """
    company = _job_attr(job, "company", "")
    role = _job_attr(job, "title", "")

    out_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = (
        out_dir
        / f"{_safe_filename(company)}_{_safe_filename(role)}_{_run_stamp()}.pdf"
    )

    letter_text = generate_letter_text(user_profile, job, jd_analysis)
    render_cover_letter_pdf(user_profile, company, role, letter_text, pdf_path)
    return letter_text, pdf_path


# =============================================================================
# Smoke test (no API call)
# =============================================================================
if __name__ == "__main__":
    from dataclasses import dataclass

    @dataclass
    class _Job:
        title: str
        company: str
        location: str
        url: str
        description: str
        job_type: str = "full-time"
        platform: str = "demo"
        date_scraped: str = ""

    demo_profile = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "+91-9000000000",
        "location": "Bengaluru, India",
        "linkedin": "https://linkedin.com/in/ada",
        "skills": ["Machine Learning", "NLP"],
        "tools": ["Docker", "Git", "AWS"],
        "programming_languages": ["Python", "SQL"],
        "frameworks": ["PyTorch", "FastAPI"],
        "projects": [
            {
                "name": "Resume-Aware Job Agent",
                "technologies": ["Python", "Playwright", "Gemini API"],
                "description": (
                    "Agent that scrapes jobs and tailors resumes."
                ),
            },
        ],
    }
    demo_job = _Job(
        title="ML Engineer",
        company="Acme AI",
        location="Bengaluru",
        url="https://example.com/jobs/1",
        description="We're hiring an ML engineer to build NLP systems in production.",
    )
    demo_jd = {
        "required_skills": ["Python", "PyTorch", "NLP"],
        "preferred_skills": ["AWS"],
        "tools": ["Docker"],
        "keywords": ["MLOps", "production"],
        "experience_required": "1-3 years",
    }

    # Render with the deterministic fallback letter so this smoke test does
    # not call the network.
    body = _fallback_letter(demo_profile, demo_job.company, demo_job.title, demo_jd)
    out_path = (
        DEFAULT_OUTPUT_DIR
        / f"smoketest_{_safe_filename(demo_job.company)}_"
        f"{_safe_filename(demo_job.title)}_{_today_stamp()}.pdf"
    )
    render_cover_letter_pdf(
        demo_profile, demo_job.company, demo_job.title, body, out_path
    )
    print(f"Wrote demo cover letter: {out_path}")
    print(
        "(Full pipeline: call generate_cover_letter(profile, job, jd_analysis))"
    )
