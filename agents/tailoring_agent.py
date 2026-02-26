"""Tailoring Agent — The core intelligence that customizes applications."""

from models import (
    Profile, JobPosting, FitAnalysis, TailoredResume, CoverLetter
)
from utils.llm import call_llm_json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fit Analysis ──────────────────────────────────────────────────────────

FIT_ANALYSIS_PROMPT = """You are an expert career advisor and recruiter. Analyze how well this candidate fits a specific job.

Think like a hiring manager reviewing this application. Be honest and specific.

Return ONLY valid JSON (no markdown, no backticks):
{
 "overall_score": 0-100,
 "strong_matches": [
 {"requirement": "what the job needs", "match_level": "strong", "evidence": "specific experience from resume"}
 ],
 "partial_matches": [
 {"requirement": "what the job needs", "match_level": "partial", "evidence": "related but not exact experience"}
 ],
 "gaps": [
 {"requirement": "what the job needs", "match_level": "gap", "evidence": "what the candidate lacks and how to address it"}
 ],
 "recommendation": "Should apply" or "Maybe" or "Skip",
 "reasoning": "2-3 sentence explanation of the overall fit"
}

Scoring guide:
- 80-100: Strong match, should definitely apply
- 60-79: Good match with some gaps, worth applying
- 40-59: Partial match, consider if the company/role is a strong interest
- 0-39: Weak match, likely not worth applying"""


def analyze_fit(profile: Profile, job: JobPosting) -> FitAnalysis:
    """Analyze how well a profile matches a job posting."""
    data = call_llm_json(
        system_prompt=FIT_ANALYSIS_PROMPT,
        user_message=f"""## Candidate Profile
{profile.model_dump_json(indent=2)}

## Job Posting
{job.model_dump_json(indent=2)}

Analyze the fit between this candidate and job.""",
        max_tokens=2000,
    )
    return FitAnalysis(**data)


# ── Resume Tailoring ──────────────────────────────────────────────────────

RESUME_TAILOR_PROMPT = """You are an expert resume writer. Given a candidate's profile and a target job posting, create a tailored resume.

CRITICAL RULES:
1. NEVER remove jobs, internships, or projects — include ALL experience from the original resume
2. NEVER remove technical skills — keep every programming language, framework, and tool
3. REORDER skills to put the most relevant ones first, but keep all of them
4. REWRITE bullet points to emphasize relevant experience using the job's language
5. ADD metrics and impact wherever possible
6. MIRROR keywords from the job posting naturally (for ATS optimization)
7. Keep it honest — never fabricate experience, only reframe existing experience
8. Skills section must contain ONLY technical/hard skills (languages, frameworks, tools) — never soft skills like "collaborative" or "team player"
9. Include a PROJECTS section if the candidate has projects — these demonstrate initiative

The resume should feel like it was written BY this person FOR this specific job, while keeping the full breadth of their experience.

Return ONLY valid JSON (no markdown, no backticks):
{
 "summary": "2-3 sentence tailored professional summary using job-relevant keywords",
 "experience_highlights": {
 "Company Name": [
 "Tailored bullet point emphasizing relevant impact",
 "Another tailored bullet point"
 ]
 },
 "skills_section": ["most relevant skill first", "second most relevant", "..."],
 "resume_text": "The complete formatted resume text ready to submit. Include ALL sections: name, contact info, summary, ALL experience entries, ALL projects, skills (technical only), education. Never omit any role or project."
}"""


def tailor_resume(profile: Profile, job: JobPosting, fit: FitAnalysis) -> TailoredResume:
    """Generate a tailored resume for a specific job posting."""
    data = call_llm_json(
        system_prompt=RESUME_TAILOR_PROMPT,
        user_message=f"""## Candidate Profile
{profile.model_dump_json(indent=2)}

## Target Job
{job.model_dump_json(indent=2)}

## Fit Analysis (use this to know what to emphasize)
{fit.model_dump_json(indent=2)}

Create a tailored resume that maximizes this candidate's chances.""",
        max_tokens=3000,
    )
    return TailoredResume(**data)


# ── Cover Letter Generation ──────────────────────────────────────────────

COVER_LETTER_PROMPT = """You are an expert cover letter writer. Write a compelling, personalized cover letter.

Key principles:
1. OPENING: Hook them — mention something specific about the company (mission, recent news, product) that genuinely excites you. Never start with "I am writing to apply for..."
2. BODY: Don't repeat the resume. Instead, tell 1-2 SHORT stories that demonstrate your most relevant impact. Use the STAR method implicitly.
3. CLOSING: Express genuine enthusiasm and include a forward-looking statement.
4. TONE: Professional but human. Sound like a real person, not a template.
5. LENGTH: 250-350 words. Hiring managers skim — every sentence must earn its place.
6. MIRROR the job posting's language naturally.

Return ONLY valid JSON (no markdown, no backticks):
{
 "greeting": "Dear [Hiring Manager / specific name if known],",
 "opening": "Opening paragraph (2-3 sentences)",
 "body": "Body paragraphs (evidence of fit, 2-3 short paragraphs)",
 "closing": "Closing paragraph with call to action (2-3 sentences)",
 "full_text": "The complete cover letter including greeting and sign-off"
}"""


def generate_cover_letter(
    profile: Profile, job: JobPosting, fit: FitAnalysis
) -> CoverLetter:
    """Generate a tailored cover letter."""
    data = call_llm_json(
        system_prompt=COVER_LETTER_PROMPT,
        user_message=f"""## Candidate Profile
{profile.model_dump_json(indent=2)}

## Target Job
{job.model_dump_json(indent=2)}

## Fit Analysis
{fit.model_dump_json(indent=2)}

Write a compelling cover letter. Remember:
- Company: {job.company}
- Role: {job.title}
- Focus on the strong matches and address gaps proactively where possible.""",
        max_tokens=2000,
    )
    return CoverLetter(**data)


# ── Full Pipeline ─────────────────────────────────────────────────────────

def run_tailoring_pipeline(
    profile: Profile,
    job: JobPosting,
    skip_if_below: int = 40,
) -> dict:
    """Run the complete tailoring pipeline."""
    print(f"\n Analyzing fit for: {job.title} at {job.company}...")
    fit = analyze_fit(profile, job)
    print(f" Score: {fit.overall_score}/100 — {fit.recommendation}")
    print(f" {len(fit.strong_matches)} strong | {len(fit.partial_matches)} partial | {len(fit.gaps)} gaps")

    result = {"fit_analysis": fit,
              "tailored_resume": None, "cover_letter": None}

    if fit.overall_score < skip_if_below:
        print(
            f"\n[WARNING] Fit score ({fit.overall_score}) below threshold ({skip_if_below}). Skipping.")
        print(f" Reason: {fit.reasoning}")
        return result

    print(f"\n Tailoring resume...")
    resume = tailor_resume(profile, job, fit)
    result["tailored_resume"] = resume
    print(
        f" [OK] Resume tailored — {len(resume.skills_section)} skills highlighted")

    print(f"\n Writing cover letter...")
    cover = generate_cover_letter(profile, job, fit)
    result["cover_letter"] = cover
    print(
        f" [OK] Cover letter generated — {len(cover.full_text.split())} words")

    return result
