"""Job Searcher — Discovers ML/AI roles from multiple job boards.

Scrapes LinkedIn, Indeed, Greenhouse boards, and Lever boards
for entry-level / new-grad ML & AI roles.
"""

from models import JobSearchResult, SearchFilters, SearchRun
from utils.llm import call_llm_json
from bs4 import BeautifulSoup
import requests
import sys
import re
import time
import json
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus, urljoin

sys.path.insert(0, str(Path(__file__).parent.parent))


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── New-grad / entry-level signal detection ─────────────────────────────

ENTRY_LEVEL_SIGNALS = [
    "new grad", "new graduate", "entry level", "entry-level",
    "junior", "associate", "0-2 year", "0-3 year", "1-3 year",
    "0 - 2 year", "0 - 3 year", "1 - 3 year",
    "early career", "recent graduate", "fresh graduate",
    "university grad", "college grad", "l3", "l4", "ic1", "ic2",
    "no experience required", "1+ year", "2+ year",
]

# Exclude senior-only roles
SENIOR_SIGNALS = [
    "senior", "staff", "principal", "lead", "director",
    "manager", "vp ", "head of", "5+ year", "7+ year", "10+ year",
    "sr.", "sr ", "iii", " iv",
]


def is_entry_level_friendly(title: str, description: str = "") -> bool:
    """Check if a role is suitable for new grad or 1-3 years experience."""
    combined = f"{title} {description}".lower()
    # Positive: has entry-level signals
    has_entry_signal = any(
        signal in combined for signal in ENTRY_LEVEL_SIGNALS)
    # Negative: has senior-only signals
    has_senior_signal = any(signal in combined for signal in SENIOR_SIGNALS)
    # If explicitly entry-level, always include. If no seniority signal, include.
    return has_entry_signal or not has_senior_signal


def meets_salary_floor(salary_text: str, min_salary: int = 120000) -> bool:
    """Check if a salary range meets the minimum threshold."""
    if not salary_text:
        return True  # Unknown salary — don't exclude
    # Extract numbers from salary text
    numbers = re.findall(r'[\d,]+', salary_text.replace(",", ""))
    for n in numbers:
        try:
            val = int(n)
            # Handle hourly rates (assume 2080 hours/year)
            if val < 500:
                val *= 2080
            if val >= min_salary:
                return True
        except ValueError:
            continue
    # If we found numbers but none met the floor, exclude
    return len(numbers) == 0  # No numbers = unknown, keep it


def is_ml_ai_role(title: str) -> bool:
    """Check if a title is ML/AI related."""
    ml_keywords = [
        "machine learning", "ml ", "ml/", "ai ", "ai/",
        "artificial intelligence", "deep learning", "data scien",
        "nlp", "natural language", "computer vision",
        "applied scientist", "research engineer", "research scientist",
        "mlops", "ml ops", "ml platform", "ai platform",
        "neural", "generative ai", "gen ai", "llm",
    ]
    title_lower = title.lower()
    return any(kw in title_lower for kw in ml_keywords)


# ── Classification via LLM (for ambiguous postings) ─────────────────────

CLASSIFY_PROMPT = """You are a job posting classifier. Given a list of job titles and snippets, classify each as:
1. Is it an ML/AI/Data Science role? (true/false)
2. Is it new-grad/entry-level friendly (0-3 years experience)? (true/false)

Return ONLY valid JSON (no markdown, no backticks):
{
 "classifications": [
 {"index": 0, "is_ml_ai": true, "is_entry_level": true},
 {"index": 1, "is_ml_ai": false, "is_entry_level": true}
 ]
}

Be generous — if a role COULD be suitable for someone with a BS in CS (graduated 2025), currently pursuing MS in CS/AI at Georgia Tech, with ~1 year of full-time SWE experience plus internships, mark it as entry-level friendly. Include roles that say 0-3 years or 1-3 years."""


def classify_jobs_with_llm(jobs: list[dict]) -> list[dict]:
    """Use LLM to classify ambiguous job postings."""
    if not jobs:
        return []

    batch_text = "\n\n".join(
        f"[{i}] Title: {j['title']}\nCompany: {j['company']}\nSnippet: {j.get('snippet', 'N/A')}"
        for i, j in enumerate(jobs)
    )

    try:
        result = call_llm_json(
            system_prompt=CLASSIFY_PROMPT,
            user_message=f"Classify these job postings:\n\n{batch_text}",
            max_tokens=1500,
        )
        return result.get("classifications", [])
    except Exception as e:
        print(f" [WARNING] LLM classification failed: {e}")
        return []


