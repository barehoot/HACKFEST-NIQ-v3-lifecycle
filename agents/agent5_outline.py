"""
agent5_outline.py

Agent 5, training-session outline node. Runs after the manager's decision on
an assessment:

  - mode "gap"      -- the manager rejected the assessment: sessions that take
                       each weak skill from the level shown to the level the
                       assessment was for (skill-gap training);
  - mode "skill_up" -- the manager approved and certified levels: sessions
                       that take certified skills to the next level (the next
                       role's target where there is one), plus any skills the
                       assessment showed were still short.

Same agent shape as the development plan (agent5_devplan.py):
  gather   -> the role skill, its target-level indicator and the real
              catalogue courses for each item (0 tokens, SQL)
  compose  -> the session outline (1 LLM call)
  verify   -> deterministic checks: every skill covered, at most 3 sessions
              per skill, courses only from that skill's own candidates,
              allowed formats, sane durations, objectives grounded in the
              level indicator (0 tokens)
  revise   -> once, with the failures, if verify fails
  fallback -> a deterministic template outline if the model is unavailable
              or still fails verification, so the manager's decision never
              blocks on the LLM. It is labelled as a template.

Only the skill, levels and a short evidence note leave the portal: no name,
no transcript, no resume. The evidence note comes from Agent 4's report, so
it is treated as untrusted data (it was derived from candidate text).
"""

import re
import sqlite3
import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
DB_PATH = AGENTS_DIR.parent / "db" / "talent360i.sqlite"
sys.path.insert(0, str(AGENTS_DIR))

import security  # noqa: E402
from agent5_devplan import _is_usable_course_title  # noqa: E402
from llm_client import call_llm_json  # noqa: E402

MAX_ITEMS = 8
MAX_SESSIONS_PER_SKILL = 3
MIN_MINUTES, MAX_MINUTES = 20, 240
FORMATS = ("Workshop", "Scenario lab", "Self-paced", "Coaching", "On-the-job practice", "Microlearning")
LEVEL_NAMES = {0: "Not assessed", 1: "Awareness", 2: "Working Knowledge", 3: "Proficient",
               4: "Advanced", 5: "Expert / Strategic"}

OUTLINE_SYSTEM_PROMPT = """You are a learning designer for a corporate finance-operations upskilling \
platform. You turn confirmed skill gaps into a short, practical training-session outline that a manager \
can run. Each session builds the exact behaviour in the skill's target-level indicator. You may cite ONLY \
a course_id listed for that same skill, or null. Always respond with ONLY a single JSON object, no prose, \
no markdown code fences."""


class OutlineInputError(ValueError):
    pass


# ---------------------------------------------------------------------------
# gather (0 tokens)
# ---------------------------------------------------------------------------

def _courses_for(cur, skill_id, skill):
    cur.execute(
        """
        SELECT DISTINCT tc.course_id, tc.course_title, tc.delivery_type
        FROM training_skill_map tsm JOIN training_catalogue tc ON tc.course_id = tsm.course_id
        WHERE tsm.skill_id = ?
        ORDER BY tc.course_id
        """,
        (skill_id,),
    )
    courses = [{"course_id": r[0], "course_title": r[1], "delivery_type": r[2]}
               for r in cur.fetchall() if _is_usable_course_title(r[1])]
    # A course whose title names the skill first (the dataset's mapping is shifted for some R2R skills).
    key = (skill or "").lower()
    courses.sort(key=lambda c: key not in c["course_title"].lower())
    return courses[:3]


