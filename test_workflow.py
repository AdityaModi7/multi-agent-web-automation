#!/usr/bin/env python3
"""End-to-end test of the Job Application Agent workflow.

Tests the full pipeline: search -> parse -> fit -> tailor -> auto-apply -> track.
Uses real Greenhouse/Lever API data + mock LLM responses (no API key needed).
"""

import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from models import (
    JobPosting, FitAnalysis, SkillMatch, TailoredResume, CoverLetter,
    SearchFilters, JobSearchResult, SearchRun, ApplicationStatus,
    ApplyAttempt, ApplyMethod, WorkflowConfig, Profile,
)
from agents.tracker import save_application, list_applications, print_dashboard, get_stats
from agents.auto_applier import detect_platform, auto_apply, get_resume_path
from agents.job_searcher import (
    is_entry_level_friendly, is_ml_ai_role, meets_salary_floor,
    search_greenhouse, search_lever, deduplicate,
)
from agents.profile_loader import load_cached_profile


def test_models():
    """Test that all Pydantic models validate correctly."""
    print("\n--- TEST 1: Model Validation ---")

    job = JobPosting(
        title="ML Engineer",
        company="TestCo",
        location="New York, NY",
        remote=True,
        salary_range="$130,000 - $180,000",
        description="Build ML systems for production.",
        responsibilities=["Build ML pipelines", "Deploy models"],
        required_skills=["Python", "PyTorch", "SQL"],
        preferred_skills=["Kubernetes", "Spark"],
        required_experience_years=2,
        education_requirement="Bachelor's in CS",
        raw_text="Test posting text",
    )
    assert job.title == "ML Engineer"

    fit = FitAnalysis(
        overall_score=75,
        strong_matches=[
            SkillMatch(requirement="Python", match_level="strong", evidence="3+ years Python experience"),
        ],
        partial_matches=[
            SkillMatch(requirement="PyTorch", match_level="partial", evidence="Used TensorFlow, similar framework"),
        ],
        gaps=[
            SkillMatch(requirement="Kubernetes", match_level="gap", evidence="No K8s experience listed"),
        ],
        recommendation="Should apply",
        reasoning="Good match with strong Python background.",
    )
    assert fit.overall_score == 75

    resume = TailoredResume(
        summary="ML-focused engineer with production experience.",
        experience_highlights={
            "TD Securities": ["Built Text-to-SQL chatbot using LLMs", "Optimized RAG retrieval in ChromaDB"],
        },
        skills_section=["Python", "PyTorch", "SQL", "ChromaDB", "LLMs"],
        resume_text="ADITYA MODI\nML Engineer\n\nSummary: ML-focused engineer...",
    )
    assert len(resume.skills_section) == 5

    cover = CoverLetter(
        greeting="Dear Hiring Manager,",
        opening="I am excited about the ML Engineer role at TestCo.",
        body="My experience building LLM-powered applications at TD Securities...",
        closing="I would welcome the opportunity to discuss how my skills align.",
        full_text="Dear Hiring Manager,\n\nI am excited about the ML Engineer role...",
    )
    assert "Dear" in cover.greeting

    print("[OK] All models validate correctly")
    return job, fit, resume, cover


def test_filters():
    """Test job filtering logic."""
    print("\n--- TEST 2: Filter Logic ---")

    # Entry-level detection
    assert is_entry_level_friendly("Junior ML Engineer", "") is True
    assert is_entry_level_friendly("ML Engineer", "") is True  # No senior signal
    assert is_entry_level_friendly("Senior ML Engineer", "") is False
    assert is_entry_level_friendly("Staff Data Scientist", "") is False
    assert is_entry_level_friendly("ML Engineer (New Grad)", "") is True
    assert is_entry_level_friendly("ML Engineer (1-3 years)", "") is True
    print(" Entry-level filter: [OK]")

    # ML/AI role detection
    assert is_ml_ai_role("Machine Learning Engineer") is True
    assert is_ml_ai_role("ML Engineer") is True
    assert is_ml_ai_role("Data Scientist") is True
    assert is_ml_ai_role("NLP Engineer") is True
    assert is_ml_ai_role("Frontend Developer") is False
    assert is_ml_ai_role("Product Manager") is False
    print(" ML/AI role filter: [OK]")

    # Salary floor
    assert meets_salary_floor("$130,000 - $180,000", 120000) is True
    assert meets_salary_floor("$80,000 - $100,000", 120000) is False
    assert meets_salary_floor("", 120000) is True  # Unknown = keep
    assert meets_salary_floor("$65/hr", 120000) is True  # 65*2080 = 135,200
    print(" Salary floor filter: [OK]")

    # Platform detection
    assert detect_platform("https://boards.greenhouse.io/openai/jobs/123") == "greenhouse"
    assert detect_platform("https://jobs.lever.co/stripe/abc") == "lever"
    assert detect_platform("https://linkedin.com/jobs/view/123") == "linkedin"
    assert detect_platform("https://company.ashbyhq.com/jobs/123") == "ashby"
    assert detect_platform("https://company.myworkdayjobs.com/en-US/jobs/123") == "workday"
    assert detect_platform("https://example.com/careers/ml-engineer") == "generic"
    print(" Platform detection: [OK]")

    print("[OK] All filters pass")


