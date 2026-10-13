# Deploying Talent 360i (v3)

Three ways to run the same code, from a laptop to the whole company. The production
target is the Azure design in *Talent 360i — Production-Scale Solution & Tech Stack*
(Oct 7, 2026); this guide covers what v3 adds to it and how to run it before that
platform exists.

| Tier | Who it's for | How | Scale |
|---|---|---|---|
| Laptop | Demos, development | `python start.py` | one team |
| Single host | Pilot / UAT | `docker compose up` (this folder) | a few thousand people |
| Azure stamp | Production | Container Apps + PostgreSQL + Service Bus | 20,000–40,000+ people |

## Laptop

```bash
python start.py            # API :8000, portal :8010, lifecycle worker
python start.py --setup    # build data + seed v3 demo data only
```

`start.py` now also runs `scripts/seed_lifecycle.py` (idempotent demo data: departments,
three new joiners on probation, evidence, reviews, development plans, pulse check-ins,
coding challenges, trends) and starts `scripts/lifecycle_worker.py` alongside the two
servers (`--no-worker` to skip it).

## Single host (Docker Compose)

```bash
cp deploy/.env.example deploy/.env    # fill in secrets + CIS key
docker compose up --build             # https://localhost:8443
```

| Service | Role |
|---|---|
| `init` | Builds reference data, the role index and demo accounts once, then exits |
| `api` | Pipeline API (agents A1–A7), private network only |
| `portal` | Web app, 2 uvicorn workers |
| `worker` | Lifecycle worker: probation from hire dates, status refresh, reminders |
| `edge` | Caddy: TLS, compression; the only published port |

The database is SQLite in WAL mode on a named volume shared by the containers on one
host. That is right for a pilot; it is **not** a multi-host design (see below).

> Not built in this environment (no container runtime was available). The commands each
> service runs were verified directly; build and smoke-test the image in CI before use.

## Production at 20,000+ (Azure)

Same image, three roles, on the stamp from the Production-Scale document:

| v3 component | Production home | Notes |
|---|---|---|
| Portal, pipeline API | Azure Container Apps, autoscale on HTTP | Stateless: sessions are signed cookies. Move login lockout and rate limits to Azure Managed Redis (they are per-process today) |
| Lifecycle worker | Container Apps **job** on a schedule (every 5 min) plus a nightly full pass | `python scripts/lifecycle_worker.py --once`; idempotent, safe to overlap |
| A6 insight, A7 classify, A4 code review | Agent workers behind Service Bus, KEDA-scaled | All three already have deterministic fallbacks, so a queue backlog degrades to templates, never to errors |
| `employee_status` snapshot + `employee_signals` | PostgreSQL tables (same shape), or a materialised view per department for leader aggregates | The read model is what keeps org pages O(page) |
| Evidence excerpts | PostgreSQL with row-level security by manager / department | Raw messages and attachments are never stored |
| Notifications outbox | Service Bus topic → Graph `sendMail` / Teams activity feed | In-app notifications already work without it |
| HR attributes (hire date, department) | Workday sync (nightly + hourly delta) into `employee_profiles` | Drives probation start and leader scope |

### Evidence from Outlook and Teams (no mailbox scanning)

1. Create an Outlook category **Talent360i evidence** (or a Teams message action).
2. A Power Automate flow, *When an email is categorised* / *For a selected message*, posts
   to `POST /integrations/evidence` with `Authorization: Bearer $TALENT360_INTEGRATION_TOKEN`:

   ```json
   {"employee_email": "...", "tagged_by_email": "...", "source": "outlook",
    "subject": "...", "body": "...", "sender": "...", "sent_at": "2026-10-05"}
   ```
3. Talent 360i checks the tagger is the employee's manager, masks emails and phone
   numbers, keeps a short excerpt, asks Agent 7 for a suggestion, and puts it in the
   manager's *Needs your attention* list. Nothing counts until the manager confirms it.

The flow runs as the manager; it needs no tenant-wide mail permissions.

## Measured scale (one laptop, one SQLite file)

`scripts/generate_scale_dataset.py` builds a synthetic organisation on the real role and
skill catalogue; `scripts/benchmark_scale.py` measures it. Results are in
`docs/scale_benchmark.json` (20,000) and `docs/scale_benchmark_40k.json` (40,000).

| | 20,000 employees | 40,000 employees |
|---|---|---|
| Recalculate everyone (worker full pass) | 9.0 s | 20.8 s |
| One employee's status (every write) | 21 ms | 28 ms |
| Directory page, any filter (DB) | 1–9 ms | 1–14 ms |
| Leader people directory (HTTP p50) | 25 ms | 45 ms |
| Leader performance overview (HTTP p50) | 55 ms | 103 ms |
| 360 profile (HTTP p50) | 49 ms | 65 ms |
| Manager dashboard (HTTP p50) | 43 ms | 64 ms |

Per-person and per-team work stays flat as the organisation grows; only the full
recalculation is linear, and it runs in the background. Reproduce:

```bash
python scripts/generate_scale_dataset.py --employees 20000
python scripts/benchmark_scale.py --db db/scale_20k.sqlite
TALENT360_DB_PATH=db/scale_20k.sqlite python start.py     # click around 20k people
```

## Health, logs, backups

- `GET /healthz` (liveness) and `GET /readyz` (database reachable) on both servers, no auth.
- Audit and LLM logs are JSONL under `TALENT360_LOG_DIR`; ship them to Log Analytics.
- SQLite backup while running: `sqlite3 db/talent360i.sqlite ".backup backup.sqlite"`.
  In production, PostgreSQL point-in-time restore replaces this.
