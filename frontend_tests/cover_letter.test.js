const { JSDOM } = require('jsdom');
const fs = require('fs');
const html = fs.readFileSync(require('path').join(__dirname, '..', 'frontend', 'index.html'), 'utf8');
const dom = new JSDOM(html, { url: 'http://127.0.0.1:8000/', runScripts: 'dangerously', pretendToBeVisual: true });
const { window } = dom;
window.Element.prototype.scrollIntoView = () => {};
window.performance = window.performance || { now: () => Date.now() };
window.IntersectionObserver = class { observe() {} disconnect() {} };

const clCalls = [];          // every /api/cover-letter request body
let clHandler = null;        // swapped per step; returns a Response-like or throws
let scanCounter = 0;

window.fetch = async (url, opts) => {
  if (url.includes('/api/analyze')) {
    scanCounter++;
    return { ok: true, json: async () => ({
      score: 70, matched: ['Figma'], missing: ['Kubernetes'], job_phrases: ['Figma', 'Kubernetes'],
      extraction_method: 'native', extraction_warning: null,
      resume_text: `resume text ${scanCounter}`, job_description: `job text ${scanCounter}`,
      match_mode: 'ai', requirements: [],
    })};
  }
  if (url.includes('/api/ai-suggestions')) {
    return { ok: true, json: async () => ({
      ok: true, summary: 'Strong fit overall.', missing_keywords: [], bullet_rewrites: [],
      writing_enhancements: [], readiness_roadmap: { recommended_projects: [], skills_to_learn: [], experience_gaps: '' },
      suggested_path: { overview: '', phases: [] }, error: null,
    })};
  }
  if (url.includes('/api/cover-letter')) {
    const body = JSON.parse(opts.body);
    clCalls.push(body);
    return clHandler(body);
  }
  throw new Error('unexpected fetch ' + url);
};
const okLetter = (text) => ({ ok: true, status: 200, json: async () => ({ ok: true, letter: text, error: null }) });
window.URL.createObjectURL = () => 'blob:fake';
window.URL.revokeObjectURL = () => {};
const wait = (ms) => new Promise(r => setTimeout(r, ms));

window.addEventListener('load', async () => {
  try {
    const s = window.document.createElement('script');
    s.textContent = fs.readFileSync(require('path').join(__dirname, '..', 'frontend', 'app.js'), 'utf8');
    window.document.body.appendChild(s);
    await wait(50);
    const doc = window.document;
    const $ = (id) => doc.getElementById(id);
    const checks = {};

    async function runScan() {
      const input = $('resume-input');
      Object.defineProperty(input, 'files', {
        value: [new window.File(['%PDF-1.4'], 'cv.pdf', { type: 'application/pdf' })], configurable: true });
      input.dispatchEvent(new window.Event('change'));
      $('job-input').value = 'job';
      $('scan-btn').click();
      await wait(1300);
    }

    await runScan();
    checks['cover letter nav enabled after scan'] = !$('nav-cover-letter').classList.contains('is-disabled');
    checks['starts in the ready state'] = !$('cover-letter-ready').hidden && $('cover-letter-result').hidden;

    // 1) generate
    clHandler = async () => okLetter('Dear Hiring Manager,\n\nFirst draft.');
    $('cover-letter-generate-btn').click();
    await wait(30);
    checks['request carries resume, job, summary and null tone'] =
      clCalls[0].resume_text === 'resume text 1' && clCalls[0].job_description === 'job text 1' &&
      clCalls[0].analysis_summary === 'Strong fit overall.' && clCalls[0].tone === null;
    checks['letter lands in the editable textarea'] = $('cover-letter-textarea').value.startsWith('Dear Hiring Manager');
    checks['result visible, loading hidden, status generated'] =
      !$('cover-letter-result').hidden && $('cover-letter-loading').hidden && $('cover-letter-status').textContent === 'generated';

    // 2) tone chip on, then off
    const concise = [...doc.querySelectorAll('.tone-chip')].find(c => c.dataset.tone === 'concise');
    clHandler = async () => okLetter('Dear Hiring Manager,\n\nShort draft.');
    concise.click(); await wait(30);
    checks['tone chip sends its key and becomes active'] =
      clCalls[1].tone === 'concise' && concise.classList.contains('is-active');
    concise.click(); await wait(30);
    checks['clicking the active tone again clears it'] =
      clCalls[2].tone === null && !concise.classList.contains('is-active');

    // 3) edit protection
    const ta = $('cover-letter-textarea');
    ta.value = ta.value + '\n\nMY OWN EDIT';
    const before = clCalls.length;
    window.confirm = () => false;
    $('cover-letter-regenerate-btn').click(); await wait(30);
    checks['declining the confirm sends nothing and keeps the edit'] =
      clCalls.length === before && ta.value.includes('MY OWN EDIT');
    window.confirm = () => true;
    clHandler = async () => okLetter('Dear Hiring Manager,\n\nFresh draft.');
    $('cover-letter-regenerate-btn').click(); await wait(30);
    checks['accepting the confirm regenerates'] = clCalls.length === before + 1 && ta.value.includes('Fresh draft');

    // 4) failed regenerate keeps the letter
    clHandler = async () => ({ ok: true, status: 200, json: async () => ({ ok: false, letter: '', error: 'Gemini is temporarily overloaded.' }) });
    $('cover-letter-regenerate-btn').click(); await wait(30);
    checks['server error is shown'] = !$('cover-letter-error').hidden && $('cover-letter-error').textContent.includes('overloaded');
    checks['the previous letter stays on screen after a failure'] =
      !$('cover-letter-result').hidden && ta.value.includes('Fresh draft');
    checks['buttons are usable again after a failure'] = $('cover-letter-regenerate-btn').disabled === false;

    // 5) network failure
    clHandler = async () => { throw new TypeError('Failed to fetch'); };
    $('cover-letter-regenerate-btn').click(); await wait(30);
    checks['network failure shows a clear message'] = $('cover-letter-error').textContent.includes("Couldn't reach");

    // 6) copy + download don't blow up
    $('cover-letter-copy-btn').click(); await wait(30);
    checks['copy reports an outcome either way'] = $('cover-letter-copy-status').textContent.length > 0;
    let blobSeen = false;
    window.URL.createObjectURL = (b) => { blobSeen = b instanceof window.Blob; return 'blob:x'; };
    $('cover-letter-download-btn').click();
    checks['download builds a text blob'] = blobSeen;

    // 7) stale response after a rescan must be ignored
    let release;
    clHandler = () => new Promise((resolve) => { release = () => resolve(okLetter('STALE LETTER FROM OLD SCAN')); });
    $('cover-letter-regenerate-btn').click(); await wait(10);
    await runScan();                  // new scan resets the panel while the old request is still pending
    release(); await wait(40);
    checks['rescan resets the panel to the ready state'] =
      !$('cover-letter-ready').hidden && $('cover-letter-result').hidden;
    checks['a late response from the previous scan is ignored'] = !ta.value.includes('STALE') && ta.value === '';
    clHandler = async () => okLetter('New scan letter');
    $('cover-letter-generate-btn').click(); await wait(30);
    checks['new scan uses the new resume/job text'] = clCalls[clCalls.length - 1].resume_text === 'resume text 2';

    let all = true;
    for (const [k, v] of Object.entries(checks)) { console.log(v ? 'PASS' : 'FAIL', '-', k); if (!v) all = false; }
    console.log(all ? '\nALL PASS' : '\nSOME FAILED');
    process.exit(all ? 0 : 1);
  } catch (e) { console.error('TEST ERROR:', e.stack); process.exit(1); }
});
