# JobAgent

An AI-powered autonomous job search and application system that automates the entire pipeline: discovering ML/AI roles, analyzing candidate fit, tailoring resumes and cover letters, and auto-applying to positions across multiple ATS platforms.

## Features

- **Intelligent Job Search** — Aggregates listings from LinkedIn, Indeed, Greenhouse, and Lever with ML/AI role detection and entry-level filtering
- **AI Fit Analysis** — Scores candidacy 0–100 with detailed strong matches, partial matches, and gaps
- **Resume Tailoring** — Rewrites and reorders resume bullets to mirror job posting keywords while preserving all real experience
- **Cover Letter Generation** — Creates personalized cover letters with company-specific hooks and STAR-format evidence
- **Auto-Apply** — Playwright-based form filling for 8+ ATS platforms with human-like typing, LLM-powered question answering, and persistent browser sessions
- **Application Tracking** — SQLite-backed tracker with URL-based deduplication, status management, and dashboard stats
- **PDF Resume Generation** — Converts tailored markdown resumes to formatted DOCX/PDF with professional styling

## Supported ATS Platforms

| Platform | Method |
|----------|--------|
| Greenhouse | React-aware form fill with id-based selectors |
| Workday | Multi-step wizard with data-automation-id |
| Lever | Standard HTML form fill |
| Ashby | Label-based dynamic forms |
| LinkedIn | Easy Apply modal automation |
| SmartRecruiters | Standard form fill |
| iCIMS | Auto-fill with manual fallback |
| Jobvite | Standard form fill |
| Generic | LLM-analyzed form structure |

## Quick Start

### Prerequisites

- Python 3.12+
- An LLM API key (Anthropic or OpenAI)
- Playwright with Chromium

### Installation

```bash
git clone https://github.com/AdityaModi7/JobAgent.git
cd JobAgent
pip install -r requirements.txt
playwright install chromium
```

### Configuration

1. **Set your API key:**

```bash
# Option A: Anthropic (Claude Sonnet 4)
export ANTHROPIC_API_KEY=sk-ant-...

# Option B: OpenAI (GPT-4o)
export OPENAI_API_KEY=sk-...
```

2. **Add your resume** to `data/my_resume.md` in markdown format.

3. **Set preferences** in `data/preferences.json`:

```json
{
  "application_defaults": {
    "needs_sponsorship": false,
    "work_authorized": true,
    "willing_to_relocate": true,
    "willing_hybrid": true,
    "how_did_you_hear": "Company Website",
    "country": "United States",
    "state": "New York",
    "years_of_experience": "1"
  },
  "eeoc_skip": true,
  "auto_check_affirmations": true
}
```

4. **Log in to job platforms** (one-time, saves cookies):

```bash
python main.py login linkedin
python main.py login workday
```

## Usage

### Full Automated Workflow

```bash
# Dry run (generates materials, fills forms, no submission)
python main.py run

# Live mode (actually submits applications)
python main.py run --live

# Custom filters
python main.py run --live --max-apps 10 --min-score 70
```

### Apply to a Single Job

```bash
# From URL
python main.py apply --url https://boards.greenhouse.io/company/jobs/12345

# With auto-apply
python main.py apply --url https://... --auto

# Live submission
python main.py apply --url https://... --auto --live
```

### Search for Jobs

```bash
python main.py search
python main.py search --keywords "ml engineer" --location "New York, NY"
```

### Batch Process

```bash
# Add URLs to data/job_urls.txt (one per line), then:
python main.py batch

# Or pass URLs directly
python main.py batch --urls https://job1.com https://job2.com
```

### Track Applications

```bash
# View dashboard
python main.py dashboard

# List recent applications
python main.py list

# Update status
python main.py status 42 interview
```

## Architecture

```
JobAgent/
├── main.py                    # CLI with 8 commands
├── config.py                  # LLM provider detection
│
├── agents/
│   ├── workflow_engine.py     # Pipeline orchestrator
│   ├── auto_applier.py        # Playwright form-filling engine
│   ├── tailoring_agent.py     # Fit analysis + resume/cover letter gen
│   ├── job_parser.py          # Job posting extraction (HTML + Playwright)
│   ├── job_searcher.py        # Multi-source job search
│   ├── profile_loader.py      # Resume → structured Profile
│   ├── tracker.py             # SQLite application database
│   ├── discovery_agent.py     # Free API job discovery
│   ├── batch_processor.py     # Batch URL processing
│   ├── form_validator.py      # Post-fill form verification
│   └── auto_submit.py         # Platform-specific form fillers
│
├── models/
│   └── __init__.py            # Pydantic models (Profile, JobPosting, FitAnalysis, etc.)
│
├── utils/
│   ├── llm.py                 # Unified LLM client (Anthropic/OpenAI)
│   ├── pdf_generator.py       # Markdown → DOCX/PDF conversion
│   └── logging_config.py      # Console + file logging setup
│
├── data/
│   ├── my_resume.md           # Your resume (input)
│   ├── profile.json           # Parsed profile cache
│   ├── preferences.json       # Application form defaults
│   ├── applications.db        # SQLite tracker database
│   └── browser_session/       # Persistent login cookies
│
└── output/
    ├── {company}_{role}/      # Per-application materials
    │   ├── resume.md
    │   ├── cover_letter.md
    │   └── fit_analysis.md
    └── auto_apply/
        └── run_report_*.json  # Workflow run summaries
```

## Pipeline Flow

```
Search Jobs (LinkedIn, Indeed, Greenhouse, Lever)
       │
       ▼
Deduplicate (URL + company|title matching)
       │
       ▼
Parse Job Posting (BeautifulSoup or Playwright)
       │
       ▼
Analyze Fit (LLM scores 0-100)
       │
       ├── Score < threshold → Skip
       │
       ▼
Tailor Resume + Cover Letter (LLM)
       │
       ▼
Generate PDF Resume
       │
       ▼
Auto-Apply (platform-specific Playwright handler)
       │
       ▼
Save to Tracker (SQLite + output files)
```

## Key Design Decisions

- **LLM-agnostic** — Swap between Anthropic and OpenAI with a single env var
- **Never fabricates experience** — Resume tailoring reframes and reorders, never invents
- **Dry-run by default** — All workflows preview before submitting
- **Persistent sessions** — Log in once, cookies saved for future runs
- **Graceful degradation** — If auto-apply fails, materials are still saved for manual use
- **Rate limiting** — Configurable cooldown between applications (default 30s)

## Application Statuses

| Status | Description |
|--------|-------------|
| `draft` | Apply attempted but failed, or materials generated only |
| `skipped` | Below fit threshold, intentionally not applied |
| `applied` | Successfully submitted |
| `followed_up` | Follow-up email sent |
| `interview` | Interview scheduled |
| `rejected` | Application rejected |
| `offer` | Offer received |
| `withdrawn` | Application withdrawn |

## License

MIT
