"""Form Validator — Verifies that the auto-applier correctly filled out application forms.

After the auto-applier fills a form, this agent:
1. Takes a screenshot of the filled form
2. Extracts all visible field values from the page DOM
3. Compares extracted values against expected applicant data
4. Uses LLM to analyze the screenshot for visual verification
5. Returns a detailed validation report with pass/fail per field

This catches issues like:
- Wrong name in fields (e.g., first/last name swapped)
- Missing or truncated email/phone
- Resume not uploaded
- Cover letter field empty when it should be filled
- Wrong information in LinkedIn/GitHub fields
- Dropdown selections not matching expected values
"""

import sys
import json
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from pydantic import BaseModel, Field
from utils.llm import call_llm_json


# ── Validation Models ──────────────────────────────────────────────────

class FieldCheck(BaseModel):
    """Result of checking a single form field."""
    field_name: str
    expected_value: str
    actual_value: str
    matches: bool
    issue: Optional[str] = None


class ValidationReport(BaseModel):
    """Complete validation report for a filled form."""
    job_url: str
    company: str
    title: str
    platform: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    overall_pass: bool = False
    field_checks: list[FieldCheck] = Field(default_factory=list)
    screenshot_path: Optional[str] = None
    llm_visual_check: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# ── DOM-Based Field Extraction ─────────────────────────────────────────

def extract_filled_fields(page) -> dict:
    """Extract all filled field values from the current page DOM.

    Returns a dict mapping field descriptions to their current values.
    """
    fields = {}

    # Extract all visible text inputs
    text_inputs = page.query_selector_all(
        'input[type="text"]:visible, input[type="email"]:visible, '
        'input[type="tel"]:visible, input[type="url"]:visible, '
        'input:not([type]):visible'
    )
    for inp in text_inputs:
        value = inp.input_value()
        if not value:
            continue

        # Build a description of the field
        name = inp.get_attribute("name") or ""
        placeholder = inp.get_attribute("placeholder") or ""
        aria_label = inp.get_attribute("aria-label") or ""
        auto_id = inp.get_attribute("data-automation-id") or ""
        field_id = inp.get_attribute("id") or ""

        # Try to find associated label
        label_text = ""
        if field_id:
            label = page.query_selector(f'label[for="{field_id}"]')
            if label:
                label_text = label.inner_text().strip()

        desc = label_text or placeholder or name or aria_label or auto_id or field_id or "unknown"
        fields[desc.lower()] = value

    # Extract all visible textareas
    textareas = page.query_selector_all('textarea:visible')
    for ta in textareas:
        value = ta.input_value()
        if not value:
            continue
        name = ta.get_attribute("name") or ""
        placeholder = ta.get_attribute("placeholder") or ""
        field_id = ta.get_attribute("id") or ""
        label_text = ""
        if field_id:
            label = page.query_selector(f'label[for="{field_id}"]')
            if label:
                label_text = label.inner_text().strip()
        desc = label_text or placeholder or name or field_id or "textarea"
        fields[desc.lower()] = value

    # Check file inputs for uploads
    file_inputs = page.query_selector_all('input[type="file"]')
    for fi in file_inputs:
        name = fi.get_attribute("name") or fi.get_attribute("id") or "file"
        # Check if the file input has a value set (Playwright sets files via setInputFiles)
        has_files = fi.evaluate("el => el.files && el.files.length > 0")
        if has_files:
            file_name = fi.evaluate("el => el.files[0] ? el.files[0].name : ''")
            fields[f"{name}_uploaded"] = "yes"
            if file_name:
                fields[f"{name}_filename"] = file_name
        else:
            # Also check nearby text for upload confirmation (some forms show filename separately)
            parent = fi.evaluate(
                "el => { let p = el.closest('.field, .upload, .form-group, div'); "
                "return p ? p.innerText : (el.parentElement ? el.parentElement.innerText : ''); }"
            )
            if parent and any(kw in parent.lower() for kw in [
                "uploaded", ".pdf", ".md", ".txt", ".doc", ".docx", "resume",
            ]):
                fields[f"{name}_uploaded"] = "yes"
            else:
                fields[f"{name}_uploaded"] = "unknown"

    # Check select elements
    selects = page.query_selector_all('select:visible')
    for sel in selects:
        value = sel.input_value()
        if value:
            name = sel.get_attribute("name") or sel.get_attribute("id") or "select"
            fields[name.lower()] = value

    return fields


# ── Field Matching Logic ───────────────────────────────────────────────

def normalize(text: str) -> str:
    """Normalize text for comparison."""
    return re.sub(r'\s+', ' ', text.strip().lower())


