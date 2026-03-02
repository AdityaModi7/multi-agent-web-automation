"""Job Parser — Extracts structured job data from URLs or raw text.

Supports JS-rendered pages (Ashby, Greenhouse, Lever, etc.) via Playwright.
Falls back to requests+BeautifulSoup for simple pages.
"""

from models import JobPosting
from utils.llm import call_llm_json
from bs4 import BeautifulSoup
import requests
import sys
import re
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


PARSER_SYSTEM_PROMPT = """You are a job posting parser. Given raw job posting text, extract structured information.

Return ONLY valid JSON with this exact schema (no markdown, no backticks):
{
 "title": "Job Title",
 "company": "Company Name",
 "location": "City, State or Remote",
 "remote": true,
 "salary_range": "$X - $Y" or null,
 "description": "Brief 2-3 sentence summary of the role",
 "responsibilities": ["responsibility 1", "responsibility 2"],
 "required_skills": ["skill 1", "skill 2"],
 "preferred_skills": ["nice-to-have 1", "nice-to-have 2"],
 "required_experience_years": null,
 "education_requirement": "Bachelor's in CS" or null,
 "company_info": "Brief company description if available" or null
}

IMPORTANT: Every field must have a value. For title, company, and description use your best guess from the text — never return null for these. Be thorough in extracting skills."""


# ── Sites that need a real browser ────────────────────────────────────────

JS_RENDERED_SITES = [
                "ashbyhq.com",
                "lever.co",
                "myworkdayjobs.com",
                "workday.com",
                "icims.com",
                "smartrecruiters.com",
                "jobvite.com",
]


def needs_browser(url: str) -> bool:
    """Check if a URL is known to need browser rendering."""
    return any(site in url.lower() for site in JS_RENDERED_SITES)


# ── Playwright Browser Fetch ─────────────────────────────────────────────

def fetch_with_browser(url: str) -> str:
    """Fetch page content using a real browser (handles JavaScript-rendered pages)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "Playwright is required for JS-rendered pages.\n"
            "Install it:\n"
            " pip install playwright\n"
            " playwright install chromium"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            content = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(content, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
        tag.decompose()

    selectors = [
        '[class*="job-description"]',
        '[class*="job-details"]',
        '[class*="posting-"]',
        '[class*="ashby-"]',
        '[class*="content"]',
        "article",
        '[id*="job"]',
        "main",
    ]

    for selector in selectors:
        elements = soup.select(selector)
        for el in elements:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 200:
                return text

    body = soup.find("body")
    if body:
        return body.get_text(separator="\n", strip=True)

    return soup.get_text(separator="\n", strip=True)


# ── Simple HTML Fetch ─────────────────────────────────────────────────────

def fetch_html_simple(url: str) -> str:
    """Simple HTML fetcher for non-JS pages."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    selectors = [
        "article",
        '[class*="job-description"]',
        '[class*="job-details"]',
        '[class*="posting"]',
        '[class*="content"]',
        '[id*="job"]',
        "main",
    ]

    for selector in selectors:
        content = soup.select_one(selector)
        if content and len(content.get_text(strip=True)) > 200:
            return content.get_text(separator="\n", strip=True)

    body = soup.find("body")
    if body:
        text = body.get_text(separator="\n", strip=True)
        if len(text) > 200:
            return text

    return soup.get_text(separator="\n", strip=True)


# ── Main Fetch Logic ──────────────────────────────────────────────────────

def fetch_job_url(url: str) -> str:
    """Fetch job posting text using the best strategy for the URL."""

    # For known JS-rendered sites, go straight to browser
    if needs_browser(url):
        print(f" JS-rendered site detected, using browser...")
        try:
            text = fetch_with_browser(url)
            if len(text.strip()) > 100:
                print(f" [OK] Fetched {len(text)} characters via browser")
                return text
        except ImportError as e:
            print(f" [WARNING] {e}")
        except Exception as e:
            print(f" [WARNING] Browser fetch failed: {e}")

    # Try simple HTML fetch
    print(f" Trying simple HTML scraper...")
    try:
        text = fetch_html_simple(url)
        if len(text.strip()) > 100:
            print(f" [OK] Fetched {len(text)} characters")
            return text
    except Exception as e:
        print(f" [WARNING] HTML scraper failed: {e}")

    # If simple fetch didn't work and we haven't tried browser yet, try it
    if not needs_browser(url):
        print(f" Simple scraper insufficient, trying browser...")
        try:
            text = fetch_with_browser(url)
            if len(text.strip()) > 100:
                print(f" [OK] Fetched {len(text)} characters via browser")
                return text
        except ImportError:
            pass
        except Exception as e:
            print(f" [WARNING] Browser fetch also failed: {e}")

    # All strategies failed
    raise ValueError(
        "Could not extract job posting content from this URL.\n\n"
        "Try instead:\n"
        " 1. Copy the job description text from the page\n"
        " 2. Save it to a file (e.g., job.txt)\n"
        " 3. Run: python main.py apply --job-file job.txt\n\n"
        "Or paste it directly:\n"
        " python main.py apply (then paste and hit Ctrl+D)"
    )


# ── Parse Job Posting ─────────────────────────────────────────────────────

def parse_job_posting(text: str = None, url: str = None) -> JobPosting:
    """Parse a job posting from raw text or URL into structured data."""
    if not text and not url:
        raise ValueError("Must provide either text or url")

    raw_text = text or ""
    if url:
        raw_text = fetch_job_url(url)

    data = call_llm_json(
        system_prompt=PARSER_SYSTEM_PROMPT,
        user_message=f"Parse this job posting:\n\n{raw_text[:8000]}",
        max_tokens=2000,
    )

    data["title"] = data.get("title") or "Unknown Title"
    data["company"] = data.get("company") or "Unknown Company"
    data["description"] = data.get("description") or "No description available"

    data["raw_text"] = raw_text[:5000]
    if url:
        data["application_url"] = url

    return JobPosting(**data)