# ── LinkedIn Jobs Scraper ───────────────────────────────────────────────

def search_linkedin(keyword: str, location: str = None, max_results: int = 25) -> list[JobSearchResult]:
    """Search LinkedIn Jobs (public listing page, no login required)."""
    results = []

    # LinkedIn public job search URL
    params = {
        "keywords": keyword,
        "f_E": "1,2",  # Entry level + Associate
        "f_TPR": "r604800",  # Past week
        "position": "1",
        "pageNum": "0",
    }
    if location:
        params["location"] = location

    query_str = "&".join(
        f"{k}={quote_plus(str(v))}" for k, v in params.items())
    url = f"https://www.linkedin.com/jobs/search/?{query_str}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # LinkedIn public job cards
        job_cards = soup.select(".base-card, .job-search-card, .base-search-card")

        for card in job_cards[:max_results]:
            title_el = card.select_one(
                ".base-search-card__title, .base-card__full-link")
            company_el = card.select_one(
                ".base-search-card__subtitle, .hidden-nested-link")
            location_el = card.select_one(
                ".job-search-card__location, .base-search-card__metadata")
            link_el = card.select_one("a[href*='/jobs/view/']") or card.select_one("a")

            title = title_el.get_text(strip=True) if title_el else None
            company = company_el.get_text(strip=True) if company_el else None
            loc = location_el.get_text(strip=True) if location_el else None
            href = link_el.get("href", "") if link_el else ""

            if not title or not company:
                continue

            # Clean the URL
            if href and not href.startswith("http"):
                href = f"https://www.linkedin.com{href}"
            href = href.split("?")[0]  # Remove tracking params

            results.append(JobSearchResult(
                title=title,
                company=company,
                location=loc,
                url=href,
                source="linkedin",
                is_new_grad=is_entry_level_friendly(title, ""),
                description_snippet="",
            ))

        print(f" LinkedIn: found {len(results)} results for '{keyword}'")

    except Exception as e:
        print(f" [WARNING] LinkedIn search failed for '{keyword}': {e}")

    return results


# ── Indeed Scraper ──────────────────────────────────────────────────────

def search_indeed(keyword: str, location: str = None, max_results: int = 25) -> list[JobSearchResult]:
    """Search Indeed for job postings."""
    results = []

    params = f"q={quote_plus(keyword)}&fromage=7&limit={max_results}"
    if location:
        params += f"&l={quote_plus(location)}"

    url = f"https://www.indeed.com/jobs?{params}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        job_cards = soup.select(
            ".job_seen_beacon, .jobsearch-ResultsList > li, .resultContent")

        for card in job_cards[:max_results]:
            title_el = card.select_one("h2 a, .jobTitle a, .jcs-JobTitle")
            company_el = card.select_one(
                "[data-testid='company-name'], .companyName, .company")
            location_el = card.select_one(
                "[data-testid='text-location'], .companyLocation")
            snippet_el = card.select_one(".job-snippet, .underShelfFooter")

            title = title_el.get_text(strip=True) if title_el else None
            company = company_el.get_text(strip=True) if company_el else None
            loc = location_el.get_text(strip=True) if location_el else None
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""

            href = ""
            if title_el:
                href = title_el.get("href", "")
                if href and not href.startswith("http"):
                    href = f"https://www.indeed.com{href}"

            if not title or not company:
                continue

            results.append(JobSearchResult(
                title=title,
                company=company,
                location=loc,
                url=href,
                source="indeed",
                is_new_grad=is_entry_level_friendly(title, snippet),
                description_snippet=snippet[:300],
            ))

        print(f" Indeed: found {len(results)} results for '{keyword}'")

    except Exception as e:
        print(f" [WARNING] Indeed search failed for '{keyword}': {e}")

    return results


# ── Greenhouse Boards Scraper ───────────────────────────────────────────

# Popular companies with public Greenhouse boards known for ML/AI hiring
GREENHOUSE_BOARDS = [
    "openai", "anthropic", "deepmind", "cohere", "huggingface",
    "databricks", "scale", "anyscale", "weights-and-biases",
    "midjourney", "stability-ai", "runway", "replicate",
    "together-ai", "modal", "pinecone", "weaviate",
    "perplexity-ai", "mistral", "adept-ai",
]


