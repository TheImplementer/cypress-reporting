import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

APP_ROOT = Path(__file__).parent.resolve()
DATA_DIR = Path(os.getenv("RESULTS_DATA_DIR", APP_ROOT / "data")).resolve()
DB_PATH = Path(os.getenv("RESULTS_DB_PATH", DATA_DIR / "results.db")).resolve()

app = Flask(__name__)
_db_ready = False


@app.before_request
def _ensure_db():
    global _db_ready
    if not _db_ready:
        _init_db()
        _db_ready = True


def _safe_id(raw):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-")
    return cleaned or str(uuid4())


def _init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
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
        conn.commit()


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
def _list_builds(page, per_page):
    offset = (page - 1) * per_page
    with _get_db() as conn:
        total = conn.execute("SELECT COUNT(1) AS count FROM builds").fetchone()["count"]
        rows = conn.execute(
            """
            SELECT build_id, job_name, build_number, build_url, branch, commit_sha,
                   report_name, overall_status, created_at
            FROM builds
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()

    builds = []
    for row in rows:
        metadata = dict(row)
        created_at = metadata.get("created_at", "")
        try:
            parsed = datetime.fromisoformat(created_at)
            metadata["display_created_at"] = parsed.replace(microsecond=0).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            metadata["display_created_at"] = created_at
        builds.append(metadata)
    total_pages = max((total + per_page - 1) // per_page, 1)
    return builds, total, total_pages


def _daily_failure_rate(days):
    cutoff = datetime.utcnow() - timedelta(days=days - 1)
    cutoff = cutoff.replace(microsecond=0)
    cutoff_iso = cutoff.isoformat()
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT created_at, overall_status
            FROM builds
            WHERE created_at >= ?
            ORDER BY created_at DESC
            """,
            (cutoff_iso,),
        ).fetchall()

    buckets = {}
    for row in rows:
        created_at = row["created_at"] or ""
        try:
            day_key = datetime.fromisoformat(created_at).strftime("%Y-%m-%d")
        except ValueError:
            continue
        if day_key not in buckets:
            buckets[day_key] = {"total": 0, "failed": 0}
        buckets[day_key]["total"] += 1
        if row["overall_status"] == "failed":
            buckets[day_key]["failed"] += 1

    days_sorted = sorted(buckets.keys(), reverse=True)[:days]
    result = []
    for day in reversed(days_sorted):
        total = buckets[day]["total"]
        failed = buckets[day]["failed"]
        rate = round((failed / total) * 100) if total else 0
        result.append({"day": day, "failed": failed, "total": total, "rate": rate})
    return result


