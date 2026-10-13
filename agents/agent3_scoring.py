"""
agent3_scoring.py

Sub-step 21 (Phase 5): Agent 3, the scoring agent.

Takes the MCQ items Agent 2 generated (agents/agent2_question.py's output)
plus the candidate's selected answer for each, and produces a weighted
assessment score. Weighting comes straight from the blueprint agent's own
output vector, carried through onto each item by Agent 2:
is_critical (critical skills count double) and target_proficiency_level
(higher-level skills carry more weight within this role's own scale).

SCOPE NOTE: this is pure arithmetic MCQ scoring, matching the HLD's own
line that "MCQ scoring bypasses [Agent 3] entirely -- pure arithmetic."
There is no free-text/descriptive answer scoring here (that's a separate,
unbuilt HLD capability, and would need an LLM call through
agents/llm_client.py rather than this arithmetic path).

GOVERNANCE NOTE (deliberate, confirmed with the user, not an oversight):
every item here is tagged sme_review_status = "Pending SME Review" by
Agent 2 -- no real SME approval gate exists yet in this project. This
agent scores ANY generated item regardless of sme_review_status, purely
to demo the scoring flow end to end. Flagged the same way every other
data/governance gap in this project has been flagged, rather than
silently assumed away.
"""


def score_item(item, selected_option):
    """
    Scores one answered MCQ item.

    weight = target_proficiency_level, doubled if the skill is_critical --
    so a wrong answer on a critical, high-level skill costs more than a
    wrong answer on a low-level, non-critical one.
    """
    is_critical = item.get("is_critical", False)
    target_level = item.get("target_proficiency_level", 1)
    weight = target_level * (2 if is_critical else 1)

    is_correct = selected_option == item["correct_option"]

    return {
        "question_id": item["question_id"],
        "skill": item["skill"],
        "target_proficiency_level": target_level,
        "is_critical": is_critical,
        "selected_option": selected_option,
        "correct_option": item["correct_option"],
        "is_correct": is_correct,
        "weight": weight,
        "weighted_score": weight if is_correct else 0.0,
    }


def score_assessment(items, answers):
    """
    Scores a full assessment.

    items: the list of generated items (agent2_question.generate_questions_for_blueprint()'s
        output["items"]).
    answers: dict of {question_id: selected_option ("A"|"B"|"C"|"D")}.

    Items with no answer are reported under "unanswered", not silently
    scored as wrong -- an unanswered question is a different signal than
    a wrong one and shouldn't be blended into the same weighted average.

    Returns:
        {
            "results": [...],            # one score_item() dict per answered item
            "unanswered": [...],         # question_ids with no submitted answer
            "total_weight": float,
            "earned_weight": float,
            "overall_score_pct": float | None,  # None if nothing was answered
            "by_skill": [...],           # per-skill rollup: correct/total/pct
        }
    """
    results = []
    unanswered = []

    for item in items:
        qid = item["question_id"]
        selected = answers.get(qid)
        if not selected:
            unanswered.append(qid)
            continue
        results.append(score_item(item, selected))

    total_weight = sum(r["weight"] for r in results)
    earned_weight = sum(r["weighted_score"] for r in results)
    overall_score_pct = round(100 * earned_weight / total_weight, 1) if total_weight else None

    by_skill = {}
    for r in results:
        entry = by_skill.setdefault(
            r["skill"],
            {
                "skill": r["skill"],
                "target_proficiency_level": r["target_proficiency_level"],
                "is_critical": r["is_critical"],
                "correct": 0,
                "total": 0,
            },
        )
        entry["total"] += 1
        if r["is_correct"]:
            entry["correct"] += 1

    by_skill_list = []
    for entry in by_skill.values():
        entry["pct"] = round(100 * entry["correct"] / entry["total"], 1) if entry["total"] else None
        by_skill_list.append(entry)

    return {
        "results": results,
        "unanswered": unanswered,
        "total_weight": total_weight,
        "earned_weight": earned_weight,
        "overall_score_pct": overall_score_pct,
        "by_skill": by_skill_list,
    }


def print_score_summary(items, answers):
    """
    Test helper: scores an assessment and prints a readable summary,
    matching the print-and-eyeball style used elsewhere in agents/.
    """
    result = score_assessment(items, answers)

    pct = result["overall_score_pct"]
    print(f"Overall score: {pct}%" if pct is not None else "Overall score: n/a (nothing answered)")
    print(f"Answered: {len(result['results'])} | Unanswered: {len(result['unanswered'])}\n")

    for r in result["results"]:
        marker = "OK" if r["is_correct"] else "!!"
        crit = "CRIT" if r["is_critical"] else "    "
        print(
            f"  [{marker}] [{crit}] {r['skill']} (L{r['target_proficiency_level']}, weight {r['weight']}): "
            f"selected {r['selected_option']}, correct {r['correct_option']}"
        )

    if result["unanswered"]:
        print(f"\n  Unanswered: {', '.join(result['unanswered'])}")

    print("\n  By skill:")
    for s in result["by_skill"]:
        print(f"    {s['skill']}: {s['correct']}/{s['total']} ({s['pct']}%)")
    print()


if __name__ == "__main__":
    import sys
    from pathlib import Path

    AGENTS_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(AGENTS_DIR))

    from agent1_blueprint import build_blueprint  # noqa: E402
    from agent2_question import generate_questions_for_blueprint  # noqa: E402

    sys.stdout.reconfigure(encoding="utf-8")

    blueprint = build_blueprint("O2C_associate_revenue_controller")
    question_result = generate_questions_for_blueprint(blueprint, max_items=3)
    items = question_result["items"]

    # Simulate a candidate answering: first item correct, second wrong,
    # third left unanswered -- exercises all three outcomes at once.
    answers = {}
    if len(items) >= 1:
        answers[items[0]["question_id"]] = items[0]["correct_option"]
    if len(items) >= 2:
        wrong_options = [o for o in ["A", "B", "C", "D"] if o != items[1]["correct_option"]]
        answers[items[1]["question_id"]] = wrong_options[0]

    print_score_summary(items, answers)
