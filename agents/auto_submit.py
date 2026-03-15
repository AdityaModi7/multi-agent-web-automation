"""Smart Auto-Fill Agent - Platform-specific form fillers for Greenhouse and Workday.

Uses Playwright with slow, human-like interactions to fill ATS forms.
Each platform has its own handler because they use completely different
DOM structures and interaction patterns.

Key insight: Greenhouse uses standard HTML forms but with React-managed state.
You must use page.type() not page.fill() so React registers the change.
Workday uses custom components with data-automation-id attributes and
multi-page flows.
"""

import sys
import time
import json
import re
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Profile, TailoredResume, CoverLetter
from utils.llm import call_llm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if "greenhouse.io" in url_lower:
        return "greenhouse"
    if "myworkdayjobs.com" in url_lower or "wd1." in url_lower or "wd5." in url_lower or "wd3." in url_lower:
        return "workday"
    if "lever.co" in url_lower:
        return "lever"
    if "ashbyhq.com" in url_lower:
        return "ashby"
    return "generic"


def load_preferences() -> dict:
    prefs_path = Path(__file__).parent.parent / "data" / "preferences.json"
    if prefs_path.exists():
        return json.loads(prefs_path.read_text())
    return {}


def slow_type(page, selector, text, delay=50):
    """Type text character by character so React/Angular state picks it up."""
    el = page.query_selector(selector)
    if el:
        el.click()
        el.fill("")
        page.wait_for_timeout(100)
        el.type(text, delay=delay)
        page.wait_for_timeout(200)
        return True
    return False


def slow_type_el(el, text, page, delay=50):
    """Type into an element reference."""
    el.click()
    el.fill("")
    page.wait_for_timeout(100)
    el.type(text, delay=delay)
    page.wait_for_timeout(200)


def answer_question_llm(question: str, profile: Profile,
                        job_title: str = "", company: str = "",
                        options: list[str] = None) -> str:
    opt_text = ""
    if options:
        opt_text = f"\nAvailable options (pick one EXACTLY): {json.dumps(options)}"
    prompt = f"""You are filling a job application for {job_title} at {company}.
Answer concisely. If yes/no, just say Yes or No. If a number, just the number.
For "How did you hear", say "Company Website".
{opt_text}
Return ONLY the answer, nothing else."""
    return call_llm(
        system_prompt=prompt,
        user_message=f"Question: {question}\nCandidate: {profile.name}",
        max_tokens=200,
    ).strip().strip('"').strip("'")


# ---------------------------------------------------------------------------
# GREENHOUSE
# ---------------------------------------------------------------------------

