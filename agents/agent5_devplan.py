"""
agent5_devplan.py

Sub-step 23 (Phase 5): Agent 5, the development-plan agent.

Per the HLD (talent360i-hld-lld-v2 (2).html, "A5 - Development plan
agent"):
  gather   -> confirmed gaps for the person, ordered by severity and criticality
  retrieve -> candidate training from the catalogue + curated SOP links
  compose  -> sequenced plan; each step names the descriptor clause it closes
  verify   -> is every gap addressed? is the sequence feasible?
  revise   -> once, if verify fails
"The verification step is what makes this an agent rather than a lookup:
a plan that leaves a critical gap unaddressed is rejected and recomposed."

Implemented here as five functions matching those nodes one-to-one:
gather_gaps() / retrieve_courses() / compose_plan() / verify_plan(), with
the revise loop inlined in _compose_and_verify(). Only compose_plan()
costs a token -- everything else is deterministic Python, same "loop is
the only place tokens are spent" discipline as agent1_blueprint.py's
allocate/verify split.

Real source-data hole this agent runs straight into (see
scripts/build_sqlite.py's load_training_catalogue docstring):
Training_Catalogue stops at TRN-0031, but Training_Skill_Map and
Skill_Gaps_TNI both reference TRN-0032..TRN-0043 -- 12 course ids, every
one for an R2R skill, with no catalogue row at all. Net effect: for an
R2R person there is not one usable course in the catalogue. This agent
does not paper over that -- retrieve_courses() reports every such gap as
"blocked", and run_agent5()/run_agent5_from_score() surface it as
unaddressed_gaps rather than inventing a course to cite. Same "surface
the hole, don't hide it" pattern as scripts/build_sqlite.py's
load_role_skill_map() and agents/agent1_blueprint.py's detail_status.

Two entry points:
  run_agent5(user_id)                       -- off the synthetic
      Skill_Gaps_TNI cohort (the 36 SYN-Uxxx users already loaded into
      skill_gaps by scripts/build_sqlite.py's load_skill_gaps()).
  run_agent5_from_score(score_result, blueprint)
      -- off a live Agent 3 run, so a real resume-driven candidate (who
      has no row in skill_gaps at all) still gets a plan. Gaps derived
      this way use _provisional_level_from_score(), a deliberate
      placeholder for the real proficiency-level engine (unbuilt) -- see
      that function's docstring. Every gap it produces is tagged
      is_provisional = True, the same way role_skill_map.is_synthetic_grounding
      travels through agent1_blueprint.py, so nothing downstream mistakes
      a provisional level for an assessed one.

Never produces a decision about the person -- only a training plan
proposal. This agent has no human-review gate of its own in the HLD (A3
and A5 "follow the same shape with a single verification loop and no
human pause"): the plan is deterministic-verified before it's returned.

Dashboard extension (build_dev_dashboard()): wraps either entry point's
result with two additive extras for the UI's "Development Plan" page --
suggest_coursera_courses() (1 more LLM call, external leads for every
confirmed gap, explicitly unverified since this project has no live
Coursera catalog access) and estimate_remediation_duration() (0 tokens,
a rough weeks-to-close estimate scaled by Agent 4's own compatibility
band when one is available). Neither extra touches the governed plan's
steps or verification above -- see that function's docstring.
"""

import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote_plus

AGENTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AGENTS_DIR.parent
DB_PATH = PROJECT_ROOT / "db" / "talent360i.sqlite"

sys.path.insert(0, str(AGENTS_DIR))

from llm_client import call_llm_json  # noqa: E402

SEVERITY_RANK = {"High": 2, "Moderate": 1}

# Matches the one known half-filled catalogue row (TRN-0031: course_title
# == "Role-based assessment remediation for " -- the auto-generated
# template with the skill name never substituted in; see
# scripts/build_sqlite.py's load_training_catalogue docstring). A course
# whose title ends right at "for" with nothing filled in is not a real,
# presentable course -- retrieve_courses() below treats it the same as a
# catalogue row that doesn't exist at all, rather than citing it to a
# candidate.
_UNFILLED_TITLE_RE = re.compile(r"\bfor\s*$", re.IGNORECASE)


def _is_usable_course_title(course_title):
    return bool(course_title and course_title.strip() and not _UNFILLED_TITLE_RE.search(course_title.strip()))

COMPOSE_SYSTEM_PROMPT = """You are a learning-and-development planner for a corporate finance-operations \
upskilling platform. You are given a person's confirmed skill gaps and, for each gap, the ONLY courses \
that actually exist to close it. You sequence those courses into a development plan. You may cite ONLY a \
course_id that appears in that specific gap's own candidate list -- never invent a course, never substitute \
a course from a different gap's list. Always respond with ONLY a single JSON object, no prose, no markdown \
code fences."""


