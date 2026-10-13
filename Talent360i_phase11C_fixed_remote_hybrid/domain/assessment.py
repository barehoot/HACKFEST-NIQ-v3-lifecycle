"""Pure assessment scoring functions. No LLM calls belong here."""
from collections import defaultdict
from .level_engine import calculate_gap, gap_label, level_from_score


def parse_answers(raw: str | None) -> dict[int, str]:
    import json
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        try:
            number = int(str(key).replace("question_", ""))
        except ValueError:
            continue
        if isinstance(value, str) and value.strip():
            result[number] = value.strip()[:5000]
    return result


def score_questions(question_set: list[dict], answers: dict[int, str]) -> dict:
    """Score objective items deterministically. Descriptive items are handled by A3.

    MCQ and scenario items are arithmetic/deterministic. Descriptive items are
    represented as pending until the A3 proposal is supplied by the service layer.
    """
    from collections import defaultdict
    from .a3_assessment import scenario_score, score_descriptive_answer

    by_skill = defaultdict(lambda: {"correct": 0, "total": 0, "objective_total": 0, "objective_correct": 0, "descriptive": 0})
    item_results = []
    objective_points = 0
    objective_possible = 0
    descriptive_results = []

    for q in question_set:
        qtype = (q.get("question_type") or "mcq").lower()
        answer = answers.get(q["number"])
        bucket = by_skill[q["skill"]]
        bucket["total"] += 1
        if qtype == "descriptive":
            bucket["descriptive"] += 1
            proposal = score_descriptive_answer(q, answer or "")
            descriptive_results.append(proposal)
            item_results.append({"number": q["number"], "skill": q["skill"], "type": qtype, "status": proposal["status"], "score_percent": proposal["score_percent"]})
            continue
        correct = False
        if qtype == "scenario":
            scenario = scenario_score(q, answer)
            correct = scenario["is_correct"]
            item_score = scenario["score_percent"]
        else:
            correct = bool(answer and answer == q.get("correct"))
            item_score = 100 if correct else 0
        bucket["objective_total"] += 1
        bucket["objective_correct"] += int(correct)
        bucket["correct"] += int(correct)
        objective_points += item_score
        objective_possible += 100
        item_results.append({"number": q["number"], "skill": q["skill"], "type": qtype, "status": "Scored", "score_percent": item_score})

    descriptive_points = sum(item["score_percent"] for item in descriptive_results)
    descriptive_possible = len(descriptive_results) * 100
    total_possible = objective_possible + descriptive_possible
    total_points = objective_points + descriptive_points
    score = round(total_points / total_possible * 100) if total_possible else 0

    performance = []
    for skill, bucket in by_skill.items():
        descriptive_for_skill = [x for x in descriptive_results if x["skill"] == skill]
        points = bucket["objective_correct"] * 100 + sum(x["score_percent"] for x in descriptive_for_skill)
        possible = bucket["objective_total"] * 100 + len(descriptive_for_skill) * 100
        pct = round(points / possible * 100) if possible else 0
        performance.append({
            "skill": skill, "score": pct, "correct": bucket["correct"],
            "total": bucket["total"], "objective_total": bucket["objective_total"],
            "descriptive": bucket["descriptive"],
        })

    return {
        "score": score,
        "correct": sum(x["correct"] for x in by_skill.values()),
        "total": len(question_set),
        "performance": performance,
        "item_results": item_results,
        "descriptive_results": descriptive_results,
        "objective_points": objective_points,
        "descriptive_points": descriptive_points,
        "status": "Pending SME Review" if any(x["borderline"] for x in descriptive_results) else "Completed",
    }


def build_proficiency(performance: list[dict], skill_rows: list[dict], framework="FinOps") -> list[dict]:
    targets = {row["name"]: row["target_level"] for row in skill_rows}
    current = {row["name"]: row["current_level"] for row in skill_rows}
    output = []
    for item in performance:
        skill = item["skill"]
        calculated = level_from_score(framework, item["score"])
        target = targets.get(skill, calculated)
        prior = current.get(skill, calculated)
        gap = calculate_gap(framework, calculated, target)
        output.append({"skill": skill, "current": prior, "calculated": calculated, "target": target, "gap": gap, "gap_label": gap_label(gap)})
    return output