class GreenhouseHandler:
    """Fills Greenhouse application forms (job-boards.greenhouse.io and boards.greenhouse.io).

    Greenhouse forms are standard HTML with React managing state.
    Key: use type() not fill() so React onChange fires.
    Form structure from StubHub example:
        - input#first_name, input#last_name, input#email, input#phone
        - input[type=file] for resume (inside a container)
        - textarea or input for LinkedIn, GitHub
        - select elements for custom questions (sponsorship, hybrid, experience)
        - input[type=checkbox] for affirmations
        - Voluntary self-ID section (skip these)
    """

    def __init__(self, page, profile: Profile, prefs: dict):
        self.page = page
        self.profile = profile
        self.prefs = prefs
        parts = profile.name.split()
        self.first = parts[0] if parts else ""
        self.last = parts[-1] if len(parts) > 1 else ""

    def fill_all(self, resume_path=None, job_title="", company="") -> int:
        filled = 0
        p = self.page

        # Wait for form to render
        p.wait_for_timeout(2000)

        # Scroll to form
        p.evaluate("document.querySelector('#application, form, #application-form')?.scrollIntoView({behavior:'smooth'})")
        p.wait_for_timeout(1000)

        # === TEXT FIELDS ===
        # Greenhouse uses id-based or name-based selectors
        text_fields = [
            ("input#first_name", self.first),
            ("input#last_name", self.last),
            ("input#email", self.profile.email),
            ("input#phone", self.profile.phone or ""),
            # Also try name-based
            ("input[name='first_name']", self.first),
            ("input[name='last_name']", self.last),
            ("input[name='email']", self.profile.email),
            ("input[name='phone']", self.profile.phone or ""),
        ]

        filled_ids = set()
        for selector, value in text_fields:
            if not value:
                continue
            try:
                el = p.query_selector(selector)
                if el:
                    el_id = el.get_attribute("id") or el.get_attribute("name") or selector
                    if el_id in filled_ids:
                        continue
                    current = el.evaluate("e => e.value") or ""
                    if current.strip():
                        filled_ids.add(el_id)
                        continue
                    slow_type_el(el, value, p)
                    filled_ids.add(el_id)
                    filled += 1
                    print(f"   Filled: {selector} = {value[:30]}")
            except Exception:
                continue

        # === LINKEDIN / GITHUB / WEBSITE (label-based) ===
        url_fields = {
            "linkedin": self.profile.linkedin or "",
            "github": self.profile.github or "",
            "portfolio": self.profile.portfolio or "",
            "website": self.profile.portfolio or self.profile.github or "",
        }
        labels = p.query_selector_all("label")
        for label_el in labels:
            try:
                lt = (label_el.inner_text() or "").lower()
                for key, value in url_fields.items():
                    if key in lt and value:
                        # Find the input for this label
                        for_attr = label_el.get_attribute("for")
                        inp = None
                        if for_attr:
                            inp = p.query_selector(f"#{for_attr}")
                        if not inp:
                            # Try finding input in same parent container
                            inp = label_el.evaluate_handle(
                                """el => {
                                    let container = el.closest('.field') || el.parentElement;
                                    return container ? container.querySelector('input, textarea') : null;
                                }"""
                            ).as_element()
                        if inp:
                            current = inp.evaluate("e => e.value") or ""
                            if not current.strip():
                                slow_type_el(inp, value, p)
                                filled += 1
                                print(f"   Filled: {key} = {value[:40]}")
                        break
            except Exception:
                continue

        # === RESUME UPLOAD ===
        if resume_path and Path(resume_path).exists():
            abs_path = str(Path(resume_path).resolve())
            try:
                # Greenhouse has input[type=file] (sometimes hidden)
                file_inputs = p.query_selector_all("input[type='file']")
                for fi in file_inputs:
                    accept = (fi.get_attribute("accept") or "").lower()
                    name = (fi.get_attribute("name") or "").lower()
                    # Resume upload usually accepts pdf/doc or has "resume" in name
                    if any(x in accept for x in [".pdf", ".doc", "application/"]) or "resume" in name or not accept:
                        fi.set_input_files(abs_path)
                        filled += 1
                        print(f"   Uploaded resume: {Path(resume_path).name}")
                        p.wait_for_timeout(2000)
                        break
            except Exception as e:
                print(f"   Resume upload failed: {e}")

        # === SELECT DROPDOWNS ===
        filled += self._fill_selects(job_title, company)

        # === CHECKBOXES (affirmations) ===
        filled += self._fill_checkboxes()

        return filled

    def _fill_selects(self, job_title: str, company: str) -> int:
        filled = 0
        p = self.page
        prefs = self.prefs.get("application_defaults", {})

        selects = p.query_selector_all("select")
        for sel in selects:
            try:
                # Skip if already has a non-default selection
                current_val = sel.evaluate("e => e.value") or ""
                if current_val and current_val != "":
                    continue

                # Get label
                sel_id = sel.get_attribute("id") or ""
                label = ""
                if sel_id:
                    lbl_el = p.query_selector(f"label[for='{sel_id}']")
                    if lbl_el:
                        label = lbl_el.inner_text().strip()
                if not label:
                    label = sel.evaluate("""e => {
                        let c = e.closest('.field, .form-group, .question');
                        let l = c && c.querySelector('label, .label');
                        return l ? l.innerText.trim() : '';
                    }""")

                label_lower = label.lower()

                # Skip voluntary self-identification
                if any(x in label_lower for x in ["gender", "race", "veteran", "disability",
                                                     "ethnicity", "hispanic", "latino"]):
                    continue

                # Get options
                options = sel.evaluate("""e => Array.from(e.options).map(o => ({
                    value: o.value, text: o.text.trim(), idx: o.index
                })).filter(o => o.value && o.text && o.text !== 'Select...' && o.text !== '--')""")

                if not options:
                    continue

                option_texts = [o["text"] for o in options]
                answer = None

                # Smart matching
                if "sponsorship" in label_lower or "visa" in label_lower:
                    need = prefs.get("needs_sponsorship", False)
                    answer = "Yes" if need else "No"
                elif "authorized" in label_lower or "legally" in label_lower:
                    answer = "Yes"
                elif "hybrid" in label_lower or "in-office" in label_lower or "on-site" in label_lower or "willing" in label_lower:
                    answer = "Yes"
                elif "relocat" in label_lower:
                    answer = "Yes"
                elif "experience" in label_lower and "year" in label_lower:
                    # Pick the lowest option that matches
                    for o in options:
                        if any(x in o["text"].lower() for x in ["0", "1", "less", "<"]):
                            answer = o["text"]
                            break
                    if not answer and options:
                        answer = options[0]["text"]
                elif "hear" in label_lower or "source" in label_lower or "find" in label_lower:
                    answer = "Company Website"
                elif "country" in label_lower:
                    answer = "United States"

                # Fallback to LLM
                if not answer:
                    answer = answer_question_llm(label, self.profile, job_title, company, option_texts)

                # Select the option
                if answer:
                    best = self._match_option(answer, options)
                    if best:
                        sel.select_option(value=best["value"])
                        filled += 1
                        print(f"   Selected: {label[:40]} = {best['text']}")
                        p.wait_for_timeout(300)

            except Exception:
                continue

        return filled

    def _match_option(self, answer: str, options: list[dict]) -> dict | None:
        a = answer.lower().strip()
        # Exact
        for o in options:
            if o["text"].lower().strip() == a:
                return o
        # Contains
        for o in options:
            if a in o["text"].lower() or o["text"].lower() in a:
                return o
        # Word overlap
        for o in options:
            a_words = set(a.split())
            o_words = set(o["text"].lower().split())
            if len(a_words & o_words) >= 1:
                return o
        return options[0] if options else None

    def _fill_checkboxes(self) -> int:
        filled = 0
        p = self.page
        checkboxes = p.query_selector_all("input[type='checkbox']")
        for cb in checkboxes:
            try:
                # Get associated label text
                cb_id = cb.get_attribute("id") or ""
                label = ""
                if cb_id:
                    lbl = p.query_selector(f"label[for='{cb_id}']")
                    if lbl:
                        label = lbl.inner_text().strip().lower()
                if not label:
                    label = cb.evaluate("""e => {
                        let l = e.closest('label');
                        return l ? l.innerText.trim() : '';
                    }""").lower()

                # Only check affirmation boxes, skip EEOC/demographic
                if any(x in label for x in ["i affirm", "i agree", "i certify",
                                              "accurate", "truthful", "acknowledge"]):
                    if not cb.is_checked():
                        cb.click()
                        filled += 1
                        print(f"   Checked: affirmation checkbox")
                        self.page.wait_for_timeout(200)
            except Exception:
                continue
        return filled


