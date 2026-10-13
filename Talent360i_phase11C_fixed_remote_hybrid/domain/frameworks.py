"""Framework registry used by the presentation layer.

The HLD defines separate proficiency labels for FinOps and RD. The UI renders
these labels as supplied here and never merges the two scales.
"""

FRAMEWORKS = {
    "finops": {
        "id": "finops",
        "name": "FinOps",
        "level_count": 6,
        "levels": [
            {"ordinal": 0, "code": "L0", "label": "Awareness"},
            {"ordinal": 1, "code": "L1", "label": "Foundation"},
            {"ordinal": 2, "code": "L2", "label": "Working"},
            {"ordinal": 3, "code": "L3", "label": "Proficient"},
            {"ordinal": 4, "code": "L4", "label": "Advanced"},
            {"ordinal": 5, "code": "L5", "label": "Expert"},
        ],
    },
    "rd": {
        "id": "rd",
        "name": "RD",
        "level_count": 3,
        "levels": [
            {"ordinal": 0, "code": "BEGINNER", "label": "Beginner"},
            {"ordinal": 1, "code": "MODERATE", "label": "Moderate"},
            {"ordinal": 2, "code": "EXPERT", "label": "Expert"},
        ],
    },
}


def get_framework(framework_id: str):
    return FRAMEWORKS.get(framework_id)


def framework_options():
    return list(FRAMEWORKS.values())
