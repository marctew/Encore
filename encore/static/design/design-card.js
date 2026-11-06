/* Design Uniformity: the Settings card for the design updater (spec 7.14).
 * Markup: an element with data-design-card (optional data-api, default /api/design,
 * data-badge="badge badge-{tone}" to match the app's badge classes, and data-csrf, sent as the
 * X-CSRF-Token header, for apps that check a CSRF token on every POST), holding any of:
 *   [data-du=version] [data-du=date] [data-du=label] [data-du=checked] [data-du=message] [data-du=warning]
 *   input[data-du=auto]   button[data-du-act=check]   button[data-du-act=rollback]
 * templates/partials/design_card.html is ready-made markup for apps using uniform.css. */
(function () {
  function setup(card) {
    var api = card.dataset.api || "/api/design";
    var badge = card.dataset.badge || "badge badge-{tone}";
    var q = function (k) { return card.querySelector('[data-du="' + k + '"]'); };
    var acts = card.querySelectorAll("[data-du-act]");
    var timer = null, polls = 0;

    function text(k, v) { var el = q(k); if (el) { el.textContent = v || ""; el.hidden = !v; } }

    function show(s) {
      text("version", s.version);
      text("date", s.date);
      text("checked", "Last checked " + s.checked);
      text("message", s.message);
      text("warning", s.warning);
      var lab = q("label");
      if (lab) { lab.textContent = s.label; lab.className = badge.replace("{tone}", s.tone); }
      var auto = q("auto");
      if (auto) auto.checked = !!s.auto;
      acts.forEach(function (b) {
        b.disabled = s.busy || (b.dataset.duAct === "rollback" && !s.can_rollback);
        if (b.dataset.duAct === "rollback") b.textContent = s.previous ? "Roll back to " + s.previous : "Roll back";
      });
      clearTimeout(timer);
      if (s.busy && polls++ < 300) timer = setTimeout(load, 2000);   // a deploy can take a few minutes
    }

    function fail(msg) { text("warning", msg); acts.forEach(function (b) { b.disabled = false; }); }

    function load() {
      fetch(api + "/status", { headers: { Accept: "application/json" } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(show)
        .catch(function () { fail("Can't read the updater's status right now."); });
    }

    function post(path, body) {
      acts.forEach(function (b) { b.disabled = true; });
      polls = 0;
      var headers = { "Content-Type": "application/json" };
      if (card.dataset.csrf) headers["X-CSRF-Token"] = card.dataset.csrf;   // apps that check CSRF on every POST
      return fetch(api + path, {
        method: "POST", headers: headers, body: JSON.stringify(body || {})
      }).then(function (r) {
        return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || r.status); return d; });
      }).then(show).catch(function (e) { fail(e.message || "Couldn't reach the server."); });
    }

    // Rolling back asks first in an inline popover (spec 7.10: never window.confirm()).
    function ask(button, question, yes, then) {
      var wrap = button.parentNode;
      if (!wrap.classList.contains("confirm-wrap")) {
        wrap = document.createElement("span");
        wrap.className = "confirm-wrap";
        button.parentNode.insertBefore(wrap, button);
        wrap.appendChild(button);
      }
      var old = wrap.querySelector(".confirm");
      if (old) { old.remove(); button.setAttribute("aria-expanded", "false"); return; }
      var pop = document.createElement("span");
      pop.className = "confirm left";
      pop.innerHTML = '<span style="display:block"></span><span class="confirm-actions">' +
        '<button type="button" class="btn btn-ghost">Cancel</button><button type="button" class="btn btn-primary"></button></span>';
      pop.firstChild.textContent = question;
      var btns = pop.querySelectorAll("button");
      btns[1].textContent = yes;
      wrap.appendChild(pop);
      if (window.duPlace) window.duPlace(pop, button);      // over the page, above the button (js/confirm.js)
      button.setAttribute("aria-expanded", "true");
      function close() {
        pop.remove(); button.setAttribute("aria-expanded", "false");
        document.removeEventListener("keydown", onKey); document.removeEventListener("click", onOut, true);
      }
      function onKey(e) { if (e.key === "Escape") close(); }
      function onOut(e) { if (!wrap.contains(e.target)) close(); }
      btns[0].addEventListener("click", close);
      btns[1].addEventListener("click", function () { close(); then(); });
      document.addEventListener("keydown", onKey);
      document.addEventListener("click", onOut, true);
      btns[1].focus();
    }

    acts.forEach(function (b) {
      b.addEventListener("click", function () {
        if (b.dataset.duAct === "rollback") {
          ask(b, "Go back to the previous version? Automatic updates turn off until you use Check now.",
              "Roll back", function () { post("/rollback"); });
        } else {
          post("/" + b.dataset.duAct);
        }
      });
    });
    var auto = q("auto");
    if (auto) auto.addEventListener("change", function () { post("/auto", { enabled: auto.checked }); });
    load();
  }
  function init() { document.querySelectorAll("[data-design-card]").forEach(setup); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
