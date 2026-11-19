---
title: ResumeMax
emoji: 📄
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# ATS Scan — AI Resume & Skill Optimizer (Web Edition)

A rebuild of the ATS Resume Optimizer as a real web app: FastAPI backend,
custom HTML/CSS/JS frontend styled as a diff/terminal-themed product rather
than a Streamlit demo.

## Why this stack

- **FastAPI** exposes the same scoring/extraction/AI logic as a clean JSON
  API — the frontend is just a client of it.
- **Plain HTML/CSS/JS** (no React/Vue build step) — full design control
  without needing Node/npm/webpack installed and configured. If you want to
  swap in React later, the API is already decoupled from the UI, so that's
  a frontend-only change.
- **No API key in the UI** — `GEMINI_API_KEY` is read server-side from a
  `.env` file. The frontend just checks `/api/health` to know whether AI
  suggestions are available; it never asks the user for a key.

## Project structure

```
ats_web/
├── Dockerfile
├── .dockerignore
├── .gitignore              # excludes .env and venv
├── .github/workflows/tests.yml  # CI: runs pytest on every push
├── backend/
│   ├── main.py             # FastAPI app + API endpoints
│   ├── engine.py           # extraction, scoring, phrase diffing, AI, export
│   ├── tests/test_engine.py
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── .env.example        # copy to .env and add your key
└── frontend/
    ├── index.html
    ├── style.css
    ├── app.js
    ├── favicon.svg
    └── og-preview.png
```

## Running tests

```bash
cd ats_web/backend
pip install -r requirements-dev.txt
pytest tests/ -v
```

## Local setup (Windows / PowerShell)

```powershell
cd ats_web\backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env
# open .env and paste your real Gemini key in place of "your-key-here"

uvicorn main:app --reload
```

Open **http://127.0.0.1:8000** — FastAPI serves the frontend directly from
the same server, so there's nothing else to run.

## Local setup (macOS / Linux)

```bash
cd ats_web/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and add your real Gemini key

uvicorn main:app --reload
```

## API reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | GET | Whether Gemini/OCR/PDF export are configured |
| `/api/analyze` | POST (multipart) | Upload resume PDF + job description → score, matched/missing phrases |
| `/api/ai-suggestions` | POST (json) | Structured Gemini rewrite suggestions, incl. suggested path |
| `/api/chat` | POST (json) | Follow-up questions grounded in a specific analysis |
| `/api/apply-rewrites` | POST (json) | Applies AI bullet rewrites back into the resume text |
| `/api/export/markdown` | POST (json) | Download the gap report as Markdown |
| `/api/export/pdf` | POST (json) | Download the gap report as PDF |
| `/api/export/resume-pdf` | POST (json) | Download the *updated* resume as PDF (content-only rebuild) |
| `/api/export/resume-txt` | POST (json) | Download the *updated* resume as plain text |

