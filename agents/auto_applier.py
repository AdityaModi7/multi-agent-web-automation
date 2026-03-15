"""Auto Applier — Smart Playwright-based form filler for job applications.

Handles multiple ATS platforms with React-aware typing, LLM-powered
question answering, dropdown/checkbox handling, and PDF resume uploads.

Platforms supported:
- Greenhouse (React forms with id-based selectors)
- Lever (standard HTML forms)
- Ashby (label-based dynamic forms)
- Workday (multi-step wizard with data-automation-id)
- SmartRecruiters (standard forms)
- Jobvite (standard forms)
- LinkedIn (opens browser for manual Easy Apply)
- iCIMS (attempts auto-fill, falls back to manual)
- Generic (LLM-analyzed forms)

Requires: playwright install chromium
"""

import sys
import os
import re
import time
import json
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.llm import call_llm_json, call_llm
from models import ApplyAttempt, ApplyMethod, Profile
from agents.form_validator import (
    validate_filled_form, llm_visual_verify,
    save_validation_report, print_validation_report,
)

logger = logging.getLogger("auto_applier")

# ── Persistent Browser Session ─────────────────────────────────────────

SESSION_DIR = Path(__file__).parent.parent / "data" / "browser_session"

PLATFORMS_NEEDING_LOGIN = {"linkedin", "workday", "icims", "smartrecruiters"}


def get_browser_context(playwright, platform: str, headless: bool = True):
    """Create a browser context with persistent session storage.

    On first run for a login-required platform, launches visible browser
    and waits for the user to log in. Saves cookies for future runs.
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    cookie_file = SESSION_DIR / f"{platform}_cookies.json"

    needs_login = platform in PLATFORMS_NEEDING_LOGIN
    has_saved_session = cookie_file.exists()

    # If login-required and no saved session, launch visible browser for login
    if needs_login and not has_saved_session:
        launch_headless = False
        print(f"    [LOGIN] First time using {platform}. Browser will open for login.")
    else:
        launch_headless = headless

    browser = playwright.chromium.launch(
        headless=launch_headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    )

    # Load saved cookies if available
    if has_saved_session:
        try:
            cookies = json.loads(cookie_file.read_text())
            context.add_cookies(cookies)
            logger.info(f"Loaded saved session for {platform} ({len(cookies)} cookies)")
        except Exception as e:
            logger.warning(f"Failed to load cookies for {platform}: {e}")

    return browser, context


def save_browser_session(context, platform: str):
    """Save browser cookies for reuse in future runs."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    cookie_file = SESSION_DIR / f"{platform}_cookies.json"
    try:
        cookies = context.cookies()
        cookie_file.write_text(json.dumps(cookies, indent=2))
        logger.info(f"Saved {len(cookies)} cookies for {platform}")
    except Exception as e:
        logger.warning(f"Failed to save cookies for {platform}: {e}")


def prompt_for_login(page, platform: str):
    """Wait for user to log in on a visible browser window."""
    print(f"\n    ╔══════════════════════════════════════════════════╗")
    print(f"    ║  LOGIN REQUIRED: {platform.upper():<32} ║")
    print(f"    ║  Please log in using the browser window.        ║")
    print(f"    ║  Press ENTER here when you're logged in.        ║")
    print(f"    ╚══════════════════════════════════════════════════╝\n")
    input("    Waiting for login... Press ENTER when done: ")
    page.wait_for_timeout(2000)


# ── Preferences & Profile Loading ──────────────────────────────────────


def load_preferences() -> dict:
    """Load application preferences (sponsorship, work auth, etc.)."""
    prefs_path = Path(__file__).parent.parent / "data" / "preferences.json"
    if prefs_path.exists():
        return json.loads(prefs_path.read_text())
    return {"application_defaults": {}, "eeoc_skip": True, "auto_check_affirmations": True}


def load_applicant_info() -> dict:
    """Load applicant info from the cached profile."""
    profile_path = Path(__file__).parent.parent / "data" / "profile.json"
    if profile_path.exists():
        return json.loads(profile_path.read_text())
    return {}


def load_profile_model() -> "Profile":
    """Load a Profile model for LLM-powered question answering."""
    from agents.profile_loader import load_cached_profile
    return load_cached_profile()


# ── Platform Detection ─────────────────────────────────────────────────


def detect_platform(url: str) -> str:
    """Detect which ATS platform a job URL belongs to."""
    url_lower = url.lower()
    platform_map = [
        (["greenhouse.io", "boards.greenhouse"], "greenhouse"),
        (["lever.co", "jobs.lever"], "lever"),
        (["linkedin.com"], "linkedin"),
        (["ashbyhq.com"], "ashby"),
        (["myworkdayjobs.com", "workday.com", "wd1.", "wd5.", "wd3."], "workday"),
        (["smartrecruiters.com"], "smartrecruiters"),
        (["icims.com"], "icims"),
        (["jobvite.com"], "jobvite"),
    ]
    for patterns, platform in platform_map:
        if any(p in url_lower for p in patterns):
            return platform
    return "generic"


# ── Human-Like Typing ──────────────────────────────────────────────────


def slow_type(el, text, page, delay=40):
    """Type text character-by-character so React/Angular state picks it up."""
    try:
        el.click()
        el.fill("")
        page.wait_for_timeout(100)
        el.type(text, delay=delay)
        page.wait_for_timeout(200)
        return True
    except Exception:
        return False


def safe_fill(el, text, page, react_aware=True):
    """Fill a field, using slow_type for React-managed forms."""
    if react_aware:
        return slow_type(el, text, page)
    try:
        el.fill(text)
        page.wait_for_timeout(200)
        return True
    except Exception:
        return False


# ── LLM Question Answering ─────────────────────────────────────────────


def answer_question_llm(question: str, profile: "Profile",
                        job_title: str = "", company: str = "",
                        options: list[str] = None) -> str:
    """Use LLM to answer a custom application question based on the profile."""
    opt_text = ""
    if options:
        opt_text = f"\nAvailable options (pick one EXACTLY as written): {json.dumps(options)}"
    prompt = f"""You are filling a job application for {job_title} at {company}.
Answer the question concisely and professionally. Rules:
- If yes/no, just say Yes or No
- If a number, just the number
- If "How did you hear about us", say "Company Website"
- For salary expectations, say "Open to discussion"
- For start date, say "2 weeks notice"
- For years of experience, use the candidate's actual experience
- Never make up information not in the candidate profile
{opt_text}
Return ONLY the answer text, nothing else."""

    profile_summary = f"Name: {profile.name}, Skills: {', '.join(profile.skills[:15])}"
    if profile.experience:
        exp = profile.experience[0]
        profile_summary += f", Current role: {exp.title} at {exp.company}"

    return call_llm(
        system_prompt=prompt,
        user_message=f"Question: {question}\nCandidate: {profile_summary}",
        max_tokens=200,
    ).strip().strip('"').strip("'")


