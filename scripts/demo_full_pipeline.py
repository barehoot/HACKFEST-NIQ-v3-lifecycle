"""
demo_full_pipeline.py

Sub-step 19 (Phase 5): end-to-end demo -- uploaded resume file -> Agent 1
(match + blueprint) -> Agent 2 (generated + self-checked questions).

Prerequisites:
    1. scripts/build_sqlite.py already run  (builds db/talent360i.sqlite)
    2. scripts/build_chroma.py already run  (builds db/chroma_store/)
    3. GROQ_API_KEY set in the .env file at the project root
       (get a free key at https://console.groq.com/keys)

Usage:
    python scripts/demo_full_pipeline.py <path-to-resume-file>
    python scripts/demo_full_pipeline.py   # uses the bundled sample resume
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
AGENTS_DIR = PROJECT_ROOT / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from resume_parser import extract_resume_text  # noqa: E402
from agents1 import run_agent1  # noqa: E402
from agent2_question import generate_questions_for_blueprint  # noqa: E402

DEFAULT_SAMPLE = PROJECT_ROOT / "data" / "sample_resumes" / "sample_resume.docx"


def run_full_pipeline(file_path, max_items=5):
    print(f"### Step 1: extract resume text -- {file_path}")
    resume_text = extract_resume_text(file_path)
    print(f"Extracted {len(resume_text)} characters.\n")

    print("### Step 2: Agent 1 -- match resume to role + build blueprint")
    agent1_result = run_agent1(resume_text)
    print(f"Status: {agent1_result['status']}")

    if agent1_result["status"] != "auto_matched":
        print("Cannot proceed to Agent 2 -- Agent 1 did not return a single confirmed blueprint.")
        if agent1_result["status"] == "needs_human_confirmation":
            print("Two roles need human confirmation:")
            for bp in agent1_result["candidate_blueprints"]:
                if bp["success"]:
                    print(f"  - {bp['role_name']} ({bp['role_id']})")
        return None

    blueprint = agent1_result["blueprint"]
    print(f"Matched role: {blueprint['role_name']} ({blueprint['role_id']}), {blueprint['skill_count']} skills\n")

    print(f"### Step 3: Agent 2 -- generate + self-check questions (max {max_items})")
    try:
        question_result = generate_questions_for_blueprint(blueprint, max_items=max_items)
    except RuntimeError as exc:
        print(f"Agent 2 could not run: {exc}")
        return {"agent1": agent1_result, "agent2": None}
    print(
        f"Generated {len(question_result['items'])} items -- "
        f"{question_result['counts']['approved']} approved, "
        f"{question_result['counts']['failed']} failed self-check, "
        f"{question_result['counts']['skipped']} skills skipped.\n"
    )

    for item in question_result["items"]:
        print(f"  [{item['sme_review_status']}] {item['skill']}: {item['question_text']}")

    return {"agent1": agent1_result, "agent2": question_result}


if __name__ == "__main__":
    # Windows terminals default stdout to cp1252, which chokes on
    # Unicode characters LLM output commonly contains -- force UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")

    resume_path = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_SAMPLE)
    run_full_pipeline(resume_path)