# ---------------------------------------------------------------------------
# Node 1: gather -- confirmed gaps for the person, ordered by severity and
# criticality. 0 tokens: straight SQL plus a deterministic sort.
# ---------------------------------------------------------------------------

def _row_to_gap(row):
    (
        gap_id, role_id, team_id, skill_id, skill, target_level, target_level_name,
        current_level, gap_level_formula, gap_severity, recommended_course_id,
        tni_recommendation, cross_functional_recommendation,
        behavior_indicator, capability, is_critical, is_synthetic_grounding,
    ) = row
    return {
        "gap_id": gap_id,
        "role_id": role_id,
        "team_id": team_id,
        "skill_id": skill_id,
        "skill": skill,
        "target_level": target_level,
        "target_level_name": target_level_name,
        "current_level": current_level,
        "gap_level_formula": gap_level_formula,
        "gap_severity": gap_severity,
        "recommended_course_id": recommended_course_id,
        "tni_recommendation": tni_recommendation,
        "cross_functional_recommendation": cross_functional_recommendation,
        "behavior_indicator": behavior_indicator,
        "capability": capability,
        "is_critical": bool(is_critical),
        "is_synthetic_grounding": bool(is_synthetic_grounding),
        "is_provisional": False,
    }


def _sort_key(gap):
    gap_width = 0
    try:
        gap_width = int(gap["gap_level_formula"])
    except (TypeError, ValueError):
        pass
    return (-SEVERITY_RANK.get(gap["gap_severity"], 0), not gap["is_critical"], -gap_width, gap["gap_id"])


def gather_gaps(user_id):
    """
    Returns every skill_gaps row for this user_id, split into three
    buckets and joined against role_skill_map (for behavior_indicator /
    capability / is_critical) and proficiency_levels (for target_level_name):

        confirmed_gaps  -- gap_severity in ("High", "Moderate"), sorted
                            severity desc, then critical first, then gap
                            width desc (the HLD's "ordered by severity and
                            criticality")
        at_target       -- gap_severity == "No gap"
        never_assessed  -- gap_severity IS NULL (current_level was never
                            recorded -- see scripts/build_sqlite.py's
                            load_skill_gaps docstring)

    Returns {"success": False, "message": ...} if user_id has no rows in
    skill_gaps at all -- not an exception, so run_agent5() can report a
    clean "gaps_unavailable" status instead of a crash.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT sg.gap_id, sg.role_id, sg.team_id, sg.skill_id, sg.skill,
               sg.target_level, pl.level_name, sg.current_level, sg.gap_level_formula,
               sg.gap_severity, sg.recommended_course_id, sg.tni_recommendation,
               sg.cross_functional_recommendation,
               rsm.behavior_indicator, COALESCE(rsm.capability, sg.capability),
               rsm.is_critical, rsm.is_synthetic_grounding
        FROM skill_gaps sg
        LEFT JOIN role_skill_map rsm ON rsm.role_id = sg.role_id AND rsm.skill_id = sg.skill_id
        LEFT JOIN proficiency_levels pl ON pl.level = sg.target_level
        WHERE sg.user_id = ?
        ORDER BY sg.gap_id;
        """,
        (user_id,),
    )
    rows = cur.fetchall()

    if not rows:
        conn.close()
        return {"success": False, "message": f"No skill-gap data found for user_id = '{user_id}'."}

    role_id = rows[0][1]
    cur.execute("SELECT role_name, role_grade FROM role_master WHERE role_id = ?;", (role_id,))
    role_row = cur.fetchone()
    conn.close()

    role_name, role_grade = role_row if role_row else (role_id, None)

    gaps = [_row_to_gap(row) for row in rows]
    confirmed_gaps = sorted(
        (g for g in gaps if g["gap_severity"] in SEVERITY_RANK), key=_sort_key
    )
    at_target = [g for g in gaps if g["gap_severity"] == "No gap"]
    never_assessed = [g for g in gaps if g["gap_severity"] is None]

    return {
        "success": True,
        "user_id": user_id,
        "role_id": role_id,
        "role_name": role_name,
        "role_grade": role_grade,
        "confirmed_gaps": confirmed_gaps,
        "at_target": at_target,
        "never_assessed": never_assessed,
    }


# ---------------------------------------------------------------------------
# Node 2: retrieve -- candidate training per gap, from the catalogue +
# the gap's own TNI recommendation. 0 tokens.
# ---------------------------------------------------------------------------