# ── Smart Option Matching ──────────────────────────────────────────────


def match_option(answer: str, options: list[dict]) -> dict | None:
    """Match an answer string to the closest option in a dropdown."""
    a = answer.lower().strip()
    # Exact match
    for o in options:
        if o["text"].lower().strip() == a:
            return o
    # Contains match
    for o in options:
        if a in o["text"].lower() or o["text"].lower() in a:
            return o
    # Word overlap
    a_words = set(a.split())
    for o in options:
        o_words = set(o["text"].lower().split())
        if len(a_words & o_words) >= 1:
            return o
    # Default to first non-empty option
    return options[0] if options else None


# ── Resume File Management ─────────────────────────────────────────────


def get_resume_path(resume_text: str, company: str, title: str) -> str:
    """Save tailored resume and generate PDF. Returns path to best format."""
    output_dir = Path(__file__).parent.parent / "output" / "auto_apply"
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r'[^\w\-]', '_', f"{company}_{title}").lower()

    # Save markdown version
    md_path = output_dir / f"resume_{safe_name}.md"
    md_path.write_text(resume_text)

    # Try to generate PDF (most ATS systems prefer PDF)
    try:
        from utils.pdf_generator import markdown_to_pdf
        pdf_path = str(output_dir / f"resume_{safe_name}.pdf")
        result_path = markdown_to_pdf(resume_text, pdf_path)
        if Path(result_path).exists():
            logger.info(f"PDF resume generated: {result_path}")
            return result_path
    except Exception as e:
        logger.warning(f"PDF generation failed, using markdown: {e}")

    return str(md_path)


# ── Smart Answer for Common Questions ──────────────────────────────────


def smart_answer(label_lower: str, prefs: dict) -> str | None:
    """Answer common application questions using preferences."""
    defaults = prefs.get("application_defaults", {})
    ll = label_lower

    if "sponsorship" in ll or "visa" in ll:
        return "No" if not defaults.get("needs_sponsorship", False) else "Yes"
    if "authorized" in ll or "legally" in ll or "eligible" in ll:
        return "Yes" if defaults.get("work_authorized", True) else "No"
    if "hybrid" in ll or "on-site" in ll or "onsite" in ll or "in-office" in ll:
        return "Yes" if defaults.get("willing_hybrid", True) else "No"
    if "relocat" in ll:
        return "Yes" if defaults.get("willing_to_relocate", True) else "No"
    if "country" in ll:
        return defaults.get("country", "United States")
    if "state" in ll and "province" not in ll:
        return defaults.get("state", "New York")
    if "source" in ll or "hear" in ll or "find" in ll or "how did" in ll:
        return defaults.get("how_did_you_hear", "Company Website")
    if "experience" in ll and "year" in ll:
        return defaults.get("years_of_experience", "1")
    if "salary" in ll or "compensation" in ll:
        val = defaults.get("desired_salary", "")
        return val if val else "Open to discussion"
    if "start" in ll and "date" in ll:
        return defaults.get("start_date", "Immediately")
    if "18" in ll or "age" in ll:
        return "Yes"
    if "felony" in ll or "convicted" in ll or "criminal" in ll:
        return "No"
    return None


# ── Dropdown Filler ────────────────────────────────────────────────────


def fill_selects(page, prefs: dict, profile: "Profile",
                 job_title: str = "", company: str = "") -> int:
    """Fill all select/dropdown elements on the page."""
    filled = 0
    selects = page.query_selector_all("select:visible")

    for sel in selects:
        try:
            current_val = sel.evaluate("e => e.value") or ""
            if current_val and current_val != "":
                continue

            sel_id = sel.get_attribute("id") or ""
            label = ""
            if sel_id:
                lbl_el = page.query_selector(f"label[for='{sel_id}']")
                if lbl_el:
                    label = lbl_el.inner_text().strip()
            if not label:
                label = sel.evaluate("""e => {
                    let c = e.closest('.field, .form-group, .question, .form-field');
                    let l = c && c.querySelector('label, .label');
                    return l ? l.innerText.trim() : '';
                }""")

            label_lower = label.lower()

            # Skip EEOC / demographics
            if prefs.get("eeoc_skip", True):
                if any(x in label_lower for x in [
                    "gender", "race", "veteran", "disability",
                    "ethnicity", "hispanic", "latino", "sex",
                    "demographic", "voluntary", "self-id"
                ]):
                    continue

            # Get options
            options = sel.evaluate("""e => Array.from(e.options).map(o => ({
                value: o.value, text: o.text.trim(), idx: o.index
            })).filter(o => o.value && o.text && o.text !== 'Select...' && o.text !== '--' && o.text !== 'Please select')""")

            if not options:
                continue

            option_texts = [o["text"] for o in options]
            answer = smart_answer(label_lower, prefs)

            # Fallback to LLM
            if not answer and profile:
                try:
                    answer = answer_question_llm(label, profile, job_title, company, option_texts)
                except Exception:
                    answer = None

            if answer:
                best = match_option(answer, options)
                if best:
                    sel.select_option(value=best["value"])
                    filled += 1
                    logger.info(f"Selected: {label[:40]} = {best['text']}")
                    page.wait_for_timeout(300)

        except Exception as e:
            logger.debug(f"Select fill error: {e}")
            continue

    return filled


# ── Checkbox Filler ────────────────────────────────────────────────────


def fill_checkboxes(page, prefs: dict) -> int:
    """Check affirmation/agreement checkboxes, skip EEOC."""
    if not prefs.get("auto_check_affirmations", True):
        return 0

    filled = 0
    checkboxes = page.query_selector_all("input[type='checkbox']:visible")

    for cb in checkboxes:
        try:
            cb_id = cb.get_attribute("id") or ""
            label = ""
            if cb_id:
                lbl = page.query_selector(f"label[for='{cb_id}']")
                if lbl:
                    label = lbl.inner_text().strip().lower()
            if not label:
                label = cb.evaluate("""e => {
                    let l = e.closest('label');
                    return l ? l.innerText.trim() : '';
                }""").lower()

            # Skip EEOC
            if any(x in label for x in [
                "gender", "race", "veteran", "disability",
                "ethnicity", "hispanic", "demographic", "voluntary"
            ]):
                continue

            # Check affirmation boxes
            if any(x in label for x in [
                "i affirm", "i agree", "i certify", "i acknowledge",
                "accurate", "truthful", "terms", "consent",
                "privacy", "i confirm", "i understand", "i accept",
            ]):
                if not cb.is_checked():
                    cb.click()
                    filled += 1
                    logger.info("Checked: affirmation checkbox")
                    page.wait_for_timeout(200)
        except Exception:
            continue

    return filled


