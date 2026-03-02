"""Auto Applier — Uses Playwright to submit job applications.

Handles multiple application platforms:
- Greenhouse (form fill)
- Lever (form fill)
- LinkedIn Easy Apply (form fill)
- Ashby (form fill)
- Generic application forms
- Email-based applications

Requires: playwright install chromium
"""

import sys
import os
import re
import time
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.llm import call_llm_json, call_llm
from models import ApplyAttempt, ApplyMethod, JobSearchResult
from agents.form_validator import (
    validate_filled_form, llm_visual_verify,
    save_validation_report, print_validation_report,
)


# ── User info for form filling (loaded from profile.json) ──────────────


def load_applicant_info() -> dict:
    """Load applicant info from the cached profile."""
    profile_path = Path(__file__).parent.parent / "data" / "profile.json"
    if profile_path.exists():
        data = json.loads(profile_path.read_text())
        return data
    return {}


# ── Platform Detection ──────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    """Detect which ATS platform a job URL belongs to."""
    url_lower = url.lower()
    if "greenhouse.io" in url_lower or "boards.greenhouse" in url_lower:
        return "greenhouse"
    elif "lever.co" in url_lower or "jobs.lever" in url_lower:
        return "lever"
    elif "linkedin.com" in url_lower:
        return "linkedin"
    elif "ashbyhq.com" in url_lower:
        return "ashby"
    elif "myworkdayjobs.com" in url_lower or "workday.com" in url_lower:
        return "workday"
    elif "smartrecruiters.com" in url_lower:
        return "smartrecruiters"
    elif "icims.com" in url_lower:
        return "icims"
    elif "jobvite.com" in url_lower:
        return "jobvite"
    else:
        return "generic"


# ── Resume File Management ──────────────────────────────────────────────

def get_resume_path(tailored_resume_text: str, company: str, title: str) -> str:
    """Save tailored resume as a text file and return path.

    For actual applications, users should convert to PDF.
    This creates a clean .txt version that many ATS systems accept.
    """
    output_dir = Path(__file__).parent.parent / "output" / "auto_apply"
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r'[^\w\-]', '_', f"{company}_{title}").lower()
    resume_path = output_dir / f"resume_{safe_name}.md"
    resume_path.write_text(tailored_resume_text)
    return str(resume_path)


# ── Greenhouse Application ──────────────────────────────────────────────