def _flaky_stats(limit, days, max_builds):
    cutoff = datetime.utcnow() - timedelta(days=days - 1)
    cutoff = cutoff.replace(microsecond=0)
    cutoff_iso = cutoff.isoformat()

    with _get_db() as conn:
        build_rows = conn.execute(
            """
            SELECT build_id
            FROM builds
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (cutoff_iso, max_builds),
        ).fetchall()

        build_ids = [row["build_id"] for row in build_rows]
        if not build_ids:
            return [], []

        placeholder = ",".join(["?"] * len(build_ids))
        feature_rows = conn.execute(
            """
            SELECT f.name AS feature_name, s.status AS status, COUNT(*) AS count
            FROM scenarios s
            JOIN features f ON s.feature_id = f.id
            WHERE f.build_id IN ({})
            GROUP BY f.name, s.status
            """.format(placeholder),
            build_ids,
        ).fetchall()

        scenario_rows = conn.execute(
            """
            SELECT f.name AS feature_name, s.name AS scenario_name, s.status AS status, COUNT(*) AS count
            FROM scenarios s
            JOIN features f ON s.feature_id = f.id
            WHERE f.build_id IN ({})
            GROUP BY f.name, s.name, s.status
            """.format(placeholder),
            build_ids,
        ).fetchall()

    feature_counts = {}
    for row in feature_rows:
        name = row["feature_name"]
        if name not in feature_counts:
            feature_counts[name] = {"total": 0, "failed": 0}
        feature_counts[name]["total"] += row["count"]
        if row["status"] == "failed":
            feature_counts[name]["failed"] += row["count"]

    scenario_counts = {}
    for row in scenario_rows:
        name = "{} :: {}".format(row["feature_name"], row["scenario_name"])
        if name not in scenario_counts:
            scenario_counts[name] = {"total": 0, "failed": 0}
        scenario_counts[name]["total"] += row["count"]
        if row["status"] == "failed":
            scenario_counts[name]["failed"] += row["count"]

    def to_ranked(items):
        ranked = []
        for name, stats in items.items():
            total = stats["total"]
            failed = stats["failed"]
            rate = round((failed / total) * 100) if total else 0
            ranked.append({"name": name, "failed": failed, "total": total, "rate": rate})
        ranked.sort(key=lambda item: (item["rate"], item["failed"]), reverse=True)
        return ranked[:limit]

    return to_ranked(feature_counts), to_ranked(scenario_counts)


def _load_build(build_id):
    with _get_db() as conn:
        row = conn.execute("SELECT * FROM builds WHERE build_id = ?", (build_id,)).fetchone()
    return dict(row) if row else None


def _scenario_status(steps):
    statuses = [step.get("result", {}).get("status", "unknown") for step in steps]
    if "failed" in statuses:
        return "failed"
    if "skipped" in statuses or "pending" in statuses or "undefined" in statuses:
        return "skipped"
    if statuses:
        return "passed"
    return "unknown"


def _load_feature_tree(build_id):
    with _get_db() as conn:
        feature_rows = conn.execute(
            """
            SELECT id, name, description, tags
            FROM features
            WHERE build_id = ?
            ORDER BY id
            """,
            (build_id,),
        ).fetchall()

        scenario_rows = conn.execute(
            """
            SELECT feature_id, name, status
            FROM scenarios
            WHERE feature_id IN ({})
            ORDER BY id
            """.format(",".join(["?"] * len(feature_rows)))
            if feature_rows
            else "SELECT feature_id, name, status FROM scenarios WHERE 1=0",
            [row["id"] for row in feature_rows],
        ).fetchall()

    scenario_map = {}
    for row in scenario_rows:
        scenario_map.setdefault(row["feature_id"], []).append(
            {"name": row["name"], "status": row["status"]}
        )

    features = []
    for row in feature_rows:
        scenarios = scenario_map.get(row["id"], [])
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        for scenario in scenarios:
            if scenario["status"] == "passed":
                counts["passed"] += 1
            elif scenario["status"] == "failed":
                counts["failed"] += 1
            else:
                counts["skipped"] += 1
        total = max(sum(counts.values()), 1)
        tags = []
        if row["tags"]:
            try:
                tags = json.loads(row["tags"])
            except (TypeError, ValueError):
                tags = []
        features.append(
            {
                "name": row["name"],
                "description": row["description"] or "",
                "tags": tags,
                "scenarios": scenarios,
                "counts": counts,
                "percent_passed": round((counts["passed"] / total) * 100),
                "percent_failed": round((counts["failed"] / total) * 100),
                "percent_skipped": round((counts["skipped"] / total) * 100),
            }
        )

    return features


@app.route("/")
def index():
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 15))
    builds, total, total_pages = _list_builds(page, per_page)
    return render_template(
        "index.html",
        builds=builds,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
    )


@app.route("/stats")
def stats():
    stats_days = int(request.args.get("stats_days", 7))
    if stats_days not in (7, 14, 30, 90):
        stats_days = 7
    max_builds = int(request.args.get("stats_builds", 200))
    if max_builds < 50:
        max_builds = 50
    if max_builds > 2000:
        max_builds = 2000
    daily = _daily_failure_rate(stats_days)
    top_features, top_scenarios = _flaky_stats(5, stats_days, max_builds)
    return render_template(
        "stats.html",
        stats_days=stats_days,
        stats_builds=max_builds,
        daily=daily,
        top_features=top_features,
        top_scenarios=top_scenarios,
    )


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        abort(404)

    payload = request.form or request.json or {}
    build_id = payload.get("build_id")
    job_name = payload.get("job_name", "")
    build_number = payload.get("build_number", "")
    report_name = payload.get("report_name") or "{} #{}".format(job_name, build_number).strip() or "Cypress Report"

    if build_id:
        build_id = _safe_id(build_id)
    else:
        seed = "{}-{}".format(job_name, build_number).strip("-")
        build_id = _safe_id(seed) if seed else _safe_id(str(uuid4()))

    cucumber_file = request.files.get("cucumber_json") if request.files else None
    cucumber_text = None

    if cucumber_file:
        cucumber_text = cucumber_file.read().decode("utf-8")
    elif request.is_json:
        cucumber_text = (request.json or {}).get("cucumber_json")

    if not cucumber_text:
        abort(400, "Missing cucumber_json")

    summary, features = _parse_cucumber(cucumber_text)
    overall_status = "passed"
    if summary["failed"] > 0:
        overall_status = "failed"
    elif summary["skipped"] > 0 and summary["passed"] == 0:
        overall_status = "skipped"

    metadata = {
        "build_id": build_id,
        "job_name": job_name,
        "build_number": build_number,
        "build_url": payload.get("build_url", ""),
        "branch": payload.get("branch", ""),
        "commit_sha": payload.get("commit_sha") or payload.get("commit", ""),
        "report_name": report_name,
        "overall_status": overall_status,
        "created_at": datetime.utcnow().isoformat(),
    }
    with _get_db() as conn:
        conn.execute("DELETE FROM scenarios WHERE feature_id IN (SELECT id FROM features WHERE build_id = ?)", (metadata["build_id"],))
        conn.execute("DELETE FROM features WHERE build_id = ?", (metadata["build_id"],))
        conn.execute(
            """
            INSERT OR REPLACE INTO builds (
                build_id, job_name, build_number, build_url, branch, commit_sha,
                report_name, overall_status, created_at, features_total, scenarios_total,
                passed_total, failed_total, skipped_total, steps_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metadata["build_id"],
                metadata["job_name"],
                metadata["build_number"],
                metadata["build_url"],
                metadata["branch"],
                metadata["commit_sha"],
                metadata["report_name"],
                metadata["overall_status"],
                metadata["created_at"],
                summary["features"],
                summary["scenarios"],
                summary["passed"],
                summary["failed"],
                summary["skipped"],
                summary["steps"],
            ),
        )
        for feature in features:
            cursor = conn.execute(
                "INSERT INTO features (build_id, name, description, tags) VALUES (?, ?, ?, ?)",
                (
                    metadata["build_id"],
                    feature["name"],
                    feature["description"],
                    json.dumps(feature["tags"]),
                ),
            )
            feature_id = cursor.lastrowid
            for scenario in feature["scenarios"]:
                conn.execute(
                    "INSERT INTO scenarios (feature_id, name, status) VALUES (?, ?, ?)",
                    (feature_id, scenario["name"], scenario["status"]),
                )
        conn.commit()

    if request.is_json:
        return jsonify({"build_id": build_id, "report_url": "/reports/{}/".format(build_id)})
    return redirect(url_for("report_index", build_id=build_id))


