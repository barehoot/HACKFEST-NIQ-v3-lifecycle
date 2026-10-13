"""
agent6_performance.py

Agent 6, performance insight. Turns one employee's de-identified status
signals -- the band the deterministic engine computed (services/performance.py
in the portal), its factors, skill levels, evidence titles, review rating,
development actions and probation progress -- into guidance a manager or
business leader can act on:

  mode "insight" -- a short narrative, strengths, risks, manager actions,
                    development actions and whether escalation is warranted
  mode "plan"    -- the same, used for its development actions (the portal
                    re-verifies and merges them into the IDP)
  mode "review"  -- also a performance-review draft the manager edits

Same agent shape as Agent 5's outline node:
  compose  -> 1 LLM call
  verify   -> deterministic: the band is never changed, development actions use
              allowed types, only the employee's own skills, due in 7-120 days;
              the escalation flag must agree with the band; a review draft's
              suggested rating must sit in the band's range; no dismissal or
              termination language, and no protected characteristics (0 tokens)
  revise   -> once, with the failures
  fallback -> a deterministic template, labelled, so the page never blocks on the LLM

The agent only ever proposes. The band is the engine's; ratings, escalations
and every decision about the person are made by people. Its input carries no
name, id, email or manager (the portal strips them before calling).
"""

import re
import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENTS_DIR))

import security  # noqa: E402
from llm_client import call_llm_json  # noqa: E402

BANDS = ("Exceeding", "On track", "At risk", "Off track", "Not enough data")
MODES = ("insight", "plan", "review")
ACTION_TYPES = ("training", "practice", "mentoring", "stretch", "behaviour", "certification", "reading")
MIN_DUE_DAYS, MAX_DUE_DAYS = 7, 120
MAX_ACTIONS = 5
MAX_LIST = 4
RATING_RANGE = {"Exceeding": (4, 5), "On track": (3, 4), "At risk": (2, 3), "Off track": (1, 2), "Not enough data": (1, 5)}

# Employment decisions belong to people, and protected characteristics have no
# place in performance guidance: either one fails verification. Phrased narrowly
# so finance vocabulary ("aged debtors", "disabled the control", "dismissed the
# duplicate invoice") doesn't trip it.
_BANNED_RE = re.compile(
    r"\b(terminat(e|ion of) (his|her|their|the employee'?s?) (employment|contract)|dismissal|"
    r"(dismiss|fire|sack) (him|her|them|the employee)|firing|let (him|her|them|the employee) go|lay ?off|"
    r"(his|her|their) age|years old|too (old|young)|pregnan\w*|maternity|paternity|religio\w*|ethnic\w*|race|"
    r"racial|gender|disabilit\w*|nationality|marital status|sexual orientation)\b", re.IGNORECASE)

SYSTEM_PROMPT = """You are a performance-development advisor for managers in a corporate finance-operations \
organisation. You turn documented, de-identified signals about one employee into fair, specific, practical \
guidance. Use only the facts given. Never recommend dismissal or termination, never decide an outcome, and never \
mention age, gender, race, religion, disability, pregnancy, nationality or family status. Always respond with ONLY \
a single JSON object, no prose, no markdown code fences."""


class InsightInputError(ValueError):
    pass


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------

def _clean(value, limit=200):
    return security.sanitize_prompt_text(str(value or ""), limit)


def normalise_payload(payload):
    """Validates and bounds the portal's payload. Raises InsightInputError."""
    if not isinstance(payload, dict):
        raise InsightInputError("Payload must be an object.")
    band = payload.get("band")
    mode = payload.get("mode", "insight")
    if band not in BANDS or mode not in MODES:
        raise InsightInputError("Unknown band or mode.")
    skills = []
    for s in (payload.get("skills") or [])[:15]:
        if isinstance(s, dict) and s.get("skill"):
            skills.append({"skill": _clean(s["skill"], 120), "current": s.get("current"), "target": s.get("target"),
                           "critical": bool(s.get("critical"))})
    return {
        "mode": mode, "band": band, "score": payload.get("score"),
        "role_name": _clean(payload.get("role_name"), 120) or "Unknown role",
        "next_role": _clean(payload.get("next_role"), 120) or None,
        "factors": [{"name": _clean(f.get("name"), 60), "score": f.get("score"), "detail": _clean(f.get("detail"), 200)}
                    for f in (payload.get("factors") or [])[:6] if isinstance(f, dict)],
        "skills": skills,
        "evidence": [{"kind": _clean(e.get("kind"), 20), "sentiment": _clean(e.get("sentiment"), 10),
                      "date": _clean(e.get("date"), 10), "title": _clean(e.get("title"), 160)}
                     for e in (payload.get("evidence") or [])[:12] if isinstance(e, dict)],
        "review_rating": payload.get("review_rating") if payload.get("review_rating") in (1, 2, 3, 4, 5) else None,
        "actions": [{"title": _clean(a.get("title"), 140), "status": _clean(a.get("status"), 20), "due": _clean(a.get("due"), 10)}
                    for a in (payload.get("actions") or [])[:10] if isinstance(a, dict)],
        "probation": payload.get("probation") if isinstance(payload.get("probation"), dict) else None,
        "signals": [_clean(s, 60) for s in (payload.get("signals") or [])[:8]],
    }


