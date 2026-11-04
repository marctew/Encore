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
 * Load it once per page: <script src="/static/design/confirm.js" defer></script>
 */
(function () {
  var open = null;

  function close(answer) {
    if (!open) return;
    var o = open;
    open = null;
    o.pop.remove();
    o.button.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("click", onOutside, true);
    o.button.focus();
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
    var left = button.getBoundingClientRect().left < 320;   // near the left edge: open rightwards
    var pop = document.createElement("span");
    pop.className = "confirm" + (left ? " left" : "");
    pop.setAttribute("role", "dialog");
    pop.innerHTML = '<span style="display:block"></span><span class="confirm-actions">' +
      '<button type="button" class="btn btn-ghost">Cancel</button><button type="button" class="btn btn-danger"></button></span>';
    pop.firstChild.textContent = question;
    var buttons = pop.querySelectorAll("button");
    buttons[1].textContent = yes || (button.textContent || "").trim() || "Yes";
    buttons[0].addEventListener("click", function () { close(false); });
    buttons[1].addEventListener("click", function () { close(true); });
    wrap.appendChild(pop);
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