def search_greenhouse(board_tokens: list[str] = None, max_per_board: int = 10) -> list[JobSearchResult]:
    """Search Greenhouse job boards for ML/AI roles."""
    results = []
    boards = board_tokens or GREENHOUSE_BOARDS

    for token in boards:
        try:
            api_url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
            resp = requests.get(api_url, timeout=10)
            if resp.status_code != 200:
                continue

            data = resp.json()
            jobs = data.get("jobs", [])

            count = 0
            for job in jobs:
                title = job.get("title", "")
                if not is_ml_ai_role(title):
                    continue

                loc_data = job.get("location", {})
                location = loc_data.get("name", "") if isinstance(
                    loc_data, dict) else str(loc_data)

                job_url = job.get("absolute_url", "")

                results.append(JobSearchResult(
                    title=title,
                    company=token.replace("-", " ").title(),
                    location=location,
                    url=job_url,
                    source="greenhouse",
                    is_new_grad=is_entry_level_friendly(title, ""),
                    description_snippet="",
                ))
                count += 1
                if count >= max_per_board:
                    break

            if count > 0:
                print(f" Greenhouse ({token}): found {count} ML/AI roles")

        except Exception as e:
            continue  # Silently skip failed boards

    return results


# ── Lever Boards Scraper ────────────────────────────────────────────────

LEVER_COMPANIES = [
    "openai", "anthropic", "figma", "netflix", "stripe",
    "notion", "vercel", "linear", "ramp",
]


def search_lever(company_slugs: list[str] = None, max_per_company: int = 10) -> list[JobSearchResult]:
    """Search Lever job boards for ML/AI roles."""
    results = []
    slugs = company_slugs or LEVER_COMPANIES

    for slug in slugs:
        try:
            api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
            resp = requests.get(api_url, timeout=10)
            if resp.status_code != 200:
                continue

            jobs = resp.json()
            count = 0

            for job in jobs:
                title = job.get("text", "")
                if not is_ml_ai_role(title):
                    continue

                categories = job.get("categories", {})
                location = categories.get("location", "")
                team = categories.get("team", "")

                job_url = job.get("hostedUrl", "") or job.get("applyUrl", "")

                results.append(JobSearchResult(
                    title=title,
                    company=slug.replace("-", " ").title(),
                    location=location,
                    url=job_url,
                    source="lever",
                    is_new_grad=is_entry_level_friendly(title, team),
                    description_snippet=job.get("descriptionPlain", "")[:300],
                ))
                count += 1
                if count >= max_per_company:
                    break

            if count > 0:
                print(f" Lever ({slug}): found {count} ML/AI roles")

        except Exception:
            continue

    return results


# ── Deduplication ───────────────────────────────────────────────────────

def deduplicate(results: list[JobSearchResult]) -> list[JobSearchResult]:
    """Remove duplicate listings by company+title similarity."""
    seen = set()
    unique = []
    for r in results:
        key = f"{r.company.lower().strip()}|{r.title.lower().strip()}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ── Main Search Orchestrator ────────────────────────────────────────────

