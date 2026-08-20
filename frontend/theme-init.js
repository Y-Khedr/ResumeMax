// Applied before paint so there's no flash of the wrong theme on load.
// Kept as a separate tiny file (not inline in index.html) specifically so
// the site's Content-Security-Policy can disallow 'unsafe-inline' scripts.
(function () {
  var saved = localStorage.getItem("resumemax-theme");
  if (saved === "light") document.documentElement.setAttribute("data-theme", "light");
})();