# ---------------------------------------------------------------------------
# WORKDAY
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WORKDAY
# ---------------------------------------------------------------------------

class WorkdayHandler:
    """Fills Workday application forms (*.myworkdayjobs.com).

    Workday is highly customized per company. Instead of trying to automate
    everything, this handler:
    1. Clicks Apply
    2. Handles sign-in (pauses for user)
    3. On EACH page: fills what it can, then PAUSES for you to fix/verify
    4. You press ENTER to advance to the next page
    5. Repeats until you're on the review page
    """

    def __init__(self, page, profile: Profile, prefs: dict):
        self.page = page
        self.profile = profile
        self.prefs = prefs
        parts = profile.name.split()
        self.first = parts[0] if parts else ""
        self.last = parts[-1] if len(parts) > 1 else ""

    def fill_all(self, resume_path=None, job_title="", company="") -> int:
        filled = 0
        p = self.page

        # Step 1: Click Apply
        try:
            apply_btn = p.locator("a:has-text('Apply'), button:has-text('Apply')").first
            if apply_btn.is_visible(timeout=3000):
                apply_btn.click()
                p.wait_for_timeout(3000)
                print("   Clicked Apply button")
        except Exception:
            pass

        # Step 2: Handle "Apply Manually" etc
        for text in ["Apply Manually", "Use My Last Application", "Autofill with Resume"]:
            try:
                btn = p.locator(f"button:has-text('{text}'), a:has-text('{text}')").first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    p.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        # Step 3: Sign-in check
        try:
            sign_in = p.locator("button:has-text('Sign In'), a:has-text('Sign In')").first
            if sign_in.is_visible(timeout=2000):
                print("\n   Workday requires sign-in.")
                print("   Please create an account or sign in manually.")
                input("   Press ENTER when you're on the application form...")
                p.wait_for_timeout(2000)
        except Exception:
            pass

        # Step 4: Page-by-page with pause
        for step in range(10):
            print(f"\n   {'='*50}")
            print(f"   PAGE {step + 1}")
            print(f"   {'='*50}")

            page_filled = self._fill_current_page(resume_path, job_title, company)
            filled += page_filled

            # Check if review/submit page
            try:
                review = p.locator("button:has-text('Submit'), h2:has-text('Review')").first
                if review.is_visible(timeout=1000):
                    print("\n   Reached review/submit page")
                    break
            except Exception:
                pass

            print(f"\n   Filled {page_filled} fields on this page.")
            print(f"   Review the form and fix anything wrong.")
            response = input("   Press ENTER to go to next page (or 'done' to stop): ").strip().lower()
            if response == "done":
                break

            # Click Next
            advanced = False
            for btn_text in ["Next", "Continue", "Save and Continue"]:
                try:
                    btn = p.locator(f"button:has-text('{btn_text}')").first
                    if btn.is_visible(timeout=1000):
                        btn.click()
                        p.wait_for_timeout(3000)
                        advanced = True
                        break
                except Exception:
                    continue

            if not advanced:
                try:
                    btn = p.locator("button[data-automation-id='bottom-navigation-next-button']").first
                    if btn.is_visible(timeout=1000):
                        btn.click()
                        p.wait_for_timeout(3000)
                        advanced = True
                except Exception:
                    pass

            if not advanced:
                print("   Couldn't find Next button. Navigate manually.")
                input("   Press ENTER when you're on the next page...")

        return filled

    def _fill_current_page(self, resume_path, job_title, company) -> int:
        filled = 0
        p = self.page
        p.wait_for_timeout(1500)

        # Resume upload first (Workday sometimes auto-fills from resume)
        if resume_path and Path(resume_path).exists():
            try:
                file_input = p.locator("input[type='file']").first
                if file_input.count() > 0:
                    file_input.set_input_files(str(Path(resume_path).resolve()))
                    filled += 1
                    print(f"   Uploaded: {Path(resume_path).name}")
                    p.wait_for_timeout(5000)
            except Exception:
                pass

        # Text fields by data-automation-id
        da_fields = {
            "legalNameSection_firstName": self.first,
            "legalNameSection_lastName": self.last,
        }
        for auto_id, value in da_fields.items():
            if not value:
                continue
            try:
                inp = p.locator(f"input[data-automation-id='{auto_id}']").first
                if inp.is_visible(timeout=500):
                    current = inp.input_value()
                    if not current.strip():
                        inp.click()
                        inp.fill("")
                        inp.type(value, delay=30)
                        filled += 1
                        print(f"   {auto_id}: {value}")
            except Exception:
                continue

        # Fill by visible labels
        filled += self._fill_inputs_by_label()

        # Dropdowns
        filled += self._fill_wd_dropdowns(job_title, company)

        return filled

    def _fill_inputs_by_label(self) -> int:
        filled = 0
        p = self.page

        label_map = {
            "first name": self.first,
            "given name": self.first,
            "last name": self.last,
            "family name": self.last,
            "email": self.profile.email,
            "phone": self.profile.phone or "",
            "mobile": self.profile.phone or "",
            "linkedin": self.profile.linkedin or "",
            "github": self.profile.github or "",
        }

        try:
            labels = p.locator("label:visible").all()
        except Exception:
            return 0

        for lbl in labels:
            try:
                label_text = lbl.inner_text().strip().lower()
                for key, value in label_map.items():
                    if key in label_text and value:
                        for_attr = lbl.get_attribute("for")
                        inp = None
                        if for_attr:
                            try:
                                candidate = p.locator(f"#{for_attr}").first
                                if candidate.is_visible(timeout=300):
                                    inp = candidate
                            except Exception:
                                pass
                        if not inp:
                            try:
                                parent = lbl.locator("..").first
                                candidate = parent.locator("input, textarea").first
                                if candidate.is_visible(timeout=300):
                                    inp = candidate
                            except Exception:
                                pass
                        if inp:
                            current = inp.input_value()
                            if not current.strip():
                                inp.click()
                                inp.fill("")
                                inp.type(value, delay=30)
                                filled += 1
                                print(f"   {label_text[:30]}: {value[:30]}")
                        break
            except Exception:
                continue
        return filled

    def _fill_wd_dropdowns(self, job_title, company) -> int:
        filled = 0
        p = self.page
        prefs = self.prefs.get("application_defaults", {})

        # Standard <select> elements
        try:
            selects = p.locator("select:visible").all()
        except Exception:
            selects = []

        for sel in selects:
            try:
                current = sel.input_value()
                if current:
                    continue

                sel_id = sel.get_attribute("id") or ""
                label = ""
                if sel_id:
                    try:
                        lbl = p.locator(f"label[for='{sel_id}']").first
                        label = lbl.inner_text().strip()
                    except Exception:
                        pass
                if not label:
                    continue
                ll = label.lower()

                if any(x in ll for x in ["gender", "race", "veteran", "disability", "ethnicity"]):
                    continue

                options = sel.evaluate("""e => Array.from(e.options)
                    .map(o => ({value: o.value, text: o.text.trim()}))
                    .filter(o => o.value && o.text)""")

                answer = self._smart_answer(ll)
                if answer:
                    for o in options:
                        if answer.lower() in o["text"].lower() or o["text"].lower() in answer.lower():
                            sel.select_option(value=o["value"])
                            filled += 1
                            print(f"   {label[:30]}: {o['text']}")
                            break
            except Exception:
                continue

        # Workday custom dropdowns
        try:
            dropdown_btns = p.locator(
                "button[data-automation-id*='dropdown'], "
                "button[data-automation-id*='searchButton']"
            ).all()
        except Exception:
            dropdown_btns = []

        for btn in dropdown_btns[:5]:
            try:
                if not btn.is_visible(timeout=300):
                    continue
                label = btn.evaluate("""e => {
                    let walk = e.parentElement;
                    for (let i = 0; i < 5 && walk; i++) {
                        let l = walk.querySelector('label');
                        if (l && l.innerText.trim().length > 2) return l.innerText.trim();
                        walk = walk.parentElement;
                    }
                    return '';
                }""")
                if not label:
                    continue
                ll = label.lower()
                if any(x in ll for x in ["gender", "race", "veteran", "disability"]):
                    continue
                answer = self._smart_answer(ll)
                if not answer:
                    continue
                btn.click()
                p.wait_for_timeout(800)
                try:
                    option = p.locator(f"div[role='option']:has-text('{answer}')").first
                    if option.is_visible(timeout=1000):
                        option.click()
                        filled += 1
                        print(f"   {label[:30]}: {answer}")
                    else:
                        p.keyboard.press("Escape")
                except Exception:
                    p.keyboard.press("Escape")
                p.wait_for_timeout(300)
            except Exception:
                continue

        return filled

    def _smart_answer(self, label_lower: str) -> str | None:
        prefs = self.prefs.get("application_defaults", {})
        ll = label_lower
        if "sponsorship" in ll or "visa" in ll:
            return "No" if not prefs.get("needs_sponsorship", False) else "Yes"
        if "authorized" in ll or "legally" in ll or "eligible" in ll:
            return "Yes"
        if "hybrid" in ll or "on-site" in ll or "willing" in ll or "relocat" in ll:
            return "Yes"
        if "country" in ll:
            return "United States"
        if "state" in ll and "province" not in ll:
            return "New York"
        if "source" in ll or "hear" in ll or "find" in ll:
            return "Company Website"
        if "experience" in ll and "year" in ll:
            return "1"
        return None

