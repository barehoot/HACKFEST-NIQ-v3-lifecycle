"""
seed_question_bank.py

Loads the hackfest dataset's SME-reviewed questions (Assessment_QBank +
SME_Review_Workflow) into the portal's reusable question bank, so every role
has approved questions to build assessments from -- including the automatic
baseline assessments -- before any new ones are generated.

  - "Approved" rows go in as approved (the dataset's SME already signed them off).
  - "Pending SME Review" rows go in as pending, into the SME review queue.
  - "Needs Rewrite" rows and rows without four options and a key are skipped.

Idempotent (INSERT OR IGNORE on DS-<question_id>), so start.py runs it on
every start and an SME's later decision on a row is never overwritten.
"""
import sys
from pathlib import Path

import openpyxl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_ROOT / "data" / "Talent360i_Auto_Assessment_Hackfest_Dataset_v2_Personal.xlsx"
sys.path.insert(0, str(PROJECT_ROOT / "Talent360i_phase11C_fixed_remote_hybrid"))

from services import db  # noqa: E402  (the portal's only writer to these tables)

STATUS = {"Approved": "approved", "Pending SME Review": "pending"}
OPTIONS = ("option_a", "option_b", "option_c", "option_d")


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_items(path=DATASET_PATH):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb["Assessment_QBank"].iter_rows(values_only=True))
    header = rows[2]
    items = []
    for raw in rows[3:]:
        row = dict(zip(header, raw))
        status = STATUS.get(row.get("sme_review_status"))
        key = str(row.get("correct_option") or "").strip().upper()
        if not status or not row.get("question_id") or not row.get("question_text"):
            continue
        if not all(row.get(o) for o in OPTIONS) or key not in {"A", "B", "C", "D"}:
            continue
        items.append({
            "question_id": f"DS-{row['question_id']}",
            "dataset_question_id": row["question_id"],
            "role_id": row["role_id"], "role_name": row["role_name"],
            "skill": row["skill"], "target_proficiency_level": int(row["target_proficiency_level"] or 3),
            "question_text": str(row["question_text"]).strip(),
            **{o: str(row[o]).strip() for o in OPTIONS},
            "correct_option": key,
            "difficulty": row.get("difficulty"),
            "is_critical": str(row.get("critical_flag") or "").lower() == "yes",
            "ai_confidence": _as_float(row.get("ai_confidence")),
            "question_source": row.get("question_source"),
            "source_question_reference": row.get("source_question_reference"),
            "sme_status": status,
            "sme_review": "Dataset SME review (SME_Review_Workflow)",
        })
    return items


def main():
    if not DATASET_PATH.exists():
        print(f"seed_question_bank: {DATASET_PATH.name} not found -- skipped.")
        return
    items = load_items()
    added = db.import_bank_questions(items)
    approved = sum(1 for i in items if i["sme_status"] == "approved")
    print(f"seed_question_bank: {len(items)} usable dataset questions ({approved} approved), {added} newly added to the bank.")


if __name__ == "__main__":
    main()
