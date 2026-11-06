/* Design Uniformity: inline confirm (spec 7.10), with no framework.
 *
 * Plain forms: put data-confirm on the form (or on one of its buttons).
 *   <form method="post" data-confirm="Reboot the Pi? Its screens go dark for a minute.">
 *     <button>Reboot</button>
 *   </form>
 * Submitting opens the popover next to the button that was pressed. The danger button says what
 * the submit button says, or data-confirm-yes.
 *
 * Script: await duConfirm(button, question, yes) resolves true for yes, false for Cancel, Escape
 * or a click outside. Use it in place of window.confirm() in click handlers.
 *   if (!(await duConfirm(b, "Delete this for good?", "Delete"))) return;
 *
 * The popover floats over the page (position: fixed), above the button, or below it when there's no room above,
 * so a scrolling table or card never clips it or gains a scrollbar. duPlace(pop, button) does the placing; the
 * Alpine confirm_button macro and design-card.js call it too.
 *
 * Load it once per page: <script src="/static/design/confirm.js" defer></script>
 */
(function () {
  var open = null;
  var GAP = 8, EDGE = 8;

  // Put a .confirm popover above its button (below if it doesn't fit), kept inside the viewport.
  function place(pop, button) {
    if (!pop || !button) return;
    pop.classList.add("is-placed");
    var r = button.getBoundingClientRect(), vw = document.documentElement.clientWidth, vh = window.innerHeight;
    var w = pop.offsetWidth, h = pop.offsetHeight;
    var top = r.top - h - GAP;
    if (top < EDGE) top = Math.min(r.bottom + GAP, vh - h - EDGE);
    var left = pop.classList.contains("left") ? r.left : r.right - w;
    left = Math.max(EDGE, Math.min(left, vw - w - EDGE));
    pop.style.top = Math.max(EDGE, top) + "px";
    pop.style.left = left + "px";
  }
  window.duPlace = place;

  // While a popover is open, keep it with its button as the page or a table scrolls; close it once the button
  // has scrolled out of sight.
  function follow() {
    document.querySelectorAll(".confirm.is-placed").forEach(function (pop) {
      var button = pop.parentNode && pop.parentNode.querySelector("button");   // the wrap's first button opened it
      if (!button || getComputedStyle(pop).display === "none") return;
      var r = button.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) {
        if (open && open.pop === pop) close(false);
        return;
      }
      place(pop, button);
    });
  }
  window.addEventListener("scroll", follow, true);
  window.addEventListener("resize", follow);

  function close(answer) {
    if (!open) return;
    var o = open;
    open = null;
    o.pop.remove();
    o.button.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("click", onOutside, true);
    o.button.focus({ preventScroll: true });
    o.done(!!answer);
  }
  function onKey(e) { if (e.key === "Escape") close(false); }
  function onOutside(e) { if (open && !open.wrap.contains(e.target)) close(false); }

  function popover(button, question, yes, done) {
    close(false);
    var wrap = button.parentNode;
    if (!wrap.classList || !wrap.classList.contains("confirm-wrap")) {
      wrap = document.createElement("span");
      wrap.className = "confirm-wrap";
      button.parentNode.insertBefore(wrap, button);
      wrap.appendChild(button);
    }
    var pop = document.createElement("span");
    pop.className = "confirm";
    pop.setAttribute("role", "dialog");
    pop.innerHTML = '<span style="display:block"></span><span class="confirm-actions">' +
      '<button type="button" class="btn btn-ghost">Cancel</button><button type="button" class="btn btn-danger"></button></span>';
    pop.firstChild.textContent = question;
    var buttons = pop.querySelectorAll("button");
    buttons[1].textContent = yes || (button.textContent || "").trim() || "Yes";
    buttons[0].addEventListener("click", function () { close(false); });
    buttons[1].addEventListener("click", function () { close(true); });
    wrap.appendChild(pop);
    place(pop, button);
    button.setAttribute("aria-expanded", "true");
    open = { pop: pop, button: button, wrap: wrap, done: done };
    document.addEventListener("keydown", onKey);
    document.addEventListener("click", onOutside, true);
    buttons[1].focus();
  }

  window.duConfirm = function (button, question, yes) {
    return new Promise(function (resolve) { popover(button, question, yes, resolve); });
  };

  document.addEventListener("submit", function (e) {
    var form = e.target;
    var button = e.submitter || form.querySelector("button[type=submit], button:not([type]), input[type=submit]");
    var question = (button && button.dataset.confirm) || form.dataset.confirm;
    if (!question) return;
    if (form.dataset.confirmed) { delete form.dataset.confirmed; return; }
    e.preventDefault();
    if (!button) return;
    popover(button, question, button.dataset.confirmYes || form.dataset.confirmYes, function (yes) {
      if (!yes) return;
      form.dataset.confirmed = "1";
      if (form.requestSubmit) form.requestSubmit(button.form === form && button.type === "submit" ? button : undefined);
      else form.submit();
    });
  }, true);
})();