# ---------------------------------------------------------------------------
# compose (1 LLM call)
# ---------------------------------------------------------------------------

def _build_prompt(p, feedback=None):
    lines = [f"Role: {p['role_name']}" + (f" (next role on the ladder: {p['next_role']})" if p["next_role"] else ""),
             f"Status band computed by the platform (do not change it): {p['band']}"
             + (f", score {p['score']}/100" if p["score"] is not None else ""),
             "Factors behind the band:"]
    lines += [f"  - {f['name']}: {f['score']}/100 -- {f['detail']}" for f in p["factors"]] or ["  - none"]
    lines.append("Assessed skills (current vs target level, 1-5):")
    lines += [f"  - {s['skill']}: L{s['current']} vs L{s['target']}{' (critical)' if s['critical'] else ''}"
              for s in p["skills"]] or ["  - none assessed"]
    if p["review_rating"]:
        lines.append(f"Latest shared review rating: {p['review_rating']}/5")
    if p["actions"]:
        lines.append("Current development actions:")
        lines += [f"  - [{a['status']}] {a['title']} (due {a['due'] or '-'})" for a in p["actions"]]
    if p["probation"]:
        pr = p["probation"]
        lines.append(f"New joiner on probation: {pr.get('days_left')} days left, "
                     f"{pr.get('checkpoints_done')} of {pr.get('checkpoints')} check-ins done")
    if p["signals"]:
        lines.append("Signals: " + "; ".join(p["signals"]))
    if p["evidence"]:
        evidence = "\n".join(f"{e['date']} {e['kind']} ({e['sentiment']}): {e['title']}" for e in p["evidence"])
        lines.append("Documented evidence (titles written by the manager or from tagged messages):\n"
                     + security.wrap_untrusted("evidence_titles", evidence, 2400))
    skills = ", ".join(s["skill"] for s in p["skills"]) or "(none -- use skill null)"
    review = ""
    if p["mode"] == "review":
        low, high = RATING_RANGE[p["band"]]
        review = f""",
  "review_draft": {{"summary": "3-4 sentences", "strengths": "...", "improvements": "...",
                    "suggested_rating": {low}}}"""
        review_rule = f"\n- review_draft.suggested_rating must be between {low} and {high} for this band; the manager decides the final rating."
    else:
        review_rule = ""
    fb = ""
    if feedback:
        fb = "\n\nYour previous answer failed these checks -- fix every one:\n" + "\n".join(f"- {f}" for f in feedback)
    return f"""Write development guidance for this employee.

{chr(10).join(lines)}

Rules:
- summary: 2-3 sentences on where the employee stands and why, citing the factors. Neutral, factual tone.
- strengths and risks: up to {MAX_LIST} short items each, grounded in the facts above.
- manager_actions: up to {MAX_LIST} concrete things the manager should do next (support first, then expectations).
- development_actions: up to {MAX_ACTIONS} actions for the employee. action_type one of: {", ".join(ACTION_TYPES)}.
  skill must be one of: {skills} -- or null. due_in_days between {MIN_DUE_DAYS} and {MAX_DUE_DAYS}.
  success_measure: how progress will be observed.
- escalation.recommended may be true only when the band is "At risk" or "Off track" and the development actions
  are not working; the reason must cite the facts. Escalation means a leader reviews the case -- never dismissal.{review_rule}{fb}

Respond with ONLY this JSON object:
{{
  "summary": "...",
  "strengths": ["..."],
  "risks": ["..."],
  "manager_actions": ["..."],
  "development_actions": [{{"title": "...", "action_type": "practice", "skill": null, "success_measure": "...", "due_in_days": 30}}],
  "escalation": {{"recommended": false, "reason": ""}}{review}
}}"""