# ── Radio Button Filler ────────────────────────────────────────────────


def fill_radio_buttons(page, prefs: dict, profile: "Profile",
                       job_title: str = "", company: str = "") -> int:
    """Fill radio button groups with smart answers."""
    filled = 0
    # Find radio button groups by name
    radios = page.query_selector_all("input[type='radio']:visible")
    groups_seen = set()

    for radio in radios:
        try:
            name = radio.get_attribute("name") or ""
            if name in groups_seen:
                continue
            groups_seen.add(name)

            # Get the question text (label of the group)
            group_label = radio.evaluate("""e => {
                let container = e.closest('.field, .form-group, .question, .form-field, fieldset');
                if (container) {
                    let label = container.querySelector('legend, label, .label, h3, h4, p');
                    if (label) return label.innerText.trim();
                }
                return '';
            }""")

            if not group_label:
                continue

            label_lower = group_label.lower()

            # Skip EEOC
            if prefs.get("eeoc_skip", True):
                if any(x in label_lower for x in [
                    "gender", "race", "veteran", "disability",
                    "ethnicity", "hispanic", "sex", "demographic"
                ]):
                    continue

            # Get all options for this group
            group_radios = page.query_selector_all(f"input[type='radio'][name='{name}']:visible")
            options = []
            for r in group_radios:
                r_id = r.get_attribute("id") or ""
                option_label = ""
                if r_id:
                    lbl = page.query_selector(f"label[for='{r_id}']")
                    if lbl:
                        option_label = lbl.inner_text().strip()
                if not option_label:
                    option_label = r.get_attribute("value") or ""
                options.append({"element": r, "text": option_label, "value": r.get_attribute("value") or ""})

            option_texts = [o["text"] for o in options if o["text"]]
            answer = smart_answer(label_lower, prefs)

            if not answer and profile and option_texts:
                try:
                    answer = answer_question_llm(group_label, profile, job_title, company, option_texts)
                except Exception:
                    continue

            if answer:
                a = answer.lower().strip()
                for o in options:
                    if o["text"].lower().strip() == a or a in o["text"].lower():
                        o["element"].click()
                        filled += 1
                        logger.info(f"Radio: {group_label[:40]} = {o['text']}")
                        page.wait_for_timeout(300)
                        break

        except Exception:
            continue

    return filled


# ── Custom Textarea Question Filler ────────────────────────────────────


def fill_custom_textareas(page, prefs: dict, profile: "Profile",
                          cover_letter: str, job_title: str = "",
                          company: str = "") -> int:
    """Fill custom textarea questions (not cover letter) with LLM answers."""
    filled = 0
    textareas = page.query_selector_all("textarea:visible")

    for ta in textareas:
        try:
            current = ta.input_value() or ""
            if current.strip():
                continue

            ta_id = ta.get_attribute("id") or ""
            name = (ta.get_attribute("name") or "").lower()
            placeholder = (ta.get_attribute("placeholder") or "").lower()

            label = ""
            if ta_id:
                lbl = page.query_selector(f"label[for='{ta_id}']")
                if lbl:
                    label = lbl.inner_text().strip()
            if not label:
                label = ta.evaluate("""e => {
                    let c = e.closest('.field, .form-group, .question, .form-field');
                    let l = c && c.querySelector('label, .label');
                    return l ? l.innerText.trim() : '';
                }""")

            combined = f"{label} {name} {placeholder}".lower()

            # Cover letter field
            if any(x in combined for x in ["cover letter", "cover_letter", "coverletter"]):
                if cover_letter:
                    safe_fill(ta, cover_letter, page, react_aware=False)
                    filled += 1
                    logger.info("Filled: cover letter textarea")
                continue

            # Skip if label is empty or too short
            if len(label) < 5:
                continue

            # Use LLM to answer custom questions
            if profile:
                try:
                    answer = answer_question_llm(label, profile, job_title, company)
                    if answer and len(answer) > 2:
                        safe_fill(ta, answer, page, react_aware=False)
                        filled += 1
                        logger.info(f"Filled textarea: {label[:40]}")
                except Exception:
                    pass

        except Exception:
            continue

    return filled


# ── Screenshot Helper ──────────────────────────────────────────────────


def take_screenshot(page, platform: str, prefix: str = "") -> str:
    """Take a screenshot and return the path."""
    screenshot_dir = Path(__file__).parent.parent / "output" / "auto_apply" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = f"{prefix}_{platform}" if prefix else platform
    screenshot_path = str(screenshot_dir / f"{label}_{timestamp}.png")
    page.screenshot(path=screenshot_path, full_page=True)
    return screenshot_path


# ── URL Field Filler ───────────────────────────────────────────────────


def fill_url_fields(page, info: dict) -> int:
    """Fill LinkedIn, GitHub, Portfolio URL fields by scanning labels."""
    filled = 0
    url_fields = {
        "linkedin": info.get("linkedin", ""),
        "github": info.get("github", ""),
        "portfolio": info.get("portfolio", ""),
        "website": info.get("portfolio", "") or info.get("github", ""),
    }

    labels = page.query_selector_all("label:visible")
    for label_el in labels:
        try:
            lt = (label_el.inner_text() or "").lower()
            for key, value in url_fields.items():
                if key in lt and value:
                    for_attr = label_el.get_attribute("for")
                    inp = None
                    if for_attr:
                        inp = page.query_selector(f"#{for_attr}")
                    if not inp:
                        inp = label_el.evaluate_handle(
                            """el => {
                                let container = el.closest('.field, .form-group, .form-field') || el.parentElement;
                                return container ? container.querySelector('input, textarea') : null;
                            }"""
                        ).as_element()
                    if inp:
                        current = inp.evaluate("e => e.value") or ""
                        if not current.strip():
                            slow_type(inp, value, page)
                            filled += 1
                            logger.info(f"Filled URL: {key} = {value[:40]}")
                    break
        except Exception:
            continue

    return filled


# ════════════════════════════════════════════════════════════════════════
# PLATFORM-SPECIFIC HANDLERS
# ════════════════════════════════════════════════════════════════════════


