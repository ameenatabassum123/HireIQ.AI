"""Resume customizer.

Takes a parsed user profile + JD analysis, asks Gemini to rewrite the
summary, experience bullets, and project descriptions to naturally
foreground the JD's required skills and keywords (WITHOUT fabricating
anything), then renders a clean ATS-friendly PDF via ReportLab.

Public entry point:
    customize_resume(user_profile, jd_analysis, *, company="", role="",
                     job=None, output_dir=None)
        -> tuple[dict, Path, list[dict]]  (profile, pdf_path, diff_list)
"""

from __future__ import annotations

import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from .gemini_client import ask_gemini, format_gemini_error, web_ui_max_retries


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "resumes"

PROMPT_TEMPLATE = """You are a resume editor. Your job is to rewrite ONLY the
descriptive prose of a candidate's resume so that the JD's required skills
and keywords are naturally surfaced.

STRICT RULES:
1. Do NOT invent jobs, projects, skills, or technologies the candidate
   does not already have.
2. Do NOT change role titles, company names, durations, project names, or
   the candidate's stated skill list.
3. Only foreground skills/keywords that are already plausibly demonstrated
   by the candidate's existing experience or projects. If a JD skill is
   absent, simply omit it - never fake it.
4. Keep the same number of experience entries and the same number of
   projects, in the same order.
5. Each experience entry must have the same approximate number of bullets
   as the original (or up to +/- 1).
6. Use active voice, quantify outcomes where the original quantifies them,
   and keep bullets concise (one line where possible).
7. Return ONLY a single JSON object. No prose, no markdown fences.

Output schema (positional, parallel to the input arrays):
{{
  "summary": "rewritten 2-3 sentence summary",
  "experience_descriptions": [
      ["bullet 1 for experience[0]", "bullet 2 for experience[0]", ...],
      ["bullet 1 for experience[1]", ...]
  ],
  "project_descriptions": [
      "rewritten description for projects[0]",
      "rewritten description for projects[1]"
  ]
}}

JD focus (foreground these where the candidate genuinely demonstrates them):
- required_skills: {required_skills}
- preferred_skills: {preferred_skills}
- tools: {tools}
- keywords: {keywords}

Candidate input:
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


# =============================================================================
# Profile customization (NO in-place mutation)
# =============================================================================
def _candidate_payload(user_profile: dict[str, Any]) -> dict[str, Any]:
    """Slim, deterministic dict we hand to Gemini (avoids leaking irrelevant fields)."""
    return {
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
                "description": e.get("description", []),
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


def _apply_rewrite(
    user_profile: dict[str, Any], rewrite: dict[str, Any]
) -> dict[str, Any]:
    """Merge Gemini's rewrite into a deep copy of user_profile (positional)."""
    out = copy.deepcopy(user_profile)

    if isinstance(rewrite.get("summary"), str) and rewrite["summary"].strip():
        out["summary"] = rewrite["summary"].strip()

    new_exp_descs = rewrite.get("experience_descriptions") or []
    for i, entry in enumerate(out.get("experience", []) or []):
        if i < len(new_exp_descs):
            bullets = new_exp_descs[i]
            if isinstance(bullets, list):
                entry["description"] = [
                    str(b).strip() for b in bullets if str(b).strip()
                ]
            elif isinstance(bullets, str) and bullets.strip():
                entry["description"] = [bullets.strip()]

    new_proj_descs = rewrite.get("project_descriptions") or []
    for i, proj in enumerate(out.get("projects", []) or []):
        if i < len(new_proj_descs):
            desc = new_proj_descs[i]
            if isinstance(desc, str) and desc.strip():
                proj["description"] = desc.strip()
            elif isinstance(desc, list):
                proj["description"] = " ".join(str(x).strip() for x in desc if str(x).strip())

    return out


