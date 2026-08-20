"""
main.py
FastAPI backend for ResumeMax.

The Gemini API key lives only on the server (env var / .env file) — the
frontend never sees it and never asks the user to type one in.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import engine

logger = logging.getLogger("resumemax.main")

load_dotenv()  # reads GEMINI_API_KEY from a .env file in backend/, if present

# Optional error monitoring — completely inert unless SENTRY_DSN is set, so
# this needs zero setup until/unless you create a Sentry account and add
# the DSN to your environment.
_SENTRY_DSN = os.getenv("SENTRY_DSN")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=_SENTRY_DSN, traces_sample_rate=0.1)
    except ImportError:
        pass  # sentry-sdk not installed — fine, just means no error reporting

app = FastAPI(title="ResumeMax API")

# CORS origin(s) — comma-separated in ALLOWED_ORIGINS, e.g.
# "https://resumemax.onrender.com,http://localhost:8000". Defaults to "*"
# for local development convenience; set this explicitly once deployed.
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
_allowed_origins = (
    ["*"] if _allowed_origins_env.strip() == "*"
    else [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """
    Baseline security headers (checklist items: block field tampering via
    strict CSP script-src, add security headers, force HTTPS via HSTS).
    Kept permissive enough to allow Google Fonts, since that's the one
    external resource this frontend actually loads.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    # HSTS only makes sense once actually served over HTTPS (Render does
    # this automatically) — harmless to send locally over http too, browsers
    # just ignore it on non-HTTPS origins.
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response

# Rate limiting — protects against a runaway client (or abuse) hammering
# the Gemini-backed endpoints and running up your API bill. Defaults are
# generous enough for normal use; tune via env vars if needed.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15MB — matches the limit shown in the UI
MAX_PROMPT_CHARS = 4000  # must match the truncation length used in engine.py's prompts


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AnalyzeResponse(BaseModel):
    score: float
    matched: list[str]
    missing: list[str]
    job_phrases: list[str]
    extraction_method: str
    extraction_warning: Optional[str] = None
    resume_text: str
    job_description: str
    resume_truncated: bool = False
    job_description_truncated: bool = False


class AISuggestRequest(BaseModel):
    resume_text: str = Field(..., max_length=50_000)
    job_description: str = Field(..., max_length=50_000)
    model: str = engine.DEFAULT_MODEL


class BulletRewriteOut(BaseModel):
    original: str
    rewritten: str
    reasoning: str


class ReadinessRoadmapOut(BaseModel):
    recommended_projects: list[str] = []
    skills_to_learn: list[str] = []
    experience_gaps: str = ""


class PathPhaseOut(BaseModel):
    title: str = ""
    duration_estimate: str = ""
    actions: list[str] = []


class SuggestedPathOut(BaseModel):
    overview: str = ""
    phases: list[PathPhaseOut] = []


class AISuggestResponse(BaseModel):
    ok: bool
    summary: str = ""
    missing_keywords: list[str] = []
    bullet_rewrites: list[BulletRewriteOut] = []
    writing_enhancements: list[str] = []
    readiness_roadmap: ReadinessRoadmapOut = ReadinessRoadmapOut()
    suggested_path: SuggestedPathOut = SuggestedPathOut()
    error: Optional[str] = None


class ExportRequest(BaseModel):
    score: float
    matched: list[str]
    missing: list[str]
    job_title: str = Field("Target Role", max_length=200)
    ai_summary: str = Field("", max_length=5_000)
    ai_missing_keywords: list[str] = []
    ai_bullet_rewrites: list[BulletRewriteOut] = []
    ai_writing_enhancements: list[str] = []
    ai_readiness_roadmap: ReadinessRoadmapOut = ReadinessRoadmapOut()
    ai_suggested_path: SuggestedPathOut = SuggestedPathOut()
    ai_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    """Frontend uses this to know whether AI suggestions are available at all."""
    return {
        "ok": True,
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
        "ocr_available": engine.OCR_AVAILABLE,
        "pdf_export_available": engine.REPORTLAB_AVAILABLE,
    }