def fuzzy_match(expected: str, actual: str) -> bool:
    """Check if two values match, allowing for minor formatting differences."""
    if not expected or not actual:
        return False
    e = normalize(expected)
    a = normalize(actual)
    # Exact match
    if e == a:
        return True
    # One contains the other
    if e in a or a in e:
        return True
    # For names, check if all parts are present
    e_parts = set(e.split())
    a_parts = set(a.split())
    if e_parts and e_parts == a_parts:
        return True
    return False


def find_field_value(fields: dict, keywords: list[str]) -> Optional[str]:
    """Find a field value by searching for keyword matches in field descriptions."""
    for desc, value in fields.items():
        if any(kw in desc for kw in keywords):
            return value
    return None


# ── Core Validation ────────────────────────────────────────────────────

def validate_filled_form(
    page,
    applicant_info: dict,
    resume_text: str,
    cover_letter_text: str,
    job_url: str,
    company: str,
    title: str,
    platform: str,
) -> ValidationReport:
    """Validate that a filled form contains the correct applicant information.

    Args:
        page: Playwright page object with the filled form.
        applicant_info: Dict with expected values (name, email, phone, etc.)
        resume_text: The resume text that should have been uploaded/pasted.
        cover_letter_text: The cover letter text that should have been filled.
        job_url: URL of the job posting.
        company: Company name.
        title: Job title.
        platform: ATS platform name.

    Returns:
        ValidationReport with per-field results and overall pass/fail.
    """
    report = ValidationReport(
        job_url=job_url,
        company=company,
        title=title,
        platform=platform,
    )

    # Step 1: Take a validation screenshot
    screenshot_dir = Path(__file__).parent.parent / "output" / "auto_apply" / "validation"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(screenshot_dir / f"validate_{platform}_{ts}.png")
    page.screenshot(path=screenshot_path, full_page=True)
    report.screenshot_path = screenshot_path
    print(f"    [VALIDATE] Screenshot saved: {screenshot_path}")

    # Step 2: Extract all filled field values from the DOM
    fields = extract_filled_fields(page)
    print(f"    [VALIDATE] Extracted {len(fields)} filled fields from DOM")

    # Step 3: Check each expected field
    name = applicant_info.get("name", "")
    name_parts = name.split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    checks = [
        {
            "field_name": "First Name",
            "expected": first_name,
            "keywords": ["first_name", "first name", "firstname", "fname"],
        },
        {
            "field_name": "Last Name",
            "expected": last_name,
            "keywords": ["last_name", "last name", "lastname", "lname"],
        },
        {
            "field_name": "Full Name",
            "expected": name,
            "keywords": ["full name", "name", "your name"],
        },
        {
            "field_name": "Email",
            "expected": applicant_info.get("email", ""),
            "keywords": ["email", "e-mail", "mail"],
        },
        {
            "field_name": "Phone",
            "expected": applicant_info.get("phone", ""),
            "keywords": ["phone", "tel", "mobile", "cell"],
        },
        {
            "field_name": "LinkedIn",
            "expected": applicant_info.get("linkedin", ""),
            "keywords": ["linkedin", "linked in"],
        },
        {
            "field_name": "GitHub",
            "expected": applicant_info.get("github", ""),
            "keywords": ["github", "git hub"],
        },
    ]

    for check in checks:
        expected = check["expected"]
        if not expected:
            continue  # Skip fields we don't have data for

        actual = find_field_value(fields, check["keywords"])

        if actual is None:
            # Field not found — might not exist on this form
            report.warnings.append(
                f"{check['field_name']}: field not found on form (may not be required)"
            )
            continue

        matches = fuzzy_match(expected, actual)
        field_check = FieldCheck(
            field_name=check["field_name"],
            expected_value=expected,
            actual_value=actual,
            matches=matches,
            issue=None if matches else f"Expected '{expected}' but found '{actual}'",
        )
        report.field_checks.append(field_check)

        if matches:
            print(f"    [VALIDATE] {check['field_name']}: [OK]")
        else:
            print(f"    [VALIDATE] {check['field_name']}: [FAIL] expected='{expected}' actual='{actual}'")

    # Step 4: Check resume upload
    resume_field = find_field_value(fields, ["resume", "cv", "file"])
    if resume_field:
        report.field_checks.append(FieldCheck(
            field_name="Resume Upload",
            expected_value="uploaded",
            actual_value=resume_field,
            matches=resume_field.lower() in ("yes", "uploaded"),
            issue=None if resume_field.lower() in ("yes", "uploaded") else "Resume may not have been uploaded",
        ))
    else:
        report.warnings.append("Could not verify resume upload status")

    # Step 5: Check cover letter
    cover_field = find_field_value(fields, ["cover", "letter", "comments", "additional"])
    if cover_letter_text and cover_field:
        # Check if at least the first 50 chars match
        matches = cover_letter_text[:50].lower() in cover_field.lower()
        report.field_checks.append(FieldCheck(
            field_name="Cover Letter",
            expected_value=cover_letter_text[:100] + "...",
            actual_value=cover_field[:100] + "...",
            matches=matches,
            issue=None if matches else "Cover letter content does not match expected text",
        ))

    # Step 6: Determine overall pass/fail
    failed_checks = [c for c in report.field_checks if not c.matches]
    report.overall_pass = len(failed_checks) == 0

    return report


