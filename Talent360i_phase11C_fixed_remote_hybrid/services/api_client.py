"""Temporary service adapter.

Templates and routes use these functions instead of importing data directly.
Replace each function body with authenticated REST calls when the backend is ready.
"""
from copy import deepcopy
from datetime import date
import json
import threading

from domain import assessment as assessment_domain
from domain import a3_assessment
from domain import a4_evidence
from domain import demo_data
from domain import question_generation
from domain import development_plan as development_domain
from agents import a5_development
from domain import demo_data as phase2_data
from domain.level_engine import ordinal
from domain import governance
from services import schedule, tni

CURRENT_USER = {"id": "U1001", "name": "Demo Employee", "initials": "DE", "role": "P2P Senior Associate", "role_id": "AP_B3_01", "tower": "AP"}

SKILLS = [
    {"id": "SK_AP_001", "name": "Invoice Processing & 3-Way Match", "family": "Accounts Payable", "current": 3, "target": 3, "status": "On Target"},
    {"id": "SK_AP_002", "name": "AP Controls & Compliance", "family": "Accounts Payable", "current": 2, "target": 4, "status": "Needs Development"},
    {"id": "SK_R2R_001", "name": "Bank Reconciliations (SOX/IPE)", "family": "Record to Report", "current": 3, "target": 4, "status": "Needs Development"},
    {"id": "SK_R2R_002", "name": "Journal Entries & Account Analysis", "family": "Record to Report", "current": 3, "target": 3, "status": "On Target"},
]

ASSESSMENTS = [
    {"id": "A1001", "name": "P2P Skill Assessment", "role": "P2P Senior Associate", "questions": 10, "duration": 15, "status": "Assigned", "due": "20 Sep 2026"},
    {"id": "A0998", "name": "AP Controls Checkpoint", "role": "P2P Senior Associate", "questions": 8, "duration": 12, "status": "Completed", "due": "05 Sep 2026"},
]

QUESTION_SET = [
    {"number": 1, "skill": "Invoice Processing & 3-Way Match", "level": "L3", "text": "Before an invoice is released for payment, which documents should be matched as part of a standard three-way match?", "options": ["Purchase order, goods receipt, and supplier invoice", "Supplier invoice, bank statement, and credit note", "Purchase requisition, payment file, and invoice", "Goods receipt, journal entry, and payment run"], "correct": "A"},
    {"number": 2, "skill": "AP Controls & Compliance", "level": "L3", "text": "Which control most directly reduces the risk of duplicate supplier payments?", "options": ["A monthly spend forecast", "Duplicate invoice checking using supplier and invoice reference", "A quarterly supplier survey", "A daily bank reconciliation"], "correct": "B"},
    {"number": 3, "skill": "Bank Reconciliations (SOX/IPE)", "level": "L3", "text": "What is the first action when an unreconciled bank item is identified?", "options": ["Write it off immediately", "Document and investigate the variance", "Reverse all transactions", "Wait for year-end review"], "correct": "B"},
    {"number": 4, "skill": "AP Controls & Compliance", "level": "L4", "text": "Which evidence best supports an effective approval control for a high-value invoice?", "options": ["An unsigned copy of the invoice", "System workflow approval by an authorized approver", "A verbal confirmation from the buyer", "The supplier's marketing email"], "correct": "B"},
    {"number": 5, "skill": "Invoice Processing & 3-Way Match", "level": "L3", "text": "A price variance exceeds the configured tolerance. What should happen next?", "options": ["Pay the invoice regardless", "Route the exception for review before payment", "Change the purchase order without approval", "Delete the invoice"], "correct": "B"},
    {"number": 6, "skill": "AP Controls & Compliance", "level": "L3", "text": "Why should vendor master-data changes use a maker-checker process?", "options": ["It speeds up payment batches", "It separates creation from approval of sensitive data", "It removes the need for audit logs", "It eliminates supplier onboarding"], "correct": "B"},
    {"number": 7, "skill": "Bank Reconciliations (SOX/IPE)", "level": "L3", "text": "Which item would normally require follow-up in a bank reconciliation?", "options": ["A matched transaction", "An aged outstanding reconciling item", "A standard chart of accounts", "A completed training record"], "correct": "B"},
    {"number": 8, "skill": "Invoice Processing & 3-Way Match", "level": "L3", "text": "What is the purpose of a blocked invoice workflow?", "options": ["To prevent review of any invoice", "To hold an exception until it is resolved", "To remove the supplier record", "To close the accounting period"], "correct": "B"},
    {"number": 9, "skill": "AP Controls & Compliance", "level": "L4", "text": "Which metric is most useful to monitor control effectiveness in AP?", "options": ["Number of office seats", "Rate of approved payment exceptions", "Supplier logo count", "Annual leave balance"], "correct": "B"},
    {"number": 10, "skill": "Bank Reconciliations (SOX/IPE)", "level": "L3", "text": "Who should validate a completed high-risk reconciliation?", "options": ["The preparer only", "An appropriate independent reviewer", "The newest team member", "The supplier"], "correct": "B"},
]

QUESTIONS = [
    {"id": "Q_1001", "skill": "AP Controls & Compliance", "family": "Accounts Payable", "level": "L4", "text": "Which evidence best supports an effective approval control for a high-value invoice?", "options": QUESTION_SET[3]["options"], "correct": "B", "rationale": "A recorded system workflow shows that the invoice was approved by an authorized approver and preserves an audit trail.", "generated_on": "14 Sep 2026", "validation": "Passed", "status": "Pending Review", "approved_by": "—", "approved_date": "—", "comment": ""},
    {"id": "Q_1002", "skill": "Invoice Processing & 3-Way Match", "family": "Accounts Payable", "level": "L3", "text": "Before an invoice is released for payment, which documents should be matched?", "options": QUESTION_SET[0]["options"], "correct": "A", "rationale": "Three-way matching compares the PO, goods receipt, and supplier invoice to validate the transaction.", "generated_on": "13 Sep 2026", "validation": "Passed", "status": "Approved", "approved_by": "R. Kapoor", "approved_date": "14 Sep 2026", "comment": ""},
    {"id": "Q_1003", "skill": "Bank Reconciliations (SOX/IPE)", "family": "Record to Report", "level": "L3", "text": "What is the first action when an unreconciled bank item is identified?", "options": QUESTION_SET[2]["options"], "correct": "B", "rationale": "Documenting and investigating variances maintains a complete, auditable reconciliation process.", "generated_on": "14 Sep 2026", "validation": "Passed", "status": "Pending Review", "approved_by": "—", "approved_date": "—", "comment": ""},
    {"id": "Q_1004", "skill": "AP Controls & Compliance", "family": "Accounts Payable", "level": "L3", "text": "Which control reduces the risk of duplicate supplier payments?", "options": QUESTION_SET[1]["options"], "correct": "B", "rationale": "Duplicate checking flags repeated invoice identifiers before payment is processed.", "generated_on": "12 Sep 2026", "validation": "Passed", "status": "Rejected", "approved_by": "—", "approved_date": "—", "comment": "Clarify the invoice identifier assumption."},
]

LEARNING = [
    {"title": "AP Controls Microlearning", "skill": "AP Controls & Compliance", "current": "L2", "target": "L4", "reason": "Strengthen exception-handling and approval controls.", "format": "Microlearning · 35 min", "priority": "High"},
    {"title": "Advanced Invoice Controls", "skill": "AP Controls & Compliance", "current": "L2", "target": "L4", "reason": "Build confidence in control design and evidence.", "format": "Course · 2.5 hrs", "priority": "High"},
    {"title": "Bank Reconciliation Workshop", "skill": "Bank Reconciliations (SOX/IPE)", "current": "L3", "target": "L4", "reason": "Close the gap in complex variance resolution.", "format": "Workshop · 3 hrs", "priority": "Medium"},
]


USERS = [
    {"id": "U001", "name": "Employee One", "email": "employee@talent360.local", "role": "employee", "department": "Finance Operations", "job_title": "P2P Senior Associate", "manager_id": "M001", "readiness": 82, "assessments": "3/4", "gaps": 2, "status": "On Track"},
    {"id": "U002", "name": "Aditi Mehta", "email": "aditi.m@talent360.local", "role": "employee", "department": "Finance Operations", "job_title": "P2P Senior Associate", "manager_id": "M001", "readiness": 76, "assessments": "4/4", "gaps": 1, "status": "On Track"},
    {"id": "U003", "name": "Nikhil Shah", "email": "nikhil.s@talent360.local", "role": "employee", "department": "Finance Operations", "job_title": "Cash Application Associate", "manager_id": "M001", "readiness": 64, "assessments": "2/4", "gaps": 3, "status": "Needs Attention"},
    {"id": "U004", "name": "Priya Das", "email": "priya.d@talent360.local", "role": "employee", "department": "Finance Operations", "job_title": "Credit Controller", "manager_id": "M001", "readiness": 71, "assessments": "3/4", "gaps": 2, "status": "On Track"},
    {"id": "U099", "name": "Outside Team Employee", "email": "outside@talent360.local", "role": "employee", "department": "Finance Operations", "job_title": "P2P Lead", "manager_id": "M002", "readiness": 91, "assessments": "4/4", "gaps": 0, "status": "On Track"},
    {"id": "M001", "name": "Manager One", "email": "manager@talent360.local", "role": "manager", "department": "Finance Operations", "job_title": "Finance Manager", "manager_id": None},
    {"id": "S001", "name": "SME Reviewer", "email": "sme@talent360.local", "role": "sme", "department": "Finance Excellence", "job_title": "Process SME", "manager_id": None},
    {"id": "A001", "name": "System Administrator", "email": "admin@talent360.local", "role": "admin", "department": "People Systems", "job_title": "Talent Platform Administrator", "manager_id": None},
]

TEAM_SKILLS = {
    "U001": {"AP": "L3", "O2C": "L2", "R2R": "L3", "overall": "L3"},
    "U002": {"AP": "L3", "O2C": "L3", "R2R": "L2", "overall": "L3"},
    "U003": {"AP": "L2", "O2C": "L3", "R2R": "L2", "overall": "L2"},
    "U004": {"AP": "L2", "O2C": "L3", "R2R": "L3", "overall": "L3"},
}


def get_current_user(): return deepcopy(CURRENT_USER)
def get_role_skills():
    return [{"id": s["id"], "name": s["name"], "family": s["family"]} for s in SKILLS]

def get_user_skills(status="All"):
    data = deepcopy(SKILLS)
    return data if status == "All" else [s for s in data if s["status"] == status]
def get_assessments(): return deepcopy(ASSESSMENTS)
def get_sme_questions(status="All"):
    data = deepcopy(QUESTIONS)
    return data if status == "All" else [q for q in data if q["status"] == status]

def get_questions(status: str = "All"):
    return get_sme_questions(status)

def get_question(question_id):
    for question in QUESTIONS:
        if question["id"] == question_id:
            result = deepcopy(question)
            result.setdefault("role", "P2P Senior Associate")
            result.setdefault("question_type", "mcq")
            result.setdefault("sop_reference", "AP control procedure — synthetic development reference")
            result.setdefault("generated_by", "A2 Question Agent (development simulation)")
            result.setdefault("regeneration_count", 0)
            result.setdefault("confidence", 0.92)
            result.setdefault("validation_checks", {"skill_relevance": True, "difficulty": True, "bloom_level": True, "bias_sensitivity": True, "sop_alignment": True})
            return result
    return None

# "Needs Revision" = failed an A2 self-check; the SME still makes the call.
_UNDECIDED = ("Pending Review", "Needs Revision")


def approve_question(question_id, approved_by="SME Reviewer"):
    for q in QUESTIONS:
        if q["id"] == question_id and q["status"] in _UNDECIDED:
            q["status"] = "Approved"
            q["approved_by"] = approved_by
            q["approved_date"] = date.today().strftime("%d %b %Y")
            q["comment"] = "Approved by SME."
            return True
    return False

def reject_question(question_id, reason):
    for q in QUESTIONS:
        if q["id"] == question_id and q["status"] in _UNDECIDED:
            q["status"] = "Rejected"
            q["comment"] = reason.strip()
            return True
    return False

def edit_question(question_id, text, rationale, correct, options, level=None):
    for q in QUESTIONS:
        if q["id"] == question_id and q["status"] == "Pending Review":
            clean_options = [o.strip() for o in options if o.strip()]
            if len(clean_options) != 4 or correct not in {"A", "B", "C", "D"} or not text.strip():
                return False
            q.update({"text": text.strip(), "rationale": rationale.strip(), "correct": correct, "options": clean_options})
            if level:
                q["level"] = level
            q["comment"] = "Edited by SME; awaiting approval."
            return True
    return False

def regenerate_question(question_id):
    for q in QUESTIONS:
        if q["id"] == question_id and q["status"] == "Pending Review" and q.get("regeneration_count", 0) < 1:
            generated = question_generation.generate_questions(q.get("role", "P2P Senior Associate"), q["skill"], q["level"], 1, q.get("question_type", "mcq"))[0]
            q.update({
                "text": generated["text"], "options": generated["options"], "correct": generated["correct"],
                "rationale": generated["rationale"], "sop_reference": generated["sop"],
                "regeneration_count": q.get("regeneration_count", 0) + 1,
                "validation": "Passed", "comment": "Regenerated after self-check iteration; awaiting SME review."
            })
            return True
    return False

