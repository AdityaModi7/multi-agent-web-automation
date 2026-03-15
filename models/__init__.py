"""Data models for the Job Application Agent."""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ── Profile / Resume Models ──────────────────────────────────────────────

class Experience(BaseModel):
                company: str
                title: str
                start_date: str
                end_date: str = "Present"
                highlights: list[str] = Field(default_factory=list)
                technologies: list[str] = Field(default_factory=list)


class Education(BaseModel):
                institution: str
                degree: str
                field: str
                graduation_date: str
                gpa: Optional[str] = None


class Profile(BaseModel):
                """Structured representation of the user's professional profile."""
                name: str
                email: str
                phone: Optional[str] = None
                location: Optional[str] = None
                linkedin: Optional[str] = None
                github: Optional[str] = None
                portfolio: Optional[str] = None
                summary: str = ""
                experience: list[Experience] = Field(default_factory=list)
                education: list[Education] = Field(default_factory=list)
                skills: list[str] = Field(default_factory=list)
                certifications: list[str] = Field(default_factory=list)


# ── Job Posting Models ────────────────────────────────────────────────────

class JobPosting(BaseModel):
                """Structured representation of a job posting."""
                title: str
                company: str
                location: Optional[str] = None
                remote: Optional[bool] = None
                salary_range: Optional[str] = None
                description: str
                responsibilities: list[str] = Field(default_factory=list)
                required_skills: list[str] = Field(default_factory=list)
                preferred_skills: list[str] = Field(default_factory=list)
                required_experience_years: Optional[int] = None
                education_requirement: Optional[str] = None
                company_info: Optional[str] = None
                application_url: Optional[str] = None
                raw_text: str = ""


# ── Tailored Output Models ───────────────────────────────────────────────

class SkillMatch(BaseModel):
                """How well a specific skill/requirement matches the candidate."""
                requirement: str
                match_level: str = Field(description="strong | partial | gap")
                evidence: str = Field(
                                description="Specific experience that demonstrates this skill")


class FitAnalysis(BaseModel):
                """Analysis of how well the candidate fits the job."""
                overall_score: int = Field(ge=0, le=100, description="0-100 fit score")
                strong_matches: list[SkillMatch] = Field(default_factory=list)
                partial_matches: list[SkillMatch] = Field(default_factory=list)
                gaps: list[SkillMatch] = Field(default_factory=list)
                recommendation: str = Field(description="Should apply / Maybe / Skip")
                reasoning: str = ""


class TailoredResume(BaseModel):
                """A resume tailored to a specific job posting."""
                summary: str = Field(description="Tailored professional summary")
                experience_highlights: dict[str, list[str]] = Field(
                                default_factory=dict,
                                description="Company -> list of tailored bullet points"
                )
                skills_section: list[str] = Field(
                                description="Skills reordered/filtered for relevance"
                )
                resume_text: str = Field(description="Full formatted resume text")


class CoverLetter(BaseModel):
                """A cover letter tailored to a specific job posting."""
                greeting: str
                opening: str = Field(description="Hook paragraph — why this company/role")
                body: str = Field(description="Evidence paragraphs — matching experience")
                closing: str = Field(description="Call to action and sign-off")
                full_text: str = Field(description="Complete cover letter")


# ── Application Tracking ─────────────────────────────────────────────────

class ApplicationStatus(str, Enum):
                DRAFT = "draft"           # Apply failed or not yet attempted
                SKIPPED = "skipped"       # Below fit threshold or not eligible
                APPLIED = "applied"
                FOLLOWED_UP = "followed_up"
                INTERVIEW = "interview"
                REJECTED = "rejected"
                OFFER = "offer"
                WITHDRAWN = "withdrawn"


class Application(BaseModel):
                """Tracks a single job application."""
                id: Optional[int] = None
                job: JobPosting
                fit_analysis: FitAnalysis
                tailored_resume: Optional[TailoredResume] = None
                cover_letter: Optional[CoverLetter] = None
                status: ApplicationStatus = ApplicationStatus.DRAFT
                applied_date: Optional[datetime] = None
                follow_up_dates: list[datetime] = Field(default_factory=list)
                notes: str = ""
                created_at: datetime = Field(default_factory=datetime.now)


# ── Job Search Models ───────────────────────────────────────────────────

class SearchFilters(BaseModel):
                """Criteria for searching job postings."""
                keywords: list[str] = Field(
                                default_factory=lambda: [
                                                "machine learning engineer",
                                                "ml engineer",
                                                "ai engineer",
                                                "data scientist",
                                                "deep learning engineer",
                                                "nlp engineer",
                                                "computer vision engineer",
                                                "applied scientist",
                                                "research engineer",
                                                "ml ops engineer",
                                ]
                )
                experience_level: str = Field(
                                default="entry",
                                description="entry | mid | senior"
                )
                max_experience_years: int = Field(default=3)
                locations: list[str] = Field(
                                default_factory=lambda: [
                                                "New York, NY",
                                                "San Francisco, CA",
                                                "Seattle, WA",
                                                "Los Angeles, CA",
                                                "Chicago, IL",
                                                "Boston, MA",
                                                "Austin, TX",
                                                "Washington, DC",
                                ],
                                description="Target metro areas — big cities only"
                )
                include_remote: bool = Field(default=True)
                min_salary: int = Field(
                                default=120000, description="Minimum salary floor ($)")
                posted_within_days: int = Field(default=7)
                exclude_companies: list[str] = Field(default_factory=list)


class JobSearchResult(BaseModel):
                """A single result from a job board search."""
                title: str
                company: str
                location: Optional[str] = None
                url: str
                source: str = Field(
                                description="linkedin | indeed | greenhouse | lever | company_site")
                posted_date: Optional[str] = None
                salary_range: Optional[str] = None
                experience_years: Optional[int] = None
                is_new_grad: bool = False
                description_snippet: str = ""


class SearchRun(BaseModel):
                """Results from a single search execution."""
                filters: SearchFilters
                results: list[JobSearchResult] = Field(default_factory=list)
                total_found: int = 0
                sources_searched: list[str] = Field(default_factory=list)
                run_timestamp: datetime = Field(default_factory=datetime.now)


# ── Auto-Apply Models ───────────────────────────────────────────────────

class ApplyMethod(str, Enum):
                """How to submit an application."""
                EASY_APPLY = "easy_apply"
                FORM_FILL = "form_fill"
                EMAIL = "email"
                REDIRECT = "redirect"
                MANUAL = "manual"


class ApplyAttempt(BaseModel):
                """Tracks a single auto-apply attempt."""
                job_url: str
                company: str
                title: str
                method: ApplyMethod
                success: bool = False
                error: Optional[str] = None
                screenshot_path: Optional[str] = None
                validation_report_path: Optional[str] = None
                timestamp: datetime = Field(default_factory=datetime.now)


class WorkflowConfig(BaseModel):
                """Configuration for the full auto-apply workflow."""
                search_filters: SearchFilters = Field(default_factory=SearchFilters)
                min_fit_score: int = Field(
                                default=50, description="Minimum fit score to proceed with application")
                max_applications_per_run: int = Field(default=20)
                dry_run: bool = Field(
                                default=True, description="Generate materials but don't submit")
                resume_path: Optional[str] = None
                delay_between_applies_sec: int = Field(
                                default=30, description="Cooldown between applications")
