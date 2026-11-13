# Frontend simulation tests

These run the real `frontend/index.html` + `frontend/app.js` inside jsdom with the
backend mocked. They are not a substitute for looking at the site in a real browser
(jsdom does no layout or painting), but they do catch broken wiring: missing element
IDs, dead buttons, bad state transitions, stale-response bugs.

    npm install jsdom
    node frontend_tests/full_site.test.js
    node frontend_tests/cover_letter.test.js

Each prints PASS/FAIL per check and exits non-zero on any failure. The
"Not implemented: navigation" lines jsdom prints are harmless (it can't follow
download links).
