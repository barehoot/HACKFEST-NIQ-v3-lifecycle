"""
seed_lifecycle.py

Demo data for the v3 employee-lifecycle module, on top of the synthetic
Hackfest cohort: departments, line-manager teams (M001-M003), hire dates (three new joiners on probation at
different stages), evidence (appreciation, achievements, 1:1s, an escalation,
pending Outlook / Teams suggestions and a peer recognition), H1 reviews,
development actions (some done, some overdue), weekly pulse check-ins, SME
coding challenges, market trends and a manager's skill-requirement proposal.
It then computes everyone's status snapshot.

All dates are relative to today, so the demo stays current whenever it is
re-seeded. Idempotent: items already present are left alone. Run by
`python start.py` on setup; re-run any time:

    python scripts/seed_lifecycle.py
"""

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "Talent360i_phase11C_fixed_remote_hybrid"
sys.path.insert(0, str(PORTAL))

from services import db, engagement, job_profiles, lifecycle_store as store, mobility, performance, probation  # noqa: E402
from services import smartrecruiters  # noqa: E402

TODAY = date.today()
MANAGER, LEADER, SME = "M001", "L001", "S001"


def days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def departments() -> dict[str, str]:
    wanted = [("Accounts Payable", "AP", "Invoice-to-pay, T&E and supplier operations"),
              ("Order to Cash", "O2C", "Billing, revenue assurance and cash application"),
              ("Record to Report", "R2R", "Banking, GA, consolidation and close")]
    for name, tower, desc in wanted:
        store.create_department(name=name, tower=tower, description=desc,
                                leader_id=LEADER if db.get_user(LEADER) else None, created_by="seed")
    return {d["tower"]: d["dept_id"] for d in store.list_departments() if d["tower"]}


def profiles(dept_by_tower: dict[str, str]) -> None:
    for user in db.list_users():
        if user["role"] != "employee":
            continue
        tower = user["department"] if user["department"] in dept_by_tower else "AP"
        store.upsert_profile(user["user_id"], dept_id=dept_by_tower.get(tower),
                             location="Pune" if user["user_id"].endswith(("1", "4", "7")) else "Chicago")
    # New joiners at three probation stages, plus the demo employee mid-way.
    for user_id, days in (("SYN-U034", 20), ("SYN-U035", 100), ("SYN-U036", 165), ("U001", 120)):
        if db.get_user(user_id) and not store.probation_for_user(user_id):
            store.upsert_profile(user_id, hire_date=days_ago(days))


# Line-manager teams for the people-by-manager view. Only employees no walkthrough step
# depends on move, and only once: a manager who already has reports is left alone, so an
# admin's later reassignments are never undone by a re-seed.
TEAMS = {
    "M002": ("SYN-U026", "SYN-U027", "SYN-U028", "SYN-U030", "SYN-U031", "SYN-U032", "SYN-U033"),
    "M003": ("SYN-U003", "SYN-U004", "SYN-U005", "SYN-U006", "SYN-U007", "SYN-U008", "SYN-U010", "SYN-U011", "SYN-U012"),
}


def teams() -> None:
    reports = {u["manager_id"] for u in db.list_users() if u["role"] == "employee" and u.get("manager_id")}
    for manager_id, members in TEAMS.items():
        if not db.get_user(manager_id) or manager_id in reports:
            continue
        for user_id in members:
            employee = db.get_user(user_id)
            if employee and employee["role"] == "employee" and employee.get("manager_id") in (None, MANAGER):
                db.update_user_role(user_id, "employee", manager_id)
    # Everyone else without a line manager reports to the demo manager.
    for user in db.list_users():
        if user["role"] == "employee" and not user.get("manager_id"):
            db.update_user_role(user["user_id"], "employee", MANAGER)