def apply_greenhouse(page, url: str, info: dict, resume_path: str, cover_letter: str, dry_run: bool = False) -> bool:
    """Fill and submit a Greenhouse application form."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    # Check if there's an apply button to click first
    apply_btn = page.query_selector(
        'a[href*="#app"], button:has-text("Apply"), a:has-text("Apply")')
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(2000)

    # Fill standard Greenhouse fields (id-based)
    name_parts = info.get("name", "").split() if info.get("name") else []
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    field_map = {
        '#first_name': first_name,
        '#last_name': last_name,
        '#email': info.get("email", ""),
        '#phone': info.get("phone", ""),
    }

    for selector, value in field_map.items():
        if value:
            el = page.query_selector(selector)
            if el:
                el.fill(value)
                page.wait_for_timeout(300)

    # Try alternate field selectors (Greenhouse varies by company)
    alt_fields = [
        ('input[name*="first_name"]', first_name),
        ('input[name*="last_name"]', last_name),
        ('input[name*="email"]', info.get("email", "")),
        ('input[name*="phone"]', info.get("phone", "")),
        ('input[name*="linkedin"]', info.get("linkedin", "")),
        ('input[name*="github"]', info.get("github", "")),
        ('input[name*="website"]', info.get("portfolio", "")),
        # Broader selectors for modern Greenhouse forms
        ('input[autocomplete="given-name"]', first_name),
        ('input[autocomplete="family-name"]', last_name),
        ('input[autocomplete="email"]', info.get("email", "")),
        ('input[autocomplete="tel"]', info.get("phone", "")),
    ]

    for selector, value in alt_fields:
        if value:
            el = page.query_selector(selector)
            if el and not el.input_value():
                el.fill(value)
                page.wait_for_timeout(300)

    # Scan all visible inputs by label/placeholder (catches custom Greenhouse forms)
    all_inputs = page.query_selector_all(
        'input[type="text"]:visible, input[type="email"]:visible, '
        'input[type="tel"]:visible, input[type="url"]:visible'
    )
    for inp in all_inputs:
        if inp.input_value():
            continue  # Already filled
        placeholder = (inp.get_attribute("placeholder") or "").lower()
        name_attr = (inp.get_attribute("name") or "").lower()
        aria = (inp.get_attribute("aria-label") or "").lower()
        field_id = inp.get_attribute("id") or ""
        label_text = ""
        if field_id:
            label = page.query_selector(f'label[for="{field_id}"]')
            if label:
                label_text = label.inner_text().lower()
        combined = f"{placeholder} {name_attr} {aria} {label_text}"

        if "first" in combined and "name" in combined:
            inp.fill(first_name)
        elif "last" in combined and "name" in combined:
            inp.fill(last_name)
        elif "full name" in combined or combined.strip() == "name":
            inp.fill(info.get("name", ""))
        elif "email" in combined:
            inp.fill(info.get("email", ""))
        elif "phone" in combined or "mobile" in combined:
            inp.fill(info.get("phone", ""))
        elif "linkedin" in combined:
            inp.fill(info.get("linkedin", ""))
        elif "github" in combined:
            inp.fill(info.get("github", ""))
        page.wait_for_timeout(200)

    # Upload resume
    resume_input = page.query_selector(
        'input[type="file"][name*="resume"], input[type="file"][id*="resume"]')
    if not resume_input:
        resume_input = page.query_selector('input[type="file"]')
    if resume_input and resume_path:
        resume_input.set_input_files(resume_path)
        page.wait_for_timeout(1000)

    # Fill cover letter textarea
    cover_el = page.query_selector(
        'textarea[name*="cover_letter"], textarea[id*="cover_letter"], '
        'textarea[placeholder*="cover letter"]'
    )
    if not cover_el:
        # Try any visible textarea
        textareas = page.query_selector_all('textarea:visible')
        for ta in textareas:
            ta_name = (ta.get_attribute("name") or "").lower()
            ta_ph = (ta.get_attribute("placeholder") or "").lower()
            if any(kw in f"{ta_name} {ta_ph}" for kw in ["cover", "letter", "additional", "comments"]):
                cover_el = ta
                break
    if cover_el and cover_letter:
        cover_el.fill(cover_letter)

    # Location / salary fields (fill if present)
    location_el = page.query_selector(
        'input[name*="location"], input[placeholder*="location"]')
    if location_el:
        location_el.fill(info.get("location", "New York, NY"))

    # Take screenshot before submit
    screenshot_dir = Path(__file__).parent.parent / \
        "output" / "auto_apply" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(screenshot_dir / f"greenhouse_{timestamp}.png")
    page.screenshot(path=screenshot_path, full_page=True)

    if dry_run:
        return True

    # Submit
    submit_btn = page.query_selector(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Submit"), button:has-text("Apply")'
    )
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(3000)

        # Check for success indicators
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "application received", "successfully submitted", "application submitted"]):
            return True

    return False


# ── Lever Application ───────────────────────────────────────────────────

def apply_lever(page, url: str, info: dict, resume_path: str, cover_letter: str, dry_run: bool = False) -> bool:
    """Fill and submit a Lever application form."""
    # Lever apply pages are at /apply
    apply_url = url if "/apply" in url else url.rstrip("/") + "/apply"
    page.goto(apply_url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    # Lever form fields
    field_map = [
        ('input[name="name"]', info.get("name", "")),
        ('input[name="email"]', info.get("email", "")),
        ('input[name="phone"]', info.get("phone", "")),
        ('input[name="org"]', info.get("experience", [{}])[0].get(
            "company", "") if info.get("experience") else ""),
        ('input[name="urls[LinkedIn]"]', info.get("linkedin", "")),
        ('input[name="urls[GitHub]"]', info.get("github", "")),
        ('input[name="urls[Portfolio]"]', info.get("portfolio", "")),
    ]

    for selector, value in field_map:
        if value:
            el = page.query_selector(selector)
            if el:
                el.fill(value)
                page.wait_for_timeout(300)

    # Upload resume
    resume_input = page.query_selector('input[type="file"][name="resume"]')
    if not resume_input:
        resume_input = page.query_selector('input[type="file"]')
    if resume_input and resume_path:
        resume_input.set_input_files(resume_path)
        page.wait_for_timeout(1000)

    # Cover letter
    cover_el = page.query_selector('textarea[name="comments"]')
    if not cover_el:
        cover_el = page.query_selector('textarea:visible')
    if cover_el and cover_letter:
        cover_el.fill(cover_letter)

    # Screenshot
    screenshot_dir = Path(__file__).parent.parent / \
        "output" / "auto_apply" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(screenshot_dir / f"lever_{timestamp}.png")
    page.screenshot(path=screenshot_path, full_page=True)

    if dry_run:
        return True

    # Submit
    submit_btn = page.query_selector(
        'button[type="submit"], button:has-text("Submit"), '
        'a.postings-btn:has-text("Submit")'
    )
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(3000)
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "application received", "submitted"]):
            return True

    return False


# ── Ashby Application ───────────────────────────────────────────────────

def apply_ashby(page, url: str, info: dict, resume_path: str, cover_letter: str, dry_run: bool = False) -> bool:
    """Fill and submit an Ashby application form."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    # Click Apply if needed
    apply_btn = page.query_selector(
        'button:has-text("Apply"), a:has-text("Apply")')
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(2000)

    # Ashby uses various input names — try common patterns
    name_parts = info.get("name", "").split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    inputs = page.query_selector_all("input:visible, textarea:visible")
    for inp in inputs:
        placeholder = (inp.get_attribute("placeholder") or "").lower()
        name = (inp.get_attribute("name") or "").lower()
        label_text = ""
        label_id = inp.get_attribute("id")
        if label_id:
            label = page.query_selector(f'label[for="{label_id}"]')
            if label:
                label_text = label.inner_text().lower()

        combined = f"{placeholder} {name} {label_text}"

        if "first" in combined and "name" in combined:
            inp.fill(first_name)
        elif "last" in combined and "name" in combined:
            inp.fill(last_name)
        elif "full name" in combined or combined.strip() == "name":
            inp.fill(info.get("name", ""))
        elif "email" in combined:
            inp.fill(info.get("email", ""))
        elif "phone" in combined:
            inp.fill(info.get("phone", ""))
        elif "linkedin" in combined:
            inp.fill(info.get("linkedin", ""))
        elif "github" in combined:
            inp.fill(info.get("github", ""))
        elif "cover" in combined and inp.tag_name == "textarea":
            inp.fill(cover_letter or "")
        page.wait_for_timeout(200)

    # Upload resume
    file_input = page.query_selector('input[type="file"]')
    if file_input and resume_path:
        file_input.set_input_files(resume_path)
        page.wait_for_timeout(1000)

    # Screenshot
    screenshot_dir = Path(__file__).parent.parent / \
        "output" / "auto_apply" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(screenshot_dir / f"ashby_{timestamp}.png")
    page.screenshot(path=screenshot_path, full_page=True)

    if dry_run:
        return True

    # Submit
    submit_btn = page.query_selector(
        'button[type="submit"], button:has-text("Submit")')
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(3000)
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "submitted", "received"]):
            return True

    return False