def generate_question_set(role, skill, level, count, question_type="mcq"):
    governance.record_agent_run("A2", "author question set", estimated_tokens=2000 * max(1, int(count)), metadata={"role": role, "skill": skill, "question_type": question_type})
    generated = question_generation.generate_questions(role, skill, level, count, question_type)
    created = []
    for index, item in enumerate(generated):
        check = question_generation.self_check(item)
        question_id = f"Q_{len(QUESTIONS) + 1001 + index:04d}"
        created.append({
            "id": question_id, "role": role, "skill": item["skill"], "family": "Accounts Payable" if "AP" in item["skill"] or "Invoice" in item["skill"] else "Record to Report",
            "level": item["level"], "question_type": item["question_type"], "text": item["text"], "options": item["options"],
            "correct": item["correct"], "rationale": item["rationale"], "sop_reference": item["sop"],
            "generated_on": date.today().strftime("%d %b %Y"), "validation": "Passed" if check["passed"] else "Failed",
            "validation_checks": check["checks"], "confidence": check["confidence"], "status": "Pending Review" if check["passed"] else "Needs Revision",
            "approved_by": "—", "approved_date": "—", "comment": "", "regeneration_count": 0,
            "generated_by": "A2 Question Agent (development simulation)",
        })
    QUESTIONS.extend(created)
    demo_data.GENERATION_RUNS.append({"role": role, "skill": skill, "level": level, "count": len(created), "status": "Pending SME Review"})
    return deepcopy(created)

def get_sme_generation_summary():
    return deepcopy(demo_data.GENERATION_RUNS[-5:])

def get_sme_dashboard():
    return {"metrics": {"generated": len(QUESTIONS), "pending": len([q for q in QUESTIONS if q["status"] == "Pending Review"]), "approved": len([q for q in QUESTIONS if q["status"] == "Approved"]), "rejected": len([q for q in QUESTIONS if q["status"] == "Rejected"])}, "pending": get_sme_questions("Pending Review"), "runs": get_sme_generation_summary()}


# --- HLD v2 first vertical slice: Scheduler + Leader -----------------------
def get_scheduler_dashboard():
    pending = sum(1 for q in QUESTIONS if q["status"] != "Approved")
    return {
        "metrics": {"blueprints": 3, "pending_sme": pending, "approved_question_sets": 4, "scheduled": 2},
        "blueprint": {"id": "BP-1001", "role": "P2P Senior Associate", "grade": "B3", "framework": "FinOps", "items": 10, "status": "Draft"},
        "steps": [
            {"name": "Blueprint", "status": "Ready"},
            {"name": "Question Generation", "status": "Ready"},
            {"name": "SME Approval", "status": "Pending"},
            {"name": "Scheduling", "status": "Blocked until approval"},
        ],
    }


def get_leader_dashboard():
    return {
        "metrics": {"employees": 48, "assessment_coverage": "81%", "open_gaps": 37, "learning_plans": 29},
        "top_gaps": [
            {"skill": "AP Controls & Compliance", "employees": 11, "target": "L4"},
            {"skill": "Bank Reconciliations", "employees": 8, "target": "L4"},
            {"skill": "Collections & Communication", "employees": 7, "target": "L4"},
        ],
    }


def get_leader_distribution(framework_id="finops"):
    from domain.demo_data import DISTRIBUTION
    return deepcopy(DISTRIBUTION.get(framework_id, DISTRIBUTION["finops"]))

# --- Phase 2: deterministic assessment -> gap -> evidence -> manager --------


def get_employee_skills(user_id: str, status: str = "All"):
    if user_id != "U001":
        return []
    rows = deepcopy(phase2_data.EMPLOYEE_SKILL_LEVELS)
    for row in rows:
        row["status"] = "On Target" if row["current_level"] == row["target_level"] else "Needs Development"
        row["gap"] = assessment_domain.calculate_gap(row["framework"], row["current_level"], row["target_level"])
        row["current"] = ordinal(row["framework"], row["current_level"])
        row["target"] = ordinal(row["framework"], row["target_level"])
    return rows if status == "All" else [r for r in rows if r["status"] == status]


def get_skill_summary(user_id="U001"):
    rows = get_employee_skills(user_id)
    if not rows:
        return {"total": 0, "on_target": 0, "gaps": 0, "average": "—"}
    finops = [r for r in rows if r["framework"] == "FinOps"]
    avg = round(sum(ordinal("FinOps", r["current_level"]) for r in finops) / len(finops), 1) if finops else 0
    return {"total": len(rows), "on_target": len([r for r in rows if r["gap"] == 0]), "gaps": len([r for r in rows if r["gap"] > 0]), "average": f"L{avg}"}


# --- Phase 5: assessment runtime + A3 descriptive scoring -----------------
# The runtime deliberately contains all three assessment item types required by
# the HLD. Objective items remain deterministic; descriptive items produce an
# A3 proposal and may enter the SME escalation queue.
RUNTIME_QUESTION_SET = [
    {**deepcopy(QUESTION_SET[0]), "question_type": "mcq", "framework": "FinOps", "sop_reference": "P2P-MATCH-01 · Three-way match procedure"},
    {**deepcopy(QUESTION_SET[1]), "question_type": "mcq", "framework": "FinOps", "sop_reference": "AP-CTRL-07 · Duplicate invoice exception handling"},
    {**deepcopy(QUESTION_SET[2]), "question_type": "scenario", "framework": "FinOps", "sop_reference": "R2R-REC-05 · Aged reconciling item escalation"},
    {**deepcopy(QUESTION_SET[3]), "question_type": "mcq", "framework": "FinOps", "sop_reference": "AP-CTRL-04 · High-value invoice approval and audit trail"},
    {**deepcopy(QUESTION_SET[4]), "question_type": "scenario", "framework": "FinOps", "sop_reference": "P2P-MATCH-03 · Tolerance exception workflow"},
    {**deepcopy(QUESTION_SET[5]), "question_type": "mcq", "framework": "FinOps", "sop_reference": "AP-CTRL-09 · Maker-checker for master data"},
    {"number": 7, "skill": "AP Controls & Compliance", "level": "L4", "question_type": "descriptive", "framework": "FinOps",
     "text": "A high-value invoice is blocked because the approval evidence is missing. Describe how you would investigate the exception, protect the control, and decide what evidence is needed before release.",
     "options": [], "correct": None, "rubric": ["situation", "action", "control_or_reasoning", "outcome"],
     "sop_reference": "AP-CTRL-04 · High-value invoice approval and audit trail"},
    {"number": 8, "skill": "Bank Reconciliations (SOX/IPE)", "level": "L4", "question_type": "descriptive", "framework": "FinOps",
     "text": "An aged reconciling item has remained unresolved beyond the normal review window. Explain the investigation, escalation and evidence you would document.",
     "options": [], "correct": None, "rubric": ["situation", "action", "control_or_reasoning", "outcome"],
     "sop_reference": "R2R-REC-05 · Aged reconciling item escalation"},
    {"number": 9, "skill": "Bank Reconciliations (SOX/IPE)", "level": "L3", "question_type": "mcq", "framework": "FinOps", "sop_reference": "R2R-REC-02 · Reconciliation review control",
     "text": "Who should validate a completed high-risk reconciliation?", "options": ["The preparer only", "An appropriate independent reviewer", "The newest team member", "The supplier"], "correct": "B"},
    {"number": 10, "skill": "Invoice Processing & 3-Way Match", "level": "L3", "question_type": "mcq", "framework": "FinOps", "sop_reference": "P2P-MATCH-03 · Tolerance exception workflow",
     "text": "A price variance exceeds the configured tolerance. What should happen next?", "options": ["Pay the invoice regardless", "Route the exception for review before payment", "Change the purchase order without approval", "Delete the invoice"], "correct": "B"},
]

A3_ESCALATIONS = []


def _runtime_assessment_status(attempt_id="ATT_1001"):
    attempt = phase2_data.ATTEMPTS.get(attempt_id)
    if not attempt:
        return "Assigned"
    return attempt.get("status", "Completed")


def get_assessment(assessment_id, user_id="U001"):
    # Phase 11C generated assessments are stored in this frontend process until
    # the backend exposes persistent assessment GET/attempt APIs.
    generated_store = globals().get("BACKEND_GENERATED_ASSESSMENTS", {})
    generated = generated_store.get(assessment_id)
    if generated and user_id == "U001":
        return deepcopy(generated)
    if assessment_id != "A1001" or user_id != "U001":
        return None
    assessment = {**deepcopy(ASSESSMENTS[0]), "question_set": deepcopy(RUNTIME_QUESTION_SET)}
    assessment["questions"] = len(RUNTIME_QUESTION_SET)
    assessment["status"] = _runtime_assessment_status()
    assessment["assigned_via"] = "AA-1001" if any(a.get("assessment_id") == assessment_id and a.get("employee_id") == user_id for a in ASSESSMENT_ASSIGNMENTS) else "Development assignment"
    return assessment


def submit_assessment(assessment_id, user_id="U001", answers=None):
    generated_store = globals().get("BACKEND_GENERATED_ASSESSMENTS", {})
    if assessment_id in generated_store:
        return submit_backend_generated_assessment(assessment_id, user_id, answers or {})
    assessment = get_assessment(assessment_id, user_id)
    if not assessment:
        return None
    # Prevent duplicate submissions from silently replacing an existing attempt.
    if "ATT_1001" in phase2_data.ATTEMPTS:
        return {"attempt_id": "ATT_1001"}

    parsed = assessment_domain.parse_answers(answers)
    scored = assessment_domain.score_questions(assessment["question_set"], parsed)
    if scored["descriptive_results"]:
        governance.record_agent_run("A3", "descriptive assessment", estimated_tokens=1200 * len(scored["descriptive_results"]), metadata={"attempt_id": "ATT_1001", "descriptive_items": len(scored["descriptive_results"])})
    proficiency = assessment_domain.build_proficiency(scored["performance"], get_employee_skills(user_id), "FinOps")
    attempt_id = "ATT_1001"
    escalation_ids = []
    for proposal in scored["descriptive_results"]:
        if proposal["borderline"]:
            escalation_id = f"A3-{1001 + len(A3_ESCALATIONS)}"
            proposal = deepcopy(proposal)
            proposal["id"] = escalation_id
            proposal["attempt_id"] = attempt_id
            proposal["employee_id"] = user_id
            proposal["employee"] = USERS[0]["name"]
            proposal["status"] = "Pending SME Review"
            A3_ESCALATIONS.append(proposal)
            escalation_ids.append(escalation_id)

    phase2_data.ATTEMPTS[attempt_id] = {
        "attempt_id": attempt_id,
        "assessment": assessment["name"],
        "completed_on": date.today().strftime("%d %b %Y"),
        "answers": parsed,
        "score": scored["score"],
        "correct": scored["correct"],
        "total": scored["total"],
        "performance": scored["performance"],
        "proficiency": proficiency,
        "item_results": scored["item_results"],
        "descriptive_results": scored["descriptive_results"],
        "a3_escalation_ids": escalation_ids,
        "status": "Pending SME Review" if escalation_ids else "Completed",
        "a3_note": "A3 proposes descriptive scoring only. Borderline proposals are routed to the SME; proficiency level remains deterministic and is not set by A3.",
    }
    return {"attempt_id": attempt_id}


def get_assessment_results(attempt_id, user_id="U001"):
    generated_store = globals().get("BACKEND_GENERATED_ATTEMPTS", {})
    generated = generated_store.get(attempt_id)
    if generated and user_id == "U001":
        return deepcopy(generated)
    if user_id != "U001" or attempt_id not in phase2_data.ATTEMPTS:
        return None
    result = deepcopy(phase2_data.ATTEMPTS[attempt_id])
    result["recommendations"] = get_learning_recommendations(user_id, result["proficiency"])
    result["decision_note"] = "Levels shown here are deterministic assessment calculations. No LLM/A3 agent sets the employee level. Behavioural evidence requires manager confirmation."
    result["a3_escalations"] = [deepcopy(x) for x in A3_ESCALATIONS if x["attempt_id"] == attempt_id]
    if result["a3_escalations"] and all(x["status"] == "SME Accepted" for x in result["a3_escalations"]):
        result["status"] = "Completed"
    return result


def get_a3_escalations(status="all"):
    rows = deepcopy(A3_ESCALATIONS)
    if status != "all":
        rows = [x for x in rows if x["status"] == status]
    return rows


def decide_a3_escalation(escalation_id, decision="accept", note=""):
    for item in A3_ESCALATIONS:
        if item["id"] != escalation_id:
            continue
        clean_note = " ".join((note or "").split())[:1000]
        if decision == "accept":
            item["status"] = "SME Accepted"
            item["sme_note"] = clean_note or "SME accepted the A3 descriptive scoring proposal."
        elif decision == "send_back":
            item["status"] = "Sent Back"
            item["sme_note"] = clean_note or "SME requested a re-review of the descriptive scoring proposal."
        else:
            return False
        return True
    return False


def get_employee_assessments(user_id: str):
    if user_id != "U001":
        return []
    rows = get_assessments()
    for row in rows:
        if row["id"] == "A1001":
            row["questions"] = len(RUNTIME_QUESTION_SET)
            row["status"] = _runtime_assessment_status()
            row["type_mix"] = "MCQ · Scenario · Descriptive"
    return rows