def probations() -> None:
    probation.ensure_for_new_joiners(TODAY)
    notes = {"on_track": "Picked up the SOPs quickly and closes assigned items on time; asks good questions.",
             "at_risk": "Accuracy on invoice matching is below standard; needs closer review for the next month."}
    plan = {"U001": ["on_track", "on_track"], "SYN-U035": ["at_risk"], "SYN-U036": ["on_track", "on_track", "on_track"]}
    for user_id, ratings in plan.items():
        record = store.probation_for_user(user_id)
        if not record:
            continue
        for checkpoint, rating in zip(store.checkpoints(record["probation_id"]), ratings):
            if not checkpoint.get("completed_at"):
                store.complete_checkpoint(checkpoint["checkpoint_id"], record["probation_id"], rating, notes[rating], MANAGER)
    record = store.probation_for_user("SYN-U036")
    if record and record["status"] == "active":
        store.recommend_probation(record["probation_id"], "confirm",
                                  "Three documented on-track check-ins; meets the Senior Revenue Associate bar on billing accuracy "
                                  "and has started mentoring the newest joiner.", MANAGER)


EVIDENCE = [
    # user, kind, title, summary, days ago, weight, skills
    ("SYN-U013", "achievement", "Cut duplicate-payment exceptions by 40% in Q3",
     "Redesigned the duplicate-invoice check with vendor-master clean-up; exceptions fell from 120 to 72 a month.", 25, 3, []),
    ("SYN-U013", "appreciation", "Thanks from the Treasury lead",
     "Treasury thanked the employee for clearing the payment-run backlog before the quarter-end freeze.", 60, 1, []),
    ("SYN-U013", "one_on_one", "Monthly 1:1: career and stretch goals",
     "Discussed readiness for a lead role and agreed to own the vendor-master project next quarter.", 12, 1, []),
    ("SYN-U017", "escalation", "Supplier escalation: late payment on a critical vendor",
     "A critical supplier escalated two late payments; root cause was an unreviewed hold on the invoice queue.", 18, 2, []),
    ("SYN-U017", "concern", "Repeated three-way-match exceptions left open",
     "Fourteen exceptions were open past SLA at month end, after reminders in two 1:1s.", 40, 1, []),
    ("SYN-U017", "one_on_one", "1:1: exception backlog plan",
     "Agreed a daily exceptions review and a checklist for invoice holds.", 9, 1, []),
    ("SYN-U025", "appreciation", "Customer praise for billing accuracy",
     "Key account confirmed zero billing disputes for the quarter and named the employee in the review.", 30, 2, []),
    ("SYN-U025", "achievement", "Automated the credit-note register",
     "Built a Power Query refresh that replaced a two-hour manual reconciliation each week.", 75, 2, []),
    ("SYN-U029", "feedback", "Peer feedback from the close team",
     "Clear revenue-recognition memos; could share drafts earlier to give reviewers time.", 50, 1, []),
    ("SYN-U001", "achievement", "Bank reconciliations closed on day 2",
     "Closed all bank reconciliations by working day 2 for two consecutive months.", 35, 2, []),
    ("SYN-U009", "concern", "Consolidation pack submitted late",
     "The intercompany elimination schedule arrived a day after the deadline and needed rework.", 28, 1, []),
    ("SYN-U009", "escalation", "Group reporting escalated a pack error",
     "Group reporting escalated an elimination error that reached the draft consolidated results.", 14, 2, []),
    ("U001", "one_on_one", "Probation 1:1: settling in",
     "Comfortable with the P2P SOPs; next step is handling vendor queries without escalation.", 21, 1, []),
    ("U001", "appreciation", "Thanks from the AP lead",
     "The AP lead thanked the employee for catching a duplicate supplier invoice worth USD 18,400.", 8, 2, []),
]

