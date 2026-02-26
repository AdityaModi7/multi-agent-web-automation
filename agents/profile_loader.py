"""Profile Loader — Parses your resume/profile into structured data."""

from models import Profile
from utils.llm import call_llm_json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


PROFILE_PARSER_PROMPT = """You are a resume parser. Given resume text, extract structured professional profile data.

Return ONLY valid JSON (no markdown, no backticks) with this schema:
{
 "name": "Full Name",
 "email": "email@example.com",
 "phone": "phone number" or null,
 "location": "City, State" or null,
 "linkedin": "URL" or null,
 "github": "URL" or null,
 "portfolio": "URL" or null,
 "summary": "Professional summary paragraph",
 "experience": [
 {
 "company": "Company Name",
 "title": "Job Title",
 "start_date": "Month Year",
 "end_date": "Month Year or Present",
 "highlights": ["achievement 1 with metrics", "achievement 2"],
 "technologies": ["Python", "AWS", "etc"]
 }
 ],
 "education": [
 {
 "institution": "University Name",
 "degree": "Bachelor's/Master's/etc",
 "field": "Computer Science",
 "graduation_date": "Year",
 "gpa": "3.8" or null
 }
 ],
 "skills": ["skill1", "skill2"],
 "certifications": ["cert1", "cert2"]
}

Be thorough — extract every skill, technology, and achievement mentioned."""


def load_profile(resume_path: str = None, resume_text: str = None) -> Profile:
    """Load and parse a resume into a structured Profile."""
    if not resume_path and not resume_text:
        default = Path(__file__).parent.parent / "data" / "my_resume.md"
        if default.exists():
            resume_path = str(default)
        else:
            raise ValueError(
                "No resume provided. Either pass resume_path, resume_text, "
                "or place your resume at data/my_resume.md"
            )

    if resume_path:
        text = Path(resume_path).read_text()
    else:
        text = resume_text

    data = call_llm_json(
        system_prompt=PROFILE_PARSER_PROMPT,
        user_message=f"Parse this resume:\n\n{text}",
        max_tokens=3000,
    )
    return Profile(**data)


def save_profile(profile: Profile, path: str = None):
    """Save parsed profile to JSON for reuse (avoids re-parsing)."""
    if path is None:
        path = str(Path(__file__).parent.parent / "data" / "profile.json")
    Path(path).write_text(profile.model_dump_json(indent=2))
    print(f"Profile saved to {path}")


def load_cached_profile(path: str = None) -> Profile | None:
    """Load a previously parsed profile from JSON cache."""
    if path is None:
        path = Path(__file__).parent.parent / "data" / "profile.json"
    else:
        path = Path(path)

    if path.exists():
        return Profile.model_validate_json(path.read_text())
    return None
