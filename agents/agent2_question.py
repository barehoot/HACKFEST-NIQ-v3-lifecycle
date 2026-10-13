"""
agent2_question.py

Sub-step 18 (Phase 5): Agent 2, the Question agent.

Input is the blueprint agent's output vector -- agent1_blueprint.
build_blueprint()'s "skills" list -- exactly what the HLD specifies: A2
generates items grounded on a competency's indicator + SOP clause,
self-checks its own output against 5 criteria, regenerates once on
failure, and stops at an SME approval gate rather than deciding anything
itself (the gate is a human action -- not built here, same as the HLD's
"pauses for an SME approval gate" description).

NOTE ON GROUNDING DATA: the HLD's grounding model references SOP clause
text (sop_links) that doesn't exist in the current SQLite schema -- only
skill_master L-indicators, role_skill_map.behavior_indicator, and
proficiency_levels.assessment_design_interpretation exist (see
scripts/build_sqlite.py). Generation here grounds on behavior_indicator +
assessment_design_interpretation, and the self-check's "sop_alignment"
criterion checks against that same indicator text as a documented proxy,
not literal SOP clause text -- flagged here rather than silently assumed
equivalent.

Only skills with assess_method == "question" are handled here -- the
remaining behavioural competencies route to Agent 4 (Evidence agent) per
the HLD's routing rules, out of scope for this module.

Generated items are shaped to match the existing Assessment_QBank sheet's
columns (see scripts/build_sqlite.py's derive_critical_flags, which reads
that same sheet) so AI-generated items slot into the same schema a human
SME already reviews, instead of inventing a parallel format.
"""

import sys
import uuid
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENTS_DIR))

from llm_client import call_llm_json  # noqa: E402

SELF_CHECK_CRITERIA = ["relevance", "difficulty_fit", "bloom_level", "bias", "sop_alignment"]

GENERATE_SYSTEM_PROMPT = """You are an expert assessment item writer for a corporate finance-operations \
upskilling platform. You write realistic, scenario-based multiple-choice questions that test whether \
someone actually has a competency at a stated proficiency level -- not just whether they've memorized \
a definition. Always respond with ONLY a single JSON object, no prose, no markdown code fences."""

SELF_CHECK_SYSTEM_PROMPT = """You are a strict quality reviewer for assessment items on a corporate \
upskilling platform. You check generated multiple-choice questions against five criteria before they \
are allowed to reach a human subject-matter expert for approval. Always respond with ONLY a single \
JSON object, no prose, no markdown code fences."""


def _build_generation_prompt(skill, role_name, role_grade):
    return f"""Write ONE scenario-based multiple-choice question for this competency.

Role: {role_name} (grade {role_grade})
Skill: {skill['skill']}
Target proficiency level: {skill['target_proficiency_level']} ({skill['target_level_name']})
Behavior indicator at this level: {skill['behavior_indicator']}
Assessment design guidance: {skill['assessment_design_interpretation']}

Requirements:
- Present a realistic workplace scenario, not a textbook definition question.
- Exactly 4 options (A-D), exactly one correct.
- The question should distinguish someone who genuinely performs at the target level
  from someone who does not, per the behavior indicator above.

Respond with ONLY this JSON object:
{{
  "question_text": "...",
  "option_a": "...",
  "option_b": "...",
  "option_c": "...",
  "option_d": "...",
  "correct_option": "A" | "B" | "C" | "D",
  "difficulty": "Foundational" | "Role Ready" | "Advanced"
}}"""


def _build_self_check_prompt(item, skill):
    return f"""Review this generated assessment item against 5 criteria.

Skill being assessed: {skill['skill']}
Target proficiency level: {skill['target_proficiency_level']} ({skill['target_level_name']})
Behavior indicator (grounding source): {skill['behavior_indicator']}

Item to review:
Question: {item['question_text']}
A: {item['option_a']}
B: {item['option_b']}
C: {item['option_c']}
D: {item['option_d']}
Correct answer: {item['correct_option']}
Stated difficulty: {item['difficulty']}

Score each criterion 1-5 and give a verdict of "pass" (score >= 3) or "fail" (score < 3):
1. relevance -- does the question actually test this skill?
2. difficulty_fit -- does it match the stated target proficiency level, not too easy/hard?
3. bloom_level -- does it require applying/analyzing judgment, not just recalling a fact?
4. bias -- is it free of unfair, exclusionary, or culturally-loaded framing?
5. sop_alignment -- is the question and correct answer consistent with the behavior indicator above?

Respond with ONLY this JSON object:
{{
  "relevance": {{"score": 1-5, "verdict": "pass"|"fail", "note": "..."}},
  "difficulty_fit": {{"score": 1-5, "verdict": "pass"|"fail", "note": "..."}},
  "bloom_level": {{"score": 1-5, "verdict": "pass"|"fail", "note": "..."}},
  "bias": {{"score": 1-5, "verdict": "pass"|"fail", "note": "..."}},
  "sop_alignment": {{"score": 1-5, "verdict": "pass"|"fail", "note": "..."}}
}}"""


