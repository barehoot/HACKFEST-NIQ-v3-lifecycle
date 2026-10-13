# Phase 11 — Actual Backend Team API Integration

## Source inspected

Backend ZIP: `HACKFEST-NIQ-2`

The supplied backend is a FastAPI application. Its current public API surface is:

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/generate` | Resume upload → role match → blueprint → generated questions |
| POST | `/api/score` | Score generated MCQ items |
| POST | `/api/case-studies` | Generate evidence/case-study questions from score |
| POST | `/api/evidence` | Evaluate written evidence |

The backend also serves its own static demo UI. It does **not** currently expose the full role-specific Talent 360i API contract used by the Jinja prototype (auth, SME queue, scheduler, manager validation, leader analytics, etc.).

Therefore Phase 11 integrates the four APIs that actually exist rather than inventing endpoints.

## Architecture

Browser → Jinja/FastAPI frontend → `services/backend_api.py` → backend team's FastAPI → agents/LLM/SQLite/Chroma.

The browser never receives the backend LLM credentials.

## Configuration

Copy `.env.example` to `.env` and set:

```env
TALENT360_API_MODE=remote
TALENT360_BACKEND_URL=http://127.0.0.1:8000
TALENT360_API_TIMEOUT=60
```

If the backend is hosted elsewhere, use its base URL. Do not put CIS/Groq keys in the frontend `.env`; those belong only to the backend environment.

## Backend startup

From the backend repository:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then run the Jinja frontend on a different port, for example:

```bash
uvicorn app:app --reload --port 8010
```

The frontend calls the backend at `http://127.0.0.1:8000`.

## Integrated adapter methods

`services/backend_api.py` now implements:

- `upload_resume(...)` → `/api/generate` multipart request
- `score(items, answers)` → `/api/score`
- `case_studies(role_id, score, max_questions)` → `/api/case-studies`
- `evidence(...)` → `/api/evidence`

`services/api_client.py` exposes corresponding frontend service functions.

## Important scope boundary

The supplied backend is not yet the complete HLD backend. In particular, this ZIP does not expose the complete API needed for the existing Jinja role dashboards and human gates. Do not remove the current mock service layer until the backend team supplies those APIs.

The HLD still requires the presentation layer to remain FastAPI + Jinja2 + HTMX and describes five human roles and the governed A1–A5 chain. The current backend ZIP implements a narrower assessment pipeline, so the remaining Phase 11 work is contract expansion, not a frontend framework change.
