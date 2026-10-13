# Talent 360i Phase 11B — Jinja → Backend /api/generate

This phase creates the first working vertical integration between the Jinja2 frontend and the backend team's actual API contract.

## Connected API

`POST /api/generate`

Backend contract from the supplied backend repository:
- multipart field: `file`
- form field: `max_items`
- response: JSON containing `status`, `match`, `items`, `skills_skipped`, and `counts`

## Frontend flow

Employee → AI Assessment → upload resume → Jinja FastAPI route → `services/backend_api.py` → backend `/api/generate` → rendered match/questions.

The browser never receives the backend LLM credentials.

## Local configuration

```env
TALENT360_API_MODE=remote
TALENT360_BACKEND_URL=http://127.0.0.1:8000
TALENT360_API_TIMEOUT=120
TALENT360_ALLOW_MOCK_AUTH=true
```

`TALENT360_ALLOW_MOCK_AUTH=true` is only a local bridge because the supplied backend repository currently exposes the assessment pipeline but not `/api/auth/me`. Keep it `false` in a real production authentication setup.

## Run frontend

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8010
```

Open:

`http://127.0.0.1:8010/employee/ai-assessment?role=employee`

## Integration test

```bash
python tests_phase11b_generate.py
```

The test starts a local HTTP contract stub for the backend's real `/api/generate` shape and verifies the complete Jinja route → multipart request → JSON response → rendered HTML path.

Expected result:

`PASS: Jinja -> /api/generate multipart -> response -> rendered AI Assessment page`

This test does not call company LLM credentials.