def test_real_search():
    """Test real API search (Greenhouse + Lever, no auth needed)."""
    print("\n--- TEST 3: Real API Search ---")

    # Search Greenhouse boards (real API calls)
    print(" Searching Greenhouse boards (top 5)...")
    gh_results = search_greenhouse(
        board_tokens=["openai", "anthropic", "databricks", "scale", "cohere"],
        max_per_board=5,
    )
    print(f" Found {len(gh_results)} Greenhouse results")
    for r in gh_results[:5]:
        print(f" - {r.title} at {r.company} ({r.location})")

    # Search Lever boards (real API calls)
    print(" Searching Lever boards (top 3)...")
    lever_results = search_lever(
        company_slugs=["openai", "anthropic", "netflix"],
        max_per_company=5,
    )
    print(f" Found {len(lever_results)} Lever results")
    for r in lever_results[:5]:
        print(f" - {r.title} at {r.company} ({r.location})")

    # Combine and deduplicate
    all_results = gh_results + lever_results
    unique = deduplicate(all_results)
    print(f" Combined: {len(all_results)} -> Deduplicated: {len(unique)}")

    # Filter to entry-level friendly
    entry_level = [r for r in unique if is_entry_level_friendly(r.title, r.description_snippet)]
    print(f" Entry-level friendly: {len(entry_level)}")

    print(f"[OK] Search found {len(unique)} total ML/AI roles from real APIs")
    return unique


def test_tracker(job, fit, resume, cover):
    """Test the application tracker (SQLite)."""
    print("\n--- TEST 4: Application Tracker ---")

    # Save an application
    app_id = save_application(
        job=job,
        fit=fit,
        resume_text=resume.resume_text,
        cover_letter_text=cover.full_text,
        status=ApplicationStatus.DRAFT,
        notes="Test application from test_workflow.py",
    )
    print(f" Saved application #{app_id}")

    # List applications
    apps = list_applications(limit=5)
    assert len(apps) > 0
    print(f" Listed {len(apps)} applications")

    # Get stats
    stats = get_stats()
    print(f" Stats: {stats['total_applications']} total, avg score: {stats['average_fit_score']}")

    # Dashboard
    print_dashboard()

    print(f"[OK] Tracker works — application #{app_id} saved and listed")
    return app_id


def test_output_files(job, fit, resume, cover):
    """Test output file generation."""
    print("\n--- TEST 5: Output File Generation ---")

    output_dir = Path("output") / "test_testco_ml_engineer"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save resume
    resume_path = output_dir / "resume.md"
    resume_path.write_text(resume.resume_text)
    print(f" Resume saved: {resume_path}")

    # Save cover letter
    cover_path = output_dir / "cover_letter.md"
    cover_path.write_text(cover.full_text)
    print(f" Cover letter saved: {cover_path}")

    # Save fit analysis
    fit_path = output_dir / "fit_analysis.md"
    fit_summary = f"""# Fit Analysis: {job.title} at {job.company}

**Score: {fit.overall_score}/100 — {fit.recommendation}**

{fit.reasoning}

## Strong Matches
{chr(10).join(f"- **{m.requirement}**: {m.evidence}" for m in fit.strong_matches)}

## Partial Matches
{chr(10).join(f"- **{m.requirement}**: {m.evidence}" for m in fit.partial_matches)}

## Gaps
{chr(10).join(f"- **{m.requirement}**: {m.evidence}" for m in fit.gaps)}
"""
    fit_path.write_text(fit_summary)
    print(f" Fit analysis saved: {fit_path}")

    assert resume_path.exists()
    assert cover_path.exists()
    assert fit_path.exists()
    print("[OK] All output files generated")


