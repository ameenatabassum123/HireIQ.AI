# Repository Guidelines

## Project Structure & Module Organization
The AI Job Application Agent is a modular Python application designed for end-to-end job search automation.

- **`main.py`**: The central orchestrator handling CLI commands and the interactive pipeline.
- **`ai/`**: Contains LLM-driven logic using Google Gemini 2.0 Flash for resume parsing (`resume_parser.py`), job description analysis (`jd_analyzer.py`), match scoring (`matcher.py`), and PDF generation for tailored resumes (`resume_customizer.py`) and cover letters (`cover_letter.py`).
- **`scrapers/`**: Houses independent scrapers for seven platforms (LinkedIn, Indeed, Naukri, Wellfound, Glassdoor, Google Jobs, Internshala). All scrapers inherit from `BaseScraper` and are managed by `scraper_manager.py`.
- **`automation/`**: Contains `apply_agent.py`, which uses Playwright for browser-driven form pre-filling and application submission.
- **`db/`**: SQLite persistence layer (`database.py`) for job listings, application history, and JD analysis stats.
- **`dashboard/`**: A Streamlit-based UI (`app.py`) for real-time monitoring and analytics.
- **`config/`**: Stores `config.yaml` for environment settings and `profiles/` for structured user resume data.

## Build, Test, and Development Commands
The project requires Python 3.10+ and Playwright.

### Setup
```bash
pip install -r requirements.txt
playwright install chromium
```

### Core Commands
- **Full Pipeline**: `python main.py`
- **Parse Resume**: `python main.py --mode parse-resume`
- **Scrape Jobs**: `python main.py --mode scrape` (Add `--parallel` for threaded scraping)
- **Analyze Matches**: `python main.py --mode analyze --limit 25`
- **Apply Automation**: `python main.py --mode apply --min-score 70`
- **Launch Dashboard**: `python main.py --mode dashboard`

## Coding Style & Naming Conventions
- **Language**: Python 3.10+ using modern type hint syntax (e.g., `str | None`).
- **File Handling**: Prefer `pathlib.Path` over `os.path`.
- **LLM**: Uses `google-generativeai` (Gemini 2.0 Flash). Prompts are embedded in the respective AI module files.
- **Web Automation**: Playwright is the preferred tool for JS-heavy scraping and application automation.

## Important Note
This repository was analyzed as a non-git directory. Ensure you manually track changes if version control is not initialized.