VALID_DIFFICULTIES = {"Foundational", "Role Ready", "Advanced"}


def _validate_item(item):
    """
    Model output is untrusted (guideline K): confirm the generated item has
    the exact shape downstream code and the SME reviewer rely on. Raises
    ValueError -- callers already treat that as "generation failed".
    """
    for key in ("question_text", "option_a", "option_b", "option_c", "option_d"):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError(f"Generated item is missing '{key}'.")
    if item.get("correct_option") not in {"A", "B", "C", "D"}:
        raise ValueError("Generated item has an invalid correct_option.")
    if item.get("difficulty") not in VALID_DIFFICULTIES:
        item["difficulty"] = "Role Ready"
    return item


def _self_check_passed(self_check):
    try:
        return all(self_check[c]["verdict"] == "pass" for c in SELF_CHECK_CRITERIA)
    except (KeyError, TypeError):
        return False  # malformed self-check counts as a failed check, never a pass


def _average_score(self_check):
    try:
        scores = [float(self_check[c]["score"]) for c in SELF_CHECK_CRITERIA]
    except (KeyError, TypeError, ValueError):
        return 0.0
    return round(min(1.0, max(0.0, sum(scores) / len(scores) / 5)), 2)  # normalized 0-1, matches QBank's ai_confidence


def _generate_valid_item(skill, role_name, role_grade):
    return _validate_item(
        call_llm_json(GENERATE_SYSTEM_PROMPT, _build_generation_prompt(skill, role_name, role_grade), agent="A2")
    )


def generate_and_check_item(skill, role_id, role_name, role_grade):
    """
    Generates one question item for a skill, self-checks it, and
    regenerates once if the check fails -- A2's "self-check ... regenerate
    once on failure" loop, per the HLD.

    Returns a dict shaped to match Assessment_QBank's columns, plus a
    self_check block and a status of "approved_for_sme_review" or
    "failed_self_check" (the latter is kept, not dropped, so a human can
    see why -- the same "no silent gaps" pattern used elsewhere in agents/).
    """
    item = _generate_valid_item(skill, role_name, role_grade)
    self_check = call_llm_json(SELF_CHECK_SYSTEM_PROMPT, _build_self_check_prompt(item, skill), agent="A2")
    attempts = 1

    if not _self_check_passed(self_check):
        item = _generate_valid_item(skill, role_name, role_grade)
        self_check = call_llm_json(SELF_CHECK_SYSTEM_PROMPT, _build_self_check_prompt(item, skill), agent="A2")
        attempts = 2

    status = "approved_for_sme_review" if _self_check_passed(self_check) else "failed_self_check"
    is_synthetic_grounding = skill.get("is_synthetic_grounding", False)

    context = (
        f"AI-generated by Agent 2, grounded on behavior_indicator for '{skill['skill']}' "
        f"at level {skill['target_proficiency_level']} ({skill['target_level_name']})."
    )
    if is_synthetic_grounding:
        context += " NOTE: grounded on synthetic placeholder data, not yet SME-validated."

    return {
        "question_id": f"GEN-{role_id}-{uuid.uuid4().hex[:8]}",
        "role_id": role_id,
        "role_name": role_name,
        "skill": skill["skill"],
        "target_proficiency_level": skill["target_proficiency_level"],
        "is_critical": skill["is_critical"],
        "is_synthetic_grounding": is_synthetic_grounding,
        "question_type": "Multiple Choice - Scenario",
        "question_text": item["question_text"],
        "option_a": item["option_a"],
        "option_b": item["option_b"],
        "option_c": item["option_c"],
        "option_d": item["option_d"],
        "correct_option": item["correct_option"],
        "difficulty": item["difficulty"],
        "ai_confidence": _average_score(self_check),
        "self_check": self_check,
        "generation_attempts": attempts,
        "status": status,
        "sme_review_status": "Pending SME Review" if status == "approved_for_sme_review" else "Needs Rewrite",
        "ai_generated": True,  # transparency: shown to SMEs and end users
        "generated_prompt_context": context,
    }