@app.post("/api/analyze", response_model=AnalyzeResponse)
@limiter.limit("15/minute")
async def analyze(
    request: Request,
    resume: UploadFile = File(...),
    job_description: str = Form(..., max_length=50_000),
    ocr_fallback: bool = Form(True),
):
    if not job_description.strip():
        raise HTTPException(400, "Job description is empty.")

    if resume.content_type != "application/pdf":
        raise HTTPException(415, "Only PDF files are accepted.")

    # Read in chunks and bail out early if the upload exceeds the limit,
    # rather than trusting the client-side "15MB" hint or the Content-Length
    # header (which a client can lie about).
    chunks = []
    total_size = 0
    while True:
        chunk = await resume.read(1024 * 1024)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"File too large — max {MAX_UPLOAD_BYTES // (1024*1024)}MB.")
        chunks.append(chunk)
    raw = b"".join(chunks)

    # A PDF's magic bytes always start with %PDF — catches a file that was
    # merely renamed to .pdf rather than actually being one.
    if not raw.startswith(b"%PDF"):
        raise HTTPException(415, "This file doesn't look like a valid PDF.")

    extraction = engine.extract_text_from_pdf(io.BytesIO(raw), ocr_fallback=ocr_fallback)

    if not extraction.text:
        raise HTTPException(422, extraction.warning or "No text could be extracted from this PDF.")

    job_phrases, matched, missing = engine.diff_phrases(extraction.text, job_description)
    score = engine.calculate_weighted_score(extraction.text, job_description, matched, job_phrases)

    return AnalyzeResponse(
        score=score,
        matched=matched,
        missing=missing,
        job_phrases=job_phrases,
        extraction_method=extraction.method,
        extraction_warning=extraction.warning,
        resume_text=extraction.text,
        job_description=job_description,
        resume_truncated=len(extraction.text) > MAX_PROMPT_CHARS,
        job_description_truncated=len(job_description) > MAX_PROMPT_CHARS,
    )


# Simple in-memory cache for identical (resume, job description, model)
# analyses — avoids paying for a duplicate Gemini call if someone re-runs
# the same scan (easy to do by accident, e.g. double-clicking "Run scan").
# Deliberately basic: process-local, no eviction beyond a hard size cap.
# Fine for a single free-tier instance; would need a shared store (Redis)
# if this ever runs across multiple instances.
_ai_cache: dict[str, "AISuggestResponse"] = {}
_AI_CACHE_MAX_ENTRIES = 200


def _ai_cache_key(resume_text: str, job_description: str, model: str) -> str:
    raw = f"{model}::{resume_text}::{job_description}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@app.post("/api/ai-suggestions", response_model=AISuggestResponse)
@limiter.limit("10/minute")
def ai_suggestions(request: Request, req: AISuggestRequest):
    cache_key = _ai_cache_key(req.resume_text, req.job_description, req.model)
    if cache_key in _ai_cache:
        return _ai_cache[cache_key]

    # engine.get_ai_suggestions already handles retries, model fallback, and
    # returns a structured AISuggestions(error=...) instead of raising for
    # any Gemini-side failure. This try/except is a last-resort safety net
    # for anything unexpected outside that (e.g. a bug), so the frontend
    # always gets a well-formed AISuggestResponse rather than a raw 500.
    try:
        result = engine.get_ai_suggestions(req.resume_text, req.job_description, model=req.model)
    except Exception as e:
        # Full detail stays in server logs only — never echo raw exception
        # text back to the client, since it can leak internal paths, library
        # versions, or other implementation details useful to an attacker.
        logger.exception("Unexpected error in /api/ai-suggestions")
        return AISuggestResponse(ok=False, error="Something went wrong generating suggestions. Try again.")

    response = AISuggestResponse(
        ok=result.ok,
        summary=result.summary,
        missing_keywords=result.missing_keywords,
        bullet_rewrites=[
            BulletRewriteOut(original=b.original, rewritten=b.rewritten, reasoning=b.reasoning)
            for b in result.bullet_rewrites
        ],
        writing_enhancements=result.writing_enhancements,
        readiness_roadmap=ReadinessRoadmapOut(
            recommended_projects=result.readiness_roadmap.recommended_projects,
            skills_to_learn=result.readiness_roadmap.skills_to_learn,
            experience_gaps=result.readiness_roadmap.experience_gaps,
        ),
        suggested_path=SuggestedPathOut(
            overview=result.suggested_path.overview,
            phases=[
                PathPhaseOut(title=p.title, duration_estimate=p.duration_estimate, actions=p.actions)
                for p in result.suggested_path.phases
            ],
        ),
        error=result.error,
    )

    # Only cache genuine successes — a transient failure shouldn't get
    # "stuck" as the cached answer for the next identical request.
    if result.ok:
        if len(_ai_cache) >= _AI_CACHE_MAX_ENTRIES:
            _ai_cache.pop(next(iter(_ai_cache)))  # evict oldest (insertion order)
        _ai_cache[cache_key] = response

    return response