def get_users():
    return deepcopy(USERS)


def _team(manager_id: str):
    return [user for user in USERS if user.get("manager_id") == manager_id]


# Team views are built from the manager's real direct reports (users table)
# and their Skill_Gaps_TNI rows (services/tni.py) -- previously they showed a
# fixed list of five demo people, none of them the synthetic cohort.
_CYCLE_PROGRESS = {
    None: "Not started",
    "resume_uploaded": "Resume uploaded",
    "pending_sme_setup": "SME set-up",
    "pending_sme_approval": "SME review",
    "ready_for_assessment": "Ready to take",
    "mcq_submitted": "In progress",
    "case_study_submitted": "In progress",
    "pending_manager_approval": "Manager review",
    "manager_approved": "Completed",
    "manager_rejected": "Completed",
}


def _team_members(team: dict):
    tni_people = {p["user_id"]: p for p in team["people"]}
    members = []
    for report in team["reports"]:
        person = tni_people.get(report["user_id"])
        cycle = workflow_db.get_active_cycle_for_user(report["user_id"])
        summary = person["summary"] if person else None
        readiness = summary["readiness_pct"] if summary and summary["assessed"] else None
        members.append({
            "id": report["user_id"], "name": report["full_name"], "email": report["email"],
            "job_title": report["job_title"], "role_name": person["role_name"] if person else report["job_title"],
            "tower": person["tower"] if person else None,
            "readiness": readiness,
            "gaps": summary["gaps"] if summary else 0,
            "high": summary["high"] if summary else 0,
            "not_assessed": summary["not_assessed"] if summary else 0,
            "assessments": _CYCLE_PROGRESS.get(cycle["status"] if cycle else None, "In progress"),
            "status": "No TNI baseline" if readiness is None else "On Track" if readiness >= 60 else "Needs Attention",
            "skills": person["skills"] if person else [],
            "path": person["path"] if person else [],
        })
    return members


def get_manager_team_views(manager_id: str) -> dict:
    """Every manager team view from one read of the team: dashboard, team,
    matrix, gaps and reports -- the shapes the manager templates render."""
    members = _team_members(tni.team_tni(manager_id))

    matrix = []
    for member in members:
        assessed = [s for s in member["skills"] if s["assessed"]]
        avg = f"L{sum(s['current'] for s in assessed) / len(assessed):.1f}" if assessed else "—"
        towers = {"AP": "—", "O2C": "—", "R2R": "—"}
        if member["tower"] in towers:
            towers[member["tower"]] = avg
        matrix.append({"employee": member["name"], "role": member["role_name"], **towers, "overall": avg})

    gaps = []
    for member in members:
        for step in member["path"]:
            course = step["course"]
            gaps.append({
                "employee": member["name"], "skill": step["skill"],
                "current": f"L{step['current']}", "target": f"L{step['target']}",
                "training": f"{course['course_id']} · {course['short_title']}" if course else "No catalogue course yet",
                "priority": step["severity"],
            })
    gaps.sort(key=lambda r: r["priority"] != "High")

    rated = [m["readiness"] for m in members if m["readiness"] is not None]
    skills = [s for m in members for s in m["skills"]]
    assessed_count = sum(1 for s in skills if s["assessed"])
    reports = {
        "team_size": len(members),
        "average_readiness": round(sum(rated) / len(rated)) if rated else 0,
        "completion": f"{round(100 * assessed_count / len(skills))}%" if skills else "—",
        "priority_gaps": sum(m["high"] for m in members),
    }

    by_skill = {}
    for member in members:
        for step in member["path"]:
            row = by_skill.setdefault(step["skill"], {"skill": step["skill"], "employees": 0, "total_gap": 0, "high": 0})
            row["employees"] += 1
            row["total_gap"] += step["gap"]
            row["high"] += step["severity"] == "High"
    top_gaps = sorted(by_skill.values(), key=lambda r: (-r["high"], -r["employees"], r["skill"]))[:5]
    for row in top_gaps:
        row["gap"] = round(row["total_gap"] / row["employees"], 1)
    dashboard = {
        "metrics": {
            "readiness": f"{reports['average_readiness']}%", "members": len(members),
            "gaps": reports["priority_gaps"],
            "pending": sum(1 for m in members if m["not_assessed"]),
        },
        "team": members,
        "gaps": top_gaps,
    }
    return {"dashboard": dashboard, "team": members, "matrix": matrix, "gaps": gaps, "reports": reports}


def get_team_skills(manager_id: str):
    return get_manager_team_views(manager_id)["team"]


def get_skill_matrix(manager_id: str):
    return get_manager_team_views(manager_id)["matrix"]


def get_team_skill_gaps(manager_id: str):
    return get_manager_team_views(manager_id)["gaps"]


def get_manager_reports(manager_id: str):
    return get_manager_team_views(manager_id)["reports"]


def get_manager_dashboard(manager_id="M001"):
    return get_manager_team_views(manager_id)["dashboard"]


def get_admin_dashboard():
    return {"metrics": {"users": len(USERS), "managers": len([u for u in USERS if u["role"] == "manager"]), "smes": len([u for u in USERS if u["role"] == "sme"]), "roles": 4, "skills": len(SKILLS), "questions": len(QUESTIONS), "assessments": len(ASSESSMENTS)}}


def get_employee_results(attempt_id, user_id="U001"):
    """Backward-compatible service name used by employee routes/dashboard."""
    return get_assessment_results(attempt_id, user_id)


def get_learning_recommendations(user_id="U001", proficiency=None):
    if user_id != "U001":
        return []
    if not proficiency:
        return deepcopy(LEARNING)
    gap_items = [p for p in proficiency if p["gap"] > 0]
    catalogue = {
        "AP Controls & Compliance": ("Advanced AP Controls", "Course · 2.5 hrs", "High"),
        "Bank Reconciliations (SOX/IPE)": ("Bank Reconciliation Workshop", "Workshop · 3 hrs", "Medium"),
        "Invoice Processing & 3-Way Match": ("Invoice Controls Microlearning", "Microlearning · 35 min", "Medium"),
    }
    output = []
    for item in gap_items:
        title, fmt, priority = catalogue.get(item["skill"], (f"{item['skill']} Development", "Curated learning · 60 min", "Medium"))
        output.append({"title": title, "skill": item["skill"], "current": item["calculated"], "target": item["target"], "reason": f"Closes the calculated gap from {item['calculated']} toward {item['target']}.", "format": fmt, "priority": priority})
    return output


def _behavioural_case_template(case_id: str, competency: dict, employee_id: str = "U001"):
    return {
        "id": case_id,
        "employee_id": employee_id,
        "skill": competency["name"],
        "competency_id": competency["id"],
        "framework": "RD",
        "claimed_level": "Expert",
        "status": "Awaiting Evidence",
        "manager": "Manager One",
        "prompt": a4_evidence.PROMPTS[competency["id"]],
        "indicator": competency["indicator"],
        "evidence_text": "",
        "probes": [],
        "a4_proposal": None,
    }


def _ensure_phase6_evidence_cases():
    existing = {x["id"]: x for x in phase2_data.EVIDENCE_CASES}
    # Keep the original synthetic case stable, but add the six HLD-routed
    # behavioural competency slots used by this demo phase.
    cases = []
    for index, competency in enumerate(a4_evidence.BEHAVIOURAL_COMPETENCIES, start=1):
        case_id = f"EV-RD-{index:03d}"
        row = existing.get(case_id) or _behavioural_case_template(case_id, competency)
        cases.append(row)
    phase2_data.EVIDENCE_CASES[:] = cases


def get_employee_evidence(user_id="U001"):
    _ensure_phase6_evidence_cases()
    return deepcopy([x for x in phase2_data.EVIDENCE_CASES if x["employee_id"] == user_id])


def _find_evidence_case(case_id: str, user_id: str):
    _ensure_phase6_evidence_cases()
    return next((x for x in phase2_data.EVIDENCE_CASES if x["id"] == case_id and x["employee_id"] == user_id), None)


def _upsert_manager_validation(case: dict, proposal: dict):
    validation_id = case["id"].replace("EV-", "MV-")
    queue = phase2_data.MANAGER_VALIDATION_QUEUE
    row = next((x for x in queue if x["id"] == validation_id), None)
    if not row:
        row = {
            "id": validation_id,
            "employee_id": case["employee_id"],
            "employee": next((u["name"] for u in USERS if u["id"] == case["employee_id"]), "Employee One"),
            "skill": case["skill"],
            "framework": case["framework"],
            "claimed_level": case["claimed_level"],
            "evidence": case.get("evidence_text", ""),
            "indicator": case["indicator"],
            "evaluation": proposal["evaluation"],
            "status": "Pending Manager Confirmation",
            "score": f'{proposal["covered_dimensions"]}/4 evidence dimensions covered',
            "missing": ", ".join(proposal["missing"]) if proposal["missing"] else "No material evidence dimension flagged.",
            "confirmed_level": None,
            "manager_note": "",
            "a4_confidence": proposal["confidence"],
            "a4_probes": proposal["probe_count"],
        }
        queue.append(row)
    else:
        row.update({
            "evidence": case.get("evidence_text", ""),
            "evaluation": proposal["evaluation"],
            "score": f'{proposal["covered_dimensions"]}/4 evidence dimensions covered',
            "missing": ", ".join(proposal["missing"]) if proposal["missing"] else "No material evidence dimension flagged.",
            "status": "Pending Manager Confirmation",
            "a4_confidence": proposal["confidence"],
            "a4_probes": proposal["probe_count"],
        })


def _run_a4(case: dict):
    governance.record_agent_run("A4", "evidence assessment", estimated_tokens=7000, metadata={"case_id": case.get("id"), "competency_id": case.get("competency_id")})
    competency = next(c for c in a4_evidence.BEHAVIOURAL_COMPETENCIES if c["id"] == case["competency_id"])
    proposal = a4_evidence.assess_evidence(competency, case.get("evidence_text", ""), case.get("probes", []))
    case["a4_proposal"] = proposal
    if proposal["sufficient"]:
        case["status"] = "Pending Manager Confirmation"
        _upsert_manager_validation(case, proposal)
    elif proposal["probe_count"] >= a4_evidence.MAX_PROBES:
        case["status"] = "Needs More Evidence"
    else:
        case["status"] = "Evidence Conversation"
    return proposal


def submit_evidence(case_id: str, user_id: str, evidence: str):
    case = _find_evidence_case(case_id, user_id)
    clean = a4_evidence.sanitise_employee_text(evidence or "")
    if not case or len(clean) < 20:
        return False
    case["evidence_text"] = clean
    case["probes"] = []
    _run_a4(case)
    return True


def answer_evidence_probe(case_id: str, user_id: str, answer: str):
    case = _find_evidence_case(case_id, user_id)
    clean = a4_evidence.sanitise_employee_text(answer or "", 1500)
    if not case:
        return {"ok": False, "reason": "Evidence case not found."}
    proposal = case.get("a4_proposal") or _run_a4(case)
    if not proposal.get("next_probe"):
        return {"ok": False, "reason": "No probe is currently required."}
    if len(clean) < 10:
        return {"ok": False, "reason": "Please provide a specific answer to the probe."}
    case.setdefault("probes", []).append({"question": proposal["next_probe"], "answer": clean})
    _run_a4(case)
    return {"ok": True, "proposal": deepcopy(case["a4_proposal"])}


def get_manager_validation_queue(manager_id="M001"):
    _ensure_phase6_evidence_cases()
    team_ids = {u["id"] for u in _team(manager_id)}
    return deepcopy([x for x in phase2_data.MANAGER_VALIDATION_QUEUE if x["employee_id"] in team_ids])


def confirm_manager_validation(validation_id: str, manager_id="M001", decision="confirm", note="", confirmed_level=None):
    for item in phase2_data.MANAGER_VALIDATION_QUEUE:
        if item["id"] != validation_id:
            continue
        team_ids = {u["id"] for u in _team(manager_id)}
        if item["employee_id"] not in team_ids:
            return False
        clean_note = " ".join((note or "").split())[:1000]
        if decision == "confirm":
            allowed = [x["code"] for x in __import__("domain.frameworks", fromlist=["FRAMEWORKS"]).FRAMEWORKS["rd"]["levels"]]
            labels = [x["label"] for x in __import__("domain.frameworks", fromlist=["FRAMEWORKS"]).FRAMEWORKS["rd"]["levels"]]
            selected = confirmed_level or item.get("claimed_level")
            if selected not in allowed + labels:
                return False
            item["status"] = "Confirmed"
            item["manager_note"] = clean_note or "Confirmed based on submitted evidence."
            item["confirmed_by"] = manager_id
            item["confirmed_level"] = selected
            item["decision_boundary"] = "Manager-confirmed level; A4 did not set the level."
        elif decision == "send_back":
            item["status"] = "Sent Back"
            item["manager_note"] = clean_note or "Please provide additional evidence."
            item["confirmed_level"] = None
        else:
            return False
        return True
    return False

def get_employee_dashboard(user_id="U001"):
    rows = get_employee_skills(user_id)
    attempts = get_employee_results("ATT_1001", user_id)
    return {
        "summary": {"skill_level": get_skill_summary(user_id)["average"], "skills_tracked": len(rows), "assessments": 1 if attempts else 0, "gaps": len([r for r in rows if r["gap"] > 0]), "readiness": 72, "learning_count": len(attempts["recommendations"]) if attempts else 0},
        "skills": rows,
        "assessment": get_assessment("A1001", user_id),
        "learning": attempts["recommendations"] if attempts else get_learning_recommendations(user_id),
    }


