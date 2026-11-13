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
    import wordninja
    WORDNINJA_AVAILABLE = True
except ImportError:
    wordninja = None
    WORDNINJA_AVAILABLE = False

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


# Some resume templates/PDF generators omit actual space characters between
# words entirely, relying purely on glyph positioning for visual spacing —
# pdfplumber then extracts a long fused run like "Commandedastructured...".
# Only worth attempting word-segmentation on tokens this long; real single
# words/identifiers essentially never reach this length, so false positives
# on genuinely-correct text are rare.
_MISSING_SPACE_TOKEN_THRESHOLD = 20
_MISSING_SPACE_TOKEN_PATTERN = re.compile(r"\S{" + str(_MISSING_SPACE_TOKEN_THRESHOLD) + r",}")


def _fix_missing_spaces(text: str) -> str:
    """
    Detects abnormally long space-less tokens (the PDF-extraction artifact
    described above) and uses statistical word-segmentation (wordninja) to
    re-insert spaces. Skips anything that looks like a URL/email/path,
    since segmenting those would actively corrupt them rather than fix them.
    """
    if not WORDNINJA_AVAILABLE or not text:
        return text

    def _fix_token(match: re.Match) -> str:
        token = match.group(0)
        if any(marker in token for marker in ("@", "/", "\\", "http", "www.")):
            return token
        letters_only = re.sub(r"[^A-Za-z]", "", token)
        if len(letters_only) < _MISSING_SPACE_TOKEN_THRESHOLD:
            return token
        try:
            words = wordninja.split(token)
        except Exception:
            return token
        # Only replace if segmentation actually found multiple words —
        # a length-1 result means wordninja itself thinks it's one token,
        # so leave it untouched rather than risk a low-confidence rewrite.
        return " ".join(words) if len(words) > 1 else token

    return _MISSING_SPACE_TOKEN_PATTERN.sub(_fix_token, text)


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
                # layout=True preserves the resume's original horizontal
                # spacing (via runs of literal spaces) instead of pdfplumber's
                # default, which reads purely by y-position and silently
                # glues side-by-side columns onto one line — e.g. a sidebar
                # skill like "Figma" fusing directly onto the neighboring
                # column's heading text with no space or punctuation between
                # them. That fusion was corrupting phrase matching for any
                # multi-column resume (common in design-oriented templates).
                # The large gaps this preserves are then treated as column
                # boundaries below, in _DELIM_PATTERN.
                extracted = page.extract_text(layout=True)
                if extracted:
                    text += extracted + "\n"
    except Exception as e:
        return ExtractionResult(text="", method="empty", warning=f"Could not parse PDF: {e}")

    text = text.strip()
    text = _fix_missing_spaces(text)

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

    Coverage is weighted far more heavily than raw similarity (0.75 vs
    0.25) — TF-IDF cosine similarity between a resume and a job posting is
    structurally modest even for a genuinely strong match, since they're
    different document types with different overall vocabulary/structure,
    not paraphrases of each other. A candidate with 100% of the required
    keywords was previously still capped around 65% under the old 0.6/0.4
    weighting purely because of that document-similarity drag — verified
    against a real extraction test with perfect 8/8 keyword coverage, which
    only produced 65.2% before this change and 85.5% after it. Coverage is
    the metric that's actually meaningful and interpretable to a candidate;
    raw similarity is kept as a smaller smoothing factor, not the primary
    signal.
    """
    base = calculate_ats_score(resume_text, job_description) / 100.0

    # Recognized skills (gazetteer) count fully; generic n-gram phrases from
    # prose count far less. Prose postings produce many n-grams that aren't
    # requirements at all ("growing", "requirements", "web platform"), and
    # letting them count equally made even a strong resume look like it was
    # missing most of the job.
    def weight(phrase: str) -> float:
        return 1.0 if _GAZETTEER_FULL.fullmatch(phrase) else 0.25

    total = sum(weight(p) for p in job_phrases)
    got = sum(weight(p) for p in matched)
    coverage = (got / total) if total else 0.0
    blended = (0.25 * base) + (0.75 * coverage)
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
    # UI/UX & product design — previously entirely absent, which meant a
    # design-focused resume got zero benefit from the curated gazetteer and
    # fell back entirely on the more error-prone n-gram matcher.
    r"adobe xd", r"figma", r"sketch", r"invision", r"framer", r"zeplin",
    r"design systems?", r"wirefram(e|ing)s?", r"prototyp(e|ing)s?",
    r"user research", r"usability testing", r"user testing",
    r"information architecture", r"interaction design", r"visual design",
    r"ui/?ux( design)?", r"user experience( design)?", r"user interface( design)?",
    r"design thinking", r"heuristic evaluation", r"a/b testing",
    r"responsive design", r"accessibility( design)?", r"wcag",
    r"design ops", r"design tokens?", r"atomic design",
    r"adobe (photoshop|illustrator|premiere|after effects)",
    r"user flows?", r"journey maps?", r"persona(s)? development",
    # Bare single-word tech names. The multi-word patterns above only catch
    # compounds ("react.js", "java spring boot"), so a plain "React" or
    # "Python" otherwise depended on winning the generic n-gram ranking.
    # Deliberately excludes words that are common English ("go", "swift").
    r"python", r"java", r"react", r"angular", r"svelte", r"django", r"flask",
    r"fastapi", r"spring", r"laravel", r"express", r"sql", r"nosql", r"html5?",
    r"css3?", r"sass", r"tailwind( css)?", r"bootstrap", r"redux", r"webpack",
    r"swift ?ui", r"kotlin", r"ruby", r"php", r"scala", r"rust", r"golang",
    r"matlab", r"tableau", r"power ?bi", r"excel", r"jira", r"confluence",
    r"notion", r"slack", r"figjam", r"miro", r"storybook", r"selenium",
    r"jest", r"pytest", r"junit", r"cypress", r"firebase", r"supabase",
    r"jenkins", r"ansible", r"nginx", r"graphql", r"oauth", r"jwt",
    r"opencv", r"pandas", r"numpy", r"keras", r"hadoop", r"snowflake", r"bigquery",
]
_GAZETTEER_PATTERN = re.compile(
    r"\b(" + "|".join(TECH_GAZETTEER) + r")\b", flags=re.IGNORECASE
)
_GAZETTEER_FULL = re.compile(r"(?:" + "|".join(TECH_GAZETTEER) + r")", flags=re.IGNORECASE)

GENERIC_NOISE = {
    "team", "work", "years", "ability", "strong", "knowledge", "skills",
    "related", "familiarity", "understanding", "looking", "experience",
    "background", "proficient", "responsible", "role", "candidate", "join",
}

# Connector/stopwords that should break an n-gram run rather than be
# included in it (so phrases don't span across "and", "with", etc.).
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS as _SKLEARN_STOPWORDS  # noqa: E402

_JOIN_BREAK_WORDS = {"and", "with", "using", "including", "such", "as", "for", "know", "must", "have"}
_DELIM_PATTERN = re.compile(r"[,;.\n•|/()]+|[ \t]{2,}")
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

    # Unigrams and multi-word phrases are ranked and capped SEPARATELY,
    # each by frequency. A single top_n cutoff applied to everything at once
    # (ranked by length) used to let a document rich in 2-3 word candidates
    # crowd out valid short skill names entirely (e.g. "python" never making
    # the cut in a resume full of multi-word phrases) — deduplication
    # already removes a short phrase when a longer one containing it is
    # kept, so there's no need for length-based ranking to do that job too.
    unigrams = {p: c for p, c in counts.items() if " " not in p}
    multiword = {p: c for p, c in counts.items() if " " in p}
    top_unigrams = sorted(unigrams.items(), key=lambda kv: -kv[1])[: top_n // 2]
    top_multiword = sorted(multiword.items(), key=lambda kv: (-len(kv[0].split()), -kv[1]))[: top_n // 2]
    return {p for p, _ in top_unigrams} | {p for p, _ in top_multiword}


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

    # Canonicalize to known skills: an n-gram that merely wraps a recognized
    # skill ("scaling design systems", "running usability testing", or an
    # artifact like "react conducted user") is noise around that skill, and
    # keeping the longer wrapper means the clean skill can never match the
    # other document's phrasing of it. Keep the skill, drop the wrapper.
    gazetteer_lower = {g.lower() for g in gazetteer_hits}
    for phrase in list(phrase_map):
        if phrase in gazetteer_lower:
            continue
        if any(re.search(r"\b" + re.escape(g) + r"\b", phrase) for g in gazetteer_lower):
            del phrase_map[phrase]

    return _dedupe_subphrases(phrase_map)


def _stem_token(tok: str) -> str:
    """Very light plural folding ('systems' -> 'system', 'prototypes' -> 'prototype').
    Applied identically to both documents, so it only has to be consistent,
    not linguistically perfect."""
    if len(tok) > 3 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _norm_tokens(text: str) -> list[str]:
    return [_stem_token(t) for t in re.findall(r"[a-z0-9+#]+", text.lower())]


def diff_phrases(resume_text: str, job_description: str) -> tuple[list[str], list[str], list[str]]:
    """
    Returns (job_phrases, matched, missing). Comparison is case-insensitive,
    ignores punctuation/hyphenation, and folds plurals — "design systems" in
    a posting matches "design system" in a resume.
    """
    resume_phrases = extract_key_phrases(resume_text)
    job_phrases = extract_key_phrases(job_description)

    resume_keys = {" ".join(_norm_tokens(p)) for p in resume_phrases}
    # Normalized full text, padded so a phrase must match on whole-token boundaries.
    resume_norm_text = " " + " ".join(_norm_tokens(resume_text)) + " "

    def _is_matched(phrase: str) -> bool:
        key = " ".join(_norm_tokens(phrase))
        if not key:
            return False
        if key in resume_keys:
            return True
        # Fallback: the phrase may be in the resume only as part of a longer
        # extracted phrase (the job asks for "design systems"; the resume says
        # "...creating design systems at scale" and extraction kept the fuller
        # phrase). Search the normalized raw text on whole-token boundaries so
        # that still counts without depending on extraction's phrase edges.
        return (" " + key + " ") in resume_norm_text

    matched = sorted([p for p in job_phrases if _is_matched(p)])
    missing = sorted([p for p in job_phrases if not _is_matched(p)])
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
    "rate limit", "overloaded", "deadline exceeded", "deadline_exceeded", "internal error",
)


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_ERROR_MARKERS)


def _is_timeout_error(exc: Exception) -> bool:
    """True for client/server timeouts. These are never retried: a call that
    already burned its whole time budget won't get faster on a second try, and
    retrying just multiplies how long the user waits."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "timeout" in name or any(m in msg for m in ("timed out", "timeout", "deadline exceeded", "deadline_exceeded"))


