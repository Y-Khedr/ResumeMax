"""
tests/test_engine.py
Run with: pytest tests/ -v

Covers:
  - Phrase extraction and diffing (the deterministic ATS scoring logic)
  - Retry / model-fallback behavior for Gemini calls (mocked — no real API calls)
  - Export generation (Markdown + PDF)
"""

import json
import types

import pytest

import engine


# ---------------------------------------------------------------------------
# Phrase extraction & scoring
# ---------------------------------------------------------------------------

def test_gazetteer_matches_multiword_phrases():
    text = "Experienced with Java Spring Boot, AWS EC2, and CI/CD Pipelines."
    phrases = engine.extract_key_phrases(text)
    assert "Java Spring Boot" in phrases
    assert "CI/CD Pipelines" in phrases


def test_ngrams_dont_cross_comma_boundaries():
    # Regression test: this used to produce garbage like "boot aws lambda"
    # by treating a comma-separated list as one continuous token stream.
    text = "Java Spring Boot, AWS Lambda, Kubernetes, GraphQL, and Terraform"
    phrases = engine.extract_key_phrases(text)
    joined = " | ".join(phrases).lower()
    assert "boot aws" not in joined
    assert "lambda kubernetes" not in joined


def test_dedupe_drops_fragments_of_longer_phrase():
    text = "Experienced in Java Spring Boot development."
    phrases = engine.extract_key_phrases(text)
    # "spring boot" / "boot" / "java spring" shouldn't linger once the full
    # gazetteer phrase is captured.
    assert "spring boot" not in {p.lower() for p in phrases if p.lower() != "java spring boot"}


def test_diff_phrases_matched_and_missing():
    resume = "Skilled in Python, Docker, and PostgreSQL."
    job = "Looking for Python, Docker, Kubernetes, and Terraform experience."
    job_phrases, matched, missing = engine.diff_phrases(resume, job)
    matched_lower = {m.lower() for m in matched}
    missing_lower = {m.lower() for m in missing}
    assert "python" in matched_lower
    assert "docker" in matched_lower
    assert "kubernetes" in missing_lower
    assert "terraform" in missing_lower


def test_ats_score_empty_inputs_return_zero():
    assert engine.calculate_ats_score("", "something") == 0.0
    assert engine.calculate_ats_score("something", "") == 0.0


def test_weighted_score_blends_coverage():
    score_full_coverage = engine.calculate_weighted_score(
        "python docker kubernetes", "python docker kubernetes",
        matched=["python", "docker", "kubernetes"], job_phrases=["python", "docker", "kubernetes"],
    )
    score_no_coverage = engine.calculate_weighted_score(
        "unrelated text entirely", "python docker kubernetes",
        matched=[], job_phrases=["python", "docker", "kubernetes"],
    )
    assert score_full_coverage > score_no_coverage


def test_keywords_to_sentence_formatting():
    assert engine._keywords_to_sentence([]) == "None found."
    assert engine._keywords_to_sentence(["Docker"]) == "To strengthen this application, focus on: Docker."
    assert "and" in engine._keywords_to_sentence(["Docker", "AWS"])
    three = engine._keywords_to_sentence(["Docker", "AWS", "Kubernetes"])
    assert three == "To strengthen this application, focus on: Docker, AWS, and Kubernetes."


# ---------------------------------------------------------------------------
# Gemini retry / fallback logic (mocked client — no real network calls)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, call_log, behavior):
        self.call_log = call_log
        self.behavior = behavior  # dict: model -> list of outcomes, popped in order

    def generate_content(self, model, contents, config=None):
        self.call_log.append(model)
        outcome = self.behavior[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(json.dumps(outcome) if isinstance(outcome, dict) else outcome)


class _FakeClient:
    def __init__(self, models):
        self.models = models


VALID_AI_PAYLOAD = {
    "summary": "Good fit.",
    "missing_keywords": ["Docker"],
    "bullet_rewrites": [],
    "writing_enhancements": ["Use metrics"],
    "readiness_roadmap": {"recommended_projects": ["X"], "skills_to_learn": ["Y"], "experience_gaps": "Z"},
    "suggested_path": {"overview": "About 2 months out.", "phases": []},
}


@pytest.fixture
def fake_genai(monkeypatch):
    """Patches engine.genai so get_ai_suggestions/chat_about_analysis never hit the real network."""
    monkeypatch.setattr(engine, "GENAI_AVAILABLE", True)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)  # skip real backoff waits in tests

    def _install(behavior):
        call_log = []
        monkeypatch.setattr(
            engine, "genai",
            types.SimpleNamespace(Client=lambda api_key: _FakeClient(_FakeModels(call_log, behavior))),
        )
        monkeypatch.setattr(
            engine, "genai_types",
            types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw),
        )
        return call_log

    return _install


def test_succeeds_on_first_try(fake_genai):
    call_log = fake_genai({engine.DEFAULT_MODEL: [VALID_AI_PAYLOAD]})
    result = engine.get_ai_suggestions("resume", "job", api_key="fake-key")
    assert result.ok
    assert result.summary == "Good fit."
    assert call_log == [engine.DEFAULT_MODEL]


def test_falls_back_after_transient_failures(fake_genai):
    call_log = fake_genai({
        engine.DEFAULT_MODEL: [Exception("503 UNAVAILABLE")] * 3,
        engine.FALLBACK_MODEL: [VALID_AI_PAYLOAD],
    })
    result = engine.get_ai_suggestions("resume", "job", api_key="fake-key")
    assert result.ok
    assert call_log == [engine.DEFAULT_MODEL] * 3 + [engine.FALLBACK_MODEL]