def generate_questions_for_blueprint(blueprint, max_items=None):
    """
    Takes a blueprint dict (agent1_blueprint.build_blueprint()'s output --
    the blueprint agent's output vector) and generates one question item
    per question-assessed skill.

    Skips:
        - skills with assess_method != "question" (behavioural -> Agent 4, not this agent)
        - skills with detail_status == "insufficient - needs SME input"
          (no behavior_indicator to ground generation on -- generation is
          skipped and reported, not silently attempted with blank grounding)

    max_items caps how many skills get a generated item (skills are
    already ordered by is_critical DESC in the blueprint, so a cap
    naturally prioritizes critical skills first). Defaults to the
    blueprint's own target_question_count if not given.

    Returns:
        {
            "role_id": ..., "role_name": ...,
            "items": [...],            # one per attempted skill
            "skills_skipped": [...],   # skill names skipped + why
            "counts": {"approved": n, "failed": n, "skipped": n},
        }
    """
    if not blueprint["success"]:
        raise ValueError(f"Cannot generate questions: {blueprint.get('message', 'blueprint build failed')}")

    if max_items is None:
        max_items = blueprint["target_question_count"] or len(blueprint["skills"])

    items = []
    skills_skipped = []

    for skill in blueprint["skills"]:
        if len(items) >= max_items:
            break

        if skill["assess_method"] != "question":
            skills_skipped.append(
                {
                    "skill": skill["skill"],
                    "reason": f"assess_method='{skill['assess_method']}' (routes to Agent 4, not A2)",
                }
            )
            continue

        if skill["detail_status"] == "insufficient - needs SME input":
            skills_skipped.append(
                {
                    "skill": skill["skill"],
                    "reason": "insufficient blueprint detail -- no behavior_indicator to ground generation on",
                }
            )
            continue

        item = generate_and_check_item(skill, blueprint["role_id"], blueprint["role_name"], blueprint["role_grade"])
        items.append(item)

    return {
        "role_id": blueprint["role_id"],
        "role_name": blueprint["role_name"],
        "items": items,
        "skills_skipped": skills_skipped,
        "counts": {
            "approved": sum(1 for i in items if i["status"] == "approved_for_sme_review"),
            "failed": sum(1 for i in items if i["status"] == "failed_self_check"),
            "skipped": len(skills_skipped),
        },
    }


def print_question_agent_summary(blueprint, max_items=None):
    """
    Test helper: runs generate_questions_for_blueprint() and prints a
    readable summary, matching the print-and-eyeball style used
    elsewhere in agents/.
    """
    result = generate_questions_for_blueprint(blueprint, max_items=max_items)

    print(f"=== Agent 2 -- {result['role_name']} ({result['role_id']}) ===")
    print(
        f"Items generated: {len(result['items'])} | Approved: {result['counts']['approved']} | "
        f"Failed self-check: {result['counts']['failed']} | Skipped: {result['counts']['skipped']}\n"
    )

    for item in result["items"]:
        marker = "OK" if item["status"] == "approved_for_sme_review" else "!!"
        print(
            f"  [{marker}] {item['skill']} (L{item['target_proficiency_level']}, {item['difficulty']}, "
            f"confidence {item['ai_confidence']}, {item['generation_attempts']} attempt(s))"
        )
        print(f"      Q: {item['question_text']}")
        print(f"      A: {item['option_a']}")
        print(f"      B: {item['option_b']}")
        print(f"      C: {item['option_c']}")
        print(f"      D: {item['option_d']}")
        print(f"      Correct: {item['correct_option']} | sme_review_status: {item['sme_review_status']}")
        print()

    if result["skills_skipped"]:
        print("  Skipped skills:")
        for s in result["skills_skipped"]:
            print(f"    - {s['skill']}: {s['reason']}")
        print()


if __name__ == "__main__":
    # Windows terminals default stdout to cp1252, which chokes on
    # Unicode characters (en-dashes, smart quotes, etc.) that LLM output
    # commonly contains -- force UTF-8 so generated question text always
    # prints cleanly.
    sys.stdout.reconfigure(encoding="utf-8")

    from agent1_blueprint import build_blueprint  # noqa: E402

    blueprint = build_blueprint("O2C_associate_revenue_controller")
    # Cap at 3 items for the smoke test -- keeps the demo run fast and
    # cheap; drop max_items (or raise it) for a full blueprint-sized run.
    print_question_agent_summary(blueprint, max_items=3)