def _make_client(api_key: str, timeout_s: float):
    """Gemini client with a hard per-request timeout. The SDK has no short
    default, so a stalled connection would otherwise hang a request indefinitely."""
    return genai.Client(api_key=api_key, http_options={"timeout": int(timeout_s * 1000)})


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
            if not _is_transient_error(e) or _is_timeout_error(e):
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
    client = _make_client(active_key, 60)
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
            if not _is_transient_error(e) or _is_timeout_error(e) or is_last_model:
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
# 4a. AI REQUIREMENT MATCHING
#     Lexical phrase matching can't tell a real requirement from filler in a
#     prose job posting, and can't recognize paraphrases ("scaling design
#     systems" vs "maintaining a scalable design system"). This asks the
#     model to extract the posting's actual requirements as short skill tags
#     and judge each against the resume, with evidence that is verified in
#     code so the model can't simply assert a match it can't support.
# ---------------------------------------------------------------------------

@dataclass
class RequirementMatch:
    skill: str
    importance: str  # "required" | "preferred"
    found: bool
    evidence: str = ""


@dataclass
class RequirementMatchResult:
    requirements: list[RequirementMatch] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.requirements) > 0


_REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": "Short atomic skill/competency tag, 1-4 words (e.g. 'Figma', 'Design Systems', 'Usability Testing', 'React'). Never a full sentence.",
                    },
                    "importance": {
                        "type": "string",
                        "description": "Either 'required' or 'preferred'.",
                    },
                    "found_in_resume": {"type": "boolean"},
                    "evidence": {
                        "type": "string",
                        "description": "Short phrase (max 15 words) copied from the resume that demonstrates this skill; empty string if not found.",
                    },
                },
                "required": ["skill", "importance", "found_in_resume", "evidence"],
            },
        }
    },
    "required": ["requirements"],
}


