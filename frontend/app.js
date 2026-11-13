const API_BASE = ""; // same-origin, FastAPI serves the frontend too

// ---------------------------------------------------------------------------
// Launch screen — visible for ~3s on first load, then fades out. Runs first
// and independently of everything else below, so a failure anywhere else in
// this file can't leave it stuck on screen. The 5s timer is a pure safety
// net: it force-hides the screen well past the expected duration in case the
// earlier timers somehow don't fire, so this can never block the app.
// ---------------------------------------------------------------------------
(function () {
  const launchScreen = document.getElementById("launch-screen");
  if (!launchScreen) return;
  setTimeout(() => launchScreen.classList.add("is-exiting"), 2600);
  setTimeout(() => { launchScreen.hidden = true; }, 3000);
  setTimeout(() => { launchScreen.hidden = true; }, 5000);
})();

// ---------------------------------------------------------------------------
// Theme toggle (dark / light). Dark is always the starting state on every
// fresh load — deliberately not persisted, so a reload always resets to
// dark regardless of what was toggled during a previous visit. The toggle
// still works normally for the current view.
// ---------------------------------------------------------------------------
const themeToggleBtn = document.getElementById("theme-toggle");
const themeToggleIcon = document.getElementById("theme-toggle-icon");

function applyTheme(theme) {
  if (theme === "light") {
    document.documentElement.setAttribute("data-theme", "light");
    themeToggleIcon.textContent = "☀️";
  } else {
    document.documentElement.removeAttribute("data-theme");
    themeToggleIcon.textContent = "🌙";
  }
}

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

themeToggleBtn.addEventListener("click", () => {
  applyTheme(currentTheme() === "light" ? "dark" : "light");
});

// ---------------------------------------------------------------------------
// Side navigation — enables/activates jump-to-section links as each part
// of the page actually becomes available, instead of linking to sections
// that are still empty/hidden.
// ---------------------------------------------------------------------------
const sideNavLinks = document.querySelectorAll(".side-nav-link");

function enableSideNavLink(id) {
  const link = document.getElementById(id);
  if (!link) return;
  link.classList.remove("is-disabled");
  link.removeAttribute("aria-disabled");
}

function setActiveSideNavLink(id) {
  sideNavLinks.forEach((link) => link.classList.remove("is-active"));
  const link = document.getElementById(id);
  if (link) link.classList.add("is-active");
}

sideNavLinks.forEach((link) => {
  link.addEventListener("click", (e) => {
    if (link.classList.contains("is-disabled")) return; // no-op link, nothing to jump to yet
    if (link.id === "nav-chat") {
      // The chat panel is a fixed-position slide-out drawer, not a normal
      // in-flow section — a native anchor jump to it does nothing useful
      // (it's off-screen/hidden until opened). Open the drawer instead.
      e.preventDefault();
      openChatDrawer();
    }
    setActiveSideNavLink(link.id);
  });
});

// Scroll-spy: keeps the active link in sync with whichever section is
// actually in view, instead of only updating on click. Chat isn't included
// here since it's a drawer, not a scrollable section — its active state is
// set explicitly when opened/clicked above.
const SCROLL_SPY_SECTIONS = [
  { id: "app", navId: "nav-inputs" },
  { id: "results-section", navId: "nav-results" },
  { id: "recommendations-anchor", navId: "nav-recommendations" },
  { id: "export-section", navId: "nav-export" },
  { id: "cover-letter-section", navId: "nav-cover-letter" },
];

const scrollSpyObserver = new IntersectionObserver(
  (entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting);
    if (visible.length === 0) return;
    // If multiple sections are partially visible at once, prefer whichever
    // one is closest to the top of the viewport.
    visible.sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
    const target = SCROLL_SPY_SECTIONS.find((s) => s.id === visible[0].target.id);
    if (target) setActiveSideNavLink(target.navId);
  },
  { rootMargin: "-15% 0px -70% 0px", threshold: 0 }
);

SCROLL_SPY_SECTIONS.forEach(({ id }) => {
  const el = document.getElementById(id);
  if (el) scrollSpyObserver.observe(el);
});

// ---------------------------------------------------------------------------
// Resume dropzone
// ---------------------------------------------------------------------------
const dropzone = document.getElementById("dropzone");
const dropzoneContent = document.getElementById("dropzone-content");
const resumeInput = document.getElementById("resume-input");
let selectedFile = null;

