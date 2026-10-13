# Talent 360i — Phase 9 Governance / Audit / Security

Built on Phase 8 Leader Insights + Reports.

## Phase 9 controls
- Server-side RBAC remains authoritative.
- CSRF token required on every POST form.
- Logout is POST + CSRF protected.
- Secure session configuration: SameSite=Strict; HTTPS-only outside development; production requires `TALENT360_SESSION_SECRET`.
- Security response headers: CSP, X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, HSTS outside development.
- User-controlled text is bounded and control characters are removed before domain operations.
- Jinja autoescaping remains enabled for HTML rendering.
- State-changing requests create masked audit events.
- Agent runs are metered per agent and per day.
- Daily token cap: 500,000; no rollover.
- Per-agent caps: A1 50k, A2 300k, A3 75k, A4 75k, A5 50k.
- LLM call ledger stores prompt hash and metadata, not prompt text.
- `/admin/audit` exposes audit/agent/LLM metadata to the admin role.
- `/admin/budget` exposes the token budget ledger.

## Important
The current project still uses deterministic local simulations and synthetic data. No external/company LLM endpoint is called in Phase 9. The governance module is the seam for the later persistent SQLAlchemy backend and company CIS integration.

## Run
```powershell
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
$env:TALENT360_DEBUG="true"
uvicorn app:app --reload
```
For a production-like run, set `TALENT360_DEBUG=false` and provide a strong `TALENT360_SESSION_SECRET`.
