# AI Job Application Agent

A local, transparent agent that finds jobs you actually fit, tailors a fresh resume + cover letter for each one, and helps you apply — with you in the loop on every submission.

Everything runs on your machine. Your resume, profile, and applications never leave your laptop except to call the LLM you've chosen.

---

## What it does

- **Scrapes 7 job boards** (Internshala, Indeed, Naukri, Wellfound, Glassdoor, Google Jobs, LinkedIn public search) and stores normalized postings in a local SQLite DB. No paid API keys required.
- **Analyzes each job description** with Google Gemini (free tier) to extract required skills, preferred skills, tools, experience, responsibilities, and keywords.
- **Scores every job 0-100** against your parsed resume profile using transparent, case-insensitive skill overlap — no black-box embeddings.
- **Generates a tailored ATS-friendly resume + cover letter PDF** per shortlisted role, foregrounding the JD's keywords without inventing experience you don't have.
- **Drives a visible browser** through the apply form, pre-filling what it can and pausing for human review before any submit click — so you stay in control of every application.

A FastAPI web app sits on top of the SQLite database for monitoring jobs, uploading resumes, reviewing tailored resumes, generating cover letters, and browsing match scores.

---

## Architecture at a glance

```
job-agent/
├── main.py                  # master orchestrator (default: web app; CLI: scrape / analyze / apply / …)
├── config/
│   ├── config.yaml          # roles, locations, platforms, filters, min match score
│   ├── user_profile.json    # parsed resume (gitignored) — legacy single-profile path
│   ├── profiles/            # per-slug parsed resumes (gitignored): data_science.json, ...
│   └── resume_manifest.yaml # optional: slug -> PDF path mapping
├── resume/
│   └── <slug>.pdf           # uploaded via web app (Profiles page)
├── ai/
│   ├── gemini_client.py     # single ask_gemini() wrapper around google-generativeai
│   ├── resume_parser.py     # PDF -> structured profile JSON via Gemini
│   ├── jd_analyzer.py       # JD text -> required/preferred/tools/keywords
│   ├── matcher.py           # weighted overlap -> match_score, matched/missing skills
│   ├── resume_customizer.py # JD-tailored profile + ReportLab PDF
│   └── cover_letter.py      # personalized 3-paragraph letter + PDF
├── scrapers/
│   ├── base_scraper.py      # Job dataclass + abstract BaseScraper
│   ├── internshala.py / indeed.py / naukri.py / wellfound.py /
│   │   glassdoor.py / google_jobs.py / linkedin.py
│   └── scraper_manager.py   # orchestrates enabled platforms (sequential or threaded)
├── automation/
│   └── apply_agent.py       # Playwright form-fill + human-review pause + DB write
├── db/
│   ├── database.py          # SQLite layer: jobs, applications, jd_analysis + stats
│   └── jobs.db
├── dashboard/
│   └── app.py               # legacy Streamlit UI (not wired; optional)
├── web/
│   ├── server.py            # FastAPI app (HTML + /api/* JSON)
│   ├── static/              # CSS + JS (responsive dark theme)
│   └── templates/           # Jinja2 pages
├── output/
│   ├── resumes/             # output/resumes/{company}_{role}_{date}.pdf
│   └── cover_letters/       # output/cover_letters/{company}_{role}_{date}.pdf
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <your-fork-url> ai-job-agent
cd ai-job-agent/job-agent

python -m venv venv

# Windows (PowerShell)
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

`playwright install` downloads a Chromium binary (~150 MB). You only need it once per machine.

### 3. Add your secrets to `.env`

```bash
cp .env.example .env          # macOS / Linux
copy .env.example .env        # Windows
```

Open `.env` and fill in:

```
GEMINI_API_KEY=...
```

That's the only key you need. Get a free one at https://aistudio.google.com/app/apikey (no credit card). The `gemini-2.0-flash` model used by this project sits inside the free tier's daily limits for typical job-search volumes.

### 4. Upload your resume (web app)

Resume PDFs are uploaded and parsed through the **web app** — not via CLI paths or `config.yaml`.

```bash
python main.py
```

Open **http://127.0.0.1:8765/profiles** (Profiles page):

1. Enter a profile name (e.g. `data_science`) or pick an existing profile.
2. Upload your resume PDF.
3. Check **Parse resume** — Gemini writes `config/profiles/<slug>.json` and saves the PDF to `resume/<slug>.pdf`.

Set the active profile in `config.yaml` under `user.active_profile`, or pass `--profile` on CLI modes.

> Power users can still run `python main.py --mode parse-resume --allow-cli-upload` from the terminal.

### 5. Edit `config/config.yaml`

Tune the `user.*` contact block, `job_search.roles` / `locations` / `job_types`, the `min_match_score` threshold, and which `platforms` you want enabled.

---

## How to run

The orchestrator is `main.py`. **With no arguments it launches the web app** on port **8765** (configurable via `web.port` in `config/config.yaml`). Use `--mode` for CLI-only phases (scrape, analyze, apply, etc.).

> **Windows / Oracle XE:** Oracle Database Express Edition often listens on **port 8080** for its web console. If you open `http://127.0.0.1:8080` and see an Oracle license page, that is not this app — use **http://127.0.0.1:8765** instead (or pass `--port 8766` if 8765 is taken).

