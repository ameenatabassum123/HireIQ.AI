"""Multi-profile storage + resolution for parsed resumes.

The agent supports several parsed resume profiles side-by-side, one JSON file
per slug under ``config/profiles/<slug>.json``. The legacy single-file profile
at ``config/user_profile.json`` keeps working untouched: when no slug is
requested and no profiles directory exists, callers fall back to it.

Active-profile resolution priority (highest wins):
    1. an explicit slug passed by the caller (e.g. ``--profile data_science``)
    2. ``config.yaml`` -> ``user.active_profile`` (alias: ``user.default_profile``)
    3. the legacy ``config/user_profile.json`` file
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
PROFILES_DIR = CONFIG_DIR / "profiles"
LEGACY_PROFILE_PATH = CONFIG_DIR / "user_profile.json"
MANIFEST_PATH = CONFIG_DIR / "resume_manifest.yaml"
RESUME_DIR = PROJECT_ROOT / "resume"
DEFAULT_RESUME_PDF = RESUME_DIR / "master_resume.pdf"

CONTACT_FALLBACK_KEYS = (
    "name", "email", "phone", "location", "linkedin", "github", "portfolio",
)


# ---------------------------------------------------------------------------
# Slugs + filesystem layout
# ---------------------------------------------------------------------------
def slugify(name: str) -> str:
    """Lower-case, underscore-separated slug suitable for a filename."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip().lower())
    return s.strip("_")


def profile_path(slug: str) -> Path:
    """Filesystem path for ``config/profiles/<slug>.json``."""
    return PROFILES_DIR / f"{slug}.json"


def list_profiles(user_id: int | None = None, db: Any | None = None) -> list[str]:
    """Slugs for known profiles (database-backed when logged in, else filesystem).

    When ``user_id`` is set, returns that user's DB resumes plus legacy filesystem
    slugs (``u{id}_*`` or unprefixed) not yet imported into the database.
    """
    slugs: set[str] = set()
    if user_id is not None and db is not None:
        for row in db.list_resumes(user_id):
            slugs.add(str(row["slug"]))
        prefix = user_slug_prefix(user_id)
        if PROFILES_DIR.exists():
            for p in PROFILES_DIR.glob("*.json"):
                stem = p.stem
                if stem.startswith(prefix) or not stem.startswith("u"):
                    slugs.add(stem)
        if RESUME_DIR.exists():
            for p in RESUME_DIR.glob("*.pdf"):
                stem = p.stem
                if stem.startswith(prefix) or not stem.startswith("u"):
                    slugs.add(stem)
        return sorted(slugs)

    if PROFILES_DIR.exists():
        slugs.update(p.stem for p in PROFILES_DIR.glob("*.json") if p.is_file())
    if RESUME_DIR.exists():
        slugs.update(p.stem for p in RESUME_DIR.glob("*.pdf") if p.is_file())
    return sorted(slugs)


def user_slug_prefix(user_id: int) -> str:
    return f"u{int(user_id)}_"


def scoped_slug(user_id: int | None, name: str) -> str:
    """Build a filesystem-safe slug, namespaced per user when logged in."""
    base = slugify(name)
    if not base:
        return ""
    if user_id is None:
        return base
    prefix = user_slug_prefix(user_id)
    if base.startswith(prefix):
        return base
    return f"{prefix}{base}"


def profile_skill_summary(profile: dict[str, Any], limit: int = 12) -> list[str]:
    """Short list of skills/tools for UI preview."""
    items: list[str] = []
    for field in (
        "skills", "programming_languages", "frameworks", "tools", "databases",
    ):
        for val in profile.get(field) or []:
            s = str(val).strip()
            if s and s not in items:
                items.append(s)
            if len(items) >= limit:
                return items
    return items