def apply_greenhouse(page, url: str, info: dict, resume_path: str,
                     cover_letter: str, prefs: dict, profile: "Profile",
                     submit: bool = True) -> bool:
    """Fill and optionally submit a Greenhouse application form."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    # Click Apply button if present
    apply_btn = page.query_selector(
        'a[href*="#app"], button:has-text("Apply"), a:has-text("Apply")')
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(2000)

    name_parts = info.get("name", "").split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    filled = 0

    # Standard Greenhouse text fields (React-aware typing)
    text_fields = [
        ('#first_name', first_name),
        ('#last_name', last_name),
        ('#email', info.get("email", "")),
        ('#phone', info.get("phone", "")),
        ('input[name*="first_name"]', first_name),
        ('input[name*="last_name"]', last_name),
        ('input[name*="email"]', info.get("email", "")),
        ('input[name*="phone"]', info.get("phone", "")),
    ]

    filled_ids = set()
    for selector, value in text_fields:
        if not value:
            continue
        el = page.query_selector(selector)
        if el:
            el_id = el.get_attribute("id") or el.get_attribute("name") or selector
            if el_id in filled_ids:
                continue
            current = el.evaluate("e => e.value") or ""
            if current.strip():
                filled_ids.add(el_id)
                continue
            if slow_type(el, value, page):
                filled_ids.add(el_id)
                filled += 1

    # URL fields (LinkedIn, GitHub, etc.)
    filled += fill_url_fields(page, info)

    # Resume upload
    resume_input = page.query_selector(
        'input[type="file"][name*="resume"], input[type="file"][id*="resume"]')
    if not resume_input:
        resume_input = page.query_selector('input[type="file"]')
    if resume_input and resume_path:
        resume_input.set_input_files(resume_path)
        filled += 1
        page.wait_for_timeout(1500)

    # Cover letter
    cover_el = page.query_selector(
        'textarea[name*="cover_letter"], textarea[id*="cover_letter"], '
        'textarea[placeholder*="cover letter" i]')
    if cover_el and cover_letter:
        cover_el.fill(cover_letter)
        filled += 1

    # Location field
    location_el = page.query_selector(
        'input[name*="location"], input[placeholder*="location" i]')
    if location_el:
        current = location_el.evaluate("e => e.value") or ""
        if not current.strip():
            slow_type(location_el, info.get("location", "New York, NY"), page)

    # Dropdowns (sponsorship, work auth, etc.)
    filled += fill_selects(page, prefs, profile)

    # Checkboxes (affirmations)
    filled += fill_checkboxes(page, prefs)

    # Radio buttons
    filled += fill_radio_buttons(page, prefs, profile)

    # Custom textarea questions
    filled += fill_custom_textareas(page, prefs, profile, cover_letter)

    logger.info(f"Greenhouse: filled {filled} fields")

    # Screenshot
    take_screenshot(page, "greenhouse", "filled")

    if not submit:
        return True

    # Submit
    submit_btn = page.query_selector(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Submit"), button:has-text("Apply")')
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(4000)
        success_text = page.content().lower()
        if any(s in success_text for s in [
            "thank you", "application received", "successfully submitted",
            "application submitted", "we have received"
        ]):
            return True

    return False


def apply_lever(page, url: str, info: dict, resume_path: str,
                cover_letter: str, prefs: dict, profile: "Profile",
                submit: bool = True) -> bool:
    """Fill and optionally submit a Lever application form."""
    apply_url = url if "/apply" in url else url.rstrip("/") + "/apply"
    page.goto(apply_url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    filled = 0
    field_map = [
        ('input[name="name"]', info.get("name", "")),
        ('input[name="email"]', info.get("email", "")),
        ('input[name="phone"]', info.get("phone", "")),
        ('input[name="org"]', info.get("experience", [{}])[0].get("company", "") if info.get("experience") else ""),
        ('input[name="urls[LinkedIn]"]', info.get("linkedin", "")),
        ('input[name="urls[GitHub]"]', info.get("github", "")),
        ('input[name="urls[Portfolio]"]', info.get("portfolio", "")),
    ]

    for selector, value in field_map:
        if value:
            el = page.query_selector(selector)
            if el:
                if slow_type(el, value, page):
                    filled += 1

    # Resume upload
    resume_input = page.query_selector('input[type="file"][name="resume"]')
    if not resume_input:
        resume_input = page.query_selector('input[type="file"]')
    if resume_input and resume_path:
        resume_input.set_input_files(resume_path)
        filled += 1
        page.wait_for_timeout(1000)

    # Cover letter
    cover_el = page.query_selector('textarea[name="comments"]')
    if not cover_el:
        cover_el = page.query_selector('textarea[name*="cover"], textarea[placeholder*="cover" i]')
    if cover_el and cover_letter:
        cover_el.fill(cover_letter)
        filled += 1

    # Dropdowns, checkboxes, radios
    filled += fill_selects(page, prefs, profile)
    filled += fill_checkboxes(page, prefs)
    filled += fill_radio_buttons(page, prefs, profile)
    filled += fill_custom_textareas(page, prefs, profile, cover_letter)

    logger.info(f"Lever: filled {filled} fields")
    take_screenshot(page, "lever", "filled")

    if not submit:
        return True

    submit_btn = page.query_selector(
        'button[type="submit"], button:has-text("Submit"), '
        'a.postings-btn:has-text("Submit")')
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(4000)
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "application received", "submitted"]):
            return True

    return False


def apply_ashby(page, url: str, info: dict, resume_path: str,
                cover_letter: str, prefs: dict, profile: "Profile",
                submit: bool = True) -> bool:
    """Fill and optionally submit an Ashby application form."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    apply_btn = page.query_selector('button:has-text("Apply"), a:has-text("Apply")')
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(2000)

    name_parts = info.get("name", "").split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    filled = 0

    inputs = page.query_selector_all("input:visible, textarea:visible")
    for inp in inputs:
        try:
            placeholder = (inp.get_attribute("placeholder") or "").lower()
            name = (inp.get_attribute("name") or "").lower()
            label_text = ""
            label_id = inp.get_attribute("id")
            if label_id:
                label = page.query_selector(f'label[for="{label_id}"]')
                if label:
                    label_text = label.inner_text().lower()

            combined = f"{placeholder} {name} {label_text}"

            # Skip if already filled
            current = inp.evaluate("e => e.value") or ""
            if current.strip():
                continue

            if "first" in combined and "name" in combined:
                slow_type(inp, first_name, page)
                filled += 1
            elif "last" in combined and "name" in combined:
                slow_type(inp, last_name, page)
                filled += 1
            elif "full name" in combined or combined.strip() == "name":
                slow_type(inp, info.get("name", ""), page)
                filled += 1
            elif "email" in combined:
                slow_type(inp, info.get("email", ""), page)
                filled += 1
            elif "phone" in combined:
                slow_type(inp, info.get("phone", ""), page)
                filled += 1
            elif "linkedin" in combined:
                slow_type(inp, info.get("linkedin", ""), page)
                filled += 1
            elif "github" in combined:
                slow_type(inp, info.get("github", ""), page)
                filled += 1
            elif "cover" in combined and inp.evaluate("e => e.tagName") == "TEXTAREA":
                if cover_letter:
                    inp.fill(cover_letter)
                    filled += 1
        except Exception:
            continue
        page.wait_for_timeout(200)

    # Resume upload
    file_input = page.query_selector('input[type="file"]')
    if file_input and resume_path:
        file_input.set_input_files(resume_path)
        filled += 1
        page.wait_for_timeout(1000)

    # Dropdowns, checkboxes, radios
    filled += fill_selects(page, prefs, profile)
    filled += fill_checkboxes(page, prefs)
    filled += fill_radio_buttons(page, prefs, profile)

    logger.info(f"Ashby: filled {filled} fields")
    take_screenshot(page, "ashby", "filled")

    if not submit:
        return True

    submit_btn = page.query_selector('button[type="submit"], button:has-text("Submit")')
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(4000)
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "submitted", "received"]):
            return True

    return False