# ── Workday Application ─────────────────────────────────────────────────

def apply_workday(page, url: str, info: dict, resume_path: str, cover_letter: str, dry_run: bool = False) -> bool:
    """Fill and submit a Workday application form.

    Workday uses a multi-step wizard. We handle the common steps:
    1. My Information (name, email, phone)
    2. My Experience (resume upload)
    3. Application Questions
    4. Review & Submit
    """
    page.goto(url, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3000)

    # Click the Apply button to start the flow
    apply_btn = page.query_selector(
        'a[data-automation-id="jobPostingApplyButton"], '
        'button[data-automation-id="jobPostingApplyButton"], '
        'a:has-text("Apply"), button:has-text("Apply")'
    )
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(3000)

    # Some Workday sites ask to create account or use autofill — try to skip
    skip_btn = page.query_selector(
        'button:has-text("Skip"), a:has-text("Manual Apply"), '
        'button:has-text("Apply Manually")'
    )
    if skip_btn:
        skip_btn.click()
        page.wait_for_timeout(2000)

    # --- Step 1: My Information ---
    name_parts = info.get("name", "").split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    workday_fields = [
        ('input[data-automation-id="legalNameSection_firstName"]', first_name),
        ('input[data-automation-id="legalNameSection_lastName"]', last_name),
        ('input[data-automation-id="email"]', info.get("email", "")),
        ('input[data-automation-id="phone-number"]', info.get("phone", "")),
        ('input[data-automation-id="addressSection_addressLine1"]',
            info.get("location", "New York, NY")),
    ]

    for selector, value in workday_fields:
        if value:
            el = page.query_selector(selector)
            if el:
                el.click()
                el.fill(value)
                page.wait_for_timeout(300)

    # Also try generic input scanning for Workday variants
    all_inputs = page.query_selector_all(
        'input[type="text"]:visible, input[type="email"]:visible, input[type="tel"]:visible')
    for inp in all_inputs:
        auto_id = inp.get_attribute("data-automation-id") or ""
        placeholder = (inp.get_attribute("placeholder") or "").lower()
        aria_label = (inp.get_attribute("aria-label") or "").lower()
        combined = f"{auto_id} {placeholder} {aria_label}".lower()

        if not inp.input_value():  # Only fill empty fields
            if "first" in combined and "name" in combined:
                inp.fill(first_name)
            elif "last" in combined and "name" in combined:
                inp.fill(last_name)
            elif "email" in combined:
                inp.fill(info.get("email", ""))
            elif "phone" in combined:
                inp.fill(info.get("phone", ""))
            page.wait_for_timeout(200)

    # --- Step 2: Resume Upload ---
    file_input = page.query_selector(
        'input[type="file"][data-automation-id*="resume"], '
        'input[type="file"][data-automation-id="file-upload-input-ref"], '
        'input[type="file"]'
    )
    if file_input and resume_path:
        file_input.set_input_files(resume_path)
        page.wait_for_timeout(2000)

    # Click Next/Continue to advance through wizard
    for _ in range(3):  # Try up to 3 steps
        next_btn = page.query_selector(
            'button[data-automation-id="bottom-navigation-next-button"], '
            'button:has-text("Next"), button:has-text("Continue"), '
            'button:has-text("Save and Continue")'
        )
        if next_btn:
            next_btn.click()
            page.wait_for_timeout(2000)

            # Fill any additional fields that appear
            for inp in page.query_selector_all('input[type="text"]:visible:not([value])'):
                auto_id = (inp.get_attribute("data-automation-id") or "").lower()
                if "linkedin" in auto_id:
                    inp.fill(info.get("linkedin", ""))
                elif "github" in auto_id or "website" in auto_id:
                    inp.fill(info.get("github", "") or info.get("portfolio", ""))

            # Fill cover letter if textarea appears
            textareas = page.query_selector_all("textarea:visible")
            for ta in textareas:
                if not ta.input_value() and cover_letter:
                    ta.fill(cover_letter)

    # Screenshot before final submit
    screenshot_dir = Path(__file__).parent.parent / \
        "output" / "auto_apply" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(screenshot_dir / f"workday_{timestamp}.png")
    page.screenshot(path=screenshot_path, full_page=True)

    if dry_run:
        return True

    # Final submit
    submit_btn = page.query_selector(
        'button[data-automation-id="bottom-navigation-next-button"]:has-text("Submit"), '
        'button:has-text("Submit Application"), button:has-text("Submit")'
    )
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(4000)
        success_text = page.content().lower()
        if any(s in success_text for s in [
            "thank you", "application submitted", "successfully submitted",
            "application received", "we have received your application",
        ]):
            return True

    return False


