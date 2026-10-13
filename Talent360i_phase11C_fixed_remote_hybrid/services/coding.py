"""SME coding challenges inside the workplace simulation.

An SME writes a coding problem (Python, SQL, Excel formulas, VBA, ...) with a
rubric and the resume keywords that trigger it ("python", "sql", "power
query"). At resume upload the pipeline API reports which of the approved
challenges' keywords the resume mentions -- only those keywords are kept, never
the resume. When the employee reaches the simulation, up to two matching
approved challenges join it as coding scenarios; the set is frozen for that
assessment so a later SME change can't move the goalposts mid-way.

The submitted code is never executed. Agent 4's code reviewer reads it
against the SME's rubric (static review, labelled AI) and an SME confirms or
overrides the score; the manager sees both on the validation page.
"""
from __future__ import annotations

import re

from domain.governance import sanitise_text
from services import api_client, lifecycle_store as store
from services.backend_api import BackendAPIError
from services.security import clean_multiline_text

MAX_CODE_CHARS = 8000
MAX_CHALLENGES_PER_CYCLE = 2
MAX_KEYWORDS = 12
DIFFICULTIES = ("foundational", "intermediate", "advanced")
_KEYWORD_RE = re.compile(r"^[a-z0-9+#.][a-z0-9+#. -]{0,39}$")


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class CodingError(ValueError):
    pass


def clean_code(text: str | None, limit: int = MAX_CODE_CHARS) -> str:
    """Control characters out, indentation kept (clean_multiline_text would collapse it)."""
    text = _CTRL_RE.sub("", str(text or "").replace("\r\n", "\n").replace("\r", "\n")).expandtabs(4)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")[:limit]


def parse_keywords(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[,\n;]", text or ""):
        word = " ".join(raw.strip().lower().split())
        if word and _KEYWORD_RE.match(word) and word not in out:
            out.append(word)
    return out[:MAX_KEYWORDS]


def trigger_keywords() -> list[str]:
    words: list[str] = []
    for challenge in store.list_challenges("approved"):
        words += [k for k in challenge["keywords"] if k not in words]
    return words[:50]


def record_resume_keywords(cycle_id: str, hits) -> None:
    known = set(trigger_keywords())
    clean = [k for k in parse_keywords(",".join(str(h) for h in (hits or [])[:50])) if k in known]
    store.save_resume_signals(cycle_id, {"keywords": clean, "challenge_ids": None})


def _match(keywords: list[str]) -> list[dict]:
    found = set(keywords)
    scored = []
    for challenge in store.list_challenges("approved"):
        hits = found & set(challenge["keywords"])
        if hits:
            scored.append((len(hits), DIFFICULTIES.index(challenge["difficulty"]), challenge))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]["challenge_id"]))
    return [c for _, _, c in scored[:MAX_CHALLENGES_PER_CYCLE]]


def challenges_for_cycle(cycle: dict) -> list[dict]:
    """The cycle's coding scenarios (frozen on first call) with each one's attempt, if submitted."""
    signals = store.resume_signals(cycle["cycle_id"])
    if signals.get("challenge_ids") is None:
        if not signals.get("keywords"):
            return []
        chosen = _match(signals["keywords"])
        signals["challenge_ids"] = [c["challenge_id"] for c in chosen]
        store.save_resume_signals(cycle["cycle_id"], signals)
    attempts = {a["challenge_id"]: a for a in store.list_attempts(cycle_id=cycle["cycle_id"])}
    out = []
    for challenge_id in signals["challenge_ids"]:
        challenge = store.get_challenge(challenge_id)
        if challenge:
            hits = sorted(set(signals["keywords"]) & set(challenge["keywords"]))
            out.append({**challenge, "matched_keywords": hits, "attempt": attempts.get(challenge_id)})
    return out


def pending_for_cycle(cycle: dict) -> list[dict]:
    return [c for c in challenges_for_cycle(cycle) if not c["attempt"]]


def submit(cycle: dict, user_id: str, challenge_id: str, code: str, paste_chars: int | None = None) -> dict:
    challenge = next((c for c in challenges_for_cycle(cycle) if c["challenge_id"] == challenge_id), None)
    if not challenge:
        raise CodingError("That coding task is not part of this assessment.")
    if challenge["attempt"]:
        raise CodingError("You've already submitted this coding task.")
    code = clean_code(code)
    if len(code.strip()) < 10:
        raise CodingError("Write your solution before submitting.")
    attempt_id = store.save_attempt(cycle_id=cycle["cycle_id"], challenge_id=challenge_id, user_id=user_id,
                                    language=challenge["language"], code=code, paste_chars=paste_chars)
    if not attempt_id:
        raise CodingError("You've already submitted this coding task.")
    payload = {k: challenge[k] for k in ("title", "prompt", "language", "rubric", "starter_code", "difficulty")}
    try:
        review = api_client.backend_code_review(payload, code, challenge["language"]) or {}
    except BackendAPIError:
        review = None
    # Flagged answers (risky calls, text aimed at the reviewer) always wait for an SME.
    reviewed = bool(review and review.get("generated_by") == "agent" and not review.get("needs_sme"))
    store.set_attempt_review(attempt_id, review, "ai_reviewed" if reviewed else "pending_review")
    return {"attempt_id": attempt_id, "ai_reviewed": reviewed}


def create(*, title: str, prompt: str, language: str, starter_code: str, keywords: str, skills: str,
           difficulty: str, rubric: str, time_minutes: int, created_by: str) -> str:
    title, prompt, rubric = sanitise_text(title, 140), clean_multiline_text(prompt, 4000), clean_multiline_text(rubric, 2000)
    words = parse_keywords(keywords)
    if len(title) < 5 or len(prompt) < 40:
        raise CodingError("Give the challenge a title and a problem statement of at least a few sentences.")
    if language not in store.CODING_LANGUAGES:
        raise CodingError("Choose a language.")
    if not words:
        raise CodingError("Add at least one resume keyword that triggers this challenge (e.g. python, sql).")
    if len(rubric) < 20:
        raise CodingError("Write the rubric the reviewer scores against (what a good answer does).")
    return store.create_challenge(
        title=title, prompt=prompt, language=language, starter_code=clean_code(starter_code, 3000),
        keywords=words, skills=[s for s in (sanitise_text(x, 120) for x in skills.split(",")) if s][:6],
        difficulty=difficulty if difficulty in DIFFICULTIES else "intermediate", rubric=rubric,
        time_minutes=max(5, min(120, int(time_minutes or 30))), created_by=created_by)


def sme_review(attempt_id: str, score: int, note: str, by: str) -> bool:
    if not 1 <= score <= 5:
        raise CodingError("Score from 1 to 5.")
    return store.sme_review_attempt(attempt_id, score, sanitise_text(note, 1000), by)