function setFile(file) {
  if (!file) return;
  if (file.type !== "application/pdf") {
    showError("Please upload a PDF file.");
    return;
  }
  // Selecting a new file always fully replaces the old one — no delete
  // step needed, no stale reference left behind.
  selectedFile = file;
  dropzone.classList.add("has-file");
  dropzoneContent.innerHTML = `
    <span class="dropzone-icon">✓</span>
    <span class="dropzone-text">${escapeHtml(file.name)}</span>
    <span class="dropzone-hint">click to replace</span>
  `;
}

resumeInput.addEventListener("change", (e) => {
  setFile(e.target.files[0]);
  // Clear the input's value so choosing the *same* filename again still
  // fires a change event next time, instead of silently doing nothing.
  e.target.value = "";
});

["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
  })
);
dropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (file) setFile(file);
});

// The dropzone is a <label> with a manually-added role="button"/tabindex
// for keyboard access (its actual <input> is visually hidden, which pulls
// it out of the tab order entirely) — but ARIA role alone doesn't give a
// browser automatic Enter/Space activation, so that has to be wired up here.
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    resumeInput.click();
  }
});

// ---------------------------------------------------------------------------
// Error banner
// ---------------------------------------------------------------------------
const errorLine = document.getElementById("error-line");
function showError(msg) {
  errorLine.textContent = msg;
  errorLine.hidden = false;
}
function clearError() {
  errorLine.hidden = true;
}

// ---------------------------------------------------------------------------
// Scan flow
// ---------------------------------------------------------------------------
const scanBtn = document.getElementById("scan-btn");
const scanBtnText = document.getElementById("scan-btn-text");
const jobInput = document.getElementById("job-input");
const ocrToggle = document.getElementById("ocr-toggle");
const jobTitleInput = document.getElementById("job-title-input");
const resultsSection = document.getElementById("results-section");
const scanSweep = document.getElementById("scan-sweep");

let lastResult = null; // {score, matched, missing} for export
let lastAI = null;     // ai suggestions payload for export
let lastResumeTextForApply = null;
let lastBulletRewritesForApply = [];
let lastUpdatedResumeText = null;

scanBtn.addEventListener("click", async () => {
  clearError();

  if (!selectedFile) return showError("Upload a resume PDF first.");
  if (!jobInput.value.trim()) return showError("Paste a job description first.");

  scanBtn.disabled = true;
  scanBtnText.textContent = "Scanning…";
  scanSweep.classList.add("is-active"); // magnifying-glass sweep over the resume card

  try {
    const formData = new FormData();
    formData.append("resume", selectedFile);
    formData.append("job_description", jobInput.value);
    formData.append("ocr_fallback", ocrToggle.checked);

    const res = await fetch(`${API_BASE}/api/analyze`, { method: "POST", body: formData });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `Scan failed (${res.status})`);
    }
    const data = await res.json();
    lastResult = data;

    renderResults(data);
    resultsSection.hidden = false;
    resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
    enableSideNavLink("nav-results");
    enableSideNavLink("nav-export"); // export works off score/diff data alone, doesn't need AI to succeed
    enableSideNavLink("nav-cover-letter");
    resetCoverLetter(data.resume_text, data.job_description); // never show a letter written for a previous scan
    setActiveSideNavLink("nav-results");

    fetchAISuggestions(data.resume_text, data.job_description);
  } catch (err) {
    showError(err.message || "Something went wrong during the scan.");
  } finally {
    scanBtn.disabled = false;
    scanBtnText.textContent = "Run scan";
    scanSweep.classList.remove("is-active");
  }
});

