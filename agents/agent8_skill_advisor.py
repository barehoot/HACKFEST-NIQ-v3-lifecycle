"""
agent8_skill_advisor.py

Agent 8, skill advisor. Two jobs, both suggestions only:

  role       a new or existing job profile -> the skills it should require
             ("pitched" from the live skill catalogue first, new skills only
             where the catalogue has nothing close), each with a target level
             1-5, criticality, the behaviour expected at that level and why
  catalogue  a vertical -> new skills worth adding to the skill catalogue
             (emerging tools, regulation, ways of working), each with L1-L5
             behaviour indicators
  resume     an employee's CV + the skills that matter to them (current role and
             career aspirations) -> a claimed level per skill, each backed by a
             quote that must appear word for word in the CV. Only the claims
             leave this module; the CV text is never returned or stored

  compose  -> 1 LLM call; the role text, trends and focus are untrusted data
              (delimited, sanitised)
  verify   -> deterministic: names 2-80 chars, no duplicates, a catalogue skill
              is flagged existing (and a new one isn't), target 1-5, a concrete
              behaviour indicator, at most 3 critical skills, catalogue mode never
              re-proposes a skill that already exists (0 tokens)
  revise   -> once, with the failures
  fallback -> rules: catalogue skills ranked by word overlap with the role text,
              plus the role's own plain-words skills; for the catalogue, logged
              market trends then a finance-operations watchlist. Labelled "rules"

Nothing changes until a person accepts a suggestion in the portal: an admin, HR
partner or leader applies it to the job profile (a manager's acceptance becomes a
proposal), or adds it to the catalogue.
"""

import re
import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENTS_DIR))

import security  # noqa: E402
from llm_client import call_llm_json  # noqa: E402

MODES = ("role", "catalogue")
MAX_ROLE_ITEMS = 10
MAX_CATALOGUE_ITEMS = 6
MAX_CRITICAL = 3
MAX_TEXT = 2500
CAPABILITIES = ("Technical Expertise", "Process Excellence", "Digital & Automation", "Controls & Compliance",
                "Stakeholder Management", "Leadership", "Analytics")

SYSTEM_PROMPT = """You are a skills architect for a finance-operations shared-services organisation. You design \
the skill requirements of job profiles and keep the skill catalogue current with the market. Prefer skills that \
already exist in the catalogue; propose a new skill only when nothing in the catalogue covers it. Behaviour \
indicators describe observable work at the stated proficiency level (1 awareness ... 5 strategic authority). \
Always respond with ONLY a single JSON object, no prose, no markdown code fences."""

_GRADE_TARGET = {"B1": 2, "B2": 2, "B3": 3, "B4": 3, "P1": 3, "P2": 3, "P3": 4, "P4": 4, "P5": 5, "M1": 4, "M2": 5}
_STOP = {"and", "the", "for", "with", "from", "into", "that", "this", "work", "role", "team", "job", "management",
         "processing", "process", "skills", "skill", "level", "using", "including", "across", "ensure", "ensures"}
_WORD_RE = re.compile(r"[a-z0-9&+#]{3,}")

# Rules fallback for the catalogue when no trend is logged: skills finance-operations
# teams are being asked for, each with a short market reason.
WATCHLIST = (
    ("AI-Assisted Reconciliation", "Digital & Automation",
     "Matching engines and GenAI copilots are moving reconciliations from manual ticking to exception review."),
    ("Intelligent Document Processing", "Digital & Automation",
     "OCR/ML invoice and remittance capture is replacing keying; teams tune and supervise the models."),
    ("E-Invoicing & Tax Compliance", "Controls & Compliance",
     "Mandatory e-invoicing (EU ViDA, India IRN, LATAM CFDI) changes how invoices are issued and validated."),
    ("Continuous Controls Monitoring", "Controls & Compliance",
     "Auditors expect automated, always-on control testing instead of quarterly samples."),
    ("Process Mining", "Analytics",
     "Event-log analytics (Celonis-style) pinpoint where P2P and O2C cycles leak time and cash."),
    ("Low-Code Automation (Power Automate / RPA)", "Digital & Automation",
     "Citizen-developer automation is now expected of senior associates for repetitive close tasks."),
    ("Data Storytelling & Visualisation", "Analytics",
     "Stakeholders want dashboards and narratives, not static extracts."),
    ("Prompt Engineering for Finance", "Digital & Automation",
     "Safe, auditable use of GenAI for variance commentary, queries and SOP drafting."),
)