class GenericHandler:
    def __init__(self, page, profile: Profile, prefs: dict):
        self.page = page
        self.profile = profile
        self.prefs = prefs
        parts = profile.name.split()
        self.first = parts[0] if parts else ""
        self.last = parts[-1] if len(parts) > 1 else ""

    def fill_all(self, resume_path=None, job_title="", company="") -> int:
        filled = 0
        p = self.page

        attempts = [
            (self.first, ["first_name", "first-name", "fname", "firstName"]),
            (self.last, ["last_name", "last-name", "lname", "lastName"]),
            (self.profile.name, ["name", "full_name", "fullName"]),
            (self.profile.email, ["email", "email_address"]),
            (self.profile.phone or "", ["phone", "phone_number", "mobile"]),
            (self.profile.linkedin or "", ["linkedin"]),
            (self.profile.github or "", ["github"]),
        ]

        for value, names in attempts:
            if not value:
                continue
            for name in names:
                try:
                    inp = p.query_selector(
                        f"input[name*='{name}' i], input[id*='{name}' i], "
                        f"input[placeholder*='{name}' i]"
                    )
                    if inp:
                        current = inp.evaluate("e => e.value") or ""
                        if not current.strip():
                            slow_type_el(inp, value, p)
                            filled += 1
                            break
                except Exception:
                    continue

        if resume_path and Path(resume_path).exists():
            try:
                fi = p.query_selector("input[type='file']")
                if fi:
                    fi.set_input_files(str(Path(resume_path).resolve()))
                    filled += 1
            except Exception:
                pass

        return filled


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class SmartSubmitter:
    def __init__(self, headless=False):
        self.headless = headless
        self.browser = None
        self.page = None

    def start_browser(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        )
        self.page = ctx.new_page()

    def close_browser(self):
        if self.browser:
            self.browser.close()
        if hasattr(self, "_pw"):
            self._pw.stop()

    def auto_fill(self, url, profile, resume=None, cover_letter=None,
                  resume_file_path=None, job_title="", company=""):
        try:
            self.start_browser()
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_timeout(3000)

            platform = detect_platform(url)
            prefs = load_preferences()
            print(f"   Platform: {platform}")
            print(f"   URL: {url}")

            # Pick the right handler
            if platform == "greenhouse":
                handler = GreenhouseHandler(self.page, profile, prefs)
            elif platform == "workday":
                handler = WorkdayHandler(self.page, profile, prefs)
            else:
                handler = GenericHandler(self.page, profile, prefs)

            # Determine resume path - prefer tailored PDF
            rpath = resume_file_path
            if not rpath and resume and resume.resume_text:
                # Generate a temp PDF from the tailored resume
                try:
                    from utils.pdf_generator import markdown_to_pdf
                    rpath = "/tmp/tailored_resume.pdf"
                    markdown_to_pdf(resume.resume_text, rpath)
                except Exception:
                    pass

            filled = handler.fill_all(
                resume_path=rpath,
                job_title=job_title,
                company=company,
            )

            print(f"\n   Filled {filled} fields total")
            print(f"\n   {'='*55}")
            print(f"   REVIEW the form in the browser window.")
            print(f"   Fix anything that looks wrong, then submit manually.")
            print(f"   {'='*55}")
            input("\n   Press ENTER when done (browser will close)...")
            return True

        except Exception as e:
            print(f"   Auto-fill error: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            self.close_browser()


def run_auto_fill(url, profile, resume=None, cover_letter=None,
                  resume_file_path=None, job_title="", company="",
                  headless=False, auto_submit=False):
    s = SmartSubmitter(headless=headless)
    return s.auto_fill(url, profile, resume, cover_letter,
                       resume_file_path, job_title, company)