def apply_workday(page, url: str, info: dict, resume_path: str,
                  cover_letter: str, prefs: dict, profile: "Profile",
                  submit: bool = True) -> bool:
    """Fill and optionally submit a Workday multi-step application."""
    page.goto(url, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3000)

    # Click Apply
    apply_btn = page.query_selector(
        'a[data-automation-id="jobPostingApplyButton"], '
        'button[data-automation-id="jobPostingApplyButton"], '
        'a:has-text("Apply"), button:has-text("Apply")')
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(3000)

    # Skip account creation
    for skip_text in ["Apply Manually", "Skip", "Manual Apply"]:
        skip_btn = page.query_selector(f'button:has-text("{skip_text}"), a:has-text("{skip_text}")')
        if skip_btn:
            skip_btn.click()
            page.wait_for_timeout(2000)
            break

    name_parts = info.get("name", "").split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    filled = 0

    # Workday-specific fields by data-automation-id
    wd_fields = [
        ('input[data-automation-id="legalNameSection_firstName"]', first_name),
        ('input[data-automation-id="legalNameSection_lastName"]', last_name),
        ('input[data-automation-id="email"]', info.get("email", "")),
        ('input[data-automation-id="phone-number"]', info.get("phone", "")),
        ('input[data-automation-id="addressSection_addressLine1"]', info.get("location", "New York, NY")),
    ]

    for selector, value in wd_fields:
        if value:
            el = page.query_selector(selector)
            if el:
                el.click()
                slow_type(el, value, page)
                filled += 1

    # Also fill by label scanning (Workday variants differ)
    filled += _fill_inputs_by_label(page, info)

    # Resume upload
    file_input = page.query_selector(
        'input[type="file"][data-automation-id*="resume"], '
        'input[type="file"][data-automation-id="file-upload-input-ref"], '
        'input[type="file"]')
    if file_input and resume_path:
        file_input.set_input_files(resume_path)
        filled += 1
        page.wait_for_timeout(2000)

    # Navigate through wizard steps
    for step in range(4):
        next_btn = page.query_selector(
            'button[data-automation-id="bottom-navigation-next-button"], '
            'button:has-text("Next"), button:has-text("Continue"), '
            'button:has-text("Save and Continue")')
        if next_btn:
            next_btn.click()
            page.wait_for_timeout(2500)

            # Fill any new fields on the new page
            _fill_inputs_by_label(page, info)

            # Fill textareas (cover letter etc.)
            for ta in page.query_selector_all("textarea:visible"):
                if not ta.input_value() and cover_letter:
                    ta.fill(cover_letter)
                    filled += 1

            # Fill additional dropdowns/checkboxes
            filled += fill_selects(page, prefs, profile)
            filled += fill_checkboxes(page, prefs)
            filled += fill_radio_buttons(page, prefs, profile)

            # Upload on subsequent pages too
            new_file = page.query_selector('input[type="file"]:visible')
            if new_file and resume_path:
                try:
                    new_file.set_input_files(resume_path)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass

    take_screenshot(page, "workday", "filled")

    if not submit:
        return True

    submit_btn = page.query_selector(
        'button[data-automation-id="bottom-navigation-next-button"]:has-text("Submit"), '
        'button:has-text("Submit Application"), button:has-text("Submit")')
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(5000)
        success_text = page.content().lower()
        if any(s in success_text for s in [
            "thank you", "application submitted", "successfully submitted",
            "application received", "we have received"
        ]):
            return True

    return False


def _fill_inputs_by_label(page, info: dict) -> int:
    """Fill text inputs by scanning visible labels (for Workday and generic forms)."""
    filled = 0
    name_parts = info.get("name", "").split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    label_map = {
        "first name": first_name,
        "given name": first_name,
        "last name": last_name,
        "family name": last_name,
        "email": info.get("email", ""),
        "phone": info.get("phone", ""),
        "mobile": info.get("phone", ""),
        "linkedin": info.get("linkedin", ""),
        "github": info.get("github", ""),
        "city": info.get("location", "New York, NY").split(",")[0].strip() if info.get("location") else "",
    }

    try:
        labels = page.query_selector_all("label:visible")
    except Exception:
        return 0

    for lbl in labels:
        try:
            label_text = (lbl.inner_text() or "").strip().lower()
            for key, value in label_map.items():
                if key in label_text and value:
                    for_attr = lbl.get_attribute("for")
                    inp = None
                    if for_attr:
                        try:
                            inp = page.query_selector(f"#{for_attr}")
                        except Exception:
                            pass
                    if not inp:
                        try:
                            parent_el = lbl.evaluate_handle("el => el.parentElement").as_element()
                            if parent_el:
                                inp = parent_el.query_selector("input, textarea")
                        except Exception:
                            pass
                    if inp:
                        current = inp.evaluate("e => e.value") or ""
                        if not current.strip():
                            slow_type(inp, value, page)
                            filled += 1
                    break
        except Exception:
            continue

    return filled