def compute_resume_diff(
    original: dict[str, Any],
    customized: dict[str, Any],
) -> list[dict[str, str]]:
    """Build a structured diff list between original and customized profiles.

    Each item: ``{section, original_text, suggested_text}``.
    """
    diffs: list[dict[str, str]] = []

    orig_summary = (original.get("summary") or "").strip()
    new_summary = (customized.get("summary") or "").strip()
    if orig_summary != new_summary and new_summary:
        diffs.append({
            "section": "summary",
            "original_text": orig_summary,
            "suggested_text": new_summary,
        })

    for i, orig_exp in enumerate(original.get("experience") or []):
        new_exp = (customized.get("experience") or [])[i] if i < len(
            customized.get("experience") or []
        ) else {}
        role = orig_exp.get("role") or f"Experience {i + 1}"
        company = orig_exp.get("company") or ""
        label = f"{role} @ {company}".strip(" @")

        orig_bullets = orig_exp.get("description") or []
        if isinstance(orig_bullets, str):
            orig_bullets = [orig_bullets]
        new_bullets = (new_exp or {}).get("description") or []
        if isinstance(new_bullets, str):
            new_bullets = [new_bullets]

        max_len = max(len(orig_bullets), len(new_bullets))
        for j in range(max_len):
            orig_b = str(orig_bullets[j]).strip() if j < len(orig_bullets) else ""
            new_b = str(new_bullets[j]).strip() if j < len(new_bullets) else ""
            if orig_b != new_b and new_b:
                diffs.append({
                    "section": f"experience[{i}].bullet[{j}] ({label})",
                    "original_text": orig_b,
                    "suggested_text": new_b,
                })

    for i, orig_proj in enumerate(original.get("projects") or []):
        new_proj = (customized.get("projects") or [])[i] if i < len(
            customized.get("projects") or []
        ) else {}
        name = orig_proj.get("name") or f"Project {i + 1}"

        orig_desc = orig_proj.get("description") or ""
        if isinstance(orig_desc, list):
            orig_desc = " ".join(str(x) for x in orig_desc)
        new_desc = (new_proj or {}).get("description") or ""
        if isinstance(new_desc, list):
            new_desc = " ".join(str(x) for x in new_desc)

        orig_s = str(orig_desc).strip()
        new_s = str(new_desc).strip()
        if orig_s != new_s and new_s:
            diffs.append({
                "section": f"projects[{i}] ({name})",
                "original_text": orig_s,
                "suggested_text": new_s,
            })

    return diffs


