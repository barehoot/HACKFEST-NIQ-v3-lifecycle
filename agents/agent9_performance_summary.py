"""
agent9_performance_summary.py

Agent 9, performance summary. When a manager or business leader prepares a
mid-year review, an annual review, a 1:1 or a promotion-readiness discussion,
this agent reads one employee's de-identified record for the period and writes
the summary they start from. The record mixes every source the portal holds:

  - the manager's reviews (rating and summary)
  - evidence: notes the manager logged, Outlook / Teams messages they tagged,
    peer recognition -- each already read by Agent 7 for context and skills
  - skills: current levels against the role, levels achieved (certified) in the
    period, and the projected learning curve
  - learning: training completed and booked, development actions done / overdue
  - for promotion: the deterministic readiness for the target role (which may be
    in another department or vertical)

  compose  -> 1 LLM call; titles, notes and review text are untrusted data
  verify   -> deterministic: every highlight / concern cites evidence refs that
              exist in the input; a suggested rating sits in the band's range;
              the promotion view repeats the rules' readiness level (never its
              own); a 1:1 has talking points; no employment-decision or
              protected-characteristic language; sizes (0 tokens)
  revise   -> once, with the failures
  fallback -> a deterministic template built from the same record, labelled

It summarises; it never rates or decides. The band is the status engine's, the
readiness level the promotion rules', the rating the manager's. Its input has
no name, id, email or manager (the portal replaces them before calling).
"""

import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENTS_DIR))

import security  # noqa: E402
from agent6_performance import _BANNED_RE, BANDS, RATING_RANGE  # noqa: E402 -- same fairness rules as Agent 6
from llm_client import call_llm_json  # noqa: E402

REVIEW_TYPES = {"mid_year": "mid-year review", "annual": "annual review", "one_on_one": "1:1 conversation",
                "promotion": "promotion-readiness discussion"}
SOURCES = ("manual", "outlook", "teams", "email_file", "peer", "integration")
PROMOTION_LEVELS = ("Ready now", "Ready in 6-12 months", "Not yet", "On hold", "Top of ladder", "No role on file")
MAX_EVIDENCE = 30
MAX_LIST = 5
MAX_ITEM = 300

SYSTEM_PROMPT = """You prepare fair, specific performance summaries for managers in a finance-operations \
organisation. You summarise only the documented record you are given -- reviews, evidence and skills data -- and \
cite the evidence you rely on by its ref. You never invent events, never decide ratings or promotions, and never \
mention protected characteristics or employment decisions. Always respond with ONLY a single JSON object, no prose, \
no markdown code fences."""


class SummaryInputError(ValueError):
    pass


def _clean(value, limit=MAX_ITEM):
    return security.sanitize_prompt_text(value, limit) if isinstance(value, str) else ""