def retrieve_courses(confirmed_gaps):
    """
    For each confirmed gap, finds every course that actually exists in
    training_catalogue and is either (a) the gap's own
    recommended_course_id, or (b) mapped to the gap's skill_id via
    training_skill_map. Splits gaps into:

        addressable -- >=1 real candidate course found; gap dict gains a
                        "candidate_courses" list
        blocked     -- every course referenced for this skill is missing
                        from training_catalogue (the R2R hole), or no
                        course is mapped to this skill at all; gap dict
                        gains "referenced_course_ids" and "reason"

    Never fabricates a course -- a gap with zero resolvable candidates
    stays blocked and is reported as such, not silently dropped.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    addressable = []
    blocked = []

    for gap in confirmed_gaps:
        candidates = {}

        if gap["recommended_course_id"]:
            cur.execute(
                """
                SELECT course_id, course_title, delivery_type, level_group, risk_level
                FROM training_catalogue WHERE course_id = ?;
                """,
                (gap["recommended_course_id"],),
            )
            row = cur.fetchone()
            if row and _is_usable_course_title(row[1]):
                candidates[row[0]] = {
                    "course_id": row[0], "course_title": row[1], "delivery_type": row[2],
                    "level_group": row[3], "risk_level": row[4], "source": "tni_recommendation",
                }

        cur.execute(
            """
            SELECT tc.course_id, tc.course_title, tc.delivery_type, tc.level_group, tc.risk_level
            FROM training_skill_map tsm
            JOIN training_catalogue tc ON tc.course_id = tsm.course_id
            WHERE tsm.skill_id = ?;
            """,
            (gap["skill_id"],),
        )
        for row in cur.fetchall():
            if not _is_usable_course_title(row[1]):
                continue
            candidates.setdefault(row[0], {
                "course_id": row[0], "course_title": row[1], "delivery_type": row[2],
                "level_group": row[3], "risk_level": row[4], "source": "skill_map",
            })

        cur.execute("SELECT course_id FROM training_skill_map WHERE skill_id = ?;", (gap["skill_id"],))
        referenced_ids = {r[0] for r in cur.fetchall()}
        if gap["recommended_course_id"]:
            referenced_ids.add(gap["recommended_course_id"])

        if candidates:
            addressable.append({**gap, "candidate_courses": list(candidates.values())})
        else:
            blocked.append({
                **gap,
                "referenced_course_ids": sorted(referenced_ids),
                "reason": (
                    "every course referenced for this skill is missing from training_catalogue"
                    if referenced_ids else
                    "no course is mapped to this skill at all"
                ),
            })

    conn.close()
    return {"addressable": addressable, "blocked": blocked}


# ---------------------------------------------------------------------------
# Node 3: compose -- sequenced plan, 1 LLM call. Each step must name the
# descriptor clause it closes and cite a course from that gap's own list.
# ---------------------------------------------------------------------------

def _format_gap_for_prompt(gap):
    lines = [
        f'gap_id: "{gap["gap_id"]}"',
        f"skill: {gap['skill']} (current level {gap['current_level']}, "
        f"target level {gap['target_level']} - {gap['target_level_name']})",
        f"severity: {gap['gap_severity']} | critical: {gap['is_critical']}",
        f"descriptor to close: {gap['behavior_indicator'] or '(no behavior indicator on file)'}",
        "candidate courses (cite ONLY one of these course_ids for THIS gap):",
    ]
    for c in gap["candidate_courses"]:
        lines.append(f"  - {c['course_id']}: \"{c['course_title']}\" ({c['delivery_type']}, {c['level_group']})")
    return "\n".join(lines)


def _build_compose_prompt(role_name, role_grade, addressable, revision_feedback=None):
    gap_blocks = "\n\n".join(_format_gap_for_prompt(g) for g in addressable)

    feedback_block = ""
    if revision_feedback:
        feedback_block = (
            "\n\nYour previous plan failed verification for these reasons -- fix every one:\n"
            + "\n".join(f"- {f}" for f in revision_feedback)
        )

    return f"""Build a sequenced development plan for a {role_name} (grade {role_grade}).

Every gap below must get exactly one step, no more, no less. Sequence critical and higher-severity \
("High" before "Moderate") gaps first. For each step, cite the course_id from THAT gap's own candidate \
list only, and name the specific descriptor clause (the "descriptor to close" text, or the precise element \
within it) that the course addresses -- not a generic restatement of the course title.

{gap_blocks}{feedback_block}

