"""Phase 11 smoke tests for local/mock mode and adapter configuration."""
import os
os.environ.setdefault("TALENT360_API_MODE", "mock")
from services import api_client

assert api_client.get_employee_dashboard("U001")
assert api_client.get_sme_questions("All")
assert api_client.get_blueprint()
assert api_client.get_manager_dashboard("M001")
assert api_client.get_leader_insights("finops")

from services.backend_api import backend_api
assert backend_api.enabled is False
print("Phase 11 mock-mode service smoke test: PASS")