def apply_smartrecruiters(page, url: str, info: dict, resume_path: str,
                          cover_letter: str, prefs: dict, profile: "Profile",
                          submit: bool = True) -> bool:
    """Fill and optionally submit a SmartRecruiters application form."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    # Click Apply
    apply_btn = page.query_selector(
        'button:has-text("Apply"), a:has-text("Apply Now"), '
        'a:has-text("Apply"), button.js-apply-button')
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(2000)

    filled = 0

    # SmartRecruiters uses standard HTML form patterns
    name_parts = info.get("name", "").split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    sr_fields = [
        ('input[name*="firstName"], input[id*="firstName"]', first_name),
        ('input[name*="lastName"], input[id*="lastName"]', last_name),
        ('input[name*="email"], input[type="email"]', info.get("email", "")),
        ('input[name*="phone"], input[type="tel"]', info.get("phone", "")),
        ('input[name*="linkedin"]', info.get("linkedin", "")),
    ]

    for selector, value in sr_fields:
        if value:
            el = page.query_selector(selector)
            if el:
                current = el.evaluate("e => e.value") or ""
                if not current.strip():
                    slow_type(el, value, page)
                    filled += 1

    # Also try label-based filling
    filled += _fill_inputs_by_label(page, info)

    # Resume upload
    file_input = page.query_selector('input[type="file"]')
    if file_input and resume_path:
        file_input.set_input_files(resume_path)
        filled += 1
        page.wait_for_timeout(1500)

    filled += fill_selects(page, prefs, profile)
    filled += fill_checkboxes(page, prefs)
    filled += fill_radio_buttons(page, prefs, profile)
    filled += fill_custom_textareas(page, prefs, profile, cover_letter)

    logger.info(f"SmartRecruiters: filled {filled} fields")
    take_screenshot(page, "smartrecruiters", "filled")

    if not submit:
        return True

    submit_btn = page.query_selector(
        'button[type="submit"], button:has-text("Submit"), button:has-text("Apply")')
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(4000)
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "submitted", "received", "success"]):
            return True

    return False


def apply_jobvite(page, url: str, info: dict, resume_path: str,
                  cover_letter: str, prefs: dict, profile: "Profile",
                  submit: bool = True) -> bool:
    """Fill and optionally submit a Jobvite application form."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    apply_btn = page.query_selector(
        'a:has-text("Apply"), button:has-text("Apply")')
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(2000)

    filled = _fill_inputs_by_label(page, info)
    filled += fill_url_fields(page, info)

    # Resume upload
    file_input = page.query_selector('input[type="file"]')
    if file_input and resume_path:
        file_input.set_input_files(resume_path)
        filled += 1
        page.wait_for_timeout(1500)

    filled += fill_selects(page, prefs, profile)
    filled += fill_checkboxes(page, prefs)
    filled += fill_radio_buttons(page, prefs, profile)
    filled += fill_custom_textareas(page, prefs, profile, cover_letter)

    logger.info(f"Jobvite: filled {filled} fields")
    take_screenshot(page, "jobvite", "filled")

    if not submit:
        return True

    submit_btn = page.query_selector(
        'button[type="submit"], button:has-text("Submit"), input[type="submit"]')
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(4000)
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "submitted", "received"]):
            return True

    return False


def apply_icims(page, url: str, info: dict, resume_path: str,
                cover_letter: str, prefs: dict, profile: "Profile",
                submit: bool = True) -> bool:
    """Attempt to fill an iCIMS application form.

    iCIMS forms are complex multi-step wizards. We try our best with
    label-based filling and fall back to generic if needed.
    """
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    # iCIMS often has an "Apply Online" link
    apply_btn = page.query_selector(
        'a:has-text("Apply Online"), a:has-text("Apply Now"), '
        'button:has-text("Apply"), a.iCIMS_Apply')
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(3000)

    filled = _fill_inputs_by_label(page, info)
    filled += fill_url_fields(page, info)

    # Resume upload
    file_input = page.query_selector('input[type="file"]')
    if file_input and resume_path:
        file_input.set_input_files(resume_path)
        filled += 1
        page.wait_for_timeout(1500)

    filled += fill_selects(page, prefs, profile)
    filled += fill_checkboxes(page, prefs)
    filled += fill_radio_buttons(page, prefs, profile)
    filled += fill_custom_textareas(page, prefs, profile, cover_letter)

    # Navigate through iCIMS pages
    for _ in range(5):
        next_btn = page.query_selector(
            'button:has-text("Next"), a:has-text("Next"), '
            'button:has-text("Continue"), input[type="submit"][value*="Next"]')
        if next_btn:
            next_btn.click()
            page.wait_for_timeout(2500)
            filled += _fill_inputs_by_label(page, info)
            filled += fill_selects(page, prefs, profile)
            filled += fill_checkboxes(page, prefs)
        else:
            break

    logger.info(f"iCIMS: filled {filled} fields")
    take_screenshot(page, "icims", "filled")

    if not submit:
        return True

    submit_btn = page.query_selector(
        'button[type="submit"]:has-text("Submit"), '
        'input[type="submit"][value*="Submit"], '
        'button:has-text("Submit Application")')
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(4000)
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "submitted", "received", "success"]):
            return True

    return False


def _dismiss_linkedin_popups(page):
    """Dismiss LinkedIn Premium promos and other overlay popups."""
    # Dismiss "Job search smarter with Premium" and similar popups
    dismiss_selectors = [
        'button[aria-label="Dismiss"]',
        'button[aria-label="Close"]',
        'button.artdeco-modal__dismiss',
        'button.msg-overlay-bubble-header__control--new-convo-btn',
        # Premium promo close button (X in top right)
        'section.premium-upsell-link button[aria-label]',
        'div.premium-upsell button.artdeco-card__dismiss',
    ]
    for sel in dismiss_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(500)
        except Exception:
            continue
    # Also try dismissing the messaging overlay if it's open
    try:
        msg_close = page.query_selector('button[data-control-name="overlay.close_conversation_window"]')
        if msg_close and msg_close.is_visible():
            msg_close.click()
            page.wait_for_timeout(300)
    except Exception:
        pass