Respond with ONLY this JSON object:
{{
  "plan_summary": "one or two sentences",
  "steps": [
    {{
      "step_number": 1,
      "gap_id": "...",
      "skill": "...",
      "course_id": "...",
      "descriptor_clause_addressed": "...",
      "rationale": "one sentence: why this course, why now"
    }}
  ]
}}"""


def compose_plan(role_name, role_grade, addressable, revision_feedback=None):
    result = call_llm_json(
        COMPOSE_SYSTEM_PROMPT,
        _build_compose_prompt(role_name, role_grade, addressable, revision_feedback),
        agent="A5",
    )
    return result.get("plan_summary", ""), result.get("steps", [])


# ---------------------------------------------------------------------------
# Node 4: verify -- six deterministic checks, 0 tokens. course_integrity
# is the governance check: it is what stops the model from inventing a
# course, per the HLD's "model output is untrusted" constraint.
# ---------------------------------------------------------------------------

def verify_plan(steps, addressable):
    gaps_by_id = {g["gap_id"]: g for g in addressable}
    checks = {}
    failures = []

    step_gap_ids = [s.get("gap_id") for s in steps]

    missing = [gid for gid in gaps_by_id if step_gap_ids.count(gid) == 0]
    duplicated = [gid for gid in gaps_by_id if step_gap_ids.count(gid) > 1]
    checks["coverage"] = not missing and not duplicated
    if missing:
        failures.append(f"coverage: gap(s) missing from the plan entirely: {', '.join(missing)}")
    if duplicated:
        failures.append(f"coverage: gap(s) given more than one step: {', '.join(duplicated)}")

    critical_ids = [gid for gid, g in gaps_by_id.items() if g["is_critical"]]
    critical_missing = [gid for gid in critical_ids if step_gap_ids.count(gid) == 0]
    checks["critical_coverage"] = not critical_missing
    if critical_missing:
        failures.append(f"critical_coverage: critical gap(s) left unaddressed: {', '.join(critical_missing)}")

    bad_courses = []
    for s in steps:
        gap = gaps_by_id.get(s.get("gap_id"))
        valid_ids = {c["course_id"] for c in gap["candidate_courses"]} if gap else set()
        if not gap or s.get("course_id") not in valid_ids:
            bad_courses.append(f"{s.get('gap_id')}->{s.get('course_id')}")
    checks["course_integrity"] = not bad_courses
    if bad_courses:
        failures.append(
            f"course_integrity: step(s) cite a course that was never offered for that gap: {', '.join(bad_courses)}"
        )

    numbers = sorted(s.get("step_number") for s in steps if isinstance(s.get("step_number"), int))
    expected = list(range(1, len(steps) + 1))
    checks["sequence_integrity"] = numbers == expected
    if numbers != expected:
        failures.append(f"sequence_integrity: step_number sequence is {numbers}, expected {expected}")

    ordered_steps = sorted(steps, key=lambda s: s.get("step_number") or 0)
    ranks = [SEVERITY_RANK.get(gaps_by_id[s["gap_id"]]["gap_severity"], 0)
             for s in ordered_steps if s.get("gap_id") in gaps_by_id]
    severity_ok = all(ranks[i] >= ranks[i + 1] for i in range(len(ranks) - 1))
    checks["severity_ordering"] = severity_ok
    if not severity_ok:
        failures.append("severity_ordering: a lower-severity gap is sequenced ahead of a higher-severity one")

    ungrounded = []
    for s in steps:
        gap = gaps_by_id.get(s.get("gap_id"))
        clause = (s.get("descriptor_clause_addressed") or "").strip()
        indicator = (gap["behavior_indicator"] or "") if gap else ""
        if not clause:
            ungrounded.append(s.get("gap_id"))
            continue
        clause_words = set(re.findall(r"[a-z]{4,}", clause.lower()))
        indicator_words = set(re.findall(r"[a-z]{4,}", indicator.lower()))
        if indicator_words and not (clause_words & indicator_words):
            ungrounded.append(s.get("gap_id"))
    checks["descriptor_grounding"] = not ungrounded
    if ungrounded:
        failures.append(
            f"descriptor_grounding: step(s) don't ground their clause in the skill's own "
            f"behavior indicator: {', '.join(ungrounded)}"
        )

    return {"passed": all(checks.values()), "checks": checks, "failures": failures}


def _compose_and_verify(role_name, role_grade, addressable):
    """Node 3 + node 4, with node 5 (revise, at most once) inlined."""
    if not addressable:
        return "", [], {"passed": True, "checks": {}, "failures": []}, 0

    plan_summary, steps = compose_plan(role_name, role_grade, addressable)
    llm_calls = 1
    verification = verify_plan(steps, addressable)

    if not verification["passed"]:
        plan_summary, steps = compose_plan(
            role_name, role_grade, addressable, revision_feedback=verification["failures"]
        )
        llm_calls += 1
        verification = verify_plan(steps, addressable)

    return plan_summary, steps, verification, llm_calls


def _build_plan_result(user_id, role_id, role_name, role_grade, confirmed_gaps, at_target, never_assessed):
    if not confirmed_gaps:
        status = "assessment_pending" if never_assessed else "no_gaps_at_target"
        return {
            "status": status,
            "user_id": user_id, "role_id": role_id, "role_name": role_name, "role_grade": role_grade,
            "plan_summary": None, "steps": [], "unaddressed_gaps": [], "all_confirmed_gaps": [],
            "never_assessed": never_assessed, "at_target": at_target,
            "verification": None, "llm_calls": 0,
        }

    retrieval = retrieve_courses(confirmed_gaps)
    addressable, blocked = retrieval["addressable"], retrieval["blocked"]

    plan_summary, steps, verification, llm_calls = _compose_and_verify(role_name, role_grade, addressable)

    # Cosmetic enrichment only -- attaches the course's own title/delivery
    # type back onto its step for display, looked up from that gap's own
    # candidate_courses (the same list course_integrity already checked
    # the cited course_id against). Doesn't affect verification, which
    # already ran above.
    gaps_by_id = {g["gap_id"]: g for g in addressable}
    for s in steps:
        gap = gaps_by_id.get(s.get("gap_id"))
        course = next(
            (c for c in gap["candidate_courses"] if c["course_id"] == s.get("course_id")), None
        ) if gap else None
        if course:
            s["course_title"] = course["course_title"]
            s["delivery_type"] = course["delivery_type"]

    if not addressable:
        status = "plan_ready_with_unaddressed_gaps"
    elif blocked:
        status = "plan_ready_with_unaddressed_gaps" if verification["passed"] else "plan_needs_review"
    else:
        status = "plan_ready" if verification["passed"] else "plan_needs_review"

    return {
        "status": status,
        "user_id": user_id, "role_id": role_id, "role_name": role_name, "role_grade": role_grade,
        "plan_summary": plan_summary,
        "steps": sorted(steps, key=lambda s: s.get("step_number") or 0),
        "unaddressed_gaps": [
            {
                "gap_id": g["gap_id"], "skill": g["skill"], "gap_severity": g["gap_severity"],
                "is_critical": g["is_critical"], "reason": g["reason"],
                "referenced_course_ids": g["referenced_course_ids"],
            }
            for g in blocked
        ],
        # Every confirmed gap, addressed or not -- kept around (not just
        # the split addressable/blocked views above) so build_dev_dashboard()
        # can estimate a timeline and look up Coursera leads for the whole
        # set without re-querying the database.
        "all_confirmed_gaps": confirmed_gaps,
        "never_assessed": never_assessed, "at_target": at_target,
        "verification": verification, "llm_calls": llm_calls,
    }


def run_agent5(user_id, max_steps=None):
    """
    Full A5 pipeline for one of the synthetic Skill_Gaps_TNI users
    (SYN-U001..SYN-U036 as loaded by scripts/build_sqlite.py's
    load_skill_gaps()).

    Returns status one of:
        "gaps_unavailable"             -- user_id has no skill_gaps rows at all
        "assessment_pending"           -- no confirmed gaps, but some
                                           skills were never assessed
        "no_gaps_at_target"            -- every skill is already at target
        "plan_ready"                   -- every confirmed gap addressed, verified
        "plan_ready_with_unaddressed_gaps" -- some/all confirmed gaps have
                                           no real course to cite (e.g. any
                                           R2R person -- see module docstring)
        "plan_needs_review"            -- verify_plan still failed after
                                           the one allowed revision

    max_steps caps how many confirmed gaps are planned for, highest
    severity/criticality first (does not affect status logic beyond that
    subset).
    """
    gap_result = gather_gaps(user_id)
    if not gap_result["success"]:
        return {"status": "gaps_unavailable", "user_id": user_id, "message": gap_result["message"]}

    confirmed = gap_result["confirmed_gaps"][:max_steps] if max_steps else gap_result["confirmed_gaps"]

    return _build_plan_result(
        user_id, gap_result["role_id"], gap_result["role_name"], gap_result["role_grade"],
        confirmed, gap_result["at_target"], gap_result["never_assessed"],
    )


# ---------------------------------------------------------------------------
# Live-pipeline entry point: derive gaps from a real Agent 3 score rather
# than the synthetic Skill_Gaps_TNI cohort.
# ---------------------------------------------------------------------------

# Gap-width bands, calibrated against the real Skill_Gaps_TNI distribution
# loaded by scripts/build_sqlite.py's load_skill_gaps() (gap width 0 -> "No
# gap" (91 rows), 1 -> "Moderate" (104 rows), 2 or 3 -> "High" (65 rows)).
_PROVISIONAL_BANDS = [(85, 0), (55, 1), (25, 2)]


def _provisional_level_from_score(pct, target_level):
    """
    PLACEHOLDER for the real proficiency-level engine, which is unbuilt
    (its signature is still blocked on the framework-registry decision).
    Maps an Agent 3 MCQ pct straight to a gap width via flat thresholds --
    no rubric, no evidence weighting, nothing but a pct cutoff. This is
    NOT the level engine. Replace this function the day the real one
    lands; every gap it produces is tagged is_provisional = True (see
    run_agent5_from_score) so nothing downstream mistakes a provisional
    level for an assessed one.

    Returns (current_level, gap_width).
    """
    if pct is None:
        return None, None
    gap_width = 3
    for threshold, width in _PROVISIONAL_BANDS:
        if pct >= threshold:
            gap_width = width
            break
    return max(target_level - gap_width, 0), gap_width


def _severity_from_gap_width(gap_width):
    if gap_width <= 0:
        return "No gap"
    if gap_width == 1:
        return "Moderate"
    return "High"


def run_agent5_from_score(score_result, blueprint, user_id=None, max_steps=None):
    """
    Derives provisional gaps straight from a live Agent 3 score
    (score_result["by_skill"]) plus the blueprint's own skill targets --
    for a resume-driven candidate who has no row in the synthetic
    Skill_Gaps_TNI sheet at all. From there it is the identical pipeline
    as run_agent5(): retrieve_courses / compose_plan / verify_plan /
    revise-once.

    A skill the assessment never asked about (not in by_skill -- either
    skipped upstream for insufficient detail, or simply not selected
    under the item budget) is reported under never_assessed, same
    semantics as a NULL gap_severity row in the synthetic path.

    Every gap produced here carries is_provisional = True.
    """
    by_skill = {s["skill"]: s for s in score_result.get("by_skill", [])}

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT skill, skill_id FROM role_skill_map WHERE role_id = ?;", (blueprint["role_id"],))
    skill_id_by_name = dict(cur.fetchall())
    conn.close()

    confirmed, at_target, never_assessed = [], [], []

    for skill in blueprint["skills"]:
        name = skill["skill"]
        target_level = skill["target_proficiency_level"]
        entry = by_skill.get(name)
        pct = entry["pct"] if entry else None

        base_gap = {
            "gap_id": f"PROV-{user_id or 'candidate'}-{skill_id_by_name.get(name, name)}",
            "role_id": blueprint["role_id"],
            "team_id": None,
            "skill_id": skill_id_by_name.get(name),
            "skill": name,
            "target_level": target_level,
            "target_level_name": skill["target_level_name"],
            "recommended_course_id": None,
            "tni_recommendation": None,
            "cross_functional_recommendation": None,
            "behavior_indicator": skill["behavior_indicator"],
            "capability": skill["capability"],
            "is_critical": skill["is_critical"],
            "is_synthetic_grounding": skill["is_synthetic_grounding"],
            "is_provisional": True,
        }

        if pct is None:
            never_assessed.append({**base_gap, "current_level": None, "gap_level_formula": None, "gap_severity": None})
            continue

        current_level, gap_width = _provisional_level_from_score(pct, target_level)
        severity = _severity_from_gap_width(gap_width)
        gap = {**base_gap, "current_level": current_level, "gap_level_formula": str(gap_width), "gap_severity": severity}

        (at_target if severity == "No gap" else confirmed).append(gap)

    confirmed.sort(key=_sort_key)
    if max_steps:
        confirmed = confirmed[:max_steps]

    return _build_plan_result(
        user_id, blueprint["role_id"], blueprint["role_name"], blueprint["role_grade"],
        confirmed, at_target, never_assessed,
    )


# ---------------------------------------------------------------------------
# Dashboard extras: model-suggested external (Coursera) courses and a
# rough remediation-timeline estimate. Both are ADDITIVE to the
# catalogue-verified plan above -- neither touches steps/verification,
# and both are clearly labeled as unverified/estimated so nothing here
# is mistaken for the governed plan's guarantees.
# ---------------------------------------------------------------------------

COURSERA_SYSTEM_PROMPT = """You are a learning-and-development researcher for a corporate finance-operations \
upskilling platform. For each skill gap you're given, name real Coursera course or Specialization titles, \
from your own general knowledge of Coursera's catalog, that would help close it. You are NOT connected to a \
live Coursera catalog and cannot confirm a title is still listed -- name only courses or providers you \
actually recognize, and never invent a plausible-sounding but fictional title; if you don't recognize a \
specific real title for a gap, say so and name the closest well-known provider/topic instead. Always respond \
with ONLY a single JSON object, no prose, no markdown code fences."""


