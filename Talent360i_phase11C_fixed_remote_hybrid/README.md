# Talent 360i — Phase 11: Jinja Backend API Integration

Phase 11 keeps the approved Jinja architecture and introduces a single REST adapter between the frontend service layer and the backend team API.

- Local mock mode remains available for development.
- Remote mode is enabled with `TALENT360_API_MODE=remote`.
- Backend URL/token are server-side environment variables.
- No LLM or database code is duplicated in this frontend project.
- See `PHASE11_API_INTEGRATION.md` for the draft API contract and handoff checklist.