class SkillAdviceInputError(ValueError):
    pass


def _clean(value, limit):
    return security.sanitize_prompt_text(value, limit) if isinstance(value, str) else ""


def _words(text):
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP}


def _grade_target(grade):
    levels = [_GRADE_TARGET.get(g.strip().upper(), 3) for g in re.split(r"[/,\s]+", grade or "") if g.strip()]
    return max(levels) if levels else 3


def _generic_indicators(skill):
    return [
        f"Aware of {skill}; follows guidance and documented steps with supervision.",
        f"Applies {skill} to routine work; flags issues and asks for help on exceptions.",
        f"Applies {skill} independently to complex cases; improves the team's approach.",
        f"Go-to expert in {skill}; designs standards, reviews others' work and coaches the team.",
        f"Sets the organisation's direction for {skill}; drives adoption and outcomes at scale.",
    ]


def _plain_skills(skills_text):
    return [s.strip(" .;") for s in re.split(r"[,;\n]+", skills_text or "") if 2 <= len(s.strip(" .;")) <= 80][:12]


# ---------------------------------------------------------------------------
# Rules fallback
# ---------------------------------------------------------------------------

def rules_role(payload):
    role_text = " ".join(payload[k] for k in ("role_name", "summary", "responsibilities", "skills_text"))
    role_words = _words(role_text)
    target = _grade_target(payload["grade"])
    current = {s.lower() for s in payload["current"]}
    scored = []
    for item in payload["catalogue"]:
        if item["skill"].lower() in current:
            continue
        overlap = len(_words(item["skill"]) & role_words)
        if overlap:
            scored.append((overlap, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["skill"]))
    out = []
    for rank, (overlap, item) in enumerate(scored[:MAX_ROLE_ITEMS - 2]):
        out.append({"skill": item["skill"], "existing": True, "capability": item.get("capability") or "Technical Expertise",
                    "target_level": target, "is_critical": rank < 2,
                    "behavior_indicator": "", "rationale": f"The role description mentions {overlap} of its key terms."})
    known = current | {c["skill"].lower() for c in payload["catalogue"]}
    for name in _plain_skills(payload["skills_text"]):
        if len(out) >= MAX_ROLE_ITEMS:
            break
        if name.lower() in known:
            continue
        known.add(name.lower())
        out.append({"skill": name[:80], "existing": False, "capability": "Technical Expertise", "target_level": target,
                    "is_critical": False,
                    "behavior_indicator": f"At L{target}, applies {name} to the role's day-to-day work independently and explains the result.",
                    "rationale": "Listed in the role's own plain-words skills."})
    return {"summary": "Matched from the catalogue by keywords (the AI was unavailable).", "skills": out}


def rules_catalogue(payload):
    existing = {c["skill"].lower() for c in payload["catalogue"]}
    out = []
    for trend in payload["trends"]:
        name = trend["skill"][:80]
        if trend.get("direction") in ("emerging", "rising") and name.lower() not in existing and len(out) < MAX_CATALOGUE_ITEMS:
            existing.add(name.lower())
            out.append({"skill": name, "capability": "Digital & Automation", "direction": trend["direction"],
                        "rationale": (trend.get("note") or "Logged as a market trend.")[:300],
                        "indicators": _generic_indicators(name)})
    for name, capability, why in WATCHLIST:
        if len(out) >= MAX_CATALOGUE_ITEMS:
            break
        if name.lower() not in existing:
            existing.add(name.lower())
            out.append({"skill": name, "capability": capability, "direction": "emerging", "rationale": why,
                        "indicators": _generic_indicators(name)})
    return {"summary": "From logged trends and the finance-operations watchlist (the AI was unavailable).", "skills": out}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _feedback(failures):
    if not failures:
        return ""
    return "\n\nYour previous answer failed these checks -- fix every one:\n" + "\n".join(f"- {f}" for f in failures)


CATALOGUE_PROMPT_CHARS = 8000  # keeps the prompt under the client's MAX_PROMPT_CHARS with a 400-skill catalogue