def _coursera_search_url(query):
    return f"https://www.coursera.org/search?query={quote_plus(query)}"


def _format_gap_for_coursera_prompt(gap):
    line = (
        f'gap_id: "{gap["gap_id"]}" | skill: {gap["skill"]} | '
        f'capability: {gap["capability"] or "n/a"} | target level: {gap["target_level_name"]} | '
        f'severity: {gap["gap_severity"]}'
    )
    if gap.get("behavior_indicator"):
        line += f' | descriptor: {gap["behavior_indicator"]}'
    return line


def _build_coursera_prompt(gaps):
    gap_lines = "\n".join(_format_gap_for_coursera_prompt(g) for g in gaps)
    return f"""For each skill gap below, name 1-2 real Coursera course or Specialization titles that would \
help close it, plus one short sentence on why it fits this specific gap.

{gap_lines}

Respond with ONLY this JSON object:
{{
  "suggestions": [
    {{
      "gap_id": "...",
      "courses": [
        {{"title": "...", "why": "..."}}
      ]
    }}
  ]
}}"""


def suggest_coursera_courses(gaps):
    """
    One LLM call covering every gap at once -- named from the model's own
    general knowledge, NOT verified against a live Coursera catalog (this
    project has no Coursera API access). Every suggested course gets a
    search_url built straight from its own title, so the link always
    resolves to a real Coursera search results page even if the exact
    title the model named has since been renamed or retired. Every gap
    also gets a skill_search_url fallback built from the skill name
    alone, for when the model doesn't recognize a specific real title.
    Every course is tagged verified=False -- render this as a starting
    point for a manual search, never as a confirmed enrollment link.
    """
    if not gaps:
        return {"suggestions": []}

    result = call_llm_json(COURSERA_SYSTEM_PROMPT, _build_coursera_prompt(gaps), agent="A5")
    raw_by_gap_id = {s.get("gap_id"): s for s in result.get("suggestions", [])}

    suggestions = []
    for gap in gaps:
        raw = raw_by_gap_id.get(gap["gap_id"], {})
        courses = [
            {
                "title": c["title"].strip(),
                "why": (c.get("why") or "").strip(),
                "search_url": _coursera_search_url(c["title"]),
                "verified": False,
            }
            for c in raw.get("courses", [])
            if c.get("title") and c["title"].strip()
        ]
        suggestions.append({
            "gap_id": gap["gap_id"],
            "skill": gap["skill"],
            "gap_severity": gap["gap_severity"],
            "is_critical": gap["is_critical"],
            "courses": courses,
            "skill_search_url": _coursera_search_url(gap["skill"]),
        })

    return {"suggestions": suggestions}


