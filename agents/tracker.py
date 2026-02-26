"""Application Tracker — SQLite-backed tracking of all applications."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from models import Application, ApplicationStatus, JobPosting, FitAnalysis


DB_PATH = Path(__file__).parent.parent / "data" / "applications.db"


def get_db() -> sqlite3.Connection:
    """Get a database connection, creating tables if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
 CREATE TABLE IF NOT EXISTS applications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 company TEXT NOT NULL,
 title TEXT NOT NULL,
 location TEXT,
 status TEXT DEFAULT 'draft',
 fit_score INTEGER,
 recommendation TEXT,
 job_data TEXT,
 fit_data TEXT,
 resume_text TEXT,
 cover_letter_text TEXT,
 applied_date TEXT,
 follow_up_dates TEXT DEFAULT '[]',
 notes TEXT DEFAULT '',
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP
 )
 """)
    conn.commit()
    return conn


def save_application(
        job: JobPosting,
        fit: FitAnalysis,
        resume_text: str = None,
        cover_letter_text: str = None,
        status: ApplicationStatus = ApplicationStatus.DRAFT,
        notes: str = "",
) -> int:
    """Save a new application to the database. Returns the application ID."""
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO applications
 (company, title, location, status, fit_score, recommendation,
 job_data, fit_data, resume_text, cover_letter_text, notes)
 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.company,
            job.title,
            job.location,
            status.value,
            fit.overall_score,
            fit.recommendation,
            job.model_dump_json(),
            fit.model_dump_json(),
            resume_text,
            cover_letter_text,
            notes,
        ),
    )
    conn.commit()
    app_id = cursor.lastrowid
    conn.close()
    return app_id


def update_status(app_id: int, status: ApplicationStatus):
    """Update the status of an application."""
    conn = get_db()
    now = datetime.now().isoformat()

    updates = {"status": status.value, "updated_at": now}
    if status == ApplicationStatus.APPLIED:
        updates["applied_date"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [app_id]

    conn.execute(f"UPDATE applications SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()


def add_note(app_id: int, note: str):
    """Append a note to an application."""
    conn = get_db()
    row = conn.execute(
        "SELECT notes FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row:
        existing = row["notes"]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_notes = f"{existing}\n[{timestamp}] {note}" if existing else f"[{timestamp}] {note}"
        conn.execute(
            "UPDATE applications SET notes = ?, updated_at = ? WHERE id = ?",
            (new_notes, datetime.now().isoformat(), app_id),
        )
    conn.commit()
    conn.close()


def list_applications(
        status: ApplicationStatus = None, limit: int = 50
) -> list[dict]:
    """List applications, optionally filtered by status."""
    conn = get_db()
    query = "SELECT * FROM applications"
    params = []

    if status:
        query += " WHERE status = ?"
        params.append(status.value)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_stats() -> dict:
    """Get application statistics."""
    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) as c FROM applications").fetchone()["c"]

    by_status = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) as c FROM applications GROUP BY status"
    ).fetchall():
        by_status[row["status"]] = row["c"]

    avg_score = conn.execute(
        "SELECT AVG(fit_score) as avg FROM applications"
    ).fetchone()["avg"]

    conn.close()

    return {
        "total_applications": total,
        "by_status": by_status,
        "average_fit_score": round(avg_score, 1) if avg_score else 0,
    }


def print_dashboard():
    """Print a simple application dashboard."""
    stats = get_stats()
    apps = list_applications(limit=10)

    print("\n" + "=" * 60)
    print(" APPLICATION DASHBOARD")
    print("=" * 60)
    print(f"Total applications: {stats['total_applications']}")
    print(f"Average fit score: {stats['average_fit_score']}")
    print(f"By status: {json.dumps(stats['by_status'], indent=2)}")

    if apps:
        print(f"\n{'ID':<4} {'Company':<20} {'Role':<25} {'Score':<6} {'Status':<10}")
        print("-" * 65)
        for app in apps:
            print(
                f"{app['id']:<4} {app['company'][:19]:<20} "
                f"{app['title'][:24]:<25} {app['fit_score']:<6} "
                f"{app['status']:<10}"
            )
    print()