def _catalogue_lines(catalogue, with_capability):
    """Catalogue skills for the prompt, in the caller's order (own vertical first), within a size budget."""
    lines, used = [], 0
    for c in catalogue:
        line = f"- {c['skill']} [{c.get('capability') or '—'}]" if with_capability else f"- {c['skill']}"
        if used + len(line) + 1 > CATALOGUE_PROMPT_CHARS:
            lines.append(f"- … and {len(catalogue) - len(lines)} more")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) or "(empty)"


def _trend_lines(trends):
    return "\n".join(f"- {t['skill']} ({t['direction']}): {t.get('note', '')}" for t in trends) or "(none logged)"


def _role_prompt(payload, failures=None):
    catalogue = _catalogue_lines(payload["catalogue"], with_capability=True)
    role = (f"Job title: {payload['role_name']}\nVertical: {payload['tower']}\nGrade: {payload['grade']}\n"
            f"Summary: {payload['summary']}\nResponsibilities: {payload['responsibilities']}\n"
            f"Skills in plain words: {payload['skills_text']}")
    return f"""Recommend the skills this job profile should require.

{security.wrap_untrusted("job_profile", role, MAX_TEXT)}

Skills it already requires (do not repeat): {", ".join(payload["current"]) or "(none yet)"}

Skill catalogue (reuse these names exactly when one fits):
{catalogue}

Market trends logged by the organisation:
{security.wrap_untrusted("trends", _trend_lines(payload["trends"]), 1500)}

Rules:
- {payload["max_items"]} skills at most; reuse catalogue skills first (existing: true, exact name).
- A new skill (existing: false) only for something the catalogue does not cover; name it in 2-6 words.
- target_level 1-5 fits the grade (B3 ~ 3, B4 ~ 3-4, P3/P4 ~ 4).
- is_critical: true for at most {MAX_CRITICAL} skills the job cannot be done without.
- behavior_indicator: one concrete sentence (20-400 characters) of observable work at the target level in THIS job.
- capability: a short family such as {", ".join(CAPABILITIES[:5])}.
- rationale: one sentence on why this job needs it (cite a trend when one applies).{_feedback(failures)}

Respond with ONLY this JSON object:
{{"summary": "...", "skills": [{{"skill": "...", "existing": true, "capability": "...", "target_level": 3, "is_critical": false, "behavior_indicator": "...", "rationale": "..."}}]}}"""


def _catalogue_prompt(payload, failures=None):
    catalogue = _catalogue_lines(payload["catalogue"], with_capability=False)
    return f"""Recommend new skills to add to the skill catalogue of the {payload['tower'] or 'whole'} vertical.

Skills already in the catalogue (never propose these or a near-duplicate):
{catalogue}

Market trends logged by the organisation:
{security.wrap_untrusted("trends", _trend_lines(payload["trends"]), 1500)}

What the requester wants to focus on:
{security.wrap_untrusted("focus", payload["focus"] or "(no specific focus)", 500)}

Rules:
- {payload["max_items"]} skills at most, each named in 2-6 words, genuinely new to the catalogue.
- direction: emerging or rising.
- rationale: one sentence on what is changing in the market.
- indicators: exactly 5 behaviour indicators, L1 (awareness) to L5 (strategic authority), each 15-300 characters.{_feedback(failures)}

Respond with ONLY this JSON object:
{{"summary": "...", "skills": [{{"skill": "...", "capability": "...", "direction": "emerging", "rationale": "...", "indicators": ["L1 ...", "L2 ...", "L3 ...", "L4 ...", "L5 ..."]}}]}}"""


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _name_ok(name):
    return isinstance(name, str) and 2 <= len(name.strip()) <= 80 and "@" not in name


def verify_role(result, payload):
    failures = []
    if not isinstance(result, dict) or not isinstance(result.get("skills"), list):
        return {"passed": False, "failures": ["response must be an object with a skills list"]}
    skills = result["skills"]
    if not skills:
        failures.append("recommend at least one skill")
    if len(skills) > payload["max_items"]:
        failures.append(f"at most {payload['max_items']} skills")
    catalogue = {c["skill"].lower() for c in payload["catalogue"]}
    current = {s.lower() for s in payload["current"]}
    seen = set()
    critical = 0
    for item in skills:
        if not isinstance(item, dict) or not _name_ok(item.get("skill")):
            failures.append("every skill needs a 2-80 character name")
            continue
        name = item["skill"].strip().lower()
        if name in seen:
            failures.append(f"'{item['skill']}' is listed twice")
        seen.add(name)
        if name in current:
            failures.append(f"'{item['skill']}' is already required")
        if bool(item.get("existing")) != (name in catalogue):
            failures.append(f"'{item['skill']}': existing must be {'true' if name in catalogue else 'false'}")
        level = item.get("target_level")
        if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 5:
            failures.append(f"'{item['skill']}': target_level must be an integer 1-5")
        indicator = str(item.get("behavior_indicator") or "").strip()
        if not 20 <= len(indicator) <= 400:
            failures.append(f"'{item['skill']}': behavior_indicator must be 20-400 characters")
        critical += bool(item.get("is_critical"))
    if critical > MAX_CRITICAL:
        failures.insert(0, f"at most {MAX_CRITICAL} critical skills")
    return {"passed": not failures, "failures": failures[:10]}


