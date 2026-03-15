"""Workflow Engine — Orchestrates the full search → tailor → apply pipeline.

This is the agentic brain that ties everything together:
1. Searches for ML/AI roles matching criteria
2. Parses each job posting in detail
3. Analyzes fit and filters by score
4. Generates tailored resume + cover letter
5. Auto-applies (or saves for manual application)
6. Tracks everything in the database

Features:
- Error recovery: continues processing if individual jobs fail
- URL-based deduplication: never applies to the same job twice
- Retry logic: retries transient failures
- Comprehensive logging and reporting
"""

import sys
import time
import json
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.auto_applier import auto_apply
from agents.tracker import (
    save_application, update_status, list_applications,
    is_duplicate, get_existing_urls, get_existing_keys,
)
from agents.tailoring_agent import run_tailoring_pipeline
from agents.profile_loader import load_profile, load_cached_profile, save_profile
from agents.job_parser import parse_job_posting
from agents.job_searcher import run_job_search, save_search_results, load_search_results
from models import (
    WorkflowConfig, SearchFilters, JobSearchResult,
    ApplicationStatus, ApplyMethod,
)

logger = logging.getLogger("workflow_engine")


def get_profile(resume_path: str = None):
    """Load profile from cache or parse from resume."""
    cached = load_cached_profile()
    if cached:
        print(f"[OK] Loaded cached profile: {cached.name}")
        return cached

    if resume_path:
        print(f" Parsing resume from {resume_path}...")
    else:
        print(" Parsing resume from data/my_resume.md...")

    profile = load_profile(resume_path=resume_path)
    save_profile(profile)
    print(
        f"[OK] Profile parsed: {profile.name} — {len(profile.skills)} skills, {len(profile.experience)} roles")
    return profile