function renderResults(data) {
  // Score ring
  const ring = document.getElementById("score-ring-fill");
  const circumference = 2 * Math.PI * 70;
  const offset = circumference - (Math.min(data.score, 100) / 100) * circumference;
  ring.style.strokeDasharray = `${circumference}`;
  requestAnimationFrame(() => {
    ring.style.strokeDashoffset = offset;
  });
  ring.style.stroke = data.score >= 65 ? "var(--match)" : data.score >= 40 ? "#FBBF24" : "var(--missing)";

  document.getElementById("score-number").textContent = Math.round(data.score);
  const note = document.getElementById("score-note");
  note.textContent =
    data.score >= 65 ? "Solid match." : data.score >= 40 ? "Partial match — some real gaps." : "Low match — review the gaps below.";

  // Header fit chip — small persistent readout once a scan has run
  const fitChip = document.getElementById("fit-chip");
  const fitChipText = document.getElementById("fit-chip-text");
  fitChipText.textContent = `${Math.round(data.score)}% fit`;
  fitChip.hidden = false;

  document.getElementById("matched-count").textContent = data.matched.length;
  document.getElementById("missing-count").textContent = data.missing.length;

  const matchedContainer = document.getElementById("matched-lines");
  const missingContainer = document.getElementById("missing-lines");
  matchedContainer.innerHTML = "";
  missingContainer.innerHTML = "";

  if (data.matched.length === 0) {
    matchedContainer.innerHTML = `<div class="diff-lines-empty">no matches found</div>`;
  } else {
    data.matched.forEach((p, i) => matchedContainer.appendChild(diffLineEl(`+ ${p}`, "add", i)));
  }

  if (data.missing.length === 0) {
    missingContainer.innerHTML = `<div class="diff-lines-empty">no gaps detected</div>`;
  } else {
    data.missing.forEach((p, i) => missingContainer.appendChild(diffLineEl(`- ${p}`, "del", i)));
  }

  const extractionNote = document.getElementById("extraction-note");
  if (data.extraction_warning) {
    extractionNote.textContent = `note: ${data.extraction_warning}`;
    extractionNote.hidden = false;
  } else if (data.extraction_method === "ocr") {
    extractionNote.textContent = "note: text recovered via OCR (scanned PDF detected).";
    extractionNote.hidden = false;
  } else {
    extractionNote.hidden = true;
  }
}

function diffLineEl(text, type, i) {
  const el = document.createElement("div");
  el.className = `diff-line diff-line--${type}`;
  el.textContent = text;
  el.style.animationDelay = `${Math.min(i * 0.03, 0.6)}s`;
  return el;
}

// ---------------------------------------------------------------------------
// AI suggestions (with simulated progress — see note in the UI: this is an
// estimate, not a real completion percentage, since Gemini's structured
// output isn't streamed)
// ---------------------------------------------------------------------------
const AI_STATUS_MESSAGES = [
  "Working…",
  "Scanning…",
  "Comparing…",
  "Reviewing…",
  "Finishing…",
];

