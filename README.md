# lavenbloom-notification-service

> **Runbook & Developer Walkthrough** — Event-driven notification and alerting microservice for the Lavenbloom platform.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [API Reference](#api-reference)
4. [Environment Variables](#environment-variables)
5. [Local Development Walkthrough](#local-development-walkthrough)
6. [Docker Walkthrough](#docker-walkthrough)
7. [Worker Thread Deep-Dive](#worker-thread-deep-dive)
8. [CI/CD Pipeline Walkthrough](#cicd-pipeline-walkthrough)
9. [Kubernetes Deployment](#kubernetes-deployment)
10. [Secrets Management](#secrets-management)
11. [Troubleshooting](#troubleshooting)

---

## Overview

`notification-service` is a **FastAPI** microservice with a built-in **background worker thread** that consumes missed-habit events from a Redis queue and persists them as notification logs in PostgreSQL.

Unlike other services, `notification-service` has **two operational components**:

1. **API server** (`uvicorn`) — serves `GET /health` and `GET /logs` endpoints
2. **Redis worker thread** — runs `BRPOP missed_habits_queue` in a blocking loop, processes events, and writes to the `notification_logs` table

This implements an **event-driven architecture**: `habit-service` publishes events; `notification-service` consumes them asynchronously, enabling full decoupling between the two services.

| Property | Value |
|---|---|
| **Runtime** | Python 3.11 |
| **Framework** | FastAPI |
| **Database** | PostgreSQL 15 (`notification_db`) |
| **Message Queue** | Redis 7 (`missed_habits_queue`) |
| **Port** | `8000` |
| **Docker image** | `lavenbloom/lavenbloom-notification-service` |

---

## Architecture

```
  habit-service
      │
      │  redis.lpush("missed_habits_queue", {...})
      ▼
 ┌───────────┐
 │  Redis    │  missed_habits_queue
 │  :6379    │
 └─────┬─────┘
       │ BRPOP (blocking pop, 5s timeout)
       ▼
 ┌─────────────────────────────────────────────┐
 │           notification-service              │
 │                                             │
 │   ┌─────────────────────┐   ┌────────────┐  │
 │   │   Worker Thread     │   │ FastAPI    │  │
 │   │  (start on startup) │   │ API :8000  │  │
 │   │                     │   │            │  │
 │   │  BRPOP loop         │   │ GET /health│  │
 │   │  → parse JSON       │   │ GET /logs  │  │
 │   │  → write to DB      │   │            │  │
 │   └──────────┬──────────┘   └────────────┘  │
 └──────────────┼──────────────────────────────┘
                │ SQLAlchemy INSERT
                ▼
 ┌──────────────────────────┐
 │   PostgreSQL             │
 │   notification_logs      │
 │   (notification_db)      │
 └──────────────────────────┘
```

### Source Layout

```
notification-service/
├── app/
│   ├── __init__.py
│   ├── main.py       # FastAPI app + startup event (starts worker thread)
│   ├── worker.py     # Redis consumer loop (start_worker_thread)
│   ├── database.py   # SQLAlchemy engine + session factory
│   ├── models.py     # NotificationLog ORM model
│   └── schemas.py    # Pydantic response schema
├── Dockerfile
├── requirements.txt
└── sonar-project.properties
```

### Data Model

`notification_logs` table (`notification_db`):

| Column | Type | Notes |
|---|---|---|
| `id` | Integer (PK) | Auto-increment |
| `user_id` | Integer | From Redis event payload |
| `habit_name` | String | Name of the missed habit |
| `message` | String | Human-readable notification message |
| `sent_at` | DateTime | UTC timestamp of log creation |
| `success` | Boolean | `True` on successful processing |

---

## API Reference

### `GET /health`

Returns service liveness status. Does **not** require authentication.

```bash
curl http://localhost:8004/health
# {"status":"ok"}
```

---

### `GET /logs`

Returns all notification log entries ordered by `sent_at` descending. Does not require authentication (internal monitoring endpoint).

```bash
curl http://localhost:8004/logs
```

**Success — `200 OK`:**
```json
[
  {
    "id": 3,
    "user_id": 42,
    "habit_name": "Morning Run",
    "message": "User 42 missed habit 'Morning Run' on 2025-04-28",
    "sent_at": "2025-04-28T07:40:00",
    "success": true
  }
]
```

> This endpoint is useful for auditing which missed-habit events were processed and when.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_URI` | ✅ | `postgresql://user:password@localhost/notification_db` | PostgreSQL connection string for log persistence |
| `REDIS_URI` | ✅ | `redis://localhost:6379/0` | Redis URI for consuming `missed_habits_queue` |
| `JWT_SECRET` | ❌ | `supersecretjwtkey` | Present in Secret for consistency; `GET /logs` does not enforce auth currently |

---

## Local Development Walkthrough

### Prerequisites

- Python 3.11+
- PostgreSQL 15 (local or Docker)
- Redis 7 (local or Docker)
- `habit-service` running (to produce events)

### Step 1 — Clone and install

```bash
git clone https://github.com/lavenbloom/lavenbloom-notification-service.git
cd lavenbloom-notification-service
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2 — Start dependencies

```bash
# PostgreSQL for notification logs
docker run -d --name notification-db \
  -e POSTGRES_USER=user \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=notification_db \
  -p 5435:5432 postgres:15-alpine

# Redis (shared with habit-service)
docker run -d --name redis -p 6379:6379 redis:7-alpine
```

### Step 3 — Set environment variables

```bash
# macOS/Linux
export POSTGRES_URI="postgresql://user:password@localhost:5435/notification_db"
export REDIS_URI="redis://localhost:6379/0"

# Windows PowerShell
$env:POSTGRES_URI = "postgresql://user:password@localhost:5435/notification_db"
$env:REDIS_URI    = "redis://localhost:6379/0"
```

### Step 4 — Run the service

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

On startup you will see the worker thread begin:

```
INFO:     Application startup complete.
INFO:     Worker thread started — listening on missed_habits_queue
```

Swagger UI: `http://localhost:8000/docs`

### Step 5 — End-to-end test

```bash
# Push a test event directly to Redis (simulates habit-service)
docker exec -it redis redis-cli LPUSH missed_habits_queue \
  '{"user_id": 1, "habit_name": "Morning Run", "date": "2025-04-28"}'

# After a few seconds, check the notification logs
curl http://localhost:8000/logs
```

---

## Docker Walkthrough

### Build the image

```bash
docker build -t lavenbloom-notification-service:local .
```

### Run standalone

```bash
docker run -d \
  -e POSTGRES_URI="postgresql://user:password@host.docker.internal:5435/notification_db" \
  -e REDIS_URI="redis://host.docker.internal:6379/0" \
  -p 8004:8000 \
  lavenbloom-notification-service:local
```

### Full stack via Docker Compose

```bash
# From project root
docker compose up notification-db redis notification-service
```

Service available at `http://localhost:8004`.

---

## Worker Thread Deep-Dive

The worker is started during FastAPI's `startup` event:

```python
@app.on_event("startup")
def startup_event():
    start_worker_thread()
```

`start_worker_thread()` (in `worker.py`) launches a **daemon thread** that runs the following loop:

```
while True:
    event = redis.brpop("missed_habits_queue", timeout=5)
    if event:
        data = json.loads(event[1])
        # Write NotificationLog row to PostgreSQL
        db.add(NotificationLog(
            user_id=data["user_id"],
            habit_name=data["habit_name"],
            message=f"User {data['user_id']} missed habit '{data['habit_name']}' on {data['date']}",
            success=True
        ))
        db.commit()
```

**Key design properties:**
- **`BRPOP` with 5s timeout** — the thread wakes up every 5 seconds even if the queue is empty, preventing CPU spin
- **Daemon thread** — automatically killed when the main process exits (no cleanup required)
- **Separate DB session** — the worker creates its own SQLAlchemy session, isolated from API request sessions

### In Kubernetes — separate worker Deployment

In the Kubernetes Helm chart, the worker is deployed as a **separate Deployment** (`notification-worker-deployment.yaml`) alongside the API Deployment. This provides:
- Independent scaling of the worker
- Independent resource limits
- Independent crash recovery (Kubernetes restarts the pod if the worker dies)

```bash
# Check worker deployment separately
kubectl get pods -n backend -l app=notification-worker
kubectl logs -n backend deployment/notification-worker -f
```

---

## CI/CD Pipeline Walkthrough

Pipeline file: `.github/workflows/ci-notification-service.yml`

| Event | Jobs triggered |
|---|---|
| Pull Request → `develop` / `main` | `sast` → `sca` → `trivy` → `pr-check` |
| Push → `develop` | `dev-publish` → `dev-cd` |
| GitHub Release created | `publish` → `cd` |

### Shared workflows used

| Job | Shared workflow | Description |
|---|---|---|
| `sast` | `ci-sast.yml` | SonarQube static analysis + quality gate |
| `sca` | `ci-sca.yml` | Snyk Python dependency scan |
| `trivy` | `ci-docker-build.yml` | Temp Docker build + CVE scan |
| `pr-check` | — | Branch-protection gate |
| `dev-publish` | `ci-docker-publish.yml` | Push `dev-{SHA}` image to Docker Hub |
| `dev-cd` | `cd-template.yml` | Update `values-dev.yaml`, ArgoCD syncs dev |
| `publish` | `ci-docker-publish.yml` | Push semver image on Release |
| `cd` | `cd-template.yml` | Update `values-prod.yaml`, ArgoCD syncs prod |

### Required GitHub Secrets

| Secret | Description |
|---|---|
| `SONAR_TOKEN` | SonarQube token |
| `SONAR_URL` | SonarQube server URL |
| `SNYK_TOKEN` | Snyk API token |
| `DOCKER_USERNAME` | Docker Hub username |
| `DOCKER_PASSWORD` | Docker Hub password or token |
| `HELM_REPO_PAT` | PAT for `lavenbloom-charts` push access |

---

## Kubernetes Deployment

`notification-service` uses **two Deployments** in the `backend` namespace:
- `notification-service` — FastAPI API server
- `notification-worker` — Redis consumer worker

### Helm install (dev)

```bash
helm install notification-service ./microservices/notification-service -f values-dev.yaml
```

### Verify both deployments

```bash
# API server
kubectl get pods -n backend -l app=notification-service
kubectl logs -n backend deployment/notification-service

# Worker
kubectl get pods -n backend -l app=notification-worker
kubectl logs -n backend deployment/notification-worker -f

# Port-forward API for testing
kubectl port-forward -n backend deployment/notification-service 8004:8000
curl http://localhost:8004/logs
```

### Check secrets

```bash
kubectl get secret notification-service-secret -n backend -o jsonpath='{.data.REDIS_URI}' | base64 -d
kubectl get secret notification-service-secret -n backend -o jsonpath='{.data.POSTGRES_URI}' | base64 -d
```

### Network policy

- `notification-service` (API) → `notification-db`: **allowed**
- `notification-worker` → Redis: **allowed**
- `notification-worker` → `notification-db`: **allowed**
- Gateway → `notification-service` (API): **allowed**
- All other traffic: **denied**

---

## Secrets Management

| Secret Name (K8s) | Keys | Notes |
|---|---|---|
| `notification-service-secret` | `POSTGRES_URI`, `REDIS_URI`, `JWT_SECRET` | Shared by both the API and worker Deployments |
| `notification-db-secret` | `POSTGRES_USER`, `POSTGRES_PASSWORD` | Used by the PostgreSQL StatefulSet |

---

## Troubleshooting

### Worker thread not starting

Check the API pod logs for the startup event:

```bash
kubectl logs -n backend deployment/notification-service | grep -i worker
```

If you see no worker log line, the `startup_event` may have failed. Look for Python import errors or connection errors to Redis.

### Events pushed to Redis but no logs appear

1. **Verify the queue has items:**
   ```bash
   kubectl exec -n backend deployment/notification-worker -- redis-cli -u $REDIS_URI LLEN missed_habits_queue
   ```
2. **Check worker logs** for errors during `BRPOP` or DB write:
   ```bash
   kubectl logs -n backend deployment/notification-worker -f
   ```
3. **Verify `REDIS_URI`** in the secret matches the Redis service DNS:
   ```bash
   kubectl get secret notification-service-secret -n backend -o jsonpath='{.data.REDIS_URI}' | base64 -d
   # Should be: redis://redis:6379/0
   ```

### `GET /logs` returns an empty array

This is expected if no missed-habit events have been processed yet. Trigger one by marking a habit as `is_done: false` via `habit-service`, then re-check.

### PostgreSQL connection errors in worker

The `notification-db` init Job must complete before the worker tries to write. Verify:

```bash
kubectl get jobs -n db | grep notification
kubectl logs job/notification-db-init -n db