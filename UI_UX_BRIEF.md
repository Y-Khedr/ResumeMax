# ResumeMax — UI/UX Design Brief (for external review)

## What this is

I'm getting a second opinion on the frontend design of a working web app called
ResumeMax. This document is a factual technical brief — current state, exact
values, known issues — so you can give specific, implementable design
direction rather than generic advice.

## What the product does

Upload a resume PDF + paste a job posting → get a git-diff-style comparison
(what matches, what's missing) → AI-generated bullet rewrites, writing
feedback, and a phased "path to job-ready" plan → download an updated resume
or a full report → ask follow-up questions in a chat panel.

Target audience: job seekers, particularly early-career/technical people —
this started as a computer engineering student's portfolio project, with
genuine intent to make it a real, usable public tool.

## Hard technical constraints (please work within these)

- **Plain HTML/CSS/JS. No React, no build step, no npm.** This was a
  deliberate choice to avoid Node tooling complexity for the person
  maintaining this. Any redesign needs to stay in this stack — don't
  suggest a framework migration as part of a visual fix.
- **FastAPI backend serves the frontend directly** as static files. One
  `index.html`, one `style.css`, one `app.js`.
- The **functional elements are real** and load live data: the score
  circle, the matched/missing diff lists, the AI analysis cards, and the
  chat panel are all driven by actual API responses, not static mockups.
  Any redesign has to account for dynamic content (variable-length lists,
  variable-length AI text, empty states, loading states).

## Current design system (exact current values, pulled from the live file)

```css
/* Dark theme (default) */
--bg: #0A0E14;
--surface: #12161F;
--surface-raised: #161B26;
--border: #232A38;
--border-soft: #1A2029;

--text: #E6E9EF;
--text-muted: #7A8394;
--text-faint: #7B838C;

--accent: #6366F1;        /* indigo, used sparingly for CTAs/brand */
--match: #4ADE80;         /* green, "matched" diff color */
--missing: #F87171;       /* red, "missing" diff color */

--font-display: 'IBM Plex Mono', ui-monospace, monospace;  /* headings, data, diff content */
--font-body: 'IBM Plex Sans', -apple-system, sans-serif;   /* body copy */

--radius: 10px;
--radius-lg: 16px;
--card-shadow: 0 1px 2px rgba(0,0,0,0.2), 0 8px 24px rgba(0,0,0,0.16);
```

A light theme exists too (toggleable), with equivalent tokens — white
surfaces, dark text, same accent/match/missing hues adjusted for contrast.

**The underlying design concept:** the whole product is themed around a
literal git-diff. Matched resume phrases render as `+ phrase` in green,
missing ones as `- phrase` in red, monospace — both in a terminal-style hero
mockup and in the actual scan results. This was a deliberate choice tied to
what the product does (show a diff), not decoration for its own sake.

## Page structure (in order)

1. **Hero** — sticky header with brand mark + theme toggle, then a 2-column
   section: headline ("Know exactly what your resume is missing.") + CTA on
   the left, a fake terminal window showing a sample diff on the right.
2. **Input section** ("01 · inputs") — a drag-and-drop resume upload zone
   (left) and a job-description textarea (right), plus an OCR toggle, a job
   title field, and a "Run scan" button.
3. **Results section** ("02 · scan result", hidden until a scan runs) — a
   circular score gauge, then two-column matched/missing diff lists.
4. **AI analysis** ("03 · rewrites") — AI summary text, priority-keyword
   chips, bullet-rewrite cards (before/after/why), an "apply all rewrites"
   button, writing-enhancement tips, a readiness-roadmap grid (projects /
   skills / experience gaps), and a phased "suggested path" section.
5. **Export row** — download buttons for Markdown/PDF reports and the
   updated resume.
6. **Chat panel** ("04 · ask a follow-up") — a message thread + input box
   for follow-up questions about the analysis.

## What's already been fixed (please don't just re-flag these)

- Text-overflow bug where long unbroken strings could blow out a card's
  width — fixed with `overflow-wrap`/`word-break` globally.
- A real PDF-extraction bug where certain resume templates produce
  concatenated text with no spaces (e.g.
  `Commandedastructureddatateamof10+members...`) — fixed at the data layer
  with statistical word-segmentation, not just CSS.
- Native OS scrollbars on the diff lists were visually heavy — replaced
  with a thin custom scrollbar.
- A real accessibility bug: the file-upload dropzone was completely
  unreachable via keyboard (its actual `<input>` was hidden, pulling it out
  of the tab order) — fixed.
- Contrast on the dimmest text color was failing WCAG AA (2.49:1) — fixed
  to 5.04:1 dark / 4.59:1 light (actually computed, not eyeballed).

## The actual, unresolved complaint

The person using this feels the overall visual result still looks unpolished
— "generic," "AI-generated," "hackathon project" — despite the above fixes.
Screenshots of the live app were reviewed and, honestly, didn't show the
specific problems that critique described (no neon colors, no contrast
mess) — but the *feeling* that it's not polished enough is real and worth
taking seriously regardless of whether the specific stated reasons matched
the screenshots.

**What would actually help:** don't give abstract direction ("use better
spacing," "feel more premium"). Give exact, implementable values — specific
hex codes, specific spacing scale (e.g. a real 4px/8px rhythm), specific
font pairing with fallback stack, specific component-level CSS changes —
the kind of thing that can be applied directly rather than interpreted.

## One alternate direction that was proposed and not yet acted on

A prior suggestion proposed moving toward a Vercel/Linear-style aesthetic
instead of the current diff/terminal one: neutral slate background
(`#09090b`), dark gray cards (`#141417`), thin borders (`#27272a`), 6px
corner radius, monochrome/off-white buttons instead of indigo, Inter or
Geist for all UI text (reserving monospace strictly for code/diff content),
and a much more compact hero that gets the input workspace visible
immediately rather than below a large headline section.

If you think that direction (or some other specific direction) is right,
I'd want exact tokens and specific component guidance, not just "go more
minimal."
