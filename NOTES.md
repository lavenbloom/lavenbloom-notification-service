# notification-service — Study Notes

---

## Table of Contents

1. [How This Service Works](#1-how-this-service-works)
2. [Dockerfile — Line by Line](#2-dockerfile--line-by-line)
3. [Q&A](#3-qa)

---

## 1. How This Service Works

### What it does

The notification-service has two responsibilities:

1. **Background worker** — a continuously running thread that watches a Redis queue (`missed_habits`) and processes missed-habit events published by habit-service. For each event, it logs a notification record to PostgreSQL.

2. **Notification log API** — an HTTP API that lets clients retrieve the history of all notifications that were sent.

### Why a background worker inside a web service?

Rather than running a separate worker process or a separate Kubernetes Job, the notification-service runs a Python `threading.Thread` alongside the FastAPI HTTP server. When FastAPI starts (via the `@app.on_event("startup")` lifecycle hook), it spawns the worker thread. The thread runs forever in the background, continuously polling Redis.

**Trade-off:** Simpler to deploy (one container = one process for Kubernetes to manage), but the worker and the HTTP server share memory and CPU. In high-throughput systems, you'd run them as separate deployments (a `worker` deployment and an `api` deployment) scaled independently.

### API routes

| Method | Path | What it does |
|---|---|---|
| `GET` | `/health` | Health check for Kubernetes |
| `GET` | `/notifications` | Returns all notification log entries, ordered by `sent_at` |

### The worker thread — step by step

```python
def worker():
    while True:
        # blpop: block until an item appears in the queue
        result = redis_client.blpop("missed_habits", timeout=30)
        if result:
            _, data = result   # result = (queue_name, value)
            event = json.loads(data)
            # Write notification log to PostgreSQL
            log = NotificationLog(
                user_id=event["user_id"],
                habit_name=event["habit_name"],
                message=f"You missed: {event['habit_name']}",
                success=True
            )
            db.add(log)
            db.commit()
```

**`blpop` (blocking left-pop):** The thread calls `blpop` with a `timeout=30`. If the queue is empty, the thread sleeps for up to 30 seconds. If a message arrives within those 30 seconds, it's immediately returned. If timeout expires with no message, `blpop` returns `None` and the loop continues. This is efficient — the thread does NOT busy-wait (no CPU burn while idle).

**Why `timeout=30` instead of infinite?** A timeout of 30 seconds allows the loop to re-check Redis connection health and recover from transient network issues without requiring an explicit reconnection mechanism.

### The data model

`NotificationLog` (from `models.py`):
- `id` — auto-generated primary key
- `user_id` — which user had the missed habit
- `habit_name` — name of the missed habit
- `message` — human-readable notification text
- `sent_at` — timestamp of when the notification was processed
- `success` — boolean: was the notification successfully recorded?

### How it connects to other components

```
habit-service → rpush("missed_habits", JSON) → Redis:6379
                                                    ↓ blpop (blocking)
                                          notification-service worker thread
                                                    ↓
                                            PostgreSQL notification-db:5432
                                                    ↑
                          User → Gateway → /notifications → API reads DB
```

No JWT authentication on API routes currently (it reads notification logs — could be a monitoring endpoint). In a production system, you'd add auth middleware to restrict access.

---

## 2. Dockerfile — Line by Line

```dockerfile
FROM python:3.11-slim
```
Same Debian-based Python 3.11 slim image used by all backend services. Consistency across services reduces cognitive load for operators — one base image means one set of OS vulnerability patches to track.

---

```dockerfile
WORKDIR /app
```
Working directory for all subsequent build instructions. `/app` is created automatically. This is the directory where uvicorn looks for `app.main:app`.

---

```dockerfile
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*
```
Build-time dependencies:
- `gcc` — C compiler for `psycopg2` compilation
- `libpq-dev` — PostgreSQL client headers for `psycopg2`

These tools are present in the final image (single-stage build) but are not executed at runtime — only during `pip install`. The multi-stage build pattern (see auth-service NOTES) would remove them from the runtime image.

---

```dockerfile
COPY requirements.txt .
```
Copied before `app/` for Docker layer caching. The `pip install` layer (next step) is only invalidated when `requirements.txt` changes — not when `app/*.py` changes. Since dependencies change far less often than code, this saves significant build time.

notification-service `requirements.txt` includes:
- `fastapi`, `uvicorn` — web framework and server
- `sqlalchemy`, `psycopg2` — ORM and PostgreSQL driver
- `redis` — Python Redis client for the `blpop` calls in the worker thread
- *(No `python-jose` — this service has no JWT auth currently)*

---

```dockerfile
RUN pip install --no-cache-dir -r requirements.txt
```
Installs all packages. The `redis` package here is the Python Redis client library (not the Redis server). `blpop`, `rpush`, and other Redis commands in `main.py` come from this library.

`--no-cache-dir`: pip's download cache is useless inside a Docker build — the build environment is discarded after the image is created. Disabling the cache keeps the image layer smaller.

---

```dockerfile
COPY app/ app/
```
Copies `main.py`, `database.py`, `models.py` into `/app/app/`. The worker logic, API routes, database models, and startup event are all in `main.py`. This layer is the "hot" layer — it changes every time a developer edits code.

---

```dockerfile
RUN useradd -m appuser && chown -R appuser /app
USER appuser
```
Security hardening. The notification-service has access to Redis and PostgreSQL. Running as root would mean a vulnerability gives the attacker control over both the queue and the database. `appuser` limits the blast radius.

- `useradd -m appuser` — creates system user with home directory
- `chown -R appuser /app` — ownership of all application files transferred
- `USER appuser` — subsequent operations and container runtime use this user

**Thread context:** The background worker thread inherits the same `appuser` security context as the main process — both run as non-root.

---

```dockerfile
EXPOSE 8000
```
Documents port 8000 as the container's listening port. The worker thread does not listen on any port — it connects *outbound* to Redis. Only the FastAPI HTTP server uses port 8000.

---

```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
Starts FastAPI via Uvicorn. The `@app.on_event("startup")` handler in `main.py` fires when uvicorn initialises the FastAPI app — this is when the background worker thread is started.

```python
@app.on_event("startup")
async def startup_event():
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
```

`daemon=True` — marks the thread as a daemon thread. When the main process (uvicorn) exits (e.g., from SIGTERM), daemon threads are automatically killed without needing explicit cleanup. Non-daemon threads would keep the process alive even after uvicorn tries to exit.

---

## 3. Q&A

**Q: What happens if notification-service restarts while there are messages in the Redis queue?**
A: Messages in Redis persist across restarts. Redis stores the `missed_habits` list in memory (optionally persisted to disk with AOF/RDB). When the notification-service pod restarts, the worker thread reconnects to Redis and resumes reading from where the list left off. No messages are lost because `blpop` only removes a message from the queue after returning it — if the service crashes mid-processing, that message is already removed. For guaranteed exactly-once delivery, you'd use Redis Streams or a more robust queue like RabbitMQ or Kafka.

**Q: Could the worker and the HTTP API be separate containers?**
A: Yes — and that's the production-ready pattern. You'd have two Kubernetes Deployments from the same image: one running uvicorn (API) and one running a Python script (worker, no HTTP server). This allows independent scaling: scale the worker if the queue backs up, scale the API if request volume increases. The current approach (one container, two threads) is simpler but couples the two concerns.

**Q: What is `@app.on_event("startup")` and is it the modern way?**
A: `on_event("startup")` is FastAPI's lifecycle hook that runs code when the application initialises. In newer FastAPI versions (0.93+), `lifespan` context managers are preferred:
```python
from contextlib import asynccontextmanager
@asynccontextmanager
async def lifespan(app: FastAPI):
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    yield
    # cleanup code here
app = FastAPI(lifespan=lifespan)
```
`on_event` still works but is deprecated in recent FastAPI versions.

**Q: Why does the worker use `threading.Thread` instead of `asyncio`?**
A: The Redis `blpop` call in the `redis` library is **synchronous** (blocking). If you ran it inside FastAPI's async event loop directly, it would block the entire loop, making the HTTP API unresponsive. Running it in a separate thread isolates the blocking call from the async loop. The alternative is using `aioredis` (async Redis client) with `await` — this would run natively in the async event loop without a separate thread.

**Q: What is the `sent_at` timestamp recorded when — when the notification was triggered or when it was processed?**
A: It's recorded when the worker processes the event from the queue (not when `habit-service` pushed the event). If the queue backs up (e.g., notification-service was down for an hour), the `sent_at` timestamps reflect when the backlog was processed, not when the habits were actually missed. For accurate missed-at timestamps, the `habit-service` should include the actual missed time in the event payload and the notification-service should store both.