async function fetchAISuggestions(resumeText, jobDescription) {
  const loading = document.getElementById("ai-loading");
  const content = document.getElementById("ai-content");
  const errorEl = document.getElementById("ai-error");
  const statusEl = document.getElementById("ai-loading-status");
  const timeEl = document.getElementById("ai-loading-time");
  const fillEl = document.getElementById("ai-progress-fill");
  const trackEl = document.getElementById("ai-progress-track");

  loading.hidden = false;
  content.hidden = true;
  errorEl.hidden = true;
  fillEl.classList.remove("is-done");
  fillEl.style.transform = "scaleX(0)";
  trackEl.setAttribute("aria-valuenow", "0");

  const startTime = performance.now();
  let statusIndex = 0;
  statusEl.textContent = AI_STATUS_MESSAGES[0];

  const statusInterval = setInterval(() => {
    statusIndex = Math.min(statusIndex + 1, AI_STATUS_MESSAGES.length - 1);
    statusEl.textContent = AI_STATUS_MESSAGES[statusIndex];
  }, 3200);

  const progressInterval = setInterval(() => {
    const elapsedSec = (performance.now() - startTime) / 1000;
    timeEl.textContent = `${Math.round(elapsedSec)}s`;
    // Eases toward ~90% and never quite reaches it — an honest "still
    // working" signal rather than a fake exact percentage. Snaps to 100%
    // only once the real response actually arrives (below).
    const pct = 90 * (1 - Math.exp(-elapsedSec / 7));
    fillEl.style.transform = `scaleX(${(pct / 100).toFixed(3)})`;
    trackEl.setAttribute("aria-valuenow", pct.toFixed(0));
  }, 250);

  function stopProgress(success) {
    clearInterval(statusInterval);
    clearInterval(progressInterval);
    if (success) {
      fillEl.style.transform = "scaleX(1)";
      trackEl.setAttribute("aria-valuenow", "100");
      fillEl.classList.add("is-done");
    }
  }

  try {
    const res = await fetch(`${API_BASE}/api/ai-suggestions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resume_text: resumeText, job_description: jobDescription }),
    });
    const data = await res.json();
    lastAI = data;
    stopProgress(data.ok);

    // Brief pause so the 100% fill is actually visible before the panel swaps.
    await new Promise((r) => setTimeout(r, 250));
    loading.hidden = true;

    if (!data.ok) {
      errorEl.textContent = data.error || "AI suggestions unavailable.";
      errorEl.hidden = false;
      return;
    }

    document.getElementById("ai-summary").textContent = data.summary;

    const kwContainer = document.getElementById("priority-keywords");
    kwContainer.innerHTML = "";
    data.missing_keywords.forEach((k) => {
      const chip = document.createElement("span");
      chip.className = "keyword-chip";
      chip.textContent = k;
      kwContainer.appendChild(chip);
    });

    const bulletList = document.getElementById("bullet-list");
    bulletList.innerHTML = "";
    data.bullet_rewrites.forEach((b) => {
      const card = document.createElement("div");
      card.className = "bullet-card";
      card.innerHTML = `
        <div class="bullet-row bullet-row--del"><span class="prefix">-</span><span>${escapeHtml(b.original)}</span></div>
        <div class="bullet-row bullet-row--add"><span class="prefix">+</span><span>${escapeHtml(b.rewritten)}</span></div>
        <div class="bullet-why">${escapeHtml(b.reasoning)}</div>
      `;
      bulletList.appendChild(card);
    });

    // Writing enhancements
    const writingTips = document.getElementById("writing-tips");
    writingTips.innerHTML = "";
    (data.writing_enhancements || []).forEach((tip) => {
      const li = document.createElement("li");
      li.textContent = tip;
      writingTips.appendChild(li);
    });

    // Readiness roadmap
    const roadmap = data.readiness_roadmap || {};
    const projectsEl = document.getElementById("roadmap-projects");
    const skillsEl = document.getElementById("roadmap-skills");
    projectsEl.innerHTML = "";
    skillsEl.innerHTML = "";
    (roadmap.recommended_projects || []).forEach((p) => {
      const li = document.createElement("li");
      li.textContent = p;
      projectsEl.appendChild(li);
    });
    (roadmap.skills_to_learn || []).forEach((s) => {
      const li = document.createElement("li");
      li.textContent = s;
      skillsEl.appendChild(li);
    });
    document.getElementById("roadmap-gaps").textContent =
      roadmap.experience_gaps || "None noted.";

    // Suggested path
    const path = data.suggested_path || {};
    document.getElementById("path-overview").textContent =
      path.overview || "No path generated.";
    const phasesEl = document.getElementById("path-phases");
    phasesEl.innerHTML = "";
    (path.phases || []).forEach((phase, i) => {
      const card = document.createElement("div");
      card.className = "path-phase-card";
      const actionsHtml = (phase.actions || [])
        .map((a) => `<li>${escapeHtml(a)}</li>`)
        .join("");
      card.innerHTML = `
        <span class="path-phase-number">${i + 1}</span>
        <div class="path-phase-header">
          <span class="path-phase-title">${escapeHtml(phase.title || `Phase ${i + 1}`)}</span>
          <span class="path-phase-duration">${escapeHtml(phase.duration_estimate || "timing TBD")}</span>
        </div>
        <ul class="tip-list">${actionsHtml}</ul>
      `;
      phasesEl.appendChild(card);
    });

    // Enable "apply rewrites" against this specific resume/bullet set
    lastResumeTextForApply = resumeText;
    lastAnalysisSummary = data.summary || "";
    lastBulletRewritesForApply = data.bullet_rewrites || [];
    document.getElementById("apply-rewrites-status").textContent = "";
    document.getElementById("apply-rewrites-downloads").hidden = true;

    content.hidden = false;
    enableSideNavLink("nav-recommendations");
    initChat(resumeText, jobDescription, data.summary || "");
  } catch (err) {
    stopProgress(false);
    loading.hidden = true;
    errorEl.textContent = "Couldn't reach the AI suggestions service.";
    errorEl.hidden = false;
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------
function buildExportPayload() {
  return {
    score: lastResult.score,
    matched: lastResult.matched,
    missing: lastResult.missing,
    job_title: jobTitleInput.value || "Target Role",
    ai_summary: lastAI?.summary || "",
    ai_missing_keywords: lastAI?.missing_keywords || [],
    ai_bullet_rewrites: lastAI?.bullet_rewrites || [],
    ai_writing_enhancements: lastAI?.writing_enhancements || [],
    ai_readiness_roadmap: lastAI?.readiness_roadmap || {
      recommended_projects: [], skills_to_learn: [], experience_gaps: "",
    },
    ai_error: lastAI?.ok === false ? lastAI.error : null,
  };
}

async function downloadExport(endpoint, filename) {
  if (!lastResult) return;
  const res = await fetch(`${API_BASE}${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(buildExportPayload()),
  });
  if (!res.ok) {
    showError("Export failed. Try again.");
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

document.getElementById("export-md-btn").addEventListener("click", () =>
  downloadExport("/api/export/markdown", "resumemax_report.md")
);
document.getElementById("export-pdf-btn").addEventListener("click", () =>
  downloadExport("/api/export/pdf", "resumemax_report.pdf")
);

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------
const chatHeading = document.getElementById("chat-heading");
const chatPanel = document.getElementById("chat-panel");
const chatMessages = document.getElementById("chat-messages");
const chatLoading = document.getElementById("chat-loading");
const chatInput = document.getElementById("chat-input");
const chatSendBtn = document.getElementById("chat-send-btn");
const chatLauncher = document.getElementById("chat-launcher");
const chatScrim = document.getElementById("chat-scrim");
const chatCloseBtn = document.getElementById("chat-close-btn");

let chatContext = null;   // { resumeText, jobDescription, analysisSummary }
let chatHistory = [];     // [{role, content}, ...] sent back to the server each turn

function initChat(resumeText, jobDescription, analysisSummary) {
  chatContext = { resumeText, jobDescription, analysisSummary };
  chatHistory = []; // fresh conversation for each new scan
  chatLauncher.hidden = false;
  enableSideNavLink("nav-chat");
}

function openChatDrawer() {
  chatScrim.hidden = false;
  chatPanel.hidden = false;
  // Two ticks: `hidden` must clear (display:none -> block) before adding
  // the class that animates the transform, or the transition won't run.
  requestAnimationFrame(() => requestAnimationFrame(() => chatPanel.classList.add("is-open")));
  chatLauncher.classList.add("is-hidden");
  chatInput.focus();
}

function closeChatDrawer() {
  chatPanel.classList.remove("is-open");
  chatLauncher.classList.remove("is-hidden");
  setTimeout(() => {
    chatPanel.hidden = true;
    chatScrim.hidden = true;
  }, 300); // matches the CSS transition duration, so it hides only after sliding out
}

chatLauncher.addEventListener("click", openChatDrawer);
chatCloseBtn.addEventListener("click", closeChatDrawer);
chatScrim.addEventListener("click", closeChatDrawer);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && chatPanel.classList.contains("is-open")) closeChatDrawer();
});

