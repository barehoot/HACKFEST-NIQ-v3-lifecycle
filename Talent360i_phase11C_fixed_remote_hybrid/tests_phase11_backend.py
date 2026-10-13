"""Phase 11 contract tests for the supplied backend team's API."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from services.backend_api import BackendAPI


def test_backend_endpoint_defaults():
    api = BackendAPI()
    assert api._endpoint("generate", "/api/generate") == "/api/generate"
    assert api._endpoint("score", "/api/score") == "/api/score"
    assert api._endpoint("case_studies", "/api/case-studies") == "/api/case-studies"
    assert api._endpoint("evidence", "/api/evidence") == "/api/evidence"


def test_backend_score_payload(monkeypatch):
    api = BackendAPI()
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update(method=method, path=path, body=kwargs["body"])
        return {"overall_score_pct": 100}

    api.request = fake_request
    result = api.score([{"question_id": "Q1"}], {"Q1": "A"})
    assert result["overall_score_pct"] == 100
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/score"
    assert captured["body"]["answers"] == {"Q1": "A"}


def test_backend_case_study_payload(monkeypatch):
    api = BackendAPI()
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update(method=method, path=path, body=kwargs["body"])
        return {"case_studies": []}

    api.request = fake_request
    api.case_studies("ROLE-1", {"overall_score_pct": 50}, 3)
    assert captured["path"] == "/api/case-studies"
    assert captured["body"]["role_id"] == "ROLE-1"
    assert captured["body"]["max_questions"] == 3


def test_backend_evidence_payload(monkeypatch):
    api = BackendAPI()
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update(method=method, path=path, body=kwargs["body"])
        return {"manager_review_status": "Pending Manager Review"}

    api.request = fake_request
    api.evidence([], {"Q1": "answer"}, {"overall_score_pct": 50}, "ROLE-1", "Role", "B3")
    assert captured["path"] == "/api/evidence"
    assert captured["body"]["role_name"] == "Role"
    assert captured["body"]["role_grade"] == "B3"


if __name__ == "__main__":
    test_backend_endpoint_defaults()
    test_backend_score_payload(None)
    test_backend_case_study_payload(None)
    test_backend_evidence_payload(None)
    print("Phase 11 backend contract tests: PASS")
