"""Phase 3 question-authoring domain helpers.

This is a deterministic development stand-in for A2. It deliberately does not
call an LLM. The production seam will pass the same request/response schemas
to the company CIS client.
"""
from copy import deepcopy

QUESTION_TEMPLATES = {
    "AP Controls & Compliance": [
        {
            "text": "Which control evidence best demonstrates that a high-value invoice was approved by an authorized approver?",
            "options": [
                "Recorded workflow approval linked to the invoice",
                "An unsigned invoice copy",
                "A supplier marketing email",
                "A verbal confirmation from the buyer",
            ], "correct": "A",
            "rationale": "A recorded workflow approval provides traceable evidence that the required authorization occurred.",
            "sop": "AP-CTRL-04 · High-value invoice approval and audit trail",
        },
        {
            "text": "A duplicate-invoice control flags a supplier invoice with a matching invoice reference. What should happen next?",
            "options": [
                "Release it immediately",
                "Investigate the match before payment",
                "Delete the supplier record",
                "Close the accounting period",
            ], "correct": "B",
            "rationale": "A duplicate flag is an exception signal and should be investigated before payment is released.",
            "sop": "AP-CTRL-07 · Duplicate invoice exception handling",
        },
    ],
    "Invoice Processing & 3-Way Match": [
        {
            "text": "A price variance exceeds the configured tolerance during invoice matching. What is the appropriate next step?",
            "options": [
                "Pay the invoice regardless",
                "Route the exception for review before payment",
                "Change the purchase order without approval",
                "Delete the invoice",
            ], "correct": "B",
            "rationale": "A tolerance breach is an exception that requires review rather than bypassing the control.",
            "sop": "P2P-MATCH-03 · Tolerance exception workflow",
        },
        {
            "text": "Which three records form the standard three-way match before an invoice is released?",
            "options": [
                "Purchase order, goods receipt, and supplier invoice",
                "Bank statement, invoice, and credit note",
                "Payment file, requisition, and journal entry",
                "Supplier master, bank statement, and payment run",
            ], "correct": "A",
            "rationale": "The three-way match compares the purchase order, receipt of goods/services, and supplier invoice.",
            "sop": "P2P-MATCH-01 · Three-way match procedure",
        },
    ],
    "Bank Reconciliations (SOX/IPE)": [
        {
            "text": "An aged unreconciled bank item remains open after the normal review window. What should the preparer do?",
            "options": [
                "Write it off immediately",
                "Document and investigate the variance using the reconciliation procedure",
                "Ignore it until year-end",
                "Reverse all bank transactions",
            ], "correct": "B",
            "rationale": "Aged reconciling items require documented investigation and resolution under the reconciliation procedure.",
            "sop": "R2R-REC-05 · Aged reconciling item escalation",
        },
        {
            "text": "Who should perform the independent review of a completed high-risk reconciliation?",
            "options": [
                "The preparer only",
                "An appropriate independent reviewer",
                "The supplier",
                "The newest team member",
            ], "correct": "B",
            "rationale": "Independent review provides the required separation of preparation and review for high-risk reconciliations.",
            "sop": "R2R-REC-02 · Reconciliation review control",
        },
    ],
}


def generate_questions(role: str, skill: str, level: str, count: int, question_type: str = "mcq"):
    templates = QUESTION_TEMPLATES.get(skill) or QUESTION_TEMPLATES["AP Controls & Compliance"]
    output = []
    for index in range(max(1, min(count, 10))):
        item = deepcopy(templates[index % len(templates)])
        item.update({
            "role": role,
            "skill": skill,
            "level": level,
            "question_type": question_type,
        })
        output.append(item)
    return output


def self_check(item: dict) -> dict:
    checks = {
        "skill_relevance": bool(item.get("skill") and item.get("text")),
        "difficulty": bool(item.get("level")),
        "bloom_level": True,
        "bias_sensitivity": True,
        "sop_alignment": bool(item.get("sop")),
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "confidence": round(sum(checks.values()) / len(checks), 2),
    }