function appendChatBubble(role, text) {
  const bubble = document.createElement("div");
  bubble.className =
    role === "user" ? "chat-bubble chat-bubble--user"
    : role === "error" ? "chat-bubble chat-bubble--error"
    : "chat-bubble chat-bubble--assistant";
  bubble.textContent = text;
  chatMessages.appendChild(bubble);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return bubble;
}

async function sendChatMessage() {
  const text = chatInput.value.trim();
  if (!text || !chatContext) return;

  chatInput.value = "";
  chatInput.disabled = true;
  chatSendBtn.disabled = true;

  appendChatBubble("user", text);
  chatHistory.push({ role: "user", content: text });
  chatLoading.hidden = false;

  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: chatHistory,
        resume_text: chatContext.resumeText,
        job_description: chatContext.jobDescription,
        analysis_summary: chatContext.analysisSummary,
      }),
    });
    const data = await res.json();

    if (!data.ok) {
      appendChatBubble("error", data.error || "Couldn't get a reply. Try again.");
    } else {
      appendChatBubble("assistant", data.reply);
      chatHistory.push({ role: "assistant", content: data.reply });
    }
  } catch (err) {
    appendChatBubble("error", "Couldn't reach the chat service.");
  } finally {
    chatLoading.hidden = true;
    chatInput.disabled = false;
    chatSendBtn.disabled = false;
    chatInput.focus();
  }
}

