# Cypress Results Hub

Small Flask service that stores Cypress Cucumber JSON results per Jenkins build and renders HTML reports.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

## Upload results

### Multipart form

```bash
curl -X POST http://localhost:5000/upload \
  -F cucumber_json=@cypress/results/cucumber.json \
  -F job_name=ui-tests \
  -F build_number=123 \
  -F build_url=https://jenkins/job/ui-tests/123/ \
  -F branch=main \
  -F commit=abc123
```

### JSON payload

```bash
curl -X POST http://localhost:5000/upload \
  -H "Content-Type: application/json" \
  -d '{
    "cucumber_json": "[ ... ]",
    "job_name": "ui-tests",
    "build_number": "123"
  }'
```

## Docker

```bash
docker build -t cypress-results-hub .
docker run -p 5000:5000 -v $PWD/data:/app/data cypress-results-hub
```

## Kubernetes

Apply manifests (adjust image + host as needed):

```bash
kubectl apply -k k8s
```

Defaults:
- Image: `cypress-reporting:latest` (edit `k8s/deployment.yaml`)
- Ingress host: `cypress-reporting.local` (edit `k8s/ingress.yaml`)
- PVC size: `5Gi` (edit `k8s/pvc.yaml`)

## API

- `GET /api/builds`
- `GET /api/builds/<build_id>`
- `POST /upload` (form or JSON)

Data is stored in a SQLite database at `data/results.db` (configurable via `RESULTS_DB_PATH`).
