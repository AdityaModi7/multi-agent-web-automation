#!/usr/bin/env python3
"""Job Application Agent — Main CLI orchestrator.

Commands:
 apply Apply to a single job (URL, text, or file)
 search Search for ML/AI jobs matching criteria
 run Full agentic workflow: search → tailor → apply
 dashboard View application stats
 status Update an application's status
 list List tracked applications
"""

from agents.tracker import (
                save_application,
                update_status,
                list_applications,
                print_dashboard,
                get_stats,
                ApplicationStatus,
)
from agents.tailoring_agent import run_tailoring_pipeline
from agents.profile_loader import load_profile, load_cached_profile, save_profile
from agents.job_parser import parse_job_posting
import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))


def get_profile(resume_path: str = None) -> "Profile":
    """Load profile from cache or parse from resume."""
    # Try cache first
    cached = load_cached_profile()
    if cached:
        print(f"[OK] Loaded cached profile: {cached.name}")
        return cached

    # Parse from resume
    if resume_path:
        print(f" Parsing resume from {resume_path}...")
    else:
        print(" Parsing resume from data/my_resume.md...")

    profile = load_profile(resume_path=resume_path)
    save_profile(profile)
    print(
                    f"[OK] Profile parsed: {profile.name} — {len(profile.skills)} skills, {len(profile.experience)} roles")
    return profile


def cmd_apply(args):
    """Process a job posting and generate tailored application materials."""
    profile = get_profile(args.resume)

    # Parse job posting
    if args.job_url:
        print(f"\n Fetching job posting from URL...")
        job = parse_job_posting(url=args.job_url)
    elif args.job_text:
        job = parse_job_posting(text=args.job_text)
    elif args.job_file:
        text = Path(args.job_file).read_text()
        job = parse_job_posting(text=text)
    else:
                    # Interactive: paste job description
        print("\n Paste the job description below (press Ctrl+D or Ctrl+Z when done):\n")
        lines = []
        try:
            while True:
                lines.append(input())
        except EOFError:
            pass
        job = parse_job_posting(text="\n".join(lines))

    print(f"\n Job: {job.title} at {job.company}")
    print(f" Location: {job.location or 'Not specified'}")
    print(f" Required skills: {', '.join(job.required_skills[:8])}")

    # Run tailoring pipeline
    result = run_tailoring_pipeline(
                    profile=profile,
                    job=job,
                    skip_if_below=args.min_score,
    )

    fit = result["fit_analysis"]

    # Save to tracker
    resume_text = result["tailored_resume"].resume_text if result["tailored_resume"] else None
    cover_text = result["cover_letter"].full_text if result["cover_letter"] else None

    app_id = save_application(
                    job=job,
                    fit=fit,
                    resume_text=resume_text,
                    cover_letter_text=cover_text,
    )
    print(f"\n Saved as application #{app_id}")

    # Output files
    if result["tailored_resume"]:
        output_dir = Path(
                        "output") / f"{job.company.lower().replace(' ', '_')}_{job.title.lower().replace(' ', '_')}"
        output_dir.mkdir(parents=True, exist_ok=True)

        resume_path = output_dir / "resume.md"
        resume_path.write_text(result["tailored_resume"].resume_text)
        print(f" Resume saved: {resume_path}")

        cover_path = output_dir / "cover_letter.md"
        cover_path.write_text(result["cover_letter"].full_text)
        print(f" Cover letter saved: {cover_path}")

        # Also save fit analysis
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

    print("\n[OK] Done! Review the materials and submit when ready.")
    print(f" Then run: python main.py status {app_id} applied")


# ── Search Command ──────────────────────────────────────────────────────

def cmd_search(args):
    """Search for ML/AI jobs matching criteria."""
    from agents.job_searcher import run_job_search, print_search_results, save_search_results
    from models import SearchFilters

    # Build search filters from CLI args
    filters = SearchFilters()

    if args.keywords:
        filters.keywords = [k.strip() for k in args.keywords.split(",")]

    if args.locations:
        filters.locations = [l.strip() for l in args.locations.split(",")]

    if args.min_salary:
        filters.min_salary = args.min_salary

    if args.remote:
        filters.include_remote = True

    if args.exclude:
        filters.exclude_companies = [c.strip() for c in args.exclude.split(",")]

    # Run search
    search_run = run_job_search(filters)
    print_search_results(search_run, limit=args.limit)
    save_search_results(search_run)

    print(f"\n[TIP] To process these results, run:")
    print(f" python main.py run # Dry run (preview)")
    print(f" python main.py run --live # Submit applications")


# ── Run (Full Workflow) Command ─────────────────────────────────────────

def cmd_run(args):
    """Run the full agentic workflow: search → tailor → apply."""
    from agents.workflow_engine import run_workflow
    from models import WorkflowConfig, SearchFilters

    # Build config from CLI args
    filters = SearchFilters()

    if args.keywords:
        filters.keywords = [k.strip() for k in args.keywords.split(",")]

    if args.locations:
        filters.locations = [l.strip() for l in args.locations.split(",")]

    if args.min_salary:
        filters.min_salary = args.min_salary

    if args.remote:
        filters.include_remote = True

    if args.exclude:
        filters.exclude_companies = [c.strip() for c in args.exclude.split(",")]

    config = WorkflowConfig(
                    search_filters=filters,
                    min_fit_score=args.min_score,
                    max_applications_per_run=args.max_apps,
                    dry_run=not args.live,
                    resume_path=args.resume,
                    delay_between_applies_sec=args.delay,
    )

    # Confirm if running live
    if args.live:
        print("\n[WARNING] LIVE MODE — Applications will be submitted for real!")
        print(f" Max applications: {args.max_apps}")
        print(f" Min fit score: {args.min_score}")
        confirm = input(" Continue? (yes/no): ").strip().lower()
        if confirm != "yes":
            print(" Aborted.")
            return

    run_workflow(config)