# --- Phase 4: governed question-set approval -> scheduler gate -> assignment ---
BLUEPRINT_ID = "BP-1001"
SCHEDULES = []
ASSESSMENT_ASSIGNMENTS = []

def get_blueprint(blueprint_id=BLUEPRINT_ID):
    if blueprint_id != BLUEPRINT_ID:
        return None
    return deepcopy(demo_data.BLUEPRINT)

def _blueprint_requirements(blueprint=None):
    blueprint = blueprint or demo_data.BLUEPRINT
    rows = []
    for item in blueprint["competencies"]:
        rows.append({"skill": item["name"], "target": item["target"], "required": item["items"]})
    return rows

def get_question_gate(blueprint_id=BLUEPRINT_ID):
    blueprint = get_blueprint(blueprint_id)
    if not blueprint:
        return None
    role = blueprint["role"]
    requirements = _blueprint_requirements(blueprint)
    approved = [q for q in QUESTIONS if q.get("status") == "Approved" and q.get("role", role) == role]
    # Questions created before Phase 4 have role added at the service boundary.
    approved_by_skill = {}
    for q in approved:
        approved_by_skill[q["skill"]] = approved_by_skill.get(q["skill"], 0) + 1
    requirement_rows = []
    total_required = 0
    total_approved = 0
    for req in requirements:
        matched = approved_by_skill.get(req["skill"], 0)
        requirement_rows.append({**req, "approved": matched, "remaining": max(0, req["required"] - matched), "ready": matched >= req["required"]})
        total_required += req["required"]
        total_approved += min(matched, req["required"])
    # Existing demo Reconciliation naming is mapped to Bank Reconciliations.
    for row in requirement_rows:
        if row["skill"] == "Reconciliation":
            alt = approved_by_skill.get("Bank Reconciliations (SOX/IPE)", 0) + approved_by_skill.get("Bank Reconciliations", 0)
            row["approved"] = alt
            row["remaining"] = max(0, row["required"] - alt)
            row["ready"] = alt >= row["required"]
    total_approved = sum(min(r["approved"], r["required"]) for r in requirement_rows)
    return {"blueprint": blueprint, "role": role, "requirements": requirement_rows, "required": total_required, "approved": total_approved, "remaining": max(0, total_required-total_approved), "ready": total_approved >= total_required and all(r["ready"] for r in requirement_rows)}

def get_scheduler_schedule(blueprint_id=BLUEPRINT_ID):
    gate = get_question_gate(blueprint_id)
    existing = next((s for s in SCHEDULES if s["blueprint_id"] == blueprint_id), None)
    return {"gate": gate, "schedule": deepcopy(existing), "assignments": deepcopy([a for a in ASSESSMENT_ASSIGNMENTS if a["blueprint_id"] == blueprint_id])}

def launch_schedule(blueprint_id=BLUEPRINT_ID, due_date="30 Sep 2026", duration=30, due_iso=None):
    gate = get_question_gate(blueprint_id)
    if not gate or not gate["ready"]:
        return {"ok": False, "reason": "Scheduling is blocked until all required questions are SME-approved.", "gate": gate}
    existing = next((s for s in SCHEDULES if s["blueprint_id"] == blueprint_id), None)
    if existing:
        return {"ok": True, "schedule": deepcopy(existing), "created": False}
    schedule = {"id": f"SCH-{1001+len(SCHEDULES)}", "blueprint_id": blueprint_id, "assessment_id": f"ASG-{1001+len(SCHEDULES)}", "name": f'{gate["role"]} Skill Assessment', "role": gate["role"], "framework": gate["blueprint"]["framework"], "question_count": gate["required"], "duration": int(duration), "due_date": due_date, "due_iso": due_iso, "status": "Scheduled", "created_on": date.today().strftime("%d %b %Y")}
    SCHEDULES.append(schedule)
    return {"ok": True, "schedule": deepcopy(schedule), "created": True}

def assign_assessment(schedule_id, employee_id="U001"):
    schedule = next((s for s in SCHEDULES if s["id"] == schedule_id), None)
    if not schedule:
        return {"ok": False, "reason": "Schedule not found."}
    if any(a["schedule_id"] == schedule_id and a["employee_id"] == employee_id for a in ASSESSMENT_ASSIGNMENTS):
        return {"ok": True, "created": False, "assignment": deepcopy(next(a for a in ASSESSMENT_ASSIGNMENTS if a["schedule_id"] == schedule_id and a["employee_id"] == employee_id))}
    employee = workflow_db.get_user(employee_id)
    if not employee or employee["role"] != "employee":
        return {"ok": False, "reason": "Employee not found."}
    assignment = {"id": f"AA-{1001+len(ASSESSMENT_ASSIGNMENTS)}", "schedule_id": schedule_id, "blueprint_id": schedule["blueprint_id"], "assessment_id": schedule["assessment_id"], "name": schedule["name"], "employee_id": employee_id, "employee": employee["full_name"], "role": employee["job_title"], "due_date": schedule["due_date"], "due_iso": schedule.get("due_iso"), "duration": schedule["duration"], "question_count": schedule["question_count"], "status": "Assigned", "assigned_on": date.today().strftime("%d %b %Y")}
    ASSESSMENT_ASSIGNMENTS.append(assignment)
    return {"ok": True, "created": True, "assignment": deepcopy(assignment)}

def get_scheduler_assignments():
    return deepcopy(ASSESSMENT_ASSIGNMENTS)

def get_schedules():
    return deepcopy(SCHEDULES)


# --- Phase 7: A5 development plan / TNI ------------------------------------
# A5 is a proposal generator. The service validates its schema before the
# proposal crosses into the presentation layer. Proficiency remains owned by
# deterministic domain logic and manager confirmation.
DEVELOPMENT_PLANS = {}


def _finops_plan_gaps(user_id: str):
    gaps = []
    attempt = phase2_data.ATTEMPTS.get("ATT_1001")
    if attempt and attempt.get("proficiency"):
        for row in attempt["proficiency"]:
            if int(row.get("gap", 0)) > 0:
                gaps.append({
                    "skill": row["skill"],
                    "framework": "FinOps",
                    "current": row["calculated"],
                    "target": row["target"],
                    "gap": int(row["gap"]),
                    "criticality": "High" if int(row["gap"]) >= 2 else "Medium",
                    "source": "assessment calculation",
                })
        return gaps

    for row in get_employee_skills(user_id):
        if row["framework"] != "FinOps" or int(row["gap"]) <= 0:
            continue
        gaps.append({
            "skill": row["name"],
            "framework": "FinOps",
            "current": row["current_level"],
            "target": row["target_level"],
            "gap": int(row["gap"]),
            "criticality": "High" if int(row["gap"]) >= 2 else "Medium",
            "source": "role target baseline",
        })
    return gaps


def _rd_confirmed_gaps(user_id: str):
    rows = []
    skill_targets = {x["name"]: x["target_level"] for x in get_employee_skills(user_id) if x["framework"] == "RD"}
    current_rows = {x["name"]: x["current_level"] for x in get_employee_skills(user_id) if x["framework"] == "RD"}
    for item in phase2_data.MANAGER_VALIDATION_QUEUE:
        if item.get("employee_id") != user_id or item.get("framework") != "RD" or item.get("status") != "Confirmed":
            continue
        current = item.get("confirmed_level") or current_rows.get(item["skill"], item.get("claimed_level", "Beginner"))
        target = skill_targets.get(item["skill"], "Expert")
        gap = assessment_domain.calculate_gap("RD", current, target)
        if gap > 0:
            rows.append({
                "skill": item["skill"],
                "framework": "RD",
                "current": current,
                "target": target,
                "gap": gap,
                "criticality": "High" if gap >= 2 else "Medium",
                "source": "manager-confirmed evidence",
            })
    return rows


def get_a5_development_gaps(user_id="U001"):
    return _finops_plan_gaps(user_id) + _rd_confirmed_gaps(user_id)


def _generate_development_plan(user_id="U001"):
    governance.record_agent_run("A5", "development plan", estimated_tokens=3000, metadata={"user_id": user_id})
    gaps = get_a5_development_gaps(user_id)
    proposal = a5_development.build_proposal(gaps)
    validation = development_domain.validate_proposal(proposal)
    if not validation["valid"]:
        return {"status": "Invalid Proposal", "revision": 0, "gaps": [], "steps": [], "errors": validation["errors"], "total_steps": 0, "estimated_minutes": 0, "agent_note": "A5 proposal failed domain validation."}
    # HLD allows one bounded revision when feasibility/coverage checks require it.
    if proposal["steps"] and len(proposal["steps"]) > 6:
        proposal = a5_development.revise_proposal(proposal)
        validation = development_domain.validate_proposal(proposal)
        if not validation["valid"]:
            return {**proposal, "status": "Invalid Proposal", "errors": validation["errors"]}
    proposal["generated_for"] = user_id
    proposal["generated_from"] = [g["source"] for g in gaps]
    return proposal


def get_employee_development_plan(user_id="U001", regenerate=False):
    if user_id != "U001":
        return {"status": "No Confirmed Gaps", "revision": 0, "gaps": [], "steps": [], "total_steps": 0, "estimated_minutes": 0, "agent_note": "No development plan is available."}
    if regenerate or user_id not in DEVELOPMENT_PLANS:
        DEVELOPMENT_PLANS[user_id] = _generate_development_plan(user_id)
    return deepcopy(DEVELOPMENT_PLANS[user_id])


def get_manager_development_plans(manager_id="M001"):
    """Agent 5 plans from the team's assessment cycles (latest cycle per report that has one)."""
    conn = workflow_db.get_conn()
    try:
        rows = conn.execute(
            """
            SELECT ac.cycle_id, ac.user_id, ac.status, ac.dev_plan_json, u.full_name
            FROM assessment_cycles ac JOIN users u ON u.user_id = ac.user_id
            WHERE u.manager_id = ? AND ac.dev_plan_json IS NOT NULL
            ORDER BY ac.updated_at DESC
            """,
            (manager_id,),
        ).fetchall()
    finally:
        conn.close()
    result, seen = [], set()
    for row in rows:
        if row["user_id"] in seen:
            continue
        seen.add(row["user_id"])
        plan = json.loads(row["dev_plan_json"])
        steps = plan.get("steps") or []
        gaps = plan.get("all_confirmed_gaps") or []
        result.append({
            "employee": row["full_name"],
            "employee_id": row["user_id"],
            "cycle_id": row["cycle_id"],
            "status": (plan.get("status") or "").replace("_", " ").capitalize() or "—",
            "approved": row["status"] == "manager_approved",
            "steps": len(steps),
            "priority": "High" if any(g.get("gap_severity") == "High" for g in gaps) else "Moderate",
            "skills": ", ".join(dict.fromkeys(x.get("skill", "") for x in steps)) or "—",
        })
    return result


# ---------------- Phase 8: Leader insights / reports ----------------
# All values are synthetic and deterministic. Leader views consume aggregated
# domain/service data only; they do not call an LLM and never change employee levels.
LEADER_MOVEMENT = {
    "finops": [
        {"period": "Apr", "l2_plus": 54, "l3_plus": 31, "l4_plus": 11},
        {"period": "May", "l2_plus": 58, "l3_plus": 35, "l4_plus": 14},
        {"period": "Jun", "l2_plus": 61, "l3_plus": 39, "l4_plus": 17},
        {"period": "Jul", "l2_plus": 64, "l3_plus": 43, "l4_plus": 20},
        {"period": "Aug", "l2_plus": 68, "l3_plus": 47, "l4_plus": 23},
        {"period": "Sep", "l2_plus": 71, "l3_plus": 51, "l4_plus": 26},
    ],
    "rd": [
        {"period": "Apr", "beginner": 31, "moderate": 54, "expert": 15},
        {"period": "May", "beginner": 28, "moderate": 56, "expert": 16},
        {"period": "Jun", "beginner": 25, "moderate": 58, "expert": 17},
        {"period": "Jul", "beginner": 22, "moderate": 59, "expert": 19},
        {"period": "Aug", "beginner": 20, "moderate": 58, "expert": 22},
        {"period": "Sep", "beginner": 18, "moderate": 57, "expert": 25},
    ],
}

LEADER_COVERAGE = {
    "finops": [
        {"skill": "AP Controls & Compliance", "criticality": "Critical", "coverage": 62, "target": 80, "gap": 18, "affected": 18},
        {"skill": "Bank Reconciliations (SOX/IPE)", "criticality": "Critical", "coverage": 68, "target": 85, "gap": 17, "affected": 14},
        {"skill": "Invoice Processing & 3-Way Match", "criticality": "High", "coverage": 76, "target": 85, "gap": 9, "affected": 11},
        {"skill": "Journal Entries & Account Analysis", "criticality": "High", "coverage": 79, "target": 88, "gap": 9, "affected": 9},
        {"skill": "Cash Application & Matching", "criticality": "Medium", "coverage": 84, "target": 85, "gap": 1, "affected": 4},
    ],
    "rd": [
        {"skill": "Stakeholder Communication", "criticality": "Critical", "coverage": 58, "target": 80, "gap": 22, "affected": 16},
        {"skill": "Ownership & Accountability", "criticality": "Critical", "coverage": 64, "target": 82, "gap": 18, "affected": 13},
        {"skill": "Problem Solving", "criticality": "High", "coverage": 71, "target": 85, "gap": 14, "affected": 12},
        {"skill": "Collaboration", "criticality": "High", "coverage": 77, "target": 85, "gap": 8, "affected": 8},
        {"skill": "Customer Focus", "criticality": "Medium", "coverage": 83, "target": 85, "gap": 2, "affected": 5},
    ],
}