PENDING = [
    ("SYN-U021", "outlook", "Re: T&E audit sample -- great turnaround",
     "Hi team,\nJust wanted to say thank you for turning round the T&E audit sample in a day. The documentation was "
     "complete and the auditors had no follow-up questions.\nAction: share the sample checklist with the wider team.\nThanks, Audit", "Internal Audit"),
    ("SYN-U017", "teams", "",
     "[10:02] Vendor desk: the Acme invoice is still on hold, they've called three times today\n[10:05] Can someone "
     "explain why this wasn't released on Monday? This is the second time this month.\n- Next step: release by noon and confirm to the vendor", None),
]


def evidence() -> None:
    for user_id, kind, title, summary, ago, weight, skills in EVIDENCE:
        if db.get_user(user_id):
            sentiment = {"appreciation": "positive", "achievement": "positive", "escalation": "negative",
                         "concern": "negative"}.get(kind, "neutral")
            store.add_evidence(user_id=user_id, kind=kind, source="manual", title=title, summary=summary,
                               occurred_on=days_ago(ago), sentiment=sentiment, created_by=MANAGER, weight=weight,
                               skills=skills, content_hash=f"seed-{user_id}-{title}")
    for user_id, source, subject, body, sender in PENDING:
        if not db.get_user(user_id):
            continue
        negative = source == "teams"
        store.add_evidence(
            user_id=user_id, kind="escalation" if negative else "appreciation", source=source,
            title=subject or "Teams conversation · " + days_ago(2), occurred_on=days_ago(2),
            summary=("A vendor hold was escalated twice in one morning; the employee was asked to release it by noon."
                     if negative else "Internal Audit thanked the employee for a complete, one-day T&E audit sample."),
            sentiment="negative" if negative else "positive", created_by=MANAGER, status="pending", origin_sender=sender,
            excerpt=body, content_hash=f"seed-pending-{user_id}-{source}",
            ai={"kind": "escalation" if negative else "appreciation", "sentiment": "negative" if negative else "positive",
                "confidence": 0.86, "generated_by": "agent", "skills": []},
            action_items=["Release the Acme invoice by noon and confirm to the vendor"] if negative
            else ["Share the T&E sample checklist with the wider team"])
    if db.get_user("SYN-U014") and db.get_user("SYN-U013"):
        store.add_evidence(user_id="SYN-U014", kind="appreciation", source="peer", title="Recognition from Synthetic Associate 013",
                           summary="Covered my supplier calls during the system cut-over so nothing slipped. Thank you!",
                           occurred_on=days_ago(3), sentiment="positive", created_by="SYN-U013", status="pending",
                           origin_sender="Synthetic Associate 013", content_hash="seed-kudos-U014")


REVIEWS = {"SYN-U013": (5, "Consistently exceeded targets; led the duplicate-payment redesign."),
           "SYN-U025": (4, "Strong billing accuracy and an automation that saves the team hours every week."),
           "SYN-U001": (4, "Reliable close performance; bank reconciliations consistently early."),
           "SYN-U029": (3, "Meets expectations; revenue memos are clear, timing can improve."),
           "SYN-U017": (2, "Exception backlog and two supplier escalations this half; a focused plan is in place."),
           "SYN-U009": (2, "Pack timeliness and accuracy below the standard for the role."),
           "SYN-U002": (3, "Solid on cash posting; building confidence on complex breaks.")}