# ── Generic Form Filler (LLM-assisted) ─────────────────────────────────

FORM_ANALYSIS_PROMPT = """You are a web form analyzer. Given the HTML of a job application form, identify the form fields and map them to applicant data.

Return ONLY valid JSON (no markdown, no backticks):
{
 "fields": [
 {
 "selector": "CSS selector to target this field",
 "field_type": "text | email | tel | file | textarea | select | checkbox",
 "label": "What the field asks for",
 "value_key": "Which applicant data to use: name | first_name | last_name | email | phone | linkedin | github | location | cover_letter | resume | company | title | other",
 "value": "Exact value to fill (only for 'other' value_key)"
 }
 ],
 "submit_selector": "CSS selector for the submit button"
}"""


def apply_generic(page, url: str, info: dict, resume_path: str, cover_letter: str, dry_run: bool = False) -> bool:
    """Use LLM to analyze and fill a generic application form."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    # Click Apply if we see a button
    apply_btn = page.query_selector(
        'button:has-text("Apply"), a:has-text("Apply Now"), '
        'a:has-text("Apply for this"), button:has-text("Apply Now")'
    )
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(2000)

    # Get form HTML for LLM analysis
    form_el = page.query_selector("form")
    if not form_el:
        # No form found — mark as manual
        return False

    form_html = form_el.inner_html()
    # Truncate to avoid token limits
    form_html = form_html[:6000]

    try:
        analysis = call_llm_json(
            system_prompt=FORM_ANALYSIS_PROMPT,
            user_message=f"Analyze this form:\n\n{form_html}",
            max_tokens=1500,
        )
    except Exception:
        return False

    name_parts = info.get("name", "").split()
    value_map = {
        "name": info.get("name", ""),
        "first_name": name_parts[0] if name_parts else "",
        "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
        "email": info.get("email", ""),
        "phone": info.get("phone", ""),
        "linkedin": info.get("linkedin", ""),
        "github": info.get("github", ""),
        "location": info.get("location", "New York, NY"),
        "cover_letter": cover_letter or "",
        "company": info.get("experience", [{}])[0].get("company", "") if info.get("experience") else "",
    }

    # Fill fields
    for field in analysis.get("fields", []):
        selector = field.get("selector", "")
        field_type = field.get("field_type", "text")
        value_key = field.get("value_key", "")

        if not selector:
            continue

        el = page.query_selector(selector)
        if not el:
            continue

        if field_type == "file" and resume_path:
            el.set_input_files(resume_path)
        elif field_type in ("text", "email", "tel", "textarea"):
            value = value_map.get(value_key, field.get("value", ""))
            if value:
                el.fill(value)
                page.wait_for_timeout(300)

    # Screenshot
    screenshot_dir = Path(__file__).parent.parent / \
        "output" / "auto_apply" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(screenshot_dir / f"generic_{timestamp}.png")
    page.screenshot(path=screenshot_path, full_page=True)

    if dry_run:
        return True

    # Submit
    submit_selector = analysis.get("submit_selector", 'button[type="submit"]')
    submit_btn = page.query_selector(submit_selector)
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(3000)
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "submitted", "received", "success"]):
            return True

    return False


# ── Main Apply Function ─────────────────────────────────────────────────

def auto_apply(
    job_url: str,
    company: str,
    title: str,
    resume_text: str,
    cover_letter_text: str,
    dry_run: bool = True,
) -> ApplyAttempt:
    """Attempt to automatically apply to a job.

    Args:
    job_url: URL of the job posting or application page.
    company: Company name.
    title: Job title.
    resume_text: Tailored resume text.
    cover_letter_text: Tailored cover letter text.
    dry_run: If True, fill forms and screenshot but don't submit.

    Returns:
    ApplyAttempt with success/failure details.
    """
    platform = detect_platform(job_url)
    info = load_applicant_info()

    # Save resume to file for upload
    resume_path = get_resume_path(resume_text, company, title)

    print(f" Platform detected: {platform}")
    print(f" {' DRY RUN — will not submit' if dry_run else ' LIVE — will submit application'}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ApplyAttempt(
            job_url=job_url,
            company=company,
            title=title,
            method=ApplyMethod.MANUAL,
            success=False,
            error="Playwright not installed. Run: pip install playwright && playwright install chromium",
        )

    attempt = ApplyAttempt(
        job_url=job_url,
        company=company,
        title=title,
        method=ApplyMethod.FORM_FILL,
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            success = False

            if platform == "greenhouse":
                attempt.method = ApplyMethod.FORM_FILL
                success = apply_greenhouse(
                    page, job_url, info, resume_path, cover_letter_text, dry_run=dry_run)

            elif platform == "lever":
                attempt.method = ApplyMethod.FORM_FILL
                success = apply_lever(
                    page, job_url, info, resume_path, cover_letter_text, dry_run=dry_run)

            elif platform == "ashby":
                attempt.method = ApplyMethod.FORM_FILL
                success = apply_ashby(
                    page, job_url, info, resume_path, cover_letter_text, dry_run=dry_run)

            elif platform == "linkedin":
                # LinkedIn Easy Apply requires login — flag for manual
                attempt.method = ApplyMethod.REDIRECT
                attempt.error = "LinkedIn requires login for Easy Apply. Application materials saved — apply manually."
                success = False

            elif platform == "workday":
                attempt.method = ApplyMethod.FORM_FILL
                success = apply_workday(
                    page, job_url, info, resume_path, cover_letter_text, dry_run=dry_run)

            elif platform == "icims":
                attempt.method = ApplyMethod.MANUAL
                attempt.error = "iCIMS has complex multi-step forms. Application materials saved — apply manually."
                success = False

            else:
                # Try generic form filler
                attempt.method = ApplyMethod.FORM_FILL
                success = apply_generic(
                    page, job_url, info, resume_path, cover_letter_text, dry_run=dry_run)

            attempt.success = success

            # ── Form Validation Step ──────────────────────────────
            # After filling (live or dry-run), validate the form
            if attempt.method == ApplyMethod.FORM_FILL and platform not in ("linkedin", "icims"):
                try:
                    print(f"    [VALIDATE] Running form validation...")
                    validation = validate_filled_form(
                        page=page,
                        applicant_info=info,
                        resume_text=resume_text,
                        cover_letter_text=cover_letter_text,
                        job_url=job_url,
                        company=company,
                        title=title,
                        platform=platform,
                    )

                    # Run LLM visual check if not dry_run
                    if not dry_run:
                        validation = llm_visual_verify(page, info, validation)

                    print_validation_report(validation)
                    report_path = save_validation_report(validation)
                    attempt.validation_report_path = report_path

                    if not validation.overall_pass and not dry_run:
                        print(f"    [WARNING] Validation FAILED — form may have incorrect data")
                        attempt.error = "Form validation failed: " + "; ".join(
                            f"{c.field_name}: {c.issue}"
                            for c in validation.field_checks if not c.matches
                        )
                except Exception as ve:
                    print(f"    [WARNING] Validation error (non-fatal): {ve}")

            browser.close()

    except Exception as e:
        attempt.success = False
        attempt.error = str(e)
        print(f"    [ERROR] Application error: {e}")

    if attempt.success:
        mode = "DRY RUN" if dry_run else "SUBMITTED"
        print(f"    [OK] [{mode}] {company} — {title}")
    else:
        print(
            f"    [WARNING] Could not auto-apply: {attempt.error or 'form submission unclear'}")
        print(f"    Materials saved at: {resume_path}")

    return attempt