def run_workflow(config: WorkflowConfig = None) -> dict:
    """Execute the full agentic workflow.

    Pipeline:
    Search → Parse → Fit Analysis → Tailor → Apply → Track

    Returns a summary dict with counts and details of each step.
    """
    config = config or WorkflowConfig()

    summary = {
        "started_at": datetime.now().isoformat(),
        "config": config.model_dump(),
        "jobs_found": 0,
        "jobs_parsed": 0,
        "jobs_above_threshold": 0,
        "resumes_tailored": 0,
        "applications_attempted": 0,
        "applications_submitted": 0,
        "applications_manual": 0,
        "applications_skipped_duplicate": 0,
        "errors": [],
        "results": [],
    }

    print("\n" + "=" * 70)
    print(" ML/AI JOB APPLICATION WORKFLOW ENGINE")
    print("=" * 70)
    print(
        f" Mode: {' DRY RUN (no submissions)' if config.dry_run else ' LIVE (will submit!)'}")
    print(f" Min fit score: {config.min_fit_score}")
    print(f" Max applications: {config.max_applications_per_run}")
    print(f" Min salary: ${config.search_filters.min_salary:,}")
    print(f" Locations: {', '.join(config.search_filters.locations[:4])}")
    print("=" * 70)

    # ── Step 1: Load Profile ────────────────────────────────────────────
    print("\n STEP 1: Loading profile...")
    profile = get_profile(config.resume_path)

    # ── Step 2: Search for Jobs ─────────────────────────────────────────
    print("\n STEP 2: Searching for ML/AI jobs...")
    search_run = run_job_search(config.search_filters)
    save_search_results(search_run)
    summary["jobs_found"] = search_run.total_found

    if not search_run.results:
        print("\n[WARNING] No jobs found matching criteria. Try broadening search filters.")
        return summary

    # ── Step 3: Build dedup sets ────────────────────────────────────────
    existing_urls = get_existing_urls()
    existing_keys = get_existing_keys()
    logger.info(f"Loaded {len(existing_urls)} existing URLs and {len(existing_keys)} company|title keys for dedup")

    # ── Step 4: Process Each Job ────────────────────────────────────────
    to_process = min(len(search_run.results), config.max_applications_per_run * 2)  # Process extra to account for skips
    print(f"\n STEP 3: Processing up to {to_process} jobs...")
    print("-" * 70)

    applied_count = 0

    for i, job_result in enumerate(search_run.results[:to_process]):
        if applied_count >= config.max_applications_per_run:
            print(
                f"\n[STOP] Reached max applications ({config.max_applications_per_run}). Stopping.")
            break

        # ── Deduplication check (URL + company|title) ──────────────
        if job_result.url and job_result.url in existing_urls:
            print(f"\n[{i+1}] [SKIP] Already applied (URL match): {job_result.title} at {job_result.company}")
            summary["applications_skipped_duplicate"] += 1
            continue

        app_key = f"{job_result.company.lower()}|{job_result.title.lower()}"
        if app_key in existing_keys:
            print(f"\n[{i+1}] [SKIP] Already applied: {job_result.title} at {job_result.company}")
            summary["applications_skipped_duplicate"] += 1
            continue

        print(
            f"\n[{i+1}/{len(search_run.results)}] {job_result.title} at {job_result.company}")
        print(
            f" Source: {job_result.source} | Location: {job_result.location or 'N/A'}")
        print(f" URL: {job_result.url[:80]}...")

        result_entry = {
            "title": job_result.title,
            "company": job_result.company,
            "url": job_result.url,
            "source": job_result.source,
        }

        # ── 4a: Parse job posting in detail ─────────────────────────
        try:
            if job_result.url:
                print(f" Parsing full job posting...")
                job = parse_job_posting(url=job_result.url)
            elif job_result.description_snippet:
                job = parse_job_posting(text=job_result.description_snippet)
            else:
                print(f" [WARNING] No URL or description — skipping")
                result_entry["status"] = "skipped_no_data"
                summary["results"].append(result_entry)
                continue

            summary["jobs_parsed"] += 1
            print(f" [OK] Parsed: {job.title} at {job.company}")
            print(f" Required skills: {', '.join(job.required_skills[:6])}")

        except Exception as e:
            print(f" [ERROR] Failed to parse: {e}")
            result_entry["status"] = "parse_error"
            result_entry["error"] = str(e)
            summary["errors"].append(f"Parse error for {job_result.company}: {e}")
            summary["results"].append(result_entry)
            logger.error(f"Parse error for {job_result.url}: {e}")
            continue  # Continue to next job instead of stopping

        # ── 4b: Run tailoring pipeline (fit + resume + cover letter) ─
        try:
            tailoring_result = run_tailoring_pipeline(
                profile=profile,
                job=job,
                skip_if_below=config.min_fit_score,
            )

            fit = tailoring_result["fit_analysis"]
            result_entry["fit_score"] = fit.overall_score
            result_entry["recommendation"] = fit.recommendation

            if fit.overall_score < config.min_fit_score:
                print(
                    f" [WARNING] Fit score {fit.overall_score} below threshold {config.min_fit_score}. Skipping.")
                result_entry["status"] = "below_threshold"

                save_application(
                    job=job,
                    fit=fit,
                    resume_text=None,
                    cover_letter_text=None,
                    status=ApplicationStatus.SKIPPED,
                    notes=f"Auto-skipped: fit score {fit.overall_score} < {config.min_fit_score}",
                )
                # Add to dedup sets (don't re-analyze skipped jobs)
                existing_keys.add(app_key)
                if job_result.url:
                    existing_urls.add(job_result.url)
                summary["results"].append(result_entry)
                continue

            summary["jobs_above_threshold"] += 1

            resume = tailoring_result["tailored_resume"]
            cover = tailoring_result["cover_letter"]

            if resume:
                summary["resumes_tailored"] += 1

            # Save output files
            safe_company = job.company.lower().replace(' ', '_').replace('/', '_')[:25]
            safe_title = job.title.lower().replace(' ', '_').replace('/', '_')[:30]
            output_dir = Path("output") / f"{safe_company}_{safe_title}"
            output_dir.mkdir(parents=True, exist_ok=True)

            (output_dir / "resume.md").write_text(resume.resume_text)
            (output_dir / "cover_letter.md").write_text(cover.full_text)
            (output_dir / "fit_analysis.md").write_text(
                f"# Fit Analysis: {job.title} at {job.company}\n\n"
                f"**Score: {fit.overall_score}/100 — {fit.recommendation}**\n\n"
                f"{fit.reasoning}\n"
            )
            print(f" Materials saved to {output_dir}/")

        except Exception as e:
            print(f" [ERROR] Tailoring failed: {e}")
            result_entry["status"] = "tailoring_error"
            result_entry["error"] = str(e)
            summary["errors"].append(f"Tailoring error for {job_result.company}: {e}")
            summary["results"].append(result_entry)
            logger.error(f"Tailoring error for {job_result.company}: {e}")
            continue  # Continue to next job

        # ── 4c: Auto-apply ──────────────────────────────────────────
        resume_text = resume.resume_text if resume else None
        cover_text = cover.full_text if cover else None

        if resume_text and job_result.url:
            summary["applications_attempted"] += 1

            try:
                print(f" Attempting auto-apply...")
                attempt = auto_apply(
                    job_url=job_result.url,
                    company=job.company,
                    title=job.title,
                    resume_text=resume_text,
                    cover_letter_text=cover_text or "",
                    dry_run=config.dry_run,
                )

                result_entry["apply_method"] = attempt.method.value
                result_entry["apply_success"] = attempt.success
                if attempt.screenshot_path:
                    result_entry["screenshot"] = attempt.screenshot_path

                if attempt.success:
                    summary["applications_submitted"] += 1
                    status = ApplicationStatus.APPLIED if not config.dry_run else ApplicationStatus.DRAFT
                    result_entry["status"] = "submitted" if not config.dry_run else "dry_run_ok"
                elif attempt.method in (ApplyMethod.MANUAL, ApplyMethod.REDIRECT):
                    summary["applications_manual"] += 1
                    status = ApplicationStatus.DRAFT
                    result_entry["status"] = "manual_needed"
                else:
                    status = ApplicationStatus.DRAFT
                    result_entry["status"] = "apply_failed"

            except Exception as e:
                print(f" [ERROR] Auto-apply error: {e}")
                status = ApplicationStatus.DRAFT
                result_entry["status"] = "apply_error"
                result_entry["error"] = str(e)
                summary["errors"].append(f"Apply error for {job_result.company}: {e}")
                logger.error(f"Apply error for {job_result.company}: {e}")
        else:
            status = ApplicationStatus.DRAFT
            result_entry["status"] = "materials_only"

        # ── 4d: Save to tracker ─────────────────────────────────────
        app_id = save_application(
            job=job,
            fit=fit,
            resume_text=resume_text,
            cover_letter_text=cover_text,
            status=status,
            notes=f"Auto-workflow | Score: {fit.overall_score} | {result_entry.get('status', 'processed')}",
        )
        result_entry["app_id"] = app_id
        result_entry["status"] = result_entry.get("status", "processed")
        summary["results"].append(result_entry)
        applied_count += 1

        # Update dedup sets only for successful applications
        if status != ApplicationStatus.DRAFT:
            existing_keys.add(app_key)
            if job_result.url:
                existing_urls.add(job_result.url)

        # Cooldown between applications
        if applied_count < config.max_applications_per_run:
            cooldown = config.delay_between_applies_sec
            print(f" Cooling down {cooldown}s...")
            time.sleep(cooldown)

    # ── Step 5: Summary ─────────────────────────────────────────────────
    summary["finished_at"] = datetime.now().isoformat()
    _print_summary(summary, config.dry_run)

    # Save run report
    report_path = Path("output") / "auto_apply" / \
        f"run_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n Full report saved to: {report_path}")

    return summary