_BASE_WEEKS_PER_GAP = 1.5
_BAND_MULTIPLIER = {
    "Strong fit": 0.85,
    "Potential fit -- development areas": 1.0,
    "Not yet ready": 1.25,
}


def estimate_remediation_duration(confirmed_gaps, evidence_report=None, mcq_score_pct=None):
    """
    Deterministic, 0-token planning estimate -- NOT a guarantee. Base
    weeks per gap, +1 week if severity is "High", +0.5 week if the skill
    is critical, then scaled by a multiplier:

      - if an Agent 4 evidence_report is available, its own
        compatibility_band is used -- that band already blends the live
        MCQ score (Agent 3) and evidence score (Agent 4) for this
        resume-matched role, so it's a single signal covering both;
      - otherwise falls back to mcq_score_pct alone (a milder swing),
        e.g. for someone who hasn't done the case-study round yet;
      - otherwise no adjustment.

    Returns per-gap weeks plus two totals: doing every course one at a
    time, and a rough parallel estimate (never faster than the single
    longest gap, even with unlimited parallelism).
    """
    per_gap = []
    total = 0.0
    for g in confirmed_gaps:
        weeks = _BASE_WEEKS_PER_GAP
        if g["gap_severity"] == "High":
            weeks += 1.0
        if g["is_critical"]:
            weeks += 0.5
        weeks = round(weeks, 1)
        per_gap.append({"gap_id": g["gap_id"], "skill": g["skill"], "estimated_weeks": weeks})
        total += weeks

    band = evidence_report.get("compatibility_band") if evidence_report else None
    if band in _BAND_MULTIPLIER:
        multiplier = _BAND_MULTIPLIER[band]
        basis = f"Agent 4 compatibility band ({band})"
    elif mcq_score_pct is not None:
        multiplier = 1.2 if mcq_score_pct < 45 else (0.9 if mcq_score_pct >= 75 else 1.0)
        basis = f"Agent 3 MCQ score ({mcq_score_pct}%) -- no evidence round completed yet"
    else:
        multiplier = 1.0
        basis = "no score/evidence signal available -- base estimate only"

    total_sequential = round(total * multiplier, 1)
    floor_weeks = max((g["estimated_weeks"] for g in per_gap), default=0)
    total_parallel = round(max(total_sequential / 2, floor_weeks), 1)

    return {
        "per_gap": per_gap,
        "multiplier_applied": multiplier,
        "multiplier_basis": basis,
        "estimated_weeks_one_at_a_time": total_sequential,
        "estimated_weeks_with_two_in_parallel": total_parallel,
        "note": (
            "Rough planning estimate from each gap's severity/criticality, scaled by the candidate's own "
            "score/evidence signal -- not a guaranteed timeline."
        ),
    }


