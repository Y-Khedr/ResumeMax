"""API-level tests: real FastAPI app, real multipart PDF upload, Gemini mocked."""

import io

import pytest
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

import engine
import main


def _pdf_bytes(lines):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 740
    for line in lines:
        c.drawString(50, y, line)
        y -= 16
    c.save()
    return buf.getvalue()


RESUME_LINES = [
    "Jordan Lee - UI/UX Designer",
    "Building and maintaining a scalable design system used across 5 product teams.",
    "Conducted user research and usability testing to inform wireframes.",
]
JOB = "We need someone scaling design systems and running usability testing. Kubernetes is a plus."


@pytest.fixture
def client():
    main._match_cache.clear()
    main.limiter.reset()
    return TestClient(main.app)


def _post(client, job=JOB):
    return client.post(
        "/api/analyze",
        files={"resume": ("cv.pdf", _pdf_bytes(RESUME_LINES), "application/pdf")},
        data={"job_description": job, "ocr_fallback": "false"},
    )


def _ai_result():
    return engine.RequirementMatchResult(requirements=[
        engine.RequirementMatch("Design Systems", "required", True, "scalable design system"),
        engine.RequirementMatch("Usability Testing", "required", True, "usability testing"),
        engine.RequirementMatch("Kubernetes", "preferred", False, ""),
    ])


