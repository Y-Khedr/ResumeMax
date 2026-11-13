const { JSDOM } = require('jsdom');
const FRONT = require('path').join(__dirname, '..', 'frontend');
const fs = require('fs');
const html = fs.readFileSync(FRONT + '/index.html', 'utf8');
const dom = new JSDOM(html, { url: 'http://127.0.0.1:8000/', runScripts: 'dangerously', pretendToBeVisual: true });
const { window } = dom;
window.Element.prototype.scrollIntoView = () => {};
window.performance = window.performance || { now: () => Date.now() };
window.IntersectionObserver = class { observe() {} disconnect() {} };

let clipboardText = null;
Object.defineProperty(window.navigator, 'clipboard', { value: { writeText: async (t) => { clipboardText = t; } } });
let confirmAnswer = true, confirmAsked = 0;
window.confirm = () => { confirmAsked++; return confirmAnswer; };
let downloadedBlob = null;
window.URL.createObjectURL = (b) => { downloadedBlob = b; return 'blob:fake'; };
window.URL.revokeObjectURL = () => {};
window.HTMLAnchorElement.prototype.click = function () { window.__lastDownloadName = this.download; };

const clCalls = [];
let clMode = 'ok';            // ok | error | slow
let releaseSlow = null;
let letterCounter = 0;
window.fetch = async (url, opts) => {
  if (url.includes('/api/analyze')) return { ok: true, json: async () => ({
    score: 70, matched: ['A'], missing: ['B'], job_phrases: ['A','B'], extraction_method: 'native',
    extraction_warning: null, resume_text: 'RESUME-' + (++letterCounter), job_description: 'JOB-' + letterCounter }) };
  if (url.includes('/api/ai-suggestions')) return { ok: true, json: async () => ({ ok: true, summary: 'Strong fit.',
    missing_keywords: [], bullet_rewrites: [], writing_enhancements: [],
    readiness_roadmap: { recommended_projects: [], skills_to_learn: [], experience_gaps: '' },
    suggested_path: { overview: '', phases: [] }, error: null }) };
  if (url.includes('/api/cover-letter')) {
    const body = JSON.parse(opts.body); clCalls.push(body);
    if (clMode === 'error') return { ok: true, json: async () => ({ ok: false, letter: '', error: 'Gemini is temporarily overloaded.' }) };
    if (clMode === 'slow') { await new Promise(r => { releaseSlow = r; }); }
    return { ok: true, json: async () => ({ ok: true, letter: 'LETTER v' + clCalls.length + ' tone=' + body.tone, error: null }) };
  }
  throw new Error('unexpected fetch ' + url);
};