chatSendBtn.addEventListener("click", sendChatMessage);
chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
});

// ---------------------------------------------------------------------------
// Apply rewrites back to the resume
// ---------------------------------------------------------------------------
const applyRewritesBtn = document.getElementById("apply-rewrites-btn");
const applyRewritesStatus = document.getElementById("apply-rewrites-status");
const applyRewritesDownloads = document.getElementById("apply-rewrites-downloads");

applyRewritesBtn.addEventListener("click", async () => {
  if (!lastResumeTextForApply || lastBulletRewritesForApply.length === 0) {
    applyRewritesStatus.textContent = "No rewrites to apply yet.";
    return;
  }

  applyRewritesBtn.disabled = true;
  applyRewritesStatus.textContent = "Applying…";
  applyRewritesDownloads.hidden = true;

  try {
    const res = await fetch(`${API_BASE}/api/apply-rewrites`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        resume_text: lastResumeTextForApply,
        bullet_rewrites: lastBulletRewritesForApply,
      }),
    });
    if (!res.ok) throw new Error(`Apply failed (${res.status})`);
    const data = await res.json();

    lastUpdatedResumeText = data.updated_resume_text;

    if (data.applied_count === data.total_count) {
      applyRewritesStatus.textContent = `Applied all ${data.applied_count} rewrite(s).`;
    } else {
      applyRewritesStatus.textContent =
        `Applied ${data.applied_count}/${data.total_count} — ${data.unmatched.length} couldn't be ` +
        `auto-located (resume wording likely differs slightly). Those need a manual edit.`;
    }

    applyRewritesDownloads.hidden = false;
  } catch (err) {
    applyRewritesStatus.textContent = "Couldn't apply rewrites — try again.";
  } finally {
    applyRewritesBtn.disabled = false;
  }
});