def run_job_search(filters: SearchFilters = None) -> SearchRun:
    """Run a full job search across all configured sources.

    Returns a SearchRun with deduplicated, filtered results.
    """
    filters = filters or SearchFilters()
    all_results: list[JobSearchResult] = []
    sources_searched = []

    print("\n Starting ML/AI job search...")
    print(f" Keywords: {', '.join(filters.keywords[:5])}...")
    print(
        f" Experience: {filters.experience_level} (max {filters.max_experience_years} yrs)")
    print(f" Locations: {', '.join(filters.locations[:4])}...")
    print(f" Min salary: ${filters.min_salary:,}")
    if filters.include_remote:
        print(f" Including remote roles: Yes")

    # 1. Search LinkedIn across locations
    print("\n Searching LinkedIn...")
    sources_searched.append("linkedin")
    for keyword in filters.keywords[:5]:  # Top 5 keywords
        for location in filters.locations[:4]:  # Top 4 locations
            results = search_linkedin(
                keyword=keyword,
                location=location,
            )
            all_results.extend(results)
            time.sleep(1)  # Rate limiting
        # Also search remote if enabled
        if filters.include_remote:
            results = search_linkedin(keyword=keyword, location="Remote")
            all_results.extend(results)
            time.sleep(1)

    # 2. Search Indeed across locations
    print("\n Searching Indeed...")
    sources_searched.append("indeed")
    for keyword in filters.keywords[:3]:  # Top 3 keywords
        for location in filters.locations[:3]:
            results = search_indeed(
                keyword=keyword,
                location=location,
            )
            all_results.extend(results)
            time.sleep(1)

    # 3. Search Greenhouse boards (AI company boards)
    print("\n Searching Greenhouse boards (AI companies)...")
    sources_searched.append("greenhouse")
    gh_results = search_greenhouse()
    all_results.extend(gh_results)

    # 4. Search Lever boards
    print("\n Searching Lever boards...")
    sources_searched.append("lever")
    lever_results = search_lever()
    all_results.extend(lever_results)

    # 5. Deduplicate
    unique_results = deduplicate(all_results)
    print(
        f"\n Raw results: {len(all_results)} → Deduplicated: {len(unique_results)}")

    # 6. Filter out excluded companies
    if filters.exclude_companies:
        before = len(unique_results)
        exclude_lower = [c.lower() for c in filters.exclude_companies]
        unique_results = [
            r for r in unique_results
            if r.company.lower() not in exclude_lower
        ]
        print(
            f" Excluded companies: removed {before - len(unique_results)} results")

    # 6b. Filter by salary floor
    before = len(unique_results)
    unique_results = [
        r for r in unique_results
        if meets_salary_floor(r.salary_range or "", filters.min_salary)
    ]
    removed = before - len(unique_results)
    if removed:
        print(
            f" Salary filter (>= ${filters.min_salary:,}): removed {removed} results")

    # 6c. Filter out senior-only roles
    before = len(unique_results)
    unique_results = [
        r for r in unique_results
        if is_entry_level_friendly(r.title, r.description_snippet)
    ]
    removed = before - len(unique_results)
    if removed:
        print(f" Seniority filter: removed {removed} senior-only roles")

    # 7. Use LLM to classify ambiguous results
    ambiguous = [
        {"title": r.title, "company": r.company, "snippet": r.description_snippet}
        for r in unique_results
        if not is_ml_ai_role(r.title)
    ]
    if ambiguous:
        print(f"\n Classifying {len(ambiguous)} ambiguous postings with LLM...")
        classifications = classify_jobs_with_llm(ambiguous)
        # Apply classifications — remove non-ML/AI roles
        ambiguous_titles = {j["title"] for j in ambiguous}
        non_ml = set()
        for cls in classifications:
            idx = cls.get("index", -1)
            if 0 <= idx < len(ambiguous) and not cls.get("is_ml_ai", False):
                non_ml.add(ambiguous[idx]["title"])
        unique_results = [
            r for r in unique_results
            if r.title not in ambiguous_titles or r.title not in non_ml
        ]

    # 8. Sort: new-grad-friendly first, then by source reliability
    source_priority = {"greenhouse": 0, "lever": 1, "linkedin": 2, "indeed": 3}
    unique_results.sort(key=lambda r: (
        0 if r.is_new_grad else 1,
        source_priority.get(r.source, 9),
    ))

    search_run = SearchRun(
        filters=filters,
        results=unique_results,
        total_found=len(unique_results),
        sources_searched=sources_searched,
    )

    print(f"\n[OK] Search complete: {len(unique_results)} ML/AI roles found")
    new_grad_count = sum(1 for r in unique_results if r.is_new_grad)
    print(f" New-grad friendly: {new_grad_count}")
    print(f" Sources: {', '.join(sources_searched)}")

    return search_run


def print_search_results(search_run: SearchRun, limit: int = 30):
    """Pretty-print search results."""
    results = search_run.results[:limit]

    print(f"\n{'#':<4} {'Title':<40} {'Company':<20} {'Source':<12} {'New Grad':<8}")
    print("-" * 88)
    for i, r in enumerate(results, 1):
        ng = "Y" if r.is_new_grad else ""
        print(
            f"{i:<4} {r.title[:39]:<40} {r.company[:19]:<20} "
            f"{r.source:<12} {ng:<8}"
        )

    if len(search_run.results) > limit:
        print(f"\n ... and {len(search_run.results) - limit} more results")


def save_search_results(search_run: SearchRun, path: str = None):
    """Save search results to JSON for later use."""
    if path is None:
        path = str(Path(__file__).parent.parent / "data" / "last_search.json")
    Path(path).write_text(search_run.model_dump_json(indent=2))
    print(f" Search results saved to {path}")
    return path


def load_search_results(path: str = None) -> SearchRun | None:
    """Load previously saved search results."""
    if path is None:
        path = Path(__file__).parent.parent / "data" / "last_search.json"
    else:
        path = Path(path)

    if path.exists():
        return SearchRun.model_validate_json(path.read_text())
    return None