def compose(p, feedback=None):
    return call_llm_json(SYSTEM_PROMPT, _build_prompt(p, feedback), agent="A6")


# ---------------------------------------------------------------------------
# verify (0 tokens)
# ---------------------------------------------------------------------------

def _strings(value, limit=MAX_LIST, chars=240):
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:chars] for v in value if isinstance(v, str) and v.strip()][:limit]


def _all_text(result):
    parts = [str(result.get("summary") or "")]
    for key in ("strengths", "risks", "manager_actions"):
        parts += _strings(result.get(key), 10, 400)
    for a in result.get("development_actions") or []:
        if isinstance(a, dict):
            parts += [str(a.get("title") or ""), str(a.get("success_measure") or "")]
    esc = result.get("escalation") if isinstance(result.get("escalation"), dict) else {}
    parts.append(str(esc.get("reason") or ""))
    draft = result.get("review_draft") if isinstance(result.get("review_draft"), dict) else {}
    parts += [str(draft.get(k) or "") for k in ("summary", "strengths", "improvements")]
    return " ".join(parts)


def verify(result, p):
    failures = []
    if not isinstance(result, dict):
        return {"passed": False, "failures": ["response is not an object"]}
    if not str(result.get("summary") or "").strip():
        failures.append("summary is missing")
    skills = {s["skill"].lower() for s in p["skills"]}
    actions = result.get("development_actions")
    if not isinstance(actions, list):
        failures.append("development_actions must be a list")
        actions = []
    if len(actions) > MAX_ACTIONS:
        failures.append(f"at most {MAX_ACTIONS} development_actions")
    for n, a in enumerate(actions[:MAX_ACTIONS], 1):
        if not isinstance(a, dict) or not str(a.get("title") or "").strip():
            failures.append(f"development action {n}: needs a title")
            continue
        if a.get("action_type") not in ACTION_TYPES:
            failures.append(f"development action {n}: action_type '{a.get('action_type')}' is not allowed")
        if a.get("skill") not in (None, "", "null") and str(a.get("skill")).lower() not in skills:
            failures.append(f"development action {n}: skill '{a.get('skill')}' is not one of the employee's skills")
        try:
            days = int(a.get("due_in_days"))
        except (TypeError, ValueError):
            days = 0
        if not MIN_DUE_DAYS <= days <= MAX_DUE_DAYS:
            failures.append(f"development action {n}: due_in_days must be {MIN_DUE_DAYS}-{MAX_DUE_DAYS}")
    escalation = result.get("escalation") if isinstance(result.get("escalation"), dict) else {}
    if escalation.get("recommended") and p["band"] not in ("At risk", "Off track"):
        failures.append(f"escalation can't be recommended when the band is {p['band']}")
    if p["mode"] == "review":
        draft = result.get("review_draft") if isinstance(result.get("review_draft"), dict) else None
        low, high = RATING_RANGE[p["band"]]
        if not draft or not str(draft.get("summary") or "").strip():
            failures.append("review_draft with a summary is required")
        elif draft.get("suggested_rating") not in range(low, high + 1):
            failures.append(f"review_draft.suggested_rating must be {low}-{high} for the band {p['band']}")
    banned = sorted({m.group(0).lower() for m in _BANNED_RE.finditer(_all_text(result))})
    if banned:
        failures.append("remove employment-decision or protected-characteristic language: " + ", ".join(banned[:5]))
    return {"passed": not failures, "failures": failures}


