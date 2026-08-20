const API_BASE = ""; // same-origin, FastAPI serves the frontend too

// ---------------------------------------------------------------------------
// Theme toggle (dark / light, persisted in localStorage)
// ---------------------------------------------------------------------------
const THEME_KEY = "resumemax-theme";
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

// Sync the icon with whatever the inline head script already applied
// (that script runs before paint to avoid a flash of the wrong theme).
applyTheme(localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark");

themeToggleBtn.addEventListener("click", () => {
  const next = currentTheme() === "light" ? "dark" : "light";
  applyTheme(next);
  localStorage.setItem(THEME_KEY, next);
});

// ---------------------------------------------------------------------------
// Hero terminal — staggered diff-line reveal on load
// ---------------------------------------------------------------------------
const HERO_LINES = [
  { type: "ctx", text: "$ resumemax --resume resume.pdf --job posting.txt" },
  { type: "ctx", text: "" },
  { type: "add", text: "+ Java Spring Boot" },
  { type: "add", text: "+ REST APIs" },
  { type: "del", text: "- Kubernetes" },
  { type: "del", text: "- CI/CD Pipelines" },
  { type: "add", text: "+ PostgreSQL" },
  { type: "ctx", text: "" },
  { type: "ctx", text: "match: 61%   2 gaps found" },
];

function renderHero() {
  const container = document.getElementById("hero-diff-lines");
  HERO_LINES.forEach((line, i) => {
    const el = document.createElement("div");
    el.className = `diff-line diff-line--${line.type}`;
    el.textContent = line.text || "\u00A0";
    el.style.animationDelay = `${0.15 * i + 0.2}s`;
    container.appendChild(el);
  });
}
renderHero();

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
  "Reading the job requirements…",
  "Comparing against your resume…",
  "Drafting bullet rewrites…",
  "Building your readiness roadmap…",
  "Almost there…",
];

async function fetchAISuggestions(resumeText, jobDescription) {
  const loading = document.getElementById("ai-loading");
  const content = document.getElementById("ai-content");
  const errorEl = document.getElementById("ai-error");
  const statusEl = document.getElementById("ai-loading-status");
  const timeEl = document.getElementById("ai-loading-time");
  const fillEl = document.getElementById("ai-progress-fill");

  loading.hidden = false;
  content.hidden = true;
  errorEl.hidden = true;
  fillEl.classList.remove("is-done");
  fillEl.style.width = "0%";

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
    fillEl.style.width = `${pct.toFixed(0)}%`;
  }, 250);

  function stopProgress(success) {
    clearInterval(statusInterval);
    clearInterval(progressInterval);
    if (success) {
      fillEl.style.width = "100%";
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
    lastBulletRewritesForApply = data.bullet_rewrites || [];
    document.getElementById("apply-rewrites-status").textContent = "";
    document.getElementById("apply-rewrites-downloads").hidden = true;

    content.hidden = false;
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

let chatContext = null;   // { resumeText, jobDescription, analysisSummary }
let chatHistory = [];     // [{role, content}, ...] sent back to the server each turn

function initChat(resumeText, jobDescription, analysisSummary) {
  chatContext = { resumeText, jobDescription, analysisSummary };
  chatHistory = []; // fresh conversation for each new scan
  chatHeading.hidden = false;
  chatPanel.hidden = false;
}

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