@app.route("/reports/<build_id>/")
def report_index(build_id):
    metadata = _load_build(build_id)
    if not metadata:
        abort(404)
    summary = {
        "features": metadata.get("features_total", 0),
        "scenarios": metadata.get("scenarios_total", 0),
        "passed": metadata.get("passed_total", 0),
        "failed": metadata.get("failed_total", 0),
        "skipped": metadata.get("skipped_total", 0),
        "steps": metadata.get("steps_total", 0),
    }
    features = _load_feature_tree(build_id)
    created_at = metadata.get("created_at", "")
    try:
        parsed = datetime.fromisoformat(created_at)
        metadata["display_created_at"] = parsed.replace(microsecond=0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        metadata["display_created_at"] = created_at
    return render_template(
        "report.html",
        build=metadata,
        build_id=build_id,
        summary=summary,
        features=features,
    )


@app.route("/reports/<build_id>/<path:filename>")
def report_assets(build_id, filename):
    abort(404)


@app.route("/api/builds")
def api_builds():
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 15))
    builds, total, total_pages = _list_builds(page, per_page)
    return jsonify({"items": builds, "total": total, "page": page, "total_pages": total_pages})


@app.route("/api/builds/<build_id>")
def api_build(build_id):
    metadata = _load_build(build_id)
    if not metadata:
        abort(404)
    metadata.pop("cucumber_json", None)
    return jsonify(metadata)


if __name__ == "__main__":
    _init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "6002")))