def _evidence_supported(evidence: str, resume_text: str) -> bool:
    """
    Guards against hallucinated matches: a skill only counts as found if the
    evidence the model quoted is actually grounded in the resume text. Not a
    strict substring check (models lightly reword quotes) — at least 60% of
    the evidence's meaningful words must appear in the resume.
    """
    tokens = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9+#]+", evidence or "")]
    tokens = [t for t in tokens if t not in _SKLEARN_STOPWORDS]
    if not tokens:
        return False
    haystack = set(t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9+#]+", resume_text))
    hits = sum(1 for t in tokens if t in haystack)
    return hits / len(tokens) >= 0.6


def score_from_requirements(requirements: list[RequirementMatch]) -> float:
    """Weighted requirement coverage: required = 1.0, preferred = 0.5."""
    if not requirements:
        return 0.0
    def w(r: RequirementMatch) -> float:
        return 1.0 if r.importance == "required" else 0.5
    total = sum(w(r) for r in requirements)
    got = sum(w(r) for r in requirements if r.found)
    return round(100.0 * got / total, 2) if total else 0.0


def match_requirements_with_ai(resume_text: str, job_description: str,
                                api_key: str = None,
                                model: str = DEFAULT_MODEL) -> RequirementMatchResult:
    """
    Extracts the job's concrete requirements and judges each against the
    resume. Never raises; returns an error result so the caller can fall
    back to the keyword matcher. Uses a smaller retry budget than the
    long-form AI features, since this sits on the scan's critical path.
    """
    if not GENAI_AVAILABLE:
        return RequirementMatchResult(error="google-genai package is not installed.")
    active_key = api_key or os.getenv("GEMINI_API_KEY")
    if not active_key:
        return RequirementMatchResult(error="GEMINI_API_KEY is not set.")

    prompt = f"""
You are an expert technical recruiter doing a precise resume-to-job match.

Job Description:
{job_description[:6000]}

Resume Text (extracted from a PDF; multi-column layouts may appear interleaved):
{resume_text[:8000]}

Task:
1. Extract the job's concrete requirements as short, atomic skill/competency
   tags (1-4 words each, e.g. "Figma", "Design Systems", "Usability Testing",
   "React"). Aim for 6-20. Ignore company description, perks, and generic
   filler ("growing team", "fast-paced"). Include at most 2 soft skills, and
   only if the posting explicitly requires them.
2. Mark each "required" or "preferred" (wording like "plus", "nice to have",
   "bonus", or "familiarity with" means preferred).
3. For each, decide whether the resume demonstrates it. Treat synonyms and
   paraphrases as a match (e.g. "scaling design systems" is satisfied by
   "maintaining a scalable design system"). Do NOT credit a skill the resume
   gives no real support for — never assume.
4. For every skill you mark as found, give a short phrase (max 15 words)
   copied from the resume as evidence. Leave evidence empty if not found.
"""
    client = _make_client(active_key, 15)
    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=_REQUIREMENTS_SCHEMA,
    )
    models_to_try = [model] if model == FALLBACK_MODEL else [model, FALLBACK_MODEL]

    last_error: Optional[Exception] = None
    response = None
    for i, current_model in enumerate(models_to_try):
        try:
            response = _call_with_retry(client, current_model, prompt, config,
                                         max_attempts=2 if i == 0 else 1)
            break
        except Exception as e:
            last_error = e
            if not _is_transient_error(e) or _is_timeout_error(e):
                break
    if response is None:
        return RequirementMatchResult(error=f"AI matching failed: {last_error}")

    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as e:
        return RequirementMatchResult(error=f"AI matching returned non-JSON output: {e}")

    seen: set[str] = set()
    out: list[RequirementMatch] = []
    for item in (data.get("requirements") or [])[:30]:
        skill = str(item.get("skill", "")).strip()[:60]
        if not skill or skill.lower() in seen:
            continue
        seen.add(skill.lower())
        importance = "preferred" if str(item.get("importance", "")).lower().startswith("pref") else "required"
        evidence = str(item.get("evidence", "")).strip()
        found = bool(item.get("found_in_resume")) and _evidence_supported(evidence, resume_text)
        out.append(RequirementMatch(skill=skill, importance=importance, found=found,
                                     evidence=evidence if found else ""))
    if not out:
        return RequirementMatchResult(error="AI matching returned no requirements.")
    return RequirementMatchResult(requirements=out)


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

    client = _make_client(active_key, 40)
    models_to_try = [model] if model == FALLBACK_MODEL else [model, FALLBACK_MODEL]

    last_error: Optional[Exception] = None
    for i, current_model in enumerate(models_to_try):
        is_last_model = i == len(models_to_try) - 1
        try:
            response = _call_with_retry(client, current_model, prompt, max_attempts=3 if i == 0 else 2)
            return ChatReply(reply=response.text.strip())
        except Exception as e:
            last_error = e
            if not _is_transient_error(e) or _is_timeout_error(e) or is_last_model:
                break
            continue

    if last_error and _is_transient_error(last_error):
        return ChatReply(error="Gemini is temporarily overloaded and didn't respond after retries. Try again in a moment.")
    return ChatReply(error=f"Error connecting to Gemini API: {last_error}")