def apply_linkedin(page, url: str, info: dict, resume_path: str,
                   cover_letter: str, prefs: dict, profile: "Profile",
                   submit: bool = True) -> bool:
    """Fill LinkedIn Easy Apply using a logged-in browser session.

    Flow:
    1. Navigate to the job posting
    2. Click Easy Apply
    3. Fill each step of the multi-modal dialog
    4. Submit (or stop at review if dry run)
    """
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    # Check if we're logged in
    logged_in = page.query_selector('nav[aria-label*="primary"], .global-nav')
    if not logged_in:
        # Check for sign-in wall
        sign_in = page.query_selector('a:has-text("Sign in"), button:has-text("Sign in")')
        if sign_in:
            page.goto("https://www.linkedin.com/login", wait_until="networkidle")
            prompt_for_login(page, "linkedin")
            # Navigate back to the job
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

    # Dismiss any popups/overlays that might block interaction
    _dismiss_linkedin_popups(page)

    # Scroll down slightly to ensure the Easy Apply button area is in view
    page.evaluate("window.scrollTo(0, 200)")
    page.wait_for_timeout(1000)

    # Click Easy Apply button — try multiple strategies with retries
    easy_apply = None
    easy_apply_selectors = [
        'button.jobs-apply-button',
        'button[aria-label*="Easy Apply"]',
        'button:has-text("Easy Apply")',
        'div.jobs-apply-button--top-card button',
        'div.jobs-s-apply button',
        'button.jobs-apply-button--top-card',
    ]
    # Try each selector, with a retry loop for async rendering
    for attempt in range(3):
        for sel in easy_apply_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    easy_apply = el
                    break
            except Exception:
                continue
        if easy_apply:
            break
        # Wait for more JS rendering between attempts
        page.wait_for_timeout(2000)
        _dismiss_linkedin_popups(page)

    if not easy_apply:
        # Last resort: find any button containing "Easy Apply" text via JS
        easy_apply_handle = page.evaluate_handle("""
            () => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.textContent.includes('Easy Apply') && btn.offsetParent !== null) {
                        return btn;
                    }
                }
                return null;
            }
        """)
        if easy_apply_handle:
            easy_apply = easy_apply_handle.as_element()

    if not easy_apply:
        logger.warning("No Easy Apply button found")
        take_screenshot(page, "linkedin", "no_easy_apply")
        return False

    easy_apply.click()
    page.wait_for_timeout(2000)

    filled = 0

    # LinkedIn Easy Apply is a multi-step modal dialog
    for step in range(8):  # Max 8 steps
        page.wait_for_timeout(1500)

        # Fill visible text inputs in the modal
        modal = page.query_selector('div[role="dialog"], .artdeco-modal')
        if not modal:
            break

        inputs = modal.query_selector_all('input[type="text"]:visible, input[type="email"]:visible, '
                                           'input[type="tel"]:visible, input:not([type]):visible')
        for inp in inputs:
            try:
                current = inp.input_value() or ""
                if current.strip():
                    continue

                label_id = inp.get_attribute("id") or ""
                label_text = ""
                if label_id:
                    lbl = page.query_selector(f'label[for="{label_id}"]')
                    if lbl:
                        label_text = lbl.inner_text().strip().lower()

                aria = (inp.get_attribute("aria-label") or "").lower()
                combined = f"{label_text} {aria}"

                name_parts = info.get("name", "").split()
                first_name = name_parts[0] if name_parts else ""
                last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

                if "first" in combined and "name" in combined:
                    slow_type(inp, first_name, page)
                    filled += 1
                elif "last" in combined and "name" in combined:
                    slow_type(inp, last_name, page)
                    filled += 1
                elif "email" in combined:
                    slow_type(inp, info.get("email", ""), page)
                    filled += 1
                elif "phone" in combined or "mobile" in combined:
                    slow_type(inp, info.get("phone", ""), page)
                    filled += 1
                elif "linkedin" in combined:
                    slow_type(inp, info.get("linkedin", ""), page)
                    filled += 1
                elif "github" in combined or "website" in combined:
                    slow_type(inp, info.get("github", "") or info.get("portfolio", ""), page)
                    filled += 1
                elif "city" in combined or "location" in combined:
                    slow_type(inp, info.get("location", "New York, NY"), page)
                    filled += 1
            except Exception:
                continue

        # Fill textareas in the modal (cover letter, additional info)
        textareas = modal.query_selector_all('textarea:visible')
        for ta in textareas:
            try:
                if not ta.input_value().strip():
                    label_id = ta.get_attribute("id") or ""
                    label_text = ""
                    if label_id:
                        lbl = page.query_selector(f'label[for="{label_id}"]')
                        if lbl:
                            label_text = lbl.inner_text().strip().lower()
                    if "cover" in label_text or not label_text:
                        if cover_letter:
                            ta.fill(cover_letter)
                            filled += 1
                    elif profile:
                        try:
                            answer = answer_question_llm(label_text, profile)
                            ta.fill(answer)
                            filled += 1
                        except Exception:
                            pass
            except Exception:
                continue

        # Fill select/dropdowns in modal
        selects = modal.query_selector_all('select:visible')
        for sel in selects:
            try:
                if sel.input_value():
                    continue
                sel_id = sel.get_attribute("id") or ""
                label = ""
                if sel_id:
                    lbl = page.query_selector(f'label[for="{sel_id}"]')
                    if lbl:
                        label = lbl.inner_text().strip()
                if label:
                    answer = smart_answer(label.lower(), prefs)
                    if answer:
                        options = sel.evaluate("""e => Array.from(e.options).map(o => ({
                            value: o.value, text: o.text.trim()
                        })).filter(o => o.value && o.text)""")
                        best = match_option(answer, options)
                        if best:
                            sel.select_option(value=best["value"])
                            filled += 1
            except Exception:
                continue

        # Fill radio buttons in modal
        filled += fill_radio_buttons(modal, prefs, profile)

        # Upload resume if file input appears
        file_input = modal.query_selector('input[type="file"]')
        if file_input and resume_path:
            try:
                file_input.set_input_files(resume_path)
                filled += 1
                page.wait_for_timeout(1500)
            except Exception:
                pass

        take_screenshot(page, "linkedin", f"step_{step}")

        # Check for Submit button (final step)
        submit_btn = modal.query_selector(
            'button[aria-label*="Submit"], button:has-text("Submit application")')
        if submit_btn:
            if submit:
                submit_btn.click()
                page.wait_for_timeout(3000)
                # Check for success
                success_el = page.query_selector(
                    'h2:has-text("submitted"), div:has-text("Application submitted")')
                if success_el:
                    return True
            else:
                logger.info("LinkedIn: at submit step (dry run, not clicking)")
                return True

        # Click Next/Continue to advance
        next_btn = modal.query_selector(
            'button[aria-label*="Continue"], button[aria-label*="Next"], '
            'button:has-text("Next"), button:has-text("Continue")')
        if next_btn:
            next_btn.click()
            page.wait_for_timeout(1500)
        else:
            # Check for Review button
            review_btn = modal.query_selector(
                'button[aria-label*="Review"], button:has-text("Review")')
            if review_btn:
                review_btn.click()
                page.wait_for_timeout(1500)
            else:
                break

    return False


# ── Generic Form Filler (LLM-Assisted) ────────────────────────────────


FORM_ANALYSIS_PROMPT = """You are a web form analyzer. Given the HTML of a job application form, identify the form fields and map them to applicant data.

Return ONLY valid JSON (no markdown, no backticks):
{
  "fields": [
    {
      "selector": "CSS selector to target this field",
      "field_type": "text | email | tel | file | textarea | select | checkbox | radio",
      "label": "What the field asks for",
      "value_key": "Which applicant data to use: name | first_name | last_name | email | phone | linkedin | github | location | cover_letter | resume | company | title | other",
      "value": "Exact value to fill (only for 'other' value_key)"
    }
  ],
  "submit_selector": "CSS selector for the submit button"
}"""


