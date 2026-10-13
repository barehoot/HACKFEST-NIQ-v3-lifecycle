"""
agent7_evidence.py

Agent 7, evidence classifier. A manager tags an email or Teams conversation
as performance evidence about one of their team; this agent proposes how to
record it -- the kind (appreciation, achievement, feedback, 1:1, escalation,
concern), the tone, which of the role's skills it shows, any action items, and
a one-line factual summary written about "the employee".

  compose  -> 1 LLM call; the message is untrusted data (delimited, sanitised)
  verify   -> deterministic: allowed kinds, tone consistent with the kind,
              skills only from the role's list, a short summary with no email
              address, at most three action items (0 tokens)
  revise   -> once, with the failures
  fallback -> keyword rules, labelled "rules"

It only suggests: nothing is recorded until the manager confirms (and can
edit) the suggestion in the portal. For a note the manager wrote by hand
(source "manual") it is a second reading: the skills it shows, a factual
summary and a tone check the manager can act on; the manager's own record
is never changed. The portal redacts emails and phone
numbers before the text arrives here, and keeps only an excerpt.
"""

import re
import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENTS_DIR))

import security  # noqa: E402
from llm_client import call_llm_json  # noqa: E402

KINDS = ("appreciation", "achievement", "feedback", "one_on_one", "escalation", "concern")
SENTIMENTS = ("positive", "neutral", "negative")
SOURCES = ("outlook", "teams", "email_file", "integration", "manual")  # manual: a note the manager wrote
MAX_TEXT = 4000
MAX_SUMMARY = 300
_FORBIDDEN_TONE = {"appreciation": "negative", "achievement": "negative", "escalation": "positive", "concern": "positive"}

SYSTEM_PROMPT = """You classify a workplace message that a manager tagged as performance evidence about one \
member of their team. Record facts only: what happened, the outcome, and its impact. Refer to the person as \
"the employee"; never repeat names, email addresses or phone numbers. Always respond with ONLY a single JSON \
object, no prose, no markdown code fences."""

_RULES = (
    ("escalation", ("escalat", "complain", "complaint", "unacceptable", "breach", "missed the deadline",
                    "missed deadline", "not acceptable", "raised an issue", "incident", "penalty", "late payment")),
    ("concern", ("concern", "worried", "struggling", "behind schedule", "quality issue", "error", "mistake", "rework")),
    ("appreciation", ("thank", "appreciate", "great job", "well done", "kudos", "excellent", "outstanding",
                      "brilliant", "grateful", "fantastic", "shout-out", "shout out")),
    ("achievement", ("delivered", "completed", "launched", "achieved", "exceeded", "automated", "reduced",
                     "saved", "closed the books", "went live")),
    ("one_on_one", ("1:1", "one-on-one", "one on one", "catch-up", "catch up", "check-in", "agenda", "we discussed")),
)
_BULLET_RE = re.compile(r"^\s*[-*•]\s+")
_LABEL_RE = re.compile(r"^\s*(?:action|todo|next step)s?\s*:\s*", re.IGNORECASE)


def _action_items(text):
    """Bulleted lines and "Action: / TODO: / Next step:" lines, label stripped."""
    out = []
    for line in text.splitlines():
        bullet = _BULLET_RE.match(line)
        rest = line[bullet.end():] if bullet else line
        label = _LABEL_RE.match(rest)
        item = (rest[label.end():] if label else rest).strip()
        if (bullet or label) and item:
            out.append(item[:200])
    return out[:3]


class ClassifyInputError(ValueError):
    pass


def _skills_in(text, skills):
    low = text.lower()
    found = []
    for skill in skills:
        words = [w for w in re.findall(r"[a-z0-9&]{4,}", skill.lower()) if w not in {"and", "with", "management"}]
        if skill.lower() in low or (words and words[0] in low):
            found.append(skill)
    return found[:4]


_RULE_WEIGHTS = {"escalation": 2.0, "concern": 1.0, "appreciation": 1.5, "achievement": 1.0, "one_on_one": 1.0}


def rules_classify(text, skills):
    low = text.lower()
    # Weighted keyword counts, so "thanks for catching the error" reads as appreciation
    # while "thanks, but this is unacceptable -- escalating" reads as an escalation.
    scores = {k: _RULE_WEIGHTS[k] * sum(low.count(w) for w in words) for k, words in _RULES}
    best = max(scores.values())
    kind = next(k for k, _ in _RULES if scores[k] == best) if best else "feedback"
    sentiment = {"appreciation": "positive", "achievement": "positive", "escalation": "negative",
                 "concern": "negative"}.get(kind, "neutral")
    first = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))[0][:200]
    return {"kind": kind, "sentiment": sentiment, "summary": f"{kind.replace('_', ' ').capitalize()} message: {first}"[:MAX_SUMMARY],
            "skills": _skills_in(text, skills), "action_items": _action_items(text), "confidence": 0.4}