def _jd_focus_terms(jd_analysis: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for key in ("required_skills", "preferred_skills", "tools", "keywords"):
        for item in jd_analysis.get(key) or []:
            text = str(item).strip()
            if text and text not in seen:
                seen.append(text)
    return seen


def _user_skill_terms(user_profile: dict[str, Any]) -> set[str]:
    pool: set[str] = set()
    for field in (
        "skills",
        "tools",
        "programming_languages",
        "frameworks",
        "databases",
    ):
        for item in user_profile.get(field) or []:
            text = str(item).strip().lower()
            if text:
                pool.add(text)
    return pool


def propose_resume_customization_local(
    user_profile: dict[str, Any],
    jd_analysis: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Keyword-based local edits when Gemini is unavailable."""
    original = copy.deepcopy(user_profile)
    customized = copy.deepcopy(user_profile)
    user_terms = _user_skill_terms(user_profile)
    matched: list[str] = []
    for term in _jd_focus_terms(jd_analysis):
        low = term.lower()
        if low in user_terms or any(low in ut or ut in low for ut in user_terms if len(ut) >= 3):
            matched.append(term)
    matched = matched[:6]

    summary = (customized.get("summary") or "").strip()
    if matched and summary:
        focus = ", ".join(matched[:4])
        if not any(m.lower() in summary.lower() for m in matched[:2]):
            new_summary = f"{summary.rstrip('.')}. Experienced with {focus}."
            customized["summary"] = new_summary

    for i, entry in enumerate(customized.get("experience") or []):
        bullets = entry.get("description") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        if not bullets or not matched:
            continue
        first = str(bullets[0]).strip()
        skill = matched[0]
        if skill.lower() not in first.lower():
            bullets = list(bullets)
            bullets[0] = f"{first.rstrip('.')} — leveraging {skill}."
            entry["description"] = bullets

    diffs = compute_resume_diff(original, customized)
    return customized, diffs


def apply_accepted_diffs(
    original: dict[str, Any],
    diffs: list[dict[str, str]],
    accepted_indices: list[int] | set[int],
) -> dict[str, Any]:
    """Merge only accepted diff items into a copy of ``original``."""
    out = copy.deepcopy(original)
    accepted = set(accepted_indices)

    for idx, diff in enumerate(diffs):
        if idx not in accepted:
            continue
        section = diff.get("section", "")
        suggested = (diff.get("suggested_text") or "").strip()
        if not suggested:
            continue

        if section == "summary":
            out["summary"] = suggested
            continue

        m = re.match(r"experience\[(\d+)\]\.bullet\[(\d+)\]", section)
        if m:
            ei, bi = int(m.group(1)), int(m.group(2))
            exp_list = out.setdefault("experience", [])
            while len(exp_list) <= ei:
                exp_list.append({"role": "", "company": "", "description": []})
            desc = exp_list[ei].setdefault("description", [])
            while len(desc) <= bi:
                desc.append("")
            desc[bi] = suggested
            continue

        m = re.match(r"projects\[(\d+)\]", section)
        if m:
            pi = int(m.group(1))
            proj_list = out.setdefault("projects", [])
            while len(proj_list) <= pi:
                proj_list.append({"name": "", "description": ""})
            proj_list[pi]["description"] = suggested

    return out


def customize_profile_for_jd(
    user_profile: dict[str, Any],
    jd_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Run the Gemini rewrite step. Returns a NEW profile dict.

    On any API/JSON failure this logs a warning and returns a deep copy of
    the original profile untouched - the pipeline should still produce a PDF.
    """
    customized, _ = propose_resume_customization(user_profile, jd_analysis)
    return customized


def propose_resume_customization(
    user_profile: dict[str, Any],
    jd_analysis: dict[str, Any],
    *,
    max_retries: int | None = None,
    allow_local_fallback: bool = True,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Propose JD-tailored resume edits without rendering a PDF.

    Returns ``(full_customized_profile, diff_list)``. On failure the diff
    list is empty and the profile is an unchanged deep copy unless
    ``allow_local_fallback`` supplies keyword-based edits.
    """
    original = copy.deepcopy(user_profile)
    payload = _candidate_payload(user_profile)
    prompt = PROMPT_TEMPLATE.format(
        required_skills=jd_analysis.get("required_skills", []),
        preferred_skills=jd_analysis.get("preferred_skills", []),
        tools=jd_analysis.get("tools", []),
        keywords=jd_analysis.get("keywords", []),
        candidate_json=json.dumps(payload, ensure_ascii=False, indent=2),
    )

    retries = web_ui_max_retries() if max_retries is None else max_retries

    try:
        rewrite = ask_gemini(
            prompt,
            expect_json=True,
            temperature=0.3,
            max_retries=retries,
        )
    except Exception as exc:
        print(
            f"[resume_customizer] Gemini rewrite failed ({exc}).",
            file=sys.stderr,
        )
        if allow_local_fallback:
            customized, diffs = propose_resume_customization_local(
                user_profile, jd_analysis
            )
            if diffs:
                return customized, diffs
        raise RuntimeError(format_gemini_error(exc)) from exc

    if not isinstance(rewrite, dict):
        print(
            f"[resume_customizer] Gemini returned non-object JSON "
            f"({type(rewrite).__name__}).",
            file=sys.stderr,
        )
        if allow_local_fallback:
            customized, diffs = propose_resume_customization_local(
                user_profile, jd_analysis
            )
            if diffs:
                return customized, diffs
        raise RuntimeError(
            "AI returned an invalid response. Try again in a moment."
        )

    customized = _apply_rewrite(user_profile, rewrite)
    diffs = compute_resume_diff(original, customized)
    return customized, diffs


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
            fontSize=20,
            leading=24,
            textColor=colors.HexColor("#111111"),
            spaceAfter=2,
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
        "section": ParagraphStyle(
            "Section",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11.5,
            leading=14,
            textColor=colors.HexColor("#1f2937"),
            spaceBefore=10,
            spaceAfter=2,
        ),
        "subheader": ParagraphStyle(
            "SubHeader",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=13,
            textColor=colors.HexColor("#111111"),
            spaceBefore=4,
            spaceAfter=1,
        ),
        "meta": ParagraphStyle(
            "Meta",
            parent=base["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=9.5,
            leading=12,
            textColor=colors.HexColor("#555555"),
            spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#222222"),
            alignment=TA_JUSTIFY,
            spaceAfter=2,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#222222"),
            alignment=TA_LEFT,
            leftIndent=10,
            spaceAfter=1,
        ),
    }


def _hr() -> HRFlowable:
    return HRFlowable(
        width="100%",
        thickness=0.6,
        color=colors.HexColor("#cccccc"),
        spaceBefore=2,
        spaceAfter=6,
    )


def _section_header(text: str, styles: dict[str, ParagraphStyle]) -> list:
    return [
        Paragraph(text.upper(), styles["section"]),
        HRFlowable(
            width="100%",
            thickness=1,
            color=colors.HexColor("#1f2937"),
            spaceBefore=0,
            spaceAfter=4,
        ),
    ]


def _bullets(items: list[str], styles: dict[str, ParagraphStyle]) -> ListFlowable | None:
    cleaned = [str(i).strip() for i in items or [] if str(i).strip()]
    if not cleaned:
        return None
    return ListFlowable(
        [ListItem(Paragraph(b, styles["bullet"]), leftIndent=8) for b in cleaned],
        bulletType="bullet",
        bulletChar="•",
        leftIndent=14,
        bulletFontSize=9,
        bulletOffsetY=0,
        spaceBefore=0,
        spaceAfter=2,
    )


def _skills_lines(profile: dict[str, Any]) -> list[tuple[str, list[str]]]:
    categories = [
        ("Programming Languages", profile.get("programming_languages")),
        ("Frameworks & Libraries", profile.get("frameworks")),
        ("Databases", profile.get("databases")),
        ("Tools & Platforms", profile.get("tools")),
        ("Other Skills", profile.get("skills")),
    ]
    out: list[tuple[str, list[str]]] = []
    for label, values in categories:
        items = [str(v).strip() for v in (values or []) if str(v).strip()]
        if items:
            out.append((label, items))
    return out


def render_resume_pdf(
    profile: dict[str, Any],
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = _build_styles()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.2 * cm,
        bottomMargin=1.2 * cm,
        title=f"Resume - {profile.get('name', '')}",
        author=profile.get("name", ""),
    )

    story: list = []

    # ---- Header --------------------------------------------------------
    name = profile.get("name") or "Your Name"
    story.append(Paragraph(name, styles["name"]))

    contact_bits: list[str] = []
    for key in ("email", "phone", "location"):
        v = profile.get(key)
        if v:
            contact_bits.append(str(v))
    for key in ("linkedin", "github", "portfolio"):
        v = profile.get(key)
        if v:
            contact_bits.append(str(v))
    if contact_bits:
        story.append(Paragraph("  •  ".join(contact_bits), styles["contact"]))
    story.append(_hr())

    # ---- Summary -------------------------------------------------------
    summary = (profile.get("summary") or "").strip()
    if summary:
        story.extend(_section_header("Summary", styles))
        story.append(Paragraph(summary, styles["body"]))

    # ---- Skills --------------------------------------------------------
    skill_lines = _skills_lines(profile)
    if skill_lines:
        story.extend(_section_header("Skills", styles))
        for label, items in skill_lines:
            line = f"<b>{label}:</b> {', '.join(items)}"
            story.append(Paragraph(line, styles["body"]))

    # ---- Experience ----------------------------------------------------
    experience = profile.get("experience") or []
    if experience:
        story.extend(_section_header("Experience", styles))
        for entry in experience:
            role = (entry.get("role") or "").strip()
            company = (entry.get("company") or "").strip()
            duration = (entry.get("duration") or "").strip()
            header = " · ".join([x for x in (role, company) if x]) or "(role)"
            block: list = [Paragraph(header, styles["subheader"])]
            if duration:
                block.append(Paragraph(duration, styles["meta"]))
            bullets = entry.get("description")
            if isinstance(bullets, str):
                bullets = [bullets]
            bullet_flow = _bullets(bullets or [], styles)
            if bullet_flow:
                block.append(bullet_flow)
            block.append(Spacer(1, 2 * mm))
            story.append(KeepTogether(block))

    # ---- Projects ------------------------------------------------------
    projects = profile.get("projects") or []
    if projects:
        story.extend(_section_header("Projects", styles))
        for proj in projects:
            name_ = (proj.get("name") or "").strip()
            techs = proj.get("technologies") or []
            techs = [str(t).strip() for t in techs if str(t).strip()]
            header_bits = [f"<b>{name_ or '(project)'}</b>"]
            if techs:
                header_bits.append(f"<i>({', '.join(techs)})</i>")
            block: list = [Paragraph(" ".join(header_bits), styles["body"])]
            desc = proj.get("description")
            if isinstance(desc, list):
                bullet_flow = _bullets(desc, styles)
                if bullet_flow:
                    block.append(bullet_flow)
            elif isinstance(desc, str) and desc.strip():
                block.append(Paragraph(desc.strip(), styles["body"]))
            block.append(Spacer(1, 2 * mm))
            story.append(KeepTogether(block))

    # ---- Education -----------------------------------------------------
    education = profile.get("education") or []
    if education:
        story.extend(_section_header("Education", styles))
        for edu in education:
            if isinstance(edu, str):
                story.append(Paragraph(edu, styles["body"]))
                continue
            degree = (edu.get("degree") or "").strip()
            inst = (edu.get("institution") or "").strip()
            year = (edu.get("year") or "").strip()
            score = (edu.get("score") or "").strip()
            head_bits = [b for b in (degree, inst) if b]
            head = ", ".join(head_bits) if head_bits else "(education)"
            if year:
                head += f" ({year})"
            story.append(Paragraph(f"<b>{head}</b>", styles["body"]))
            if score:
                story.append(Paragraph(f"Score: {score}", styles["meta"]))

    # ---- Certifications -----------------------------------------------
    certs = profile.get("certifications") or []
    certs = [str(c).strip() for c in certs if str(c).strip()]
    if certs:
        story.extend(_section_header("Certifications", styles))
        flow = _bullets(certs, styles)
        if flow:
            story.append(flow)

    doc.build(story)
    return output_path


# =============================================================================
# Public entry point
# =============================================================================
def customize_resume(
    user_profile: dict[str, Any],
    jd_analysis: dict[str, Any],
    *,
    company: str = "",
    role: str = "",
    job: Any = None,
    output_dir: str | Path | None = None,
    accepted_diff_indices: list[int] | None = None,
    diffs: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], Path, list[dict[str, str]]]:
    """Tailor the profile to the JD and render a PDF.

    When ``accepted_diff_indices`` and ``diffs`` are supplied, only accepted
    changes are merged before PDF render. Otherwise all proposed changes apply.

    Returns ``(customized_profile, pdf_path, diff_list)``. The original
    ``user_profile`` is never mutated.
    """
    if job is not None:
        company = company or getattr(job, "company", "") or ""
        role = role or getattr(job, "title", "") or ""

    out_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = (
        f"{_safe_filename(company)}_{_safe_filename(role)}_{_run_stamp()}.pdf"
    )
    pdf_path = out_dir / filename

    if diffs is not None and accepted_diff_indices is not None:
        customized = apply_accepted_diffs(
            user_profile, diffs, accepted_diff_indices
        )
        diff_list = diffs
    else:
        customized, diff_list = propose_resume_customization(
            user_profile, jd_analysis
        )

    render_resume_pdf(customized, pdf_path)
    return customized, pdf_path, diff_list


# =============================================================================
# Smoke test (PDF path only - no API call)
# =============================================================================
if __name__ == "__main__":
    demo_profile = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "+91-9000000000",
        "location": "Bengaluru, India",
        "linkedin": "https://linkedin.com/in/ada",
        "summary": (
            "ML engineer with 2 years building NLP and recommender systems "
            "in production. Comfortable across the stack from data pipelines "
            "to model serving."
        ),
        "skills": ["Machine Learning", "NLP", "Recommender Systems"],
        "tools": ["Docker", "Git", "AWS"],
        "programming_languages": ["Python", "SQL"],
        "frameworks": ["PyTorch", "FastAPI", "scikit-learn"],
        "databases": ["PostgreSQL", "Redis"],
        "education": [
            {"degree": "B.Tech CSE", "institution": "IIT Example",
             "year": "2024", "score": "8.7 CGPA"},
        ],
        "experience": [
            {
                "role": "ML Engineer",
                "company": "Acme AI",
                "duration": "2024 - Present",
                "description": [
                    "Built a transformer-based intent classifier serving 5M requests/day",
                    "Reduced inference latency by 38% via ONNX quantization",
                ],
            },
        ],
        "projects": [
            {
                "name": "Resume-Aware Job Agent",
                "technologies": ["Python", "Playwright", "Gemini API"],
                "description": (
                    "End-to-end agent that scrapes jobs, tailors resumes, "
                    "and tracks applications in SQLite."
                ),
            },
        ],
        "certifications": ["AWS ML Specialty"],
    }

    demo_jd_analysis = {
        "required_skills": ["Python", "PyTorch", "NLP"],
        "preferred_skills": ["AWS"],
        "tools": ["Docker"],
        "keywords": ["MLOps", "production"],
        "experience_required": "1-3 years",
    }

    # Skip the Gemini call for the smoke test - just render with the original profile.
    out_dir = DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"smoketest_{_today_stamp()}.pdf"
    render_resume_pdf(demo_profile, out_path)
    print(f"Wrote demo PDF: {out_path}")
    print(
        "(Full pipeline with Gemini rewrite: call "
        "customize_resume(profile, jd_analysis, company='Acme', role='ML Engineer'))"
    )