def test_permanent_error_does_not_retry_or_fallback(fake_genai):
    call_log = fake_genai({
        engine.DEFAULT_MODEL: [Exception("401 UNAUTHENTICATED: API key not valid")],
        engine.FALLBACK_MODEL: [],
    })
    result = engine.get_ai_suggestions("resume", "job", api_key="fake-key")
    assert not result.ok
    assert call_log == [engine.DEFAULT_MODEL]


def test_both_models_exhausted_returns_graceful_error(fake_genai):
    call_log = fake_genai({
        engine.DEFAULT_MODEL: [Exception("503 UNAVAILABLE")] * 3,
        engine.FALLBACK_MODEL: [Exception("503 UNAVAILABLE")] * 2,
    })
    result = engine.get_ai_suggestions("resume", "job", api_key="fake-key")
    assert not result.ok
    assert "overloaded" in result.error.lower()
    assert len(call_log) == 5


def test_get_ai_suggestions_no_api_key_returns_clear_error(monkeypatch):
    monkeypatch.setattr(engine, "GENAI_AVAILABLE", True)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = engine.get_ai_suggestions("resume", "job", api_key=None)
    assert not result.ok
    assert "GEMINI_API_KEY" in result.error


def test_chat_about_analysis_basic(fake_genai):
    fake_genai({engine.DEFAULT_MODEL: ["Focus on Docker first."]})
    result = engine.chat_about_analysis(
        messages=[{"role": "user", "content": "What should I do first?"}],
        resume_text="resume", job_description="job", api_key="fake-key",
    )
    assert result.ok
    assert result.reply == "Focus on Docker first."


# ---------------------------------------------------------------------------
# Export generation
# ---------------------------------------------------------------------------

def _sample_ai_suggestions():
    return engine.AISuggestions(
        summary="Decent fit.",
        missing_keywords=["Docker", "AWS"],
        bullet_rewrites=[engine.BulletRewrite("old bullet", "new bullet", "clearer impact")],
        writing_enhancements=["Use active verbs"],
        readiness_roadmap=engine.ReadinessRoadmap(["Build an API"], ["Docker"], "No cloud experience"),
        suggested_path=engine.SuggestedPath(
            overview="About 2 months out.",
            phases=[engine.PathPhase("Phase 1: Fundamentals", "2 weeks", ["Learn FastAPI"])],
        ),
    )


def test_markdown_report_contains_all_sections():
    md = engine.generate_markdown_report(45.5, ["Python"], ["Docker", "AWS"], _sample_ai_suggestions(), "Backend Engineer")
    assert "# ResumeMax Gap Report" in md
    assert "Decent fit." in md
    assert "Suggested Path to Get There" in md
    assert "Phase 1: Fundamentals" in md
    # Missing keywords should read as a sentence, not one-per-line bullets.
    assert "Focus on: Docker, AWS" in md or "Focus on: Docker and AWS" in md


def test_pdf_report_generates_nonempty_bytes():
    pdf_bytes = engine.generate_pdf_report(45.5, ["Python"], ["Docker"], _sample_ai_suggestions(), "Backend Engineer")
    assert pdf_bytes is not None
    assert pdf_bytes[:4] == b"%PDF"
    assert len(pdf_bytes) > 500


def test_markdown_report_handles_failed_ai_gracefully():
    failed_ai = engine.AISuggestions(error="Gemini is temporarily overloaded.")
    md = engine.generate_markdown_report(30.0, [], ["Python"], failed_ai, "Backend Engineer")
    assert "AI suggestions unavailable" in md


# ---------------------------------------------------------------------------
# Apply rewrites back to the resume
# ---------------------------------------------------------------------------

def test_apply_rewrites_exact_match():
    resume = "Built a web app.\nUsed Python and Flask."
    rewrites = [engine.BulletRewrite("Built a web app.", "Engineered a production web app.", "stronger verb")]
    result = engine.apply_rewrites_to_resume(resume, rewrites)
    assert "Engineered a production web app." in result.updated_text
    assert "Built a web app." not in result.updated_text
    assert result.applied == ["Built a web app."]
    assert result.unmatched == []


def test_apply_rewrites_whitespace_normalized_fallback():
    # Simulates a PDF-extraction artifact: a bullet wrapped across a line
    # break with irregular spacing, which won't exact-match the model's
    # (cleanly single-spaced) verbatim quote.
    resume = "Co-developed an AI-integrated  technical solution\nfeaturing an embedded agent."
    rewrites = [engine.BulletRewrite(
        "Co-developed an AI-integrated technical solution featuring an embedded agent.",
        "Engineered an AI-integrated solution with an embedded agent.",
        "clearer",
    )]
    result = engine.apply_rewrites_to_resume(resume, rewrites)
    assert "Engineered an AI-integrated solution" in result.updated_text
    assert len(result.applied) == 1
    assert result.unmatched == []


def test_apply_rewrites_reports_genuinely_unmatched_text():
    resume = "Built a web app."
    rewrites = [engine.BulletRewrite("This text does not appear anywhere.", "Replacement.", "n/a")]
    result = engine.apply_rewrites_to_resume(resume, rewrites)
    assert result.applied == []
    assert result.unmatched == ["This text does not appear anywhere."]
    assert result.updated_text == resume  # unchanged


def test_generate_resume_pdf_produces_valid_pdf():
    pdf_bytes = engine.generate_resume_pdf("Jane Doe\nSoftware Engineer\n\nBuilt things.")
    assert pdf_bytes is not None
    assert pdf_bytes[:4] == b"%PDF"