def broad_cohort() -> None:
    """A review, a 1:1 and an evidence item or two for everyone else in the synthetic cohort, deterministic
    by employee number, so the demo shows a realistic spread rather than mostly "not enough data"."""
    period = f"{TODAY.year}-H1" if TODAY.month > 6 else f"{TODAY.year - 1}-H2"
    explicit = set(REVIEWS) | {e[0] for e in EVIDENCE} | {"SYN-U034", "SYN-U035", "SYN-U036"}
    ratings = [4, 3, 3, 4, 2, 3, 5, 3, 4, 3]
    for user in db.list_employees():
        uid = user["user_id"]
        if not uid.startswith("SYN-U") or uid in explicit:
            continue
        n = int(uid[-3:])
        rating = ratings[n % len(ratings)]
        store.save_review(user_id=uid, period=period, manager_id=MANAGER, rating=rating,
                          summary={5: "Outstanding half: delivered beyond the role and lifted the team.",
                                   4: "Strong, reliable delivery with clear ownership.",
                                   3: "Meets the role's expectations; steady progress on development areas.",
                                   2: "Below expectations on core deliverables this half."}[rating],
                          strengths="", improvements="Timeliness on month-end deliverables." if rating <= 3 else "",
                          share=True, ai_assisted=False)
        store.add_evidence(user_id=uid, kind="one_on_one", source="manual", title="Monthly 1:1",
                           summary="Reviewed priorities, blockers and progress on the development plan.",
                           occurred_on=days_ago(5 + n % 30), sentiment="neutral", created_by=MANAGER,
                           content_hash=f"seed-1on1-{uid}")
        if rating >= 4 or n % 3 == 0:
            store.add_evidence(user_id=uid, kind="appreciation", source="manual", title="Thanks from a stakeholder",
                               summary="A business partner thanked the employee for a fast, accurate turnaround on a month-end request.",
                               occurred_on=days_ago(10 + n % 60), sentiment="positive", created_by=MANAGER,
                               weight=2 if rating == 5 else 1, content_hash=f"seed-thanks-{uid}")
        if rating <= 2 or n % 7 == 0:
            store.add_evidence(user_id=uid, kind="concern", source="manual", title="Deadline missed on a recurring report",
                               summary="A recurring report was submitted after the agreed deadline without notice.",
                               occurred_on=days_ago(15 + n % 45), sentiment="negative", created_by=MANAGER,
                               content_hash=f"seed-concern-{uid}")


def simulate_history() -> None:
    """SYN-U017 has been off track for 40 days with a plan that isn't moving: back-date the snapshot's
    band start so the leader-level escalation rule fires, as it would have in real time."""
    row = store.status_rows(["SYN-U017"]).get("SYN-U017")
    if row and row["band"] == "Off track" and not store.list_escalations(user_ids=["SYN-U017"], level="leader"):
        conn = store.conn()
        try:
            conn.execute("UPDATE employee_status SET band_since = ? WHERE user_id = 'SYN-U017'", (days_ago(40),))
            conn.commit()
        finally:
            conn.close()
        performance.refresh(["SYN-U017"], TODAY)


def reviews() -> None:
    period = f"{TODAY.year}-H1" if TODAY.month > 6 else f"{TODAY.year - 1}-H2"
    for user_id, (rating, summary) in REVIEWS.items():
        if db.get_user(user_id):
            review_id = store.save_review(user_id=user_id, period=period, manager_id=MANAGER, rating=rating, summary=summary,
                                          strengths="", improvements="" if rating >= 3 else "Timeliness and accuracy of core deliverables.",
                                          share=True, ai_assisted=False)
            if review_id and rating >= 4:
                store.acknowledge_review(review_id, user_id, "Thanks -- agreed, and keen on the stretch project.")


ACTIONS = {
    "SYN-U017": [("Daily review of open three-way-match exceptions", "behaviour", -20, "done"),
                 ("Complete the invoice-hold SOP refresher", "training", -10, "open"),
                 ("Shadow the AP lead on supplier escalations", "mentoring", -5, "open"),
                 ("Zero exceptions past SLA for one month", "practice", 25, "open")],
    "SYN-U009": [("Elimination-schedule checklist before submission", "behaviour", -12, "open"),
                 ("Consolidation pack walkthrough with the R2R lead", "mentoring", -4, "open")],
    "SYN-U013": [("Own the vendor-master clean-up project", "stretch", 45, "in_progress"),
                 ("Present the duplicate-check redesign to the AP leadership team", "practice", -15, "done")],
    "SYN-U025": [("Document the credit-note automation for the team", "practice", -8, "done"),
                 ("Power Query advanced course", "training", 30, "open")],
}


