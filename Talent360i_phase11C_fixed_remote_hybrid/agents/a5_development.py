"""Local deterministic stand-in for the HLD A5 Development Plan Agent.

A5 turns confirmed/calculated skill gaps into a sequenced development proposal.
It does not set proficiency levels. The proposal is schema-checked by the
service/domain boundary before it is shown to the employee.
"""
from __future__ import annotations

from copy import deepcopy

MAX_STEPS = 8

# Synthetic catalogue records. In production these rows come from the approved
# Training Catalogue / Workday-aligned mapping and curated SOP links.
TRAINING_CATALOGUE = [
    {
        "id": "TRN_AP_001",
        "title": "AP Controls Microlearning",
        "skill": "AP Controls & Compliance",
        "framework": "FinOps",
        "type": "Microlearning",
        "duration": "35 min",
        "deep_link": "/learning/catalog/TRN_AP_001",
        "descriptor_clause": "FinOps L3–L4: apply approval, exception and evidence controls consistently.",
        "sop_reference": "AP-CTRL-04 · High-value invoice approval and audit trail",
        "sequence": 1,
    },
    {
        "id": "TRN_AP_002",
        "title": "Advanced AP Controls",
        "skill": "AP Controls & Compliance",
        "framework": "FinOps",
        "type": "Course",
        "duration": "2.5 hrs",
        "deep_link": "/learning/catalog/TRN_AP_002",
        "descriptor_clause": "FinOps L4: design, challenge and evidence control effectiveness for complex exceptions.",
        "sop_reference": "AP-CTRL-09 · Maker-checker for master data",
        "sequence": 2,
    },
    {
        "id": "TRN_AP_003",
        "title": "AP Exception Case Lab",
        "skill": "AP Controls & Compliance",
        "framework": "FinOps",
        "type": "Case practice",
        "duration": "60 min",
        "deep_link": "/learning/catalog/TRN_AP_003",
        "descriptor_clause": "FinOps L4: resolve control exceptions using documented evidence and escalation paths.",
        "sop_reference": "AP-CTRL-04 · High-value invoice approval and audit trail",
        "sequence": 3,
    },
    {
        "id": "TRN_R2R_001",
        "title": "Bank Reconciliation Workshop",
        "skill": "Bank Reconciliations (SOX/IPE)",
        "framework": "FinOps",
        "type": "Workshop",
        "duration": "3 hrs",
        "deep_link": "/learning/catalog/TRN_R2R_001",
        "descriptor_clause": "FinOps L4: investigate aged reconciling items and document independent review evidence.",
        "sop_reference": "R2R-REC-05 · Aged reconciling item escalation",
        "sequence": 1,
    },
    {
        "id": "TRN_R2R_002",
        "title": "Reconciliation Controls Practice",
        "skill": "Bank Reconciliations (SOX/IPE)",
        "framework": "FinOps",
        "type": "Case practice",
        "duration": "75 min",
        "deep_link": "/learning/catalog/TRN_R2R_002",
        "descriptor_clause": "FinOps L3–L4: identify variances, assess risk and retain reconciliation support.",
        "sop_reference": "R2R-REC-02 · Reconciliation review control",
        "sequence": 2,
    },
    {
        "id": "TRN_RD_001",
        "title": "Stakeholder Communication Practice",
        "skill": "Stakeholder Communication",
        "framework": "RD",
        "type": "Practice lab",
        "duration": "60 min",
        "deep_link": "/learning/catalog/TRN_RD_001",
        "descriptor_clause": "RD Moderate–Expert: adapt communication to stakeholder needs and drive a clear outcome.",
        "sop_reference": "RD-COMP-02 · Stakeholder communication indicator",
        "sequence": 1,
    },
    {
        "id": "TRN_RD_002",
        "title": "Cross-Functional Influence Lab",
        "skill": "Stakeholder Communication",
        "framework": "RD",
        "type": "Case practice",
        "duration": "90 min",
        "deep_link": "/learning/catalog/TRN_RD_002",
        "descriptor_clause": "RD Expert: tailor communication, manage competing needs and sustain stakeholder alignment.",
        "sop_reference": "RD-COMP-02 · Stakeholder communication indicator",
        "sequence": 2,
    },
]