def _build_prompt(text, skills, source, feedback=None):
    fb = ""
    if feedback:
        fb = "\n\nYour previous answer failed these checks -- fix every one:\n" + "\n".join(f"- {f}" for f in feedback)
    origin = "wrote this note" if source == "manual" else f"tagged this {source} message"
    return f"""A manager {origin} as evidence about one of their team members.

The role's skills (choose only from these, or none): {", ".join(skills) or "(none)"}

{security.wrap_untrusted("tagged_message", text, MAX_TEXT)}

Classify it:
- kind: one of {", ".join(KINDS)} ("one_on_one" for notes of a 1:1 conversation).
- sentiment: positive, neutral or negative -- consistent with the kind.
- summary: one or two factual sentences (max {MAX_SUMMARY} characters) about what the employee did and its impact.
- skills: the role skills the message shows evidence of (may be empty).
- action_items: up to 3 follow-ups agreed in the message (may be empty).
- confidence: 0 to 1.{fb}

Respond with ONLY this JSON object:
{{"kind": "...", "sentiment": "...", "summary": "...", "skills": [], "action_items": [], "confidence": 0.8}}"""


def verify(result, skills):
    failures = []
    if not isinstance(result, dict):
        return {"passed": False, "failures": ["response is not an object"]}
    kind, sentiment = result.get("kind"), result.get("sentiment")
    if kind not in KINDS:
        failures.append(f"kind '{kind}' is not allowed")
    if sentiment not in SENTIMENTS:
        failures.append(f"sentiment '{sentiment}' is not allowed")
    elif _FORBIDDEN_TONE.get(kind) == sentiment:
        failures.append(f"a {kind} can't be {sentiment}")
    summary = str(result.get("summary") or "").strip()
    if not summary or len(summary) > MAX_SUMMARY:
        failures.append(f"summary must be 1-{MAX_SUMMARY} characters")
    if "@" in summary:
        failures.append("summary must not contain an email address")
    allowed = {s.lower() for s in skills}
    picked = result.get("skills") if isinstance(result.get("skills"), list) else []
    bad = [s for s in picked if not isinstance(s, str) or s.lower() not in allowed]
    if bad:
        failures.append(f"skills not in the role's list: {', '.join(str(b) for b in bad[:3])}")
    if not isinstance(result.get("action_items", []), list) or len(result.get("action_items") or []) > 3:
        failures.append("action_items must be a list of at most 3")
    return {"passed": not failures, "failures": failures}


def classify(text, skills, source="outlook"):
    if source not in SOURCES:
        raise ClassifyInputError("Unknown source.")
    text = security.sanitize_prompt_text(text, MAX_TEXT)
    if len(text) < 20:
        raise ClassifyInputError("Not enough text to classify.")
    skills = [security.sanitize_prompt_text(s, 120) for s in skills if isinstance(s, str) and s.strip()][:20]
    flags = security.detect_injection(text)
    if flags:
        security.audit("prompt_injection_suspected", actor="A7", outcome="flagged", stream="llm",
                       resource="evidence_classify", details={"indicators": flags})

    generated_by, verification, llm_calls = "agent", None, 0
    try:
        result = call_llm_json(SYSTEM_PROMPT, _build_prompt(text, skills, source), agent="A7")
        llm_calls = 1
        verification = verify(result, skills)
        if not verification["passed"]:
            result = call_llm_json(SYSTEM_PROMPT, _build_prompt(text, skills, source, verification["failures"]), agent="A7")
            llm_calls = 2
            verification = verify(result, skills)
    except (RuntimeError, ValueError) as exc:
        security.audit("a7_classify", actor="A7", outcome="llm_unavailable", details={"error": security.redact(exc)})
        result, verification = None, {"passed": False, "failures": ["the model was unavailable"]}

    if not verification["passed"]:
        generated_by = "rules"
        result = rules_classify(text, skills)
    canonical = {s.lower(): s for s in skills}
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "kind": result["kind"], "sentiment": result["sentiment"],
        "summary": str(result["summary"]).strip()[:MAX_SUMMARY],
        "skills": [canonical[s.lower()] for s in result.get("skills") or [] if s.lower() in canonical][:4],
        "action_items": [str(a).strip()[:200] for a in result.get("action_items") or [] if str(a).strip()][:3],
        "confidence": round(confidence, 2), "injection_flags": flags,
        "generated_by": generated_by, "llm_calls": llm_calls,
    }