async function downloadUpdatedResume(endpoint, filename) {
  if (!lastUpdatedResumeText) return;
  const res = await fetch(`${API_BASE}${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resume_text: lastUpdatedResumeText }),
  });
  if (!res.ok) {
    showError("Resume export failed. Try again.");
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

document.getElementById("download-resume-pdf-btn").addEventListener("click", () =>
  downloadUpdatedResume("/api/export/resume-pdf", "resumemax_updated_resume.pdf")
);
document.getElementById("download-resume-txt-btn").addEventListener("click", () =>
  downloadUpdatedResume("/api/export/resume-txt", "resumemax_updated_resume.txt")
);


// ---------------------------------------------------------------------------
// Cover letter — optional step after the scan. Reuses the resume and job
// text from the scan, so there is nothing to re-enter.
// ---------------------------------------------------------------------------
const coverLetterReady = document.getElementById("cover-letter-ready");
const coverLetterLoading = document.getElementById("cover-letter-loading");
const coverLetterError = document.getElementById("cover-letter-error");
const coverLetterResult = document.getElementById("cover-letter-result");
const coverLetterTextarea = document.getElementById("cover-letter-textarea");
const coverLetterStatus = document.getElementById("cover-letter-status");
const coverLetterCopyStatus = document.getElementById("cover-letter-copy-status");
const coverLetterGenerateBtn = document.getElementById("cover-letter-generate-btn");
const coverLetterRegenerateBtn = document.getElementById("cover-letter-regenerate-btn");
const coverLetterCopyBtn = document.getElementById("cover-letter-copy-btn");
const coverLetterDownloadBtn = document.getElementById("cover-letter-download-btn");
const toneChips = document.querySelectorAll(".tone-chip");

let lastAnalysisResumeText = null;
let lastAnalysisJobDescription = null;
let lastAnalysisSummary = "";
let coverLetterTone = null;        // currently applied tone key, or null
let lastGeneratedLetter = "";      // what the model last returned, to detect user edits
let coverLetterRequestId = 0;      // lets us ignore a response that arrives after a newer scan/request

function setCoverLetterBusy(busy) {
  coverLetterGenerateBtn.disabled = busy;
  coverLetterRegenerateBtn.disabled = busy;
  toneChips.forEach((chip) => { chip.disabled = busy; });
}

function resetCoverLetter(resumeText, jobDescription) {
  coverLetterRequestId++; // invalidates any request still in flight for the previous scan
  lastAnalysisResumeText = resumeText;
  lastAnalysisJobDescription = jobDescription;
  lastAnalysisSummary = "";
  coverLetterTone = null;
  lastGeneratedLetter = "";
  coverLetterTextarea.value = "";
  toneChips.forEach((chip) => chip.classList.remove("is-active"));
  coverLetterResult.hidden = true;
  coverLetterLoading.hidden = true;
  coverLetterError.hidden = true;
  coverLetterReady.hidden = false;
  coverLetterStatus.textContent = "optional";
  setCoverLetterBusy(false);
}

function userHasEditedLetter() {
  return lastGeneratedLetter !== "" && coverLetterTextarea.value !== lastGeneratedLetter;
}

async function generateCoverLetter(tone) {
  if (!lastAnalysisResumeText || !lastAnalysisJobDescription) return;

  // Don't silently throw away text the user has been editing.
  if (userHasEditedLetter() &&
      !window.confirm("Regenerating will replace your edits to the letter. Continue?")) {
    return;
  }

  const requestId = ++coverLetterRequestId;
  coverLetterTone = tone;
  toneChips.forEach((chip) => chip.classList.toggle("is-active", chip.dataset.tone === tone));

  coverLetterError.hidden = true;
  coverLetterReady.hidden = true;
  coverLetterResult.hidden = true;
  coverLetterLoading.hidden = false;
  coverLetterStatus.textContent = "writing";
  setCoverLetterBusy(true);

  const showError = (message) => {
    coverLetterLoading.hidden = true;
    coverLetterError.textContent = message;
    coverLetterError.hidden = false;
    // keep whatever letter they already had visible; otherwise offer the button again
    if (lastGeneratedLetter) coverLetterResult.hidden = false;
    else coverLetterReady.hidden = false;
    coverLetterStatus.textContent = lastGeneratedLetter ? "generated" : "optional";
    setCoverLetterBusy(false);
  };

  try {
    const res = await fetch(`${API_BASE}/api/cover-letter`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        resume_text: lastAnalysisResumeText,
        job_description: lastAnalysisJobDescription,
        analysis_summary: lastAnalysisSummary,
        tone: tone,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (requestId !== coverLetterRequestId) return; // a newer scan/request superseded this one

    if (!res.ok || !data.ok) {
      showError(data.error || "Couldn't write the cover letter. Try again.");
      return;
    }
    lastGeneratedLetter = data.letter;
    coverLetterTextarea.value = data.letter;
    coverLetterLoading.hidden = true;
    coverLetterResult.hidden = false;
    coverLetterStatus.textContent = "generated";
    setCoverLetterBusy(false);
  } catch (err) {
    if (requestId !== coverLetterRequestId) return;
    showError("Couldn't reach the cover letter service.");
  }
}

coverLetterGenerateBtn.addEventListener("click", () => generateCoverLetter(null));
coverLetterRegenerateBtn.addEventListener("click", () => generateCoverLetter(coverLetterTone));
toneChips.forEach((chip) => {
  chip.addEventListener("click", () => {
    // clicking the active tone again clears it
    generateCoverLetter(coverLetterTone === chip.dataset.tone ? null : chip.dataset.tone);
  });
});

coverLetterCopyBtn.addEventListener("click", async () => {
  let copied = false;
  try {
    await navigator.clipboard.writeText(coverLetterTextarea.value);
    copied = true;
  } catch (_) {
    // clipboard API needs a secure context; fall back to the older route
    try {
      coverLetterTextarea.select();
      copied = document.execCommand("copy");
    } catch (__) { /* fall through */ }
  }
  coverLetterCopyStatus.textContent = copied ? "Copied." : "Couldn't copy. Select the text and copy it manually.";
  setTimeout(() => { coverLetterCopyStatus.textContent = ""; }, 2500);
});

coverLetterDownloadBtn.addEventListener("click", () => {
  // Honest label: this is plain text, so the button says .txt, not PDF.
  const blob = new Blob([coverLetterTextarea.value], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "cover_letter.txt";
  a.click();
  URL.revokeObjectURL(url);
});