def test_analyze_uses_ai_matching_when_available(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(engine, "GENAI_AVAILABLE", True)
    monkeypatch.setattr(engine, "match_requirements_with_ai", lambda *a, **k: _ai_result())
    r = _post(client)
    assert r.status_code == 200
    body = r.json()
    assert body["match_mode"] == "ai"
    assert body["matched"] == ["Design Systems", "Usability Testing"]
    assert body["missing"] == ["Kubernetes"]
    assert body["score"] == 80.0  # required 2.0 of 2.5 possible
    assert body["requirements"][0]["evidence"] == "scalable design system"
    assert "unavailable" not in (body["extraction_warning"] or "")


def test_analyze_falls_back_to_keywords_and_says_so_when_ai_fails(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(engine, "GENAI_AVAILABLE", True)
    monkeypatch.setattr(engine, "match_requirements_with_ai",
                        lambda *a, **k: engine.RequirementMatchResult(error="boom"))
    r = _post(client)
    assert r.status_code == 200
    body = r.json()
    assert body["match_mode"] == "lexical"
    assert "keyword matching" in body["extraction_warning"]
    assert body["requirements"] == []


def test_analyze_without_a_key_uses_keyword_mode(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = _post(client)
    assert r.status_code == 200
    body = r.json()
    assert body["match_mode"] == "lexical"
    assert "keyword matching" in body["extraction_warning"]
    # the keyword matcher still finds skills that are literally in the resume,
    # including singular-vs-plural ("design system" vs "design systems")
    matched = {m.lower() for m in body["matched"]}
    assert {"design systems", "usability testing"} <= matched


def test_successful_ai_matches_are_cached_but_failures_are_not(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(engine, "GENAI_AVAILABLE", True)

    calls = {"n": 0}
    def ok(*a, **k):
        calls["n"] += 1
        return _ai_result()
    monkeypatch.setattr(engine, "match_requirements_with_ai", ok)
    _post(client); _post(client)
    assert calls["n"] == 1, "an identical scan should not pay for a second model call"

    main._match_cache.clear()
    calls["n"] = 0
    def fail(*a, **k):
        calls["n"] += 1
        return engine.RequirementMatchResult(error="transient")
    monkeypatch.setattr(engine, "match_requirements_with_ai", fail)
    _post(client); _post(client)
    assert calls["n"] == 2, "a failure must not get cached"


def test_analyze_rejects_a_non_pdf_upload(client):
    r = client.post(
        "/api/analyze",
        files={"resume": ("cv.pdf", b"definitely not a pdf", "application/pdf")},
        data={"job_description": "x"},
    )
    assert r.status_code == 415


def test_analyze_rejects_an_empty_job_description(client):
    assert _post(client, job="   ").status_code == 400


def test_health_reports_configuration(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert {"ok", "gemini_configured", "ocr_available", "pdf_export_available"} <= set(r.json())


# ---------------------------------------------------------------------------
# /api/cover-letter
# ---------------------------------------------------------------------------

def _letter_body(**over):
    body = {"resume_text": "Jordan Lee. Designer.", "job_description": "Product designer role."}
    body.update(over)
    return body


def test_cover_letter_endpoint_returns_the_letter(client, monkeypatch):
    captured = {}
    def fake(**kw):
        captured.update(kw)
        return engine.CoverLetterResult(letter="Dear Hiring Manager,\nHello.\nSincerely,\nJordan Lee")
    monkeypatch.setattr(engine, "generate_cover_letter", fake)
    r = client.post("/api/cover-letter", json=_letter_body(tone="confident", analysis_summary="Strong fit."))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["letter"].startswith("Dear Hiring Manager")
    assert captured["tone"] == "confident" and captured["analysis_summary"] == "Strong fit."


def test_cover_letter_endpoint_surfaces_a_generation_error(client, monkeypatch):
    monkeypatch.setattr(engine, "generate_cover_letter",
                        lambda **kw: engine.CoverLetterResult(error="Gemini is temporarily overloaded."))
    r = client.post("/api/cover-letter", json=_letter_body())
    assert r.status_code == 200
    assert r.json() == {"ok": False, "letter": "", "error": "Gemini is temporarily overloaded."}


def test_cover_letter_endpoint_hides_unexpected_exception_details(client, monkeypatch):
    def boom(**kw):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(engine, "generate_cover_letter", boom)
    r = client.post("/api/cover-letter", json=_letter_body())
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "secret internal detail" not in r.text


def test_cover_letter_endpoint_validates_input(client):
    assert client.post("/api/cover-letter", json=_letter_body(resume_text="")).status_code == 422
    assert client.post("/api/cover-letter", json=_letter_body(job_description="x" * 50_001)).status_code == 422
    assert client.post("/api/cover-letter", json={"resume_text": "only one field"}).status_code == 422


# ---------------------------------------------------------------------------
# /api/cover-letter
# ---------------------------------------------------------------------------

COVER_PAYLOAD = {"resume_text": "Jordan Lee. Figma, design systems.", "job_description": "Product Designer"}


def test_cover_letter_endpoint_returns_the_letter(client, monkeypatch):
    monkeypatch.setattr(engine, "generate_cover_letter",
                        lambda **k: engine.CoverLetterResult(letter="Dear Hiring Manager,\n\nHello."))
    r = client.post("/api/cover-letter", json=COVER_PAYLOAD)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "letter": "Dear Hiring Manager,\n\nHello.", "error": None}


def test_cover_letter_endpoint_passes_tone_and_context_through(client, monkeypatch):
    seen = {}
    def fake(**kwargs):
        seen.update(kwargs)
        return engine.CoverLetterResult(letter="ok")
    monkeypatch.setattr(engine, "generate_cover_letter", fake)
    client.post("/api/cover-letter", json={**COVER_PAYLOAD, "tone": "technical", "analysis_summary": "Strong fit."})
    assert seen["tone"] == "technical"
    assert seen["analysis_summary"] == "Strong fit."
    assert seen["resume_text"].startswith("Jordan Lee")


def test_cover_letter_endpoint_surfaces_engine_errors_without_a_500(client, monkeypatch):
    monkeypatch.setattr(engine, "generate_cover_letter",
                        lambda **k: engine.CoverLetterResult(error="Gemini is temporarily overloaded."))
    r = client.post("/api/cover-letter", json=COVER_PAYLOAD)
    assert r.status_code == 200
    assert r.json()["ok"] is False and "overloaded" in r.json()["error"]


def test_cover_letter_endpoint_hides_unexpected_exceptions(client, monkeypatch):
    def boom(**k):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(engine, "generate_cover_letter", boom)
    r = client.post("/api/cover-letter", json=COVER_PAYLOAD)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "secret internal detail" not in r.text


def test_cover_letter_endpoint_validates_input(client):
    assert client.post("/api/cover-letter", json={**COVER_PAYLOAD, "resume_text": ""}).status_code == 422
    assert client.post("/api/cover-letter", json={"resume_text": "x"}).status_code == 422
    assert client.post("/api/cover-letter",
                       json={**COVER_PAYLOAD, "resume_text": "x" * 60_000}).status_code == 422