def _print_summary(summary: dict, dry_run: bool):
    """Print a formatted summary of the workflow run."""
    print("\n" + "=" * 70)
    print(" WORKFLOW SUMMARY")
    print("=" * 70)
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f" Mode: {mode}")
    print(f" Jobs found: {summary['jobs_found']}")
    print(f" Jobs parsed: {summary['jobs_parsed']}")
    print(f" Duplicates skipped: {summary['applications_skipped_duplicate']}")
    print(f" Above fit threshold: {summary['jobs_above_threshold']}")
    print(f" Resumes tailored: {summary['resumes_tailored']}")
    print(f" Apply attempts: {summary['applications_attempted']}")
    print(f" Submitted: {summary['applications_submitted']}")
    print(f" Manual needed: {summary['applications_manual']}")
    if summary["errors"]:
        print(f" Errors: {len(summary['errors'])}")
    print("=" * 70)

    # Show individual results
    if summary["results"]:
        print(f"\n{'#':<4} {'Company':<20} {'Score':<6} {'Status':<18} {'Method':<12}")
        print("-" * 64)
        for i, r in enumerate(summary["results"], 1):
            score = str(r.get("fit_score", "-"))
            status = r.get("status", "?")
            method = r.get("apply_method", "-")
            print(
                f"{i:<4} {r['company'][:19]:<20} {score:<6} "
                f"{status[:17]:<18} {method:<12}"
            )

    if dry_run:
        print("\n[TIP] This was a DRY RUN. To submit applications for real, run:")
        print(" python main.py run --live")
    print()
