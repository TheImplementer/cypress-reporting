import json
import os
import re
from datetime import datetime, timezone
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

app = Flask(__name__)


def _safe_id(raw):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-")
    return cleaned or str(uuid4())


def _load_metadata(build_dir):
    meta_path = build_dir / "metadata.json"
    if not meta_path.exists():
        return {}
    with meta_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _list_builds():
    if not DATA_DIR.exists():
        return []

    builds = []
    for entry in DATA_DIR.iterdir():
        if not entry.is_dir():
            continue
        metadata = _load_metadata(entry)
        if metadata:
            created_at = metadata.get("created_at", "")
            try:
                parsed = datetime.fromisoformat(created_at)
                metadata["display_created_at"] = parsed.replace(microsecond=0).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                metadata["display_created_at"] = created_at
            if "overall_status" not in metadata:
                cucumber_json = _load_cucumber(entry)
                summary, _ = _summarize_report(cucumber_json)
                if summary["failed"] > 0:
                    metadata["overall_status"] = "failed"
                elif summary["skipped"] > 0 and summary["passed"] == 0:
                    metadata["overall_status"] = "skipped"
                else:
                    metadata["overall_status"] = "passed"
            metadata["build_id"] = entry.name
            builds.append(metadata)
    builds.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return builds


def _load_cucumber(build_dir):
    cucumber_path = build_dir / "cucumber.json"
    if not cucumber_path.exists():
        return []
    with cucumber_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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
            scenarios.append(
                {
                    "name": element.get("name", "Unnamed scenario"),
                    "status": status,
                    "steps": steps,
                }
            )

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
    report_name = payload.get("report_name") or f"{job_name} #{build_number}".strip() or "Cypress Report"

    if build_id:
        build_id = _safe_id(build_id)
    else:
        seed = f"{job_name}-{build_number}".strip("-")
        build_id = _safe_id(seed) if seed else _safe_id(str(uuid4()))

    cucumber_file = request.files.get("cucumber_json") if request.files else None
    cucumber_text = None

    if cucumber_file:
        cucumber_text = cucumber_file.read().decode("utf-8")
    elif request.is_json:
        cucumber_text = (request.json or {}).get("cucumber_json")

    if not cucumber_text:
        abort(400, "Missing cucumber_json")

    build_dir = DATA_DIR / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

    cucumber_path = build_dir / "cucumber.json"
    cucumber_path.write_text(cucumber_text, encoding="utf-8")

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
        "commit": payload.get("commit", ""),
        "report_name": report_name,
        "overall_status": overall_status,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (build_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    if request.is_json:
        return jsonify({"build_id": build_id, "report_url": f"/reports/{build_id}/"})
    return redirect(url_for("report_index", build_id=build_id))


@app.route("/reports/<build_id>/")
def report_index(build_id):
    build_dir = DATA_DIR / build_id
    if not build_dir.exists():
        abort(404)
    cucumber_json = _load_cucumber(build_dir)
    summary, features = _summarize_report(cucumber_json)
    metadata = _load_metadata(build_dir)
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
    build_dir = DATA_DIR / build_id
    if not build_dir.exists():
        abort(404)
    return jsonify(_load_metadata(build_dir))


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "6002")))