def apply_generic(page, url: str, info: dict, resume_path: str,
                  cover_letter: str, prefs: dict, profile: "Profile",
                  submit: bool = True) -> bool:
    """Use LLM to analyze and fill a generic application form."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    apply_btn = page.query_selector(
        'button:has-text("Apply"), a:has-text("Apply Now"), '
        'a:has-text("Apply for this"), button:has-text("Apply Now")')
    if apply_btn:
        apply_btn.click()
        page.wait_for_timeout(2000)

    filled = 0

    # First try label-based filling (fast, no LLM needed)
    filled += _fill_inputs_by_label(page, info)
    filled += fill_url_fields(page, info)

    # Resume upload
    file_input = page.query_selector('input[type="file"]')
    if file_input and resume_path:
        file_input.set_input_files(resume_path)
        filled += 1
        page.wait_for_timeout(1000)

    # If label-based didn't fill much, use LLM analysis
    if filled < 3:
        form_el = page.query_selector("form")
        if form_el:
            form_html = form_el.inner_html()[:6000]
            try:
                analysis = call_llm_json(
                    system_prompt=FORM_ANALYSIS_PROMPT,
                    user_message=f"Analyze this form:\n\n{form_html}",
                    max_tokens=1500,
                )

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
                }

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
                        filled += 1
                    elif field_type in ("text", "email", "tel", "textarea"):
                        value = value_map.get(value_key, field.get("value", ""))
                        if value:
                            current = el.evaluate("e => e.value") or ""
                            if not current.strip():
                                safe_fill(el, value, page)
                                filled += 1
            except Exception as e:
                logger.warning(f"LLM form analysis failed: {e}")

    # Dropdowns, checkboxes, radios
    filled += fill_selects(page, prefs, profile)
    filled += fill_checkboxes(page, prefs)
    filled += fill_radio_buttons(page, prefs, profile)
    filled += fill_custom_textareas(page, prefs, profile, cover_letter)

    logger.info(f"Generic: filled {filled} fields")
    take_screenshot(page, "generic", "filled")

    if not submit:
        return True

    submit_btn = page.query_selector(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Submit"), button:has-text("Apply")')
    if submit_btn:
        submit_btn.click()
        page.wait_for_timeout(4000)
        success_text = page.content().lower()
        if any(s in success_text for s in ["thank you", "submitted", "received", "success"]):
            return True

    return False


# ════════════════════════════════════════════════════════════════════════
# MAIN AUTO-APPLY FUNCTION
# ════════════════════════════════════════════════════════════════════════


def auto_apply(
    job_url: str,
    company: str,
    title: str,
    resume_text: str,
    cover_letter_text: str,
    dry_run: bool = True,
    max_retries: int = 2,
) -> ApplyAttempt:
    """Attempt to automatically apply to a job with retry logic.

    In dry_run mode, fills the form completely but skips the submit click.
    In live mode, fills and submits.

    Args:
        job_url: URL of the job posting or application page.
        company: Company name.
        title: Job title.
        resume_text: Tailored resume text.
        cover_letter_text: Tailored cover letter text.
        dry_run: If True, fill forms but don't click submit.
        max_retries: Number of retries on transient failures.

    Returns:
        ApplyAttempt with success/failure details.
    """
    platform = detect_platform(job_url)
    info = load_applicant_info()
    prefs = load_preferences()
    profile = load_profile_model()

    # Generate resume file (PDF preferred)
    resume_path = get_resume_path(resume_text, company, title)

    print(f"    Platform: {platform}")
    print(f"    {'DRY RUN — fill only' if dry_run else 'LIVE — will submit'}")
    print(f"    Resume: {Path(resume_path).name}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ApplyAttempt(
            job_url=job_url, company=company, title=title,
            method=ApplyMethod.MANUAL, success=False,
            error="Playwright not installed. Run: pip install playwright && playwright install chromium",
        )

    attempt = ApplyAttempt(
        job_url=job_url, company=company, title=title,
        method=ApplyMethod.FORM_FILL,
    )

    last_error = None

    for retry in range(max_retries + 1):
        if retry > 0:
            wait = 5 * retry
            print(f"    Retry {retry}/{max_retries} (waiting {wait}s)...")
            time.sleep(wait)

        try:
            with sync_playwright() as p:
                # Use persistent session for login-required platforms
                browser, context = get_browser_context(p, platform, headless=True)
                page = context.new_page()
                page.set_default_timeout(15000)

                success = False
                should_submit = not dry_run

                handler_map = {
                    "greenhouse": apply_greenhouse,
                    "lever": apply_lever,
                    "ashby": apply_ashby,
                    "workday": apply_workday,
                    "smartrecruiters": apply_smartrecruiters,
                    "jobvite": apply_jobvite,
                    "icims": apply_icims,
                    "linkedin": apply_linkedin,
                    "generic": apply_generic,
                }

                if platform == "linkedin":
                    attempt.method = ApplyMethod.EASY_APPLY
                elif platform in handler_map:
                    attempt.method = ApplyMethod.FORM_FILL

                if platform in handler_map:
                    handler = handler_map[platform]
                    success = handler(
                        page, job_url, info, resume_path,
                        cover_letter_text, prefs, profile,
                        submit=should_submit,
                    )
                    if dry_run and platform != "linkedin":
                        success = True  # Dry run always succeeds if no crash

                attempt.success = success

                # Save session cookies after successful interaction
                if platform in PLATFORMS_NEEDING_LOGIN:
                    save_browser_session(context, platform)

                # Form validation (skip for LinkedIn — modal-based)
                if attempt.method == ApplyMethod.FORM_FILL and platform != "linkedin":
                    try:
                        print(f"    Validating form...")
                        validation = validate_filled_form(
                            page=page, applicant_info=info,
                            resume_text=resume_text,
                            cover_letter_text=cover_letter_text,
                            job_url=job_url, company=company,
                            title=title, platform=platform,
                        )
                        if not dry_run:
                            validation = llm_visual_verify(page, info, validation)

                        print_validation_report(validation)
                        report_path = save_validation_report(validation)
                        attempt.validation_report_path = report_path

                        if not validation.overall_pass and not dry_run:
                            print(f"    [WARNING] Validation FAILED")
                            attempt.error = "Form validation failed: " + "; ".join(
                                f"{c.field_name}: {c.issue}"
                                for c in validation.field_checks if not c.matches
                            )
                    except Exception as ve:
                        logger.warning(f"Validation error (non-fatal): {ve}")

                # Take final screenshot
                attempt.screenshot_path = take_screenshot(page, platform, "final")

                browser.close()

            # If we got here without exception, break the retry loop
            break

        except Exception as e:
            last_error = str(e)
            logger.error(f"Attempt {retry + 1} failed: {e}")
            if retry == max_retries:
                attempt.success = False
                attempt.error = f"Failed after {max_retries + 1} attempts: {last_error}"
                print(f"    [ERROR] {attempt.error}")

    if attempt.success:
        mode = "DRY RUN" if dry_run else "SUBMITTED"
        print(f"    [OK] [{mode}] {company} — {title}")
    else:
        if not attempt.error:
            attempt.error = "Form submission unclear"
        print(f"    [WARNING] {attempt.error}")
        print(f"    Materials saved at: {resume_path}")

    return attempt