def test_auto_apply_dry_run(search_results):
    """Test the auto-applier in dry-run mode against a real job URL."""
    print("\n--- TEST 6: Auto-Apply Dry Run ---")

    # Find a Greenhouse or Lever result to test against
    test_job = None
    for r in search_results:
        if r.source in ("greenhouse", "lever") and r.url:
            test_job = r
            break

    if not test_job:
        print(" [WARNING] No Greenhouse/Lever URL found — skipping auto-apply test")
        print(" (This is expected if those APIs returned no ML/AI roles)")
        return

    print(f" Testing against: {test_job.title} at {test_job.company}")
    print(f" URL: {test_job.url}")
    print(f" Platform: {detect_platform(test_job.url)}")

    # Create a mock tailored resume
    mock_resume = """ADITYA MODI
New York, NY | adityapmodi@gmail.com | 848-213-3048
LinkedIn: linkedin.com/in/aditya-modi | GitHub: github.com/Aditya

SUMMARY
ML-focused software engineer with experience building LLM-powered applications
and data pipelines. MS CS candidate at Georgia Tech, BS CS from Rutgers.

EXPERIENCE
Software Engineering Analyst | TD Securities | July 2025 - Present
- Developed a Text-to-SQL chatbot translating natural language to executable SQL
- Integrated Vanna OSS with custom Dremio client for grounded model responses
- Evaluated LLMs (Mistral & SQLCoder) across prompting strategies
- Optimized RAG retrieval in ChromaDB to improve relevance filtering

SKILLS
Python, SQL, Java, PyTorch, TensorFlow, ChromaDB, AWS, Docker, Linux, Git
"""

    mock_cover = """Dear Hiring Manager,

I am excited about this ML engineering opportunity. My experience building
LLM-powered applications at TD Securities and my ongoing MS in CS at Georgia
Tech have prepared me well for this role.

Best regards,
Aditya Modi
"""

    # Run auto-apply in dry-run mode
    attempt = auto_apply(
        job_url=test_job.url,
        company=test_job.company,
        title=test_job.title,
        resume_text=mock_resume,
        cover_letter_text=mock_cover,
        dry_run=True,
    )

    print(f" Method: {attempt.method.value}")
    print(f" Success: {attempt.success}")
    if attempt.screenshot_path:
        print(f" Screenshot: {attempt.screenshot_path}")
    if attempt.error:
        print(f" Note: {attempt.error}")

    print(f"[OK] Auto-apply dry run completed for {test_job.company}")
    return attempt


def test_profile():
    """Test profile loading."""
    print("\n--- TEST 7: Profile Loading ---")

    profile = load_cached_profile()
    if profile:
        print(f" Profile: {profile.name}")
        print(f" Email: {profile.email}")
        print(f" Skills: {len(profile.skills)} skills")
        print(f" Experience: {len(profile.experience)} roles")
        print(f" Education: {len(profile.education)} degrees")
        print(f"[OK] Cached profile loaded")
    else:
        print(" [WARNING] No cached profile found (would need LLM to parse)")
        print(" Loading raw profile.json instead...")
        profile_path = Path("data/profile.json")
        if profile_path.exists():
            data = json.loads(profile_path.read_text())
            print(f" Name: {data.get('name')}")
            print(f" Skills: {len(data.get('skills', []))}")
            print(f"[OK] Profile data accessible")

    return profile


def main():
    print("=" * 70)
    print(" JOB APPLICATION AGENT — END-TO-END TEST")
    print("=" * 70)
    print(f" Timestamp: {datetime.now().isoformat()}")
    print(f" Note: Uses mock LLM data (no API key required)")
    print("=" * 70)

    passed = 0
    failed = 0

    # Test 1: Models
    try:
        job, fit, resume, cover = test_models()
        passed += 1
    except Exception as e:
        print(f"[ERROR] Model test failed: {e}")
        failed += 1
        return

    # Test 2: Filters
    try:
        test_filters()
        passed += 1
    except Exception as e:
        print(f"[ERROR] Filter test failed: {e}")
        failed += 1

    # Test 3: Real API search
    search_results = []
    try:
        search_results = test_real_search()
        passed += 1
    except Exception as e:
        print(f"[ERROR] Search test failed: {e}")
        failed += 1

    # Test 4: Tracker
    try:
        test_tracker(job, fit, resume, cover)
        passed += 1
    except Exception as e:
        print(f"[ERROR] Tracker test failed: {e}")
        failed += 1

    # Test 5: Output files
    try:
        test_output_files(job, fit, resume, cover)
        passed += 1
    except Exception as e:
        print(f"[ERROR] Output file test failed: {e}")
        failed += 1

    # Test 6: Auto-apply dry run
    try:
        test_auto_apply_dry_run(search_results)
        passed += 1
    except Exception as e:
        print(f"[ERROR] Auto-apply test failed: {e}")
        failed += 1

    # Test 7: Profile
    try:
        test_profile()
        passed += 1
    except Exception as e:
        print(f"[ERROR] Profile test failed: {e}")
        failed += 1

    # Summary
    print("\n" + "=" * 70)
    print(" TEST SUMMARY")
    print("=" * 70)
    print(f" Passed: {passed}/{passed + failed}")
    print(f" Failed: {failed}/{passed + failed}")
    if failed == 0:
        print("\n [OK] All tests passed! Pipeline is ready.")
        print(" To run with real LLM (requires API key):")
        print("   export ANTHROPIC_API_KEY=sk-ant-...")
        print("   python main.py run             # Dry run")
        print("   python main.py run --live       # Submit applications")
    else:
        print(f"\n [WARNING] {failed} test(s) failed. Review errors above.")
    print("=" * 70)


if __name__ == "__main__":
    main()