def build_dev_dashboard(plan_result, evidence_report=None, mcq_score_pct=None):
    """
    Wraps a run_agent5()/run_agent5_from_score() result with the two
    additive extras above. Neither touches plan_result["steps"] or its
    verification -- they're appended as clearly separate, clearly
    labeled sections for the dashboard UI to render alongside the
    governed plan.

    coursera_suggestions covers every confirmed gap, whether or not it
    already has a step in the internal plan -- our own training_catalogue
    is small and, for R2R, empty (see module docstring), so external
    leads are useful even alongside an internally-verified step.
    """
    if plan_result["status"] == "gaps_unavailable":
        return {**plan_result, "duration_estimate": None, "coursera_suggestions": []}

    confirmed_gaps = plan_result.get("all_confirmed_gaps") or []
    if not confirmed_gaps:
        return {
            **plan_result,
            "duration_estimate": {
                "per_gap": [], "estimated_weeks_one_at_a_time": 0, "estimated_weeks_with_two_in_parallel": 0,
                "note": "No confirmed gaps to plan for.",
            },
            "coursera_suggestions": [],
        }

    duration = estimate_remediation_duration(confirmed_gaps, evidence_report=evidence_report, mcq_score_pct=mcq_score_pct)
    coursera = suggest_coursera_courses(confirmed_gaps)

    return {
        **plan_result,
        "duration_estimate": duration,
        "coursera_suggestions": coursera["suggestions"],
        "llm_calls": plan_result.get("llm_calls", 0) + 1,
    }


