// Simulates the whole UI in jsdom with the backend mocked. Run: node frontend_tests/full_site.test.js
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const read = (f) => fs.readFileSync(path.join(__dirname, '..', 'frontend', f), 'utf8');

const dom = new JSDOM(read('index.html'), { url: 'http://127.0.0.1:8000/', runScripts: 'dangerously', pretendToBeVisual: true });
const { window } = dom;
window.Element.prototype.scrollIntoView = () => {};
window.performance = window.performance || { now: () => Date.now() };
window.IntersectionObserver = class { observe() {} disconnect() {} };

const fetchLog = [];
window.fetch = async (url) => {
  fetchLog.push(url);
  if (url.includes('/api/analyze')) return { ok: true, json: async () => ({
    score: 85.5, matched: ['Figma', 'Adobe XD'], missing: ['Kubernetes'], job_phrases: ['Figma', 'Adobe XD', 'Kubernetes'],
    extraction_method: 'native', extraction_warning: null, resume_text: 'resume text', job_description: 'job text',
    match_mode: 'ai', requirements: [] }) };
  if (url.includes('/api/ai-suggestions')) return { ok: true, json: async () => ({
    ok: true, summary: 'Strong fit overall.', missing_keywords: ['Kubernetes'],
    bullet_rewrites: [{ original: 'old bullet', rewritten: 'new bullet', reasoning: 'clearer' }],
    writing_enhancements: ['Use metrics'],
    readiness_roadmap: { recommended_projects: ['Build X'], skills_to_learn: ['K8s'], experience_gaps: 'No cluster work' },
    suggested_path: { overview: 'A 6 week plan.', phases: [{ title: 'Phase 1', duration_estimate: '2 weeks', actions: ['Learn K8s'] }] },
    error: null }) };
  if (url.includes('/api/apply-rewrites')) return { ok: true, json: async () => ({ updated_resume_text: 'updated', applied_count: 1, total_count: 1, unmatched: [] }) };
  if (url.includes('/api/chat')) return { ok: true, json: async () => ({ ok: true, reply: 'Focus on Kubernetes first.', error: null }) };
  if (url.includes('/api/export/')) return { ok: true, blob: async () => new window.Blob(['x']) };
  throw new Error('unexpected fetch: ' + url);
};
window.URL.createObjectURL = () => 'blob:fake';
window.URL.revokeObjectURL = () => {};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

window.addEventListener('load', async () => {
  try {
    const s = window.document.createElement('script');
    s.textContent = read('app.js');
    window.document.body.appendChild(s);
    await wait(50);
    const doc = window.document;
    const $ = (id) => doc.getElementById(id);

    $('theme-toggle').click();
    const lightOn = doc.documentElement.getAttribute('data-theme') === 'light';
    $('theme-toggle').click();
    const darkBack = doc.documentElement.getAttribute('data-theme') !== 'light';

    const fileInput = $('resume-input');
    Object.defineProperty(fileInput, 'files', { value: [new window.File(['x'], 'x.txt', { type: 'text/plain' })], configurable: true });
    fileInput.dispatchEvent(new window.Event('change'));
    const rejectedNonPdf = !$('error-line').hidden;

    const callsBefore = fetchLog.length;
    $('scan-btn').click();
    const validationBlocked = fetchLog.length === callsBefore;

    Object.defineProperty(fileInput, 'files', { value: [new window.File(['%PDF-1.4'], 'cv.pdf', { type: 'application/pdf' })], configurable: true });
    fileInput.dispatchEvent(new window.Event('change'));
    $('job-input').value = 'job text';
    $('scan-btn').click();
    await wait(1300);

    const scoreShown = ['85', '86'].includes($('score-number').textContent);
    const matchedShown = $('matched-lines').textContent.includes('Figma');
    const missingShown = $('missing-lines').textContent.includes('Kubernetes');
    const fitChip = !$('fit-chip').hidden;
    const aiShown = !$('ai-content').hidden && $('ai-summary').textContent.includes('Strong fit');
    const pathShown = $('path-phases').textContent.includes('Phase 1');
    const bulletShown = $('bullet-list').textContent.includes('new bullet');
    const loaderGone = $('ai-loading').hidden === true;
    const navEnabled = ['nav-results', 'nav-recommendations', 'nav-export', 'nav-cover-letter', 'nav-chat']
      .every((id) => !$(id).classList.contains('is-disabled'));

    $('apply-rewrites-btn').click();
    await wait(300);
    const applied = $('apply-rewrites-status').textContent.includes('Applied all 1');
    const downloadsShown = !$('apply-rewrites-downloads').hidden;

    $('export-md-btn').click(); $('export-pdf-btn').click();
    await wait(300);
    const exportsCalled = fetchLog.some((u) => u.includes('/export/markdown')) && fetchLog.some((u) => u.includes('/export/pdf'));

    $('chat-launcher').click();
    await wait(60);
    const drawerOpen = $('chat-panel').classList.contains('is-open');
    $('chat-input').value = 'what first?';
    $('chat-send-btn').click();
    await wait(300);
    const chatReply = $('chat-messages').textContent.includes('Focus on Kubernetes first.');
    doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    await wait(350);
    const closedByEsc = $('chat-panel').hidden === true;

    $('nav-chat').click();
    await wait(60);
    const sidebarOpens = $('chat-panel').classList.contains('is-open');

    await wait(3000);
    const launchGone = $('launch-screen').hidden === true;

    const results = {
      'theme -> light': lightOn, 'theme -> dark': darkBack, 'non-PDF rejected': rejectedNonPdf,
      'empty scan blocked client-side (no request)': validationBlocked,
      'score renders': scoreShown, 'matched list renders': matchedShown, 'missing list renders': missingShown,
      'header fit chip appears': fitChip, 'AI summary renders': aiShown, 'suggested path renders': pathShown,
      'bullet rewrites render': bulletShown, 'AI loader hidden after completion': loaderGone,
      'all sidebar links enabled (incl. cover letter)': navEnabled,
      'apply-rewrites works': applied, 'resume download buttons appear': downloadsShown,
      'markdown + pdf export endpoints called': exportsCalled,
      'chat drawer opens via launcher': drawerOpen, 'chat reply renders': chatReply,
      'Escape closes drawer': closedByEsc, 'sidebar Chat link opens drawer': sidebarOpens,
      'launch screen dismissed': launchGone,
    };
    let all = true;
    for (const [k, v] of Object.entries(results)) { console.log(v ? 'PASS' : 'FAIL', '-', k); if (!v) all = false; }
    console.log(all ? '\nALL PASS' : '\nSOME FAILED');
    process.exit(all ? 0 : 1);
  } catch (e) { console.error('TEST ERROR:', e.stack); process.exit(1); }
});