LEADER_ITEM_ANALYSIS = [
    {"id": "Q-1007", "skill": "AP Controls & Compliance", "correct_rate": 42, "discrimination": 0.12, "flags": 18, "signal": "Training signal", "reason": "Low success rate is concentrated across otherwise consistent AP control items."},
    {"id": "Q-1012", "skill": "Bank Reconciliations", "correct_rate": 61, "discrimination": 0.31, "flags": 7, "signal": "Monitor", "reason": "Moderate difficulty with acceptable separation between higher and lower performers."},
    {"id": "Q-1002", "skill": "Invoice Processing & 3-Way Match", "correct_rate": 84, "discrimination": 0.48, "flags": 2, "signal": "Healthy", "reason": "Strong separation and expected success rate for the target level."},
    {"id": "Q-1018", "skill": "Cash Application & Matching", "correct_rate": 91, "discrimination": 0.44, "flags": 1, "signal": "Review difficulty", "reason": "Very high success rate may indicate an item that is too easy for the cohort."},
]

LEADER_CALIBRATION = [
    {"manager": "Manager One", "reviews": 14, "confirmed": 4, "sent_back": 10, "evidence_attached": 12, "avg_decision_sec": 38, "signal": "Review pattern", "note": "High send-back rate; most decisions include evidence, so this remains an advisory calibration signal."},
    {"manager": "Manager Two", "reviews": 11, "confirmed": 8, "sent_back": 3, "evidence_attached": 10, "avg_decision_sec": 31, "signal": "Within range", "note": "Decision pattern is broadly aligned with the team distribution."},
    {"manager": "Manager Three", "reviews": 9, "confirmed": 7, "sent_back": 2, "evidence_attached": 8, "avg_decision_sec": 27, "signal": "Within range", "note": "Fast decisions with evidence attached on most validations."},
]

def get_leader_critical_coverage(framework_id="finops"):
    return deepcopy(LEADER_COVERAGE.get(framework_id, LEADER_COVERAGE["finops"]))

def get_leader_movement(framework_id="finops"):
    return deepcopy(LEADER_MOVEMENT.get(framework_id, LEADER_MOVEMENT["finops"]))

def get_leader_item_analysis():
    return deepcopy(LEADER_ITEM_ANALYSIS)

def get_leader_calibration():
    return deepcopy(LEADER_CALIBRATION)

def get_leader_insights(framework_id="finops"):
    coverage = get_leader_critical_coverage(framework_id)
    movement = get_leader_movement(framework_id)
    distribution = get_leader_distribution(framework_id)
    return {
        "framework": framework_id,
        "distribution": distribution,
        "coverage": coverage,
        "movement": movement,
        "item_analysis": get_leader_item_analysis(),
        "calibration": get_leader_calibration(),
        "headline": {
            "critical_skills_below_target": sum(1 for x in coverage if x["criticality"] == "Critical" and x["coverage"] < x["target"]),
            "average_critical_coverage": round(sum(x["coverage"] for x in coverage if x["criticality"] == "Critical") / max(1, sum(1 for x in coverage if x["criticality"] == "Critical"))),
            "latest_highest_level_share": distribution[-1]["value"] if distribution else 0,
            "movement_delta": movement[-1]["l4_plus"] - movement[0]["l4_plus"] if framework_id == "finops" else movement[-1]["expert"] - movement[0]["expert"],
        },
    }


# ---------------------------------------------------------------------------
# Phase 11C — generated backend assessment session store
# ---------------------------------------------------------------------------
# The supplied backend is intentionally stateless for /api/generate and
# /api/score.  Keep the generated question set server-side in this Jinja
# process so the browser does not need to carry the answer key back to the
# server.  This is a prototype/session bridge until the backend exposes
# persistent assessment/attempt APIs.
BACKEND_GENERATED_ASSESSMENTS = {}
BACKEND_GENERATED_ATTEMPTS = {}

def _normalize_backend_items(items):
    normalized = []
    for index, item in enumerate(items or [], start=1):
        options = [item.get(k) for k in ("option_a", "option_b", "option_c", "option_d") if item.get(k) is not None]
        normalized.append({
            "number": index,
            "question_id": item.get("question_id") or f"GEN-Q-{index}",
            "skill": item.get("skill", "General Skill"),
            "level": f"L{item.get('target_proficiency_level', 1)}",
            "text": item.get("question_text", item.get("text", "")),
            "options": options,
            "correct": item.get("correct_option"),
            "question_type": "mcq",
            "framework": item.get("framework", "FinOps"),
            "target_proficiency_level": item.get("target_proficiency_level", 1),
            "is_critical": bool(item.get("is_critical", False)),
            "difficulty": item.get("difficulty", "Role Ready"),
            "sop_reference": item.get("sop_reference", item.get("generated_prompt_context", "")),
            # Preserve the exact backend item for /api/score.
            "backend_item": deepcopy(item),
        })
    return normalized

def create_backend_assessment(result):
    import uuid
    assessment_id = f"AI-{uuid.uuid4().hex[:10].upper()}"
    items = _normalize_backend_items(result.get("items", []))
    assessment = {
        "id": assessment_id,
        "name": f"AI Assessment · {result.get('match', {}).get('role_name', 'Matched Role')}",
        "role": result.get("match", {}).get("role_name", "Matched Role"),
        "role_id": result.get("match", {}).get("role_id"),
        "role_grade": result.get("match", {}).get("role_grade"),
        "framework": result.get("match", {}).get("framework", "FinOps"),
        "duration": max(5, len(items) * 2),
        "question_set": items,
        "source": "backend /api/generate",
        "status": "Assigned",
    }
    BACKEND_GENERATED_ASSESSMENTS[assessment_id] = assessment
    return deepcopy(assessment)

def get_backend_generated_assessment(assessment_id):
    return deepcopy(BACKEND_GENERATED_ASSESSMENTS.get(assessment_id))

def submit_backend_generated_assessment(assessment_id, user_id, answers):
    assessment = BACKEND_GENERATED_ASSESSMENTS.get(assessment_id)
    if not assessment:
        return None
    parsed = assessment_domain.parse_answers(answers)
    backend_answers = {}
    for q in assessment["question_set"]:
        selected = parsed.get(str(q["number"])) or parsed.get(q["question_id"])
        if selected:
            backend_answers[q["question_id"]] = selected
    if backend_api.enabled:
        score = backend_api.score([q["backend_item"] for q in assessment["question_set"]], backend_answers)
    else:
        # Local fallback keeps the vertical slice testable without the backend.
        score = assessment_domain.score_questions(assessment["question_set"], parsed)
    import uuid
    attempt_id = f"AI-ATT-{uuid.uuid4().hex[:10].upper()}"
    result = {
        "attempt_id": attempt_id,
        "assessment": assessment["name"],
        "assessment_id": assessment_id,
        "completed_on": date.today().strftime("%d %b %Y"),
        "score": score.get("overall_score_pct", score.get("score", 0)),
        "overall_score_pct": score.get("overall_score_pct", score.get("score", 0)),
        "correct": sum(1 for x in score.get("results", []) if x.get("is_correct")),
        "total": len(assessment["question_set"]),
        "answered": len(score.get("results", [])),
        "unanswered": score.get("unanswered", []),
        "by_skill": score.get("by_skill", []),
        "item_results": score.get("results", []),
        "backend_score": deepcopy(score),
        "status": "Completed",
        "decision_note": "Score returned by the backend team's /api/score. This flow does not set proficiency level.",
    }
    BACKEND_GENERATED_ATTEMPTS[attempt_id] = result
    return {"attempt_id": attempt_id}

def get_backend_generated_results(attempt_id, user_id):
    return deepcopy(BACKEND_GENERATED_ATTEMPTS.get(attempt_id))

# ---------------------------------------------------------------------------
# Phase 11 — REST backend integration
# ---------------------------------------------------------------------------
# Keep the Phase 1–10 deterministic implementation intact as the local demo
# adapter.  In TALENT360_API_MODE=remote, exported service functions are
# rebound to this thin REST adapter. Templates and route handlers therefore do
# not change when the backend team replaces the mock with the real API.
from services.backend_api import backend_api, call as _backend_call, BackendAPIError


def _remote_user_headers(user_id=None, role=None):
    headers = {}
    # These headers are correlation hints only. Production authorization must
    # be enforced by the backend/SSO layer; never treat these as trust anchors.
    if user_id:
        headers["X-Talent360-User-Id"] = user_id
    if role:
        headers["X-Talent360-Role"] = role
    return headers


def _remote_get_current_user():
    return _backend_call("get_current_user")

def _remote_get_role_skills():
    return _backend_call("get_role_skills")

def _remote_get_user_skills(status="All"):
    return _backend_call("get_user_skills", params={"status": status})

def _remote_get_assessments():
    return _backend_call("get_assessments")

def _remote_get_sme_questions(status="All"):
    return _backend_call("get_sme_questions", params={"status": status})

def _remote_get_questions(status="All"):
    return _backend_call("get_questions", params={"status": status})

def _remote_get_question(question_id):
    return _backend_call("get_question", path_params={"question_id": question_id})

def _remote_approve_question(question_id, approved_by="SME Reviewer"):
    return bool(_backend_call("approve_question", path_params={"question_id": question_id}, body={"approved_by": approved_by}))

def _remote_reject_question(question_id, reason):
    return bool(_backend_call("reject_question", path_params={"question_id": question_id}, body={"reason": reason}))

def _remote_edit_question(question_id, text, rationale, correct, options, level=None):
    result = _backend_call("edit_question", path_params={"question_id": question_id}, body={"text": text, "rationale": rationale, "correct": correct, "options": options, "level": level})
    return bool(result)

def _remote_regenerate_question(question_id):
    return bool(_backend_call("regenerate_question", path_params={"question_id": question_id}))

def _remote_generate_question_set(role, skill, level, count, question_type="mcq"):
    return _backend_call("generate_question_set", body={"role": role, "skill": skill, "level": level, "count": int(count), "question_type": question_type})

def _remote_get_sme_generation_summary():
    return _backend_call("get_sme_generation_summary")

def _remote_get_sme_dashboard():
    return _backend_call("get_sme_dashboard")

def _remote_get_scheduler_dashboard():
    return _backend_call("get_scheduler_dashboard")

def _remote_get_leader_dashboard():
    return _backend_call("get_leader_dashboard")

def _remote_get_leader_distribution(framework_id="finops"):
    return _backend_call("get_leader_distribution", params={"framework": framework_id})

def _remote_get_employee_skills(user_id: str, status: str = "All"):
    return _backend_call("get_employee_skills", params={"user_id": user_id, "status": status}, headers=_remote_user_headers(user_id))

def _remote_get_skill_summary(user_id="U001"):
    return _backend_call("get_skill_summary", params={"user_id": user_id}, headers=_remote_user_headers(user_id))

def _remote_get_assessment(assessment_id, user_id="U001"):
    generated = get_backend_generated_assessment(assessment_id)
    if generated:
        return generated
    # The supplied backend does not expose a persistent assessment GET route yet.
    raise BackendAPIError("Generated assessment session was not found in the frontend session store.")

def _remote_submit_assessment(assessment_id, user_id="U001", answers=None):
    if get_backend_generated_assessment(assessment_id):
        return submit_backend_generated_assessment(assessment_id, user_id, answers or {})
    raise BackendAPIError("This assessment was not generated by the connected backend pipeline.")

def _remote_get_assessment_results(attempt_id, user_id="U001"):
    generated = get_backend_generated_results(attempt_id, user_id)
    if generated:
        return generated
    raise BackendAPIError("Generated assessment result was not found in the frontend session store.")

def _remote_get_a3_escalations(status="all"):
    return _backend_call("get_a3_escalations", params={"status": status})

def _remote_decide_a3_escalation(escalation_id, decision="accept", note=""):
    return bool(_backend_call("decide_a3_escalation", path_params={"escalation_id": escalation_id}, body={"decision": decision, "note": note}))

def _remote_get_employee_assessments(user_id: str):
    return _backend_call("get_employee_assessments", params={"user_id": user_id}, headers=_remote_user_headers(user_id))

def _remote_get_users():
    return _backend_call("get_users")

def _remote_get_team_skills(manager_id: str):
    return _backend_call("get_team_skills", params={"manager_id": manager_id}, headers=_remote_user_headers(manager_id, "manager"))

def _remote_get_skill_matrix(manager_id: str):
    return _backend_call("get_skill_matrix", params={"manager_id": manager_id}, headers=_remote_user_headers(manager_id, "manager"))

def _remote_get_team_skill_gaps(manager_id: str):
    return _backend_call("get_team_skill_gaps", params={"manager_id": manager_id}, headers=_remote_user_headers(manager_id, "manager"))