def _to_ai_suggestions(req: ExportRequest) -> engine.AISuggestions:
    return engine.AISuggestions(
        summary=req.ai_summary,
        missing_keywords=req.ai_missing_keywords,
        bullet_rewrites=[
            engine.BulletRewrite(original=b.original, rewritten=b.rewritten, reasoning=b.reasoning)
            for b in req.ai_bullet_rewrites
        ],
        writing_enhancements=req.ai_writing_enhancements,
        readiness_roadmap=engine.ReadinessRoadmap(
            recommended_projects=req.ai_readiness_roadmap.recommended_projects,
            skills_to_learn=req.ai_readiness_roadmap.skills_to_learn,
            experience_gaps=req.ai_readiness_roadmap.experience_gaps,
        ),
        suggested_path=engine.SuggestedPath(
            overview=req.ai_suggested_path.overview,
            phases=[
                engine.PathPhase(title=p.title, duration_estimate=p.duration_estimate, actions=p.actions)
                for p in req.ai_suggested_path.phases
            ],
        ),
        error=req.ai_error,
    )


# ---------------------------------------------------------------------------
# Follow-up chat
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str = Field(..., max_length=5_000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., max_length=50)
    resume_text: str = Field(..., max_length=50_000)
    job_description: str = Field(..., max_length=50_000)
    analysis_summary: str = Field("", max_length=2_000)


class ChatResponse(BaseModel):
    ok: bool
    reply: str = ""
    error: Optional[str] = None


@app.post("/api/chat", response_model=ChatResponse)
@limiter.limit("20/minute")
def chat(request: Request, req: ChatRequest):
    try:
        result = engine.chat_about_analysis(
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
            resume_text=req.resume_text,
            job_description=req.job_description,
            analysis_summary=req.analysis_summary,
        )
    except Exception as e:
        logger.exception("Unexpected error in /api/chat")
        return ChatResponse(ok=False, error="Something went wrong. Try again.")

    return ChatResponse(ok=result.ok, reply=result.reply, error=result.error)


@app.post("/api/export/markdown")
def export_markdown(req: ExportRequest):
    ai = _to_ai_suggestions(req)
    md = engine.generate_markdown_report(req.score, req.matched, req.missing, ai, req.job_title)
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": "attachment; filename=resumemax_report.md"},
    )


@app.post("/api/export/pdf")
def export_pdf(req: ExportRequest):
    if not engine.REPORTLAB_AVAILABLE:
        raise HTTPException(501, "PDF export isn't available on this server (reportlab not installed).")
    ai = _to_ai_suggestions(req)
    pdf_bytes = engine.generate_pdf_report(req.score, req.matched, req.missing, ai, req.job_title)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=resumemax_report.pdf"},
    )


# ---------------------------------------------------------------------------
# Apply rewrites back to the resume
# ---------------------------------------------------------------------------

class ApplyRewritesRequest(BaseModel):
    resume_text: str = Field(..., max_length=50_000)
    bullet_rewrites: list[BulletRewriteOut] = Field(..., max_length=20)


class ApplyRewritesResponse(BaseModel):
    updated_resume_text: str
    applied_count: int
    total_count: int
    unmatched: list[str] = []


@app.post("/api/apply-rewrites", response_model=ApplyRewritesResponse)
@limiter.limit("15/minute")
def apply_rewrites(request: Request, req: ApplyRewritesRequest):
    rewrites = [
        engine.BulletRewrite(original=b.original, rewritten=b.rewritten, reasoning=b.reasoning)
        for b in req.bullet_rewrites
    ]
    result = engine.apply_rewrites_to_resume(req.resume_text, rewrites)
    return ApplyRewritesResponse(
        updated_resume_text=result.updated_text,
        applied_count=len(result.applied),
        total_count=len(rewrites),
        unmatched=result.unmatched,
    )


class ExportResumeRequest(BaseModel):
    resume_text: str = Field(..., max_length=50_000)


@app.post("/api/export/resume-pdf")
def export_resume_pdf(req: ExportResumeRequest):
    if not engine.REPORTLAB_AVAILABLE:
        raise HTTPException(501, "PDF export isn't available on this server (reportlab not installed).")
    pdf_bytes = engine.generate_resume_pdf(req.resume_text)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=resumemax_updated_resume.pdf"},
    )


@app.post("/api/export/resume-txt")
def export_resume_txt(req: ExportResumeRequest):
    return Response(
        content=req.resume_text,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=resumemax_updated_resume.txt"},
    )


# ---------------------------------------------------------------------------
# Static frontend (served from the same origin, so no separate frontend
# server or CORS setup is needed for normal use).
# ---------------------------------------------------------------------------
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
