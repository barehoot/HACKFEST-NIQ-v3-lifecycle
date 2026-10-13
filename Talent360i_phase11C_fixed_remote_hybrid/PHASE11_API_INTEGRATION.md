# Phase 11 — Jinja REST API Integration

## Goal
Connect the existing server-rendered Jinja frontend to the backend team's REST APIs without moving to Next.js and without duplicating the backend/LLM/database.

### Ownership
- **Jinja frontend:** templates, route handlers, navigation, UX states, forms, charts, assessment runtime UI.
- **Backend team:** API, authentication/RBAC, database, A1–A5 agents, company LLM, token budget, audit/business rules.
- **This phase:** one outbound REST adapter (`services/backend_api.py`) plus a service boundary in `services/api_client.py`.

## Modes
### Local demo (default)
```text
TALENT360_API_MODE=mock
```
Uses the Phase 1–10 deterministic demo services. No backend is required.

### Backend integration
```text
TALENT360_API_MODE=remote
TALENT360_BACKEND_URL=https://<internal-backend-host>
TALENT360_BACKEND_TOKEN=<server-to-server-token-if-required>
```
In remote mode, the same Jinja routes call the REST backend through `services.backend_api`.

## Draft API contract
The adapter includes provisional defaults based on the role flows and HLD. These are **not claimed to be the backend team's final paths**. Every path can be overridden with `TALENT360_API_ENDPOINT_<FUNCTION>`.

| Frontend service | Default method/path | Used by |
|---|---|---|
| `get_current_user` | GET `/api/auth/me` | auth/session |
| `get_employee_dashboard` | GET `/api/employee/dashboard` | Employee |
| `get_employee_skills` | GET `/api/employee/skills` | Employee |
| `get_employee_assessments` | GET `/api/employee/assessments` | Employee |
| `get_assessment` | GET `/api/employee/assessments/{assessment_id}` | Employee |
| `submit_assessment` | POST `/api/employee/assessments/{assessment_id}/submit` | Employee |
| `get_assessment_results` | GET `/api/employee/results/{attempt_id}` | Employee |
| `get_employee_evidence` | GET `/api/employee/evidence` | Employee |
| `submit_evidence` | POST `/api/employee/evidence/{case_id}/submit` | Employee |
| `answer_evidence_probe` | POST `/api/employee/evidence/{case_id}/probe` | Employee |
| `get_employee_development_plan` | GET `/api/employee/development-plan` | Employee |
| `get_sme_questions` | GET `/api/sme/questions` | SME |
| `approve_question` | POST `/api/sme/questions/{question_id}/approve` | SME |
| `reject_question` | POST `/api/sme/questions/{question_id}/reject` | SME |
| `edit_question` | PATCH `/api/sme/questions/{question_id}` | SME |
| `regenerate_question` | POST `/api/sme/questions/{question_id}/regenerate` | SME |
| `generate_question_set` | POST `/api/sme/questions/generate` | SME |
| `get_a3_escalations` | GET `/api/sme/assessment-reviews` | SME |
| `decide_a3_escalation` | POST `/api/sme/assessment-reviews/{id}/decision` | SME |
| `get_blueprint` | GET `/api/scheduler/blueprints/{id}` | Scheduler |
| `get_question_gate` | GET `/api/scheduler/blueprints/{id}/readiness` | Scheduler |
| `launch_schedule` | POST `/api/scheduler/schedules` | Scheduler |
| `assign_assessment` | POST `/api/scheduler/assignments` | Scheduler |
| `get_manager_validation_queue` | GET `/api/manager/validations` | Manager |
| `confirm_manager_validation` | POST `/api/manager/validations/{id}/decision` | Manager |
| `get_manager_dashboard` | GET `/api/manager/dashboard` | Manager |
| `get_leader_insights` | GET `/api/leader/insights` | Leader |

## Security
- Do not put backend tokens in Jinja templates or browser JavaScript.
- Do not expose the company LLM endpoint to the browser.
- The frontend treats API JSON as untrusted data and renders it through Jinja escaping.
- Production authentication/RBAC remains backend/SSO-owned.
- `X-Talent360-User-Id` / `X-Talent360-Role` are only correlation hints in this adapter and must not be treated as authorization by the backend.
- Use HTTPS in production.

## Backend-team handoff checklist
Ask the backend team for:
1. OpenAPI/Swagger URL or `openapi.json`.
2. Authentication method (Azure/Entra SSO, JWT, gateway token, etc.).
3. Exact endpoint paths and request/response schemas.
4. Error format and status codes.
5. CORS is not required for this server-to-server Jinja pattern.
6. Required correlation/request headers.
7. Pagination/filter conventions.

Once those are known, update only `services/backend_api.py` endpoint defaults/overrides and, where response shapes differ, the adapter normalization — templates should not need to know backend implementation details.

## Run
```powershell
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
$env:TALENT360_API_MODE="mock"
uvicorn app:app --reload
```

For remote mode:
```powershell
$env:TALENT360_API_MODE="remote"
$env:TALENT360_BACKEND_URL="https://internal-api.example"
$env:TALENT360_BACKEND_TOKEN="<secret>"
uvicorn app:app
```
