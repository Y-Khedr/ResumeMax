// End-to-end simulation of the whole frontend against a mocked API (jsdom).
// Run:  cd frontend-tests && npm install && npm test
const { JSDOM } = require('jsdom');
const FRONT = require('path').join(__dirname, '..', 'frontend');
const fs = require('fs');
const html = fs.readFileSync(FRONT + '/index.html', 'utf8');
const dom = new JSDOM(html, { url: 'http://127.0.0.1:8000/', runScripts: 'dangerously', pretendToBeVisual: true });
const { window } = dom;
window.Element.prototype.scrollIntoView = () => {};
window.performance = window.performance || { now: () => Date.now() };
window.IntersectionObserver = class { observe() {} disconnect() {} };
// Silence jsdom's navigation noise for programmatic *download* links only; real
// navigation anchors (like the sidebar links) must keep their normal click behavior.
const _origAnchorClick = window.HTMLAnchorElement.prototype.click;
window.HTMLAnchorElement.prototype.click = function () { if (this.download) return; return _origAnchorClick.call(this); };

const fetchLog = [];
window.fetch = async (url) => {
  fetchLog.push(url);
  if (url.includes('/api/analyze')) return { ok: true, json: async () => ({
    score: 85.5, matched: ['Figma', 'Adobe XD'], missing: ['Kubernetes'], job_phrases: ['Figma', 'Adobe XD', 'Kubernetes'],
    extraction_method: 'native', extraction_warning: null, resume_text: 'resume text', job_description: 'job text',
    match_mode: 'ai', requirements: [] }) };
  if (url.includes('/api/ai-suggestions')) return { ok: true, json: async () => ({ ok: true, summary: 'Strong fit overall.',
    missing_keywords: ['Kubernetes'], bullet_rewrites: [{ original: 'old bullet', rewritten: 'new bullet', reasoning: 'clearer' }],
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
    s.textContent = fs.readFileSync(FRONT + '/app.js', 'utf8');
    window.document.body.appendChild(s);
    await wait(50);
    const doc = window.document; const $ = (id) => doc.getElementById(id); const R = {};

    $('theme-toggle').click(); R['theme -> light'] = doc.documentElement.getAttribute('data-theme') === 'light';
    $('theme-toggle').click(); R['theme -> dark'] = doc.documentElement.getAttribute('data-theme') !== 'light';

    const fileInput = $('resume-input');
    Object.defineProperty(fileInput, 'files', { value: [new window.File(['x'], 'x.txt', { type: 'text/plain' })], configurable: true });
    fileInput.dispatchEvent(new window.Event('change'));
    R['non-PDF rejected'] = !$('error-line').hidden;

    const before = fetchLog.length; $('scan-btn').click();
    R['empty scan blocked client-side (no request)'] = fetchLog.length === before;

    Object.defineProperty(fileInput, 'files', { value: [new window.File(['%PDF-1.4'], 'cv.pdf', { type: 'application/pdf' })], configurable: true });
    fileInput.dispatchEvent(new window.Event('change'));
    $('job-input').value = 'job text'; $('scan-btn').click(); await wait(1300);

    R['score renders'] = ['85', '86'].includes($('score-number').textContent);
    R['matched list renders'] = $('matched-lines').textContent.includes('Figma');
    R['missing list renders'] = $('missing-lines').textContent.includes('Kubernetes');
    R['header fit chip appears'] = !$('fit-chip').hidden;
    R['AI summary renders'] = !$('ai-content').hidden && $('ai-summary').textContent.includes('Strong fit');
    R['suggested path renders'] = $('path-phases').textContent.includes('Phase 1');
    R['bullet rewrites render'] = $('bullet-list').textContent.includes('new bullet');
    R['AI loader hidden after completion'] = $('ai-loading').hidden === true;
    R['all sidebar links enabled (incl. cover letter)'] =
      ['nav-results', 'nav-recommendations', 'nav-export', 'nav-cover-letter', 'nav-chat'].every((id) => !$(id).classList.contains('is-disabled'));

    $('apply-rewrites-btn').click(); await wait(300);
    R['apply-rewrites works'] = $('apply-rewrites-status').textContent.includes('Applied all 1');
    R['resume download buttons appear'] = !$('apply-rewrites-downloads').hidden;

    $('export-md-btn').click(); $('export-pdf-btn').click(); await wait(300);
    R['markdown + pdf export endpoints called'] = fetchLog.some((u) => u.includes('/export/markdown')) && fetchLog.some((u) => u.includes('/export/pdf'));

    $('chat-launcher').click(); await wait(60);
    R['chat drawer opens via launcher'] = $('chat-panel').classList.contains('is-open');
    $('chat-input').value = 'what first?'; $('chat-send-btn').click(); await wait(300);
    R['chat reply renders'] = $('chat-messages').textContent.includes('Focus on Kubernetes first.');
    doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' })); await wait(350);
    R['Escape closes drawer'] = $('chat-panel').hidden === true;
    $('nav-chat').click(); await wait(60);
    R['sidebar Chat link opens drawer'] = $('chat-panel').classList.contains('is-open');

    await wait(3000);
    R['launch screen dismissed'] = $('launch-screen').hidden === true;

    let all = true;
    for (const [k, v] of Object.entries(R)) { console.log(v ? 'PASS' : 'FAIL', '-', k); if (!v) all = false; }
    console.log(all ? '\nALL PASS' : '\nSOME FAILED');
    process.exit(all ? 0 : 1);
  } catch (e) { console.error('TEST ERROR:', e.stack); process.exit(1); }
});
