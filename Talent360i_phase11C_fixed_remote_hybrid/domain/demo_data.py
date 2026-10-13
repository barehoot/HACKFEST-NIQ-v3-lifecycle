"""Small synthetic dataset for the first Talent 360i vertical slice."""

EMPLOYEE_SKILLS = [
    {"skill": "Invoice Processing & 3-Way Match", "framework": "FinOps", "current": "L3", "target": "L4", "gap": 1, "progress": 75},
    {"skill": "AP Controls & Compliance", "framework": "FinOps", "current": "L2", "target": "L4", "gap": 2, "progress": 50},
    {"skill": "Cash Application & Matching", "framework": "FinOps", "current": "L3", "target": "L3", "gap": 0, "progress": 100},
    {"skill": "Bank Reconciliations", "framework": "FinOps", "current": "L3", "target": "L4", "gap": 1, "progress": 75},
]

BLUEPRINT = {
    "id": "BP-1001",
    "role": "P2P Senior Associate",
    "grade": "B3",
    "framework": "FinOps",
    "status": "Draft",
    "total_items": 10,
    "competencies": [
        {"name": "Invoice Processing & 3-Way Match", "target": "L3", "items": 4},
        {"name": "AP Controls & Compliance", "target": "L4", "items": 3},
        {"name": "Bank Reconciliations (SOX/IPE)", "target": "L3", "items": 3},
    ],
}

SME_QUEUE = [
    {"id": "Q-1001", "skill": "AP Controls & Compliance", "framework": "FinOps", "level": "L3", "type": "Scenario", "confidence": 0.92, "status": "Pending Review"},
    {"id": "Q-1002", "skill": "Invoice Processing & 3-Way Match", "framework": "FinOps", "level": "L3", "type": "MCQ", "confidence": 0.95, "status": "Approved"},
    {"id": "Q-1003", "skill": "Bank Reconciliations", "framework": "FinOps", "level": "L4", "type": "Case", "confidence": 0.88, "status": "Needs Revision"},
]

TEAM = [
    {"name": "Aditi Mehta", "role": "P2P Senior Associate", "readiness": 82, "top_gap": "AP Controls", "assessment": "Completed"},
    {"name": "Nikhil Shah", "role": "Cash Application Associate", "readiness": 64, "top_gap": "Collections", "assessment": "Pending"},
    {"name": "Priya Das", "role": "Credit Controller", "readiness": 71, "top_gap": "Customer Communication", "assessment": "Completed"},
]

DISTRIBUTION = {
    "finops": [
        {"label": "L0", "value": 4}, {"label": "L1", "value": 12}, {"label": "L2", "value": 26},
        {"label": "L3", "value": 38}, {"label": "L4", "value": 15}, {"label": "L5", "value": 5},
    ],
    "rd": [
        {"label": "Beginner", "value": 18}, {"label": "Moderate", "value": 57}, {"label": "Expert", "value": 25},
    ],
}

# --- Phase 2 employee -> evidence -> manager vertical slice ---------------
EMPLOYEE_SKILL_LEVELS = [
    {"id": "SK_AP_001", "name": "Invoice Processing & 3-Way Match", "framework": "FinOps", "current_level": "L3", "target_level": "L4", "evidence_required": False},
    {"id": "SK_AP_002", "name": "AP Controls & Compliance", "framework": "FinOps", "current_level": "L2", "target_level": "L4", "evidence_required": False},
    {"id": "SK_R2R_001", "name": "Bank Reconciliations (SOX/IPE)", "framework": "FinOps", "current_level": "L3", "target_level": "L4", "evidence_required": False},
    {"id": "SK_BHV_001", "name": "Stakeholder Communication", "framework": "RD", "current_level": "Moderate", "target_level": "Expert", "evidence_required": True},
]

EVIDENCE_CASES = [
    {"id": "EV-1001", "employee_id": "U001", "skill": "Stakeholder Communication", "framework": "RD", "claimed_level": "Expert", "status": "Awaiting Evidence", "manager": "Manager One", "prompt": "Describe a recent situation where you adapted communication for different stakeholders. Include the situation, your actions, and the outcome."},
]

MANAGER_VALIDATION_QUEUE = [
    {"id": "MV-1001", "employee_id": "U001", "employee": "Employee One", "skill": "Stakeholder Communication", "framework": "RD", "claimed_level": "Expert", "evidence": "Worked with AP, procurement and a supplier during an invoice exception. I clarified the issue separately with each group, aligned the next action and closed the exception without delaying the payment run.", "indicator": "Adapts communication to stakeholder needs and drives a clear outcome.", "evaluation": "Evidence covers stakeholder adaptation and outcome. Specificity is sufficient for manager review; the decision remains with the manager.", "status": "Pending Manager Confirmation", "score": "Meets evidence indicators", "missing": "No material gap flagged by the evidence assessment."},
]

ATTEMPTS = {}

# Phase 3 authoring state. Synthetic only; production persistence will move to the backend.
GENERATION_RUNS = []