# ---------------------------------------------------------------------------
# 4c. COVER LETTER GENERATION
#     Reuses the resume/job text the candidate already provided — no separate
#     form. Same retry + model-fallback strategy as the other AI features.
# ---------------------------------------------------------------------------

@dataclass
class CoverLetterResult:
    letter: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.letter)


# Optional tone modifiers: a short client-facing key -> a concrete instruction
# appended to the prompt. Unknown or missing tone means no modifier.
_COVER_LETTER_TONES = {
    "concise": "Make this version noticeably shorter than a standard cover letter: 3 tight paragraphs at most, no filler.",
    "confident": "Use a more confident, assertive tone throughout, without overstating anything the resume doesn't support.",
    "conversational": "Use a warmer, more conversational tone: less corporate boilerplate, more like a direct, genuine note.",
    "leadership": "Emphasize leadership, ownership, and cross-functional collaboration wherever the resume actually supports it. Do not invent leadership experience.",
    "technical": "Emphasize specific technical skills, tools, and hands-on implementation details wherever the resume actually supports it.",
}


def generate_cover_letter(resume_text: str, job_description: str, analysis_summary: str = "",
                           tone: Optional[str] = None, api_key: str = None,
                           model: str = DEFAULT_MODEL) -> CoverLetterResult:
    """
    Writes a tailored cover letter from the resume and job text already on
    hand. Returns plain text. Never raises; failures come back as an error
    result so the API layer can respond cleanly.
    """
    if not GENAI_AVAILABLE:
        return CoverLetterResult(error="google-genai package is not installed in this environment.")
    active_key = api_key or os.getenv("GEMINI_API_KEY")
    if not active_key:
        return CoverLetterResult(error="⚠️ GEMINI_API_KEY is not set. Add it to backend/.env and restart the server.")

    tone_line = _COVER_LETTER_TONES.get((tone or "").strip().lower(), "")

    prompt = f"""
You are a career coach writing a complete, tailored cover letter for this
candidate and role.

Job Description:
{job_description[:6000]}

Resume Text:
{resume_text[:8000]}

Analysis summary so far: {analysis_summary or "(none provided)"}

Instructions:
1. Find the candidate's real name at the top of the resume and use it in the
   signature. If it genuinely can't be determined, use "[Your Name]". Never
   invent a name.
2. Do not invent a hiring manager's name or a company name unless the job
   description states one. Otherwise address the letter to "Hiring Manager"
   and keep company references generic.
3. Write 3-4 paragraphs: an opening that names the role with a genuine hook;
   a middle that connects specific resume experience to specific job
   requirements (concrete, not generic); and a closing that is enthusiastic
   without being over the top.
4. Do not fabricate metrics, employers, or experience that isn't in the
   resume. Ground every claim in what is actually there.
{("5. " + tone_line) if tone_line else ""}

Respond with ONLY the finished letter text: no markdown, no subject line, no
commentary before or after it.
"""

    client = _make_client(active_key, 50)
    models_to_try = [model] if model == FALLBACK_MODEL else [model, FALLBACK_MODEL]

    last_error: Optional[Exception] = None
    for i, current_model in enumerate(models_to_try):
        is_last_model = i == len(models_to_try) - 1
        try:
            response = _call_with_retry(client, current_model, prompt, None,
                                         max_attempts=3 if i == 0 else 2)
            letter = (response.text or "").strip()
            if not letter:
                return CoverLetterResult(error="The model returned an empty letter. Try again.")
            return CoverLetterResult(letter=letter)
        except Exception as e:
            last_error = e
            if not _is_transient_error(e) or _is_timeout_error(e) or is_last_model:
                break

    if last_error and _is_transient_error(last_error):
        return CoverLetterResult(error="Gemini is temporarily overloaded and didn't respond after retries. Try again in a moment.")
    return CoverLetterResult(error=f"Error connecting to Gemini API: {last_error}")


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