def cmd_dashboard(args):
    """Show application dashboard."""
    print_dashboard()


def cmd_status(args):
    """Update application status."""
    try:
        status = ApplicationStatus(args.status)
    except ValueError:
        valid = [s.value for s in ApplicationStatus]
        print(f"Invalid status. Choose from: {', '.join(valid)}")
        return

    update_status(args.app_id, status)
    print(f"[OK] Application #{args.app_id} → {status.value}")


def cmd_list(args):
    """List applications."""
    status_filter = ApplicationStatus(args.status) if args.status else None
    apps = list_applications(status=status_filter)

    if not apps:
        print("No applications found.")
        return

    print(
                    f"\n{'ID':<4} {'Company':<20} {'Role':<25} {'Score':<6} {'Status':<12} {'Date':<12}")
    print("-" * 79)
    for app in apps:
        date = app["created_at"][:10] if app["created_at"] else ""
        print(
                        f"{app['id']:<4} {app['company'][:19]:<20} "
                        f"{app['title'][:24]:<25} {app['fit_score']:<6} "
                        f"{app['status']:<12} {date:<12}"
        )


def main():
    parser = argparse.ArgumentParser(
                    description=" Job Application Agent — AI-powered ML/AI job search & auto-apply"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # ── apply command ──
    apply_parser = subparsers.add_parser("apply", help="Apply to a single job")
    apply_parser.add_argument("--job-url", "-u", help="URL to job posting")
    apply_parser.add_argument("--job-text", "-t", help="Raw job posting text")
    apply_parser.add_argument(
                    "--job-file", "-f", help="File containing job posting")
    apply_parser.add_argument(
                    "--resume", "-r", help="Path to your resume file")
    apply_parser.add_argument(
                    "--min-score", type=int, default=40,
                    help="Minimum fit score to generate materials (default: 40)"
    )
    apply_parser.set_defaults(func=cmd_apply)

    # ── search command ──
    search_parser = subparsers.add_parser(
                    "search", help="Search for ML/AI jobs")
    search_parser.add_argument(
                    "--keywords", "-k",
                    help="Comma-separated keywords (default: ml engineer, ai engineer, data scientist, ...)"
    )
    search_parser.add_argument(
                    "--locations", "-l",
                    help="Comma-separated target cities (default: NYC, SF, Seattle, LA, Chicago, Boston, Austin, DC)"
    )
    search_parser.add_argument(
                    "--min-salary", type=int, default=120000,
                    help="Minimum salary floor (default: $120,000)"
    )
    search_parser.add_argument(
                    "--remote", action="store_true", help="Include remote roles")
    search_parser.add_argument(
                    "--exclude", help="Comma-separated companies to exclude")
    search_parser.add_argument(
                    "--limit", type=int, default=30,
                    help="Max results to display (default: 30)"
    )
    search_parser.set_defaults(func=cmd_search)

    # ── run command (full workflow) ──
    run_parser = subparsers.add_parser(
                    "run",
                    help="Full workflow: search → tailor resume → auto-apply"
    )
    run_parser.add_argument("--live", action="store_true",
                                                                                                            help="Submit applications for real (default: dry run)")
    run_parser.add_argument(
                    "--keywords", "-k",
                    help="Comma-separated keywords (default: ml engineer, ai engineer, data scientist, ...)"
    )
    run_parser.add_argument(
                    "--locations", "-l",
                    help="Comma-separated target cities (default: NYC, SF, Seattle, LA, Chicago, Boston, Austin, DC)"
    )
    run_parser.add_argument(
                    "--min-salary", type=int, default=120000,
                    help="Minimum salary floor (default: $120,000)"
    )
    run_parser.add_argument(
                    "--remote", action="store_true", help="Include remote roles")
    run_parser.add_argument(
                    "--exclude", help="Comma-separated companies to exclude")
    run_parser.add_argument("--resume", "-r", help="Path to your resume file")
    run_parser.add_argument(
                    "--min-score", type=int, default=50,
                    help="Minimum fit score to proceed with application (default: 50)"
    )
    run_parser.add_argument(
                    "--max-apps", type=int, default=20,
                    help="Maximum applications per run (default: 20)"
    )
    run_parser.add_argument(
                    "--delay", type=int, default=30,
                    help="Seconds between applications (default: 30)"
    )
    run_parser.set_defaults(func=cmd_run)

    # ── dashboard command ──
    dash_parser = subparsers.add_parser(
                    "dashboard", help="View application dashboard")
    dash_parser.set_defaults(func=cmd_dashboard)

    # ── status command ──
    status_parser = subparsers.add_parser(
                    "status", help="Update application status")
    status_parser.add_argument("app_id", type=int, help="Application ID")
    status_parser.add_argument(
                    "status", help="New status (draft/applied/interview/rejected/offer/withdrawn)")
    status_parser.set_defaults(func=cmd_status)

    # ── list command ──
    list_parser = subparsers.add_parser("list", help="List applications")
    list_parser.add_argument("--status", "-s", help="Filter by status")
    list_parser.set_defaults(func=cmd_list)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        print("\n[TIP] Quick start:")
        print(' python main.py apply --job-url "https://example.com/job"')
        print(" python main.py search # Find ML/AI jobs")
        print(" python main.py run # Dry run: search + tailor")
        print(" python main.py run --live # Full auto-apply")
        print(" python main.py dashboard # View stats")
        return

    args.func(args)


if __name__ == "__main__":
    main()
