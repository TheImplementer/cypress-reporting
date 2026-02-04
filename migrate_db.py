import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

APP_ROOT = Path(__file__).parent.resolve()
DATA_DIR = Path(os.getenv("RESULTS_DATA_DIR", APP_ROOT / "data")).resolve()
SRC_DB = Path(os.getenv("RESULTS_DB_PATH", DATA_DIR / "results.db")).resolve()
DEST_DB = Path(os.getenv("RESULTS_DB_PATH_NEW", DATA_DIR / "results_v2.db")).resolve()


def _scenario_status(steps):
    statuses = [step.get("result", {}).get("status", "unknown") for step in steps]
    if "failed" in statuses:
        return "failed"
    if "skipped" in statuses or "pending" in statuses or "undefined" in statuses:
        return "skipped"
    if statuses:
        return "passed"
    return "unknown"


def _parse_cucumber(cucumber_text):
    try:
        data = json.loads(cucumber_text)
    except (TypeError, ValueError):
        data = []

    summary = {
        "features": 0,
        "scenarios": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "steps": 0,
    }

    features = []
    for feature in data:
        elements = feature.get("elements", []) or []
        scenarios = []
        for element in elements:
            steps = element.get("steps", []) or []
            status = _scenario_status(steps)
            summary["scenarios"] += 1
            summary["steps"] += len(steps)
            if status == "passed":
                summary["passed"] += 1
            elif status == "failed":
                summary["failed"] += 1
            else:
                summary["skipped"] += 1
            scenarios.append({"name": element.get("name", "Unnamed scenario"), "status": status})

        summary["features"] += 1
        features.append(
            {
                "name": feature.get("name", "Unnamed feature"),
                "description": feature.get("description", ""),
                "tags": [tag.get("name") for tag in feature.get("tags", []) if tag.get("name")],
                "scenarios": scenarios,
            }
        )

    return summary, features


def _create_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS builds (
            build_id TEXT PRIMARY KEY,
            job_name TEXT,
            build_number TEXT,
            build_url TEXT,
            branch TEXT,
            commit_sha TEXT,
            report_name TEXT,
            overall_status TEXT,
            created_at TEXT,
            features_total INTEGER,
            scenarios_total INTEGER,
            passed_total INTEGER,
            failed_total INTEGER,
            skipped_total INTEGER,
            steps_total INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            build_id TEXT,
            name TEXT,
            description TEXT,
            tags TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scenarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_id INTEGER,
            name TEXT,
            status TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_builds_created_at ON builds(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_builds_status ON builds(overall_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_features_build ON features(build_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scenarios_feature ON scenarios(feature_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scenarios_status ON scenarios(status)")


def _has_column(conn, table, column):
    columns = conn.execute("PRAGMA table_info({})".format(table)).fetchall()
    for col in columns:
        if col[1] == column:
            return True
    return False


def _copy_normalized(src, dest):
    src.row_factory = sqlite3.Row
    dest.row_factory = sqlite3.Row
    _create_schema(dest)
    rows = src.execute("SELECT * FROM builds").fetchall()
    for row in rows:
        dest.execute(
            """
            INSERT OR REPLACE INTO builds (
                build_id, job_name, build_number, build_url, branch, commit_sha,
                report_name, overall_status, created_at, features_total, scenarios_total,
                passed_total, failed_total, skipped_total, steps_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["build_id"],
                row["job_name"],
                row["build_number"],
                row["build_url"],
                row["branch"],
                row["commit_sha"],
                row["report_name"],
                row["overall_status"],
                row["created_at"],
                row["features_total"],
                row["scenarios_total"],
                row["passed_total"],
                row["failed_total"],
                row["skipped_total"],
                row["steps_total"],
            ),
        )
    feature_rows = src.execute("SELECT * FROM features").fetchall()
    for row in feature_rows:
        dest.execute(
            "INSERT INTO features (id, build_id, name, description, tags) VALUES (?, ?, ?, ?, ?)",
            (row["id"], row["build_id"], row["name"], row["description"], row["tags"]),
        )
    scenario_rows = src.execute("SELECT * FROM scenarios").fetchall()
    for row in scenario_rows:
        dest.execute(
            "INSERT INTO scenarios (id, feature_id, name, status) VALUES (?, ?, ?, ?)",
            (row["id"], row["feature_id"], row["name"], row["status"]),
        )


def _migrate_legacy(src, dest):
    src.row_factory = sqlite3.Row
    _create_schema(dest)
    rows = src.execute("SELECT * FROM builds").fetchall()
    for row in rows:
        cucumber_text = row["cucumber_json"] or "[]"
        summary, features = _parse_cucumber(cucumber_text)
        dest.execute(
            """
            INSERT OR REPLACE INTO builds (
                build_id, job_name, build_number, build_url, branch, commit_sha,
                report_name, overall_status, created_at, features_total, scenarios_total,
                passed_total, failed_total, skipped_total, steps_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["build_id"],
                row["job_name"],
                row["build_number"],
                row["build_url"],
                row["branch"],
                row["commit_sha"],
                row["report_name"],
                row["overall_status"],
                row["created_at"],
                summary["features"],
                summary["scenarios"],
                summary["passed"],
                summary["failed"],
                summary["skipped"],
                summary["steps"],
            ),
        )
        for feature in features:
            cursor = dest.execute(
                "INSERT INTO features (build_id, name, description, tags) VALUES (?, ?, ?, ?)",
                (row["build_id"], feature["name"], feature["description"], json.dumps(feature["tags"])),
            )
            feature_id = cursor.lastrowid
            for scenario in feature["scenarios"]:
                dest.execute(
                    "INSERT INTO scenarios (feature_id, name, status) VALUES (?, ?, ?)",
                    (feature_id, scenario["name"], scenario["status"]),
                )


def main():
    if not SRC_DB.exists():
        raise SystemExit("Source DB not found: {}".format(SRC_DB))
    if DEST_DB.exists():
        raise SystemExit("Destination DB already exists: {}".format(DEST_DB))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(SRC_DB) as src_conn, sqlite3.connect(DEST_DB) as dest_conn:
        if _has_column(src_conn, "builds", "cucumber_json"):
            _migrate_legacy(src_conn, dest_conn)
        else:
            _copy_normalized(src_conn, dest_conn)
        dest_conn.commit()

    print("Migrated to {}".format(DEST_DB))


if __name__ == "__main__":
    main()
