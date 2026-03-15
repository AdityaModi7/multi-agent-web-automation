"""Batch Processor — Apply to multiple jobs from a file of URLs or a list.

Processes each URL through the full pipeline:
parse → fit analysis → tailor resume → auto-apply → track

Continues processing even if individual jobs fail.
"""

import sys
import json
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.job_parser import parse_job_posting
from agents.tailoring_agent import run_tailoring_pipeline
from agents.auto_applier import auto_apply
from agents.tracker import save_application, is_duplicate, ApplicationStatus
from models import Profile

logger = logging.getLogger("batch_processor")


def process_batch(
    profile: Profile,
    urls_file: str = None,
    urls: list[str] = None,
    min_score: int = 40,
    dry_run: bool = True,
    delay_between: int = 15,
) -> dict:
    """Process multiple job applications from a list of URLs.

    Args:
        profile: Parsed user profile.
        urls_file: Path to file with one URL per line.
        urls: List of URLs to process.
        min_score: Minimum fit score to proceed.
        dry_run: If True, fill forms but don't submit.
        delay_between: Seconds between applications.

    Returns:
        Summary dict with applied/skipped/failed counts.
    """
    if urls_file:
        text = Path(urls_file).read_text()
        url_list = [line.strip() for line in text.splitlines()
                    if line.strip() and not line.startswith("#")]
    elif urls:
        url_list = urls
    else:
        raise ValueError("Provide either urls_file or urls")

    total = len(url_list)
    print(f"\n{'='*60}")
    print(f"BATCH APPLICATION - {total} jobs")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'='*60}")

    results = {"applied": [], "skipped": [], "failed": [], "duplicates": []}

    for i, url in enumerate(url_list, 1):
        print(f"\n{'-'*60}")
        print(f"[{i}/{total}] {url}")
        print(f"{'-'*60}")

        try:
            # Parse job posting
            print("   Parsing job posting...")
            job = parse_job_posting(url=url)
            print(f"   {job.title} at {job.company}")

            # Check for duplicates
            if is_duplicate(job.company, job.title, url):
                print(f"   [SKIP] Already applied to this job")
                results["duplicates"].append({
                    "url": url, "title": job.title, "company": job.company,
                })
                continue

            # Run tailoring pipeline
            result = run_tailoring_pipeline(
                profile=profile, job=job, skip_if_below=min_score,
            )

            fit = result["fit_analysis"]

            if not result["tailored_resume"]:
                print(f"   [SKIP] Fit score {fit.overall_score} below threshold {min_score}")
                results["skipped"].append({
                    "url": url, "title": job.title, "company": job.company,
                    "score": fit.overall_score, "reason": fit.recommendation,
                })
                # Still save to tracker
                save_application(
                    job=job, fit=fit,
                    notes=f"Batch skip: score {fit.overall_score} < {min_score}",
                )
                continue

            # Save output files
            safe_company = job.company.lower().replace(" ", "_").replace("/", "_")[:20]
            safe_title = job.title.lower().replace(" ", "_").replace("/", "_")[:25]
            output_dir = Path("output") / f"{safe_company}_{safe_title}"
            output_dir.mkdir(parents=True, exist_ok=True)

            (output_dir / "resume.md").write_text(result["tailored_resume"].resume_text)
            if result["cover_letter"]:
                (output_dir / "cover_letter.md").write_text(result["cover_letter"].full_text)

            # Auto-apply
            resume_text = result["tailored_resume"].resume_text
            cover_text = result["cover_letter"].full_text if result["cover_letter"] else ""

            print(f"   Auto-applying...")
            attempt = auto_apply(
                job_url=url,
                company=job.company,
                title=job.title,
                resume_text=resume_text,
                cover_letter_text=cover_text,
                dry_run=dry_run,
            )

            # Save to tracker
            status = ApplicationStatus.APPLIED if attempt.success and not dry_run else ApplicationStatus.DRAFT
            app_id = save_application(
                job=job, fit=fit,
                resume_text=resume_text,
                cover_letter_text=cover_text,
                status=status,
                notes=f"Batch {'dry-run' if dry_run else 'live'} | {attempt.method.value} | {'OK' if attempt.success else 'FAIL'}",
            )

            print(f"   Saved as application #{app_id} -> {output_dir}")

            results["applied"].append({
                "id": app_id, "url": url, "title": job.title,
                "company": job.company, "score": fit.overall_score,
                "method": attempt.method.value,
                "success": attempt.success,
                "output_dir": str(output_dir),
            })

            if i < total:
                print(f"   Waiting {delay_between}s...")
                time.sleep(delay_between)

        except Exception as e:
            print(f"   [ERROR] {e}")
            results["failed"].append({"url": url, "error": str(e)})
            logger.error(f"Batch item failed: {url}: {e}")
            continue  # Continue to next URL

    # Print summary
    print(f"\n{'='*60}")
    print(f"BATCH COMPLETE")
    print(f"{'='*60}")
    print(f"   Applied:     {len(results['applied'])}")
    print(f"   Skipped:     {len(results['skipped'])}")
    print(f"   Duplicates:  {len(results['duplicates'])}")
    print(f"   Failed:      {len(results['failed'])}")

    if results["applied"]:
        print(f"\n   Applications:")
        for app in results["applied"]:
            status_icon = "[OK]" if app["success"] else "[--]"
            print(f"   #{app['id']} {status_icon} [{app['score']}/100] {app['title']} at {app['company']} ({app['method']})")

    if results["skipped"]:
        print(f"\n   Skipped (below {min_score} fit score):")
        for s in results["skipped"]:
            print(f"   [{s['score']}/100] {s['title']} at {s['company']}")

    if results["failed"]:
        print(f"\n   Failed:")
        for f in results["failed"]:
            print(f"   {f['url'][:60]}... — {f['error'][:50]}")

    # Save summary
    summary_path = Path("output") / "batch_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n   Summary saved to {summary_path}")

    return results
