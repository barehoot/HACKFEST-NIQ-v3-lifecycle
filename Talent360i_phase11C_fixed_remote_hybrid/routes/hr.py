"""The HR business partner's workspace: organisation structure (verticals,
departments, managers' spans of control), job-profile onboarding and readiness,
the skill catalogue, new joiners on probation and the people directory.

HR reads people data organisation-wide and maintains structure, job profiles and
the skill catalogue. It records nothing about an individual's performance: evidence,
reviews and development plans stay with the line manager, probation decisions and
calibration with the business leader.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request

from services import auth, job_profiles, lifecycle_store as store, performance, probation
from routes.web import pop_flash, render

router = APIRouter()
HR = ("hr", "admin")
WIDE_SPAN = 15        # direct reports above which a manager's span of control is flagged
SUPPORT_SHARE = 0.4   # share of a team at risk or off track that flags the manager for support


def team_flags(cluster: dict) -> list[dict]:
    """Leader/HR prompts for one manager's team, from its band mix and size (rules, not AI)."""
    flags = []
    concern = (cluster["at_risk"] or 0) + (cluster["off_track"] or 0)
    if cluster["manager_id"] is None:
        flags.append({"label": "Assign a manager", "tone": "warn"})
    if cluster["team"] >= 3 and concern / cluster["team"] >= SUPPORT_SHARE:
        flags.append({"label": "Needs support", "tone": "bad"})
    if cluster["team"] > WIDE_SPAN:
        flags.append({"label": f"Wide span ({cluster['team']})", "tone": "warn"})
    if cluster["promotion_ready"]:
        flags.append({"label": f"{cluster['promotion_ready']} promotion-ready", "tone": "good"})
    return flags


@router.get("/hr/dashboard")
def hr_dashboard(request: Request, user: dict = Depends(auth.require_role(*HR))):
    today = date.today()
    performance.refresh_missing(today)
    clusters, managers = store.manager_clusters({}, page=1, page_size=6, sort="attention")
    for cluster in clusters:
        cluster["flags"] = team_flags(cluster)
    catalogue = job_profiles.catalogue()
    pipeline = []
    for role in catalogue["roles"]:
        if role["custom"] or role["skills"] < job_profiles.MIN_SKILLS:
            ready = job_profiles.readiness(role["role_id"])
            pipeline.append({**role, "readiness": ready})
    pipeline.sort(key=lambda r: (r["readiness"]["pct"], r["role_name"]))
    suggestions = store.pending_suggestion_counts()
    return render(request, "hr/dashboard.html", bands=performance.band_strip({}), clusters=clusters, managers=managers,
                  verticals=store.list_verticals(), departments=store.list_departments(), pipeline=pipeline[:8],
                  pending_changes=catalogue["pending"], role_suggestions=sum(v for k, v in suggestions.items() if k != "catalogue"),
                  catalogue_suggestions=suggestions.get("catalogue", 0), probation=probation.summary(None, today),
                  headcount=store.headcount(), skills=len(store.skill_master_rows()), notice=pop_flash(request))
