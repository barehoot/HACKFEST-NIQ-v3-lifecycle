# Talent 360i Phase 11C — Remote Mode Fix

## What was fixed

The frontend was configured for `TALENT360_API_MODE=remote`, but `services/api_client.py` was globally rebinding all Phase 1–10 service functions to the remote adapter. The supplied backend only exposes four pipeline APIs:

- `POST /api/generate`
- `POST /api/score`
- `POST /api/case-studies`
- `POST /api/evidence`

As a result, `/employee/dashboard` called `get_employee_dashboard` remotely, but that function has no corresponding backend endpoint and caused:

`KeyError: 'get_employee_dashboard'`

The fix keeps dashboard/SME/scheduler/manager/leader demo services local until those backend APIs exist, while the AI assessment pipeline continues to use the real backend APIs.

Generated assessments are also kept in the Jinja process session bridge so `Generate -> Take -> Submit -> Results` continues to work with the stateless backend contract.

## Local configuration

Create `.env` from `.env.example`:

```env
TALENT360_API_MODE=remote
TALENT360_BACKEND_URL=http://127.0.0.1:8000
TALENT360_API_TIMEOUT=120
TALENT360_DEBUG=true
TALENT360_SESSION_SECRET=change-me-in-development
TALENT360_ALLOW_MOCK_AUTH=true
```

Do not put backend LLM credentials in this frontend `.env`.

## Run

Backend:

```powershell
cd HACKFEST-NIQ-2
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Frontend:

```powershell
.venv\Scripts\Activate.ps1
uvicorn app:app --reload --port 8010
```

Open:

`http://127.0.0.1:8010/login`

Then use the Employee role and open AI Assessment.

## Expected behavior

- `/employee/dashboard` returns HTTP 200 even in remote mode.
- AI Assessment shows remote backend status.
- Resume generation calls backend `POST /api/generate`.
- Generated questions can be opened with `Take this assessment`.
- Submission calls backend `POST /api/score`.
- Results are displayed by Jinja.

## Verification performed

- Remote-mode employee dashboard: HTTP 200
- Phase 11C generate -> take -> submit -> results: PASS
- Phase 11 backend contract tests: PASS
- Phase 11B generate integration test: PASS
- Python compile check: PASS