def _remote_get_manager_reports(manager_id: str):
    return _backend_call("get_manager_reports", params={"manager_id": manager_id}, headers=_remote_user_headers(manager_id, "manager"))

def _remote_get_manager_dashboard(manager_id="M001"):
    return _backend_call("get_manager_dashboard", params={"manager_id": manager_id}, headers=_remote_user_headers(manager_id, "manager"))

def _remote_get_admin_dashboard():
    return _backend_call("get_admin_dashboard")

def _remote_get_employee_results(attempt_id, user_id="U001"):
    return _remote_get_assessment_results(attempt_id, user_id)

def _remote_get_learning_recommendations(user_id="U001", proficiency=None):
    return _backend_call("get_learning_recommendations", params={"user_id": user_id, "proficiency": proficiency}, headers=_remote_user_headers(user_id))

def _remote_get_employee_evidence(user_id="U001"):
    return _backend_call("get_employee_evidence", params={"user_id": user_id}, headers=_remote_user_headers(user_id))

def _remote_submit_evidence(case_id: str, user_id: str, evidence: str):
    return bool(_backend_call("submit_evidence", path_params={"case_id": case_id}, body={"user_id": user_id, "evidence": evidence}, headers=_remote_user_headers(user_id)))

def _remote_answer_evidence_probe(case_id: str, user_id: str, answer: str):
    result = _backend_call("answer_evidence_probe", path_params={"case_id": case_id}, body={"user_id": user_id, "answer": answer}, headers=_remote_user_headers(user_id))
    return result if isinstance(result, dict) else {"ok": bool(result)}

def _remote_get_manager_validation_queue(manager_id="M001"):
    return _backend_call("get_manager_validation_queue", params={"manager_id": manager_id}, headers=_remote_user_headers(manager_id, "manager"))

def _remote_confirm_manager_validation(validation_id: str, manager_id="M001", decision="confirm", note="", confirmed_level=None):
    return bool(_backend_call("confirm_manager_validation", path_params={"validation_id": validation_id}, body={"manager_id": manager_id, "decision": decision, "note": note, "confirmed_level": confirmed_level}, headers=_remote_user_headers(manager_id, "manager")))

def _remote_get_employee_dashboard(user_id="U001"):
    return _backend_call("get_employee_dashboard", params={"user_id": user_id}, headers=_remote_user_headers(user_id, "employee"))

def _remote_get_blueprint(blueprint_id=BLUEPRINT_ID):
    return _backend_call("get_blueprint", path_params={"blueprint_id": blueprint_id})

def _remote_get_question_gate(blueprint_id=BLUEPRINT_ID):
    return _backend_call("get_question_gate", path_params={"blueprint_id": blueprint_id})

def _remote_get_scheduler_schedule(blueprint_id=BLUEPRINT_ID):
    return _backend_call("get_scheduler_schedule", params={"blueprint_id": blueprint_id})

def _remote_launch_schedule(blueprint_id=BLUEPRINT_ID, due_date="30 Sep 2026", duration=30):
    return _backend_call("launch_schedule", body={"blueprint_id": blueprint_id, "due_date": due_date, "duration": int(duration)})

def _remote_assign_assessment(schedule_id, employee_id="U001"):
    return _backend_call("assign_assessment", body={"schedule_id": schedule_id, "employee_id": employee_id})

def _remote_get_scheduler_assignments():
    return _backend_call("get_scheduler_assignments")

def _remote_get_schedules():
    return _backend_call("get_schedules")

def _remote_get_a5_development_gaps(user_id="U001"):
    return _backend_call("get_a5_development_gaps", params={"user_id": user_id}, headers=_remote_user_headers(user_id))

def _remote_get_employee_development_plan(user_id="U001", regenerate=False):
    return _backend_call("get_employee_development_plan", params={"user_id": user_id, "regenerate": str(bool(regenerate)).lower()}, headers=_remote_user_headers(user_id))

def _remote_get_manager_development_plans(manager_id="M001"):
    return _backend_call("get_manager_development_plans", params={"manager_id": manager_id}, headers=_remote_user_headers(manager_id, "manager"))

def _remote_get_leader_critical_coverage(framework_id="finops"):
    return _backend_call("get_leader_critical_coverage", params={"framework": framework_id})

def _remote_get_leader_movement(framework_id="finops"):
    return _backend_call("get_leader_movement", params={"framework": framework_id})

def _remote_get_leader_item_analysis():
    return _backend_call("get_leader_item_analysis")

def _remote_get_leader_calibration():
    return _backend_call("get_leader_calibration")

def _remote_get_leader_insights(framework_id="finops"):
    return _backend_call("get_leader_insights", params={"framework": framework_id})


# Do not globally rebind the Phase 1-10 service surface to the remote backend.
# The supplied backend currently exposes only the assessment pipeline
# (/api/generate, /api/score, /api/case-studies, /api/evidence). Rebinding
# dashboard/SME/scheduler/manager/leader functions to nonexistent endpoints
# makes unrelated pages fail with KeyError before the user reaches the
# connected AI assessment flow. Keep those existing demo services local until
# their corresponding backend APIs are actually delivered.
_REMOTE_FUNCTIONS = {}


if backend_api.enabled:
    globals().update(_REMOTE_FUNCTIONS)

# ---------------------------------------------------------------------------
# Phase 11: real backend-team API integration
# ---------------------------------------------------------------------------
from services.backend_api import BackendAPIError, backend_api


def backend_pipeline_enabled() -> bool:
    return backend_api.enabled


def backend_generate_resume(file_bytes: bytes, filename: str, max_items: int = 5):
    return backend_api.upload_resume(file_bytes, filename, max_items=max_items)


def backend_match_resume(file_bytes: bytes, filename: str, keywords: list[str] | None = None):
    return backend_api.match_resume(file_bytes, filename, keywords=keywords)


def backend_generate_for_role(role_id: str, max_items: int, level: int | None = None, skills: list[str] | None = None):
    return backend_api.generate_for_role(role_id, max_items, level=level, skills=skills)


def backend_score(items, answers):
    return backend_api.score(items, answers)


def backend_case_studies(role_id, score, max_questions: int = 3):
    return backend_api.case_studies(role_id, score, max_questions=max_questions)


def backend_evidence(case_studies, answers, score, role_id, role_name, role_grade, transcripts=None, timeout=None):
    return backend_api.evidence(case_studies, answers, score, role_id, role_name, role_grade, transcripts=transcripts,
                                timeout=timeout)


def backend_probe(case_study, transcript):
    return backend_api.probe(case_study, transcript)


def backend_devplan(role_id, score, evidence=None, max_steps=None):
    return backend_api.devplan(role_id, score, evidence=evidence, max_steps=max_steps)


def backend_training_outline(role_id, mode, items):
    return backend_api.training_outline(role_id, mode, items)


def backend_performance_insight(payload: dict, mode: str = "insight"):
    return backend_api.performance_insight(payload, mode=mode)


def backend_classify_evidence(text: str, skills: list[str], source: str):
    return backend_api.classify_evidence(text, skills, source)


def backend_code_review(challenge: dict, code: str, language: str):
    return backend_api.code_review(challenge, code, language)


def backend_skill_advice(payload: dict):
    return backend_api.skill_advice(payload)


def backend_resume_skills(file_bytes: bytes, filename: str, skills: list[str]):
    return backend_api.resume_skills(file_bytes, filename, skills)


def backend_performance_summary(payload: dict):
    return backend_api.performance_summary(payload)


# ---------------------------------------------------------------------------
# Employee assessment-cycle workflow: resume -> SME question approval ->
# MCQ (score hidden from employee) -> case study -> manager approval ->
# training. Real, persisted state in services/db.py -- this replaces the
# stateless sessionStorage pass-through the backend's own static frontend
# used, since SME/manager review requires state to survive between users.
# ---------------------------------------------------------------------------
from services import db as workflow_db


def get_employee_cycle(user_id: str):
    """The employee's current assessment cycle (or None if no resume uploaded yet)."""
    return workflow_db.get_active_cycle_for_user(user_id)


def start_employee_cycle(user_id: str, file_bytes: bytes, filename: str):
    """Runs the resume through Agent 1 (role match only). On a match, creates a
    cycle waiting for an SME to set up the assessment -- the SME decides how
    many questions, at what proficiency level, and which ones (bank or new).
    Returns the backend's /api/match result, plus cycle_id on success, so the
    caller can surface no_match_found / needs_human_confirmation.
    """
    from services import coding  # lazy: coding imports this module

    result = backend_match_resume(file_bytes, filename, keywords=coding.trigger_keywords())
    if isinstance(result, dict) and result.get("status") == "auto_matched":
        match = result.get("match", {})
        result["cycle_id"] = workflow_db.create_cycle(
            user_id=user_id,
            resume_filename=filename,
            resume_text=f"Uploaded: {filename}",
            matched_role_id=match.get("role_id"),
            matched_role_name=match.get("role_name"),
            matched_role_grade=match.get("role_grade"),
            match_status=result["status"],
            status="pending_sme_setup",
        )
        # Only which SME trigger keywords the resume mentions is kept -- never the resume text.
        coding.record_resume_keywords(result["cycle_id"], result.get("keyword_hits") or [])
    return result


def generate_bank_questions(role_id: str, count: int, level: int | None, skills: list[str] | None, sme_id: str) -> dict:
    """SME-requested Agent 2 generation into the question bank (pending SME review)."""
    result = backend_generate_for_role(role_id, count, level=level, skills=skills) or {}
    items = [i for i in result.get("items") or [] if i.get("question_id")]
    workflow_db.insert_bank_questions(items, sme_id)
    return {"items": items, "skills_skipped": result.get("skills_skipped") or [], "role": result.get("match") or {}}


def get_employee_takeable_assessment(cycle_id: str):
    """Builds an employee/assessment.html-shaped assessment from this
    cycle's SME-approved questions, reusing the same item normalizer the
    original AI-assessment flow used so the existing template/JS work
    unmodified.
    """
    cycle = workflow_db.get_cycle(cycle_id)
    if not cycle:
        return None
    approved = workflow_db.get_questions_for_cycle(cycle_id, sme_status="approved")
    items = [json.loads(q["item_json"]) for q in approved]
    question_set = _normalize_backend_items(items)
    return {
        "id": cycle_id,
        "name": f"Assessment · {cycle.get('matched_role_name') or 'Matched Role'}",
        "role": cycle.get("matched_role_name"),
        "question_set": question_set,
        "duration": max(5, len(question_set) * 2),
    }


def submit_employee_assessment(cycle_id: str, answers_raw: str | None):
    """Scores the MCQ round via the backend and persists the score -- never
    returned to the employee's own view, only to SME/manager queues.
    """
    approved = workflow_db.get_questions_for_cycle(cycle_id, sme_status="approved")
    items = [json.loads(q["item_json"]) for q in approved]
    question_set = _normalize_backend_items(items)
    parsed = assessment_domain.parse_answers(answers_raw)
    backend_answers = {}
    for q in question_set:
        selected = parsed.get(q["number"])
        if selected:
            backend_answers[q["question_id"]] = selected
    score = backend_score(items, backend_answers)
    workflow_db.save_answers(cycle_id, backend_answers)
    workflow_db.update_cycle(cycle_id, mcq_score_json=json.dumps(score), status="mcq_submitted")
    return score


# Agent 4 workplace simulation: SIMULATION_SCENARIOS scenarios per cycle,
# each probed up to MAX_PROBES times (the HLD's "probe at most three times").
# The transcript is persisted after every turn in case_study_answers_json:
#   {"mode": "simulation", "transcripts": {qid: [turns]}, "completed": {qid: reason}}
SIMULATION_SCENARIOS = 2
MAX_PROBES = 3
SCENARIO_END_REASONS = {
    "sufficient_evidence": "The assessor had enough evidence.",
    "probe_limit": "All follow-up questions used.",
    "ended_by_candidate": "You finished this scenario.",
    "no_response": "No response given.",
    "invalid_probe": "The assessor ended this scenario.",
    "assessor_unavailable": "The assessor was unavailable; your answers are saved as they are.",
}


def get_employee_case_studies(cycle_id: str, max_questions: int = SIMULATION_SCENARIOS):
    """Generates (once) or returns the simulation scenarios for this cycle."""
    cycle = workflow_db.get_cycle(cycle_id)
    if not cycle:
        return None
    if cycle.get("case_study"):
        return cycle["case_study"]
    result = backend_case_studies(cycle["matched_role_id"], cycle["mcq_score"], max_questions=max_questions)
    workflow_db.update_cycle(cycle_id, case_study_json=json.dumps(result))
    return result


def _simulation_state(cycle: dict) -> dict:
    state = cycle.get("case_study_answers") or {}
    if state.get("mode") != "simulation":
        state = {"mode": "simulation", "transcripts": {}, "completed": {}}
    return state


def get_simulation(cycle: dict) -> dict:
    """The cycle's scenarios with their transcripts, and which one the employee is on."""
    state = _simulation_state(cycle)
    scenarios, current = [], None
    for index, cs in enumerate(((cycle.get("case_study") or {}).get("case_studies")) or [], start=1):
        qid = cs["question_id"]
        turns = state["transcripts"].get(qid, [])
        reason = state["completed"].get(qid)
        item = {
            **cs, "index": index, "turns": turns, "done": reason is not None,
            "end_reason": SCENARIO_END_REASONS.get(reason, reason),
            "probes_used": sum(1 for t in turns if t["role"] == "agent"),
            "max_probes": min(int(cs.get("max_probes") or MAX_PROBES), MAX_PROBES),
            "responses": sum(1 for t in turns if t["role"] == "candidate"),
        }
        scenarios.append(item)
        if current is None and not item["done"]:
            current = item
    return {"scenarios": scenarios, "current": current,
            "all_done": current is None, "state": state}