def _normalise(result, p):
    escalation = result.get("escalation") if isinstance(result.get("escalation"), dict) else {}
    out = {
        "summary": str(result.get("summary") or "").strip()[:700],
        "strengths": _strings(result.get("strengths")), "risks": _strings(result.get("risks")),
        "manager_actions": _strings(result.get("manager_actions")),
        "development_actions": [
            {"title": str(a["title"]).strip()[:140], "action_type": a["action_type"],
             "skill": next((s["skill"] for s in p["skills"] if s["skill"].lower() == str(a.get("skill") or "").lower()), None),
             "success_measure": str(a.get("success_measure") or "").strip()[:240], "due_in_days": int(a["due_in_days"])}
            for a in (result.get("development_actions") or [])[:MAX_ACTIONS]],
        "escalation": {"recommended": bool(escalation.get("recommended")),
                       "reason": str(escalation.get("reason") or "").strip()[:400]},
    }
    if p["mode"] == "review":
        draft = result["review_draft"]
        out["review_draft"] = {k: str(draft.get(k) or "").strip()[:1200] for k in ("summary", "strengths", "improvements")}
        out["review_draft"]["suggested_rating"] = int(draft["suggested_rating"])
    return out


# ---------------------------------------------------------------------------
# fallback (0 tokens)
# ---------------------------------------------------------------------------

def template_insight(p):
    gaps = sorted((s for s in p["skills"] if s["current"] is not None and s["target"] and s["current"] < s["target"]),
                  key=lambda s: (not s["critical"], s["current"] - s["target"]))
    strong = [s["skill"] for s in p["skills"] if s["current"] is not None and s["target"] and s["current"] >= s["target"]]
    factors = sorted(p["factors"], key=lambda f: f["score"] or 0)
    summary = f"Status is {p['band'].lower()}" + (f" (score {p['score']}/100)" if p["score"] is not None else "") + "."
    if factors:
        summary += f" Strongest factor: {factors[-1]['name'].lower()}; weakest: {factors[0]['name'].lower()}."
    actions = [{"title": f"Apply {s['skill']} on a live case with a reviewer", "action_type": "practice",
                "skill": s["skill"], "success_measure": f"L{s['target']} behaviour confirmed by the manager or an SME.",
                "due_in_days": 45} for s in gaps[:2]]
    if p["band"] in ("At risk", "Off track"):
        actions.append({"title": "Weekly coaching with a mentor or buddy", "action_type": "mentoring", "skill": None,
                        "success_measure": "Four sessions held and logged as 1:1s.", "due_in_days": 30})
    manager = {"Exceeding": ["Recognise the impact specifically", "Discuss a stretch assignment or the next role"],
               "On track": ["Keep regular 1:1s", "Agree one stretch goal"],
               "At risk": ["Agree two or three focused actions with 30-day check-ins", "Hold weekly 1:1s and log them"],
               "Off track": ["Set out clear, written expectations with weekly checkpoints",
                             "Involve your HR business partner in a structured improvement plan"],
               "Not enough data": ["Log 1:1s and evidence", "Assign an assessment so the status can be calculated"]}[p["band"]]
    out = {
        "summary": summary,
        "strengths": [f"At target in {', '.join(strong[:3])}"] if strong else [],
        "risks": [f"{s['skill']} is L{s['current']} against a target of L{s['target']}" for s in gaps[:3]],
        "manager_actions": manager,
        "development_actions": actions[:MAX_ACTIONS],
        "escalation": {"recommended": False, "reason": ""},
    }
    if p["mode"] == "review":
        low, high = RATING_RANGE[p["band"]]
        out["review_draft"] = {
            "summary": summary + " This draft was generated from the recorded factors; edit it before sharing.",
            "strengths": "; ".join(out["strengths"]) or "Add specific examples from the evidence.",
            "improvements": "; ".join(out["risks"]) or "Add specific development areas.",
            "suggested_rating": high if p["band"] in ("Exceeding", "On track") else low,
        }
    return out


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def build_insight(payload):
    p = normalise_payload(payload)
    generated_by, llm_calls, verification = "agent", 0, None
    try:
        result = compose(p)
        llm_calls = 1
        verification = verify(result, p)
        if not verification["passed"]:
            result = compose(p, feedback=verification["failures"])
            llm_calls = 2
            verification = verify(result, p)
    except (RuntimeError, ValueError) as exc:
        security.audit("a6_insight", actor="A6", outcome="llm_unavailable", details={"error": security.redact(exc)})
        result, verification = None, {"passed": False, "failures": ["the model was unavailable"]}

    if verification["passed"]:
        insight = _normalise(result, p)
    else:
        generated_by = "template"
        insight = template_insight(p)
        verification = {**verify(insight, p), "ai_failures": verification["failures"][:10]}
    return {**insight, "band": p["band"], "mode": p["mode"], "generated_by": generated_by,
            "verification": verification, "llm_calls": llm_calls}