Rate limits: `/api/analyze` 15/min, `/api/ai-suggestions` 10/min, `/api/chat`
20/min, `/api/apply-rewrites` 15/min — per client IP, in-memory (resets on
restart, and isn't shared across instances if you ever scale beyond one).


## Deploying (verified steps — Render, free tier)

Render's free web service tier doesn't require a credit card, gives 750
hrs/month, and is currently the best free option for this app (checked
against current info, not just assumed). Trade-off: it sleeps after 15 min
of inactivity and takes ~30–60s to wake on the next request — fine for a
portfolio demo, just know the first load after idle time will be slow.

### 1. Push to GitHub

**Important — do this before anything else:** this repo includes a
`.gitignore` that excludes `.env` and `venv/`. Do not remove it or skip it.
If you `git add .` without it, your real Gemini API key gets committed into
Git history — recoverable by anyone with repo access, even after you
delete the file in a later commit.

Run these from **inside the `ats_web` folder specifically** (not its parent) —
that folder's contents (`Dockerfile`, `backend/`, `frontend/`, `README.md`)
need to *be* the repo root, so Render's default build process finds
everything where it expects it, with no extra path configuration:

```powershell
cd C:\Users\Youssef\OneDrive\Desktop\Projects\ResumeMax\ats_web
git init
git add .
git commit -m "Initial ResumeMax release"
git branch -M main
git remote add origin https://github.com/Y-Khedr/ResumeMax.git
git push -u origin main
```

If GitHub rejects the push because the repo already has commits (e.g. you
initialized it with a README on GitHub's side), run
`git pull origin main --allow-unrelated-histories` first, resolve any
conflict, then push.

### 2. Create the Render web service

1. [render.com](https://render.com) → sign up with GitHub → **New +** → **Web Service**.
2. Connect the `Y-Khedr/ResumeMax` repo.
3. Fill in:
   - **Name:** `resumemax`
   - **Root Directory:** leave blank (repo root already *is* `ats_web`'s contents)
   - **Runtime:** Docker — Render will detect and use the `Dockerfile` automatically
   - **Instance Type:** Free
4. Under **Environment Variables**, add:
   - `GEMINI_API_KEY` → your real key
5. **Create Web Service.** First build takes a few minutes; you'll get a URL like `https://resumemax.onrender.com`.

### 3. Confirm it actually works

Once deployed, check `https://resumemax.onrender.com/api/health` in your
browser — you should see `{"ok":true,"gemini_configured":true,...}`. If
`gemini_configured` is `false`, the environment variable didn't get set
correctly on Render's side — double check step 2.4.

---

## Later: Google sign-in + saved history, and Sentry

Not needed yet — you already agreed to sequence these after the core
product is live, and I agree with that. Two notes for when you get there:

- **Sentry (error monitoring):** already wired into `main.py` — it's
  completely inert until you set a `SENTRY_DSN` environment variable. When
  ready: create a free account at [sentry.io](https://sentry.io), create a
  Python/FastAPI project, copy the DSN, add it as an env var on Render. No
  code changes needed at that point.
- **Google sign-in + persistence:** raw Google Cloud OAuth (client ID/secret,
  consent screen, redirect URIs) works, but means building session handling
  and a database yourself. Worth considering
  [Supabase](https://supabase.com)'s free tier instead — it bundles a
  Postgres database *and* built-in Google OAuth in one free service, which
  meaningfully cuts the amount of custom auth code you'd otherwise have to
  write and get right. Also relevant: Render's free tier has **no
  persistent disk**, so if you go the raw-SQLite route your saved history
  would vanish on every redeploy — another reason a hosted DB (Supabase, or
  Render's own free Postgres, which expires after 90 days) is the better
  fit here, not a detail to discover after building it.

## Known limitation

Phrase deduplication occasionally leaves a near-duplicate fragment when a
gazetteer phrase contains a symbol (e.g. both "CI/CD Pipelines" and "cd
pipelines" can appear, since the slash breaks word-matching in the dedupe
step). Cosmetic only — doesn't affect scoring — worth cleaning up later in
`engine.py`'s `_dedupe_subphrases`.

## Testing

```bash
# Backend (API + engine, Gemini mocked — no key or network needed)
cd backend && pip install -r requirements-dev.txt && pytest tests/ -v

# Frontend (optional, dev-only; the app itself needs no Node)
cd frontend-tests && npm install && npm test
```

## How scoring works

With a `GEMINI_API_KEY` set, `/api/analyze` has Gemini extract the job's concrete
requirements and judge each against the resume (so paraphrases match and filler
is ignored). Each claimed match must be backed by evidence that is verified
against the resume text. Without a key, or if the call fails, it falls back to
keyword matching and the response says so (`match_mode: "lexical"`).

## Scoring and matching

`POST /api/analyze` reports how the score was produced in `match_mode`:

- `"ai"` — Gemini extracts the posting's concrete requirements as short skill tags and judges each
  against the resume. Paraphrases count ("scaling design systems" matches "maintaining a scalable
  design system"), filler ("growing team") is ignored, and a match only counts if the evidence the
  model quotes is actually found in the resume text. Score = weighted requirement coverage
  (required 1.0, preferred 0.5). Per-requirement detail is returned in `requirements`.
- `"lexical"` — keyword fallback, used when no `GEMINI_API_KEY` is set or the AI call fails (the
  response says so in `extraction_warning`). Recognized skills count fully, generic n-grams count
  little; matching ignores case, punctuation and plural/singular. Less accurate on long prose postings.

PDF text is extracted with layout preserved, so multi-column resumes keep their columns separate.

## Cover letter

`POST /api/cover-letter` `{resume_text, job_description, analysis_summary?, tone?}` →
`{ok, letter, error}`. `tone` is one of `concise`, `confident`, `conversational`, `leadership`,
`technical`. Rate limited to 10/minute. The letter is an AI draft: the UI tells the user to verify
every claim and fill in any `[bracketed]` placeholders.