### Web app (default)

```bash
python main.py
# same as: python main.py --mode web
# alias:   python main.py --mode website
# custom:  python main.py --port 8766
```

Opens **http://127.0.0.1:8765** in your default browser (bind address defaults to `127.0.0.1`; change `web.host` / `web.port` in `config/config.yaml` to listen elsewhere).

| Page | URL | What it does |
|---|---|---|
| Home | `/` | Overview, stats, active profile status |
| Profiles | `/profiles` | Upload PDF, parse resume, list profiles |
| Jobs | `/jobs` | Filterable job list (location, min score, search) |
| Job detail | `/jobs/{id}` | Match score, explanation, JD analysis |
| Resume review | `/resume-review` | Accept/reject tailored resume diffs, generate PDF |
| Cover letter | `/cover-letter` | Generate cover letter PDF for a job |

JSON API endpoints live under `/api/*` (e.g. `/api/jobs`, `/api/stats`) for filter fetches. Basic flows work without JavaScript; filters and resume-review use progressive enhancement.

> `--mode dashboard` is deprecated and redirects to the web app with a notice. The old Streamlit code under `dashboard/` is kept for reference only.

### CLI modes

```bash
# Deprecated CLI parse (power users only — prefer web app upload)
python main.py --mode parse-resume --allow-cli-upload --profile data_science
```

```bash
# Scrape every enabled platform + score new jobs
python main.py --mode scrape

# Parallel mode (1 thread per platform; each scraper runs its own Playwright instance)
python main.py --mode scrape --parallel --workers 4
```

```bash
# Re-score every job in the DB against your current profile, print top 10
python main.py --mode analyze
python main.py --mode analyze --limit 25
```

```bash
# Batch apply: opens a visible browser, pre-fills fields, pauses for ENTER per job
python main.py --mode apply --min-score 65 --limit 5
```

### Common flags

| Flag | Default | Notes |
|---|---|---|
| `--mode` | (web app) | Omit for web UI; or `parse-resume`, `scrape`, `analyze`, `apply`, `web`, `dashboard` (deprecated) |
| `--allow-cli-upload` | off | Required for CLI `parse-resume` (default: use web app) |
| `--resume PATH` | portal upload path | Only with `parse-resume --allow-cli-upload` |
| `--profile SLUG` | from `config.user.active_profile` | Pick a named resume profile (see below) |
| `--min-score N` | from `config.yaml` | Lower for early testing, raise once you have skills extracted |
| `--limit N` | `10` | Cap for analyze / apply |
| `--parallel` | off | Threaded scraping (faster but louder in logs) |
| `--workers N` | `3` | Threadpool size when `--parallel` is set |

---

## Multiple resume profiles

You can keep several parsed resumes side-by-side — one per target role — and pick which one drives matching + apply. Upload and parse each PDF in the web app under **Profiles** (saved as `resume/<slug>.pdf` and `config/profiles/<slug>.json`).

### 1. Upload each resume in the web app

Open **Profiles** at `/profiles`, enter a profile name (slug), upload the PDF, and check **Parse resume**. Repeat for each target role (`data_science`, `full_stack`, etc.).

Optional: if PDFs already live under `resume/`, add a `config/resume_manifest.yaml` mapping for non-standard paths:

```yaml
# config/resume_manifest.yaml - maps profile slug -> PDF path (relative to job-agent/)
data_science: resume/data_science.pdf
full_stack:   resume/web/full_stack_v3.pdf
python:       resume/python.pdf
```

### 2. Re-parse or add profiles

Use the web app **Profiles** page to upload updates. Power users:

```bash
python main.py --mode parse-resume --allow-cli-upload --profile data_science
```

Each successful parse writes `config/profiles/<slug>.json` (gitignored — contains PII).

### 3. Run any mode against a specific profile

```bash
python main.py --profile data_science                  # full pipeline
python main.py --mode scrape    --profile full_stack
python main.py --mode analyze   --profile python --limit 25
python main.py --mode apply     --profile data_science --min-score 65 --limit 5
python main.py --profile data_science  # web app uses config.user.active_profile by default
```

