# ResumeMax — Improvement Roadmap

Rating scale: **5 = neutral / doesn't matter much**, **below 5 = actively not worth doing right now**, **above 5 = worth doing**, **10 = the single highest-leverage thing you could do next**.

These are ranked *for your actual stated goal*: a portfolio piece that could genuinely become a used product. A few of these would rate differently if this were purely an internal tool or purely a CV bullet point — noted where relevant.

---

## Do these first (8–10)

### 1. Deploy it to a real public URL — **10**
Right now this only runs on `localhost`. A link in your CV/LinkedIn that actually opens a working product is worth more than everything else on this list combined — it's the difference between "I built this" and "here, try it." Render, Railway, or Fly.io all have free/cheap tiers that run a FastAPI app with almost no config changes needed (your `.env` becomes a dashboard environment variable instead of a file).

### 2. Rate limiting on `/api/analyze` and `/api/ai-suggestions` — **9**
The moment this is public, anyone can hit these endpoints in a loop and either rack up your Gemini bill or just break the app for everyone else. This isn't optional once deployed — it's the difference between "portfolio project" and "someone's $400 surprise API bill." FastAPI has simple middleware for this (`slowapi` is the common choice).

### 3. Automated tests (pytest for `engine.py`) — **8**
We found two real bugs by hand this conversation (the duplicate-function bug from a bad edit, and the stuck-loading CSS bug) — both are exactly the kind of thing a test suite catches automatically before you ship, instead of a user finding them for you. Given this is heading toward "real people might use it," this stops being optional pretty quickly. Also a genuinely strong CV signal on its own.

### 4. Server-side upload validation (size + type, enforced, not just suggested) — **8**
The frontend checks file type and shows "15MB" as a hint, but confirm the backend actually *rejects* oversized or malicious files rather than trusting the client. A user (or a bad actor) can bypass anything client-side trivially.

### 5. Privacy policy / data handling disclosure — **8**
People are uploading resumes — real names, addresses sometimes, work history. The moment strangers use this, "what happens to my data" is a real question, and you're also sending it to Google's API, which needs disclosing. This is a legal/trust requirement before genuine public use, not a nice-to-have.

---

## High value, do soon (6–7)

### 6. Actual professional visual polish pass — **7**
You already asked for this and I haven't delivered it yet — refined color palette, spacing consistency, and general "does this look like a funded startup or a hackathon project" pass. The diff/terminal concept is strong; it just needs another 20% of craft.

### 7. Mobile responsiveness — actually test it — **7**
CSS media queries exist but I've never seen this render on an actual phone. Given people will likely open a portfolio link on their phone first, this matters more than it might seem.

### 8. Support `.docx` resume uploads, not just PDF — **7**
A meaningful chunk of people have their resume as a Word doc, not a PDF, and "please convert your file first" is real friction that loses users before they even try the product.

### 9. Let users apply the AI's rewritten bullets back into an editable draft — **7**
Right now the tool tells you what to change but doesn't help you *make* the change. Turning this into "edit your resume text right here, see it update live" is a real differentiator — turns it from an analyzer into an actual resume-building tool.

### 10. Streaming the AI response (typed-out effect) — **7**
Bigger perceived-speed win than the progress bar alone, though more work: instead of one blocking call, stream tokens as they arrive so text visibly appears rather than waiting then dumping everything at once.

### 11. Google sign-in + saved scan history — **7**
You already flagged this as a later-stage item, and I agree with your sequencing — but when you do get here, it's a real value-add: "compare this scan to the one I did last month" is genuinely useful, not just a login wall for its own sake.

### 12. OpenGraph meta tags + favicon — **6**
When you share the link on LinkedIn, right now it'll show up as a bare URL with no preview image or title card. Ten minutes of work, meaningfully better first impression.

### 13. Cache identical resume+job analyses — **6**
If someone re-runs the same resume against the same job description (easy to do by accident), you're paying for a duplicate Gemini call for identical output. Simple hash-based cache saves real cost at scale.

### 14. Accessibility pass (ARIA labels, keyboard nav, contrast check) — **6**
Right now if someone navigates by keyboard or uses a screen reader, this probably doesn't work well. Matters both ethically and for "does this look like a professional product" — accessibility gaps are one of the first things a real engineering reviewer would notice.

### 15. CORS locked to your actual deployed origin instead of `"*"` — **6**
Fine for local development, but once deployed, restrict this to your actual frontend domain — low effort, meaningfully reduces attack surface.

### 16. Tell the user when their resume/job text got truncated — **6**
The prompt caps resume/job text at 4000 characters silently. If someone's resume is long, part of it is invisibly ignored — a small "note: only the first portion of your resume was analyzed" would prevent confusing/inconsistent results.

### 17. CI pipeline (GitHub Actions running tests on push) — **6**
Pairs with item #3 — once you have tests, running them automatically on every push is a very standard, very visible engineering-maturity signal for anyone looking at your repo.

---

## Worth doing eventually, not urgent (4–5)

### 18. Resume version comparison (draft A vs. draft B against the same job) — **6**
Nice differentiator, moderate build effort — good "phase 2" feature once the core product is solid.

### 19. Multi-job comparison (one resume against several postings at once) — **5**
Useful for power users, but a smaller slice of your actual audience than the core single-scan flow.

### 20. Dockerize the app — **6**
Makes deployment more portable and is a clean CV line item, but only matters once you're actually choosing a deployment target — sequence after #1.

### 21. Error monitoring (e.g., Sentry) — **5**
Genuinely useful once there's real traffic to monitor; low value before that, since you'd just be watching an empty dashboard.

### 22. Multi-language resume/job support — **4–5 depending on your audience**
If you expect non-English users, this matters a lot; if not, it's low priority. Worth deciding based on who you actually expect to use this.

### 23. Shareable public link of a scan report — **4**
Sounds nice, but creates real privacy complexity (someone's resume gap analysis, publicly linkable) that isn't worth solving until the core product and privacy policy (#5) are solid.

---

## Lower priority right now (below 5)

### 24. Prompt-injection hardening for resume/job text — **5**
Real in theory (someone could put "ignore previous instructions" in a job description), but low real-world stakes for this specific tool since the output is just advice text, not an action with consequences. Worth a light pass, not a priority.

### 25. Onboarding tooltips / first-time-user walkthrough — **5**
The product is fairly self-explanatory as-is (upload, paste, scan). Probably not worth the effort until you have real usage data showing people are actually confused.

### 26. Micro-interaction polish (hover states, card transition timing) — **4**
Nice, but marginal — you already have a fairly considered animation system (scan sweep, progress bar, diff-line reveals). Diminishing returns from polishing further before the bigger items above are done.

---