const wait = (ms) => new Promise(r => setTimeout(r, ms));
window.addEventListener('load', async () => {
  try {
    const s = window.document.createElement('script');
    s.textContent = fs.readFileSync(FRONT + '/app.js', 'utf8');
    window.document.body.appendChild(s);
    await wait(50);
    const doc = window.document; const $ = (id) => doc.getElementById(id);
    const R = {};

    R['nav link disabled before any scan'] = $('nav-cover-letter').classList.contains('is-disabled');

    async function scan() {
      const f = $('resume-input');
      Object.defineProperty(f, 'files', { value: [new window.File(['%PDF-1.4'], 'cv.pdf', { type: 'application/pdf' })], configurable: true });
      f.dispatchEvent(new window.Event('change'));
      $('job-input').value = 'job text';
      $('scan-btn').click();
      await wait(1300);
    }
    await scan();

    R['nav link enabled after scan'] = !$('nav-cover-letter').classList.contains('is-disabled');
    R['starts in ready state'] = !$('cover-letter-ready').hidden && $('cover-letter-result').hidden;

    $('cover-letter-generate-btn').click();
    // sampled synchronously: the handler flips to the loading state before its first await
    R['loading state shown the instant generation starts'] =
      !$('cover-letter-loading').hidden && $('cover-letter-status').textContent === 'writing' &&
      $('cover-letter-generate-btn').disabled && $('cover-letter-ready').hidden;
    await wait(100);
    R['letter appears in an editable textarea'] = !$('cover-letter-result').hidden && $('cover-letter-textarea').value.startsWith('LETTER v1');
    R['status shows generated'] = $('cover-letter-status').textContent === 'generated';
    R['request carries the scan\'s resume + job + summary'] =
      clCalls[0].resume_text === 'RESUME-1' && clCalls[0].job_description === 'JOB-1' &&
      clCalls[0].analysis_summary === 'Strong fit.' && clCalls[0].tone === null;

    // tone chip: apply, then clear by clicking again
    const concise = doc.querySelector('[data-tone="concise"]');
    concise.click(); await wait(100);
    R['tone chip sends its tone and becomes active'] = clCalls[1].tone === 'concise' && concise.classList.contains('is-active');
    concise.click(); await wait(100);
    R['clicking the active chip clears the tone'] = clCalls[2].tone === null && !concise.classList.contains('is-active');

    // edit protection
    $('cover-letter-textarea').value = 'MY EDITED VERSION';
    confirmAnswer = false; const before = clCalls.length;
    $('cover-letter-regenerate-btn').click(); await wait(100);
    R['declining the prompt keeps edits and sends no request'] =
      $('cover-letter-textarea').value === 'MY EDITED VERSION' && clCalls.length === before && confirmAsked === 1;
    confirmAnswer = true;
    $('cover-letter-regenerate-btn').click(); await wait(100);
    R['confirming replaces the edits with a new letter'] =
      clCalls.length === before + 1 && $('cover-letter-textarea').value.startsWith('LETTER v');

    // untouched letter regenerates without nagging
    const asked = confirmAsked;
    $('cover-letter-regenerate-btn').click(); await wait(100);
    R['no confirm prompt when the letter is unedited'] = confirmAsked === asked;

    // copy + download
    $('cover-letter-textarea').value = 'FINAL TEXT TO SHIP';
    $('cover-letter-copy-btn').click(); await wait(50);
    R['copy puts the textarea contents on the clipboard'] = clipboardText === 'FINAL TEXT TO SHIP' && $('cover-letter-copy-status').textContent === 'Copied.';
    $('cover-letter-download-btn').click(); await wait(50);
    const dlText = downloadedBlob ? await new Promise(r => { const fr = new window.FileReader(); fr.onload = () => r(fr.result); fr.readAsText(downloadedBlob); }) : null;
    R['download is the edited text, named .txt'] = dlText === 'FINAL TEXT TO SHIP' && window.__lastDownloadName === 'cover_letter.txt';

    // error handling
    clMode = 'error';
    $('cover-letter-textarea').value = 'FINAL TEXT TO SHIP'; // resync so it isn't treated as an edit
    confirmAnswer = true;
    $('cover-letter-regenerate-btn').click(); await wait(100);
    R['error is shown and the existing letter is kept'] =
      !$('cover-letter-error').hidden && $('cover-letter-error').textContent.includes('overloaded') &&
      !$('cover-letter-result').hidden;
    R['buttons are usable again after an error'] = !$('cover-letter-regenerate-btn').disabled;
    clMode = 'ok';

    // stale response: start a slow request, then run a new scan before it returns
    clMode = 'slow';
    $('cover-letter-regenerate-btn').click(); await wait(50);
    R['loading stays visible while the response is slow'] =
      !$('cover-letter-loading').hidden && $('cover-letter-regenerate-btn').disabled &&
      Array.from(doc.querySelectorAll('.tone-chip')).every(c => c.disabled);
    await scan();   // new scan -> resetCoverLetter
    releaseSlow(); await wait(100);
    R['a late response from the previous scan is ignored'] =
      $('cover-letter-textarea').value === '' && $('cover-letter-result').hidden && !$('cover-letter-ready').hidden;
    R['new scan resets the panel to ready'] = $('cover-letter-status').textContent === 'optional' && !$('cover-letter-generate-btn').disabled;
    clMode = 'ok';
    $('cover-letter-generate-btn').click(); await wait(100);
    const last = clCalls[clCalls.length - 1];
    R['next letter uses the NEW scan\'s text'] = last.resume_text === 'RESUME-2' && last.job_description === 'JOB-2';

    let all = true;
    for (const [k, v] of Object.entries(R)) { console.log(v ? 'PASS' : 'FAIL', '-', k); if (!v) all = false; }
    console.log(all ? '\nALL PASS' : '\nSOME FAILED');
    process.exit(all ? 0 : 1);
  } catch (e) { console.error('TEST ERROR', e.stack); process.exit(1); }
});
