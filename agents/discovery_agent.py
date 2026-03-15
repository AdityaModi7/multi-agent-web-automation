"""Job Discovery Agent — Automatically finds jobs matching your profile.

Uses free/freemium job APIs:
- Adzuna (free tier: 250 requests/day)
- RemoteOK (free, no key needed)
- Arbeitnow (free, no key needed)
- HackerNews Who's Hiring (free, scraped)

Set API keys in environment:
  export ADZUNA_APP_ID=your_id
  export ADZUNA_API_KEY=your_key
"""

import sys
import os
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from models import Profile
from utils.llm import call_llm_json


@dataclass
class DiscoveredJob:
    """A job found by the discovery agent."""
    title: str
    company: str
    location: str
    url: str
    source: str
    description: str = ""
    salary: str = ""
    posted_date: str = ""
    tags: list[str] = field(default_factory=list)
    relevance_score: int = 0  # 0-100, set by scoring agent


# ── Job Sources ───────────────────────────────────────────────────────────

def search_remoteok(keywords: list[str], limit: int = 20) -> list[DiscoveredJob]:
    """Search RemoteOK — free, no API key needed. Remote jobs only."""
    print("   🔍 Searching RemoteOK...")
    jobs = []
    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "JobAgent/1.0"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            # First item is metadata, skip it
            for item in data[1:]:
                title = item.get("position", "")
                company = item.get("company", "")
                desc = item.get("description", "")
                tags = item.get("tags", [])

                # Check if any keyword matches title, tags, or description
                text_blob = f"{title} {desc} {' '.join(tags)}".lower()
                if any(kw.lower() in text_blob for kw in keywords):
                    jobs.append(DiscoveredJob(
                        title=title,
                        company=company,
                        location="Remote",
                        url=item.get("url", ""),
                        source="RemoteOK",
                        description=desc[:500],
                        salary=item.get("salary", ""),
                        posted_date=item.get("date", ""),
                        tags=tags,
                    ))
                if len(jobs) >= limit:
                    break
        print(f"   ✅ RemoteOK: found {len(jobs)} matching jobs")
    except Exception as e:
        print(f"   ⚠️  RemoteOK failed: {e}")
    return jobs