def verify_catalogue(result, payload):
    failures = []
    if not isinstance(result, dict) or not isinstance(result.get("skills"), list):
        return {"passed": False, "failures": ["response must be an object with a skills list"]}
    skills = result["skills"]
    if not skills:
        failures.append("recommend at least one skill")
    if len(skills) > payload["max_items"]:
        failures.append(f"at most {payload['max_items']} skills")
    existing = {c["skill"].lower() for c in payload["catalogue"]}
    seen = set()
    for item in skills:
        if not isinstance(item, dict) or not _name_ok(item.get("skill")):
            failures.append("every skill needs a 2-80 character name")
            continue
        name = item["skill"].strip().lower()
        if name in existing:
            failures.append(f"'{item['skill']}' is already in the catalogue")
        if name in seen:
            failures.append(f"'{item['skill']}' is listed twice")
        seen.add(name)
        indicators = item.get("indicators")
        if not isinstance(indicators, list) or len(indicators) != 5 or not all(
                isinstance(i, str) and 15 <= len(i.strip()) <= 300 for i in indicators):
            failures.append(f"'{item['skill']}': indicators must be 5 strings of 15-300 characters (L1-L5)")
        if item.get("direction") not in ("emerging", "rising"):
            failures.append(f"'{item['skill']}': direction must be emerging or rising")
    return {"passed": not failures, "failures": failures[:10]}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _normalise_payload(payload):
    mode = payload.get("mode")
    if mode not in MODES:
        raise SkillAdviceInputError("Unknown mode.")
    catalogue = []
    for item in (payload.get("catalogue") or [])[:400]:
        name = _clean((item or {}).get("skill"), 80)
        if name:
            catalogue.append({"skill": name, "capability": _clean(item.get("capability"), 60)})
    trends = [{"skill": _clean(t.get("skill"), 80), "direction": t.get("direction") if t.get("direction") in
               ("emerging", "rising", "stable", "declining") else "stable", "note": _clean(t.get("note"), 300)}
              for t in (payload.get("trends") or [])[:15] if isinstance(t, dict) and _clean(t.get("skill"), 80)]
    clean = {"mode": mode, "tower": _clean(payload.get("tower"), 20), "catalogue": catalogue, "trends": trends,
             "max_items": MAX_ROLE_ITEMS if mode == "role" else MAX_CATALOGUE_ITEMS}
    try:
        clean["max_items"] = max(1, min(clean["max_items"], int(payload.get("max_items") or clean["max_items"])))
    except (TypeError, ValueError):
        pass
    if mode == "role":
        clean.update(role_name=_clean(payload.get("role_name"), 120), grade=_clean(payload.get("grade"), 20),
                     summary=_clean(payload.get("summary"), 1000), responsibilities=_clean(payload.get("responsibilities"), 1500),
                     skills_text=_clean(payload.get("skills_text"), 800),
                     current=[_clean(s, 80) for s in (payload.get("current") or [])[:40] if _clean(s, 80)])
        if len(clean["role_name"]) < 3:
            raise SkillAdviceInputError("The job profile needs a title.")
    else:
        clean["focus"] = _clean(payload.get("focus"), 500)
    return clean


def _finish_role(result, payload):
    canonical = {c["skill"].lower(): c for c in payload["catalogue"]}
    out = []
    for item in result["skills"][:payload["max_items"]]:
        name = item["skill"].strip()
        match = canonical.get(name.lower())
        level = item.get("target_level") if isinstance(item.get("target_level"), int) else _grade_target(payload["grade"])
        out.append({
            "skill": match["skill"] if match else name[:80], "existing": bool(match),
            "capability": _clean(item.get("capability"), 60) or (match or {}).get("capability") or "Technical Expertise",
            "target_level": max(1, min(5, level)), "is_critical": bool(item.get("is_critical")),
            "behavior_indicator": str(item.get("behavior_indicator") or "").strip()[:400],
            "rationale": str(item.get("rationale") or "").strip()[:300],
        })
    return out


