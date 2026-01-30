import json
import os
import re
import sqlite3
from datetime import datetime
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
                cucumber_json TEXT
            )
            """
        )
        conn.commit()


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _list_builds():
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT build_id, job_name, build_number, build_url, branch, commit_sha,
                   report_name, overall_status, created_at
            FROM builds
            ORDER BY created_at DESC
            """
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
    return builds


def _load_cucumber_from_db(build_id):
    with _get_db() as conn:
        row = conn.execute(
            "SELECT cucumber_json FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
    if not row:
        return []
    return json.loads(row["cucumber_json"])


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


def _summarize_report(cucumber_json):
    summary = {
        "features": 0,
        "scenarios": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "steps": 0,
    }

    features = []
    for feature in cucumber_json:
        elements = feature.get("elements", []) or []
        scenarios = []
        feature_counts = {"passed": 0, "failed": 0, "skipped": 0}
        for element in elements:
            steps = element.get("steps", []) or []
            status = _scenario_status(steps)
            summary["scenarios"] += 1
            summary["steps"] += len(steps)
            if status == "passed":
                summary["passed"] += 1
                feature_counts["passed"] += 1
            elif status == "failed":
                summary["failed"] += 1
                feature_counts["failed"] += 1
            else:
                summary["skipped"] += 1
                feature_counts["skipped"] += 1
            scenarios.append(
                {
                    "name": element.get("name", "Unnamed scenario"),
                    "status": status,
                    "steps": steps,
                }
            )

        summary["features"] += 1
        total = max(sum(feature_counts.values()), 1)
        features.append(
            {
                "name": feature.get("name", "Unnamed feature"),
                "description": feature.get("description", ""),
                "tags": [tag.get("name") for tag in feature.get("tags", []) if tag.get("name")],
                "scenarios": scenarios,
                "counts": feature_counts,
                "percent_passed": round((feature_counts["passed"] / total) * 100),
                "percent_failed": round((feature_counts["failed"] / total) * 100),
                "percent_skipped": round((feature_counts["skipped"] / total) * 100),
            }
        )

    return summary, features


@app.route("/")
def index():
    return render_template("index.html", builds=_list_builds())


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

    summary, _ = _summarize_report(json.loads(cucumber_text))
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
        conn.execute(
            """
            INSERT OR REPLACE INTO builds (
                build_id, job_name, build_number, build_url, branch, commit_sha,
                report_name, overall_status, created_at, cucumber_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                cucumber_text,
            ),
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
    cucumber_json = json.loads(metadata.get("cucumber_json") or "[]")
    metadata.pop("cucumber_json", None)
    summary, features = _summarize_report(cucumber_json)
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
    return jsonify(_list_builds())


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