def search_adzuna(
    keywords: list[str],
    location: str = "us",
    limit: int = 20,
) -> list[DiscoveredJob]:
    """Search Adzuna — requires free API key (250 req/day)."""
    app_id = os.environ.get("ADZUNA_APP_ID")
    api_key = os.environ.get("ADZUNA_API_KEY")

    if not app_id or not api_key:
        print("   ⏭️  Adzuna: skipped (no API key — set ADZUNA_APP_ID and ADZUNA_API_KEY)")
        return []

    print("   🔍 Searching Adzuna...")
    jobs = []
    query = " ".join(keywords)
    
    try:
        resp = requests.get(
            f"https://api.adzuna.com/v1/api/jobs/{location}/search/1",
            params={
                "app_id": app_id,
                "app_key": api_key,
                "what": query,
                "results_per_page": limit,
                "content-type": "application/json",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("results", []):
                jobs.append(DiscoveredJob(
                    title=item.get("title", ""),
                    company=item.get("company", {}).get("display_name", ""),
                    location=item.get("location", {}).get("display_name", ""),
                    url=item.get("redirect_url", ""),
                    source="Adzuna",
                    description=item.get("description", "")[:500],
                    salary=f"${item.get('salary_min', '')}-${item.get('salary_max', '')}",
                    posted_date=item.get("created", ""),
                ))
        print(f"   ✅ Adzuna: found {len(jobs)} jobs")
    except Exception as e:
        print(f"   ⚠️  Adzuna failed: {e}")
    return jobs


def search_arbeitnow(keywords: list[str], limit: int = 20) -> list[DiscoveredJob]:
    """Search Arbeitnow — free, no API key. Tech-focused jobs."""
    print("   🔍 Searching Arbeitnow...")
    jobs = []
    try:
        resp = requests.get(
            "https://www.arbeitnow.com/api/job-board-api",
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("data", []):
                title = item.get("title", "")
                company = item.get("company_name", "")
                desc = item.get("description", "")
                tags = item.get("tags", [])

                text_blob = f"{title} {desc} {' '.join(tags)}".lower()
                if any(kw.lower() in text_blob for kw in keywords):
                    jobs.append(DiscoveredJob(
                        title=title,
                        company=company,
                        location=item.get("location", ""),
                        url=item.get("url", ""),
                        source="Arbeitnow",
                        description=desc[:500],
                        posted_date=item.get("created_at", ""),
                        tags=tags,
                    ))
                if len(jobs) >= limit:
                    break
        print(f"   ✅ Arbeitnow: found {len(jobs)} matching jobs")
    except Exception as e:
        print(f"   ⚠️  Arbeitnow failed: {e}")
    return jobs


# ── Keyword Extraction ────────────────────────────────────────────────────

def extract_search_keywords(profile: Profile) -> list[str]:
    """Use LLM to extract the best job search keywords from a profile."""
    data = call_llm_json(
        system_prompt="""Given a professional profile, extract the best job search keywords.
Return ONLY valid JSON (no markdown, no backticks):
{
    "job_titles": ["Software Engineer", "ML Engineer", "Data Scientist"],
    "key_skills": ["Python", "machine learning", "NLP"],
    "industries": ["fintech", "AI", "data"]
}
Generate 3-5 job titles that this person would be a good fit for,
5-8 key technical skills, and 2-4 industries.""",
        user_message=f"Extract search keywords for this profile:\n{profile.model_dump_json(indent=2)}",
        max_tokens=500,
    )
    return data


# ── Relevance Scoring ─────────────────────────────────────────────────────

def score_jobs(profile: Profile, jobs: list[DiscoveredJob]) -> list[DiscoveredJob]:
    """Use LLM to score job relevance in batch (efficient — one API call)."""
    if not jobs:
        return jobs

    # Prepare compact job summaries for scoring
    job_summaries = []
    for i, job in enumerate(jobs):
        job_summaries.append({
            "id": i,
            "title": job.title,
            "company": job.company,
            "tags": job.tags[:5],
            "snippet": job.description[:200],
        })

    data = call_llm_json(
        system_prompt="""You are a job-fit scoring engine. Given a candidate profile and a list of jobs, 
score each job 0-100 for relevance. Consider: skill match, experience level match, and career trajectory.

Return ONLY valid JSON (no markdown, no backticks):
{
    "scores": [
        {"id": 0, "score": 85, "reason": "Strong Python/ML match"},
        {"id": 1, "score": 45, "reason": "Needs 10yr experience, candidate has 2"}
    ]
}""",
        user_message=f"""## Candidate
Name: {profile.name}
Skills: {', '.join(profile.skills[:15])}
Experience: {len(profile.experience)} roles, most recent: {profile.experience[0].title if profile.experience else 'N/A'}
Education: {profile.education[0].degree + ' ' + profile.education[0].field if profile.education else 'N/A'}

## Jobs to Score
{json.dumps(job_summaries, indent=2)}""",
        max_tokens=2000,
    )

    # Apply scores
    score_map = {s["id"]: s for s in data.get("scores", [])}
    for i, job in enumerate(jobs):
        if i in score_map:
            job.relevance_score = score_map[i].get("score", 0)

    # Sort by relevance
    jobs.sort(key=lambda j: j.relevance_score, reverse=True)
    return jobs


# ── Main Discovery Pipeline ──────────────────────────────────────────────

def discover_jobs(
    profile: Profile,
    min_score: int = 50,
    max_results: int = 20,
    custom_keywords: list[str] = None,
) -> list[DiscoveredJob]:
    """Run the full job discovery pipeline.
    
    Args:
        profile: Your professional profile
        min_score: Minimum relevance score to include (0-100)
        max_results: Maximum jobs to return
        custom_keywords: Override auto-extracted keywords
    
    Returns:
        List of DiscoveredJob sorted by relevance
    """
    print("\n🔎 JOB DISCOVERY")
    print("=" * 50)

    # Step 1: Extract keywords
    if custom_keywords:
        keywords_data = {"key_skills": custom_keywords, "job_titles": [], "industries": []}
        search_terms = custom_keywords
    else:
        print("\n📝 Extracting search keywords from your profile...")
        keywords_data = extract_search_keywords(profile)
        print(f"   Job titles: {keywords_data.get('job_titles', [])}")
        print(f"   Key skills: {keywords_data.get('key_skills', [])}")
        print(f"   Industries: {keywords_data.get('industries', [])}")
        search_terms = (
            keywords_data.get("key_skills", []) +
            keywords_data.get("job_titles", [])
        )

    # Step 2: Search all sources
    print(f"\n🌐 Searching job boards...")
    all_jobs = []
    all_jobs.extend(search_remoteok(search_terms))
    all_jobs.extend(search_adzuna(search_terms))
    all_jobs.extend(search_arbeitnow(search_terms))

    print(f"\n📊 Found {len(all_jobs)} total jobs across all sources")

    if not all_jobs:
        print("   No jobs found. Try different keywords or check API keys.")
        return []

    # Step 3: Deduplicate by company+title
    seen = set()
    unique_jobs = []
    for job in all_jobs:
        key = f"{job.company.lower().strip()}|{job.title.lower().strip()}"
        if key not in seen:
            seen.add(key)
            unique_jobs.append(job)

    print(f"   {len(unique_jobs)} unique jobs after dedup")

    # Step 4: Score relevance
    print(f"\n🎯 Scoring job relevance...")
    scored_jobs = score_jobs(profile, unique_jobs)

    # Step 5: Filter by minimum score
    qualified = [j for j in scored_jobs if j.relevance_score >= min_score]
    print(f"   {len(qualified)} jobs scored above {min_score}/100")

    result = qualified[:max_results]

    # Print results
    print(f"\n{'='*70}")
    print(f"{'#':<4} {'Score':<6} {'Title':<30} {'Company':<20} {'Source':<10}")
    print(f"{'-'*70}")
    for i, job in enumerate(result, 1):
        print(f"{i:<4} {job.relevance_score:<6} {job.title[:29]:<30} {job.company[:19]:<20} {job.source:<10}")

    return result


if __name__ == "__main__":
    from agents.profile_loader import load_profile, load_cached_profile

    profile = load_cached_profile() or load_profile()
    jobs = discover_jobs(profile)