def _finish_catalogue(result, payload):
    out = []
    for item in result["skills"][:payload["max_items"]]:
        name = item["skill"].strip()[:80]
        indicators = [str(i).strip()[:300] for i in (item.get("indicators") or [])][:5]
        out.append({"skill": name, "capability": _clean(item.get("capability"), 60) or "Digital & Automation",
                    "direction": item.get("direction") if item.get("direction") in ("emerging", "rising") else "emerging",
                    "rationale": str(item.get("rationale") or "").strip()[:300],
                    "indicators": indicators if len(indicators) == 5 else _generic_indicators(name)})
    return out


def advise(payload):
    """Returns {"mode", "summary", "skills", "generated_by", "llm_calls", "injection_flags"}."""
    payload = _normalise_payload(payload or {})
    mode = payload["mode"]
    untrusted = " ".join([payload.get("summary", ""), payload.get("responsibilities", ""), payload.get("skills_text", ""),
                          payload.get("focus", ""), " ".join(t["note"] for t in payload["trends"])])
    flags = security.detect_injection(untrusted)
    if flags:
        security.audit("prompt_injection_suspected", actor="A8", outcome="flagged", stream="llm",
                       resource=f"skill_advice_{mode}", details={"indicators": flags})
    build, check = (_role_prompt, verify_role) if mode == "role" else (_catalogue_prompt, verify_catalogue)

    generated_by, llm_calls, result, verification = "agent", 0, None, None
    try:
        result = call_llm_json(SYSTEM_PROMPT, build(payload), agent="A8")
        llm_calls = 1
        verification = check(result, payload)
        if not verification["passed"]:
            result = call_llm_json(SYSTEM_PROMPT, build(payload, verification["failures"]), agent="A8")
            llm_calls = 2
            verification = check(result, payload)
    except (RuntimeError, ValueError) as exc:
        security.audit("a8_skill_advice", actor="A8", outcome="llm_unavailable", details={"error": security.redact(exc)})
        verification = {"passed": False, "failures": ["the model was unavailable"]}

    if not verification["passed"]:
        generated_by = "rules"
        result = rules_role(payload) if mode == "role" else rules_catalogue(payload)
        skills = result["skills"]
    else:
        skills = _finish_role(result, payload) if mode == "role" else _finish_catalogue(result, payload)
    return {"mode": mode, "summary": str(result.get("summary") or "").strip()[:400], "skills": skills,
            "generated_by": generated_by, "llm_calls": llm_calls, "injection_flags": flags}


# ---------------------------------------------------------------------------
# CV evidence: claimed skill levels backed by verbatim quotes
# ---------------------------------------------------------------------------

MAX_RESUME_SKILLS = 40
MAX_QUOTE = 160
RESUME_PROMPT_CHARS = 15000  # the CV share of the prompt, under the client's MAX_PROMPT_CHARS
RESUME_SYSTEM_PROMPT = """You read a CV to find evidence of specific skills. You report only what the CV itself says, quoting it exactly. A claimed level is the CV holder's own claim, not a validated level. Always respond with ONLY a single JSON object, no prose, no markdown code fences."""


def _norm(text):
    return " ".join(str(text or "").lower().split())


def _resume_prompt(text, skills, failures=None):
    return f"""For each skill below, decide whether the CV shows evidence of it.

Skills (use these exact names): {", ".join(skills)}

{security.wrap_untrusted("cv", text, RESUME_PROMPT_CHARS)}

Rules:
- Only skills the CV gives evidence for; leave the others out.
- level: the claimed proficiency 1-5 (1 awareness, 2 working knowledge, 3 independent, 4 advanced / leads others,
  5 expert / sets direction), judged from what the CV describes (years, scope, outcomes).
- quote: up to {MAX_QUOTE} characters copied EXACTLY from the CV that supports the claim.{_feedback(failures)}

Respond with ONLY this JSON object:
{{"claims": [{{"skill": "...", "level": 3, "quote": "..."}}]}}"""