def _scenario_payload(cycle: dict, question_id: str) -> dict | None:
    return next((cs for cs in (cycle.get("case_study") or {}).get("case_studies") or []
                 if cs["question_id"] == question_id), None)


def respond_to_simulation(cycle_id: str, question_id: str, response: str, meta: dict | None = None) -> dict:
    """Records the employee's response to the current scenario, then asks Agent 4
    for the next probe. Returns {"ok": bool, "reason": str | None}."""
    cycle = workflow_db.get_cycle(cycle_id)
    sim = get_simulation(cycle) if cycle else None
    current = sim["current"] if sim else None
    if not current or current["question_id"] != question_id:
        return {"ok": False, "reason": "That scenario is not the one in progress."}
    state = sim["state"]
    turn = {"role": "candidate", "text": response}
    if meta:
        turn["meta"] = meta
    turns = list(current["turns"]) + [turn]
    if current["probes_used"] >= current["max_probes"]:
        state["completed"][question_id] = "probe_limit"
    else:
        try:
            result = backend_probe(_scenario_payload(cycle, question_id), turns) or {}
        except BackendAPIError:
            state["completed"][question_id] = "assessor_unavailable"
        else:
            question = " ".join(str(result.get("question") or "").split())[:400]
            if result.get("action") == "probe" and question:
                turns.append({"role": "agent", "text": question})
            else:
                state["completed"][question_id] = result.get("reason") or "sufficient_evidence"
    state["transcripts"][question_id] = turns
    workflow_db.update_cycle(cycle_id, case_study_answers_json=json.dumps(state))
    return {"ok": True, "reason": None}


def finish_simulation_scenario(cycle_id: str, question_id: str) -> bool:
    """The employee ends the current scenario early (after answering at least once)."""
    cycle = workflow_db.get_cycle(cycle_id)
    sim = get_simulation(cycle) if cycle else None
    current = sim["current"] if sim else None
    if not current or current["question_id"] != question_id or not current["responses"]:
        return False
    sim["state"]["completed"][question_id] = "ended_by_candidate"
    workflow_db.update_cycle(cycle_id, case_study_answers_json=json.dumps(sim["state"]))
    return True


def _request_evidence(cycle: dict, sim: dict, timeout: float | None = None) -> dict:
    transcripts = {s["question_id"]: s["turns"] for s in sim["scenarios"] if s["turns"]}
    answers = {qid: next((t["text"] for t in turns if t["role"] == "candidate"), "") for qid, turns in transcripts.items()}
    return backend_evidence(
        cycle["case_study"]["case_studies"], answers, cycle["mcq_score"],
        cycle["matched_role_id"], cycle["matched_role_name"], cycle.get("matched_role_grade") or "",
        transcripts=transcripts, timeout=timeout,
    )


# A retry runs off the request path, so it can wait out a provider's rate limit.
EVIDENCE_RETRY_TIMEOUT = 240
BACKGROUND_EVIDENCE_RETRY = True  # tests switch it off to drive retries themselves


def submit_employee_case_study(cycle_id: str):
    """Hands the finished simulation to the manager queue, with Agent 4's evidence
    report when it can be generated now. If Agent 4 is unavailable (a rate limit,
    say) the submission still reaches the manager: the report is marked pending
    and retried in the background, by the lifecycle worker and from the manager's
    page, instead of stranding the employee's work. Neither the score nor the
    evidence report is ever shown back to the employee. Returns None until every
    scenario is finished, else {"evidence_pending": bool}.
    """
    cycle = workflow_db.get_cycle(cycle_id)
    if not cycle or not cycle.get("case_study"):
        return None
    sim = get_simulation(cycle)
    if not sim["all_done"]:
        return None
    try:
        evidence = _request_evidence(cycle, sim)
    except BackendAPIError:
        evidence = None
    fields = {"case_study_answers_json": json.dumps(sim["state"]), "status": "pending_manager_approval"}
    if evidence is not None:
        fields["evidence_report_json"] = json.dumps(evidence)
    workflow_db.update_cycle(cycle_id, **fields)
    if evidence is None and BACKGROUND_EVIDENCE_RETRY:
        threading.Thread(target=retry_evidence_report, args=(cycle_id,), daemon=True,
                         name=f"evidence-{cycle_id}").start()
    return {"evidence_pending": evidence is None}


def evidence_pending(cycle: dict | None) -> bool:
    """A submitted simulation still waiting for Agent 4's evidence report."""
    return bool(cycle and cycle.get("status") == "pending_manager_approval" and cycle.get("case_study")
                and cycle.get("case_study_answers") and not cycle.get("evidence_report"))


def retry_evidence_report(cycle_id: str, timeout: float = EVIDENCE_RETRY_TIMEOUT) -> bool:
    """Generates the missing evidence report for a submitted simulation. Saved only
    while the cycle still waits for the manager, so a report can never appear under
    a decision that was made without it. Returns True once the cycle has a report."""
    cycle = workflow_db.get_cycle(cycle_id)
    if not evidence_pending(cycle):
        return bool(cycle and cycle.get("evidence_report"))
    try:
        evidence = _request_evidence(cycle, get_simulation(cycle), timeout=timeout)
    except BackendAPIError:
        return False
    return workflow_db.save_pending_evidence(cycle_id, json.dumps(evidence))


def retry_pending_evidence(limit: int = 3) -> int:
    """Lifecycle-worker pass: retries the oldest submissions still missing a report."""
    done = 0
    for cycle_id in workflow_db.cycles_missing_evidence(limit):
        done += retry_evidence_report(cycle_id)
    return done


def simulation_conversations(cycle: dict) -> list[dict]:
    """Every simulation scenario with the employee's conversation, read from what the
    portal saved rather than from the AI report, so a manager or leader always sees
    it: while the report is pending, and for assessments taken before the
    simulation asked follow-ups (one written answer per scenario)."""
    saved = cycle.get("case_study_answers") or {}
    if saved.get("mode") == "simulation":
        transcripts, completed = saved.get("transcripts") or {}, saved.get("completed") or {}
    else:
        transcripts = {qid: [{"role": "candidate", "text": text}] for qid, text in saved.items()
                       if isinstance(text, str) and text.strip()}
        completed = {}
    # Agent 4 copies each transcript into its report: the fallback when nothing was saved.
    reported = {r.get("question_id"): r["transcript"] for r in ((cycle.get("evidence_report") or {}).get("evidence_results") or [])
                if r.get("transcript")}
    scenarios = ((cycle.get("case_study") or {}).get("case_studies")) or [
        {"question_id": r.get("question_id"), "skill": r.get("skill")}
        for r in ((cycle.get("evidence_report") or {}).get("evidence_results") or []) if r.get("transcript")]
    out = []
    for index, cs in enumerate(scenarios, start=1):
        turns = transcripts.get(cs.get("question_id")) or reported.get(cs.get("question_id")) or []
        reason = completed.get(cs.get("question_id"))
        out.append({
            "index": index, "question_id": cs.get("question_id"), "skill": cs.get("skill"),
            "title": cs.get("scenario_title"), "scenario": cs.get("scenario"), "task": cs.get("question_text"),
            "turns": turns, "end_reason": SCENARIO_END_REASONS.get(reason, reason),
            "responses": sum(1 for t in turns if t.get("role") == "candidate"),
            "follow_ups": sum(1 for t in turns if t.get("role") == "agent"),
        })
    return out


def generate_employee_dev_plan(cycle_id: str, max_steps=None):
    """Generates (once) or returns the cached Agent 5 dev plan for this cycle
    -- same cache-once pattern as get_employee_case_studies, so viewing it
    repeatedly (e.g. the manager validation detail page) doesn't re-trigger
    an LLM call each time.
    """
    cycle = workflow_db.get_cycle(cycle_id)
    if not cycle:
        return None
    if cycle.get("dev_plan"):
        return cycle["dev_plan"]
    result = backend_devplan(
        cycle["matched_role_id"], cycle["mcq_score"], evidence=cycle.get("evidence_report"), max_steps=max_steps
    )
    workflow_db.update_cycle(cycle_id, dev_plan_json=json.dumps(result))
    return result


# The SME reviews questions from two sources, shown in one queue:
#   "cycle" -- Agent 2 questions generated for an employee's resume cycle,
#              persisted in generated_questions (services/db.py);
#   "bank"  -- the question bank the scheduler's launch gate counts, which
#              the "AI Generation" simulator adds to (QUESTIONS, in memory).
# Both are normalised to one shape so the queue, review page, dashboard and
# approved list link to the same /sme/questions/{id} route. Before this,
# the dashboard and simulator linked bank ids to a route that only looked in
# SQLite, so every "Review" link from them was a 404.
_BANK_STATUS = {"Pending Review": "pending", "Needs Revision": "pending", "Approved": "approved", "Rejected": "rejected"}


def _options(a, b, c, d):
    return [(letter, text) for letter, text in zip("ABCD", (a, b, c, d)) if text]


def _cycle_question_view(question: dict) -> dict:
    item = json.loads(question["item_json"]) if question.get("item_json") else {}
    cycle = workflow_db.get_cycle(question["cycle_id"]) or {}
    employee = workflow_db.get_user(cycle.get("user_id", "")) or {}
    level = item.get("target_proficiency_level")
    return {
        "question_id": question["question_id"],
        "source": "cycle",
        "source_label": "Resume assessment",
        "employee_name": employee.get("full_name") or cycle.get("user_id") or "—",
        "role_name": item.get("role_name") or cycle.get("matched_role_name") or "—",
        "skill": question["skill"],
        "level_label": f"L{level}" if level is not None else "—",
        "difficulty": question.get("difficulty") or "—",
        "question_type_label": item.get("question_type") or "Multiple choice",
        "is_critical": bool(question.get("is_critical")),
        "question_text": question["question_text"],
        "options": _options(question["option_a"], question["option_b"], question["option_c"], question["option_d"]),
        "correct_option": question["correct_option"],
        "rationale": item.get("rationale"),
        "sop_reference": item.get("generated_prompt_context"),
        "is_synthetic_grounding": bool(item.get("is_synthetic_grounding")),
        "self_check": item.get("self_check") or {},
        "ai_confidence": question.get("ai_confidence"),
        "generation_attempts": item.get("generation_attempts", 1),
        "sme_status": question["sme_status"],
        "reviewed_at": question.get("reviewed_at"),
        "reviewed_by": question.get("reviewed_by"),
    }


def _bank_question_view(question: dict) -> dict:
    options = question.get("options") or []
    checks = question.get("validation_checks") or {}
    return {
        "question_id": question["id"],
        "source": "bank",
        "source_label": "Question bank",
        "employee_name": "Question bank",
        "role_name": question.get("role", "P2P Senior Associate"),
        "skill": question["skill"],
        "level_label": question.get("level") or "—",
        "difficulty": question.get("level") or "—",
        "question_type_label": (question.get("question_type") or "mcq").upper(),
        "is_critical": False,
        "question_text": question["text"],
        "options": _options(*(options + [None] * 4)[:4]),
        "correct_option": question.get("correct"),
        "rationale": question.get("rationale"),
        "sop_reference": question.get("sop_reference"),
        "is_synthetic_grounding": True,
        "self_check": {name: {"score": 5 if ok else 1, "verdict": "pass" if ok else "fail"} for name, ok in checks.items()},
        "ai_confidence": question.get("confidence"),
        "generation_attempts": 1 + int(question.get("regeneration_count", 0)),
        "sme_status": _BANK_STATUS.get(question.get("status"), "pending"),
        "reviewed_at": question.get("approved_date") if question.get("approved_date") not in (None, "—") else None,
        "reviewed_by": question.get("approved_by") if question.get("approved_by") not in (None, "—") else None,
    }


def _qbank_question_view(bank: dict) -> dict:
    item = json.loads(bank["item_json"]) if bank.get("item_json") else {}
    level = bank.get("target_level")
    reviewer = workflow_db.get_user(bank["reviewed_by"]) if bank.get("reviewed_by") else None
    return {
        "question_id": bank["question_id"],
        "source": "qbank",
        "source_label": "Question bank",
        "employee_name": "Question bank",
        "role_name": bank.get("role_name") or bank.get("role_id") or "—",
        "skill": bank["skill"],
        "level_label": f"L{level}" if level else "—",
        "difficulty": bank.get("difficulty") or "—",
        "question_type_label": item.get("question_type") or "Multiple choice",
        "is_critical": bool(bank.get("is_critical")),
        "question_text": bank["question_text"],
        "options": _options(bank["option_a"], bank["option_b"], bank["option_c"], bank["option_d"]),
        "correct_option": bank["correct_option"],
        "rationale": item.get("rationale"),
        "sop_reference": item.get("generated_prompt_context"),
        "is_synthetic_grounding": bool(item.get("is_synthetic_grounding")),
        "self_check": item.get("self_check") or {},
        "ai_confidence": bank.get("ai_confidence"),
        "generation_attempts": item.get("generation_attempts", 1),
        "sme_status": bank["sme_status"],
        "reviewed_at": bank.get("reviewed_at"),
        "reviewed_by": reviewer["full_name"] if reviewer else bank.get("reviewed_by"),
        "times_used": bank.get("times_used", 0),
    }


