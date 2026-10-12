// Classic (non-module) script, loaded in <head> so the saved theme applies before first paint.
// Default (nothing stored, or storage blocked): light. "system" is stored explicitly by Auto.
(function () {
  var t = null;
  try { t = window.localStorage.getItem("harness_manager.theme"); } catch (e) { /* storage blocked: light */ }
  if (t === "dark") document.documentElement.setAttribute("data-theme", "dark");
  else if (t !== "system") document.documentElement.setAttribute("data-theme", "light");
})();