def verify_resume(result, text, skills):
    failures = []
    if not isinstance(result, dict) or not isinstance(result.get("claims"), list):
        return {"passed": False, "failures": ["response must be an object with a claims list"]}
    allowed = {s.lower() for s in skills}
    haystack, seen = _norm(text), set()
    for claim in result["claims"]:
        if not isinstance(claim, dict) or not isinstance(claim.get("skill"), str):
            failures.append("every claim needs a skill name")
            continue
        name = claim["skill"].strip().lower()
        if name not in allowed:
            failures.append(f"'{claim['skill']}' is not one of the listed skills")
        if name in seen:
            failures.append(f"'{claim['skill']}' is claimed twice")
        seen.add(name)
        level = claim.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 5:
            failures.append(f"'{claim['skill']}': level must be an integer 1-5")
        quote = _norm(claim.get("quote"))
        if not quote or len(quote) > MAX_QUOTE or quote not in haystack:
            failures.append(f"'{claim['skill']}': the quote must be copied exactly from the CV (max {MAX_QUOTE} characters)")
    return {"passed": not failures, "failures": failures[:10]}


_GENERIC_CV_WORDS = {"data", "basic", "advanced", "tools", "skills", "work"}  # too common in CVs to prove a skill alone


def _sentence_with(text, needle):
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        if needle in sentence.lower():
            return " ".join(sentence.split()).lstrip("-•*· ")[:MAX_QUOTE]
    return needle


def rules_resume(text, skills):
    """A skill whose full name, an acronym in it (SQL, BI, VBA), or at least half of its distinctive
    words appears in the CV: claimed L2, quoting the sentence it appears in."""
    low = text.lower()
    claims = []
    for skill in skills:
        acronyms = [a.lower() for a in re.findall(r"\b[A-Z]{2,5}\b", skill)]
        words = [w for w in _words(skill) if len(w) >= 4 and w not in _GENERIC_CV_WORDS]
        found = [w for w in words if re.search(rf"\b{re.escape(w)}\b", low)]
        hit = next((a for a in acronyms if re.search(rf"\b{re.escape(a)}\b", low)), None)
        if skill.lower() in low:
            needle = skill.lower()
        elif hit:
            needle = hit
        elif words and len(found) * 2 >= len(words):
            needle = max(found, key=len)  # the most distinctive word picks the sentence to quote
        else:
            continue
        claims.append({"skill": skill, "level": 2, "quote": _sentence_with(text, needle)})
    return claims


def assess_resume(text, skills):
    """Returns {"claims": [{"skill", "level", "quote"}], "generated_by", "llm_calls", "injection_flags"}."""
    text = security.sanitize_prompt_text(text, RESUME_PROMPT_CHARS)  # quotes are checked against what the model saw
    if len(text) < 40:
        raise SkillAdviceInputError("The CV contained no usable text.")
    skills = list(dict.fromkeys(_clean(s, 80) for s in (skills or []) if _clean(s, 80)))[:MAX_RESUME_SKILLS]
    if not skills:
        raise SkillAdviceInputError("No skills to look for.")
    flags = security.detect_injection(text)
    if flags:
        security.audit("prompt_injection_suspected", actor="A8", outcome="flagged", stream="llm",
                       resource="resume_skills", details={"indicators": flags})
    generated_by, llm_calls, check, result = "agent", 0, {"passed": False}, None
    try:
        result = call_llm_json(RESUME_SYSTEM_PROMPT, _resume_prompt(text, skills), agent="A8")
        llm_calls = 1
        check = verify_resume(result, text, skills)
        if not check["passed"]:
            result = call_llm_json(RESUME_SYSTEM_PROMPT, _resume_prompt(text, skills, check["failures"]), agent="A8")
            llm_calls = 2
            check = verify_resume(result, text, skills)
    except (RuntimeError, ValueError) as exc:
        security.audit("a8_resume_skills", actor="A8", outcome="llm_unavailable", details={"error": security.redact(exc)})
    canonical = {s.lower(): s for s in skills}
    if check["passed"]:
        claims = [{"skill": canonical[c["skill"].strip().lower()], "level": c["level"], "quote": " ".join(c["quote"].split())[:MAX_QUOTE]}
                  for c in result["claims"]]
    else:
        generated_by, claims = "rules", rules_resume(text, skills)
    return {"claims": claims, "generated_by": generated_by, "llm_calls": llm_calls, "injection_flags": flags}