def _bank_questions(sme_status: str | None = None):
    views = [_bank_question_view(get_question(q["id"])) for q in QUESTIONS]
    return [v for v in views if sme_status is None or v["sme_status"] == sme_status]


def get_sme_question_detail(question_id: str):
    question = workflow_db.get_question(question_id)
    if question:
        return _cycle_question_view(question)
    bank_question = workflow_db.get_bank_question(question_id)
    if bank_question:
        return _qbank_question_view(bank_question)
    bank = get_question(question_id)
    return _bank_question_view(bank) if bank else None


def get_sme_pending_queue():
    """Every question waiting for an SME decision: resume-cycle questions first, then the question bank."""
    cycle_rows = [
        {**row, "source": "cycle", "source_label": "Resume assessment"}
        for row in workflow_db.get_pending_review_queue()
    ]
    qbank_rows = [
        {"question_id": q["question_id"], "source": "qbank", "source_label": "Question bank",
         "employee_name": "New for the bank", "matched_role_name": q["role_name"], "skill": q["skill"],
         "question_text": q["question_text"], "ai_confidence": q["ai_confidence"]}
        for q in workflow_db.list_bank(status="pending")
    ]
    bank_rows = [
        {"question_id": v["question_id"], "source": "bank", "source_label": v["source_label"],
         "employee_name": v["employee_name"], "matched_role_name": v["role_name"], "skill": v["skill"],
         "question_text": v["question_text"], "ai_confidence": v["ai_confidence"]}
        for v in _bank_questions("pending")
    ]
    return cycle_rows + qbank_rows + bank_rows


def get_sme_approved_questions():
    conn = workflow_db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM generated_questions WHERE sme_status = 'approved' ORDER BY reviewed_at DESC"
        ).fetchall()
    finally:
        conn.close()
    views = ([_cycle_question_view(dict(r)) for r in rows if "bank_question_id" not in (r["item_json"] or "")]
             + [_qbank_question_view(q) for q in workflow_db.list_bank(status="approved") if q["source"] == "generated"]
             + _bank_questions("approved"))
    reviewer_names = {}
    for view in views:
        rid = view["reviewed_by"]
        if rid and view["source"] == "cycle":
            if rid not in reviewer_names:
                reviewer = workflow_db.get_user(rid)
                reviewer_names[rid] = reviewer["full_name"] if reviewer else rid
            view["reviewed_by"] = reviewer_names[rid]
    return views


def get_sme_overview():
    """SME dashboard counts and pending list across both question sources."""
    conn = workflow_db.get_conn()
    try:
        # Deployed bank copies are not new questions; count each question once.
        counts = dict(conn.execute(
            "SELECT sme_status, COUNT(*) FROM generated_questions "
            "WHERE json_extract(item_json, '$.bank_question_id') IS NULL GROUP BY sme_status").fetchall())
    finally:
        conn.close()
    bank = _bank_questions() + [{"sme_status": q["sme_status"]} for q in workflow_db.list_bank() if q["source"] == "generated"]
    for status in ("pending", "approved", "rejected"):
        counts[status] = counts.get(status, 0) + sum(1 for v in bank if v["sme_status"] == status)
    return {
        "metrics": {"generated": sum(counts.values()), "pending": counts["pending"],
                    "approved": counts["approved"], "rejected": counts["rejected"]},
        "pending": get_sme_pending_queue(),
    }


def decide_sme_question(question_id: str, decision: str, reviewer_id: str):
    """Approves/rejects one question from either source. For a resume-cycle
    question the owning cycle advances to 'ready_for_assessment' once nothing
    is left pending and at least one item is approved. Returns falsy if the
    question doesn't exist or was already decided.
    """
    if workflow_db.get_question(question_id):
        return workflow_db.decide_question(question_id, decision, reviewer_id)
    if workflow_db.get_bank_question(question_id):
        return workflow_db.decide_bank_question(question_id, decision, reviewer_id)
    reviewer = workflow_db.get_user(reviewer_id)
    reviewer_name = reviewer["full_name"] if reviewer else reviewer_id
    if decision == "approved":
        return approve_question(question_id, approved_by=reviewer_name)
    return reject_question(question_id, f"Rejected by {reviewer_name}.")


def get_sme_scored_cycles():
    """Cycles with an MCQ score -- SME gets score visibility alongside the manager."""
    return workflow_db.get_scored_cycles()


def get_manager_cycle_queue(manager_id: str):
    return workflow_db.get_manager_queue(manager_id)


def get_manager_cycle_detail(cycle_id: str):
    """One cycle with its employee's manager_id attached, for the manager
    validation detail view's resource-level authorization check.
    """
    cycle = workflow_db.get_cycle(cycle_id)
    if not cycle:
        return None
    employee = workflow_db.get_user(cycle["user_id"])
    cycle["manager_id"] = employee.get("manager_id") if employee else None
    cycle["employee_name"] = employee.get("full_name") if employee else cycle["user_id"]
    return cycle


def get_cycle_governance_notes(cycle_id: str):
    """Human-oversight trail for one cycle: question counts/SME reviewers plus
    the cycle's own stage flags -- reused by the manager validation detail
    page's 'Governance notes' panel. Built from data already recorded by the
    SME-approval-gate and manager-decision workflow, not a separate log.
    """
    cycle = workflow_db.get_cycle(cycle_id)
    if not cycle:
        return None
    questions = workflow_db.get_questions_for_cycle(cycle_id)
    approved = [q for q in questions if q["sme_status"] == "approved"]
    rejected = [q for q in questions if q["sme_status"] == "rejected"]
    pending = [q for q in questions if q["sme_status"] == "pending"]
    reviewer_ids = sorted({q["reviewed_by"] for q in questions if q.get("reviewed_by")})
    reviewer_names = []
    for uid in reviewer_ids:
        reviewer = workflow_db.get_user(uid)
        reviewer_names.append(reviewer["full_name"] if reviewer else uid)
    return {
        "questions_total": len(questions),
        "questions_approved": len(approved),
        "questions_rejected": len(rejected),
        "questions_pending": len(pending),
        "reviewer_names": reviewer_names,
        "created_at": cycle["created_at"],
        "updated_at": cycle["updated_at"],
        "match_status": cycle["match_status"],
        "has_mcq_score": bool(cycle.get("mcq_score")),
        "has_evidence_report": bool(cycle.get("evidence_report")),
        "status": cycle["status"],
        "manager_decision": cycle.get("manager_decision"),
        "manager_decision_notes": cycle.get("manager_decision_notes"),
    }


def decide_manager_cycle(cycle_id: str, decision: str, notes: str, manager_id: str, max_steps=None):
    """Manager approval unlocks the employee's training page: this generates
    the Agent 5 dev plan (once) before flipping the cycle to manager_approved.
    """
    cycle = workflow_db.get_cycle(cycle_id)
    if not cycle or cycle["status"] != "pending_manager_approval":
        return False
    dev_plan = generate_employee_dev_plan(cycle_id, max_steps=max_steps) if decision == "approved" else None
    return workflow_db.decide_manager_review(cycle_id, decision, notes, manager_id, dev_plan=dev_plan)


def get_all_users():
    return workflow_db.list_users()


def get_manager_options():
    return workflow_db.list_managers()


def create_portal_user(**kwargs):
    return workflow_db.create_user(**kwargs)


def update_portal_user(user_id: str, role: str, manager_id: str | None = None):
    return workflow_db.update_user_role(user_id, role, manager_id)


def get_calibration_reviews(cycle_id: str | None = None):
    return workflow_db.list_calibration_reviews(cycle_id)


# Employee "Assessments" tab: every resume assessment cycle, every assessment
# the scheduler booked, and the TNI baseline -- grouped Pending / In progress /
# Completed / Scheduled. Scores stay hidden from the employee, as elsewhere.
_CYCLE_STAGE_VIEW = {
    "resume_uploaded": ("Pending", "Resume uploaded", None),
    "pending_sme_setup": ("Pending", "Resume received · an SME is setting up your assessment", None),
    "pending_sme_approval": ("Pending", "Questions are with an SME for approval", None),
    "ready_for_assessment": ("Pending", "Ready to take", ("Start assessment", "/employee/assessment/{id}")),
    "mcq_submitted": ("In progress", "MCQ done · workplace simulation next", ("Continue simulation", "/employee/evidence/{id}")),
    "case_study_submitted": ("In progress", "Submitted · awaiting manager review", None),
    "pending_manager_approval": ("In progress", "Submitted · awaiting manager review", None),
    "manager_approved": ("Completed", "Completed · approved, levels certified", ("View training plan", "/employee/learning")),
    "manager_rejected": ("Completed", "Completed · not approved", ("View training plan", "/employee/learning")),
}
_SOURCE_KIND = {"auto_baseline": "Baseline (auto-assigned)", "auto_retake": "Retake", "auto_reassessment": "Re-assessment",
                "promotion": "Promotion readiness", "sme_assigned": "Assigned by SME"}
_DECISION_STAGE = {"rejected": "Completed · rejected, skill-gap training assigned",
                   "sent_back": "Completed · sent back to retake"}


def assessment_stage(cycle: dict) -> str:
    """Where an assessment that is not yet with the manager stands, for the manager's view."""
    status = cycle["status"]
    if status == "mcq_submitted":
        sim = get_simulation(cycle) if cycle.get("case_study") else None
        if not sim or not any(s["turns"] for s in sim["scenarios"]):
            return "MCQ done · simulation not started"
        if sim["all_done"]:
            return "Simulation finished · not submitted yet"
        return f"Simulation in progress · scenario {sim['current']['index']} of {len(sim['scenarios'])}"
    return {"resume_uploaded": "Resume received", "pending_sme_setup": "Waiting for an SME to set up the test",
            "pending_sme_approval": "Questions with an SME for approval",
            "ready_for_assessment": "Assigned · MCQ not taken yet",
            "case_study_submitted": "Simulation submitted"}.get(status, status.replace("_", " "))


def manager_assessment_pipeline(manager_id: str) -> dict:
    pipeline = workflow_db.team_assessment_pipeline(manager_id)
    for cycle in pipeline["in_progress"]:
        cycle["stage"] = assessment_stage(cycle)
    return pipeline


def get_employee_assessment_history(user_id: str, today: date):
    rows = []
    cycles = workflow_db.list_cycles_for_user(user_id)
    for index, cycle in enumerate(cycles):
        group, stage, action = _CYCLE_STAGE_VIEW.get(cycle["status"], ("In progress", cycle["status"].replace("_", " "), None))
        if cycle["status"] == "manager_rejected":
            stage = _DECISION_STAGE.get(cycle.get("manager_decision"), stage)
        if index > 0:
            action = None  # only the latest cycle can be continued
        questions = len(workflow_db.get_questions_for_cycle(cycle["cycle_id"], sme_status="approved"))
        scenarios = len(((cycle.get("case_study") or {}).get("case_studies")) or [])
        rows.append({
            "id": cycle["cycle_id"], "name": f"Skill assessment · {cycle.get('matched_role_name') or 'matched role'}",
            "kind": f"{_SOURCE_KIND.get(cycle.get('match_status'), 'Resume-matched')}: MCQ + workplace simulation",
            "detail": " · ".join(x for x in (f"{questions} MCQ question(s)" if questions else "", f"{scenarios} simulation scenario(s)" if scenarios else "") if x),
            "group": group, "stage": stage, "date": cycle["updated_at"][:10],
            "note": cycle.get("manager_decision_notes") if cycle["status"] == "manager_rejected" else None,
            "action": (action[0], action[1].format(id=cycle["cycle_id"])) if action else None,
        })
    for event in schedule.events_for_user(user_id):
        if event["kind"] != "assessment":
            continue
        upcoming = event["date"] >= today.isoformat()
        group = "Completed" if event["status"] == "completed" else "Scheduled" if upcoming else "Pending"
        stage = ("Completed" if event["status"] == "completed" else
                 f"Scheduled for {event['date']}{' · ' + event['time'] if event['time'] else ''}" if upcoming else
                 f"Was scheduled for {event['date']} · not yet recorded as done")
        rows.append({"id": event["id"], "name": event["title"], "kind": "Scheduled assessment",
                     "detail": f"{event['duration']} min" if event["duration"] else "", "group": group, "stage": stage,
                     "date": event["date"], "note": None, "action": None})
    plan = tni.employee_tni(user_id)
    if plan:
        s = plan["summary"]
        pending = s["not_assessed"]
        rows.append({
            "id": "TNI", "name": f"TNI baseline · {plan['role_name']}", "kind": "Role skill baseline",
            "detail": f"{s['assessed']} of {s['total']} role skills assessed",
            "group": "Pending" if pending else "Completed",
            "stage": f"{pending} skill(s) still to be assessed" if pending else "All role skills assessed",
            "date": "", "note": None, "action": ("View skills & TNI", "/employee/skills"),
        })
    order = {"In progress": 0, "Pending": 1, "Scheduled": 2, "Completed": 3}
    return sorted(rows, key=lambda r: (order.get(r["group"], 9), r["date"] or "9999"))