def actions() -> None:
    for user_id, rows in ACTIONS.items():
        if not db.get_user(user_id) or store.list_actions(user_id):
            continue
        ids = store.add_actions(user_id, [{"title": t, "action_type": kind, "due_date": days_ago(-due), "source": "manager",
                                           "success_measure": "Confirmed at the next 1:1."} for t, kind, due, _ in rows], MANAGER)
        for action_id, (_, _, _, status) in zip(ids, rows):
            if status != "open":
                store.set_action_status(action_id, status)
        # Back-date the plan so the escalation rules see how long it has been running.
        conn = store.conn()
        try:
            conn.execute("UPDATE development_actions SET created_at = ? WHERE user_id = ?", (days_ago(45) + "T09:00:00+00:00", user_id))
            conn.commit()
        finally:
            conn.close()


def pulses() -> None:
    sample = {"SYN-U013": [(5, 3), (4, 3), (5, 2)], "SYN-U017": [(2, 5), (2, 5), (3, 4)], "SYN-U025": [(4, 3), (4, 4)],
              "SYN-U001": [(4, 3), (3, 3), (4, 3)], "U001": [(4, 3), (4, 2)], "SYN-U009": [(3, 4), (2, 4)]}
    for user_id, weeks in sample.items():
        if db.get_user(user_id):
            for n, (mood, workload) in enumerate(weeks):
                store.upsert_pulse(user_id=user_id, week=engagement.week_key(TODAY - timedelta(weeks=n)), mood=mood,
                                   workload=workload, comment="Month-end is heavy this time" if workload >= 5 else "",
                                   shared=workload >= 5)


CHALLENGES = [
    ("Match bank lines to open invoices", "python", ["python", "pandas"], ["Reconciliations & Clearing"], "intermediate",
     "You have two lists of dicts: bank_lines (id, amount, reference, value_date) and open_invoices (invoice_no, amount, "
     "customer, due_date). Write match(bank_lines, open_invoices) returning (matches, unmatched_bank, unmatched_invoices). "
     "Match on the invoice number appearing in the bank reference first; otherwise on an exact amount when exactly one "
     "invoice has it. Never match one bank line to two invoices.",
     "def match(bank_lines, open_invoices):\n    matches = []\n    # your code here\n    return matches, [], []\n",
     "Correct reference match first, then unique-amount fallback; no double matching; unmatched lists are complete; "
     "handles empty inputs and amounts as Decimal or cents (no float equality); readable names and a short docstring."),
    ("Aged receivables by bucket", "sql", ["sql", "t-sql", "postgresql", "snowflake"], ["Collections & Cash Application"], "intermediate",
     "Table ar_open_items(customer_id, invoice_no, invoice_date, due_date, open_amount). Write one query returning, per "
     "customer, the open amount in buckets: current, 1-30, 31-60, 61-90 and 90+ days past due as of DATE '2026-10-01', plus "
     "the total, ordered by the 90+ bucket descending.",
     "SELECT customer_id\n     -- your buckets here\nFROM ar_open_items\n",
     "Uses due_date (not invoice_date) for ageing; mutually exclusive buckets with correct boundaries; SUM with CASE or "
     "FILTER; groups by customer; includes the total; ordering correct; no hard-coded customer IDs."),
    ("Flag duplicate supplier invoices", "vba", ["vba", "excel", "power query", "macros"], ["Invoice Processing & 3-Way Match"], "foundational",
     "Sheet 'Invoices' has columns A:Supplier, B:InvoiceNo, C:Amount, D:InvoiceDate from row 2. Write a VBA macro that "
     "writes 'POSSIBLE DUPLICATE' in column E for any row whose supplier and invoice number (ignoring case, spaces and "
     "leading zeros) match an earlier row, or whose supplier and amount match an earlier row within 7 days.",
     "Sub FlagDuplicates()\n    ' your code here\nEnd Sub\n",
     "Normalises invoice numbers; checks both rules; compares only against earlier rows; finds the last row dynamically; "
     "uses a Dictionary or Collection rather than nested loops for the first rule; no hard-coded row counts."),
]


