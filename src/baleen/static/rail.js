/* rail.js: runs synchronously right after <div class="app"> opens, before the rail is painted,
   so the remembered collapse choice never flashes. localStorage is a per-viewer convenience and
   may be unavailable (private window, blocked storage): then the width decides (< 1280 px
   collapsed), as in the design. */
(function () {
  "use strict";
  var app = document.currentScript && document.currentScript.parentElement;
  if (!app) return;
  var pref = null;
  try { pref = window.localStorage.getItem("baleen.rail"); } catch (e) { pref = null; }
  var collapsed = pref === "collapsed" || (pref !== "expanded" && window.innerWidth < 1280);
  if (collapsed) app.classList.add("collapsed");
})();