def print_plan_result(result):
    """Test helper: prints a readable summary, matching agents/'s style."""
    print(f"=== Agent 5 -- {result.get('role_name')} ({result.get('user_id')}) ===")
    print(f"Status: {result['status']}")
    if result["status"] == "gaps_unavailable":
        print(f"  {result['message']}\n")
        return

    steps = result.get("steps") or []
    unaddressed = result.get("unaddressed_gaps") or []
    llm_calls = result.get("llm_calls")
    print(
        f"Steps: {len(steps)} | Unaddressed gaps: {len(unaddressed)} | "
        f"Never assessed: {len(result.get('never_assessed') or [])} | LLM calls: {llm_calls}"
    )
    if result.get("plan_summary"):
        print(f"Plan: {result['plan_summary']}")
    for s in steps:
        print(f"  {s.get('step_number')}. [{s.get('gap_id')}] {s.get('skill')} -> {s.get('course_id')}")
        print(f"     closes: {s.get('descriptor_clause_addressed')}")
    for g in unaddressed:
        print(f"  BLOCKED [{g['gap_id']}] {g['skill']} ({g['gap_severity']}): {g['reason']}")
    if result.get("verification"):
        print(f"  Verification: {result['verification']['checks']}")
    print()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")

    # Deterministic-only smoke test (no LLM calls, no API key needed):
    # exercises gather_gaps() + retrieve_courses() across the three
    # documented cases -- addressable (O2C), fully blocked (R2R), and
    # never-assessed -- plus the unknown-user path.
    for uid in ["SYN-U032", "SYN-U001", "SYN-U004", "NOPE"]:
        gap_result = gather_gaps(uid)
        if not gap_result["success"]:
            print(f"{uid}: gaps_unavailable -- {gap_result['message']}")
            continue
        retrieval = retrieve_courses(gap_result["confirmed_gaps"])
        print(
            f"{uid} ({gap_result['role_name']}): "
            f"{len(gap_result['confirmed_gaps'])} confirmed, "
            f"{len(retrieval['addressable'])} addressable, "
            f"{len(retrieval['blocked'])} blocked, "
            f"{len(gap_result['never_assessed'])} never-assessed"
        )

    print("\n--- Full pipeline (makes LLM calls) ---\n")
    print_plan_result(run_agent5("SYN-U032"))
    print_plan_result(run_agent5("SYN-U001"))
    print_plan_result(run_agent5("SYN-U004"))
    print_plan_result(run_agent5("NOPE"))

    print("\n--- Dashboard extras (Coursera + duration, makes 1 more LLM call) ---\n")
    dashboard = build_dev_dashboard(run_agent5("SYN-U032"))
    print(f"Duration: {dashboard['duration_estimate']}")
    for s in dashboard["coursera_suggestions"]:
        print(f"  {s['skill']}: {[c['title'] for c in s['courses']] or s['skill_search_url']}")
