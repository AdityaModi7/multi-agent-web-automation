"""GitHub Job List Scraper - Extracts job URLs from GitHub markdown lists.

Parses markdown tables like the speedyapply/2026-SWE-College-Jobs repo
and extracts application URLs for batch processing.

Usage:
    python github_scraper.py https://github.com/speedyapply/2026-SWE-College-Jobs/blob/main/NEW_GRAD_USA.md
    python github_scraper.py https://github.com/speedyapply/2026-SWE-College-Jobs/blob/main/NEW_GRAD_USA.md --apply
"""

import re
import sys
import json
import argparse
import requests
from pathlib import Path


def github_blob_to_raw(url: str) -> str:
    """Convert a GitHub blob URL to a raw content URL."""
    # https://github.com/user/repo/blob/branch/file.md
    # -> https://raw.githubusercontent.com/user/repo/branch/file.md
    url = url.replace("github.com", "raw.githubusercontent.com")
    url = url.replace("/blob/", "/")
    return url


def fetch_markdown(url: str) -> str:
    """Fetch the raw markdown content from a GitHub URL.
    
    Handles:
    - Direct blob URLs: github.com/user/repo/blob/branch/file.md
    - Repo root URLs: github.com/user/repo (tries master/README.md then main/README.md)
    """
    # If URL points to repo root (no /blob/ and no .md extension), try README.md
    if "/blob/" not in url and not url.endswith(".md"):
        url = url.rstrip("/")
        # Try master branch first, then main
        for branch in ["master", "main"]:
            raw_url = url.replace("github.com", "raw.githubusercontent.com")
            raw_url = f"{raw_url}/{branch}/README.md"
            print(f"Fetching: {raw_url}")
            try:
                resp = requests.get(raw_url, timeout=30)
                if resp.status_code == 200:
                    return resp.text
            except Exception:
                continue
        raise RuntimeError(f"Could not find README.md at {url} (tried master and main branches)")
    
    raw_url = github_blob_to_raw(url)
    print(f"Fetching: {raw_url}")
    resp = requests.get(raw_url, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_jobs(md_text: str) -> list[dict]:
    """Extract job entries from a markdown table.

    Handles both standard markdown links [text](url) and HTML links <a href="url">text</a>.
    The table may have columns like: Company | Role | Location | Link | Date
    """
    jobs = []

    lines = md_text.strip().split("\n")
    header_idx = None
    columns = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue

        cells = [c.strip() for c in stripped.split("|")[1:-1]]

        # Detect header row
        if header_idx is None and len(cells) >= 3:
            lower_cells = [c.lower().strip() for c in cells]
            if any(x in " ".join(lower_cells) for x in ["company", "role", "position", "link", "apply"]):
                columns = lower_cells
                header_idx = i
                continue

        # Skip separator row
        if header_idx is not None and all(set(c.strip()) <= {"-", ":", " "} for c in cells if c.strip()):
            continue

        # Data row
        if header_idx is not None and len(cells) >= 2:
            job = {}
            for j, col in enumerate(columns):
                if j < len(cells):
                    job[col] = cells[j]

            # Extract ALL URLs from all cells
            all_urls = []
            full_row = "|".join(cells)  # rejoin to handle HTML split across cells

            # 1. HTML href links: <a href="url">
            for match in re.finditer(r'href=["\']([^"\']+)["\']', full_row):
                all_urls.append(match.group(1))
            # 2. Markdown links: [text](url)
            for match in re.finditer(r'\[([^\]]*)\]\(([^)]+)\)', full_row):
                all_urls.append(match.group(2))
            # 3. Bare URLs (stop at quotes, angle brackets, pipes, parens)
            for match in re.finditer(r'(https?://[^\s<>"\'|)]+)', full_row):
                all_urls.append(match.group(1))

            # Deduplicate while preserving order
            seen = set()
            unique_urls = []
            for u in all_urls:
                # Clean each URL
                u = re.sub(r'[<>"\']+.*$', '', u).strip().rstrip('/')
                if u and u not in seen:
                    seen.add(u)
                    unique_urls.append(u)

            # Find the APPLICATION url (not the company homepage)
            apply_url = None
            company_url = None
            for url in unique_urls:
                url_lower = url.lower()
                # Skip obviously bad URLs
                if len(url) < 15:
                    continue
                # These are ATS / application URLs
                if any(x in url_lower for x in [
                    "greenhouse", "lever.co", "workday", "ashby",
                    "jobs.", "careers.", "/jobs/", "/careers/",
                    "/apply", "job-boards", "boards.",
                    "icims", "taleo", "jobvite", "smartrecruiters",
                    "myworkday", "applicant"
                ]):
                    apply_url = url
                    break
                # Company homepage (fallback only)
                if not company_url:
                    company_url = url

            # Use apply URL preferably; skip entries with only company homepages
            url = apply_url
            if not url:
                continue  # skip jobs without a direct application link

            # Extract company name from cells
            company = ""
            for col_name in columns:
                if "company" in col_name or "name" in col_name:
                    idx = columns.index(col_name)
                    if idx < len(cells):
                        raw = cells[idx]
                        # Extract text from HTML: <a href="..."><strong>Name</strong></a>
                        html_match = re.search(r'>([^<]+)<', raw)
                        if html_match:
                            company = html_match.group(1).strip()
                        if not company:
                            # Markdown link
                            md_match = re.search(r'\[([^\]]+)\]', raw)
                            if md_match:
                                company = md_match.group(1).strip()
                        if not company:
                            company = re.sub(r'<[^>]+>', '', raw).strip()
                            company = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', company)
                            company = re.sub(r'[*_~`]', '', company).strip()
                    break

            # Extract role
            role = ""
            for col_name in columns:
                if any(x in col_name for x in ["role", "position", "title"]):
                    idx = columns.index(col_name)
                    if idx < len(cells):
                        raw = cells[idx]
                        # Try to get just the text from a link
                        md_match = re.search(r'\[([^\]]+)\]', raw)
                        if md_match:
                            role = md_match.group(1).strip()
                        else:
                            role = re.sub(r'<[^>]+>', '', raw).strip()
                            role = re.sub(r'[*_~`]', '', role).strip()
                    break

            # Extract location
            location = ""
            for col_name in columns:
                if "location" in col_name:
                    idx = columns.index(col_name)
                    if idx < len(cells):
                        location = re.sub(r'<[^>]+>', '', cells[idx]).strip()
                    break

            if url and (company or role):
                jobs.append({
                    "company": company,
                    "role": role,
                    "location": location,
                    "url": url,
                })

    return jobs


def save_urls(jobs: list[dict], output_path: str = "data/job_urls.txt"):
    """Save just the URLs to a file for batch processing."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    urls = [j["url"] for j in jobs]
    Path(output_path).write_text("\n".join(urls))
    print(f"\nSaved {len(urls)} URLs to {output_path}")
    return output_path


def save_full_list(jobs: list[dict], output_path: str = "data/scraped_jobs.json"):
    """Save the full job list with metadata."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(jobs, indent=2))
    print(f"Saved full job list to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Scrape job URLs from GitHub job lists")
    parser.add_argument("url", help="GitHub URL to the markdown file")
    parser.add_argument("--output", "-o", default="data/job_urls.txt",
                        help="Output file for URLs (default: data/job_urls.txt)")
    parser.add_argument("--apply", action="store_true",
                        help="Immediately run batch apply after scraping")
    parser.add_argument("--auto-fill", action="store_true",
                        help="Auto-fill application forms in browser (used with --apply)")
    parser.add_argument("--min-score", type=int, default=40,
                        help="Minimum fit score (default: 40)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of jobs to process")
    parser.add_argument("--filter", "-f", default=None,
                        help="Filter jobs by keyword in company/role (e.g., 'AI,ML,data')")
    args = parser.parse_args()

    # Fetch and parse
    md_text = fetch_markdown(args.url)
    jobs = extract_jobs(md_text)

    if not jobs:
        print("No jobs found. The markdown format might not match expected table structure.")
        return

    print(f"\nFound {len(jobs)} jobs:")

    # Filter
    if args.filter:
        keywords = [k.strip().lower() for k in args.filter.split(",")]
        jobs = [j for j in jobs if any(
            kw in j["company"].lower() or kw in j["role"].lower()
            for kw in keywords
        )]
        print(f"Filtered to {len(jobs)} jobs matching: {args.filter}")

    # Limit
    if args.limit:
        jobs = jobs[:args.limit]
        print(f"Limited to {args.limit} jobs")

    # Show first 20
    for i, j in enumerate(jobs[:20]):
        print(f"  [{i+1}] {j['company'][:25]:<25} | {j['role'][:35]:<35} | {j['url'][:50]}")
    if len(jobs) > 20:
        print(f"  ... and {len(jobs) - 20} more")

    # Save
    url_file = save_urls(jobs, args.output)
    save_full_list(jobs)

    # Optionally run batch apply
    if args.apply:
        print(f"\nStarting batch apply on {len(jobs)} jobs (min score: {args.min_score})...")
        import subprocess
        cmd = ["python", "main.py", "batch", "--file", url_file,
               "--min-score", str(args.min_score)]
        if args.auto_fill:
            cmd.append("--auto-fill")
        subprocess.run(cmd)


if __name__ == "__main__":
    main()