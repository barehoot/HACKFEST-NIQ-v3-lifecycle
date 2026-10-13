"""REST adapter for the real Talent 360i backend supplied by the backend team.

Phase 11 integrates the backend contract that is actually present in
HACKFEST-NIQ-2.  The current backend exposes the assessment pipeline through:
  POST /api/generate
  POST /api/score
  POST /api/case-studies
  POST /api/evidence

The Jinja application remains the presentation layer.  LLM credentials are
never sent to the browser; only the server-side adapter talks to the backend.

Configuration:
  TALENT360_API_MODE=mock|remote
  TALENT360_BACKEND_URL=http://127.0.0.1:8000
  TALENT360_BACKEND_TOKEN=optional
  TALENT360_API_TIMEOUT=60
"""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from dotenv import load_dotenv

# Load the frontend project .env before creating the BackendAPI singleton.
# This fixes local runs where variables in .env were not being loaded into
# os.environ, causing the UI to fall back to mock mode.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen


class BackendAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class BackendAPI:
    def __init__(self):
        self.base_url = os.getenv("TALENT360_BACKEND_URL", "").rstrip("/") + "/"
        self.token = os.getenv("TALENT360_BACKEND_TOKEN", "")
        self.timeout = float(os.getenv("TALENT360_API_TIMEOUT", "60"))

    @property
    def enabled(self) -> bool:
        return os.getenv("TALENT360_API_MODE", "mock").lower() == "remote"

    def _check_transport(self) -> None:
        """TLS for anything that leaves the machine (guideline B.1); plain http only to loopback."""
        parts = urlsplit(self.base_url)
        if parts.scheme == "https":
            return
        if parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}:
            return
        # The override only relaxes http-to-a-remote-host; file:// and other
        # schemes are never allowed to reach urlopen().
        if parts.scheme == "http" and os.getenv("TALENT360_ALLOW_INSECURE_BACKEND", "false").lower() == "true":
            return
        raise BackendAPIError("Backend URL must use https:// (plain http is only allowed for localhost).")

    def _endpoint(self, function_name: str, default: str) -> str:
        return os.getenv(f"TALENT360_API_ENDPOINT_{function_name.upper()}", default)

    def _headers(self, *, content_type: str | None = None, headers=None):
        result = {"Accept": "application/json", "User-Agent": "Talent360i-Jinja/11"}
        if content_type:
            result["Content-Type"] = content_type
        if self.token:
            result["Authorization"] = f"Bearer {self.token}"
        if headers:
            result.update({k: str(v) for k, v in headers.items() if v is not None})
        return result

    @staticmethod
    def _decode(raw: str):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"data": raw}

    def request(self, method: str, path: str, *, params=None, body=None, headers=None, timeout: float | None = None):
        if not self.base_url.strip("/"):
            raise BackendAPIError("TALENT360_BACKEND_URL is not configured.")
        self._check_transport()
        url = urljoin(self.base_url, path.lstrip("/"))
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += ("&" if "?" in url else "?") + urlencode(clean, doseq=True)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(url, data=data, headers=self._headers(content_type="application/json" if body is not None else None, headers=headers), method=method.upper())
        try:
            # Scheme limited to http(s) by _check_transport() (bandit B310 reviewed).
            with urlopen(req, timeout=timeout or self.timeout) as response:  # nosec B310
                return self._decode(response.read().decode("utf-8"))
        except HTTPError as exc:
            payload = self._decode(exc.read().decode("utf-8", errors="replace"))
            raise BackendAPIError(f"Backend returned HTTP {exc.code}", exc.code, payload) from exc
        except URLError as exc:
            raise BackendAPIError(f"Backend connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise BackendAPIError("Backend request timed out.") from exc

    def upload_resume(self, file_bytes: bytes, filename: str, *, max_items: int | None = 5, endpoint: str = "generate",
                      keywords: list[str] | None = None, fields: dict[str, str] | None = None):
        """Multipart resume upload: POST /api/generate (match + generate), or
        endpoint="match" for POST /api/match (role match only, no max_items)."""
        self._check_transport()
        # Unpredictable boundary, and a filename stripped to a safe charset, so
        # neither can break out of the multipart framing / header (injection).
        boundary = f"----Talent360i{secrets.token_hex(16)}"
        content_type = "application/octet-stream"
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename or "resume"))
        # The backend declares UploadFile(file=...) and Form(max_items=...), so
        # construct a standards-compliant multipart/form-data request.
        parts = []
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{os.path.basename(filename)}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
            + file_bytes
            + b"\r\n"
        )
        if max_items is not None:
            parts.append(
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="max_items"\r\n\r\n'
                f"{int(max_items)}\r\n".encode("utf-8")
            )
        if keywords:
            # SME coding-challenge triggers; the API only reports which ones the resume mentions.
            clean = ",".join(re.sub(r"[^A-Za-z0-9+#. -]", "", k)[:40] for k in keywords[:50])
            parts.append(
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="probe_keywords"\r\n\r\n'
                f"{clean}\r\n".encode("utf-8")
            )
        for name, value in (fields or {}).items():
            # Fixed field names from this module; values are JSON (no raw CR/LF) and the boundary is random.
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        path = self._endpoint(endpoint, f"/api/{endpoint}")
        url = urljoin(self.base_url, path.lstrip("/"))
        req = Request(
            url,
            data=body,
            headers=self._headers(content_type=f"multipart/form-data; boundary={boundary}"),
            method="POST",
        )
        try:
            # Scheme limited to http(s) by _check_transport() (bandit B310 reviewed).
            with urlopen(req, timeout=self.timeout) as response:  # nosec B310
                return self._decode(response.read().decode("utf-8"))
        except HTTPError as exc:
            payload = self._decode(exc.read().decode("utf-8", errors="replace"))
            raise BackendAPIError(f"Backend returned HTTP {exc.code}", exc.code, payload) from exc
        except URLError as exc:
            raise BackendAPIError(f"Backend connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise BackendAPIError("Backend request timed out.") from exc

    def match_resume(self, file_bytes: bytes, filename: str, keywords: list[str] | None = None):
        """Resume -> matched role only. The SME decides question count and level afterwards."""
        return self.upload_resume(file_bytes, filename, max_items=None, endpoint="match", keywords=keywords)

    def performance_insight(self, payload: dict, mode: str = "insight"):
        """Agent 6: insight, development actions and (mode=review) a review draft, from de-identified signals."""
        return self.request("POST", self._endpoint("performance_insight", "/api/performance-insight"),
                            body={**payload, "mode": mode})

    def classify_evidence(self, text: str, skills: list[str], source: str):
        """Agent 7: classify a tagged email / Teams message into a factual evidence suggestion."""
        return self.request("POST", self._endpoint("evidence_classify", "/api/evidence-classify"),
                            body={"text": text, "skills": skills, "source": source})

    def code_review(self, challenge: dict, code: str, language: str):
        """Agent 4 code reviewer: rubric review of a coding answer. The code is never executed."""
        return self.request("POST", self._endpoint("code_review", "/api/code-review"),
                            body={"challenge": challenge, "code": code, "language": language})

    def resume_skills(self, file_bytes: bytes, filename: str, skills: list[str]):
        """Agent 8 (resume mode): claimed levels for these skills, each quoting the CV. The CV is not kept."""
        return self.upload_resume(file_bytes, filename, max_items=None, endpoint="resume-skills",
                                  fields={"skills": json.dumps([str(s)[:120] for s in skills[:40]])})

    def performance_summary(self, payload: dict):
        """Agent 9: mid-year / annual / 1:1 / promotion summary from a de-identified record."""
        return self.request("POST", self._endpoint("performance_summary", "/api/performance-summary"), body=payload)

    def skill_advice(self, payload: dict):
        """Agent 8: skills a job profile should require (mode=role) or new catalogue skills (mode=catalogue)."""
        return self.request("POST", self._endpoint("skill_advice", "/api/skill-advice"), body=payload)

    def generate_for_role(self, role_id: str, max_items: int, level: int | None = None, skills: list[str] | None = None):
        body = {"role_id": role_id, "max_items": int(max_items)}
        if level:
            body["level"] = int(level)
        if skills:
            body["skills"] = skills
        return self.request("POST", self._endpoint("generate_for_role", "/api/generate-for-role"), body=body)

    def score(self, items, answers):
        return self.request("POST", self._endpoint("score", "/api/score"), body={"items": items, "answers": answers})

    def case_studies(self, role_id, score, max_questions=3):
        return self.request(
            "POST",
            self._endpoint("case_studies", "/api/case-studies"),
            body={"role_id": role_id, "score": score, "max_questions": int(max_questions)},
        )

    def evidence(self, case_studies, answers, score, role_id, role_name, role_grade, transcripts=None, timeout=None):
        body = {
            "case_studies": case_studies,
            "answers": answers,
            "score": score,
            "role_id": role_id,
            "role_name": role_name,
            "role_grade": role_grade,
        }
        if transcripts:
            body["transcripts"] = transcripts
        return self.request("POST", self._endpoint("evidence", "/api/evidence"), body=body, timeout=timeout)

    def probe(self, case_study, transcript):
        """One turn of the Agent 4 simulation: the next probing question, or completion."""
        return self.request(
            "POST",
            self._endpoint("probe", "/api/simulation/probe"),
            body={"case_study": case_study, "transcript": transcript},
        )

    def devplan(self, role_id, score, evidence=None, max_steps=None):
        body = {"role_id": role_id, "score": score}
        if evidence is not None:
            body["evidence"] = evidence
        if max_steps is not None:
            body["max_steps"] = int(max_steps)
        return self.request("POST", self._endpoint("devplan", "/api/devplan"), body=body)

    def training_outline(self, role_id, mode, items):
        """Agent 5's training-session outline for skill-gap or skill-up training."""
        return self.request(
            "POST",
            self._endpoint("training_outline", "/api/training-outline"),
            body={"role_id": role_id, "mode": mode, "items": items},
        )

    @staticmethod
    def unwrap(payload):
        if isinstance(payload, dict):
            for key in ("data", "result", "payload"):
                if key in payload and len(payload) <= 3:
                    return payload[key]
        return payload


backend_api = BackendAPI()

# The full HLD role API is not present in the supplied backend yet. These are
# deliberately limited to the four routes that actually exist in that repo.
ENDPOINTS = {
    "generate": ("POST", "/api/generate"),
    "score": ("POST", "/api/score"),
    "case_studies": ("POST", "/api/case-studies"),
    "evidence": ("POST", "/api/evidence"),
    "probe": ("POST", "/api/simulation/probe"),
    "devplan": ("POST", "/api/devplan"),
}


def call(function_name: str, *, path_params=None, params=None, body=None, headers=None):
    method, default_path = ENDPOINTS[function_name]
    path = backend_api._endpoint(function_name, default_path)
    for key, value in (path_params or {}).items():
        path = path.replace("{" + key + "}", str(value))
    return backend_api.unwrap(backend_api.request(method, path, params=params, body=body, headers=headers))