CURATED_SOP_LINKS = {
    "AP-CTRL-04": {"title": "High-value invoice approval and audit trail", "deep_link": "/knowledge/sops/AP-CTRL-04"},
    "AP-CTRL-09": {"title": "Maker-checker for master data", "deep_link": "/knowledge/sops/AP-CTRL-09"},
    "R2R-REC-02": {"title": "Reconciliation review control", "deep_link": "/knowledge/sops/R2R-REC-02"},
    "R2R-REC-05": {"title": "Aged reconciling item escalation", "deep_link": "/knowledge/sops/R2R-REC-05"},
    "RD-COMP-02": {"title": "Stakeholder communication indicator", "deep_link": "/knowledge/sops/RD-COMP-02"},
}


def _catalogue_for(skill: str, framework: str) -> list[dict]:
    return [
        deepcopy(item)
        for item in TRAINING_CATALOGUE
        if item["skill"] == skill and item["framework"].lower() == framework.lower()
    ]


def _priority(gap: int, criticality: str = "Medium") -> str:
    if gap >= 2 or criticality.lower() == "high":
        return "High"
    if gap == 1:
        return "Medium"
    return "Low"


def build_proposal(gaps: list[dict]) -> dict:
    """Generate a sequenced A5 proposal from already-authoritative gaps."""
    ordered = sorted(
        [deepcopy(g) for g in gaps if int(g.get("gap", 0)) > 0],
        key=lambda g: (-int(g.get("gap", 0)), 0 if g.get("criticality") == "High" else 1, g.get("skill", "")),
    )
    steps: list[dict] = []
    gap_summaries = []
    for gap in ordered:
        skill = gap["skill"]
        framework = gap["framework"]
        catalogue = _catalogue_for(skill, framework)
        if not catalogue:
            catalogue = [{
                "id": f"CURATED-{len(steps)+1:03d}",
                "title": f"Curated {skill} development",
                "type": "Curated learning",
                "duration": "60 min",
                "deep_link": f"/learning/catalog/{skill.lower().replace(' ', '-')}",
                "descriptor_clause": f"{framework}: close the approved descriptor gap for {skill}.",
                "sop_reference": "Curated SOP reference required",
                "sequence": 1,
            }]
        priority = _priority(int(gap["gap"]), gap.get("criticality", "Medium"))
        gap_summaries.append({
            "skill": skill,
            "framework": framework,
            "current": gap["current"],
            "target": gap["target"],
            "gap": gap["gap"],
            "priority": priority,
            "source": gap.get("source", "domain"),
        })
        for item in sorted(catalogue, key=lambda x: x.get("sequence", 99)):
            if len(steps) >= MAX_STEPS:
                break
            sop_code = (item.get("sop_reference", "").split(" · ", 1)[0] or "").strip()
            sop = CURATED_SOP_LINKS.get(sop_code, {"title": item.get("sop_reference", "Curated SOP"), "deep_link": ""})
            steps.append({
                "step": len(steps) + 1,
                "skill": skill,
                "framework": framework,
                "priority": priority,
                "title": item["title"],
                "type": item["type"],
                "duration": item["duration"],
                "training_link": item["deep_link"],
                "sop_reference": item.get("sop_reference", "Curated SOP"),
                "sop_link": sop.get("deep_link", ""),
                "descriptor_clause": item["descriptor_clause"],
                "why": f"Closes the {gap['current']} → {gap['target']} gap for {skill}.",
            })

    total_minutes = 0
    for step in steps:
        value = step["duration"].split()[0]
        try:
            total_minutes += int(float(value))
        except ValueError:
            pass
    return {
        "status": "Proposed" if steps else "No Confirmed Gaps",
        "revision": 0,
        "gaps": gap_summaries,
        "steps": steps,
        "total_steps": len(steps),
        "estimated_minutes": total_minutes,
        "agent_note": "A5 proposes a sequence from approved learning/SOP mappings. It does not set proficiency levels.",
    }


def revise_proposal(proposal: dict) -> dict:
    """One bounded revision: preserve priority and remove duplicate skill steps."""
    revised = deepcopy(proposal)
    seen = set()
    kept = []
    for step in revised.get("steps", []):
        key = (step["skill"], step["title"])
        if key in seen:
            continue
        seen.add(key)
        kept.append(step)
    for index, step in enumerate(kept, start=1):
        step["step"] = index
    revised["steps"] = kept
    revised["total_steps"] = len(kept)
    revised["revision"] = 1
    return revised
