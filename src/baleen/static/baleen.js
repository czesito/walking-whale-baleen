/* baleen.js: the small vanilla module of design §09 and handoff §12. htmx does the requests and
   swaps; this file adds dialogs (focus trap, initial focus per D-17), toasts, the inspector keys
   (↑↓, Esc, focus return), table and segmented-control keys, rail collapse and the drawer,
   polling with the connection-lost banner (UI-G3), the tab title and the opt-in notification
   (UI-G2), and polite live-region announcements. No inline scripts (SEC-6). */
(function () {
  "use strict";

  var LIVE = { preparing: 1, running: 1, finishing: 1 };
  var FINAL = { completed: 1, cancelled: 1, stopped: 1 };
  var body = document.body;
  var app = document.getElementById("app");

  function $(id) { return document.getElementById(id); }
  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (e) { /* storage unavailable: a per-viewer convenience only */ }
    return null;
  }

  /* ------------------------------------------------------------ live region & toast */
  function say(text) {
    var el = $("live");
    if (!el || !text) return;
    el.textContent = "";
    window.setTimeout(function () { el.textContent = text; }, 40);
  }
  var toastTimer = 0;
  function toast(text) {
    var el = $("toast");
    if (!el || !text) return;
    el.innerHTML = '<svg class="ic" aria-hidden="true"><use href="#i-tick"/></svg>';
    el.appendChild(document.createTextNode(text));
    el.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () { el.classList.remove("show"); }, 2600);
  }

  /* ------------------------------------------------------------ rail collapse & drawer */
  function railCollapsed() {
    var pref = store("baleen.rail");
    if (pref === "collapsed") return true;
    if (pref === "expanded") return false;
    return window.innerWidth < 1280;
  }
  function syncCollapseButton() {
    var b = $("rail-collapse");
    if (!b || !app) return;
    var c = app.classList.contains("collapsed");
    b.setAttribute("aria-label", c ? "Expand sidebar" : "Collapse sidebar");
    b.setAttribute("data-tip", c ? "Expand" : "Collapse");
    var use = b.querySelector("use");
    if (use) use.setAttribute("href", c ? "#i-expand" : "#i-collapse");
  }
  function applyRail() {
    if (!app) return;
    app.classList.toggle("collapsed", railCollapsed());
    syncCollapseButton();
  }
  function toggleRail() {
    var c = !app.classList.contains("collapsed");
    store("baleen.rail", c ? "collapsed" : "expanded");
    app.classList.toggle("collapsed", c);
    syncCollapseButton();
  }
  function openDrawer() {
    app.classList.add("drawer");
    var b = $("drawer-open");
    if (b) b.setAttribute("aria-expanded", "true");
    window.setTimeout(function () {
      var first = document.querySelector("#rail .rail-item");
      if (first) first.focus();
    }, 30);
  }
  function closeDrawer(returnFocus) {
    if (!app.classList.contains("drawer")) return;
    app.classList.remove("drawer");
    var b = $("drawer-open");
    if (b) {
      b.setAttribute("aria-expanded", "false");
      if (returnFocus) b.focus();
    }
  }

  /* ------------------------------------------------------------ dialogs */
  var lastFocus = null;
  function overlayOpen() { var o = $("overlay"); return !!(o && o.classList.contains("open")); }
  function focusables(root) {
    return Array.prototype.filter.call(
      root.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"),
      function (el) { return !el.disabled && el.offsetParent !== null; });
  }
  function openModal(opener) {
    var m = $("modal");
    if (!m) return;
    if (!overlayOpen()) lastFocus = opener || document.activeElement;
    $("overlay").classList.add("open");
    if (window.htmx) window.htmx.process(m);
    window.setTimeout(function () {
      var b = m.querySelector("[data-autofocus]") || m.querySelector(".btn.primary");
      if (b) b.focus();
    }, 30);
  }
  function openTemplate(id, opener) {
    var t = $(id);
    var m = $("modal");
    if (!t || !m) return;
    m.innerHTML = "";
    m.appendChild(t.content.cloneNode(true));
    if (id === "tpl-quit") {
      var rj = $("railjob");
      var running = !!(rj && LIVE[rj.getAttribute("data-state")]);
      Array.prototype.forEach.call(m.querySelectorAll("[data-when]"), function (el) {
        if (el.getAttribute("data-when") !== (running ? "running" : "idle")) el.remove();
      });
    }
    openModal(opener);
  }
  function closeModal() {
    var o = $("overlay");
    if (!o || !o.classList.contains("open")) return;
    o.classList.remove("open");
    $("modal").innerHTML = "";
    var back = lastFocus;
    lastFocus = null;
    if (back && document.contains(back)) back.focus();
  }
  function trapTab(e) {
    var items = focusables($("modal"));
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (!$("modal").contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  }

  /* ------------------------------------------------------------ menu (run page "more") */
  function closeMenus(returnFocus) {
    Array.prototype.forEach.call(document.querySelectorAll(".menu:not([hidden])"), function (m) {
      m.hidden = true;
      var b = document.querySelector("[aria-controls='" + m.id + "']");
      if (b) {
        b.setAttribute("aria-expanded", "false");
        if (returnFocus) b.focus();
      }
    });
  }
  function toggleMenu(btn) {
    var m = $(btn.getAttribute("aria-controls"));
    if (!m) return;
    var open = m.hidden;
    closeMenus(false);
    if (open) {
      m.hidden = false;
      btn.setAttribute("aria-expanded", "true");
      var first = m.querySelector(".menu-item");
      if (first) first.focus();
    }
  }

  /* ------------------------------------------------------------ copy */
  function copyText(text, message) {
    function done() { toast(message); }
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.className = "sr";
      document.body.appendChild(ta);
      ta.select();
      try { if (document.execCommand("copy")) done(); } catch (e) { /* nothing to copy with */ }
      ta.remove();
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  }

  /* ------------------------------------------------------------ inspector (UI-R5) */
  function inspector() { return $("inspector"); }
  function inspectorOpen() { var i = inspector(); return !!(i && i.classList.contains("open")); }
  function selectRow(n) {
    Array.prototype.forEach.call(document.querySelectorAll("tr.sel"), function (r) {
      r.classList.remove("sel");
      r.setAttribute("aria-selected", "false");
    });
    var row = n ? $("row-" + n) : null;
    if (row) {
      row.classList.add("sel");
      row.setAttribute("aria-selected", "true");
    }
  }
  function openInspector() {
    var head = $("insp-head");
    if (!head) return;
    inspector().classList.add("open");
    $("main").classList.add("docked");
    selectRow(head.getAttribute("data-n"));
  }
  function closeInspector() {
    var insp = inspector();
    if (!insp || !insp.classList.contains("open")) return;
    var head = $("insp-head");
    var n = head ? head.getAttribute("data-n") : null;
    insp.classList.remove("open");
    $("main").classList.remove("docked");
    selectRow(null);
    var row = n ? $("row-" + n) : null;
    if (row) row.focus();
    window.setTimeout(function () { if (!insp.classList.contains("open")) insp.innerHTML = ""; }, 320);
  }
  function moveRow(row, down) {
    var next = down ? row.nextElementSibling : row.previousElementSibling;
    while (next && !next.matches("tr[data-n], tr[data-href]")) next = down ? next.nextElementSibling : next.previousElementSibling;
    if (!next) return;
    next.focus();
    if (next.hasAttribute("data-n") && inspectorOpen()) next.click();
  }

  /* ------------------------------------------------------------ tab title & finish (UI-G2) */
  var lastJobState = null;
  var doneTitle = "";
  function updateTitle() {
    var rj = $("railjob");
    var pageTitle = body.getAttribute("data-page-title") || document.title;
    var progress = body.getAttribute("data-progress-title") === "true";
    if (doneTitle && progress) { document.title = doneTitle; return; }
    if (progress && rj && LIVE[rj.getAttribute("data-state")]) {
      document.title = rj.getAttribute("data-pct") + "% · " + rj.getAttribute("data-wf") + " · Baleen";
    } else {
      document.title = pageTitle;
    }
  }
  function onFinish(rj) {
    var wf = rj.getAttribute("data-wf");
    doneTitle = "✓ Done · " + wf + " · Baleen";
    var text = rj.getAttribute("data-finish") || (wf + " finished");
    var url = rj.getAttribute("data-url");
    if (url !== window.location.pathname) say(text);
    if (body.getAttribute("data-notify") === "true" && "Notification" in window &&
        window.Notification.permission === "granted") {
      try {
        var n = new window.Notification(text);
        n.onclick = function () { window.focus(); window.location.href = url; n.close(); };
      } catch (e) { /* notifications unavailable */ }
    }
  }
  function checkJob() {
    var rj = $("railjob");
    if (!rj) return;
    var state = rj.getAttribute("data-state") || "";
    if (lastJobState !== null && LIVE[lastJobState] && FINAL[state]) onFinish(rj);
    lastJobState = state;
    updateTitle();
  }
  window.addEventListener("focus", function () {
    if (doneTitle) { doneTitle = ""; updateTitle(); }
  });

  /* ------------------------------------------------------------ announcements & fields */
  var lastQuartile = {};
  function refresh() {
    Array.prototype.forEach.call(document.querySelectorAll(".msg[id^='f-']"), function (msg) {
      var field = msg.closest(".field");
      var error = msg.getAttribute("data-state") === "error";
      if (field) field.classList.toggle("error", error);
      var input = $(msg.id.slice(0, -4));
      if (input) {
        if (error) input.setAttribute("aria-invalid", "true");
        else input.removeAttribute("aria-invalid");
      }
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-announce]"), function (el) {
      say(el.getAttribute("data-announce"));
      el.removeAttribute("data-announce");
    });
    var lp = $("live-progress");
    var live = $("run-live");
    if (lp && live && lp.getAttribute("data-state") === "running") {
      var run = live.getAttribute("data-run");
      var q = Math.floor((+lp.getAttribute("data-pct") || 0) / 25);
      if (lastQuartile[run] === undefined) lastQuartile[run] = q;
      else if (q > lastQuartile[run] && q < 4) {
        lastQuartile[run] = q;
        say(q * 25 + "% · " + lp.getAttribute("data-done") + " of " + lp.getAttribute("data-total") + " files");
      }
    }
    checkJob();
  }
  var refreshTimer = 0;
  function scheduleRefresh() {
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(refresh, 30);
  }

  /* ------------------------------------------------------------ polling & connection (UI-G3) */
  var failures = 0;
  var lost = false;
  var pingTimer = 0;
  function isPoll(elt) { return !!(elt && elt.hasAttribute && elt.hasAttribute("data-poll")); }
  function pollTick() {
    if (!window.htmx) return;
    var now = Date.now();
    Array.prototype.forEach.call(document.querySelectorAll("[data-poll]"), function (el) {
      if (el.classList.contains("htmx-request")) return;
      var every = +el.getAttribute("data-poll") || 1000;
      if (document.hidden) every = Math.max(every, +(el.getAttribute("data-poll-hidden") || 3000));
      if (lost) every = 5000;
      if (!el._baleenNext) { el._baleenNext = now + every; return; }
      if (now >= el._baleenNext) {
        el._baleenNext = now + every;
        window.htmx.trigger(el, "baleen:poll");
      }
    });
  }
  function pollNow() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-poll]"), function (el) { el._baleenNext = 1; });
  }
  function ping() {
    if (document.querySelector("[data-poll]")) return; /* the polls retry by themselves */
    window.fetch("/job", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { if (r.ok) connected(); }).catch(function () { /* still lost */ });
  }
  function showLost() {
    if (lost) return;
    lost = true;
    var b = $("lost-banner");
    if (b) b.hidden = false;
    pingTimer = window.setInterval(ping, 5000);
  }
  function connected() {
    failures = 0;
    if (!lost) return;
    lost = false;
    var b = $("lost-banner");
    if (b) b.hidden = true;
    window.clearInterval(pingTimer);
  }
  function failed(fromPoll) {
    failures += 1;
    if (!fromPoll || failures >= 3) showLost();
  }

  /* ------------------------------------------------------------ events */
  document.addEventListener("click", function (e) {
    var t = e.target.closest ? e.target.closest("[data-act], [data-copy], [data-toast], tr[data-href]") : null;
    if (!t) {
      if (!e.target.closest || !e.target.closest(".menu")) closeMenus(false);
      return;
    }
    if (t.hasAttribute("data-copy")) {
      copyText(t.getAttribute("data-copy"), t.getAttribute("data-toast") || "Copied");
      closeMenus(false);
      return;
    }
    if (t.matches("tr[data-href]")) {
      if (!e.target.closest("a, button")) window.location.href = t.getAttribute("data-href");
      return;
    }
    var act = t.getAttribute("data-act");
    if (!act && t.hasAttribute("data-toast")) { toast(t.getAttribute("data-toast")); return; }
    switch (act) {
      case "collapse": toggleRail(); break;
      case "drawer-open": openDrawer(); break;
      case "drawer-close": closeDrawer(true); break;
      case "quit": closeDrawer(false); openTemplate("tpl-quit", t); break;
      case "cancel": openTemplate("tpl-cancel", t); break;
      case "dialog": openTemplate(t.getAttribute("data-template"), t); break;
      case "closemodal": closeModal(); break;
      case "closeinsp": closeInspector(); break;
      case "menu": toggleMenu(t); break;
      case "reconnect": pollNow(); ping(); break;
      default: break;
    }
  });

  /* The notification permission must be asked from the click itself (UI-G2, §11). */
  document.addEventListener("click", function (e) {
    var sw = e.target.closest ? e.target.closest("[data-pref='notify']") : null;
    if (!sw || sw.getAttribute("aria-checked") === "true") return;
    if ("Notification" in window && window.Notification.permission === "default") {
      toast("The browser will ask for permission");
      try { window.Notification.requestPermission(); } catch (err) { /* unsupported */ }
    }
  }, true);

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (overlayOpen()) { e.preventDefault(); closeModal(); return; }
      if (document.querySelector(".menu:not([hidden])")) { closeMenus(true); return; }
      if (inspectorOpen()) { e.preventDefault(); closeInspector(); return; }
      if (app && app.classList.contains("drawer")) { closeDrawer(true); return; }
      return;
    }
    if (e.key === "Tab" && overlayOpen()) { trapTab(e); return; }
    var t = e.target;
    if (!t || !t.closest) return;
    var editing = t.matches("input, select, textarea");
    var row = t.closest("tr[data-n], tr[data-href]");
    if (row && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); moveRow(row, e.key === "ArrowDown"); return; }
    if (row && e.key === "Enter" && t === row) { e.preventDefault(); row.click(); return; }
    if (!editing && inspectorOpen() && inspector().contains(t) && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      var sel = document.querySelector("tr.sel");
      if (sel) { e.preventDefault(); moveRow(sel, e.key === "ArrowDown"); }
      return;
    }
    var seg = t.closest(".seg");
    if (seg && t.tagName === "BUTTON" && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
      var buttons = Array.prototype.slice.call(seg.querySelectorAll("button"));
      var i = buttons.indexOf(t) + (e.key === "ArrowRight" ? 1 : -1);
      if (i >= 0 && i < buttons.length) { e.preventDefault(); buttons[i].focus(); buttons[i].click(); }
      return;
    }
    var menu = t.closest(".menu");
    if (menu && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      var items = Array.prototype.slice.call(menu.querySelectorAll(".menu-item"));
      var j = items.indexOf(t) + (e.key === "ArrowDown" ? 1 : -1);
      if (j >= 0 && j < items.length) { e.preventDefault(); items[j].focus(); }
      return;
    }
    if (e.key === "/" && !editing && $("rows-q")) { e.preventDefault(); $("rows-q").focus(); }
  });

  document.addEventListener("input", function (e) {
    var r = e.target;
    if (r && r.classList && r.classList.contains("range")) {
      var unit = r.getAttribute("data-unit") || "";
      r.setAttribute("aria-valuetext", r.value + " " + unit);
      var lbl = $(r.id + "-lbl");
      if (lbl) lbl.textContent = r.value + " " + unit;
    }
  });

  window.addEventListener("resize", function () { if (store("baleen.rail") === null) applyRail(); });

  body.addEventListener("htmx:afterSettle", function (e) {
    if (e.detail && e.detail.target && e.detail.target.id === "inspector") openInspector();
    refresh();
  });
  body.addEventListener("htmx:oobAfterSwap", scheduleRefresh);
  body.addEventListener("htmx:afterRequest", function (e) {
    if (e.detail && e.detail.successful) connected();
    scheduleRefresh();
  });
  body.addEventListener("htmx:sendError", function (e) { failed(isPoll(e.detail && e.detail.elt)); });
  body.addEventListener("htmx:timeout", function (e) { failed(isPoll(e.detail && e.detail.elt)); });
  body.addEventListener("htmx:responseError", function (e) {
    var status = e.detail && e.detail.xhr ? e.detail.xhr.status : 0;
    if (status === 403) { window.location.reload(); return; }
    if (status === 0 || status >= 500) failed(isPoll(e.detail.elt));
  });

  /* Server-sent events (HX-Trigger response headers). */
  body.addEventListener("baleen:toast", function (e) { toast(e.detail && e.detail.value); });
  body.addEventListener("baleen:modal-open", function () { openModal(document.activeElement); });
  body.addEventListener("baleen:modal-close", function () { closeModal(); });
  body.addEventListener("baleen:poll-now", function () { pollNow(); });
  body.addEventListener("baleen:set-value", function (e) {
    var v = e.detail && e.detail.value;
    var input = v ? $(v.id) : null;
    if (input) input.value = v.value;
  });
  body.addEventListener("baleen:prefs", function (e) {
    var v = (e.detail && e.detail.value) || {};
    body.setAttribute("data-notify", v.notify ? "true" : "false");
    body.setAttribute("data-progress-title", v.title ? "true" : "false");
    updateTitle();
  });

  applyRail();
  window.setInterval(pollTick, 200);
  refresh();
})();