def _int(value, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def normalise_payload(payload):
    review_type = payload.get("review_type")
    if review_type not in REVIEW_TYPES:
        raise SummaryInputError("Unknown review type.")
    band = payload.get("band")
    if band not in BANDS:
        raise SummaryInputError("Unknown band.")
    evidence = []
    for n, e in enumerate((payload.get("evidence") or [])[:MAX_EVIDENCE], start=1):
        if not isinstance(e, dict):
            continue
        evidence.append({"ref": _clean(e.get("ref"), 8) or f"E{n}", "kind": _clean(e.get("kind"), 20),
                         "sentiment": _clean(e.get("sentiment"), 10),
                         "source": e.get("source") if e.get("source") in SOURCES else "manual",
                         "date": _clean(e.get("date"), 10), "title": _clean(e.get("title"), 160),
                         "context": _clean(e.get("context"), 300),
                         "skills": [_clean(s, 80) for s in (e.get("skills") or [])[:4] if _clean(s, 80)],
                         "weight": _int(e.get("weight"), 1, 3) or 1})
    promotion = payload.get("promotion") if isinstance(payload.get("promotion"), dict) else None
    if review_type == "promotion":
        if not promotion or promotion.get("level") not in PROMOTION_LEVELS:
            raise SummaryInputError("A promotion summary needs the readiness for a target role.")
        promotion = {"target_role": _clean(promotion.get("target_role"), 120), "level": promotion["level"],
                     "skills_band": _clean(promotion.get("skills_band"), 20), "pct": _int(promotion.get("pct"), 0, 100),
                     "critical_missed": [_clean(s, 80) for s in (promotion.get("critical_missed") or [])[:8]],
                     "cross_department": bool(promotion.get("cross_department")), "reason": _clean(promotion.get("reason"), 300)}
    learning = payload.get("learning") if isinstance(payload.get("learning"), dict) else {}
    return {
        "review_type": review_type, "band": band, "score": _int(payload.get("score"), 0, 100),
        "window": {k: _clean((payload.get("window") or {}).get(k), 10) for k in ("from", "to")},
        "role_name": _clean(payload.get("role_name"), 120) or "their role",
        "factors": [{"name": _clean(f.get("name"), 60), "score": _int(f.get("score"), 0, 100), "detail": _clean(f.get("detail"), 160)}
                    for f in (payload.get("factors") or [])[:6] if isinstance(f, dict)],
        "reviews": [{"period": _clean(r.get("period"), 10), "rating": _int(r.get("rating"), 1, 5), "summary": _clean(r.get("summary"), 400)}
                    for r in (payload.get("reviews") or [])[:4] if isinstance(r, dict)],
        "evidence": evidence,
        "skills": [{"skill": _clean(s.get("skill"), 80), "current": _int(s.get("current"), 0, 5), "target": _int(s.get("target"), 0, 5),
                    "critical": bool(s.get("critical")), "validated": bool(s.get("validated"))}
                   for s in (payload.get("skills") or [])[:15] if isinstance(s, dict)],
        "skills_achieved": [{"skill": _clean(s.get("skill"), 80), "level": _int(s.get("level"), 0, 5),
                             "previous": _int(s.get("previous"), 0, 5), "date": _clean(s.get("date"), 10)}
                            for s in (payload.get("skills_achieved") or [])[:10] if isinstance(s, dict)],
        "learning": {"completed": [_clean(t, 160) for t in (learning.get("completed") or [])[:8]],
                     "booked": _int(learning.get("booked"), 0, 999) or 0, "actions_done": _int(learning.get("actions_done"), 0, 999) or 0,
                     "actions_open": _int(learning.get("actions_open"), 0, 999) or 0,
                     "actions_overdue": _int(learning.get("actions_overdue"), 0, 999) or 0,
                     "curve": {k: (learning.get("curve") or {}).get(k) for k in ("today_avg", "projected_avg", "target_avg", "weeks")}},
        "promotion": promotion,
        "open_actions": [_clean(a, 160) for a in (payload.get("open_actions") or [])[:6]],
    }


def _record(p):
    lines = [f"Role: {p['role_name']}. Status band (rules, not yours to change): {p['band']}"
             + (f", score {p['score']}/100" if p["score"] is not None else "") + f". Period: {p['window']['from']} to {p['window']['to']}."]
    for f in p["factors"]:
        lines.append(f"Factor {f['name']}: {f['score']}/100 ({f['detail']})")
    for r in p["reviews"]:
        lines.append(f"Manager review {r['period']}: rating {r['rating'] or '—'}/5. {r['summary']}")
    for e in p["evidence"]:
        lines.append(f"[{e['ref']}] {e['date']} {e['kind']} ({e['sentiment']}, source {e['source']}, weight {e['weight']}): "
                     f"{e['title']}. {e['context']}" + (f" Skills: {', '.join(e['skills'])}." if e["skills"] else ""))
    for s in p["skills"]:
        lines.append(f"Skill {s['skill']}: L{s['current']} vs target L{s['target']}" + (" (critical)" if s["critical"] else "")
                     + (" validated" if s["validated"] else ""))
    for s in p["skills_achieved"]:
        lines.append(f"Achieved {s['skill']} L{s['level']}" + (f" (from L{s['previous']})" if s["previous"] is not None else "") + f" on {s['date']}")
    learning = p["learning"]
    lines.append(f"Learning: completed {', '.join(learning['completed']) or 'none'}; {learning['booked']} session(s) booked; "
                 f"development actions {learning['actions_done']} done, {learning['actions_open']} open, {learning['actions_overdue']} overdue.")
    curve = learning["curve"]
    if curve.get("today_avg") is not None:
        lines.append(f"Learning curve: average L{curve['today_avg']} today, projected L{curve['projected_avg']} in {curve['weeks']} weeks "
                     f"(role target L{curve['target_avg']}).")
    if p["open_actions"]:
        lines.append("Open actions: " + "; ".join(p["open_actions"]))
    if p["promotion"]:
        pr = p["promotion"]
        lines.append(f"Promotion target: {pr['target_role']}" + (" (another department / vertical)" if pr["cross_department"] else "")
                     + f". Rules readiness: {pr['level']} ({pr['pct']}% of skills at target; skills band {pr['skills_band']}"
                     + (f"; critical gaps: {', '.join(pr['critical_missed'])}" if pr["critical_missed"] else "") + f"). {pr['reason']}")
    return "\n".join(lines)


def _build_prompt(p, feedback=None):
    kind = REVIEW_TYPES[p["review_type"]]
    extra = {
        "mid_year": f'- suggested_rating: 1-5 within {RATING_RANGE[p["band"]]} for the "{p["band"]}" band; the manager decides.',
        "annual": f'- suggested_rating: 1-5 within {RATING_RANGE[p["band"]]} for the "{p["band"]}" band; the manager decides.',
        "one_on_one": "- talking_points: 2-5 specific questions or topics for the 1:1 (open actions, recent evidence, learning).",
        "promotion": f'- promotion_view: {{"recommendation": "{(p["promotion"] or {}).get("level")}" (copy exactly), "rationale": "..."}}.',
    }[p["review_type"]]
    fb = ""
    if feedback:
        fb = "\n\nYour previous answer failed these checks -- fix every one:\n" + "\n".join(f"- {f}" for f in feedback)
    return f"""Write the summary a manager starts from for a {kind}.

{security.wrap_untrusted("employee_record", _record(p), 9000)}

Rules:
- headline: one sentence; overall: 2-4 sentences on the period, balanced.
- highlights and concerns: up to {MAX_LIST} each, each {{"text": "...", "refs": ["E1"]}} citing only refs from the record.
- evidence_mix: one sentence on what the record rests on (manager notes, tagged email / Teams, peer recognition, reviews).
- skills_growth: up to {MAX_LIST} points on skills achieved and the gap to the role target.
- learning: up to {MAX_LIST} points on training, development actions and the learning curve.
- development_focus: up to {MAX_LIST} next-period focus areas.
{extra}
- Refer to "the employee"; never invent facts outside the record.{fb}

Respond with ONLY this JSON object:
{{"headline": "...", "overall": "...", "highlights": [{{"text": "...", "refs": ["E1"]}}], "concerns": [], "evidence_mix": "...", "skills_growth": [], "learning": [], "development_focus": [], "talking_points": [], "suggested_rating": null, "promotion_view": null}}"""


def _strings(value, limit=MAX_LIST, chars=MAX_ITEM):
    return [str(v).strip()[:chars] for v in (value or []) if isinstance(v, str) and str(v).strip()][:limit]


def _cited(value):
    out = []
    for item in (value or [])[:MAX_LIST]:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            out.append({"text": str(item["text"]).strip()[:MAX_ITEM],
                        "refs": [str(r) for r in (item.get("refs") or []) if isinstance(r, str)][:6]})
    return out


def verify(result, p):
    failures = []
    if not isinstance(result, dict):
        return {"passed": False, "failures": ["response is not an object"]}
    if not str(result.get("headline") or "").strip() or not str(result.get("overall") or "").strip():
        failures.append("headline and overall are required")
    refs = {e["ref"] for e in p["evidence"]}
    for key in ("highlights", "concerns"):
        for item in _cited(result.get(key)):
            bad = [r for r in item["refs"] if r not in refs]
            if bad:
                failures.append(f"{key} cite unknown refs: {', '.join(bad[:3])}")
            if refs and not item["refs"]:
                failures.append(f"every {key[:-1]} must cite at least one evidence ref")
    rating = result.get("suggested_rating")
    if p["review_type"] in ("mid_year", "annual"):
        low, high = RATING_RANGE[p["band"]]
        if not isinstance(rating, int) or isinstance(rating, bool) or not low <= rating <= high:
            failures.append(f"suggested_rating must be an integer from {low} to {high} for the {p['band']} band")
    if p["review_type"] == "one_on_one" and len(_strings(result.get("talking_points"))) < 2:
        failures.append("a 1:1 summary needs at least two talking points")
    if p["review_type"] == "promotion":
        view = result.get("promotion_view") if isinstance(result.get("promotion_view"), dict) else {}
        if view.get("recommendation") != p["promotion"]["level"]:
            failures.append(f"promotion_view.recommendation must be exactly \"{p['promotion']['level']}\" (the rules decide it)")
    text = " ".join(str(v) for v in result.values() if isinstance(v, (str, list, dict)))
    if _BANNED_RE.search(text):
        failures.append("remove employment-decision or protected-characteristic language")
    return {"passed": not failures, "failures": failures[:10]}


def _normalise(result, p):
    out = {
        "headline": str(result.get("headline") or "").strip()[:200], "overall": str(result.get("overall") or "").strip()[:900],
        "highlights": _cited(result.get("highlights")), "concerns": _cited(result.get("concerns")),
        "evidence_mix": str(result.get("evidence_mix") or "").strip()[:MAX_ITEM],
        "skills_growth": _strings(result.get("skills_growth")), "learning": _strings(result.get("learning")),
        "development_focus": _strings(result.get("development_focus")), "talking_points": _strings(result.get("talking_points")),
        "suggested_rating": None, "promotion_view": None,
    }
    if p["review_type"] in ("mid_year", "annual"):
        out["suggested_rating"] = result.get("suggested_rating")
    if p["review_type"] == "promotion":
        out["promotion_view"] = {"recommendation": p["promotion"]["level"],
                                 "rationale": str((result.get("promotion_view") or {}).get("rationale") or "").strip()[:500]}
    return out


def template_summary(p):
    """Deterministic summary from the same record, used when the model is unavailable or unverifiable."""
    positive = [e for e in p["evidence"] if e["sentiment"] == "positive"]
    negative = [e for e in p["evidence"] if e["sentiment"] == "negative"]
    by_source = {}
    for e in p["evidence"]:
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
    labels = {"manual": "manager note", "outlook": "tagged email", "email_file": "tagged email", "teams": "Teams conversation",
              "peer": "peer recognition", "integration": "integration"}
    mix = ", ".join(f"{n} {labels[s]}{'s' if n != 1 else ''}" for s, n in sorted(by_source.items(), key=lambda kv: -kv[1]))
    rating_low, rating_high = RATING_RANGE[p["band"]]
    learning = p["learning"]
    curve = learning["curve"]
    out = {
        "headline": f"{p['band']} for the period in the {p['role_name']} role.",
        "overall": (f"The status engine places the employee at {p['band'].lower()}"
                    + (f" (score {p['score']}/100)" if p["score"] is not None else "") + f" for {p['window']['from']} to {p['window']['to']}. "
                    f"The record holds {len(positive)} positive and {len(negative)} negative evidence item(s)"
                    + (f" and {len(p['reviews'])} manager review(s)." if p["reviews"] else ".")),
        "highlights": [{"text": e["title"], "refs": [e["ref"]]} for e in sorted(positive, key=lambda e: -e["weight"])[:3]],
        "concerns": [{"text": e["title"], "refs": [e["ref"]]} for e in sorted(negative, key=lambda e: -e["weight"])[:3]],
        "evidence_mix": f"Based on {mix or 'no evidence items'}" + (f" and {len(p['reviews'])} review(s)." if p["reviews"] else "."),
        "skills_growth": [f"{s['skill']} certified at L{s['level']}" + (f" (from L{s['previous']})" if s["previous"] is not None else "")
                          for s in p["skills_achieved"][:3]]
                         + [f"{s['skill']}: L{s['current']} of L{s['target']} target" for s in p["skills"]
                            if s["current"] is not None and s["target"] and s["current"] < s["target"]][:2],
        "learning": ([f"Completed: {', '.join(learning['completed'][:3])}"] if learning["completed"] else [])
                    + [f"Development actions: {learning['actions_done']} done, {learning['actions_open']} open, {learning['actions_overdue']} overdue"]
                    + ([f"Projected average L{curve['projected_avg']} in {curve['weeks']} weeks (target L{curve['target_avg']})"]
                       if curve.get("projected_avg") is not None else []),
        "development_focus": [f"Close the {s['skill']} gap" for s in p["skills"]
                              if s["critical"] and s["current"] is not None and s["target"] and s["current"] < s["target"]][:3],
        "talking_points": [], "suggested_rating": None, "promotion_view": None,
    }
    if p["review_type"] == "one_on_one":
        out["talking_points"] = ([f"Progress on: {a}" for a in p["open_actions"][:3]]
                                 + [f"Discuss: {e['title']}" for e in (negative or positive)[:2]]
                                 or ["How is the workload?", "What support do you need this month?"])[:5]
        if len(out["talking_points"]) < 2:
            out["talking_points"].append("What support do you need before the next 1:1?")
    if p["review_type"] in ("mid_year", "annual"):
        out["suggested_rating"] = round((rating_low + rating_high) / 2) if p["band"] != "Not enough data" else 3
    if p["review_type"] == "promotion":
        pr = p["promotion"]
        out["promotion_view"] = {"recommendation": pr["level"],
                                 "rationale": f"{pr['pct']}% of {pr['target_role']} skills at target" +
                                              (f"; critical gaps: {', '.join(pr['critical_missed'])}" if pr["critical_missed"] else "") + "."}
    return out


def build_summary(payload):
    """Returns the summary plus {"generated_by", "llm_calls", "injection_flags", "review_type"}."""
    p = normalise_payload(payload or {})
    flags = security.detect_injection(_record(p))
    if flags:
        security.audit("prompt_injection_suspected", actor="A9", outcome="flagged", stream="llm",
                       resource="performance_summary", details={"indicators": flags})
    generated_by, llm_calls, result = "agent", 0, None
    try:
        result = call_llm_json(SYSTEM_PROMPT, _build_prompt(p), agent="A9")
        llm_calls = 1
        check = verify(result, p)
        if not check["passed"]:
            result = call_llm_json(SYSTEM_PROMPT, _build_prompt(p, check["failures"]), agent="A9")
            llm_calls = 2
            check = verify(result, p)
    except (RuntimeError, ValueError) as exc:
        security.audit("a9_summary", actor="A9", outcome="llm_unavailable", details={"error": security.redact(exc)})
        check = {"passed": False}
    summary = _normalise(result, p) if check["passed"] else template_summary(p)
    if not check["passed"]:
        generated_by = "template"
    return {**summary, "review_type": p["review_type"], "generated_by": generated_by, "llm_calls": llm_calls,
            "injection_flags": flags}