Set `user.active_profile` in `config.yaml` to pick the default profile for matching and apply.

### 4. Pick a default in `config.yaml`

To avoid passing `--profile` every time, set the default under `user:`:

```yaml
user:
  active_profile: data_science     # alias: default_profile
```

CLI `--profile` always wins over the YAML default, which always wins over the legacy single-file mode.

---

## Platform coverage

The agent ships seven scrapers, each behind the same `BaseScraper` interface and toggleable in `config.yaml -> platforms`.

| Platform | Difficulty | Approach | Notes |
|---|---|---|---|
| **Internshala** | Easy | `requests` + BeautifulSoup | Server-rendered listings; visits each card for the full description. |
| **Google Jobs** | Medium | Playwright (no API key) | Hits `google.com/search?...&ibp=htl;jobs` directly. Dismisses EU consent banner, scrolls for lazy-loaded cards. Selectors rot frequently. |
| **LinkedIn** | Medium | Playwright (no login) | Uses the public `/jobs/search/` URL. Public cards only — full JDs + Easy Apply need login and are intentionally not attempted. |
| **Indeed (in.indeed.com)** | Medium | Playwright + 3-page pagination | Visits each card to grab `#jobDescriptionText`. May trigger Cloudflare from datacenter IPs. |
| **Wellfound** | Medium-Hard | Playwright (JS-heavy) | Lazy-loaded React app; scrolls to trigger card loads. Public fields only. |
| **Naukri** | Hard | Playwright + realistic UA + delays | Aggressively fingerprints bots; residential IP / proxy recommended. |
| **Glassdoor (.co.in)** | Hard | Playwright + login-popup dismissal | Dismisses the "create account" modal across 6 known selectors, retries after each scroll. |

Each scraper traps per-card and per-page exceptions, so a single bad listing never kills a run. Selectors are best-current-effort and will inevitably need updating as sites change — fix-up points are clearly marked as `_parse_cards()` in each file.

---

## Tech stack

| Layer | What it does | Library |
|---|---|---|
| LLM | Resume parsing, JD analysis, resume / cover-letter generation | `google-generativeai` (Gemini 2.0 Flash, free tier) |
| PDF in | Text extraction from your resume | `pdfplumber` |
| PDF out | Tailored resume + cover letter rendering | `reportlab` |
| Web HTTP | Lightweight scrapers (Internshala) | `requests`, `beautifulsoup4` |
| Browser | JS-heavy scrapers + apply automation | `playwright` (Chromium) |
| Anti-fingerprint | Realistic UA rotation | `fake-useragent` |
| Storage | Jobs, applications, JD analyses | `sqlite3` (stdlib) |
| Config | YAML config + env secrets | `pyyaml`, `python-dotenv` |
| Web UI | Browser app (default entry point) | `fastapi`, `uvicorn`, `jinja2` |

Python 3.10+ required (uses `str | None` syntax and structural pattern features).

---

## Important: ethical use

This is your assistant, not a fraud machine. The agent is built around the assumption that **you** are the final reviewer of every application.

- **Customize wording, never fabricate experience.** The resume customizer prompt explicitly forbids inventing skills, projects, durations, or job titles. It is only allowed to *reword* what is already true to foreground JD keywords. If a JD asks for a skill you don't have, the agent leaves it out — and so should you.
- **Read every resume and cover letter before submitting.** Open the generated PDFs under `output/resumes/` and `output/cover_letters/` and skim them. If something feels off, regenerate.
- **The apply agent pauses for a reason.** It will fill what it can, then hand the browser back to you for review with a prompt to press `ENTER`. Use that pause. Fix anything wrong, then submit. Ctrl+C aborts cleanly.
- **Respect each platform's Terms of Service and rate limits.** The scrapers include random delays and realistic user agents, but volume is still your responsibility. Don't run this against a single platform at industrial scale.
- **Use your own accounts, not someone else's.** And if you're applying from a work machine, make sure your employer is okay with it.
- **Tell the truth in interviews.** If you can't speak to a skill in 5 minutes face-to-face, it shouldn't be on your tailored resume.

You remain accountable for everything the agent submits on your behalf.

---

## Screenshots

> Drop PNGs into `docs/screenshots/` and uncomment the references below.

<!--
### Overview
![Overview](docs/screenshots/overview.png)

### Job Browser
![Job Browser](docs/screenshots/job_browser.png)

### Applications
![Applications](docs/screenshots/applications.png)

### Analytics
![Analytics](docs/screenshots/analytics.png)

### Settings
![Settings](docs/screenshots/settings.png)
-->

---

## License

Provided as-is for personal job-search use. Be a decent person with it.