# ── LLM Visual Verification ───────────────────────────────────────────

VISUAL_CHECK_PROMPT = """You are a quality assurance agent reviewing a job application form that was auto-filled.

Given the page content and expected applicant data, verify that the form was filled correctly.

Check for these common issues:
1. Name fields: first/last name swapped, missing parts, wrong casing
2. Email: typos, wrong email address
3. Phone: wrong format, missing digits
4. LinkedIn/GitHub: wrong URLs or missing
5. Resume: was it uploaded? Is there a file name visible?
6. Cover letter: is the textarea filled? Does it look like a real cover letter?
7. Any required fields left empty
8. Any obvious errors in dropdown selections

Return ONLY valid JSON (no markdown, no backticks):
{
  "overall_correct": true or false,
  "issues_found": [
    {"field": "field name", "issue": "description of the problem"}
  ],
  "confidence": "high" or "medium" or "low",
  "summary": "One-sentence summary of the validation result"
}"""


def llm_visual_verify(
    page,
    applicant_info: dict,
    report: ValidationReport,
) -> ValidationReport:
    """Use LLM to analyze the page content and verify the form fill.

    This provides a second layer of verification beyond DOM extraction.
    """
    # Get the visible text content of the form
    form_el = page.query_selector("form")
    if form_el:
        page_text = form_el.inner_text()[:4000]
    else:
        page_text = page.inner_text("body")[:4000]

    try:
        result = call_llm_json(
            system_prompt=VISUAL_CHECK_PROMPT,
            user_message=f"""## Expected Applicant Data
{json.dumps(applicant_info, indent=2)}

## Visible Form Content
{page_text}

Verify the form was filled correctly with the applicant's information.""",
            max_tokens=1000,
        )

        report.llm_visual_check = result.get("summary", "LLM check completed")

        if not result.get("overall_correct", True):
            for issue in result.get("issues_found", []):
                report.errors.append(
                    f"LLM detected issue in '{issue.get('field', 'unknown')}': "
                    f"{issue.get('issue', 'unspecified')}"
                )
            report.overall_pass = False

        confidence = result.get("confidence", "low")
        print(f"    [VALIDATE] LLM visual check: {report.llm_visual_check} (confidence: {confidence})")

    except Exception as e:
        report.warnings.append(f"LLM visual verification failed: {e}")
        print(f"    [VALIDATE] LLM check skipped: {e}")

    return report


# ── Save Validation Report ─────────────────────────────────────────────

def save_validation_report(report: ValidationReport) -> str:
    """Save the validation report to disk and return the path."""
    output_dir = Path(__file__).parent.parent / "output" / "auto_apply" / "validation"
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_company = re.sub(r'[^\w\-]', '_', report.company).lower()
    report_path = output_dir / f"report_{safe_company}_{ts}.json"
    report_path.write_text(report.model_dump_json(indent=2))
    return str(report_path)


def print_validation_report(report: ValidationReport):
    """Print a human-readable validation report."""
    print(f"\n    {'=' * 55}")
    print(f"    FORM VALIDATION REPORT")
    print(f"    {'=' * 55}")
    print(f"    Company:  {report.company}")
    print(f"    Role:     {report.title}")
    print(f"    Platform: {report.platform}")
    print(f"    Result:   {'PASS' if report.overall_pass else 'FAIL'}")
    print(f"    {'=' * 55}")

    if report.field_checks:
        print(f"\n    {'Field':<18} {'Expected':<25} {'Actual':<25} {'OK':<4}")
        print(f"    {'-' * 72}")
        for check in report.field_checks:
            status = "[OK]" if check.matches else "FAIL"
            expected = check.expected_value[:24]
            actual = check.actual_value[:24]
            print(f"    {check.field_name:<18} {expected:<25} {actual:<25} {status:<4}")

    if report.warnings:
        print(f"\n    Warnings:")
        for w in report.warnings:
            print(f"      - {w}")

    if report.errors:
        print(f"\n    Errors:")
        for e in report.errors:
            print(f"      - {e}")

    if report.llm_visual_check:
        print(f"\n    LLM Assessment: {report.llm_visual_check}")

    if report.screenshot_path:
        print(f"\n    Screenshot: {report.screenshot_path}")

    print()
