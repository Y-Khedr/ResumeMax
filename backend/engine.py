"""
engine.py
Core processing engine for the AI ATS Resume & Skill Optimizer.

Responsibilities:
  - PDF text extraction (with OCR fallback for scanned resumes)
  - Deterministic ATS scoring (TF-IDF cosine similarity, phrase-weighted)
  - Multi-word technical phrase extraction (gazetteer + n-gram TF-IDF)
  - Structured LLM analysis via Gemini (JSON-schema constrained output)
  - Markdown / PDF report export
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pdfplumber
from dotenv import load_dotenv
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger("resumemax.engine")

# Loaded here too (not just in main.py) so GEMINI_API_KEY is available via
# os.getenv() whenever engine.py is imported/used standalone — e.g. scripts,
# tests, or a different entrypoint than the FastAPI app.
load_dotenv()

# ---------------------------------------------------------------------------
# Optional dependencies (OCR + PDF export). Imported lazily / defensively so
# the app degrades gracefully if a deployment environment is missing system
# packages (e.g. tesseract) rather than crashing on import.
# ---------------------------------------------------------------------------
try:
    import pytesseract
    from pdf2image import convert_from_bytes
    OCR_AVAILABLE = True
except ImportError:
    pytesseract = None
    convert_from_bytes = None
    OCR_AVAILABLE = False

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, ListFlowable, ListItem
    )
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    genai = None
    genai_types = None
    GENAI_AVAILABLE = False


# ---------------------------------------------------------------------------
# 1. PDF EXTRACTION (with OCR fallback for scanned / image-based resumes)
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    text: str
    method: str  # "native" | "ocr" | "empty"
    warning: Optional[str] = None


def extract_text_from_pdf(pdf_file, ocr_fallback: bool = True) -> ExtractionResult:
    """
    Extracts raw text from an uploaded PDF. If native text extraction returns
    little/no content (a strong signal the PDF is a scanned image), falls
    back to OCR when the optional OCR stack is available.
    """
    text = ""
    try:
        # pdfplumber needs a fresh stream position each time it's read.
        pdf_file.seek(0)
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
    except Exception as e:
        return ExtractionResult(text="", method="empty", warning=f"Could not parse PDF: {e}")

    text = text.strip()

    # Heuristic: fewer than 40 characters of extracted text almost always
    # means the PDF is a scanned image with no embedded text layer.
    if len(text) >= 40:
        return ExtractionResult(text=text, method="native")

    if not ocr_fallback:
        return ExtractionResult(
            text=text,
            method="empty",
            warning="No selectable text found. This looks like a scanned PDF; enable OCR to extract it.",
        )

    if not OCR_AVAILABLE:
        return ExtractionResult(
            text=text,
            method="empty",
            warning=(
                "This looks like a scanned PDF (no embedded text layer). "
                "OCR dependencies (pytesseract, pdf2image, poppler, tesseract) "
                "are not installed in this environment, so text could not be recovered."
            ),
        )

    try:
        pdf_file.seek(0)
        images = convert_from_bytes(pdf_file.read())
        ocr_text = ""
        for img in images:
            ocr_text += pytesseract.image_to_string(img) + "\n"
        ocr_text = ocr_text.strip()
        if ocr_text:
            return ExtractionResult(text=ocr_text, method="ocr")
        return ExtractionResult(
            text="", method="empty",
            warning="OCR ran but no text could be recovered from the scanned pages.",
        )
    except Exception as e:
        return ExtractionResult(
            text="", method="empty",
            warning=f"OCR fallback failed: {e}",
        )


# ---------------------------------------------------------------------------
# 2. ATS SCORING
# ---------------------------------------------------------------------------

def calculate_ats_score(resume_text: str, job_description: str) -> float:
    """Base TF-IDF cosine similarity match percentage between resume and job post."""
    if not resume_text or not job_description:
        return 0.0
    corpus = [resume_text, job_description]
    vectorizer = TfidfVectorizer(stop_words="english")
    tfidf_matrix = vectorizer.fit_transform(corpus)
    similarity = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])
    return round(float(similarity[0][0]) * 100, 2)


def calculate_weighted_score(resume_text: str, job_description: str,
                              matched: list[str], job_phrases: list[str]) -> float:
    """
    Blends the raw cosine-similarity score with explicit keyword-phrase
    coverage, since ATS systems in practice weight exact keyword hits more
    heavily than general semantic overlap.
    """
    base = calculate_ats_score(resume_text, job_description) / 100.0
    coverage = (len(matched) / len(job_phrases)) if job_phrases else 0.0
    blended = (0.6 * base) + (0.4 * coverage)
    return round(blended * 100, 2)


# ---------------------------------------------------------------------------
# 3. MULTI-WORD TECHNICAL PHRASE EXTRACTION
#    (gazetteer regex matching + n-gram TF-IDF, in place of single-word
#    vocabulary sampling)
# ---------------------------------------------------------------------------

# A curated gazetteer of common multi-word technical phrases. Matched via
# case-insensitive regex against the *original* text so we preserve natural
# casing (e.g. "AWS EC2", "CI/CD Pipelines") instead of lowercasing everything.
TECH_GAZETTEER = [
    r"java spring boot", r"spring boot", r"node\.?js", r"react\.?js", r"vue\.?js",
    r"next\.?js", r"asp\.?net( core)?", r"\.net core", r"ruby on rails",
    r"aws (ec2|s3|lambda|rds|ecs|eks|cloudformation|sagemaker|dynamodb)",
    r"amazon web services", r"google cloud platform", r"microsoft azure",
    r"azure devops", r"ci/cd( pipelines?)?", r"continuous integration",
    r"continuous deployment", r"docker compose", r"kubernetes", r"terraform",
    r"infrastructure as code", r"github actions", r"gitlab ci",
    r"rest(ful)? apis?", r"graphql", r"grpc", r"microservices?( architecture)?",
    r"event[- ]driven architecture", r"message queues?", r"apache kafka",
    r"rabbitmq", r"redis", r"elasticsearch", r"postgres(ql)?", r"mysql",
    r"mongodb", r"nosql", r"sql server", r"data warehousing", r"etl pipelines?",
    r"apache spark", r"apache airflow", r"pandas", r"numpy", r"scikit-learn",
    r"tensorflow", r"pytorch", r"machine learning", r"deep learning",
    r"natural language processing", r"computer vision", r"large language models?",
    r"llms?", r"generative ai", r"prompt engineering", r"unit testing",
    r"integration testing", r"test[- ]driven development", r"agile( methodology)?",
    r"scrum", r"kanban", r"object[- ]oriented programming", r"design patterns?",
    r"version control", r"git(hub|lab)?", r"linux", r"bash scripting",
    r"powershell", r"c\+\+", r"c#", r"typescript", r"javascript",
]
_GAZETTEER_PATTERN = re.compile(
    r"\b(" + "|".join(TECH_GAZETTEER) + r")\b", flags=re.IGNORECASE
)

GENERIC_NOISE = {
    "team", "work", "years", "ability", "strong", "knowledge", "skills",
    "related", "familiarity", "understanding", "looking", "experience",
    "background", "proficient", "responsible", "role", "candidate", "join",
}

# Connector/stopwords that should break an n-gram run rather than be
# included in it (so phrases don't span across "and", "with", etc.).
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS as _SKLEARN_STOPWORDS  # noqa: E402

_JOIN_BREAK_WORDS = {"and", "with", "using", "including", "such", "as", "for", "know", "must", "have"}
_DELIM_PATTERN = re.compile(r"[,;.\n•|/()]+")
_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.#-]{1,}")


def _gazetteer_matches(text: str) -> set[str]:
    """Finds curated multi-word tech phrases, preserving original casing."""
    found = set()
    for m in _GAZETTEER_PATTERN.finditer(text):
        span_text = m.group(0).strip()
        # Normalize whitespace, keep original casing as it appeared.
        found.add(re.sub(r"\s+", " ", span_text))
    return found


def _content_runs(text: str) -> list[list[str]]:
    """
    Splits text into clauses on punctuation, then further into runs of
    consecutive 'content' tokens — breaking the run at stopwords/connectors
    so n-grams never span across unrelated list items or filler words
    (e.g. avoids "boot aws lambda" spanning a comma-separated skills list).
    """
    runs: list[list[str]] = []
    for clause in _DELIM_PATTERN.split(text):
        tokens = _TOKEN_PATTERN.findall(clause)
        current: list[str] = []
        for tok in tokens:
            low = tok.lower()
            is_breaker = (
                low in _SKLEARN_STOPWORDS or low in _JOIN_BREAK_WORDS
                or low in GENERIC_NOISE or len(low) < 2
            )
            if is_breaker:
                if current:
                    runs.append(current)
                    current = []
                continue
            current.append(tok)
        if current:
            runs.append(current)
    return runs


def _ngram_phrases(text: str, top_n: int = 40) -> set[str]:
    """
    Extracts salient uni/bi/tri-grams as a complement to the gazetteer,
    catching domain terms not in the curated list (e.g. niche tools,
    internal frameworks, product names) — without crossing clause or
    stopword boundaries, which is what made naive whole-text n-gramming
    produce nonsense phrases.
    """
    if not text:
        return set()
    from collections import Counter
    counts: Counter[str] = Counter()
    for run in _content_runs(text):
        n = len(run)
        for size in (1, 2, 3):
            if size > n:
                continue
            for i in range(n - size + 1):
                gram = run[i:i + size]
                if size == 1 and len(gram[0]) < 3:
                    continue
                counts[" ".join(gram).lower()] += 1

    # Prefer longer, more specific phrases; break ties by frequency.
    ranked = sorted(counts.items(), key=lambda kv: (-len(kv[0].split()), -kv[1]))
    return {phrase for phrase, _ in ranked[:top_n]}


def _dedupe_subphrases(phrase_map: dict[str, str]) -> set[str]:
    """
    Given a {lowercase: original_casing} map, drops any phrase that is a
    contiguous word-subsequence of a longer phrase also present (e.g. drops
    "spring boot" and "boot" once "java spring boot" is already kept), so
    the same skill isn't reported three times at different granularities.
    """
    ordered = sorted(phrase_map.keys(), key=lambda p: -len(p.split()))
    kept_lower: list[str] = []
    for p in ordered:
        p_words = p.split()
        contained = False
        for k in kept_lower:
            k_words = k.split()
            if len(p_words) >= len(k_words):
                continue
            for i in range(len(k_words) - len(p_words) + 1):
                if k_words[i:i + len(p_words)] == p_words:
                    contained = True
                    break
            if contained:
                break
        if not contained:
            kept_lower.append(p)
    return {phrase_map[lp] for lp in kept_lower}


def extract_key_phrases(text: str) -> set[str]:
    """
    Replaces the old single-word TfidfVectorizer vocabulary sampling with
    multi-word technical phrase extraction: curated gazetteer matches
    (normalized, original-cased) unioned with salient n-gram candidates,
    then deduplicated so shorter fragments of an already-captured phrase
    aren't reported separately.
    """
    if not text:
        return set()
    gazetteer_hits = _gazetteer_matches(text)
    ngram_hits = _ngram_phrases(text)

    # Gazetteer casing wins on collision; n-grams fill in whatever's left.
    phrase_map: dict[str, str] = {n: n for n in ngram_hits}
    for g in gazetteer_hits:
        phrase_map[g.lower()] = g

    return _dedupe_subphrases(phrase_map)


def diff_phrases(resume_text: str, job_description: str) -> tuple[list[str], list[str], list[str]]:
    """Returns (job_phrases, matched, missing) using case-insensitive comparison."""
    resume_phrases = extract_key_phrases(resume_text)
    job_phrases = extract_key_phrases(job_description)

    resume_lower = {p.lower() for p in resume_phrases}
    matched = sorted([p for p in job_phrases if p.lower() in resume_lower])
    missing = sorted([p for p in job_phrases if p.lower() not in resume_lower])
    return sorted(job_phrases), matched, missing


# ---------------------------------------------------------------------------
# 4. STRUCTURED LLM ANALYSIS (Gemini, JSON-schema constrained)
# ---------------------------------------------------------------------------

@dataclass
class BulletRewrite:
    original: str
    rewritten: str
    reasoning: str


@dataclass
class ReadinessRoadmap:
    recommended_projects: list[str] = field(default_factory=list)
    skills_to_learn: list[str] = field(default_factory=list)
    experience_gaps: str = ""


@dataclass
class PathPhase:
    title: str = ""
    duration_estimate: str = ""
    actions: list[str] = field(default_factory=list)


@dataclass
class SuggestedPath:
    overview: str = ""
    phases: list[PathPhase] = field(default_factory=list)


@dataclass
class AISuggestions:
    summary: str = ""
    missing_keywords: list[str] = field(default_factory=list)
    bullet_rewrites: list[BulletRewrite] = field(default_factory=list)
    writing_enhancements: list[str] = field(default_factory=list)
    readiness_roadmap: ReadinessRoadmap = field(default_factory=ReadinessRoadmap)
    suggested_path: SuggestedPath = field(default_factory=SuggestedPath)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


# Default model. gemini-2.5-flash was deprecated (returning 404s ahead of its
# official Oct 16 2026 shutdown); gemini-3.7-flash is the current stable GA
# model as of Aug 2026. Do NOT fall back to gemini-1.5-flash — that
# generation is fully shut down and will also 404.
DEFAULT_MODEL = "gemini-3.7-flash"

# JSON schema Gemini is constrained to. Using structured output eliminates
# fragile markdown/regex parsing of the model's free-text response.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One or two sentence overview of the candidate's fit for the role.",
        },
        "missing_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Top 3-5 critical technical keywords/phrases missing from the resume.",
        },
        "bullet_rewrites": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {
                        "type": "string",
                        "description": (
                            "Copied VERBATIM, character-for-character, from the "
                            "Resume Text provided below — not paraphrased or "
                            "summarized. This must be an exact substring of the "
                            "resume text so it can be programmatically located "
                            "and replaced."
                        ),
                    },
                    "rewritten": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["original", "rewritten", "reasoning"],
            },
            "description": "2-3 concrete bullet points to rewrite, each with a short rationale.",
        },
        "writing_enhancements": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "2-4 actionable writing tips on structure, active verbs, and "
                "metric quantification — general craft feedback, not tied to "
                "one specific bullet."
            ),
        },
        "readiness_roadmap": {
            "type": "object",
            "properties": {
                "recommended_projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "1-3 concrete project ideas that would close the candidate's gap for this role.",
                },
                "skills_to_learn": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific skills/tools worth learning before applying.",
                },
                "experience_gaps": {
                    "type": "string",
                    "description": "One or two sentences on the key domain experience missing, if any.",
                },
            },
            "required": ["recommended_projects", "skills_to_learn", "experience_gaps"],
            "description": "A readiness roadmap: what to build/learn before applying if the match is weak.",
        },
        "suggested_path": {
            "type": "object",
            "properties": {
                "overview": {
                    "type": "string",
                    "description": "1-2 sentence framing of the overall journey from the candidate's current level to job-ready for this specific role.",
                },
                "phases": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Short phase name, e.g. 'Phase 1: Core Backend Fundamentals'.",
                            },
                            "duration_estimate": {
                                "type": "string",
                                "description": "Realistic time estimate for this phase, e.g. '2-3 weeks'.",
                            },
                            "actions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "3-5 concrete, specific actions for this phase.",
                            },
                        },
                        "required": ["title", "duration_estimate", "actions"],
                    },
                    "description": "3-5 sequential phases, ordered from where the candidate is now to job-ready for this role.",
                },
            },
            "required": ["overview", "phases"],
            "description": (
                "A clear, ordered, step-by-step path to reach the level needed "
                "for this specific job — more concrete and sequential than the "
                "readiness roadmap's flat lists. This is the single most "
                "actionable section for a candidate who is genuinely not yet "
                "qualified: it should read like a realistic study/build plan, "
                "not generic advice."
            ),
        },
    },
    "required": [
        "summary", "missing_keywords", "bullet_rewrites",
        "writing_enhancements", "readiness_roadmap", "suggested_path",
    ],
}

# Fallback if the primary model is degraded/overloaded. NOT gemini-2.5-flash
# — that generation is deprecated and already returning 404s for some users,
# so it's not a safe fallback target. gemini-3.6-flash is the current stable
# GA model one tier below 3.7 Flash.
FALLBACK_MODEL = "gemini-3.6-flash"

# Substrings that indicate a *transient* failure worth retrying (server-side
# overload, rate limiting) as opposed to a *permanent* one (bad key, invalid
# request) where retrying just wastes time before failing anyway.
_TRANSIENT_ERROR_MARKERS = (
    "503", "unavailable", "resource_exhausted", "429",
    "rate limit", "overloaded", "deadline exceeded", "internal error",
)


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_ERROR_MARKERS)


def _call_with_retry(client, model: str, prompt: str, config=None,
                      max_attempts: int = 3, base_delay: float = 2.0):
    """
    Calls generate_content with exponential backoff (2s, 4s, 8s, ...) on
    transient errors only. Permanent errors (bad key, malformed request)
    raise immediately on the first attempt rather than retrying blindly.
    `config` is optional — omitted entirely (not passed as None) for plain
    text calls like chat, which don't use a JSON response schema.
    """
    last_exc: Optional[Exception] = None
    kwargs = {"model": model, "contents": prompt}
    if config is not None:
        kwargs["config"] = config
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(**kwargs)
        except Exception as e:
            last_exc = e
            if not _is_transient_error(e):
                raise
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Gemini call to %s failed (attempt %d/%d, transient): %s — retrying in %.0fs",
                    model, attempt, max_attempts, e, delay,
                )
                time.sleep(delay)
    raise last_exc


def get_ai_suggestions(resume_text: str, job_description: str,
                        api_key: str = None, model: str = DEFAULT_MODEL) -> AISuggestions:
    """
    Calls Gemini with a JSON schema so the response comes back as
    machine-parseable structured data rather than free-form markdown.
    The API key is read from GEMINI_API_KEY (env var / .env) unless an
    explicit api_key override is passed in.

    Resilience: each model is tried with up to 3 attempts (exponential
    backoff) on transient errors (503/rate-limit/overload). If the primary
    model is still failing after retries, falls back once to FALLBACK_MODEL
    with its own retry budget. Permanent errors (bad key, invalid request)
    are not retried and are returned immediately. Every path returns a
    structured AISuggestions — this function never raises, so a Gemini
    outage degrades to a clear error message rather than a failed request.
    """
    if not GENAI_AVAILABLE:
        return AISuggestions(error="google-genai package is not installed in this environment.")

    active_key = api_key or os.getenv("GEMINI_API_KEY")
    if not active_key:
        return AISuggestions(error="⚠️ GEMINI_API_KEY is not set. Add it to backend/.env and restart the server.")

    prompt = f"""
