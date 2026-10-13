# Talent 360i — Phase 11C

## Working vertical slice

This phase connects the generated question set from the backend team's real `POST /api/generate` contract to the existing Jinja assessment runtime and sends submitted answers to the backend team's real `POST /api/score` contract.

### Flow

1. Employee opens `/employee/ai-assessment`.
2. Resume is uploaded to the Jinja server.
3. Jinja calls backend `POST /api/generate`.
4. Generated backend items are normalized for the existing assessment UI.
5. The generated assessment is stored in the Jinja process as a temporary session bridge.
6. Employee clicks **Take this assessment**.
7. Existing assessment runtime renders the generated questions.
8. Answers are submitted to Jinja.
9. Jinja converts UI answer names back to the backend question IDs.
10. Jinja calls backend `POST /api/score` with the exact generated backend items and answers.
11. Score is stored as a temporary attempt result and rendered in the existing Results page.

## Why a temporary frontend store exists

The supplied backend exposes `/api/generate` and `/api/score` as stateless endpoints. It does not yet expose a persistent assessment/attempt API for the generated question set. Therefore this phase keeps the generated question set and result in the Jinja process only. This is a bridge, not a replacement for backend persistence.

## Local configuration

Copy `.env.example` to `.env` and set:

```env
TALENT360_API_MODE=remote
TALENT360_BACKEND_URL=http://127.0.0.1:8000
TALENT360_API_TIMEOUT=60
TALENT360_ALLOW_MOCK_AUTH=true
```

`TALENT360_ALLOW_MOCK_AUTH=true` is only a local development bridge because the supplied backend does not currently expose `/api/auth/me`. Do not use it as the production authentication model.

## Run

### Backend

```bash
cd HACKFEST-NIQ-2
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Jinja frontend

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8010
```

Open:

`http://127.0.0.1:8010/employee/ai-assessment?role=employee`

## Automated vertical-slice test

```bash
python tests_phase11c_vertical_slice.py
```

Expected:

```text
PASS: Phase 11C generate -> take -> submit -> results
```

The automated test uses a controlled backend-contract stub for `/api/generate` and `/api/score`, so it validates the Jinja integration without requiring company-network LLM access.

## Security

- Backend LLM/API credentials stay server-side.
- Do not place backend `.env` or API keys in this frontend ZIP.
- The frontend does not call the LLM directly.
- The generated question set is not sent back from the browser with the answer key; the Jinja process retains it for the score request.
- The temporary store is process-local and should be replaced by backend persistence before production/multi-worker deployment.


## Local .env loading fix

This release explicitly loads the frontend project's `.env` before the backend API adapter is initialized. Copy `.env.example` to `.env` and set:

```env
TALENT360_API_MODE=remote
TALENT360_BACKEND_URL=http://127.0.0.1:8000
TALENT360_API_TIMEOUT=120
```

Do not put CIS/Groq credentials in the frontend `.env`.