# ---------------------------------------------------------------------------
# Active-profile resolution
# ---------------------------------------------------------------------------
def resolve_active_slug(
    config: dict[str, Any] | None = None,
    override: str | None = None,
    user_id: int | None = None,
    db: Any | None = None,
) -> str | None:
    """Pick the active profile slug. Returns None when no slug applies (legacy mode)."""
    if override:
        return slugify(override)
    if user_id and db is not None:
        settings = db.get_user_settings(user_id)
        if settings and settings.get("active_profile"):
            return slugify(str(settings["active_profile"]))
    cfg_user = (config or {}).get("user") or {}
    raw = cfg_user.get("active_profile") or cfg_user.get("default_profile")
    if raw:
        return slugify(str(raw))
    return None


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_profile(
    slug: str | None = None,
    config: dict[str, Any] | None = None,
    user_id: int | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """Load a parsed profile dict, merged with ``config.user.*`` contact fallbacks.

    When ``user_id`` and ``db`` are set, reads from the database first. Otherwise
    reads ``config/profiles/<slug>.json`` or legacy ``config/user_profile.json``.
    """
    profile: dict[str, Any] = {}
    if slug and user_id is not None and db is not None:
        row = db.get_resume(user_id, slug)
        if row and isinstance(row.get("profile"), dict):
            profile = dict(row["profile"])

    if not profile:
        if slug:
            profile = _read_json(profile_path(slug))
        else:
            profile = _read_json(LEGACY_PROFILE_PATH)

    cfg_user = (config or {}).get("user") or {}
    for key in CONTACT_FALLBACK_KEYS:
        if not profile.get(key) and cfg_user.get(key):
            profile[key] = cfg_user[key]
    return profile


def profile_exists_in_db(user_id: int, slug: str, db: Any) -> bool:
    return db.get_resume(user_id, slug) is not None


def has_resume_pdf(
    slug: str,
    user_id: int | None = None,
    db: Any | None = None,
) -> bool:
    """True when a PDF exists in the database or on disk for ``slug``."""
    if user_id is not None and db is not None:
        row = db.get_resume(user_id, slug)
        if row and row.get("has_pdf"):
            return True
    pdf = resolve_resume_pdf(slug)
    return pdf.exists()


def import_disk_resume_to_db(
    user_id: int,
    slug: str,
    db: Any,
) -> bool:
    """Best-effort import of a filesystem resume into the database for ``user_id``."""
    if db.get_resume(user_id, slug):
        return False

    prefix = user_slug_prefix(user_id)
    if slug.startswith("u") and not slug.startswith(prefix):
        return False

    pdf_path = RESUME_DIR / f"{slug}.pdf"
    if not pdf_path.exists():
        return False

    profile = _read_json(profile_path(slug))
    parse_status = "pdf_only"
    if profile:
        parse_status = "parsed"

    db.save_resume(
        user_id,
        slug,
        pdf_bytes=pdf_path.read_bytes(),
        filename=f"{slug}.pdf",
        profile_json=profile or None,
        parse_status=parse_status,
    )
    return True


def import_user_legacy_resumes(user_id: int, db: Any) -> int:
    """Import on-disk resumes for ``user_id`` that are not yet in the database."""
    imported = 0
    prefix = user_slug_prefix(user_id)
    slugs: set[str] = set()
    if PROFILES_DIR.exists():
        for p in PROFILES_DIR.glob("*.json"):
            stem = p.stem
            if stem.startswith(prefix) or not stem.startswith("u"):
                slugs.add(stem)
    if RESUME_DIR.exists():
        for p in RESUME_DIR.glob("*.pdf"):
            stem = p.stem
            if stem.startswith(prefix) or not stem.startswith("u"):
                slugs.add(stem)
    for slug in sorted(slugs):
        if import_disk_resume_to_db(user_id, slug, db):
            imported += 1
    return imported


def delete_profile(
    slug: str,
    user_id: int | None = None,
    db: Any | None = None,
) -> bool:
    """Remove profile from database and/or filesystem."""
    clean = slugify(slug)
    if not clean:
        return False
    removed = False
    if user_id is not None and db is not None:
        if db.delete_resume(user_id, clean):
            removed = True
    json_path = profile_path(clean)
    if json_path.exists():
        json_path.unlink()
        removed = True
    pdf_path = RESUME_DIR / f"{clean}.pdf"
    if pdf_path.exists():
        pdf_path.unlink()
        removed = True
    return removed


def save_profile(
    slug: str | None,
    profile: dict[str, Any],
    *,
    user_id: int | None = None,
    db: Any | None = None,
    parse_status: str | None = None,
) -> Path | None:
    """Persist profile JSON to database (when logged in) and/or filesystem."""
    if slug and user_id is not None and db is not None:
        status = parse_status or "parsed"
        db.save_resume(
            user_id,
            slug,
            profile_json=profile,
            parse_status=status,
        )
        return None

    out_path = profile_path(slug) if slug else LEGACY_PROFILE_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return out_path


# ---------------------------------------------------------------------------
# Resume PDF resolution (slug -> PDF path)
# ---------------------------------------------------------------------------
def load_manifest() -> dict[str, str]:
    """Return the ``slug -> pdf path`` map from ``config/resume_manifest.yaml``.

    Accepts either a flat ``{slug: path}`` mapping or a nested
    ``{profiles: {slug: path}}`` block. Missing file -> empty dict.
    """
    if not MANIFEST_PATH.exists():
        return {}
    try:
        with MANIFEST_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return {}
    if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
        data = data["profiles"]
    if not isinstance(data, dict):
        return {}
    return {slugify(str(k)): str(v) for k, v in data.items() if v}


def resolve_resume_pdf(
    slug: str | None, config: dict[str, Any] | None = None
) -> Path:
    """Best-effort PDF path for a slug.

    PDFs are uploaded via the web app (Profiles page) into
    ``resume/<slug>.pdf``. ``config.yaml`` ``user.resume_path`` is not used.

    Lookup order when ``slug`` is set:
        1. ``config/resume_manifest.yaml`` entry for ``slug``
        2. ``resume/<slug>.pdf``

    When ``slug`` is None (legacy single-profile mode):
        - ``resume/master_resume.pdf`` if present, else the same default path
    """
    _ = config  # kept for call-site compatibility; not used for PDF resolution
    if slug:
        manifest = load_manifest()
        if slug in manifest:
            raw = manifest[slug]
            p = Path(raw)
            if not p.is_absolute():
                p = (PROJECT_ROOT / p).resolve()
            if p.exists():
                return p
        guess = RESUME_DIR / f"{slug}.pdf"
        if guess.exists():
            return guess
    else:
        if DEFAULT_RESUME_PDF.exists():
            return DEFAULT_RESUME_PDF
    return DEFAULT_RESUME_PDF


def materialize_resume_pdf(
    slug: str,
    user_id: int | None = None,
    db: Any | None = None,
) -> Path | None:
    """Return a readable PDF path for parsing/preview (DB blob or disk file).

    Writes a temporary file when the PDF only exists in the database.
    """
    if user_id is not None and db is not None:
        pdf_bytes = db.get_resume_pdf_bytes(user_id, slug)
        if pdf_bytes:
            tmp = Path(tempfile.gettempdir()) / f"job_agent_resume_{user_id}_{slug}.pdf"
            tmp.write_bytes(pdf_bytes)
            return tmp

    pdf = resolve_resume_pdf(slug)
    return pdf if pdf.exists() else None