def challenges() -> None:
    if store.list_challenges():
        return
    for title, language, keywords, skills, difficulty, prompt, starter, rubric in CHALLENGES:
        challenge_id = store.create_challenge(title=title, prompt=prompt, language=language, starter_code=starter,
                                              keywords=keywords, skills=skills, difficulty=difficulty, rubric=rubric,
                                              time_minutes=30, created_by=SME)
        store.set_challenge_status(challenge_id, "approved", SME)


TRENDS = [
    ("AI-assisted reconciliation", "R2R", "emerging",
     "Matching engines with ML suggestions are moving reconciliation work from matching to exception judgement.",
     "Vendor roadmaps (BlackLine, HighRadius); 2026 close benchmark"),
    ("Python & Power Query for finance automation", None, "rising",
     "Teams expect associates to automate recurring reconciliations and reports themselves.", "Internal automation backlog"),
    ("E-invoicing compliance", "AP", "rising",
     "Mandatory e-invoicing (EU ViDA, India IRN) changes invoice validation and supplier onboarding.", "Regulatory calendar"),
    ("Manual journal posting", "R2R", "declining",
     "Recurring journals are moving to templates and automation; manual posting volume keeps falling.", "Close metrics"),
]


def trends_and_changes() -> None:
    if not store.list_trends():
        for skill, tower, direction, note, source in TRENDS:
            store.add_trend(skill=skill, tower=tower, direction=direction, note=note, source=source, added_by=LEADER)
    if not store.list_skill_changes() and db.get_user(MANAGER):
        trend = next((t for t in store.list_trends() if t["skill"].startswith("Python")), None)
        store.add_skill_change(
            role_id="O2C_senior_revenue_associate", action="add", skill="Finance Automation (Python / Power Query)",
            payload={"target_level": 2, "is_critical": False, "capability": "Digital",
                     "behavior_indicator": "Automates a recurring reconciliation or report with Python or Power Query, "
                                           "with a documented refresh and exception log"},
            reason="Two of four associates already automate the credit-note register; make it part of the role.",
            trend_id=trend["trend_id"] if trend else None, proposed_by=MANAGER)


# ---------------------------------------------------------------------------
# Career mobility demo: a Data & Analytics vertical with a Data Science department,
# Data Analyst / Data Scientist job profiles and courses, internal postings, and a few
# finance-operations people who want to cross into data.
# ---------------------------------------------------------------------------