def gather(role_id, items, db_path=DB_PATH):
    """Resolves each requested item against the role's own skills. Raises
    OutlineInputError for an unknown role; drops skills the role doesn't have."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT role_name, role_grade FROM role_master WHERE role_id = ?", (role_id,))
        role = cur.fetchone()
        if not role:
            raise OutlineInputError("Unknown role.")
        cur.execute(
            """
            SELECT rsm.skill, rsm.skill_id, rsm.capability, rsm.is_critical, rsm.behavior_indicator,
                   sm.l1_indicator, sm.l2_indicator, sm.l3_indicator, sm.l4_indicator, sm.l5_indicator
            FROM role_skill_map rsm LEFT JOIN skill_master sm ON sm.skill_id = rsm.skill_id
            WHERE rsm.role_id = ?
            """,
            (role_id,),
        )
        skills = {r[0].strip().lower(): r for r in cur.fetchall()}
        cur.execute("SELECT level, behavior_indicator FROM proficiency_levels")
        generic = dict(cur.fetchall())

        resolved, dropped, seen = [], [], set()
        for item in items[:MAX_ITEMS]:
            row = skills.get(str(item.get("skill", "")).strip().lower())
            if not row or row[0] in seen:
                dropped.append(item.get("skill"))
                continue
            seen.add(row[0])
            to_level = int(item["to_level"])
            from_level = item.get("from_level")
            indicators = row[5:10]
            resolved.append({
                "skill": row[0], "skill_id": row[1], "capability": row[2], "is_critical": bool(row[3]),
                "from_level": None if from_level is None else int(from_level), "to_level": to_level,
                "to_level_name": LEVEL_NAMES.get(to_level, f"L{to_level}"),
                "level_indicator": indicators[to_level - 1] or generic.get(to_level) or "",
                "behavior_indicator": row[4] or "",
                "evidence_note": str(item.get("evidence_note") or "")[:300],
                "candidate_courses": _courses_for(cur, row[1], row[0]),
            })
    finally:
        conn.close()
    if not resolved:
        raise OutlineInputError("None of the requested skills belong to this role.")
    return {"role_id": role_id, "role_name": role[0], "role_grade": role[1], "items": resolved, "dropped": dropped}


# ---------------------------------------------------------------------------
# compose (1 LLM call)
# ---------------------------------------------------------------------------

def _format_item(item):
    lines = [
        f"skill: {item['skill']} (capability: {item['capability'] or '-'}; critical: {item['is_critical']})",
        f"move from: {'L' + str(item['from_level']) if item['from_level'] is not None else 'unknown'} "
        f"to L{item['to_level']} - {item['to_level_name']}",
        f"target-level indicator (the behaviour to build): {item['level_indicator'] or '(none on file)'}",
        f"role behaviour indicator: {item['behavior_indicator'] or '(none on file)'}",
    ]
    if item["evidence_note"]:
        lines.append("what the assessment found missing:\n"
                     + security.wrap_untrusted("assessment_note", item["evidence_note"], 300))
    if item["candidate_courses"]:
        lines.append("courses you may cite for THIS skill (or null):")
        lines += [f"  - {c['course_id']}: \"{c['course_title']}\" ({c['delivery_type']})" for c in item["candidate_courses"]]
    else:
        lines.append("courses you may cite for THIS skill: none -- use course_id null")
    return "\n".join(lines)


def _build_prompt(context, mode, feedback=None):
    purpose = ("close the gaps an assessment found (skill-gap training after the manager rejected the assessment)"
               if mode == "gap" else
               "build on levels the manager has just certified and prepare the person for the next level (skill-up training)")
    blocks = "\n\n".join(_format_item(i) for i in context["items"])
    fb = ""
    if feedback:
        fb = "\n\nYour previous outline failed these checks -- fix every one:\n" + "\n".join(f"- {f}" for f in feedback)
    return f"""Design a training-session outline for a {context['role_name']} (grade {context['role_grade']}) to {purpose}.

Rules:
- Give every skill below 1 to {MAX_SESSIONS_PER_SKILL} sessions, in the order listed (critical skills are listed first).
- format must be one of: {", ".join(FORMATS)}.
- duration_minutes between {MIN_MINUTES} and {MAX_MINUTES}.
- objectives: 2 or 3 short, observable outcomes that use the words of that skill's target-level indicator.
- activities: 2 or 3 concrete things the person does in the session (not "learn about").
- practice_task: one realistic on-the-job task that proves the behaviour.
- success_check: how the manager or SME will confirm the level was reached.
- course_id: one of that skill's listed courses, or null.