You are an expert technical recruiter and career coach. Analyze the resume
against the target job description and respond ONLY with data matching the
provided JSON schema — no markdown, no commentary outside the schema fields.

Job Description:
{job_description[:4000]}

Resume Text:
{resume_text[:4000]}

Instructions:
1. Write a concise 1-2 sentence summary of overall fit.
2. List the top 3-5 critical missing technical keywords/skills.
3. Provide 2-3 concrete bullet point rewrites: the ORIGINAL bullet copied
   verbatim character-for-character from the Resume Text above (this is
   critical — it will be used to automatically find and replace that exact
   text in the candidate's resume, so paraphrasing it breaks that feature),
   an improved version with quantified impact where plausible, and a
   one-line reason it's stronger.
4. Give 2-4 general writing enhancement tips: structure, active verbs,
   metric quantification — craft-level feedback, not specific to one bullet.
5. Build a readiness roadmap: 1-3 recommended project ideas, specific
   skills to learn, and a short note on any domain experience gap — this
   should reflect what's genuinely missing for this role, not generic advice.
6. Build a suggested path: a clear, ordered, step-by-step plan (3-5 phases)
   to go from the candidate's current level to job-ready for this specific
   role. This is the section a candidate follows literally — be concrete and
   sequential (what to do first, second, third), with realistic time
   estimates, not just a bag of generic tips.
"""
    client = genai.Client(api_key=active_key)
    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=_RESPONSE_SCHEMA,
    )

    # Try the requested model first, then the fallback — but only bother
    # falling back if the primary failure looks transient. A bad API key
    # or malformed request will fail identically on the fallback model too.
    models_to_try = [model]
    if model != FALLBACK_MODEL:
        models_to_try.append(FALLBACK_MODEL)

    last_error: Optional[Exception] = None
    for i, current_model in enumerate(models_to_try):
        is_last_model = i == len(models_to_try) - 1
        try:
            response = _call_with_retry(
                client, current_model, prompt, config,
                max_attempts=3 if i == 0 else 2,
            )
        except Exception as e:
            last_error = e
            if not _is_transient_error(e) or is_last_model:
                break
            logger.warning("Falling back from %s to %s after transient failure: %s",
                            current_model, models_to_try[i + 1], e)
            continue

        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as e:
            return AISuggestions(error=f"Model returned non-JSON output: {e}")

        rewrites = [
            BulletRewrite(
                original=b.get("original", ""),
                rewritten=b.get("rewritten", ""),
                reasoning=b.get("reasoning", ""),
            )
            for b in data.get("bullet_rewrites", [])
        ]
        roadmap_data = data.get("readiness_roadmap", {}) or {}
        roadmap = ReadinessRoadmap(
            recommended_projects=roadmap_data.get("recommended_projects", []),
            skills_to_learn=roadmap_data.get("skills_to_learn", []),
            experience_gaps=roadmap_data.get("experience_gaps", ""),
        )
        path_data = data.get("suggested_path", {}) or {}
        path = SuggestedPath(
            overview=path_data.get("overview", ""),
            phases=[
                PathPhase(
                    title=p.get("title", ""),
                    duration_estimate=p.get("duration_estimate", ""),
                    actions=p.get("actions", []),
                )
                for p in path_data.get("phases", [])
            ],
        )
        return AISuggestions(
            summary=data.get("summary", ""),
            missing_keywords=data.get("missing_keywords", []),
            bullet_rewrites=rewrites,
            writing_enhancements=data.get("writing_enhancements", []),
            readiness_roadmap=roadmap,
            suggested_path=path,
        )

    # Every model/attempt exhausted — return a clear structured error rather
    # than raising, so the frontend gets a message instead of a failed request.
    if last_error and _is_transient_error(last_error):
        return AISuggestions(
            error=(
                "Gemini is temporarily overloaded and didn't respond after "
                f"retries across {model} and {FALLBACK_MODEL}. Try again in a moment."
            )
        )
    return AISuggestions(error=f"Error connecting to Gemini API: {last_error}")


# ---------------------------------------------------------------------------
# 4b. FOLLOW-UP CHAT
#     Lets the user ask questions about their specific analysis after it's
#     generated. Stateless like the rest of the API — the frontend resends
#     the message history each turn.
# ---------------------------------------------------------------------------

@dataclass
class ChatReply:
    reply: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def chat_about_analysis(messages: list[dict], resume_text: str, job_description: str,
                         analysis_summary: str = "", api_key: str = None,
                         model: str = DEFAULT_MODEL) -> ChatReply:
    """
    Continues a conversation grounded in a specific resume/job analysis.
    `messages` is the full chronological history as
    [{"role": "user"|"assistant", "content": str}, ...]. Uses the same
    retry + model-fallback strategy as get_ai_suggestions.
    """
    if not GENAI_AVAILABLE:
        return ChatReply(error="google-genai package is not installed in this environment.")

    active_key = api_key or os.getenv("GEMINI_API_KEY")
    if not active_key:
        return ChatReply(error="⚠️ GEMINI_API_KEY is not set. Add it to backend/.env and restart the server.")

    if not messages:
        return ChatReply(error="No message to respond to.")

    transcript_lines = []
    for m in messages:
        speaker = "Candidate" if m.get("role") == "user" else "Coach"
        transcript_lines.append(f"{speaker}: {m.get('content', '')}")
    transcript = "\n".join(transcript_lines)

    prompt = f"""
You are a career coach continuing a conversation about a resume/job match
analysis you already generated for this candidate.

Job Description:
{job_description[:3000]}

Resume Text:
{resume_text[:3000]}

Your earlier one-line summary of fit: {analysis_summary or "(none provided)"}

Conversation so far:
{transcript}

Reply as "Coach" to the candidate's most recent message. Ground your answer
concretely in the resume and job description above. Keep it to a few
sentences — conversational, not another full report. Respond with plain
text only, no markdown headers.
Coach:"""

    client = genai.Client(api_key=active_key)
    models_to_try = [model] if model == FALLBACK_MODEL else [model, FALLBACK_MODEL]

    last_error: Optional[Exception] = None
    for i, current_model in enumerate(models_to_try):
        is_last_model = i == len(models_to_try) - 1
        try:
            response = _call_with_retry(client, current_model, prompt, max_attempts=3 if i == 0 else 2)
            return ChatReply(reply=response.text.strip())
        except Exception as e:
            last_error = e
            if not _is_transient_error(e) or is_last_model:
                break
            continue

    if last_error and _is_transient_error(last_error):
        return ChatReply(error="Gemini is temporarily overloaded and didn't respond after retries. Try again in a moment.")
    return ChatReply(error=f"Error connecting to Gemini API: {last_error}")


# ---------------------------------------------------------------------------
# 5. EXPORT (Markdown + PDF)
# ---------------------------------------------------------------------------

def _keywords_to_sentence(keywords: list[str], lead_in: str = "To strengthen this application, focus on") -> str:
    """Joins a list of keywords into a natural sentence instead of a bullet
    list — reads as guidance rather than a raw dump. `lead_in` lets callers
    phrase it differently for different contexts (gaps vs. matches)."""
    if not keywords:
        return "None found."
    if len(keywords) == 1:
        joined = keywords[0]
    elif len(keywords) == 2:
        joined = f"{keywords[0]} and {keywords[1]}"
    else:
        joined = ", ".join(keywords[:-1]) + f", and {keywords[-1]}"
    return f"{lead_in}: {joined}."


def generate_markdown_report(score: float, matched: list[str], missing: list[str],
                              ai: AISuggestions, job_title: str = "Target Role") -> str:
    lines = [
        f"# ResumeMax Gap Report — {job_title}",
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        f"## Overall Match Score: {score}%",
        "",
        "## Matched Keywords",
        *([f"- ✅ {k}" for k in matched] if matched else ["- None found."]),
        "",
        "## Missing Keywords",
        *([f"- ❌ {k}" for k in missing] if missing else ["- No major gaps detected."]),
        "",
    ]
    if ai.ok:
        lines += [
            "## AI Summary",
            ai.summary or "_No summary generated._",
            "",
            "## Priority Keywords to Add",
            _keywords_to_sentence(ai.missing_keywords, "Focus on"),
            "",
            "## Suggested Bullet Rewrites",
        ]
        if ai.bullet_rewrites:
            for i, b in enumerate(ai.bullet_rewrites, 1):
                lines += [
                    f"### {i}. {b.rewritten}",
                    f"- **Original:** {b.original}",
                    f"- **Why it's stronger:** {b.reasoning}",
                    "",
                ]
        else:
            lines.append("_No rewrites generated._")

        lines += ["", "## Writing Enhancements"]
        lines += (
            [f"- {tip}" for tip in ai.writing_enhancements]
            if ai.writing_enhancements else ["_No writing tips generated._"]
        )

        lines += ["", "## Readiness Roadmap"]
        rm = ai.readiness_roadmap
        lines += ["**Recommended projects:**"]
        lines += [f"- {p}" for p in rm.recommended_projects] if rm.recommended_projects else ["- None suggested."]
        lines += ["", "**Skills to learn:**"]
        lines += [f"- {s}" for s in rm.skills_to_learn] if rm.skills_to_learn else ["- None suggested."]
        lines += ["", "**Experience gaps:**", rm.experience_gaps or "_None noted._"]

        lines += ["", "## Suggested Path to Get There"]
        sp = ai.suggested_path
        lines += [sp.overview or "_No path generated._", ""]
        if sp.phases:
            for i, phase in enumerate(sp.phases, 1):
                lines += [f"### {phase.title or f'Phase {i}'} ({phase.duration_estimate or 'timing TBD'})"]
                lines += [f"- {a}" for a in phase.actions] if phase.actions else ["- No specific actions listed."]
                lines += [""]
        else:
            lines.append("_No phases generated._")
    else:
        lines += ["## AI Summary", f"_AI suggestions unavailable: {ai.error}_"]
    return "\n".join(lines)


def generate_pdf_report(score: float, matched: list[str], missing: list[str],
                         ai: AISuggestions, job_title: str = "Target Role") -> Optional[bytes]:
    """Renders the gap report as a formatted PDF using reportlab. Returns None if unavailable."""
    if not REPORTLAB_AVAILABLE:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER,
                             topMargin=0.75 * inch, bottomMargin=0.75 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], textColor=colors.HexColor("#1f2937"))
    h2 = ParagraphStyle("H2X", parent=styles["Heading2"], textColor=colors.HexColor("#4338CA"),
                         spaceBefore=14, spaceAfter=6)
    body = styles["BodyText"]

    story = [
        Paragraph(f"ResumeMax Gap Report — {job_title}", title_style),
        Paragraph(datetime.now().strftime("Generated %Y-%m-%d %H:%M"), body),
        Spacer(1, 12),
        Paragraph(f"<b>Overall Match Score:</b> {score}%", body),
        Spacer(1, 10),
    ]

    story.append(Paragraph("Matched Keywords", h2))
    story.append(Paragraph(
        f"{len(matched)} matched. " + _keywords_to_sentence(matched, "These already appear in the resume")
        if matched else "None found.",
        body,
    ))

    story.append(Paragraph("Missing Keywords", h2))
    story.append(Paragraph(
        f"{len(missing)} gaps found. " + _keywords_to_sentence(missing, "Focus on")
        if missing else "No major gaps detected.",
        body,
    ))

    if ai.ok:
        story.append(Paragraph("AI Summary", h2))
        story.append(Paragraph(ai.summary or "No summary generated.", body))

        story.append(Paragraph("Priority Keywords to Add", h2))
        story.append(Paragraph(_keywords_to_sentence(ai.missing_keywords, "Focus on"), body))

        story.append(Paragraph("Suggested Bullet Rewrites", h2))
        if ai.bullet_rewrites:
            table_data = [["Original", "Rewritten", "Why it's stronger"]]
            for b in ai.bullet_rewrites:
                table_data.append([
                    Paragraph(b.original, body),
                    Paragraph(b.rewritten, body),
                    Paragraph(b.reasoning, body),
                ])
            t = Table(table_data, colWidths=[1.8 * inch, 2.2 * inch, 1.8 * inch])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4338CA")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]))
            story.append(t)
        else:
            story.append(Paragraph("No rewrites generated.", body))

        story.append(Paragraph("Writing Enhancements", h2))
        if ai.writing_enhancements:
            story.append(ListFlowable(
                [ListItem(Paragraph(tip, body)) for tip in ai.writing_enhancements], bulletType="bullet"))
        else:
            story.append(Paragraph("No writing tips generated.", body))

        story.append(Paragraph("Readiness Roadmap", h2))
        rm = ai.readiness_roadmap
        story.append(Paragraph("<b>Recommended projects:</b>", body))
        if rm.recommended_projects:
            story.append(ListFlowable(
                [ListItem(Paragraph(p, body)) for p in rm.recommended_projects], bulletType="bullet"))
        else:
            story.append(Paragraph("None suggested.", body))
        story.append(Paragraph("<b>Skills to learn:</b>", body))
        if rm.skills_to_learn:
            story.append(ListFlowable(
                [ListItem(Paragraph(s, body)) for s in rm.skills_to_learn], bulletType="bullet"))
        else:
            story.append(Paragraph("None suggested.", body))
        story.append(Paragraph("<b>Experience gaps:</b>", body))
        story.append(Paragraph(rm.experience_gaps or "None noted.", body))

        story.append(Paragraph("Suggested Path to Get There", h2))
        sp = ai.suggested_path
        story.append(Paragraph(sp.overview or "No path generated.", body))
        if sp.phases:
            for i, phase in enumerate(sp.phases, 1):
                title = phase.title or f"Phase {i}"
                duration = phase.duration_estimate or "timing TBD"
                story.append(Paragraph(f"<b>{title}</b> — {duration}", body))
                if phase.actions:
                    story.append(ListFlowable(
                        [ListItem(Paragraph(a, body)) for a in phase.actions], bulletType="bullet"))
                else:
                    story.append(Paragraph("No specific actions listed.", body))
        else:
            story.append(Paragraph("No phases generated.", body))
    else:
        story.append(Paragraph("AI Summary", h2))
        story.append(Paragraph(f"AI suggestions unavailable: {ai.error}", body))

    doc.build(story)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 6. APPLY REWRITES BACK TO THE RESUME
#    Finds each AI-suggested bullet in the original resume text and swaps
#    in the rewritten version, so the user can download an updated resume
#    instead of just reading advice about it.
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    updated_text: str
    applied: list[str] = field(default_factory=list)    # original snippets successfully replaced
    unmatched: list[str] = field(default_factory=list)  # snippets that couldn't be located


def _normalize_whitespace_map(text: str) -> tuple[str, list[int]]:
    """
    Collapses whitespace runs to single spaces and returns (normalized, index_map)
    where index_map[i] is the index into the ORIGINAL text corresponding to
    normalized[i]. Used as a fallback match when PDF-extraction whitespace
    (odd line breaks, double spaces) prevents an exact substring match.
    """
    chars, index_map = [], []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            start = i
            while i < n and text[i].isspace():
                i += 1
            chars.append(" ")
            index_map.append(start)
        else:
            chars.append(ch)
            index_map.append(i)
            i += 1
    return "".join(chars), index_map


def apply_rewrites_to_resume(resume_text: str, bullet_rewrites: list[BulletRewrite]) -> ApplyResult:
    """
    Replaces each bullet_rewrite's `original` text with its `rewritten`
    version inside resume_text. Tries an exact substring match first; if
    that fails (usually due to PDF-extraction whitespace quirks), falls
    back to a whitespace-normalized match. Rewrites whose original text
    genuinely can't be located are reported in `unmatched` rather than
    silently dropped, so the UI can tell the user which ones need a manual
    touch-up.
    """
    updated = resume_text
    applied, unmatched = [], []

    for rewrite in bullet_rewrites:
        original = (rewrite.original or "").strip()
        if not original:
            unmatched.append(rewrite.original)
            continue

        idx = updated.find(original)
        if idx != -1:
            updated = updated[:idx] + rewrite.rewritten + updated[idx + len(original):]
            applied.append(rewrite.original)
            continue

        norm_updated, idx_map = _normalize_whitespace_map(updated)
        norm_original, _ = _normalize_whitespace_map(original)
        norm_idx = norm_updated.find(norm_original)
        if norm_idx != -1 and norm_original:
            start = idx_map[norm_idx]
            end = idx_map[norm_idx + len(norm_original) - 1] + 1
            updated = updated[:start] + rewrite.rewritten + updated[end:]
            applied.append(rewrite.original)
            continue

        unmatched.append(rewrite.original)

    return ApplyResult(updated_text=updated, applied=applied, unmatched=unmatched)


def generate_resume_pdf(resume_text: str) -> Optional[bytes]:
    """
    Reformats resume text into a clean, simple PDF. Important honesty note:
    this does NOT preserve the original document's visual design — fonts,
    columns, logos, spacing from whatever template the resume was made in
    are gone. It's a content-only rebuild, good for a quick downloadable
    draft or as text to paste into your own template — not a drop-in
    replacement for a professionally designed resume file.
    """
    if not REPORTLAB_AVAILABLE:
        return None
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=LETTER,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch,
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("ResumeBody", parent=styles["BodyText"], fontSize=10.5, leading=15)

    story = []
    for line in resume_text.split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 8))
            continue
        safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        story.append(Paragraph(safe, body))

    doc.build(story)
    return buffer.getvalue()