SEED_EDITOR = {"id": "A001", "role": "admin"}
DATA_SKILLS = [  # name, capability, course id, course title
    ("Python Programming", "Digital & Automation", "TRN-DNA-01", "Python Programming for Finance Analysts"),
    ("SQL & Data Wrangling", "Technical Expertise", "TRN-DNA-02", "SQL & Data Wrangling Fundamentals"),
    ("Statistics & Experimentation", "Analytics", "TRN-DNA-03", "Statistics & Experimentation Refresher"),
    ("Machine Learning", "Analytics", "TRN-DNA-04", "Machine Learning Foundations"),
    ("Data Visualisation (Power BI)", "Analytics", "TRN-DNA-05", "Power BI Dashboards & Data Visualisation"),
    ("Data Storytelling", "Stakeholder Management", "TRN-DNA-06", "Data Storytelling for Analysts"),
]
DATA_ROLES = {
    "Data Analyst": ("B4", "Turns finance-operations data into insight: SQL data sets from the ERP and sub-ledgers, Python automation, "
                           "Power BI dashboards and clear explanations for process owners.", [
        ("SQL & Data Wrangling", 3, True, "Builds reliable SQL data sets joining ERP and sub-ledger tables, with reconciled totals."),
        ("Data Visualisation (Power BI)", 3, True, "Designs Power BI dashboards process owners use weekly, with clear definitions."),
        ("Python Programming", 3, False, "Automates recurring analysis in Python (pandas) with reproducible, reviewed scripts."),
        ("Statistics & Experimentation", 2, False, "Uses descriptive statistics and simple tests to separate signal from noise."),
        ("Data Storytelling", 3, False, "Explains trends and anomalies to finance leaders in plain language with a recommendation."),
    ]),
    "Data Scientist": ("P3", "Builds and validates the models that predict late payments, duplicate invoices and cash-flow risk, "
                             "and measures their impact with AP, O2C and Treasury.", [
        ("Python Programming", 4, True, "Builds production-quality Python modelling pipelines with tests and versioned data."),
        ("Statistics & Experimentation", 4, True, "Designs experiments, chooses tests and quantifies uncertainty in model impact."),
        ("Machine Learning", 3, True, "Trains, validates and monitors supervised models for finance-operations use cases."),
        ("SQL & Data Wrangling", 3, False, "Builds feature tables in SQL from ERP data with documented lineage."),
        ("Data Storytelling", 3, False, "Explains model results, limits and trade-offs to non-technical stakeholders."),
    ]),
}
ASPIRATIONS = [  # user, role name, note
    ("U001", "Data Analyst", "I automate invoice checks with Python and Power BI and want to move into analytics."),
    ("U001", "P2P Lead", "Next step in AP if a lead role opens."),
    ("SYN-U025", "Data Analyst", "I built the credit-note register automation and enjoy the data side most."),
    ("SYN-U029", "Data Scientist", "Revenue analytics is where I want to grow."),
    ("SYN-U013", "Data Analyst", "Interested in the vendor-master analytics work."),
]
CV_CLAIMS = {  # user -> (skill, claimed level, quote) -- the kind of claims Agent 8 extracts
    "U001": [("Python Programming", 2, "Built Python scripts to automate invoice matching checks"),
             ("SQL & Data Wrangling", 2, "Wrote SQL queries to reconcile vendor balances against the ERP"),
             ("Data Visualisation (Power BI)", 3, "Created Power BI dashboards for AP ageing used in the weekly review")],
    "SYN-U025": [("SQL & Data Wrangling", 3, "Designed SQL extracts for billing accuracy reporting across 40 accounts"),
                 ("Data Visualisation (Power BI)", 3, "Built a Power Query and Power BI refresh replacing a two-hour manual reconciliation"),
                 ("Data Storytelling", 3, "Presented billing dispute trends to sales leadership every quarter")],
    # Hasn't applied for anything: shows up only as a "potential fit" in a talent-pool search.
    "SYN-U030": [("Python Programming", 3, "Maintain Python scripts that reconcile revenue schedules each month"),
                 ("SQL & Data Wrangling", 3, "Query the revenue sub-ledger in SQL to prepare audit samples"),
                 ("Statistics & Experimentation", 2, "Used regression to explain monthly variance in deferred revenue")],
}


