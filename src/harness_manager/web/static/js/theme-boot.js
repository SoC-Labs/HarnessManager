// Classic (non-module) script, loaded in <head> so the saved theme applies before first paint.
(function () {
  try {
    var t = window.localStorage.getItem("harness_manager.theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: follow the system theme */ }
})();