{blocks}{fb}

Respond with ONLY this JSON object:
{{
  "summary": "one or two sentences for the employee",
  "sessions": [
    {{"skill": "...", "title": "...", "format": "...", "duration_minutes": 60,
      "objectives": ["...", "..."], "activities": ["...", "..."],
      "practice_task": "...", "success_check": "...", "course_id": null}}
  ]
}}"""


def compose(context, mode, feedback=None):
    result = call_llm_json(OUTLINE_SYSTEM_PROMPT, _build_prompt(context, mode, feedback), agent="A5")
    sessions = result.get("sessions")
    return str(result.get("summary") or "")[:600], sessions if isinstance(sessions, list) else []


# ---------------------------------------------------------------------------
# verify (0 tokens)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z]{4,}")
_STOP = {"with", "from", "that", "this", "their", "into", "using", "able", "will", "have", "when", "what",
         "which", "they", "them", "your", "more", "other", "than", "then", "also", "each", "such"}


def _words(text):
    return set(_WORD_RE.findall((text or "").lower())) - _STOP


def _str_list(value, limit):
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:240] for v in value if str(v).strip()][:limit]


def verify(sessions, context):
    items = {i["skill"].lower(): i for i in context["items"]}
    failures = []
    per_skill = {}
    for n, s in enumerate(sessions, 1):
        if not isinstance(s, dict):
            failures.append(f"session {n}: not an object")
            continue
        item = items.get(str(s.get("skill", "")).strip().lower())
        if not item:
            failures.append(f"session {n}: skill '{s.get('skill')}' is not one of the listed skills")
            continue
        per_skill[item["skill"]] = per_skill.get(item["skill"], 0) + 1
        if s.get("format") not in FORMATS:
            failures.append(f"session {n} ({item['skill']}): format '{s.get('format')}' is not allowed")
        try:
            minutes = int(s.get("duration_minutes"))
        except (TypeError, ValueError):
            minutes = 0
        if not MIN_MINUTES <= minutes <= MAX_MINUTES:
            failures.append(f"session {n} ({item['skill']}): duration_minutes must be {MIN_MINUTES}-{MAX_MINUTES}")
        valid_courses = {c["course_id"] for c in item["candidate_courses"]}
        if s.get("course_id") not in (None, "", "null") and s.get("course_id") not in valid_courses:
            failures.append(f"session {n} ({item['skill']}): course {s.get('course_id')} was not offered for this skill")
        objectives = _str_list(s.get("objectives"), 4)
        if not objectives or not _str_list(s.get("activities"), 4) or not str(s.get("title") or "").strip():
            failures.append(f"session {n} ({item['skill']}): needs a title, objectives and activities")
        anchor = _words(item["level_indicator"]) | _words(item["behavior_indicator"]) | _words(item["skill"])
        if anchor and objectives and not (_words(" ".join(objectives)) & anchor):
            failures.append(f"session {n} ({item['skill']}): objectives don't use the target-level indicator's language")
    for item in context["items"]:
        count = per_skill.get(item["skill"], 0)
        if count == 0:
            failures.append(f"coverage: '{item['skill']}' has no session")
        elif count > MAX_SESSIONS_PER_SKILL:
            failures.append(f"coverage: '{item['skill']}' has {count} sessions (max {MAX_SESSIONS_PER_SKILL})")
    return {"passed": not failures, "failures": failures}


def _normalise(sessions, context):
    """Orders sessions by the item order, numbers them and attaches course titles and levels."""
    order = {i["skill"].lower(): n for n, i in enumerate(context["items"])}
    items = {i["skill"].lower(): i for i in context["items"]}
    out = []
    for s in sorted(sessions, key=lambda s: order.get(str(s.get("skill", "")).lower(), 99)):
        item = items[str(s["skill"]).lower()]
        course = next((c for c in item["candidate_courses"] if c["course_id"] == s.get("course_id")), None)
        out.append({
            "skill": item["skill"], "is_critical": item["is_critical"],
            "from_level": item["from_level"], "to_level": item["to_level"], "to_level_name": item["to_level_name"],
            "title": str(s["title"]).strip()[:140], "format": s["format"], "duration_minutes": int(s["duration_minutes"]),
            "objectives": _str_list(s.get("objectives"), 4), "activities": _str_list(s.get("activities"), 4),
            "practice_task": str(s.get("practice_task") or "").strip()[:400],
            "success_check": str(s.get("success_check") or "").strip()[:400],
            "course_id": course["course_id"] if course else None,
            "course_title": course["course_title"] if course else None,
        })
    for n, s in enumerate(out, 1):
        s["session_number"] = n
    return out


# ---------------------------------------------------------------------------
# fallback (0 tokens)
# ---------------------------------------------------------------------------

def template_sessions(context, mode):
    sessions = []
    for item in context["items"]:
        indicator = item["level_indicator"] or item["behavior_indicator"] or f"{item['skill']} at L{item['to_level']}"
        course = item["candidate_courses"][0] if item["candidate_courses"] else None
        sessions.append({
            "skill": item["skill"], "format": "Workshop" if course is None else "Self-paced", "duration_minutes": 60,
            "title": f"{item['skill']}: what L{item['to_level']} ({item['to_level_name']}) looks like",
            "objectives": [f"Explain the L{item['to_level']} expectation: {indicator}",
                           f"Identify where current practice in {item['skill']} falls short of it"],
            "activities": ["Walk through the level descriptor and the SOP steps it covers with an SME",
                           "Review a worked example and note each control or decision point"],
            "practice_task": "", "success_check": "",
            "course_id": course["course_id"] if course else None,
        })
        sessions.append({
            "skill": item["skill"], "format": "Scenario lab", "duration_minutes": 90,
            "title": f"{item['skill']}: supervised scenario practice",
            "objectives": [f"Apply {item['skill']} at L{item['to_level']}: {indicator}"],
            "activities": ["Work a realistic case end to end with a reviewer",
                           "Document the decision, the evidence kept and any escalation"],
            "practice_task": f"Handle a live or simulated {item['skill']} case independently and record the evidence.",
            "success_check": f"Manager or SME confirms the L{item['to_level']} behaviour on the case"
                             + (" before the re-assessment." if mode == "gap" else "."),
            "course_id": None,
        })
    return sessions


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def build_training_outline(role_id, mode, items, db_path=DB_PATH):
    if mode not in ("gap", "skill_up"):
        raise OutlineInputError("mode must be 'gap' or 'skill_up'.")
    context = gather(role_id, items, db_path=db_path)

    generated_by, llm_calls, summary, verification = "agent", 0, "", None
    try:
        summary, sessions = compose(context, mode)
        llm_calls = 1
        verification = verify(sessions, context)
        if not verification["passed"]:
            summary, sessions = compose(context, mode, feedback=verification["failures"])
            llm_calls = 2
            verification = verify(sessions, context)
    except (RuntimeError, ValueError) as exc:
        security.audit("a5_outline", actor="A5", outcome="llm_unavailable", details={"error": security.redact(exc)})
        sessions, verification = [], {"passed": False, "failures": ["the model was unavailable"]}

    if not verification["passed"]:
        generated_by = "template"
        failed_checks = verification["failures"]
        sessions = template_sessions(context, mode)
        summary = ("A standard two-session outline per skill (the AI outline could not be verified, so this "
                   "template was used). Your manager can adapt it.")
        verification = {**verify(sessions, context), "ai_failures": failed_checks[:10]}

    sessions = _normalise(sessions, context)
    return {
        "mode": mode, "role_id": context["role_id"], "role_name": context["role_name"],
        "summary": summary, "sessions": sessions,
        "total_minutes": sum(s["duration_minutes"] for s in sessions),
        "skills": [{"skill": i["skill"], "from_level": i["from_level"], "to_level": i["to_level"],
                    "is_critical": i["is_critical"]} for i in context["items"]],
        "dropped_skills": context["dropped"],
        "generated_by": generated_by, "verification": verification, "llm_calls": llm_calls,
    }