def data_career_path() -> None:
    if "DNA" not in job_profiles.towers():
        job_profiles.create_vertical(code="DNA", name="Data & Analytics", description="Analytics, data science and BI",
                                     user=SEED_EDITOR)
    dept = next((d for d in store.list_departments() if d["name"] == "Data Science"), None)
    dept_id = dept["dept_id"] if dept else job_profiles.create_department(
        name="Data Science", tower="DNA", description="Analytics and models for finance operations",
        leader_id=LEADER if db.get_user(LEADER) else None, user=SEED_EDITOR)
    for name, capability, _, _ in DATA_SKILLS:
        if not store.skill_by_name(name):
            job_profiles.add_catalogue_skill(skill=name, tower="DNA", capability=capability, indicators=[], user=SEED_EDITOR)
    conn = db.get_conn()
    try:
        for name, capability, course_id, title in DATA_SKILLS:
            skill = store.skill_by_name(name)
            conn.execute("INSERT OR IGNORE INTO training_catalogue (course_id, tower, course_title, delivery_type, level_group, "
                         "risk_level, spoc_role) VALUES (?, 'DNA', ?, 'Virtual workshop', 'B3/B4/P3', 'Medium', 'Data Science lead')",
                         (course_id, title))
            conn.execute("INSERT OR IGNORE INTO training_skill_map (training_skill_map_id, course_id, tower, skill_id, skill, capability, "
                         "level_group, mapping_confidence) VALUES (?, ?, 'DNA', ?, ?, ?, 'B3/B4/P3', 'High')",
                         (f"TSM-{course_id}", course_id, skill["skill_id"], name, capability))
        conn.commit()
    finally:
        conn.close()
    for role_name, (grade, summary, skills) in DATA_ROLES.items():
        role_id = job_profiles.role_id_for("DNA", role_name)
        if not job_profiles.role_detail(role_id):
            job_profiles.create_role(name=role_name, tower="DNA", process_group="Data Science", grade=grade, dept_id=dept_id,
                                     summary=summary, responsibilities=summary,
                                     skills_text=", ".join(s[0] for s in skills), user=SEED_EDITOR)
        have = {s["skill"].lower() for s in job_profiles.role_detail(role_id)["skills"]}
        for skill, level, critical, behaviour in skills:
            if skill.lower() not in have:
                job_profiles.request_change(role_id, action="add", skill=skill,
                                            form={"target_level": level, "is_critical": critical, "behavior_indicator": behaviour},
                                            reason="Demo job profile for the data-science career path.", trend_id=None,
                                            user=SEED_EDITOR)


def mobility_demo() -> None:
    smartrecruiters.sync(force=True)  # demo feed unless SMARTRECRUITERS_COMPANY_ID is set
    roles = {r["role_name"]: r["role_id"] for r in db.role_master_rows()}
    for user_id, role_name, note in ASPIRATIONS:
        role_id = roles.get(role_name)
        if role_id and db.get_user(user_id) and not any(a["role_id"] == role_id for a in store.list_aspirations(user_id=user_id)):
            store.add_aspiration(user_id=user_id, role_id=role_id, posting_id=None, note=note)
    for user_id, claims in CV_CLAIMS.items():
        if db.get_user(user_id) and not store.cv_claims([user_id]).get(user_id):
            store.replace_cv_claims(user_id, [{"skill": s, "level": lv, "quote": q} for s, lv, q in claims], "seed",
                                    "demo-cv.pdf")
    if (db.get_user("SYN-U025") and store.get_posting("DEMO-1001")
            and not store.list_applications(user_id="SYN-U025", statuses=("submitted", "shortlisted"))):
        mobility.apply("SYN-U025", posting_id="DEMO-1001",
                       note="I have run the billing analytics for two years (SQL, Power BI) and want to do it full time "
                            "for the whole finance-operations data set.")


def main() -> None:
    if not db.get_user(MANAGER):
        raise SystemExit("Seed the users first (python scripts/seed_users.py).")
    dept_by_tower = departments()
    teams()
    profiles(dept_by_tower)
    probations()
    evidence()
    reviews()
    broad_cohort()
    actions()
    pulses()
    challenges()
    trends_and_changes()
    data_career_path()
    count = performance.refresh_all(TODAY)
    mobility_demo()
    simulate_history()
    bands: dict[str, int] = {}
    for row in store.status_rows(store.employee_ids()).values():
        bands[row["band"]] = bands.get(row["band"], 0) + 1
    print(f"Lifecycle demo data ready: {count} employees scored -- " + ", ".join(f"{k}: {v}" for k, v in sorted(bands.items())))


if __name__ == "__main__":
    main()
